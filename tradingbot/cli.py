"""Commandoregel: python -m tradingbot <commando>."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

from .config import ConfigError, Settings, load_credentials, load_settings

log = logging.getLogger("tradingbot")


def _setup_logging(settings: Settings, to_file: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        p = settings.path("data/bot.log")
        p.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(p, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


# ------------------------------------------------------------------------------- check
async def cmd_check(s: Settings, _a) -> int:
    from .exchange.bitget.rest import BitgetRest
    from .exchange.bitget.ws_private import PrivateFeed
    ok = True
    print(f"modus={s.bot.mode} omgeving={s.exchange.environment}")
    rest = BitgetRest(s)
    await rest.start()
    print(f"klokoffset t.o.v. server: {rest.clock.offset_ms} ms")
    insts = await rest.get_instruments()
    online = [i for i in insts if i.status == "online" and i.quote ==
              s.universe.quote and not i.is_tokenized_stock]
    print(
        f"publieke data OK: {len(insts)} symbolen, {len(online)} {s.universe.quote}-crypto online")
    await rest.close()
    for env in ("demo", "live"):
        try:
            creds = load_credentials(s, env, read_only=True)
        except ConfigError as e:
            print(f"[{env}] credentials ontbreken: {e}")
            continue
        r = BitgetRest(s, creds)
        await r.start()
        try:
            if env == "demo":
                bals = await r.get_balances()
                usdt = next((b.total for b in bals if b.coin == "USDT"), 0.0)
                print(f"[demo] REST-auth OK, USDT-saldo {usdt:.2f}")
            # live: alleen het (alleen-lezen) accounttarief; geen saldo, geen orders
            else:
                m, t = await r.get_trade_rate("WLDUSDT")
                print(
                    f"[live] REST-auth OK; accounttarief WLDUSDT: maker {m*1e4:.1f} bps, taker {t*1e4:.1f} bps")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[{env}] REST-auth MISLUKT: {e}")
        await r.close()
        if env == "demo":
            feed = PrivateFeed(s, creds, r.clock)
            feed.start()
            ready = await feed.wait_ready(8)
            print(
                f"[demo] private WebSocket login: {'OK' if ready else 'MISLUKT'}")
            ok &= ready
            await feed.stop()
    return 0 if ok else 1


# ----------------------------------------------------------------------------- history
async def _research_symbols(s: Settings, rest, max_symbols: int) -> tuple[list[str], dict[str, float]]:
    from .agents import universe as uni
    insts = await rest.get_instruments()
    cands = await uni.build_candidates(rest, s.universe, insts)
    uni.rank(cands, s.universe, s.fees.taker_bps, s.decision.slippage_bps)
    hard = {"not_online", "volume", "spread", "spike",
            "sd1m_extreme", "suspicious_spread", "history"}
    pool = [c for c in cands if not (set(c.fails) & hard)]
    pool.sort(key=lambda c: c.volume_usdt, reverse=True)
    syms = [c.symbol for c in pool[:max_symbols]]
    for ctx in s.universe.context_symbols:
        if ctx not in syms:
            syms.append(ctx)
    uni.save_snapshot(s.path("data/universe_snapshot.json"), cands)
    return syms, {c.symbol: c.spread_bps for c in cands}


async def cmd_history(s: Settings, a) -> int:
    from .exchange.bitget.rest import BitgetRest
    from .history import download_symbol
    rest = BitgetRest(s)
    if a.rate:
        s.rate.rest_public_per_s = a.rate
        rest = BitgetRest(s)
    await rest.start()
    syms, _spreads = await _research_symbols(s, rest, a.max_symbols)
    print(f"{len(syms)} symbolen: {', '.join(syms)}")
    cache = s.path("data/history")
    sem = asyncio.Semaphore(a.concurrency)
    done = 0

    async def one(sym: str) -> None:
        nonlocal done
        async with sem:
            t0 = time.time()
            df = await download_symbol(rest, sym, a.days or s.model.history_days, cache)
            done += 1
            print(f"[{done}/{len(syms)}] {sym}: {len(df)} bars ({len(df)/1440:.1f} dagen) in {time.time()-t0:.0f}s",
                  flush=True)

    await asyncio.gather(*(one(sym) for sym in syms))
    await rest.close()
    return 0


# ------------------------------------------------------------------------------- train
def cmd_train(s: Settings, a) -> int:
    from .history import load_cached
    from .model import set_current
    from .training import train_and_validate, write_report
    from .agents.universe import Candidate  # noqa: F401
    from .app import load_instruments
    cache = s.path("data/history")
    frames: dict[str, pd.DataFrame] = {}
    for f in sorted(cache.glob("*_1m.parquet")):
        frames[f.name.replace("_1m.parquet", "")] = pd.read_parquet(f)
    if "BTCUSDT" not in frames or "ETHUSDT" not in frames:
        print("BTCUSDT/ETHUSDT ontbreken in de historie; draai eerst `history`")
        return 1
    snap_p = s.path("data/universe_snapshot.json")
    spreads = {}
    if snap_p.exists():
        spreads = {c["symbol"]: c["spread_bps"]
                   for c in json.loads(snap_p.read_text(encoding="utf-8"))}
    insts_p = s.path("data/instruments.json")
    tick_bps = {}
    if insts_p.exists():
        insts = load_instruments(insts_p)
        for sym, df in frames.items():
            if sym in insts and len(df):
                tick_bps[sym] = insts[sym].tick / \
                    float(df["close"].iloc[-1]) * 1e4
    print(
        f"trainen op {len(frames)} symbolen, {sum(len(d) for d in frames.values())} bars")
    bundle, report = train_and_validate(frames, tick_bps, spreads, s)
    bundle.save(s.path(s.model.dir))
    rp = write_report(s, report)
    g = report["gate"]
    ev = report["evaluation"]
    print(f"model {bundle.version}; rapport: {rp}")
    for h, info in report["horizons"].items():
        print(f"  horizon {h:>3} min: train={info['n_train']} val={info['n_val']} test={info['n_test']} "
              f"AUC(val)={info['val_auc']:.3f} base_rate={info['val_base_rate']:.3f}")
    t = ev.get("trades", {})
    print(
        f"  test: {t.get('n', 0)} trades, gem. netto {t.get('mean_net_bps')} bps, hit {t.get('hit_rate')}")
    print(
        f"  baselines: order_flow={report['baselines']['order_flow']} naive={report['baselines']['naive']}")
    print(
        f"  poort: {'GESLAAGD' if g['passed'] else 'NIET GEHAALD'} {g['checks']}")
    if not a.no_activate:
        set_current(s.path(s.model.dir), bundle.version)
        print("model ingesteld als huidig" +
              ("" if g["passed"] else " (shadow: stemt niet mee tot het live bewijs levert)"))
    return 0


# --------------------------------------------------------------------------------- run
async def cmd_run(s: Settings, a) -> int:
    from .app import build_bot
    bot = await build_bot(s)
    await bot.start()
    log.info("bot gestart: modus=%s omgeving=%s model=%s",
             s.bot.mode, s.exchange.environment, bot.edge.version)
    if s.trading_enabled:
        log.warning(
            "Bij stoppen blijven wachtende take-profit-orders staan; open posities worden dan niet meer bewaakt.")
    try:
        end = time.time() + a.minutes * 60 if a.minutes else None
        while end is None or time.time() < end:
            await asyncio.sleep(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        print(json.dumps(bot.status(), indent=1, default=str))
        await bot.stop()
    return 0


# -------------------------------------------------------------------------------- measure
async def cmd_measure(s: Settings, a) -> int:
    from .measure import measure_orders_demo, measure_rest_public, measure_ws, recommend
    if s.is_live:
        print("measure draait alleen met environment = demo")
        return 1
    rest_pub = await measure_rest_public(s) if not a.ws_only else {}
    print("REST publiek:", json.dumps(rest_pub, indent=1))
    orders = await measure_orders_demo(s) if not (a.skip_orders or a.ws_only) else {}
    print("Orders (demo):", json.dumps(orders, indent=1, default=str))
    ws = await measure_ws(s)
    print("WebSocket:", json.dumps(ws, indent=1, default=str))
    rec = recommend(rest_pub, orders)
    print("Voorgestelde [rate]-waarden (50% van het foutvrije maximum):", rec)
    out = s.path("reports")
    out.mkdir(exist_ok=True)
    f = out / f"rate_measurement_{time.strftime('%Y%m%dT%H%M%S')}.json"
    f.write_text(json.dumps({"rest_public": rest_pub, "orders": orders, "ws": ws, "recommended": rec}, indent=1,
                            default=str), encoding="utf-8")
    print("opgeslagen:", f)
    return 0


# --------------------------------------------------------------------------------- replay
async def cmd_replay(s: Settings, a) -> int:
    from .app import load_instruments
    from .backtest import replay
    from .history import load_cached
    from .model import load_current
    rec_dir = s.path(a.dir or s.recorder.dir)
    insts = load_instruments(s.path("data/instruments.json"))
    bundle = load_current(s.path(s.model.dir))
    snap = json.loads(
        s.path("data/universe_snapshot.json").read_text(encoding="utf-8"))
    active = a.symbols.split(",") if a.symbols else [
        c["symbol"] for c in snap if not c["fails"]][: s.universe.top_n]
    frames = {}
    for sym in set(active) | set(s.universe.context_symbols):
        df = load_cached(sym, s.path("data/history"))
        if df is not None:
            frames[sym] = df
    report = await replay(s, rec_dir, insts, active, bundle, frames, a.usdt)
    print(json.dumps(report, indent=1, default=str))
    return 0


# ----------------------------------------------------------------------------- demo-smoke
async def cmd_demo_smoke(s: Settings, a) -> int:
    """Keten-test op DEMO: kleine BTC-koop en -verkoop via de uitvoeringslaag (WS-orders, events, boekhouding)."""
    from .execution import ExecutionEngine
    from .exchange.bitget.gateway import BitgetGateway
    from .exchange.bitget.rest import BitgetRest
    from .portfolio import Portfolio
    from .risk import OrderIntent, RiskManager
    from .storage import Storage
    if s.is_live:
        print("demo-smoke draait nooit op live")
        return 1
    creds = load_credentials(s, "demo")
    rest = BitgetRest(s, creds)
    await rest.start()
    demo = {i.symbol: i for i in await rest.get_instruments(demo=True)}
    inst = demo[a.symbol]
    book = await rest.get_orderbook(a.symbol, 5, demo=True)
    live_book = await rest.get_orderbook(a.symbol, 5)
    dev = abs(book.mid / live_book.mid - 1) * 1e4
    print(
        f"demo-mid {book.mid} vs live-mid {live_book.mid} (afwijking {dev:.0f} bps), spread {book.spread_bps:.1f} bps")
    if dev > 100:
        print("demo-prijs wijkt te veel af; test afgebroken")
        await rest.close()
        return 1
    bals = await rest.get_balances()
    usdt = next((b.total for b in bals if b.coin == "USDT"), 0.0)
    gw = BitgetGateway(s, creds, rest)
    pf = Portfolio(
        usdt, {b.coin: b.total for b in bals if b.coin != "USDT" and b.total > 0})
    pf.lot[a.symbol] = inst.lot
    db = Storage(":memory:")
    risk = RiskManager(s)
    eng = ExecutionEngine(s, gw, pf, risk, db, {
                          a.symbol: inst}, rest.clock.now_ms)
    await gw.start()
    eng.start()
    print("private ws klaar:", gw.connected)
    price = inst.ceil_price(book.best_ask * 1.001)
    qty = inst.round_qty(a.notional / price)
    buy = eng.submit(OrderIntent(a.symbol, "buy", qty,
                     price, qty * price, "ioc", "entry"))
    await asyncio.wait_for(buy.done.wait(), 10)
    print(f"BUY: state={buy.state} gevuld={buy.filled_base} gem.prijs={buy.avg_price:.2f} fee={buy.fee} "
          f"ack={buy.ack_latency_ms:.0f} ms, eerste fill na {buy.first_fill_ms - buy.created_ms if buy.first_fill_ms else None} ms")
    pos = pf.positions.get(a.symbol)
    if pos is None:
        print("geen positie ontstaan; einde")
    else:
        print(
            f"positie: qty={pos.qty} (netto na fee in basismunt), kosten={pos.cost_usdt:.4f} USDT")
        sell_qty = inst.round_qty(pos.qty)
        bid_book = await rest.get_orderbook(a.symbol, 5, demo=True)
        sell_price = inst.floor_price(bid_book.best_bid * 0.999)
        sell = eng.submit(OrderIntent(a.symbol, "sell", sell_qty,
                          sell_price, sell_qty * sell_price, "ioc", "exit"))
        await asyncio.wait_for(sell.done.wait(), 10)
        print(f"SELL: state={sell.state} gevuld={sell.filled_base} gem.prijs={sell.avg_price:.2f} fee={sell.fee} "
              f"ack={sell.ack_latency_ms:.0f} ms")
    await asyncio.sleep(1.5)
    issues = await eng.reconcile()
    print("reconciliatie:", issues or "geen afwijkingen")
    dust = {k: v for k, v in pf.baseline.items() if k == inst.base}
    print(f"realized P&L: {pf.realized_total:.4f} USDT; open posities: {list(pf.positions)}; "
          f"stof naar baseline: {dust}")
    await eng.stop()
    await gw.stop()
    await rest.close()
    return 0


def cmd_simulate(s: Settings, a) -> int:
    """Walk-forward simulatie 'alsof live' over de gedownloade historie; hertraint wekelijks op het verleden."""
    from .app import load_instruments
    from .model import set_current
    from .walkforward import run_walkforward, summarize
    cache = s.path("data/history")
    frames = {f.name.replace("_1m.parquet", ""): pd.read_parquet(f)
              for f in sorted(cache.glob("*_1m.parquet"))}
    if a.symbols:
        keep = set(a.symbols.split(",")) | set(s.universe.context_symbols)
        frames = {k: v for k, v in frames.items() if k in keep}
    if "BTCUSDT" not in frames or "ETHUSDT" not in frames:
        print("BTCUSDT/ETHUSDT ontbreken in de historie; draai eerst `history`")
        return 1
    spreads, tick_bps = {}, {}
    snap_p = s.path("data/universe_snapshot.json")
    if snap_p.exists():
        spreads = {c["symbol"]: c["spread_bps"]
                   for c in json.loads(snap_p.read_text(encoding="utf-8"))}
    insts_p = s.path("data/instruments.json")
    if insts_p.exists():
        insts = load_instruments(insts_p)
        tick_bps = {k: insts[k].tick / float(v["close"].iloc[-1])
                    * 1e4 for k, v in frames.items() if k in insts and len(v)}
    days = {k: round(len(v) / 1440) for k, v in frames.items()}
    print(f"{len(frames)} paren, dagen historie: {days}", flush=True)
    result, bundle = run_walkforward(frames, tick_bps, spreads, s, warmup_days=a.warmup_days,
                                     train_days=a.train_days, retrain_days=a.retrain_days, stride=a.stride,
                                     progress=lambda m: print(m, flush=True))
    print(summarize(result))
    out = s.path("reports")
    out.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    trades = {k: v.pop("trade_list", [])
              for k, v in result.get("variants", {}).items()}
    f = out / f"walkforward_{stamp}.json"
    f.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    for name, tl in trades.items():
        if tl:
            pd.DataFrame(tl).to_parquet(
                out / f"walkforward_{stamp}_{name}_trades.parquet", index=False)
    print("rapport:", f)
    if bundle is not None and not a.no_save:
        bundle.save(s.path(s.model.dir))
        set_current(s.path(s.model.dir), bundle.version)
        print(f"eindmodel {bundle.version} opgeslagen als huidig model (poort "
              f"{'geslaagd' if bundle.passed_gate else 'niet gehaald: shadow'})")
    return 0


def cmd_status(s: Settings, _a) -> int:
    p = s.path(s.health.path)
    if not p.exists():
        print("geen health-bestand; draait de bot?")
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradingbot")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    p = sub.add_parser("history")
    p.add_argument("--days", type=float, default=None)
    p.add_argument("--max-symbols", type=int, default=30)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--rate", type=float, default=None,
                   help="aanvragen per seconde voor de download")
    p = sub.add_parser("train")
    p.add_argument("--no-activate", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--minutes", type=float, default=None)
    p = sub.add_parser("measure")
    p.add_argument("--skip-orders", action="store_true")
    p.add_argument("--ws-only", action="store_true")
    p = sub.add_parser("replay")
    p.add_argument("--dir", default=None)
    p.add_argument("--symbols", default=None)
    p.add_argument("--usdt", type=float, default=1000.0)
    p = sub.add_parser("demo-smoke")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--notional", type=float, default=10.0)
    p = sub.add_parser("simulate")
    p.add_argument("--warmup-days", type=int, default=30)
    p.add_argument("--train-days", type=int, default=60)
    p.add_argument("--retrain-days", type=int, default=7)
    p.add_argument("--stride", type=int, default=3)
    p.add_argument("--symbols", default=None)
    p.add_argument("--no-save", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    try:
        s = load_settings(a.config)
    except ConfigError as e:
        print(f"Configuratiefout: {e}")
        return 2
    _setup_logging(s, to_file=a.cmd == "run")
    try:
        if a.cmd == "train":
            return cmd_train(s, a)
        if a.cmd == "status":
            return cmd_status(s, a)
        if a.cmd == "simulate":
            return cmd_simulate(s, a)
        fn = {"check": cmd_check, "history": cmd_history, "run": cmd_run, "measure": cmd_measure,
              "replay": cmd_replay, "demo-smoke": cmd_demo_smoke}[a.cmd]
        return asyncio.run(fn(s, a))
    except KeyboardInterrupt:
        return 130
    except ConfigError as e:
        print(f"Configuratiefout: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
