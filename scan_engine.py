#!/usr/bin/env python3
"""
scan_engine.py — the ONLY place in this project that talks to yfinance.

Fetch/universe logic is orchestration around read-only-vendored code copied
from the private `jeeeepp/agent-notification` repo (see vendor/README.md).

Unlike that repo's fixed "ทรงดีมีพลัง" rule (hard-coded SMA 5/20/150/200 +
RSI(50) thresholds), this project's screen is fully user-configurable at
filter time (see conditions.py) — RSI/SMA/EMA periods and thresholds are
picked in the UI, not baked into the scan. To make that possible without a
new yfinance call per filter change, each ticker's trailing CLOSE prices are
kept in the cache (see ScanRow.closes) so conditions.py can compute any
period's SMA/EMA/RSI purely from already-fetched data.

`run_full_scan()` is expensive (fetches ~500-700+ tickers' price history) and
is meant to be called on an internal schedule / manual force-refresh only —
see app.py's ScanState for the caching + single-flight-lock layer that keeps
this from ever running per page-view or per filter change.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    # market_screener.py does `from scanner_core import ...` as a top-level
    # (not relative) import, so both vendored files need to resolve each
    # other off sys.path rather than via package-relative imports.
    sys.path.insert(0, str(VENDOR_DIR))

from scanner_core import LOOKBACK_CALENDAR_DAYS, load_watchlist  # noqa: E402
from market_screener import (  # noqa: E402
    fetch_price_history_bulk,
    is_squeezing,
    load_universe,
)

log = logging.getLogger("scan_engine")

WATCHLIST_PATH = BASE_DIR / "watchlist.txt"

# How many trailing trading days of CLOSE price to keep per ticker for
# on-demand indicator math (see conditions.py). Bounded rather than storing
# the full ~1000-trading-day fetch, to keep cache.json a sane size -- 450
# comfortably covers the largest period this project's condition models
# allow (SMA/EMA up to 400, RSI up to 200) with margin for the rolling
# window's own warm-up. NOTE: this trades a little RSI-convergence accuracy
# for a much smaller/faster cache -- this project is a screener (explore
# candidates, tune conditions live), not the original repo's exact-match
# daily alert bot, so "close enough" here is an intentional, documented
# choice, not an oversight.
MAX_STORED_CLOSES = 450


@dataclass
class ScanRow:
    ticker: str
    market: str  # "SET" or "US"
    price: float | None
    as_of_date: str | None
    squeeze: bool
    owned: bool
    closes: list[float] = field(default_factory=list)  # oldest -> newest


def _market_for(ticker: str) -> str:
    return "SET" if ticker.endswith(".BK") else "US"


def run_full_scan() -> tuple[list[ScanRow], dict]:
    """Fetch the whole watchlist + SET + US universe once, keeping each
    ticker's trailing close-price series for later on-demand indicator
    computation (see conditions.py) -- no scoring/filtering happens here.

    Returns (rows, stats). A single bad ticker never aborts the run (that's
    handled inside fetch_price_history_bulk already) -- this only raises if
    the universe itself comes back empty, since that indicates a real
    upstream problem (SP500/SET fetch both failed) worth surfacing to the
    caller rather than silently caching zero rows.
    """
    start = time.monotonic()

    watchlist = load_watchlist(str(WATCHLIST_PATH))
    owned = set(watchlist)

    # load_universe() deliberately EXCLUDES the watchlist (that's
    # agent-notification's signal_bot.py's job upstream) -- but this
    # dashboard wants to show the user's own watchlist tickers too, so we
    # scan the union of both.
    universe = load_universe(watchlist)
    all_tickers = [*watchlist, *universe]

    if not all_tickers:
        raise RuntimeError(
            "Universe is empty -- SP500/SET fetch likely failed upstream "
            "(see market_screener.load_universe logs)."
        )

    log.info("Fetching price history for %d ticker(s)...", len(all_tickers))
    history = fetch_price_history_bulk(all_tickers, LOOKBACK_CALENDAR_DAYS)

    rows: list[ScanRow] = []
    ok_count, failed_count = 0, 0
    for ticker, df in history.items():
        if df is None:
            failed_count += 1
            continue

        close = df["Close"].dropna()
        if close.empty:
            failed_count += 1
            continue

        price = float(close.iloc[-1])
        as_of_date = close.index[-1]
        as_of_date = as_of_date.date().isoformat() if hasattr(as_of_date, "date") else str(as_of_date)

        rows.append(
            ScanRow(
                ticker=ticker,
                market=_market_for(ticker),
                price=price,
                as_of_date=as_of_date,
                squeeze=is_squeezing(close),  # computed on the FULL fetched
                                               # series, before trimming below
                owned=ticker in owned,
                closes=[round(float(v), 6) for v in close.tail(MAX_STORED_CLOSES)],
            )
        )
        ok_count += 1

    elapsed = time.monotonic() - start
    stats = {
        "universe_count": len(all_tickers),
        "ok_count": ok_count,
        "failed_count": failed_count,
        "elapsed_sec": round(elapsed, 1),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
    log.info(
        "Scan done: %d ok, %d failed, %.1fs elapsed.", ok_count, failed_count, elapsed
    )
    return rows, stats


def rows_to_dicts(rows: list[ScanRow]) -> list[dict]:
    return [asdict(r) for r in rows]


if __name__ == "__main__":
    # Manual smoke test -- see README.md's Verification section.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )
    result_rows, result_stats = run_full_scan()
    log.info("RESULT stats=%s", result_stats)
    log.info(
        "Sample row: %s",
        {**asdict(result_rows[0]), "closes": f"<{len(result_rows[0].closes)} values>"}
        if result_rows else None,
    )
