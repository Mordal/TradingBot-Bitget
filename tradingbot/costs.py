"""Kostenmodel van een rondrit."""
from __future__ import annotations


def roundtrip_cost_bps(spread_bps: float, fee_in_bps: float, fee_out_bps: float, slippage_bps: float) -> float:
    """kosten = fee_in + fee_out + spread + 2 x slippage (bps van de notional)."""
    return fee_in_bps + fee_out_bps + spread_bps + 2.0 * slippage_bps
