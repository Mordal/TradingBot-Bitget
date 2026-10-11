"""Bitget-handtekeningen en klokoffset."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time


def _sign(secret: str, message: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()


def rest_signature(secret: str, ts_ms: int, method: str, path: str, query: str = "", body: str = "") -> str:
    """base64(HMAC-SHA256(ts + METHOD + path[?query] + body))."""
    target = f"{path}?{query}" if query else path
    return _sign(secret, f"{ts_ms}{method.upper()}{target}{body}")


def ws_login_signature(secret: str, ts_s: int) -> str:
    """WS-login ondertekent ts(seconden) + GET + /user/verify."""
    return _sign(secret, f"{ts_s}GET/user/verify")


class Clock:
    """Serverklok = lokale klok + offset (de pc liep ~5 s achter)."""

    def __init__(self) -> None:
        self.offset_ms = 0

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    def calibrate(self, server_ms: int, rtt_ms: float = 0.0) -> None:
        self.offset_ms = int(server_ms + rtt_ms / 2 - time.time() * 1000)
