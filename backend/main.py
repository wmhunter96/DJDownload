"""
main.py — DJDownload FastAPI server.

Endpoints:
  GET  /              → serve web UI
  GET  /api/settings  → return current config
  POST /api/settings  → update config
  POST /api/jobs      → submit a download job
  GET  /api/jobs      → list all jobs
  GET  /api/jobs/{id} → get job status + logs
  GET  /api/discovery/djs           → list known DJs (Discover dropdown)
  POST /api/discovery/scan-library  → backfill known DJs from audio ID3 tags
  POST /api/discovery/search        → search YouTube for missed sets (one DJ)
  POST /api/discovery/dismiss       → dismiss a candidate (won't resurface)
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
from backend import library
from backend.discovery import search_youtube, YouTubeApiError

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
    youtube_api_key: str = ""


class DiscoverySearchRequest(BaseModel):
    dj: str   # required — searches are restricted to one DJ at a time (YouTube API quota)


class DiscoveryDismissRequest(BaseModel):
    video_id: str


# ---------------------------------------------------------------------------
# Routes — settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.post("/api/settings")
def post_settings(req: UpdateSettingsRequest):
    current = load_settings()
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
        "discovery": {
            "youtube_api_key": req.youtube_api_key,
            # dismissed_ids is server-managed (see /api/discovery/dismiss),
            # not part of the settings form — carry the existing value forward.
            "dismissed_ids": current["discovery"]["dismissed_ids"],
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
        "stage": None,          # human-readable current step, e.g. "Downloading audio"
        "progress": None,       # 0-100 percent for the current stage, or None if indeterminate
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
# Routes — discovery (missed-set finder)
# ---------------------------------------------------------------------------

@app.get("/api/discovery/djs")
def list_discovery_djs():
    settings = load_settings()
    return {"djs": settings["discovery"]["known_djs"]}


@app.post("/api/discovery/scan-library")
def scan_discovery_library():
    """Backfill the DJ dropdown from the audio library's ID3 tags.

    known_djs is normally built up as jobs complete (works for video-only
    downloads too, which carry no ID3 tags to scan) — this covers DJs already
    in the library from before that tracking existed, or added outside the app.
    """
    settings = load_settings()
    scanned = library.list_artists(settings["audio"]["output_dir"])
    before = set(name.lower() for name in settings["discovery"]["known_djs"])
    settings["discovery"]["known_djs"] = library.merge_dj_names(settings["discovery"]["known_djs"], scanned)
    save_settings(settings)
    added = [name for name in scanned if name.lower() not in before]
    return {"djs": settings["discovery"]["known_djs"], "added": added}


@app.post("/api/discovery/search")
async def discovery_search(req: DiscoverySearchRequest):
    dj = req.dj.strip()
    if not dj:
        raise HTTPException(status_code=400, detail="Select a DJ to search for.")

    settings = load_settings()
    api_key = settings["discovery"]["youtube_api_key"].strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="No YouTube API key configured — add one in Settings.")

    audio_dir = settings["audio"]["output_dir"]
    dismissed_ids = set(settings["discovery"]["dismissed_ids"])
    library_entries = [e for e in library.scan_library(audio_dir) if e["artist"].lower() == dj.lower()]

    def run_search():
        try:
            candidates = search_youtube(api_key, dj)
        except YouTubeApiError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        results = []
        for candidate in candidates:
            if candidate["duration_s"] < 1800:
                continue
            if candidate["video_id"] in dismissed_ids:
                continue
            if library.is_duplicate(candidate["title"], candidate["video_id"], library_entries):
                continue
            results.append({**candidate, "matched_dj": dj})
        return results

    results = await asyncio.to_thread(run_search)
    return {"results": results}


@app.post("/api/discovery/dismiss")
def discovery_dismiss(req: DiscoveryDismissRequest):
    settings = load_settings()
    dismissed = settings["discovery"]["dismissed_ids"]
    if req.video_id not in dismissed:
        dismissed.append(req.video_id)
    save_settings(settings)
    return {"ok": True}


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


def _remember_dj(artist: str) -> None:
    """Record `artist` in discovery.known_djs so it shows up in the Discover dropdown.

    Called on every successful job — covers video-only downloads (no ID3 tags to
    scan) as well as audio. Pre-existing library entries are backfilled via the
    "Scan Audio Library" button (POST /api/discovery/scan-library).
    """
    artist = artist.strip()
    if not artist:
        return
    settings = load_settings()
    settings["discovery"]["known_djs"] = library.merge_dj_names(settings["discovery"]["known_djs"], [artist])
    save_settings(settings)


async def _run_job(job_id: str):
    job = jobs[job_id]
    job["status"] = "running"

    def log(msg: str):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        job["logs"].append(f"[{ts}] {msg}")

    def progress_cb(percent: float):
        job["progress"] = percent

    settings = load_settings()

    try:
        # 1. Fetch metadata
        job["stage"] = "Fetching metadata"
        log(f"Fetching metadata for: {job['url']}")
        meta = await _run_with_forbidden_retry(fetch_metadata, job["url"], log=log)
        title = meta["title"]
        uploader = meta["uploader"]
        youtube_id = meta.get("id") or None
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
            job["stage"] = "Downloading video"
            job["progress"] = 0
            log("Starting video download...")
            video_path = await _run_with_forbidden_retry(
                download_video,
                job["url"],
                settings["video"]["output_dir"],
                log,
                progress_cb,
                log=log,
            )
            if video_path:
                log(f"Video saved: {video_path}")
            else:
                log("ℹ Video saved (webm or other format — path capture skipped).")

        # 4. Download audio
        if settings["audio"]["enabled"]:
            job["stage"] = "Downloading audio"
            job["progress"] = 0
            log("Starting audio download...")
            audio_path = await _run_with_forbidden_retry(
                download_audio,
                job["url"],
                settings["audio"]["output_dir"],
                log,
                progress_cb,
                log=log,
            )
            if audio_path:
                log(f"Audio saved: {audio_path}")
            else:
                raise RuntimeError("Audio download failed — MP3 path not found.")

            # 5. Tag MP3
            job["stage"] = "Tagging"
            job["progress"] = None
            log("Tagging MP3...")
            final_path = await asyncio.to_thread(
                tag_mp3,
                audio_path,
                title,
                artist,
                title,   # album = title
                youtube_id=youtube_id,
            )
            log(f"✅ Tagged MP3: {final_path}")
            job["result"]["audio_path"] = final_path

        if video_path:
            job["result"]["video_path"] = video_path

        job["status"] = "done"
        log("🎉 Job complete.")
        _remember_dj(artist)

    except Exception as exc:
        job["status"] = "error"
        log(f"❌ Error: {exc}")

    finally:
        job["stage"] = None
        job["progress"] = None
        job["finished_at"] = datetime.utcnow().isoformat()
