#!/usr/bin/env python3
"""
conditions.py — the user-configurable RSI/SMA/EMA screen that REPLACES
agent-notification's fixed "ทรงดีมีพลัง" (shape/power) rule for this project.

Everything here operates ONLY on a ticker's already-cached trailing close
prices (ScanRow.closes, see scan_engine.py) -- no yfinance call ever happens
here, so changing a period/operator/threshold and re-filtering is always
cheap, matching the rest of the app's "filtering reads the cache only" rule.

Close price only, by design (per the user's explicit requirement) -- no
open/high/low is ever used for any condition here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    # Same reasoning as scan_engine.py: scanner_core.py is not a package,
    # just a plain module on sys.path -- this module is self-sufficient
    # rather than relying on scan_engine.py having already run this insert.
    sys.path.insert(0, str(VENDOR_DIR))

from scanner_core import compute_rsi_wilder, compute_sma  # noqa: E402

Operator = Literal[">", "<", "="]

# Bounds are enforced here (not just in the UI) because they also determine
# how much history scan_engine.MAX_STORED_CLOSES must retain -- a period
# above that cap can never be satisfied no matter what's cached.
MIN_PERIOD = 2
MAX_MA_PERIOD = 400
MAX_RSI_PERIOD = 200


class RsiCondition(BaseModel):
    enabled: bool = False
    period: int = Field(50, ge=MIN_PERIOD, le=MAX_RSI_PERIOD)
    operator: Operator = ">"
    threshold: float = 60.0


class MaGroupCondition(BaseModel):
    """One group (SMA or EMA): a single operator shared across every period
    in the group (per the user's choice), each period individually
    editable/removable. A ticker passes the group only if price satisfies
    `operator` against EVERY listed period's moving average (AND)."""

    enabled: bool = False
    operator: Operator = ">"
    periods: list[int] = Field(default_factory=list)

    @field_validator("periods")
    @classmethod
    def _drop_out_of_range_periods(cls, periods: list[int]) -> list[int]:
        # Silently drop (not 422) out-of-range values -- mirrors the
        # frontend's own parsePeriods(), which already filters while the
        # user is still typing a comma-separated list; a hard validation
        # error here would be jarring for what's meant to be a live,
        # forgiving filter UI. in-range duplicates are also collapsed.
        seen: set[int] = set()
        cleaned = []
        for p in periods:
            if MIN_PERIOD <= p <= MAX_MA_PERIOD and p not in seen:
                seen.add(p)
                cleaned.append(p)
        return cleaned


class ScanFilterRequest(BaseModel):
    rsi: RsiCondition = Field(default_factory=RsiCondition)
    sma: MaGroupCondition = Field(
        default_factory=lambda: MaGroupCondition(periods=[5, 20, 150, 200])
    )
    ema: MaGroupCondition = Field(
        default_factory=lambda: MaGroupCondition(periods=[7, 30, 89, 200])
    )
    market: Literal["all", "SET", "US"] = "all"
    owned_only: bool = False
    squeeze_only: bool = False
    q: str = ""


def _compare(value: float, operator: Operator, target: float) -> bool:
    if operator == ">":
        return value > target
    if operator == "<":
        return value < target
    # "=" on indicator floats: exact equality is almost never meaningful, so
    # treat it as "close enough" -- 0.5% of the target (or a small absolute
    # floor for a near-zero target, e.g. RSI threshold 0).
    tolerance = max(abs(target) * 0.005, 0.01)
    return abs(value - target) <= tolerance


def evaluate_ticker(closes: list[float], price: float, req: ScanFilterRequest) -> dict | None:
    """Computes every ENABLED indicator in `req` from `closes` and checks it
    against its condition. Returns None if there isn't enough history to
    evaluate an enabled condition (that ticker is simply excluded from
    results needing it, same as scanner_core's own "not enough history"
    skip) -- otherwise a dict of computed values + an overall `match` bool
    (AND across every enabled group)."""
    if not closes:
        return None
    series = pd.Series(closes, dtype=float)

    match = True
    rsi_value: float | None = None
    sma_values: dict[int, float] = {}
    ema_values: dict[int, float] = {}

    if req.rsi.enabled:
        if len(series) < req.rsi.period:
            return None
        rsi_series = compute_rsi_wilder(series, req.rsi.period)
        v = float(rsi_series.iloc[-1])
        if np.isnan(v):
            return None
        rsi_value = round(v, 2)
        if not _compare(v, req.rsi.operator, req.rsi.threshold):
            match = False

    if req.sma.enabled:
        for period in req.sma.periods:
            if len(series) < period:
                return None
            v = float(compute_sma(series, period).iloc[-1])
            if np.isnan(v):
                return None
            sma_values[period] = round(v, 4)
            if not _compare(price, req.sma.operator, v):
                match = False

    if req.ema.enabled:
        for period in req.ema.periods:
            if len(series) < period:
                return None
            v = float(series.ewm(span=period, adjust=False, min_periods=period).mean().iloc[-1])
            if np.isnan(v):
                return None
            ema_values[period] = round(v, 4)
            if not _compare(price, req.ema.operator, v):
                match = False

    return {
        "match": match,
        "rsi": rsi_value,
        "sma": sma_values,
        "ema": ema_values,
    }


def keep_row(row: dict, req: ScanFilterRequest) -> bool:
    """The plain (non-indicator) filters -- market/owned/squeeze/ticker
    search -- checked before the more expensive indicator math above."""
    if req.market != "all" and row["market"] != req.market:
        return False
    if req.owned_only and not row["owned"]:
        return False
    if req.squeeze_only and not row["squeeze"]:
        return False
    if req.q:
        if req.q.strip().upper() not in row["ticker"].upper():
            return False
    return True
