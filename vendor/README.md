# Vendored files — read-only, do not edit

`scanner_core.py` and `market_screener.py` in this directory are **verbatim
copies** pulled from the private `jeeeepp/agent-notification` repo (the KMK
scanner bot project) on 2026-09-05, at these paths in that repo:

- `scanner_core.py` → scoring engine (`score_ticker`, `TickerScore`,
  `load_watchlist`, the SMA/RSI constants)
- `market_screener.py` → universe building (`load_universe`, SET+S&P500
  fetch), bulk price fetch (`fetch_price_history_bulk`), and the Bollinger
  squeeze helpers (`is_squeezing`, `compute_bollinger_bands`)

This project is a **separate repo/deployment** from `agent-notification`, not
a fork or submodule, so there's no git history link — these are plain copies.

## Rules for this directory

1. **Never edit these two files.** `dashboard-web`'s own code
   (`scan_engine.py`, `app.py`) only ever *imports* from them, the same
   "reuse via import" pattern `agent-notification`'s own
   `squeeze_scanner.py`/`confluence_scanner.py` use internally.
2. If the trading rule ever changes upstream (SMA/RSI periods, thresholds,
   universe logic, batching/rate-limit constants), **manually re-copy** the
   updated file(s) from `agent-notification` into this directory — there is
   no automated sync between the two repos. Diff before overwriting so you
   notice what changed.
3. Do not add new logic here. New behavior for the dashboard belongs in
   `../scan_engine.py`, which is this project's own code.
