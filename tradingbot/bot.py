"""TradingBot: verbindt feeds, agents, beslissing, risk en uitvoering. `tick()` is los aanroepbaar (live en replay)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Callable, Optional

import numpy as np

from .agents import universe as uni
from .agents.base import Advice, Regime
from .agents.edge_lgbm import AGENT_ID as EDGE_ID, EdgeAgent
from .agents.order_flow import OrderFlowAgent
from .agents.position_exit import PositionExitAgent
from .agents.regime import RegimeAgent, btc_shock
from .config import Settings
from .costs import roundtrip_cost_bps
from .decision import Decision, DecisionAgent
from .exchange.base import ExchangeError, Instrument, TradingGateway
from .execution import ExecutionEngine
from .feedback import Feedback
from .health import Health
from .marketdata import MarketState
from .model import ModelBundle, load_current
from .portfolio import ClosedTrade, Portfolio
from .recorder import Recorder
from .risk import RiskContext, RiskManager
from .storage import Storage

log = logging.getLogger(__name__)

FRAME_BARS = 400


class TradingBot:
    def __init__(self, settings: Settings, now_ms: Callable[[], int], storage: Storage, portfolio: Portfolio,
                 gateway: TradingGateway, instruments: dict[str, Instrument], bundle: Optional[ModelBundle] = None,
                 rest=None, recorder: Optional[Recorder] = None, feed=None, use_threads: bool = True,
                 ms: Optional[MarketState] = None, fee_rest=None):
        self.s = settings
        self.now_ms = now_ms
        self.db, self.pf, self.gw, self.rest, self.feed = storage, portfolio, gateway, rest, feed
        # alleen-lezen client voor GET trade-rate (accounttarief)
        self.fee_rest = fee_rest
        self.instruments = instruments
        self.use_threads = use_threads
        self.ms = ms or MarketState(now_ms)
        self.risk = RiskManager(settings)
        self.engine = ExecutionEngine(
            settings, gateway, portfolio, self.risk, storage, instruments, now_ms)
        self.feedback = Feedback(settings, storage, self.ms, now_ms)
        self.flow = OrderFlowAgent(
            settings.agents, calibrate=self.feedback.flow_cal)
        self.edge = EdgeAgent(settings, bundle)
        self.regime = RegimeAgent(settings)
        self.exit_agent = PositionExitAgent(settings.agents)
        self.decider = DecisionAgent(settings)
        self.recorder = recorder
        self.health = Health(settings)
        self.selector = uni.UniverseSelector(settings.universe)
        self.active: list[str] = []
        self.exit_only: set[str] = set()
        self.depth_ref: dict[str, float] = {}
        # bv. alleen demo-handelbare paren
        self.symbol_filter: Optional[set[str]] = None
        self.fee_bps: dict[str, float] = {}
        self.edge_cache: dict[str, Advice] = {}
        self.edge_used: dict[str, int] = {}
        self._edge_queue: set[str] = set()
        self.counters: dict[str, int] = defaultdict(int)
        self.started_ms = now_ms()
        self._last_eval = 0
        self._last_equity_mark = 0
        self._last_persist = 0
        self._subs_dirty = False
        self._tasks: list[asyncio.Task] = []
        self.ms.on_bar.append(self._on_bar)
        self.pf.on_closed = self._on_trade_closed
        self.pf.lot = {s: i.lot for s, i in instruments.items()}
        status = "active" if (
            bundle is not None and bundle.passed_gate) or settings.bot.force_model_active else "shadow"
        self.feedback.set_initial(EDGE_ID, status)
        self.state_key = f"portfolio:{settings.exchange.environment}:{settings.bot.mode}"

    # ---------------------------------------------------------------- invoer
    def on_ws(self, channel: str, symbol: str, action: str, data: list) -> None:
        self.ms.on_ws(channel, symbol, action, data)
        if self.recorder:
            self.recorder.on_ws(channel, symbol, action, data)

    def _on_bar(self, symbol: str, bar) -> None:
        if symbol in self.active or symbol in self.pf.positions or symbol in self.exit_only:
            self._edge_queue.add(symbol)

    def _on_trade_closed(self, ct: ClosedTrade) -> None:
        self.db.insert("trades_closed", {
            "instrument": ct.symbol, "opened_ms": ct.opened_ms, "closed_ms": ct.closed_ms, "qty": ct.qty,
            "entry_price": ct.entry_price, "exit_price": ct.exit_price, "pnl_usdt": ct.pnl_usdt,
            "pnl_bps": ct.pnl_bps, "reason": ct.reason, "agent_versions": ct.agents})
        self._subs_dirty = True
        self.counters["trades_closed"] += 1

    # -------------------------------------------------------------- universum
    def set_universe(self, active: list[str], exit_only: Optional[set[str]] = None) -> None:
        self.active = list(active)
        self.exit_only = set(exit_only or set()) | {
            s for s in self.pf.positions if s not in self.active}

    async def refresh_universe(self) -> None:
        assert self.rest is not None
        insts = await self.rest.get_instruments()
        for i in insts:
            self.instruments[i.symbol] = i
        self.pf.lot.update({i.symbol: i.lot for i in insts})
        cands = await uni.build_candidates(self.rest, self.s.universe, insts)
        if self.symbol_filter is not None:
            cands = [c for c in cands if c.symbol in self.symbol_filter]
        fee = self.s.fees.taker_bps
        good = uni.rank(cands, self.s.universe, fee,
                        self.s.decision.slippage_bps)
        emergency = {s for s in self.pf.positions
                     if s not in self.instruments or self.instruments[s].status != "online"}
        active, exit_only = self.selector.update(
            [c.symbol for c in good], emergency, set(self.pf.positions))
        self.active, self.exit_only = active, exit_only
        by = {c.symbol: c for c in cands}
        self.depth_ref = {
            s: min(by[s].depth10_bid, by[s].depth10_ask) for s in by}
        uni.save_snapshot(self.s.path("data/universe_snapshot.json"), cands)
        self.db.kv_set("universe", json.dumps({"active": active, "exit_only": sorted(exit_only),
                                               "ts": self.now_ms()}))
        log.info("universum: %d actief (%s), %d exit-only",
                 len(active), ",".join(active[:8]), len(exit_only))
        await self._load_fees(active)
        await self.sync_subscriptions()

    async def _load_fees(self, symbols: list[str]) -> None:
        client = self.fee_rest or self.rest
        if client is None or client.creds is None:
            return
        for s in symbols:
            if s in self.fee_bps:
                continue
            try:
                _maker, taker = await client.get_trade_rate(s)
                self.fee_bps[s] = taker * 1e4
            except ExchangeError as e:
                log.warning("trade-rate %s niet op te halen: %s", s, e)

    def wanted_subscriptions(self) -> set[tuple[str, str]]:
        syms = set(self.active) | self.exit_only | set(self.pf.positions)
        subs = {(ch, s) for s in syms for ch in ("trade", "books5", "ticker")}
        for c in self.s.universe.context_symbols:
            subs |= {("trade", c), ("ticker", c)}
        return subs

    async def sync_subscriptions(self) -> None:
        if self.feed is None:
            return
        for s in self.s.universe.context_symbols + self.active:
            await self.seed(s)
        await self.feed.set_subscriptions(self.wanted_subscriptions())
        keep = {s for _c, s in self.wanted_subscriptions()}
        for s in [s for s in self.ms.inst if s not in keep]:
            self.ms.drop(s)
        self._subs_dirty = False

    async def seed(self, symbol: str) -> None:
        st = self.ms.inst.get(symbol)
        if (st is not None and len(st.bars) >= 300) or self.rest is None:
            return
        try:
            candles = await self.rest.get_candles(symbol, "1min", limit=1000)
        except ExchangeError as e:
            log.warning("seed %s mislukt: %s", symbol, e)
            return
        now_min = self.now_ms() // 60_000 * 60_000
        self.ms.seed_bars(symbol, [c for c in candles if c.ts_ms < now_min])

    # ------------------------------------------------------------------- tick
    def _marks(self) -> dict[str, float]:
        out = {}
        for s in set(self.pf.positions) | set(self.active):
            st = self.ms.inst.get(s)
            if st and st.mid == st.mid and st.mid > 0:
                out[s] = st.mid
        return out

    def spread_bps(self, symbol: str) -> float:
        return self.ms.inst[symbol].spread_bps

    def fee_for(self, symbol: str) -> float:
        return self.fee_bps.get(symbol, self.s.fees.taker_bps)

    def _tick_bps(self, symbol: str) -> float:
        inst, st = self.instruments.get(symbol), self.ms.inst.get(symbol)
        if inst is None or st is None or not (st.mid > 0):
            return float("nan")
        return inst.tick / st.mid * 1e4

    async def _run_edge(self, now: int) -> None:
        queue, self._edge_queue = self._edge_queue, set()
        btc = self.ms.frame("BTCUSDT", FRAME_BARS)
        eth = self.ms.frame("ETHUSDT", FRAME_BARS)
        for sym in queue:
            st = self.ms.inst.get(sym)
            if st is None or sym in self.s.universe.context_symbols and sym not in self.active:
                continue
            df = self.ms.frame(sym, FRAME_BARS)
            args = (sym, df, btc if len(btc) else None, eth if len(eth) else None, self._tick_bps(sym),
                    st.spread_bps if st.spread_bps != float(
                        "inf") else self.s.universe.max_spread_bps,
                    self.fee_for(sym), now)
            adv = await asyncio.to_thread(self.edge.evaluate, *args) if self.use_threads else self.edge.evaluate(*args)
            self.edge_cache[sym] = adv
            cost = float(adv.meta.get("cost_bps", roundtrip_cost_bps(
                args[5], args[6], args[6], self.s.decision.slippage_bps)))
            self.feedback.record(adv, st.mid, cost)

    async def tick(self) -> None:
        now = self.now_ms()
        self.ms.flush(now)
        await self._run_edge(now)
        await self._manage_positions(now)
        await self._look_for_entries(now)
        if now - self._last_eval >= 10_000:
            self._last_eval = now
            n = self.feedback.evaluate_due(now)
            if n:
                self.counters["outcomes"] += n
        if now - self._last_equity_mark >= 10_000:
            self._last_equity_mark = now
            self.pf.mark_equity(now, self.pf.equity(self._marks()))
        if now - self._last_persist >= 30_000:
            self._last_persist = now
            self.db.kv_set(self.state_key, self.pf.dump())

    def _ctx(self, sym: str, now: int, marks: dict[str, float]) -> Optional[RiskContext]:
        inst, st = self.instruments.get(sym), self.ms.inst.get(sym)
        if inst is None or st is None:
            return None
        return RiskContext(now_ms=now, portfolio=self.pf, marks=marks, inst=inst, book=st.book,
                           data_age_ms=self.ms.age_ms(sym), active=sym in self.active and sym not in self.exit_only,
                           in_flight=self.engine.in_flight_symbols(), depth_ref_usdt=self.depth_ref.get(sym))

    def _log_decision(self, d: Decision, result: str) -> None:
        self.db.insert("decisions", {"ts_ms": d.ts_ms, "instrument": d.instrument, "action": d.action,
                                     "notional_usdt": d.notional_usdt, "ev_bps": d.ev_bps, "p_win": d.p_win,
                                     "risk_result": result, "detail": d.to_dict()})

    # ------------------------------------------------------------------ exits
    async def _manage_positions(self, now: int) -> None:
        marks = self._marks()
        for sym, pos in list(self.pf.positions.items()):
            st = self.ms.inst.get(sym)
            if st is None or st.book is None or not st.book.bids:
                continue
            bid = st.book.best_bid
            pos.highest = max(pos.highest, bid)
            if sym in self.engine.in_flight_symbols():
                continue
            emergency = self._is_emergency(sym, now)
            adv = self.exit_agent.advise(
                pos, bid, now, self.edge_cache.get(sym), emergency)
            if adv.action == "HOLD":
                continue
            if adv.reason == "take_profit" and self.engine.active_tp(sym):
                continue                         # de wachtende limietorder vult zelf
            self.counters[f"exit:{adv.reason}"] += 1
            if not await self.engine.cancel_take_profit(sym):
                continue
            if self.engine.exit_attempts.get(sym, 0) >= 2:
                adv.urgent = True                # twee mislukte pogingen: ruimere limiet
            dec = self.decider.decide_exit(pos, adv, bid, now)
            pos.exit_reason = adv.reason
            ctx = self._ctx(sym, now, marks)
            if ctx is None:
                continue
            res = self.risk.check_exit(dec, ctx)
            if not res.approved:
                self._log_decision(dec, f"risk_reject:{res.reason}")
                if res.reason == "dust_position":
                    self._write_off_dust(sym)
                continue
            if adv.reason in ("stop_loss", "trailing_stop", "emergency"):
                self.risk.note_stop(sym, now)
            self._log_decision(dec, "approved")
            self.engine.submit(res.order, dec)

    def _is_emergency(self, sym: str, now: int) -> bool:
        inst = self.instruments.get(sym)
        if inst is not None and inst.status != "online":
            return True
        if sym in self.selector.members or sym in self.active:
            return False
        rep = self.regime.assess(self.ms, sym, now, None, False)
        return rep.regime == Regime.BLOCKED and any(r in ("spread", "depth", "crossed_book") for r in rep.reasons)

    def _write_off_dust(self, sym: str) -> None:
        pos = self.pf.positions.pop(sym, None)
        if pos is not None and pos.qty > 0:
            self.pf.baseline[pos.base] = self.pf.baseline.get(
                pos.base, 0.0) + pos.qty
            self.counters["dust_written_off"] += 1
            log.info(
                "positie %s is stof (te klein om te verkopen); buiten de bot-boekhouding geplaatst", sym)

    # ---------------------------------------------------------------- entries
    async def _look_for_entries(self, now: int) -> None:
        d_cfg = self.s.decision
        marks = self._marks()
        edge_status = self.feedback.get_status(EDGE_ID)
        shock = btc_shock(self.ms, self.s.risk)
        for sym in self.active:
            if sym in self.exit_only:
                continue
            edge = self.edge_cache.get(sym)
            if edge is None or edge.advice != "BUY":
                continue
            if now - edge.timestamp_ms > self.s.agents.edge_max_age_s * 1000:
                continue
            if self.edge_used.get(sym) == edge.timestamp_ms:
                continue                          # elk edge-advies levert hooguit één entry op
            if edge_status != "active":
                self.counters["skipped_shadow_model"] += 1
                continue
            st = self.ms.inst.get(sym)
            if st is None or st.book is None or not st.book.asks:
                continue
            flow = self.flow.advise(self.ms, sym, now)
            rep = self.regime.assess(self.ms, sym, now, None, shock)
            dec = self.decider.decide_entry(
                sym, now, [edge, flow], rep, self.pf.positions.get(
                    sym), st.book.best_bid, st.book.best_ask,
                st.book.spread_bps, self.fee_for(sym), self.pf.available(self.s.risk.min_usdt_reserve))
            if dec.action != "BUY":
                self.counters[f"hold:{dec.reason.split(':')[0].split('(')[0]}"] += 1
                continue
            ctx = self._ctx(sym, now, marks)
            if ctx is None:
                continue
            res = self.risk.check_entry(dec, ctx)
            self._log_decision(
                dec, "approved" if res.approved else f"risk_reject:{res.reason}")
            if not res.approved:
                self.counters[f"risk_reject:{res.reason.split('(')[0]}"] += 1
                continue
            self.edge_used[sym] = edge.timestamp_ms
            self.counters["entries"] += 1
            self.engine.submit(res.order, dec)

    # ------------------------------------------------------------- levenscyclus
    async def start(self) -> None:
        await self.gw.start()
        self.engine.start()
        await self.restore_state()
        if self.rest is not None:
            await self.refresh_universe()
        if self.s.trading_enabled:
            issues = await self.engine.reconcile()
            if issues:
                log.warning("reconciliatie bij opstart: %s", issues)
        if self.gw is not None and hasattr(self.gw, "feed") and hasattr(self.gw.feed, "on_reconnect"):
            self.gw.feed.on_reconnect = self._after_reconnect
        self._tasks = [asyncio.create_task(self._loop(self._tick_loop, "tick")),
                       asyncio.create_task(self._loop(
                           self._universe_loop, "universe")),
                       asyncio.create_task(self._loop(self._housekeeping_loop, "housekeeping"))]

    async def _after_reconnect(self) -> None:
        log.warning("private ws herverbonden: reconciliatie")
        await self.engine.reconcile()

    async def restore_state(self) -> None:
        raw = self.db.kv_get(self.state_key)
        if raw and self.s.trading_enabled:
            self.pf.load(raw)
            log.info("portefeuille hersteld: %d posities",
                     len(self.pf.positions))
            for pos in self.pf.positions.values():
                pos.tp_client_oid = None          # orders worden bij reconciliatie opnieuw bepaald

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.s.trading_enabled and self.gw is not None:
            # Nog niet gevulde entries intrekken; resting take-profits blijven staan (die beschermen de winst).
            for rec in [o for o in self.engine.orders.values() if not o.terminal and o.purpose == "entry"]:
                try:
                    await self.gw.cancel_order(rec.symbol, client_oid=rec.client_oid)
                except Exception:  # noqa: BLE001
                    pass
        self.db.kv_set(self.state_key, self.pf.dump())
        if self.recorder:
            self.recorder.flush()
        await self.engine.stop()
        if self.feed is not None:
            await self.feed.stop()
        await self.gw.stop()
        if self.rest is not None:
            await self.rest.close()
        if self.fee_rest is not None:
            await self.fee_rest.close()

    async def _loop(self, fn, name: str) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("lus %s: fout; opnieuw over 5 s", name)
                self.health.alert(f"fout in lus {name}")
                await asyncio.sleep(5)

    async def _tick_loop(self) -> None:
        interval = self.s.decision.decision_interval_ms / 1000
        while True:
            t0 = time.perf_counter()
            await self.tick()
            self.counters["ticks"] += 1
            await asyncio.sleep(max(0.0, interval - (time.perf_counter() - t0)))

    async def _universe_loop(self) -> None:
        if self.rest is None:
            await asyncio.sleep(3600)
            return
        while True:
            await asyncio.sleep(self.s.universe.refresh_s)
            await self.refresh_universe()

    async def _housekeeping_loop(self) -> None:
        last_rec = 0.0
        while True:
            await asyncio.sleep(self.s.health.interval_s)
            self.health.write(self.status())
            for a in self.engine.alerts[:]:
                self.health.alert(a)
            self.engine.alerts.clear()
            if self.recorder:
                self.recorder.flush()
            if self._subs_dirty and self.feed is not None:
                await self.sync_subscriptions()
            if self.s.trading_enabled and time.monotonic() - last_rec > 60:
                last_rec = time.monotonic()
                await self.engine.reconcile()

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        marks = self._marks()
        eq = self.pf.equity(marks)
        lat = self.engine.ack_latencies[-200:]
        ages = {s: round(self.ms.age_ms(s)) for s in self.active[:30]}
        return {
            "mode": self.s.bot.mode, "environment": self.s.exchange.environment,
            "uptime_s": (self.now_ms() - self.started_ms) // 1000,
            "model": self.edge.version, "model_gate_passed": bool(self.edge.bundle and self.edge.bundle.passed_gate),
            "edge_status": self.feedback.get_status(EDGE_ID),
            "active": self.active, "exit_only": sorted(self.exit_only),
            "positions": {s: {"qty": p.qty, "avg": p.avg_price, "tp_bps": p.tp_bps, "sl_bps": p.sl_bps}
                          for s, p in self.pf.positions.items()},
            "cash": self.pf.cash, "equity": eq, "drawdown": self.pf.drawdown(eq), "realized": self.pf.realized_total,
            "breaker": self.risk.breaker_reason, "killed": self.risk.killed(),
            "feed": self.feed.stats() if self.feed is not None else None,
            "private_ws": self.gw.connected,
            "data_age_ms": ages,
            "ack_latency_ms": {"n": len(lat), "p50": float(np.percentile(lat, 50)) if lat else None,
                               "p95": float(np.percentile(lat, 95)) if lat else None},
            "rest": self.rest.stats if self.rest is not None else None,
            "counters": dict(self.counters),
        }
