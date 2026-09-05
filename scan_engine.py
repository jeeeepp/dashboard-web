#!/usr/bin/env python3
"""
scan_engine.py — the ONLY place in this project that talks to yfinance.

Everything here is orchestration around read-only-vendored code copied from
the private `jeeeepp/agent-notification` repo (see vendor/README.md) — no
trading-rule logic (SMA/RSI periods, thresholds, universe definition) is
redefined here, it's all imported.

`run_full_scan()` is expensive (fetches ~500-700 tickers' price history) and
is meant to be called on an internal schedule / manual force-refresh only —
see app.py's ScanState for the caching + single-flight-lock layer that keeps
this from ever running per page-view or per filter change.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    # market_screener.py does `from scanner_core import ...` as a top-level
    # (not relative) import, so both vendored files need to resolve each
    # other off sys.path rather than via package-relative imports.
    sys.path.insert(0, str(VENDOR_DIR))

from scanner_core import LOOKBACK_CALENDAR_DAYS, load_watchlist, score_ticker  # noqa: E402
from market_screener import (  # noqa: E402
    fetch_price_history_bulk,
    is_squeezing,
    load_universe,
)

log = logging.getLogger("scan_engine")

WATCHLIST_PATH = BASE_DIR / "watchlist.txt"


@dataclass
class ScanRow:
    ticker: str
    market: str  # "SET" or "US"
    price: float | None
    as_of_date: str | None
    shape_score: float | None
    rsi: float | None
    power_score: int | None
    total_score: float | None
    full_signal: bool | None
    squeeze: bool
    owned: bool


def _market_for(ticker: str) -> str:
    return "SET" if ticker.endswith(".BK") else "US"


def run_full_scan() -> tuple[list[ScanRow], dict]:
    """Fetch + score the whole watchlist + SET + US universe once.

    Returns (rows, stats). A single bad ticker never aborts the run (that's
    handled inside fetch_price_history_bulk/score_ticker already) — this
    only raises if the universe itself comes back empty, since that
    indicates a real upstream problem (SP500/SET fetch both failed) worth
    surfacing to the caller rather than silently caching zero rows.
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

        score = score_ticker(df, ticker)
        if not score.ok:
            failed_count += 1
            continue

        close = df["Close"].dropna()
        price = float(close.iloc[-1]) if len(close) else None

        rows.append(
            ScanRow(
                ticker=ticker,
                market=_market_for(ticker),
                price=price,
                as_of_date=score.as_of_date,
                shape_score=score.shape_score,
                rsi=score.rsi,
                power_score=score.power_score,
                total_score=score.total_score,
                full_signal=score.full_signal,
                squeeze=is_squeezing(close),
                owned=ticker in owned,
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
    hits = [r for r in result_rows if r.full_signal]
    log.info("%d full-signal ('ทรงดีมีพลัง') hits: %s",
              len(hits), ", ".join(r.ticker for r in hits[:20]))
