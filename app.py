#!/usr/bin/env python3
"""
app.py — FastAPI service: internal scheduler + cache + filter API + static
frontend. See README.md for the full design rationale (this file implements
the "Design" section of the original plan almost verbatim).

Run locally:
    uvicorn app:app --reload --port 8000

Deploy: see README.md's Hosting section.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from conditions import ScanFilterRequest, evaluate_ticker, keep_row
from scan_engine import run_full_scan, rows_to_dicts

load_dotenv()  # no-op in prod (Render sets real env vars directly)

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "cache.json"
STATIC_DIR = BASE_DIR / "static"

REFRESH_INTERVAL_SEC = int(os.environ.get("REFRESH_INTERVAL_MIN", "45")) * 60
REFRESH_COOLDOWN_SEC = int(os.environ.get("REFRESH_COOLDOWN_SEC", "120"))

log = logging.getLogger("dashboard.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

security = HTTPBasic()


# ---------------------------------------------------------------------------
# Cache + scheduler state
# ---------------------------------------------------------------------------

class ScanState:
    """All mutable server state lives here (not module globals) so the
    lifespan hook can construct exactly one instance per process."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.stats: dict = {}
        self.lock = asyncio.Lock()
        self.last_refresh_finished = 0.0
        self.scheduler_task: Optional[asyncio.Task] = None

    def load_from_disk(self) -> bool:
        if not CACHE_PATH.exists():
            return False
        try:
            payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            self.rows = payload.get("rows", [])
            self.stats = payload.get("stats", {})
            log.info(
                "Loaded cached scan from disk (%d rows, generated %s).",
                len(self.rows), self.stats.get("generated_utc"),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load cache.json (%s) -- starting empty.", exc)
            return False

    def save_to_disk(self) -> None:
        # Atomic write (temp file + os.replace) -- same pattern
        # agent-notification's signal_bot.py uses for state.json, so a crash
        # mid-write never leaves a corrupt cache.json behind.
        tmp_path = CACHE_PATH.with_suffix(".json.tmp")
        payload = {"rows": self.rows, "stats": self.stats}
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, CACHE_PATH)

    def is_stale(self) -> bool:
        generated = self.stats.get("generated_utc")
        if not generated:
            return True
        try:
            generated_dt = datetime.fromisoformat(generated)
        except ValueError:
            return True
        age_sec = (datetime.now(timezone.utc) - generated_dt).total_seconds()
        return age_sec > REFRESH_INTERVAL_SEC

    async def _do_scan(self) -> None:
        async with self.lock:
            try:
                loop = asyncio.get_running_loop()
                # run_full_scan() is synchronous/blocking (yfinance calls) --
                # run it in a thread so it never blocks the event loop (and
                # therefore never blocks /api/scan reads from the cache
                # while a scan is in flight).
                rows, stats = await loop.run_in_executor(None, run_full_scan)
                self.rows = rows_to_dicts(rows)
                self.stats = stats
                self.save_to_disk()
                log.info("Scan complete: %s", stats)
            except Exception as exc:  # noqa: BLE001
                log.error("Scan failed: %s", exc)
                self.stats = {**self.stats, "last_error": str(exc)}
            finally:
                # Cooldown is measured from when a scan FINISHES, not when it
                # started -- a full scan (~a few minutes, see README.md) can
                # take longer than the cooldown window itself, so timing it
                # from the start would let it expire mid-scan or the instant
                # a scan completes, defeating the "block rapid back-to-back
                # clicks" purpose the cooldown exists for.
                self.last_refresh_finished = time.monotonic()

    def start_refresh_if_allowed(self, *, bypass_cooldown: bool = False) -> dict:
        """Non-blocking: kicks off a background scan task if allowed and
        returns immediately with a status the caller can show/poll on.
        Single-flight lock (self.lock) + short cooldown -- see README.md's
        rate-limit-safeguards section for why these numbers are small."""
        if self.lock.locked():
            return {"status": "already_running"}

        now = time.monotonic()
        elapsed = now - self.last_refresh_finished
        if not bypass_cooldown and self.last_refresh_finished and elapsed < REFRESH_COOLDOWN_SEC:
            return {
                "status": "cooldown",
                "retry_after_sec": round(REFRESH_COOLDOWN_SEC - elapsed, 1),
            }

        asyncio.create_task(self._do_scan())
        return {"status": "started"}

    async def scheduler_loop(self) -> None:
        """Passive refresh every REFRESH_INTERVAL_SEC. bypass_cooldown=True
        because the cooldown exists to stop rapid manual clicks, not to
        block the scheduler's own regular cadence."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_SEC)
            result = self.start_refresh_if_allowed(bypass_cooldown=True)
            log.info("Scheduled refresh: %s", result)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = ScanState()
    app.state.scan = state

    loaded = state.load_from_disk()
    if not loaded or state.is_stale():
        # Fire-and-forget: serves whatever's cached (possibly nothing) to
        # the first request immediately rather than blocking startup on a
        # multi-minute full-universe scan.
        state.start_refresh_if_allowed(bypass_cooldown=True)

    state.scheduler_task = asyncio.create_task(state.scheduler_loop())
    try:
        yield
    finally:
        state.scheduler_task.cancel()


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    username = os.environ.get("DASHBOARD_USERNAME", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not username or not password:
        # Fail CLOSED: if creds aren't configured, refuse rather than
        # silently serving personal stock picks unauthenticated.
        raise HTTPException(
            status_code=500,
            detail="Server auth not configured (DASHBOARD_USERNAME/DASHBOARD_PASSWORD unset).",
        )
    ok_user = secrets.compare_digest(credentials.username, username)
    ok_pass = secrets.compare_digest(credentials.password, password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/api/scan")
async def api_scan(req: ScanFilterRequest, _: None = Depends(check_auth)):
    """Filters + evaluates conditions against the IN-MEMORY CACHE ONLY -- no
    yfinance call ever happens on this path, no matter how conditions
    change (RSI/SMA/EMA periods, operators, thresholds all recompute purely
    from each row's cached `closes`, see conditions.py). POST (not GET)
    because the condition payload is a nested structure, not a few scalar
    query params. See ScanState._do_scan for the only code path that ever
    calls run_full_scan()."""
    state: ScanState = app.state.scan

    matched: list[dict] = []
    for row in state.rows:
        if not keep_row(row, req):
            continue
        evaluated = evaluate_ticker(row["closes"], row["price"], req)
        if evaluated is None or not evaluated["match"]:
            continue
        # Don't echo the raw close-price series back to the client -- it's
        # only needed server-side for indicator math.
        public_row = {k: v for k, v in row.items() if k != "closes"}
        matched.append({**public_row, **evaluated})

    return {"rows": matched, "stats": state.stats, "count": len(matched), "total": len(state.rows)}


@app.get("/api/status")
async def api_status(_: None = Depends(check_auth)):
    state: ScanState = app.state.scan
    elapsed = time.monotonic() - state.last_refresh_finished if state.last_refresh_finished else None
    cooldown_remaining = None
    if elapsed is not None and elapsed < REFRESH_COOLDOWN_SEC:
        cooldown_remaining = round(REFRESH_COOLDOWN_SEC - elapsed, 1)
    return {
        "scanning": state.lock.locked(),
        "stats": state.stats,
        "cooldown_remaining_sec": cooldown_remaining,
        "row_count": len(state.rows),
    }


@app.post("/api/refresh")
async def api_refresh(_: None = Depends(check_auth)):
    state: ScanState = app.state.scan
    return state.start_refresh_if_allowed()


@app.get("/", dependencies=[Depends(check_auth)])
async def index():
    # Deliberately NOT an app.mount("/static", StaticFiles(...)) -- a
    # StaticFiles mount is a separate ASGI sub-app that does not go through
    # this route's `dependencies=[Depends(check_auth)]`, so it would serve
    # index.html completely unauthenticated (confirmed on a live deploy).
    # index.html is self-contained (inline CSS/JS, no /static/* references),
    # so serving it only via this single auth-gated route is sufficient.
    return FileResponse(STATIC_DIR / "index.html")
