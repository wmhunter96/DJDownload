"""
main.py — DJDownload FastAPI server.

Endpoints:
  GET  /              → serve web UI
  GET  /api/settings  → return current config
  POST /api/settings  → update config
  POST /api/jobs      → submit a download job
  GET  /api/jobs      → list all jobs
  GET  /api/jobs/{id} → get job status + logs
  GET  /api/status    → health check
"""

import asyncio
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.config import load_settings, save_settings
from backend.downloader import (
    fetch_metadata,
    download_audio,
    download_video,
    update_yt_dlp,
    YtDlpForbiddenError,
)
from backend.tagging import tag_mp3

import os

app = FastAPI(title="DJDownload")


@app.on_event("startup")
async def _update_yt_dlp_on_startup():
    """Self-update yt-dlp at startup so long-running containers don't go stale."""
    def log(msg: str):
        print(f"[startup] {msg}", flush=True)

    try:
        await asyncio.to_thread(update_yt_dlp, log)
    except Exception as exc:
        print(f"[startup] yt-dlp update failed, continuing with existing binary: {exc}", flush=True)


# ---------------------------------------------------------------------------
# In-memory job store  (replace with SQLite for persistence later)
# ---------------------------------------------------------------------------

jobs: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SubmitJobRequest(BaseModel):
    url: str
    artist_override: Optional[str] = None   # overrides settings artist mode for this job


class UpdateSettingsRequest(BaseModel):
    audio_enabled: bool
    audio_output_dir: str
    video_enabled: bool
    video_output_dir: str
    artist_mode: str          # "channel" | "custom"
    artist_custom_name: str


# ---------------------------------------------------------------------------
# Routes — settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.post("/api/settings")
def post_settings(req: UpdateSettingsRequest):
    settings = {
        "audio": {
            "enabled": req.audio_enabled,
            "output_dir": req.audio_output_dir,
        },
        "video": {
            "enabled": req.video_enabled,
            "output_dir": req.video_output_dir,
        },
        "artist": {
            "mode": req.artist_mode,
            "custom_name": req.artist_custom_name,
        },
    }
    save_settings(settings)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — jobs
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
async def submit_job(req: SubmitJobRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "url": req.url,
        "artist_override": req.artist_override,
        "status": "queued",    # queued | running | done | error
        "created_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "logs": [],
        "result": {},
        "title": None,          # YouTube video title (from yt-dlp metadata)
        "uploader": None,       # YouTube channel name (from yt-dlp metadata)
        "thumbnail": None,      # YouTube video thumbnail URL (from yt-dlp metadata)
        "artist": None,         # resolved artist: override, or settings custom name, or uploader
    }
    jobs[job_id] = job
    background_tasks.add_task(_run_job, job_id)
    return {"job_id": job_id}


@app.get("/api/jobs")
def list_jobs():
    return list(reversed(list(jobs.values())))


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ---------------------------------------------------------------------------
# Routes — misc
# ---------------------------------------------------------------------------

@app.get("/api/status")
def status():
    return {"status": "ok"}


# Serve the frontend SPA for all non-API routes
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

@app.get("/")
def serve_ui():
    index = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return JSONResponse({"error": "Frontend not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------

async def _run_with_forbidden_retry(func, *args, log, **kwargs):
    """Run `func` in a thread; on a 403 Forbidden, self-update yt-dlp once and retry."""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except YtDlpForbiddenError:
        log("⚠ yt-dlp got a 403 Forbidden — binary looks stale, self-updating...")
        await asyncio.to_thread(update_yt_dlp, log)
        return await asyncio.to_thread(func, *args, **kwargs)


async def _run_job(job_id: str):
    job = jobs[job_id]
    job["status"] = "running"

    def log(msg: str):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        job["logs"].append(f"[{ts}] {msg}")

    settings = load_settings()

    try:
        # 1. Fetch metadata
        log(f"Fetching metadata for: {job['url']}")
        meta = await _run_with_forbidden_retry(fetch_metadata, job["url"], log=log)
        title = meta["title"]
        uploader = meta["uploader"]
        job["title"] = title
        job["uploader"] = uploader
        job["thumbnail"] = meta.get("thumbnail") or None
        log(f"Title:    {title}")
        log(f"Uploader: {uploader}")

        # 2. Resolve artist
        override = job.get("artist_override", "").strip() if job.get("artist_override") else ""
        if override:
            artist = override
        elif settings["artist"]["mode"] == "custom" and settings["artist"]["custom_name"].strip():
            artist = settings["artist"]["custom_name"].strip()
        else:
            artist = uploader
        job["artist"] = artist
        log(f"Artist:   {artist}")

        audio_path = None
        video_path = None

        # 3. Download video
        if settings["video"]["enabled"]:
            log("Starting video download...")
            video_path = await _run_with_forbidden_retry(
                download_video,
                job["url"],
                settings["video"]["output_dir"],
                log,
                log=log,
            )
            if video_path:
                log(f"Video saved: {video_path}")
            else:
                log("ℹ Video saved (webm or other format — path capture skipped).")

        # 4. Download audio
        if settings["audio"]["enabled"]:
            log("Starting audio download...")
            audio_path = await _run_with_forbidden_retry(
                download_audio,
                job["url"],
                settings["audio"]["output_dir"],
                log,
                log=log,
            )
            if audio_path:
                log(f"Audio saved: {audio_path}")
            else:
                raise RuntimeError("Audio download failed — MP3 path not found.")

            # 5. Tag MP3
            log("Tagging MP3...")
            final_path = await asyncio.to_thread(
                tag_mp3,
                audio_path,
                title,
                artist,
                title,   # album = title
            )
            log(f"✅ Tagged MP3: {final_path}")
            job["result"]["audio_path"] = final_path

        if video_path:
            job["result"]["video_path"] = video_path

        job["status"] = "done"
        log("🎉 Job complete.")

    except Exception as exc:
        job["status"] = "error"
        log(f"❌ Error: {exc}")

    finally:
        job["finished_at"] = datetime.utcnow().isoformat()
