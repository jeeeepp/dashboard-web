#!/usr/bin/env python3
"""
KMK Scanner Core
=================

Pure, side-effect-free logic for fetching price history and scoring a ticker
on the "ทรงดีมีพลัง" rule from the King's Court KMK trading course (EP:01-03):

  - "ทรง" (shape)  0.0/0.5/1.0  -- from 4 SIMPLE moving averages (SMA, not EMA)
  - "พลัง" (power) -1/0/+1      -- from a custom-tuned RSI, interpreted
                                    CONTRARIAN to the textbook meaning

See the constants block below for the exact rule values, and README.md /
kmk_trade_checklist.html (sections F and I) for where these numbers come
from in the source videos.

This module has NO knowledge of Discord, state.json diffing, or CLI
argument parsing -- that logic lives only in signal_bot.py (the daily
unattended alert bot).

⚠️ IMPORTANT -- DUPLICATED LOGIC, NOT A SHARED IMPORT: everything in this
file (constants, TickerScore, load_watchlist, fetch_price_history,
compute_sma, compute_rsi_wilder, score_ticker) is a deliberate COPY of the
equivalent code in signal_bot.py, kept separate on purpose so mcp_server.py
never has to import signal_bot.py (and signal_bot.py never has to change at
all). The tradeoff: **if you ever tune the trading rule -- SMA/RSI periods,
RSI_UPPER/RSI_LOWER thresholds, ALERT_THRESHOLD, LOOKBACK_CALENDAR_DAYS --
you must edit the constants block in BOTH signal_bot.py and this file**, or
the MCP scan tool (mcp_server.py) will silently disagree with the daily
Discord bot's answer. There is no automated check for this drift; diff the
two files' constants blocks by eye after any rule change.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Constants -- the ONE place to tune the trading rule. See README.md /
# kmk_trade_checklist.html (sections F and I) for where these numbers come
# from in the source videos.
# ---------------------------------------------------------------------------

SMA_SHORT_FAST = 5
SMA_SHORT_SLOW = 20
SMA_LONG_FAST = 150
SMA_LONG_SLOW = 200

RSI_LENGTH = 50
RSI_UPPER = 60.0   # "green" -- CONTRARIAN meaning: unusually strong buying,
                   # follow it (NOT the textbook "overbought, sell" meaning)
RSI_LOWER = 50.0   # "red"   -- CONTRARIAN meaning: unusually strong selling,
                   # flee (NOT the textbook "oversold, buy" meaning)
# CAVEAT (stated explicitly in the source material): this contrarian
# green=buy / red=sell reading is only valid for an instrument that already
# has a clear trend. For a genuinely range-bound / sideways instrument the
# classic overbought=sell / oversold=buy reading would apply instead. v1 of
# this bot implements the single contrarian rule above regardless of
# trend state -- revisit this if it produces obvious false positives on
# tickers that are actually just chopping sideways.

LOOKBACK_CALENDAR_DAYS = 1500  # NOTE: 400 is enough for MA200 alone, but NOT
                               # enough for RSI(50) to converge -- Wilder's
                               # smoothing (an EWM) is path-dependent and
                               # "remembers" its arbitrary starting point for
                               # a long time. Verified empirically on real
                               # PTT.BK data: RSI(50) with only 271 trading
                               # days of history (400 calendar days) came out
                               # to 60.63, vs 60.19 with 1000+ trading days
                               # (1500+ calendar days) -- which also matches
                               # what Yahoo's own chart displays. 1500
                               # calendar days (~1000 trading days) gives the
                               # same value as 3650 days tested side by side,
                               # i.e. it's already fully converged with
                               # margin to spare. MA5/20/150/200 are plain
                               # rolling means and are NOT affected by this
                               # (identical at 400 vs 1500 days) -- this only
                               # ever mattered for RSI.
ALERT_THRESHOLD = 2.0          # shape(1.0) + power(+1) == "ทรงดีมีพลัง"
                               # lower this (e.g. to 1.5) to also catch
                               # "good shape, neutral power" if desired

INTER_REQUEST_SLEEP_SEC = 0.5
MAX_RETRIES = 2
REQUEST_TIMEOUT_SEC = 15

DEFAULT_WATCHLIST_PATH = "watchlist.txt"

# Resolved relative to this file, not the caller's CWD -- an MCP client can
# spawn mcp_server.py from an arbitrary working directory, so anything using
# the bare DEFAULT_WATCHLIST_PATH string above would silently look in the
# wrong place. Prefer WATCHLIST_PATH (a real Path) over the bare string
# constant when calling load_watchlist() from mcp_server.py.
BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = BASE_DIR / DEFAULT_WATCHLIST_PATH

log = logging.getLogger("scanner_core")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TickerScore:
    ticker: str
    as_of_date: str | None = None
    sma5: float | None = None
    sma20: float | None = None
    sma150: float | None = None
    sma200: float | None = None
    shape_score: float | None = None
    rsi: float | None = None
    power_score: int | None = None
    total_score: float | None = None
    full_signal: bool | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_state_entry(self) -> dict:
        """Only the fields worth persisting across runs for diffing."""
        return {
            "as_of_date": self.as_of_date,
            "shape_score": self.shape_score,
            "power_score": self.power_score,
            "total_score": self.total_score,
            "full_signal": self.full_signal,
        }


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def load_watchlist(path: str) -> list[str]:
    """Read watchlist.txt: one ticker per line, '#' starts a comment, blank
    lines ignored. Missing file or empty file is NOT an error -- just an
    empty watchlist (the caller will simply do nothing)."""
    p = Path(path)
    if not p.exists():
        log.warning("Watchlist file not found at %s -- treating as empty.", path)
        return []

    tickers: list[str] = []
    seen: set[str] = set()
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line in seen:
            log.warning("Duplicate ticker '%s' in watchlist -- ignoring the repeat.", line)
            continue
        seen.add(line)
        tickers.append(line)

    if not tickers:
        log.warning("Watchlist at %s is empty -- nothing to scan.", path)

    return tickers


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_price_history(ticker: str) -> pd.DataFrame | None:
    """Download daily OHLC history for `ticker` via yfinance. Never raises --
    returns None (with a logged warning) on any failure so one bad ticker
    can't take down the whole run."""
    start = date.today() - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    end = date.today() + timedelta(days=1)

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):  # e.g. MAX_RETRIES=2 -> 3 tries total
        try:
            df = yf.Ticker(ticker).history(
                start=start.isoformat(),
                end=end.isoformat(),
                interval="1d",
                auto_adjust=False,
            )
            if df is None or df.empty:
                raise ValueError("yfinance returned no rows")
            if "Close" not in df.columns:
                raise ValueError("yfinance result missing 'Close' column")

            closes = df["Close"].dropna()
            if len(closes) < SMA_LONG_SLOW:
                log.warning(
                    "Skipping %s: only %d trading days of history available, "
                    "need >= %d for MA%d.",
                    ticker, len(closes), SMA_LONG_SLOW, SMA_LONG_SLOW,
                )
                return None

            return df

        except Exception as exc:  # noqa: BLE001 -- deliberately broad: network,
                                    # bad-ticker JSON, rate limits, etc. must
                                    # all be caught and reported, not raised.
            last_exc = exc
            if attempt <= MAX_RETRIES:
                backoff = 2 * attempt
                log.warning(
                    "Fetch attempt %d/%d for %s failed (%s); retrying in %ds...",
                    attempt, MAX_RETRIES + 1, ticker, exc, backoff,
                )
                time.sleep(backoff)
            else:
                log.warning("Giving up on %s after %d attempts: %s", ticker, attempt, exc)

    return None


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def compute_sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def compute_rsi_wilder(series: pd.Series, length: int) -> pd.Series:
    """RSI using Wilder's smoothing (the standard/original RSI method --
    chosen deliberately over a plain rolling-average RSI variant, since
    Wilder's method is what most charting platforms use by default).

    Implemented via an EWM with alpha=1/length, which is a well-known
    practical equivalent of Wilder's iterative recursive smoothing -- but
    it's path-dependent: it needs a long run-up of history before the
    influence of its arbitrary starting point fully washes out. See
    LOOKBACK_CALENDAR_DAYS above for the empirical convergence check that
    sets how much history callers must feed this function.
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))

    # Edge cases: avg_loss == 0 means no losses in the window at all.
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)

    return rsi


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_ticker(df: pd.DataFrame, ticker: str) -> TickerScore:
    close = df["Close"].dropna()

    sma5 = compute_sma(close, SMA_SHORT_FAST)
    sma20 = compute_sma(close, SMA_SHORT_SLOW)
    sma150 = compute_sma(close, SMA_LONG_FAST)
    sma200 = compute_sma(close, SMA_LONG_SLOW)
    rsi = compute_rsi_wilder(close, RSI_LENGTH)

    last_idx = close.index[-1]
    v_sma5, v_sma20 = float(sma5.iloc[-1]), float(sma20.iloc[-1])
    v_sma150, v_sma200 = float(sma150.iloc[-1]), float(sma200.iloc[-1])
    v_rsi = float(rsi.iloc[-1])

    if any(np.isnan(x) for x in (v_sma5, v_sma20, v_sma150, v_sma200, v_rsi)):
        return TickerScore(ticker=ticker, error="indicator produced NaN on latest bar")

    short_sub = 0.5 if v_sma5 > v_sma20 else 0.0
    long_sub = 0.5 if v_sma150 > v_sma200 else 0.0
    shape_score = short_sub + long_sub

    if v_rsi >= RSI_UPPER:
        power_score = 1
    elif v_rsi <= RSI_LOWER:
        power_score = -1
    else:
        power_score = 0

    total_score = shape_score + power_score
    full_signal = total_score >= ALERT_THRESHOLD

    as_of_date = pd.Timestamp(last_idx).date().isoformat()

    return TickerScore(
        ticker=ticker,
        as_of_date=as_of_date,
        sma5=round(v_sma5, 4),
        sma20=round(v_sma20, 4),
        sma150=round(v_sma150, 4),
        sma200=round(v_sma200, 4),
        shape_score=shape_score,
        rsi=round(v_rsi, 2),
        power_score=power_score,
        total_score=total_score,
        full_signal=full_signal,
    )
