import base64
import hashlib
import hmac

import pytest

from tradingbot.config import ConfigError, Credentials, Settings, load_settings, validate
from tradingbot.exchange.bitget.signing import Clock, rest_signature, ws_login_signature
from tradingbot.exchange.base import ExchangeError, Instrument, OrderBook
from tradingbot.ratelimit import Backoff, TokenBucket


def _expected(secret: str, msg: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()


def test_rest_signature_includes_query_and_body():
    sig = rest_signature("s3cret", 1700000000000, "post",
                         "/api/v2/spot/trade/place-order", "", '{"a":1}')
    assert sig == _expected(
        "s3cret", '1700000000000POST/api/v2/spot/trade/place-order{"a":1}')
    sig = rest_signature("s3cret", 1, "GET", "/p", "symbol=BTCUSDT")
    assert sig == _expected("s3cret", "1GET/p?symbol=BTCUSDT")


def test_ws_login_signature_uses_user_verify():
    assert ws_login_signature("k", 1700000000) == _expected(
        "k", "1700000000GET/user/verify")


def test_clock_offset_applied():
    c = Clock()
    c.calibrate(int(__import__("time").time() * 1000) + 5000)
    assert 4500 < c.offset_ms < 5500


def test_credentials_repr_hides_secrets():
    cr = Credentials("KEYVALUE", "SECRETVALUE", "PASSVALUE", True)
    text = repr(cr)
    assert "KEYVALUE" not in text and "SECRETVALUE" not in text and "PASSVALUE" not in text


def test_default_config_file_loads_and_matches_defaults():
    s = load_settings()
    assert s.exchange.environment == "demo" and s.bot.mode == "shadow"
    assert s.decision.min_ev_bps == 5.0 and s.risk.max_open_positions == 6 and s.universe.top_n == 20


def test_live_trading_requires_confirmation():
    s = Settings()
    s.bot.mode, s.exchange.environment = "trade", "live"
    with pytest.raises(ConfigError):
        validate(s)
    s.exchange.live_confirmed = True
    validate(s)
    s.bot.force_model_active = True
    with pytest.raises(ConfigError):
        validate(s)


def test_unknown_config_key_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[risk]\nmax_open_positionz = 3\n")
    with pytest.raises(ConfigError):
        load_settings(p)


def test_invalid_thresholds_rejected():
    s = Settings()
    s.risk.dd_soft, s.risk.dd_hard = 0.1, 0.05
    with pytest.raises(ConfigError):
        validate(s)


def test_exchange_error_rate_limit_detection():
    assert ExchangeError("x", code="429").is_rate_limit
    assert ExchangeError("x", http_status=429).is_rate_limit
    assert not ExchangeError("x", code="40001").is_rate_limit


def test_instrument_rounding(inst):
    assert inst.floor_price(
        0.54619) == 0.5461 and inst.ceil_price(0.54611) == 0.5462
    assert inst.round_qty(12.349) == 12.34
    assert Instrument("rAAPLUSDT", "rAAPL", "USDT",
                      "online", 2, 4, 1).is_tokenized_stock


def test_orderbook_depth_and_vwap():
    ob = OrderBook(bids=[(0.99, 100), (0.98, 100)], asks=[
                   (1.01, 100), (1.02, 100)], ts_ms=0)
    assert ob.mid == 1.0 and round(ob.spread_bps, 1) == 200.0
    assert ob.depth_usdt("ask", 150) == pytest.approx(101.0)
    assert ob.vwap_buy(101.0) == pytest.approx(1.01)
    # onvoldoende zichtbare diepte
    assert ob.vwap_buy(1000.0) is None
    assert ob.vwap_sell(150) == pytest.approx((0.99 * 100 + 0.98 * 50) / 150)


async def test_token_bucket_limits_rate():
    t = [0.0]
    b = TokenBucket(2.0, 2.0, now=lambda: t[0])
    assert b.try_acquire() and b.try_acquire() and not b.try_acquire()
    t[0] = 0.5
    assert b.try_acquire() and not b.try_acquire()
    b.penalize(1.0)
    assert b.wait_time() > 1.0


def test_backoff_grows_and_resets():
    import random
    b = Backoff(0.5, 4.0, random.Random(1))
    d = [b.next_delay() for _ in range(5)]
    assert d[0] <= 0.5 and max(d) <= 4.0 and d[3] > d[0]
    b.reset()
    assert b.next_delay() <= 0.5
