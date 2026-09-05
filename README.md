# Scan Dashboard

Live web dashboard: a filterable, sortable table of every ticker in the SET
(Thai) + S&P 500 (US) universe that matches **your own RSI/SMA/EMA
conditions** — periods, operator (`>`/`<`/`=`), and threshold are all
editable in the UI, not fixed — with near-real-time price data pulled from
yfinance. Primarily a market-wide **scanner/screener**, not a watchlist
viewer — the optional "Owned" column (see Local setup) is a small extra on
top, not the point of the tool.

This project's screen deliberately does **not** reuse
`agent-notification`'s fixed "ทรงดีมีพลัง" rule (hard-coded SMA
5/20/150/200 + RSI(50) thresholds) — it replaces it with a fully
user-configurable one (see `conditions.py`). Close price only, everywhere,
by design — no open/high/low is ever used.

This is a **standalone project**, separate from the `jeeeepp/agent-notification`
repo (the Discord scanner bot). It does not touch that repo's code or
workflows at all — it re-uses its scoring/fetch logic via **vendored, hand-copied
files** (see `vendor/README.md`), because the two live in different repos with
no shared git history.

## Why it's built this way

Earlier designs considered here (and rejected, in order):

1. Extending the bot's 4 scanner scripts + 5 GitHub Actions workflows to
   commit a shared `scan_results.json`, viewed as a static site — rejected:
   no touching the existing workflows/scripts at all.
2. A static site (GitHub Pages / Cloudflare Pages) with GitHub Actions
   (`workflow_dispatch`) as the on-demand data puller — rejected twice: no
   GitHub Actions in the data path, and no static-only hosting.
3. A single origin repo hosting `dashboard/` as a subfolder of
   `agent-notification` — superseded once the decision was made to make
   this a **fully separate repo/project** instead.

What's left, and what this repo implements: an **always-on FastAPI backend**
that fetches the whole universe on an internal schedule (not per page-view,
not per filter change), caches each ticker's trailing close prices in memory
+ on disk, and serves every dashboard request (including changing RSI/SMA/EMA
conditions) from that cache. Nobody's filter tweak ever triggers a new
yfinance call — SMA/EMA/RSI for whatever period/operator/threshold you pick
are computed on the spot from the cached prices.

## Layout

- **`vendor/scanner_core.py`, `vendor/market_screener.py`** — read-only
  copies from `agent-notification`. Never edit these; see `vendor/README.md`
  for the re-sync process if the upstream code ever changes. This project
  only reuses their universe-building/fetch machinery (`load_universe`,
  `fetch_price_history_bulk`, `is_squeezing`) plus two indicator primitives
  (`compute_sma`, `compute_rsi_wilder`) — **not** their fixed shape/power
  scoring rule, which this project replaces (see `conditions.py`).
- **`scan_engine.py`** — the only file that calls into yfinance-backed code.
  `run_full_scan()` builds the universe (watchlist ∪ SET ∪ US), bulk-fetches
  price history (reusing `market_screener.fetch_price_history_bulk`'s
  existing batching/retry/backoff — nothing reinvented), flags the
  Bollinger squeeze (`market_screener.is_squeezing`), and keeps each
  ticker's trailing close prices (`ScanRow.closes`, last
  `MAX_STORED_CLOSES=450` trading days) for `conditions.py` to compute
  indicators from later, at any period.
- **`conditions.py`** — the user-configurable RSI/SMA/EMA screen. Pydantic
  request models (`RsiCondition`, `MaGroupCondition`, `ScanFilterRequest`)
  plus `evaluate_ticker()`, which computes only the ENABLED indicators from
  a ticker's cached closes and checks each against its operator/threshold —
  RSI is a single period+operator+threshold; SMA/EMA are each one shared
  operator applied as an AND across an editable list of periods (e.g.
  "price > SMA for every period in [5,20,150,200]"). All periods/thresholds
  are bounded (`MIN_PERIOD`–`MAX_MA_PERIOD`/`MAX_RSI_PERIOD`) since they
  can't exceed what `MAX_STORED_CLOSES` retains; out-of-range SMA/EMA
  periods are silently dropped (matches the frontend's own live-typing
  leniency) rather than rejected with a 422.
- **`app.py`** — FastAPI service: startup loads `cache.json` if present (so
  a cold start after a host sleep serves *something* instantly), a
  background scheduler calls `run_full_scan()` every `REFRESH_INTERVAL_MIN`
  minutes, `POST /api/scan` (a body, not query params, since the condition
  payload is nested) filters + evaluates conditions against the in-memory
  cache only, `POST /api/refresh` force-refreshes behind a single-flight
  lock + short cooldown, `GET /api/status` reports scan-in-progress/cooldown
  state for the frontend to poll, and everything sits behind HTTP Basic Auth.
- **`static/index.html`** — RSI/SMA/EMA condition panel + plain filters
  (market/squeeze/owned/ticker search) + sortable table, no build step,
  polls `/api/status` and re-POSTs `/api/scan` on every condition/filter
  change.
- **`watchlist.example.txt`** — template; copy to `watchlist.txt` (gitignored,
  not committed — this repo is public and a scanner, not a watchlist viewer)
  and fill in your own tickers if you want the optional "Owned" column.
  Missing `watchlist.txt` is not an error — the scan just runs with an empty
  watchlist and no ticker is flagged as owned.

