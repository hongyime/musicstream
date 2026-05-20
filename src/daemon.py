"""
musicstream/daemon.py — FastAPI + APScheduler Control Plane
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyOAuth, SpotifyPKCE

from src.schemas.responses import ApiResponse, TrackStats, HealthStatus
from src.ws.manager import manager
from src.core.config import (
    LOG_DIR, TIMEZONE, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, 
    SPOTIFY_TOKEN_CACHE, DISABLE_DOWNLOADS, MAX_CONCURRENT_WORKERS
)
import src.core.tasks as tasks

# ── Logging Setup ─────────────────────────────────────────────────────────────

def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    
    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    
    # File handler
    ms_handler = logging.handlers.RotatingFileHandler(LOG_DIR / "musicstream.log", maxBytes=5*1024*1024, backupCount=3)
    ms_handler.setFormatter(fmt)
    root.addHandler(ms_handler)

_configure_logging()
logger = logging.getLogger("musicstream.daemon")

# ── Globals ───────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=TIMEZONE)
_start_time = time.time()
_background_tasks: set = set()  # Strong refs to fire-and-forget tasks; asyncio only holds weakrefs and will GC unsupervised tasks mid-flight.

# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Step 1 & 2: DB + migrations
    logger.info("Initializing DB and running migrations...")
    try:
        from src.db import init_db, run_migrations, wait_for_db
        engine = wait_for_db()
        init_db(engine=engine)
        run_migrations()
    except Exception as e:
        logger.error("DB initialization failed: %s", e)
        raise SystemExit(1) from e
    
    # Start background startup sequence
    _bg_task = asyncio.create_task(_background_startup())
    _background_tasks.add(_bg_task)
    _bg_task.add_done_callback(_background_tasks.discard)
    
    yield
    
    # Shutdown logic
    scheduler.shutdown()

async def _background_startup():
    """Run the 9-step startup sequence in the background."""
    try:
        logger.info("Step 3/9: Skipping legacy banner (UI-only now)")

        if os.environ.get("SKIP_STARTUP_INTEGRITY", "true").lower() in ("1", "true", "yes", "on"):
            logger.info("Step 4/9: Integrity check SKIPPED on startup (runs Sun 05:00 via cron). Set SKIP_STARTUP_INTEGRITY=false to re-enable.")
        else:
            logger.info("Step 4/9: Running integrity check…")
            await asyncio.to_thread(tasks.integrity_check)

        logger.info("Step 5/9: Running Spotify incremental sync…")
        await asyncio.to_thread(tasks.spotify_incremental_sync)

        logger.info("Step 6/9: Running download pipeline…")
        run_id = await asyncio.to_thread(tasks._record_run_start, "startup")
        dl, fail = await asyncio.to_thread(tasks.download_pipeline)
        await asyncio.to_thread(tasks._record_run_complete, run_id=run_id, downloaded=dl, failed=fail)

        logger.info("Step 7/9: Running ListenBrainz discovery…")
        await asyncio.to_thread(tasks.listenbrainz_discovery)

        logger.info("Step 8/9: Running DB backup…")
        await asyncio.to_thread(tasks.db_backup)

        logger.info("Step 9/9: Starting APScheduler…")
        _register_scheduler_jobs()
        scheduler.start()

        _hb_task = asyncio.create_task(_broadcast_health())
        _background_tasks.add(_hb_task)
        _hb_task.add_done_callback(_background_tasks.discard)

        logger.info("Daemon fully initialised. Scheduler running.")
    except Exception as exc:
        logger.error("Background startup failed: %s", exc, exc_info=True)

app = FastAPI(title="Musicstream API", lifespan=lifespan)

# ── Background Tasks ──────────────────────────────────────────────────────────

async def _broadcast_health():
    while True:
        try:
            health = [
                {
                    "service": "Daemon",
                    "status": "online",
                    "latency_ms": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                },
                {
                    "service": "Database",
                    "status": "online",
                    "updated_at": datetime.now(timezone.utc).isoformat()
                },
                {
                    "service": "Scheduler",
                    "status": "online" if scheduler.running else "idle",
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
            ]
            await manager.broadcast(json.dumps(health))
        except Exception as e:
            logger.error("Health broadcast error: %s", e)
        await asyncio.sleep(5)

def _register_scheduler_jobs():
    scheduler.add_job(tasks.spotify_incremental_sync, "cron", minute="*/15", id="spotify_sync", replace_existing=True)
    scheduler.add_job(tasks.full_download_pipeline, "cron", hour=3, id="download_pipeline", replace_existing=True)
    scheduler.add_job(tasks.listenbrainz_discovery, "cron", hour=4, id="lb_discovery", replace_existing=True)
    scheduler.add_job(tasks.full_integrity_check, "cron", day_of_week="sun", hour=5, id="integrity_check", replace_existing=True)
    scheduler.add_job(tasks.db_backup, "cron", day_of_week="sun", hour=5, id="db_backup", replace_existing=True)

# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/api/musicstream/stats", response_model=ApiResponse[TrackStats])
async def get_stats():
    from src.db import get_session
    from src.models import Track
    try:
        with get_session() as session:
            total = session.query(Track).count()
            dl = session.query(Track).filter(Track.status == "downloaded").count()
            pend = session.query(Track).filter(Track.status == "pending").count()
            fail = session.query(Track).filter(Track.status.in_(["failed", "failed_validation", "timed_out"])).count()
            active = session.query(Track).filter(Track.status == "downloading").count()
            
            stats = TrackStats(
                total_tracks=total,
                downloaded=dl,
                pending=pend,
                failed=fail,
                active=active,
                progress_pct=(dl / total * 100) if total > 0 else 0
            )
            return ApiResponse(data=stats)
    except Exception as e:
        return ApiResponse(error=str(e))

@app.get("/api/musicstream/tracks")
async def get_tracks(status: str = "pending", limit: int = 100):
    from src.db import get_session
    from src.models import Track
    try:
        with get_session() as session:
            query = session.query(Track)
            if status == "failed":
                query = query.filter(Track.status.in_(["failed", "failed_validation", "timed_out"]))
            else:
                query = query.filter(Track.status == status)
            
            tracks = query.order_by(Track.updated_at.desc()).limit(limit).all()
            return ApiResponse(data=[{
                "id": t.id,
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "status": t.status,
                "method": t.download_method,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None
            } for t in tracks])
    except Exception as e:
        return ApiResponse(error=str(e))

@app.get("/api/musicstream/metrics")
async def get_metrics():
    from src.db import get_session
    from src.models import DownloadAttempt
    from sqlalchemy import func
    try:
        with get_session() as session:
            results = session.query(
                DownloadAttempt.method,
                DownloadAttempt.success,
                func.count(DownloadAttempt.id)
            ).group_by(DownloadAttempt.method, DownloadAttempt.success).all()
            
            metrics = {}
            for method, success, count in results:
                if method not in metrics:
                    metrics[method] = {"success": 0, "fail": 0, "total": 0}
                if success:
                    metrics[method]["success"] += count
                else:
                    metrics[method]["fail"] += count
                metrics[method]["total"] += count
            
            data = []
            for method, stats in metrics.items():
                data.append({
                    "id": method,
                    "method": method,
                    "success": stats["success"],
                    "fail": stats["fail"],
                    "total": stats["total"],
                    "rate": round(stats["success"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
                })
            
            return ApiResponse(data=data)
    except Exception as e:
        return ApiResponse(error=str(e))

@app.post("/api/musicstream/sync")
async def trigger_sync():
    await asyncio.to_thread(tasks.spotify_incremental_sync)
    return ApiResponse(data={"queued": True})

@app.post("/api/musicstream/integrity")
async def trigger_integrity():
    await asyncio.to_thread(tasks.integrity_check)
    return ApiResponse(data={"queued": True})

@app.post("/api/musicstream/tracks/reset-failed")
async def reset_failed():
    from src.db import get_session
    from src.models import Track, TrackStatus
    try:
        with get_session() as session:
            count = session.query(Track).filter(
                Track.status.in_(["failed", "failed_validation", "timed_out"])
            ).update({"status": TrackStatus.PENDING.value}, synchronize_session=False)
            session.commit()
            return ApiResponse(data={"reset_count": count})
    except Exception as e:
        return ApiResponse(error=str(e))

# ── Spotify Auth ──────────────────────────────────────────────────────────────

_SCOPES = "playlist-read-private playlist-read-collaborative user-library-read user-follow-read user-read-recently-played"
_REDIRECT_URI = "http://127.0.0.1:9079/auth/spotify/callback"

def _get_auth_manager():
    """Create a fresh SpotifyPKCE manager for the OAuth flow."""
    # Use PKCE (like scraper.py) for the browser flow
    return SpotifyPKCE(
        client_id=SPOTIFY_CLIENT_ID,
        redirect_uri=_REDIRECT_URI,
        scope=_SCOPES,
        cache_handler=CacheFileHandler(cache_path=SPOTIFY_TOKEN_CACHE),
        open_browser=False
    )

@app.api_route("/auth/spotify/login", methods=["GET", "POST"])
async def spotify_login(request: Request):
    if not SPOTIFY_CLIENT_ID:
        logger.error("Spotify login failed: SPOTIFY_CLIENT_ID missing")
        raise HTTPException(status_code=500, detail="SPOTIFY_CLIENT_ID missing")
    
    auth_manager = _get_auth_manager()
    auth_url = auth_manager.get_authorize_url()
    logger.info("Initiating Spotify OAuth: %s", auth_url)
    return RedirectResponse(auth_url)

@app.get("/auth/spotify/callback")
async def spotify_callback(code: str = None, error: str = None):
    if error:
        logger.error("Spotify callback returned error: %s", error)
        return RedirectResponse("/?error=" + error)

    if code:
        try:
            auth_manager = _get_auth_manager()
            # This exchange requires the code_verifier stored in the auth_manager state
            # but since we are stateless between requests, let's try to just exchange it.
            token = await asyncio.to_thread(auth_manager.get_access_token, code)
            if token:
                logger.info("Spotify token successfully obtained via UI flow.")
            else:
                logger.error("Spotify token exchange returned None.")
        except Exception as e:
            logger.error("Failed to exchange Spotify code: %s", exc_info=True)
    
    return RedirectResponse("/")

@app.get("/api/musicstream/auth/status")
async def get_auth_status(request: Request):
    if not SPOTIFY_CLIENT_ID:
        return ApiResponse(data={"status": "missing_config", "client_id": None})
    auth_manager = _get_auth_manager()
    is_valid = auth_manager.validate_token(auth_manager.get_cached_token()) is not None
    return ApiResponse(data={
        "status": "authenticated" if is_valid else "needs_auth",
        "client_id": SPOTIFY_CLIENT_ID,
        "redirect_uri": _REDIRECT_URI
    })

# ── WebSockets ────────────────────────────────────────────────────────────────

@app.websocket("/ws/health")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ── Static Files ──────────────────────────────────────────────────────────────

static_path = Path("static")
static_path.mkdir(exist_ok=True)

if (static_path / "assets").exists():
    app.mount("/assets", StaticFiles(directory="static/assets"), name="static")

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    file_path = static_path / full_path
    if file_path.is_file():
        return FileResponse(file_path)
    index_path = static_path / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse({"error": "Dashboard not built. Run 'npm run build' in frontend folder."}, status_code=404)
