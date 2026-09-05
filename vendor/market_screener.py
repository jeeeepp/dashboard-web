#!/usr/bin/env python3
"""
KMK Market Screener
====================

A SECOND, INDEPENDENT bot from `signal_bot.py`. Where `signal_bot.py` only
watches the fixed tickers in `watchlist.txt` and alerts when one of THEM
flips into "ทรงดีมีพลัง", this script goes looking for NEW candidates --
tickers that are NOT already in `watchlist.txt` -- across a broader universe
(S&P 500 + the whole Thai (SET) market, both auto-fetched live -- see
fetch_sp500_tickers()/fetch_set_tickers() below) that just crossed into good
shape/power today. Think of it as market discovery, not watchlist babysitting.

Rule (same scoring engine as the daily bot, imported read-only from
`scanner_core.py` -- see that file's docstring for the full rule
explanation and the RSI-lookback/convergence caveats):

  - "ทรง" (shape) must be 1.0 (both MA5>MA20 AND MA150>MA200)
  - RSI(50) must have just crossed UP through RSI_UPPER (60) TODAY, i.e.
    yesterday's RSI < 60 and today's RSI >= 60 -- an edge condition checked
    directly off the two most recent bars of the SAME price history this
    run already downloaded. No state.json needed: unlike signal_bot.py,
    this script has nothing to diff against a previous run for -- "just
    crossed" is self-limiting by definition (a ticker that stays above 60
    tomorrow does not cross again, so it naturally won't re-fire).

Does NOT touch signal_bot.py, scanner_core.py, or mcp_server.py -- imports
scanner_core read-only as its scoring engine so the trading rule can't drift
out of sync between bots by accident. Sends its own Discord message (same
webhook as signal_bot.py, but every embed is colored YELLOW so it's visually
obvious in the channel which bot found it).

Usage:
    python market_screener.py                # real run: fetch, score, alert
    python market_screener.py --dry-run       # compute + log the would-be
                                               # Discord payload, never POST,
                                               # never require a webhook URL
    python market_screener.py --force         # also report tickers that are
                                               # CURRENTLY shape=1.0 and
                                               # RSI>=60, even if the cross
                                               # didn't happen today -- for
                                               # manual/on-demand test runs,
                                               # since the real edge condition
                                               # may legitimately be empty
    python market_screener.py --verbose

Exit codes:
    0 -- ran fine (including a quiet day with zero hits)
    2 -- hard failure (missing config, or the whole universe failed to fetch)
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from dotenv import dotenv_values

from scanner_core import (
    RSI_LENGTH,
    RSI_UPPER,
    SMA_LONG_FAST,
    SMA_LONG_SLOW,
    SMA_SHORT_FAST,
    SMA_SHORT_SLOW,
    TickerScore,
    compute_rsi_wilder,
    compute_sma,
    load_watchlist,
    score_ticker,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "master/data/constituents.csv"
)
# Chosen as a stable, well-known, free public source for the US universe so
# we don't have to maintain our own S&P500 list by hand. If this source ever
# goes away or changes shape, fetch_sp500_tickers() below fails soft (logs a
# warning, returns []) rather than crashing the whole run -- the Thai side
# (fetch_set_tickers()) is independent and still gets scanned either way.

SET_SCREENER_PAGE_SIZE = 250  # Yahoo's screener API max page size.
# Thai (SET) universe is auto-fetched live via yfinance's screener API
# (EquityQuery region='th') -- same idea as fetch_sp500_tickers() above, no
# manually-maintained ticker file needed. That raw query returns EVERY
# region=Thailand instrument Yahoo knows about (~2200+ as of writing),
# which includes two kinds of non-stock noise fetch_set_tickers() filters
# out client-side (no server-side field for this -- see that function):
#   - DR products (e.g. ZJINNO80.BK tracking China's Zijin Mining) --
#     identified by "_DR " appearing in shortName.
#   - Rights-issue tickers (e.g. A-R.BK, a temporary rights offering for
#     A.BK) -- identified by a "-R" suffix before ".BK". These are
#     short-lived anyway and would mostly get skipped downstream for
#     insufficient price history (see fetch_price_history_bulk's
#     SMA_LONG_SLOW check), but filtering them up front avoids wasting a
#     fetch attempt on ~800 of them every run.
# Verified empirically: filtering leaves ~889 real SET-listed tickers, and
# includes every ticker that used to be manually curated in the old
# universe_th.txt (now removed).

DEFAULT_WATCHLIST_PATH = "watchlist.txt"
DEFAULT_ENV_PATH = ".env"

PREFILTER_LOOKBACK_CALENDAR_DAYS = 400
# Two-stage fetch to avoid paying for the expensive 1500-day RSI-convergence
# history (scanner_core.LOOKBACK_CALENDAR_DAYS) on the WHOLE universe:
#
#   Stage 1 (cheap):  fetch only PREFILTER_LOOKBACK_CALENDAR_DAYS of history
#                      for every candidate and compute JUST the shape score
#                      (moving averages aren't path-dependent -- 400 calendar
#                      days is already known-sufficient for MA200, see the
#                      RSI-lookback bug note in scanner_core.py/PROJECT_NOTES.md).
#   Stage 2 (expensive, survivors only): re-fetch the full RSI-convergence
#                      history ONLY for tickers that already passed
#                      shape_score == 1.0 in stage 1, then run the real
#                      RSI-crossing check on those.
#
# A ticker can only ever be a hit if it passes shape==1.0, so this discards
# non-qualifying tickers before they ever cost an expensive fetch, with
# identical final results to fetching everything at full depth up front.

BATCH_SIZE = 40          # tickers per yf.download() call -- keeps the total
                          # number of HTTP round-trips in the dozens, not the
                          # hundreds, for a ~500-700 ticker universe.
INTER_BATCH_SLEEP_SEC = 1.5
MAX_BATCH_RETRIES = 2
REQUEST_TIMEOUT_SEC = 20

DISCORD_MAX_EMBEDS_PER_MESSAGE = 10
SCREENER_COLOR = 0xFFFF00  # yellow -- deliberately different from
                            # signal_bot.py's green (0x2ECC71), so the two
                            # bots' alerts are visually distinguishable at a
                            # glance in the same Discord channel.

BB_PERIOD = 80          # from the KMK course: SMA(80) as the Bollinger
                        # middle band (course also mentions a 3-sigma outer
                        # band for "band-walk" confirmation -- not
                        # implemented here, this is squeeze-detection only)
BB_STDDEV_MULT = 2.0    # inner band multiplier, used for the bandwidth/
                        # squeeze calculation below
SQUEEZE_LOOKBACK_DAYS = 125   # ~6 trading months -- standard window for a
                              # "Bollinger Bandwidth percentile" squeeze read
SQUEEZE_PERCENTILE = 0.10     # today's bandwidth in the narrowest 10% of
                              # the lookback window = "squeezing"
# Squeeze is a TIMING signal only ("a big move is coming soon"), never a
# direction signal -- it's added as an informational tag on hits that
# already passed shape+power, never as an extra AND-condition, so a good
# shape+power opportunity is never hidden just because it isn't currently
# squeezing.

log = logging.getLogger("market_screener")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(env_path: str, dry_run: bool) -> dict:
    """Same shape as signal_bot.py's load_config -- duplicated on purpose
    (see module docstring: this script deliberately does not import from
    signal_bot.py)."""
    values = dotenv_values(env_path)
    webhook_url = (values.get("DISCORD_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()

    if not webhook_url and not dry_run:
        log.error(
            "DISCORD_WEBHOOK_URL is not set (checked %s and the environment). "
            "Copy .env.example to .env and fill in your webhook URL, or pass "
            "--dry-run to test without one.",
            env_path,
        )
        sys.exit(2)

    if not webhook_url and dry_run:
        log.warning("No DISCORD_WEBHOOK_URL configured -- fine for --dry-run, "
                    "but a real run will fail until .env is filled in.")

    return {"discord_webhook_url": webhook_url}


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def fetch_sp500_tickers() -> list[str]:
    """Fetch the current S&P 500 constituent list from a stable public CSV.
    Never raises -- returns [] (with a logged warning) on any failure, so a
    network hiccup or upstream schema change degrades to "just scan the Thai
    universe" instead of crashing the whole run."""
    try:
        resp = requests.get(SP500_CSV_URL, timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        if "Symbol" not in df.columns:
            log.warning(
                "S&P500 CSV at %s has no 'Symbol' column (columns: %s) -- "
                "source format may have changed; skipping US universe this run.",
                SP500_CSV_URL, list(df.columns),
            )
            return []
        # yfinance uses '-' where these tickers use '.' (e.g. BRK.B -> BRK-B).
        tickers = [str(s).strip().replace(".", "-") for s in df["Symbol"].dropna()]
        tickers = [t for t in tickers if t]
        log.info("Fetched %d S&P500 tickers from %s.", len(tickers), SP500_CSV_URL)
        return tickers
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch S&P500 universe (%s) -- skipping US universe this run.", exc)
        return []


def fetch_set_tickers() -> list[str]:
    """Fetch the whole Thai (SET) equity universe live via yfinance's
    screener API, paginating until Yahoo's reported total is reached. Never
    raises -- returns [] (with a logged warning) on any failure, so a
    network hiccup or an API shape change degrades to "just scan the US
    universe" instead of crashing the whole run. See the constants block
    above for what's filtered out and why."""
    try:
        from yfinance import EquityQuery

        query = EquityQuery("eq", ["region", "th"])
        quotes: list[dict] = []
        offset = 0
        while True:
            resp = yf.screen(query, size=SET_SCREENER_PAGE_SIZE, offset=offset, sortField="ticker", sortAsc=True)
            page = resp.get("quotes", [])
            quotes.extend(page)
            total = resp.get("total", len(quotes))
            offset += SET_SCREENER_PAGE_SIZE
            if offset >= total or not page:
                break
            time.sleep(0.5)

        tickers = []
        for quote in quotes:
            symbol = quote.get("symbol")
            if not symbol:
                continue
            if "_DR " in (quote.get("shortName") or ""):
                continue  # DR product tracking a foreign underlying stock
            if symbol.split(".")[0].endswith("-R"):
                continue  # temporary rights-issue ticker
            tickers.append(symbol)

        log.info("Fetched %d SET tickers (region=th, filtered) via yfinance screener.", len(tickers))
        return tickers
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch SET universe (%s) -- skipping Thai universe this run.", exc)
        return []


def load_universe(watchlist: list[str]) -> list[str]:
    """Union of (S&P500 auto-fetch) and (SET auto-fetch), minus anything
    already in watchlist.txt -- those are signal_bot.py's job, not ours."""
    sp500 = fetch_sp500_tickers()
    th = fetch_set_tickers()

    excluded = set(watchlist)
    seen: set[str] = set()
    universe: list[str] = []
    for ticker in [*sp500, *th]:
        if ticker in excluded or ticker in seen:
            continue
        seen.add(ticker)
        universe.append(ticker)

    log.info(
        "Universe: %d S&P500 + %d SET candidates, %d excluded (already in "
        "watchlist), %d unique tickers to scan.",
        len(sp500), len(th), len(set(sp500) | set(th)) - len(universe), len(universe),
    )
    return universe


# ---------------------------------------------------------------------------
# Bulk data fetch
# ---------------------------------------------------------------------------

def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _extract_ticker_frame(data: pd.DataFrame, ticker: str, single_ticker_chunk: bool) -> pd.DataFrame | None:
    """Pull one ticker's OHLC frame out of a (possibly multi-ticker)
    yf.download() result. Handles both the MultiIndex-columns case (the
    normal case for group_by='ticker' with 2+ tickers) and the flat-columns
    case (some yfinance versions return flat columns for a 1-ticker
    request)."""
    if isinstance(data.columns, pd.MultiIndex):
        if ticker not in data.columns.get_level_values(0):
            return None
        sub = data[ticker]
    elif single_ticker_chunk:
        sub = data
    else:
        return None

    if sub is None or sub.empty or "Close" not in sub.columns:
        return None
    return sub


def fetch_price_history_bulk(tickers: list[str], lookback_calendar_days: int) -> dict[str, pd.DataFrame | None]:
    """Batch-fetch daily OHLC history for many tickers at once via
    yf.download(), in chunks of BATCH_SIZE -- far fewer HTTP round-trips than
    scanner_core.fetch_price_history()'s one-request-per-ticker loop, which
    is fine for an 18-name watchlist but not for a ~500-700 name universe.

    Returns a dict covering every input ticker; value is None for anything
    that failed or didn't have enough history."""
    from datetime import timedelta

    start = date.today() - timedelta(days=lookback_calendar_days)
    end = date.today() + timedelta(days=1)

    results: dict[str, pd.DataFrame | None] = {t: None for t in tickers}

    for batch_num, batch in enumerate(_chunk(tickers, BATCH_SIZE), start=1):
        last_exc: Exception | None = None
        data: pd.DataFrame | None = None
        for attempt in range(1, MAX_BATCH_RETRIES + 2):
            try:
                data = yf.download(
                    tickers=batch,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    interval="1d",
                    auto_adjust=False,
                    group_by="ticker",
                    threads=True,
                    progress=False,
                )
                if data is None or data.empty:
                    raise ValueError("yfinance returned no rows for this batch")
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                data = None
                if attempt <= MAX_BATCH_RETRIES:
                    backoff = 2 * attempt
                    log.warning(
                        "Batch %d/%d (attempt %d/%d) failed (%s); retrying in %ds...",
                        batch_num, len(_chunk(tickers, BATCH_SIZE)), attempt,
                        MAX_BATCH_RETRIES + 1, exc, backoff,
                    )
                    time.sleep(backoff)

        if data is None:
            log.warning(
                "Giving up on batch %d (%d tickers) after %d attempts: %s",
                batch_num, len(batch), MAX_BATCH_RETRIES + 1, last_exc,
            )
        else:
            single = len(batch) == 1
            for ticker in batch:
                sub = _extract_ticker_frame(data, ticker, single_ticker_chunk=single)
                if sub is None:
                    continue
                closes = sub["Close"].dropna()
                if len(closes) < SMA_LONG_SLOW:
                    log.debug(
                        "Skipping %s: only %d trading days available, need >= %d.",
                        ticker, len(closes), SMA_LONG_SLOW,
                    )
                    continue
                results[ticker] = sub

        time.sleep(INTER_BATCH_SLEEP_SEC)

    return results


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------

def compute_shape_only(df: pd.DataFrame) -> float | None:
    """Cheap shape-only score (0.0/0.5/1.0) from a SHORT lookback window --
    used as stage 1 of the two-stage fetch (see PREFILTER_LOOKBACK_CALENDAR_DAYS
    above). Moving averages are plain rolling means, not path-dependent like
    RSI's EWM smoothing, so a short window is fine here even though the same
    ticker will need the full 1500-day window later for an accurate RSI.
    Returns None if there isn't enough history to compute MA200."""
    close = df["Close"].dropna()
    if len(close) < SMA_LONG_SLOW:
        return None

    v_sma5 = float(compute_sma(close, SMA_SHORT_FAST).iloc[-1])
    v_sma20 = float(compute_sma(close, SMA_SHORT_SLOW).iloc[-1])
    v_sma150 = float(compute_sma(close, SMA_LONG_FAST).iloc[-1])
    v_sma200 = float(compute_sma(close, SMA_LONG_SLOW).iloc[-1])
    if any(np.isnan(x) for x in (v_sma5, v_sma20, v_sma150, v_sma200)):
        return None

    short_sub = 0.5 if v_sma5 > v_sma20 else 0.0
    long_sub = 0.5 if v_sma150 > v_sma200 else 0.0
    return short_sub + long_sub


def compute_bollinger_bands(
    close: pd.Series, period: int, stddev_mult: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Standard Bollinger Bands: middle = SMA(period), upper/lower = middle
    +/- stddev_mult * rolling stddev(period). Pure function, no side
    effects."""
    middle = close.rolling(window=period, min_periods=period).mean()
    stddev = close.rolling(window=period, min_periods=period).std()
    upper = middle + stddev_mult * stddev
    lower = middle - stddev_mult * stddev
    return middle, upper, lower


def is_squeezing(close: pd.Series) -> bool:
    """Bollinger "squeeze" detection from the KMK course: the price range
    has been compressing for a long time (bandwidth near its narrowest in
    the last SQUEEZE_LOOKBACK_DAYS), which often precedes a big move --
    direction unknown, just timing. This is a TAG on hits that already
    passed shape+power, never an extra required condition (see the
    constants block above for why).

    Returns False (never raises) if there isn't enough history for a
    reliable read -- in practice every real hit already has 1500 days of
    history from the RSI-lookback requirement, far more than BB_PERIOD +
    SQUEEZE_LOOKBACK_DAYS needs, so this is just a defensive guard."""
    middle, upper, lower = compute_bollinger_bands(close, BB_PERIOD, BB_STDDEV_MULT)
    bandwidth = (upper - lower) / middle
    bandwidth = bandwidth.dropna()

    if len(bandwidth) < SQUEEZE_LOOKBACK_DAYS:
        return False

    window = bandwidth.tail(SQUEEZE_LOOKBACK_DAYS)
    threshold = window.quantile(SQUEEZE_PERCENTILE)
    return bool(bandwidth.iloc[-1] <= threshold)


def screen_ticker(df: pd.DataFrame, ticker: str, force: bool) -> TickerScore | None:
    """Score one ticker and decide whether it's a screener "hit" today.
    Returns the TickerScore if it qualifies, else None. Reuses
    scanner_core.score_ticker() for the shape/power/RSI numbers to report,
    and recomputes the RSI series once more (cheap -- same in-memory data,
    no extra network call) just to look at the prior bar for the crossing
    check."""
    score = score_ticker(df, ticker)
    if not score.ok:
        return None

    # scanner_core.TickerScore has no `price`/`squeeze` fields (it's a
    # shared, read-only import -- see module docstring on why we don't
    # edit scanner_core.py). It's a plain, unfrozen dataclass though, so
    # attaching ad-hoc attributes here is safe and keeps this data
    # available for the Discord embed without touching that file.
    close_series = df["Close"].dropna()
    score.price = float(close_series.iloc[-1]) if len(close_series) else None
    score.squeeze = is_squeezing(close_series)

    if force:
        hit = score.shape_score == 1.0 and score.rsi is not None and score.rsi >= RSI_UPPER
        return score if hit else None

    close = df["Close"].dropna()
    rsi_series = compute_rsi_wilder(close, RSI_LENGTH)
    if len(rsi_series) < 2:
        return None
    rsi_prev, rsi_today = float(rsi_series.iloc[-2]), float(rsi_series.iloc[-1])
    if np.isnan(rsi_prev) or np.isnan(rsi_today):
        return None

    crossed_up_today = rsi_prev < RSI_UPPER <= rsi_today
    hit = score.shape_score == 1.0 and crossed_up_today
    return score if hit else None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def plain_chart_url(ticker: str) -> str:
    """Identical in spirit to signal_bot.py's plain_chart_url -- short,
    first-party Yahoo Finance link, no shortener, no embedded config. See
    that function's docstring in signal_bot.py for the full history of why."""
    from urllib.parse import quote

    return f"https://finance.yahoo.com/chart/{quote(ticker, safe='')}"


def format_discord_messages(hits: list[TickerScore], run_date: str) -> list[dict]:
    """Returns a LIST of Discord payloads, not one -- Discord caps embeds at
    DISCORD_MAX_EMBEDS_PER_MESSAGE per message. This used to just slice
    hits[:cap], silently dropping the rest, which was survivable when the
    universe was small (a handful of hits/day) but became a real bug once
    the Thai side switched to auto-fetching the whole SET market (see
    fetch_set_tickers()): a good day can now easily produce 50-90+ hits, so
    silent truncation would drop the large majority of them. Same chunking
    approach as signal_bot.py's format_daily_report()."""
    header = {
        "title": f"🔍 หุ้นใหม่นอก watchlist ที่เพิ่งตัดขึ้น RSI 60 — {run_date}",
        "description": f"พบจากการกวาดตลาด (S&P500 + ตลาดหุ้นไทยทั้งหมด) ไม่ใช่จาก watchlist.txt เดิม — {len(hits)} ตัว",
        "color": SCREENER_COLOR,
    }

    ticker_embeds = []
    for score in hits:
        price = getattr(score, "price", None)
        price_line = f"ราคาปัจจุบัน: {price:.2f}\n" if price is not None else ""
        # Squeeze is an informational TAG, not a filter -- every hit that
        # already passed shape+power shows up regardless; this line just
        # adds context for the ones that also happen to be compressing.
        squeeze_line = "\n🎯 กำลังบีบตัว (Bollinger squeeze)" if getattr(score, "squeeze", False) else ""
        ticker_embeds.append({
            "title": f"📈 {score.ticker}",
            "url": plain_chart_url(score.ticker),
            "color": SCREENER_COLOR,
            "description": (
                f"{price_line}"
                f"ทรง: {score.shape_score:.1f} | RSI: {score.rsi:.1f} | "
                f"รวม: {score.total_score:.1f}\n"
                f"MA5: {score.sma5:.2f}\n"
                f"MA20: {score.sma20:.2f}\n"
                f"MA150: {score.sma150:.2f}\n"
                f"MA200: {score.sma200:.2f}"
                f"{squeeze_line}"
            ),
        })

    messages = [{"embeds": [header, *ticker_embeds[:DISCORD_MAX_EMBEDS_PER_MESSAGE - 1]]}]
    remaining = ticker_embeds[DISCORD_MAX_EMBEDS_PER_MESSAGE - 1:]
    for i in range(0, len(remaining), DISCORD_MAX_EMBEDS_PER_MESSAGE):
        messages.append({"embeds": remaining[i:i + DISCORD_MAX_EMBEDS_PER_MESSAGE]})
    return messages


def format_discord_error(reason: str) -> dict:
    return {"content": f"⚠️ market_screener run failed: {reason}"}


def send_discord_message(webhook_url: str, payload: dict, dry_run: bool) -> bool:
    import json

    if dry_run:
        log.info("[dry-run] Would POST to Discord webhook:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))
        return True

    if not webhook_url:
        log.error("Cannot send Discord message: no webhook URL configured.")
        return False

    last_exc: Exception | None = None
    for attempt in range(1, MAX_BATCH_RETRIES + 2):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SEC)
            if resp.status_code in (200, 204):
                return True
            log.warning(
                "Discord webhook returned HTTP %d on attempt %d/%d: %s",
                resp.status_code, attempt, MAX_BATCH_RETRIES + 1, resp.text[:300],
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("Discord webhook request failed on attempt %d/%d: %s", attempt, MAX_BATCH_RETRIES + 1, exc)

        if attempt <= MAX_BATCH_RETRIES:
            time.sleep(2 * attempt)

    log.error("Giving up sending Discord message after %d attempts (%s).", MAX_BATCH_RETRIES + 1, last_exc)
    return False


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KMK Market Screener")
    parser.add_argument("--dry-run", action="store_true", help="Compute and log everything, never POST to Discord and never require a webhook URL.")
    parser.add_argument("--watchlist", default=DEFAULT_WATCHLIST_PATH, help=f"Path to the watchlist to EXCLUDE from screening (default: {DEFAULT_WATCHLIST_PATH})")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help=f"Path to .env file (default: {DEFAULT_ENV_PATH})")
    parser.add_argument("--verbose", action="store_true", help="DEBUG-level logging.")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Report every candidate that is CURRENTLY shape=1.0 and RSI>=60, "
            "not just ones that crossed up through 60 today. Use for "
            "on-demand/manual test runs, since the real 'just crossed today' "
            "condition may legitimately be empty most days."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.env, dry_run=args.dry_run)
    watchlist = load_watchlist(args.watchlist)
    universe = load_universe(watchlist)

    if not universe:
        log.info("Empty universe -- nothing to scan. RUN_SUMMARY: total=0 ok=0 failed=0 hits=0")
        return 0

    from scanner_core import LOOKBACK_CALENDAR_DAYS

    # --- Stage 1: cheap shape-only prefilter over the WHOLE universe -------
    # Short lookback (MA isn't path-dependent -- only RSI needs the long
    # convergence window), so this is much lighter than fetching everyone's
    # full 1500-day history up front. See PREFILTER_LOOKBACK_CALENDAR_DAYS.
    log.info(
        "Stage 1/2: shape-only prefilter over %d candidate(s) (lookback=%d days)...",
        len(universe), PREFILTER_LOOKBACK_CALENDAR_DAYS,
    )
    prefilter_history = fetch_price_history_bulk(universe, PREFILTER_LOOKBACK_CALENDAR_DAYS)
    prefilter_failed = [t for t, df in prefilter_history.items() if df is None]

    shape_survivors: list[str] = []
    for ticker, df in prefilter_history.items():
        if df is None:
            continue
        shape = compute_shape_only(df)
        if shape == 1.0:
            shape_survivors.append(ticker)

    log.info(
        "Stage 1 done: %d/%d passed shape=1.0 (%d failed to fetch) -- only "
        "these get the expensive full RSI-lookback fetch in stage 2.",
        len(shape_survivors), len(universe), len(prefilter_failed),
    )

    if not shape_survivors:
        log.info("No candidates passed the shape prefilter today.")
        log.info(
            "RUN_SUMMARY: universe=%d prefilter_failed=%d shape_survivors=0 hits=0",
            len(universe), len(prefilter_failed),
        )
        return 0

    # --- Stage 2: full RSI-lookback fetch, survivors only -------------------
    log.info(
        "Stage 2/2: full-history fetch for %d shape-qualified candidate(s)...",
        len(shape_survivors),
    )
    history = fetch_price_history_bulk(shape_survivors, LOOKBACK_CALENDAR_DAYS)
    failed = [t for t, df in history.items() if df is None]
    ok_count = len(shape_survivors) - len(failed)

    if ok_count == 0:
        reason = f"all {len(shape_survivors)} shape-qualified candidate(s) failed to fetch in stage 2"
        log.error(reason + " -- aborting.")
        send_discord_message(config["discord_webhook_url"], format_discord_error(reason), args.dry_run)
        log.info(
            "RUN_SUMMARY: universe=%d prefilter_failed=%d shape_survivors=%d stage2_ok=0 stage2_failed=%d hits=0",
            len(universe), len(prefilter_failed), len(shape_survivors), len(failed),
        )
        return 2

    hits: list[TickerScore] = []
    for ticker, df in history.items():
        if df is None:
            continue
        hit = screen_ticker(df, ticker, force=args.force)
        if hit is not None:
            hits.append(hit)
            log.debug(
                "HIT %s: shape=%.1f rsi=%.1f total=%.1f",
                hit.ticker, hit.shape_score, hit.rsi, hit.total_score,
            )

    sent_ok = 0
    messages: list[dict] = []
    if hits:
        run_date = date.today().isoformat()
        messages = format_discord_messages(hits, run_date)
        for i, payload in enumerate(messages):
            if send_discord_message(config["discord_webhook_url"], payload, args.dry_run):
                sent_ok += 1
            else:
                log.error("Failed to deliver message %d/%d.", i + 1, len(messages))
            if i < len(messages) - 1:
                time.sleep(INTER_BATCH_SLEEP_SEC)

        if sent_ok == len(messages):
            log.info("Alerted on %d hit(s) across %d message(s): %s",
                      len(hits), len(messages), ", ".join(h.ticker for h in hits))
        elif sent_ok > 0:
            log.warning("Delivered %d/%d message(s) -- some hits may be missing from Discord.",
                        sent_ok, len(messages))
        else:
            log.error("Failed to deliver ANY of the %d message(s) for %d hit(s).", len(messages), len(hits))
    else:
        log.info("No new candidates found today.")

    log.info(
        "RUN_SUMMARY: universe=%d prefilter_failed=%d shape_survivors=%d stage2_ok=%d stage2_failed=%d hits=%d messages_sent=%d/%d",
        len(universe), len(prefilter_failed), len(shape_survivors), ok_count, len(failed), len(hits),
        sent_ok, len(messages),
    )
    return 0 if not hits or sent_ok > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