## Performance note

`POST /api/scan` recomputes indicators for every ticker on every request
(no caching of computed values, only of raw prices) -- measured at ~3s for
the full ~1400-ticker universe with RSI + 4 SMA periods + 4 EMA periods all
enabled at once. Fine for a single-user tool with the frontend's 300ms
debounce, but if this ever needs to feel more instant (e.g. multiple
concurrent users), the straightforward next step is vectorizing across
tickers with numpy instead of one `pandas.Series` + rolling/ewm call per
ticker.

## Rate-limit safeguards

1. Full-universe yfinance pulls happen only on the internal timer (every
   `REFRESH_INTERVAL_MIN`, default 45) or a manual force-refresh — never per
   page view or per filter change.
2. Reuses `market_screener.py`'s already-battle-tested
   `fetch_price_history_bulk` (batching + inter-batch sleep + retry/backoff)
   rather than writing new fetch logic.
3. Manual refresh is guarded by a single-flight lock (no two scans ever run
   concurrently) plus a short (`REFRESH_COOLDOWN_SEC`, default 120s)
   cooldown against rapid back-to-back clicks — occasional manual triggering
   by one person isn't the failure mode that causes yfinance rate-limiting;
   rapid/concurrent/automated polling is.
4. On-disk `cache.json` survives a host sleep/restart, so a cold start never
   forces an immediate re-fetch — it serves stale data first, refreshes in
   the background.
5. Based on `market_screener.py`'s own constants (`BATCH_SIZE=40`,
   `INTER_BATCH_SLEEP_SEC=1.5`), a full SET+US universe scan (~500-700
   tickers) is expected to take on the order of a few minutes, not hours —
   confirm this empirically with the local smoke test below before tuning
   the numbers above.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env        # then edit DASHBOARD_USERNAME/PASSWORD
cp watchlist.example.txt watchlist.txt   # optional: fill in your own tickers for the "Owned" column
```

**1. Smoke-test the scan engine alone first** (confirms no yfinance 429s and
measures real wall-clock time — this informs the refresh-interval/cooldown
numbers above):

```bash
python scan_engine.py
```

**2. Run the full app:**

```bash
uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000` (browser will prompt for the Basic Auth
credentials from `.env`).

## Verification checklist

All of these have been run at least once against the real universe (not
just syntax-checked) — re-run after any change to `scan_engine.py`,
`conditions.py`, or `app.py`, per this project's "test locally before
pushing" rule:

- [x] `python scan_engine.py` completes with no visible yfinance 429s; note
      the wall-clock time. (Last measured: ~120s for ~1400 tickers.)
- [x] `python test_conditions_manual.py` passes (synthetic data, no
      network) — covers SMA/EMA/RSI matching, insufficient-history
      exclusion, all-disabled vacuous match, and the `=` tolerance.
- [x] `POST /api/scan` with different RSI/SMA/EMA/market/owned/squeeze/q
      combinations returns different, correct results, with no new
      yfinance calls per request (watch the uvicorn logs — only the
      scheduler/refresh path should ever log a fetch). Cross-checked one
      ticker's RSI(50) value against the old fixed-score system's output —
      matched to 2 decimals, validating the `MAX_STORED_CLOSES` trim.
- [x] Malformed conditions (period 0/negative/5000, invalid operator
      string, RSI period past its Field bound) don't 500 — either silently
      excluded/vacuous or a clean 422.
- [x] Kill and restart the app; confirm it loads `cache.json` and serves
      data immediately instead of blocking on a fresh scan.
- [x] Click "Refresh now" twice in a row within the cooldown window; confirm
      the second click gets a `cooldown` response, not a second concurrent
      scan. Wait past the cooldown and confirm a fresh refresh is allowed.
- [x] Confirm HTTP Basic Auth actually blocks unauthenticated access to
      every route on the live deploy, not just `/` — this project shipped a
      real bug here once (an `app.mount("/static", ...)` that bypassed
      auth entirely) that only local route-by-route curling caught.

## Hosting

**Render free tier**, using the included `render.yaml` Blueprint:

1. [dashboard.render.com](https://dashboard.render.com) → sign in with GitHub
   (first time only: authorize the Render GitHub App for this repo, or all
   repos).
2. **New +** → **Blueprint** → pick `jeeeepp/dashboard-web` → Render reads
   `render.yaml` and pre-fills the service (name, build/start command, free
   plan).
3. It'll prompt for `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` (left blank
   in `render.yaml` on purpose — this is a public repo, real credentials
   never belong in it). Fill in real values, not the `testuser`/`testpass123`
   used for local testing.
4. Deploy. First boot has no `cache.json` yet, so the dashboard loads
   immediately but shows 0 rows for the first ~2 minutes while the initial
   scan runs in the background (poll `/api/status` / watch the "scanning..."
   state in the UI — this is expected, not a bug).

**Known free-tier limitation**: Render's free web services don't include a
persistent disk, so `cache.json` is not guaranteed to survive a redeploy or
a cold restart after ~15min idle — expect an empty-then-populating dashboard
again after either. If that's annoying in practice, either upgrade to a paid
Render disk, or move to Fly.io (small always-on VM, same app code) — worth
revisiting after trying the free tier first.

Separate cron job + backend, self-hosted VM, or a serverless
scheduled-function split are all still on the table if Render doesn't work
out in practice.
