import pytest

from tradingbot.exchange.base import Balance
from tradingbot.portfolio import Portfolio


def buy(pf, price=10.0, base=10.0, fee=0.01, coin="WLD", fid="1", oid="o1", ts=1000):
    return pf.apply_fill("WLDUSDT", "WLD", "buy", price, base, fee, coin, ts, oid, fid)


def test_buy_fee_in_base_coin_reduces_quantity_not_cash():
    pf = Portfolio(1000.0)
    buy(pf)                                  # 10 WLD voor 100 USDT, fee 0,01 WLD
    pos = pf.positions["WLDUSDT"]
    assert pos.qty == pytest.approx(9.99) and pf.cash == pytest.approx(900.0)
    assert pos.cost_usdt == pytest.approx(
        100.0) and pos.avg_price == pytest.approx(100 / 9.99)
    assert pos.fees_usdt == pytest.approx(0.1)


def test_buy_fee_in_usdt_adds_to_cost():
    pf = Portfolio(1000.0)
    buy(pf, fee=0.1, coin="USDT")
    assert pf.positions["WLDUSDT"].qty == 10.0 and pf.cash == pytest.approx(
        899.9)
    assert pf.positions["WLDUSDT"].cost_usdt == pytest.approx(100.1)


def test_fill_is_idempotent():
    pf = Portfolio(1000.0)
    assert buy(pf) is True
    assert buy(pf) is False
    assert pf.positions["WLDUSDT"].qty == pytest.approx(9.99)


def test_full_round_trip_pnl_and_closed_trade():
    pf = Portfolio(1000.0)
    closed = []
    pf.on_closed = closed.append
    pf.lot["WLDUSDT"] = 0.01
    buy(pf)
    pf.positions["WLDUSDT"].exit_reason = "take_profit"
    # verkoop 9,99 @ 10,5 met 0,1% fee in USDT
    pf.apply_fill("WLDUSDT", "WLD", "sell", 10.5, 9.99,
                  0.104895, "USDT", 2000, "o2", "2")
    assert "WLDUSDT" not in pf.positions and len(closed) == 1
    expected = 10.5 * 9.99 - 0.104895 - 100.0
    assert closed[0].pnl_usdt == pytest.approx(expected)
    assert closed[0].reason == "take_profit" and closed[0].pnl_bps == pytest.approx(
        expected / 100 * 1e4)
    assert pf.cash == pytest.approx(900.0 + 10.5 * 9.99 - 0.104895)


def test_partial_sell_keeps_average_cost():
    pf = Portfolio(1000.0)
    pf.lot["WLDUSDT"] = 0.01
    buy(pf, fee=0.0)
    pf.apply_fill("WLDUSDT", "WLD", "sell", 11.0,
                  4.0, 0.0, "USDT", 2000, "o2", "2")
    p = pf.positions["WLDUSDT"]
    assert p.qty == pytest.approx(6.0) and p.avg_price == pytest.approx(10.0)
    assert p.realized_usdt == pytest.approx(4.0)


def test_dust_remainder_closes_position_and_moves_to_baseline():
    pf = Portfolio(1000.0)
    pf.lot["WLDUSDT"] = 0.01
    buy(pf, fee=0.0)
    pf.apply_fill("WLDUSDT", "WLD", "sell", 10.0,
                  9.995, 0.0, "USDT", 2000, "o2", "2")
    assert "WLDUSDT" not in pf.positions
    assert pf.baseline["WLD"] == pytest.approx(0.005)


def test_sell_without_bot_position_is_ignored():
    pf = Portfolio(1000.0, baseline={"WLD": 50.0})
    pf.apply_fill("WLDUSDT", "WLD", "sell", 10.0,
                  5.0, 0.01, "USDT", 1, "x", "1")
    assert pf.cash == 1000.0 and not pf.positions


def test_reservations_and_budget():
    pf = Portfolio(1000.0, capital_cap=400.0)
    assert pf.available(min_reserve=50) == 350.0
    pf.reserve("a", 100.0)
    assert pf.available(50) == 250.0
    pf.release("a")
    assert pf.available(50) == 350.0
    assert Portfolio(1000.0).available(50) == 950.0


def test_equity_drawdown_and_day_loss():
    pf = Portfolio(1000.0)
    pf.mark_equity(0, 1000.0)
    pf.mark_equity(3600_000, 1100.0)
    assert pf.drawdown(1045.0) == pytest.approx(0.05)
    assert pf.day_loss(900.0) == pytest.approx(0.1)
    # piek van >24u geleden valt weg
    pf.mark_equity(26 * 3600_000, 1000.0)
    assert pf.drawdown(1000.0) == pytest.approx(0.0)


def test_reconcile_detects_mismatch_and_ignores_baseline():
    pf = Portfolio(1000.0, baseline={"WLD": 5.0})
    pf.lot["WLDUSDT"] = 0.01
    # 9,99 netto, cash 900
    buy(pf)
    ok = [Balance("USDT", 900.0), Balance("WLD", 5.0 + 9.99)]
    assert pf.reconcile(ok) == []
    bad = [Balance("USDT", 850.0), Balance("WLD", 5.0 + 9.99)]
    assert any("USDT" in i for i in pf.reconcile(bad))
    bad2 = [Balance("USDT", 900.0), Balance("WLD", 5.0)]
    assert any("WLD" in i for i in pf.reconcile(bad2))


def test_dump_and_load_roundtrip():
    pf = Portfolio(1000.0)
    buy(pf)
    pf.positions["WLDUSDT"].tp_bps = 55.0
    other = Portfolio(0.0)
    other.load(pf.dump())
    assert other.cash == pf.cash and other.positions["WLDUSDT"].tp_bps == 55.0
    assert other.positions["WLDUSDT"].qty == pytest.approx(9.99)
