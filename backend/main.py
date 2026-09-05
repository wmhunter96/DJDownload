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
  GET  /api/analytics               → per-DJ set-count leaderboard (Analytics tab)
  GET  /api/analytics/files         → files for one DJ, for the drill-down grid
  GET  /api/analytics/thumbnail     → embedded cover art for one file
  GET  /api/analytics/plex-link     → Plex Web deep link for one file ("Play in Plex")
  POST /api/analytics/plex-refresh  → nudge Plex to (re)scan its music library
  POST /api/settings/plex-test      → step-by-step Plex connection/match diagnostics
  GET  /api/status    → health check
"""

import asyncio
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
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
from backend import plex
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
    artist_override: Optional[str] = None   # if blank, the YouTube channel name is used
    mode: str = "mix"   # "mix" (default, per Settings) or "song" (video forced off, audio
                         # saved to songs.output_dir/<artist>/ instead of audio.output_dir)


class UpdateSettingsRequest(BaseModel):
    audio_enabled: bool
    audio_output_dir: str
    video_enabled: bool
    video_output_dir: str
    songs_output_dir: str = ""
    youtube_api_key: str = ""
    plex_server_url: str = ""
    plex_token: str = ""
    plex_section_name: str = ""


class DiscoverySearchRequest(BaseModel):
    dj: str   # required — searches are restricted to one DJ at a time (YouTube API quota)


class DiscoveryDismissRequest(BaseModel):
    video_id: str


class PlexTestRequest(BaseModel):
    server_url: str
    token: str
    section_name: str = ""


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
        "songs": {
            "output_dir": req.songs_output_dir,
        },
        "discovery": {
            "youtube_api_key": req.youtube_api_key,
            # dismissed_ids/known_djs/dj_counts are server-managed (see the
            # /api/discovery/* routes below), not part of the settings form —
            # carry the existing values forward.
            "dismissed_ids": current["discovery"]["dismissed_ids"],
            "known_djs": current["discovery"]["known_djs"],
            "dj_counts": current["discovery"]["dj_counts"],
        },
        "plex": {
            "server_url": req.plex_server_url,
            "token": req.plex_token,
            "section_name": req.plex_section_name,
        },
    }
    save_settings(settings)
    return {"ok": True}


@app.post("/api/settings/plex-test")
def post_settings_plex_test(req: PlexTestRequest):
    """Step-by-step Plex connection diagnostics for the Settings page's
    "Test Connection" button — tests the URL/token from the (possibly
    unsaved) form fields directly, and cross-checks the current audio
    library against Plex's index so a match failure is caught even when
    connectivity and auth are both fine."""
    settings = load_settings()
    local_filenames = [e["filename"] for e in library.scan_library(settings["audio"]["output_dir"])]
    return plex.test_connection(req.server_url, req.token, req.section_name, local_filenames)


# ---------------------------------------------------------------------------
# Routes — jobs
# ---------------------------------------------------------------------------

def _video_enabled_for(settings: dict, mode: str) -> bool:
    """"Song" mode always forces video off, regardless of the global setting."""
    return False if mode == "song" else settings["video"]["enabled"]


_INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_folder_name(name: str) -> str:
    """Make `name` safe to use as a single path segment (Linux + SMB/Windows shares)."""
    cleaned = _INVALID_FOLDER_CHARS.sub("", name).strip().strip(".")
    return cleaned or "Unknown Artist"


def _initial_operations(settings: dict, mode: str = "mix") -> List[dict]:
    """Build this job's operation list (one entry per progress bar) from current settings.

    Only includes stages that will actually run — e.g. a video-only job (audio
    disabled) has one bar, not two. Tagging isn't included: it's a near-instant
    ID3 write with no meaningful progress of its own, so it doesn't get a bar —
    it just happens after the "Audio" bar completes.
    """
    operations = []
    if _video_enabled_for(settings, mode):
        operations.append({"key": "video", "label": "Video", "status": "pending", "progress": None})
    if settings["audio"]["enabled"]:
        operations.append({"key": "audio", "label": "Audio", "status": "pending", "progress": None})
    return operations


@app.post("/api/jobs")
async def submit_job(req: SubmitJobRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    settings = load_settings()
    job = {
        "id": job_id,
        "url": req.url,
        "artist_override": req.artist_override,
        "mode": req.mode,        # "mix" or "song" — see _video_enabled_for / _run_job
        "status": "queued",    # queued | running | done | error
        "created_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "logs": [],
        "result": {},
        "title": None,          # YouTube video title (from yt-dlp metadata)
        "uploader": None,       # YouTube channel name (from yt-dlp metadata)
        "thumbnail": None,      # YouTube video thumbnail URL (from yt-dlp metadata)
        "artist": None,         # resolved artist: override, or uploader (channel name)
        "stage": None,          # human-readable current step, e.g. "Downloading audio"
        "progress": None,       # 0-100 percent for the current stage, or None if indeterminate
        "operations": _initial_operations(settings, req.mode),  # one entry per progress bar (video/audio/tagging)
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
    known = settings["discovery"]["known_djs"]
    counts = settings["discovery"]["dj_counts"]
    return {"djs": [{"name": name, "count": counts.get(name, 0)} for name in known]}


@app.post("/api/discovery/scan-library")
def scan_discovery_library():
    """Backfill the DJ dropdown (and set counts) from the audio library's ID3 tags.

    known_djs is normally built up as jobs complete (works for video-only
    downloads too, which carry no ID3 tags to scan) — this covers DJs already
    in the library from before that tracking existed, or added outside the app.
    """
    settings = load_settings()
    scanned_counts = library.count_by_artist(settings["audio"]["output_dir"])

    known = settings["discovery"]["known_djs"]
    dj_counts = settings["discovery"]["dj_counts"]
    before = set(name.lower() for name in known)

    settings["discovery"]["known_djs"] = library.merge_dj_names(known, list(scanned_counts.keys()))
    for scanned_name, count in scanned_counts.items():
        # Only trust the file count for names dj_counts doesn't already track
        # (new backfills, or older known_djs entries from before dj_counts
        # existed) — don't clobber a count already being kept in sync by
        # _remember_dj as jobs complete.
        canonical = next(n for n in settings["discovery"]["known_djs"] if n.lower() == scanned_name.lower())
        if canonical not in dj_counts:
            dj_counts[canonical] = count

    save_settings(settings)
    added = [name for name in scanned_counts if name.lower() not in before]
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
# Routes — analytics
# ---------------------------------------------------------------------------

@app.get("/api/analytics")
def get_analytics():
    """Per-DJ leaderboard for the Analytics tab, built from the audio library's
    ID3 tags (same data source as the Discover tab's known-DJ list)."""
    settings = load_settings()
    artists = library.analytics_by_artist(settings["audio"]["output_dir"])
    artists.sort(key=lambda a: (-a["count"], a["artist"].lower()))

    return {
        "artists": artists,
        "total_djs": len(artists),
        "total_sets": sum(a["count"] for a in artists),
        "most_sets": artists[0] if artists else None,
        "fewest_sets": min(artists, key=lambda a: a["count"]) if artists else None,
    }


@app.get("/api/analytics/files")
def get_analytics_files(artist: str):
    """Every downloaded file for one DJ — backs the Analytics tab's drill-down
    grid (thumbnail, title, length, size, date added)."""
    settings = load_settings()
    audio_dir = settings["audio"]["output_dir"]
    entries = library.list_files_by_artist(audio_dir, artist)

    return {
        "artist": artist,
        "files": [
            {
                "filename": e["filename"],
                "title": e["title"],
                "duration_s": e["duration_s"],
                "size_bytes": e["size_bytes"],
                "added": datetime.fromtimestamp(e["mtime"]).isoformat() if e["mtime"] is not None else None,
                "has_thumbnail": e["has_thumbnail"],
            }
            for e in entries
        ],
    }


@app.get("/api/analytics/thumbnail")
def get_analytics_thumbnail(filename: str):
    """Embedded cover art (yt-dlp's --embed-thumbnail) for one library file."""
    settings = load_settings()
    audio_dir = settings["audio"]["output_dir"]
    result = library.get_thumbnail(audio_dir, filename)
    if not result:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    data, mime = result
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/analytics/plex-link")
def get_analytics_plex_link(filename: str):
    """Plex Web deep link for one library file, matched by filename against
    Plex's own library index — backs the "Play in Plex" button on the
    Analytics tab's file grid. 404s if Plex isn't configured, unreachable,
    or hasn't scanned this file in yet."""
    settings = load_settings()
    plex_settings = settings["plex"]
    url = plex.get_play_link(plex_settings["server_url"], plex_settings["token"], filename, plex_settings["section_name"])
    if not url:
        raise HTTPException(status_code=404, detail="Not found in Plex library")
    return {"url": url}


@app.post("/api/analytics/plex-refresh")
def post_analytics_plex_refresh():
    """Nudge Plex to (re)scan its music library and drop our own cached
    filename index, so newly-downloaded sets show up as playable without
    waiting on Plex's own scan interval or our cache TTL. Fired automatically
    when the Analytics tab is opened — best-effort, always returns ok even
    if Plex isn't configured/reachable (check server logs for [plex] lines)."""
    settings = load_settings()
    plex_settings = settings["plex"]
    reached = plex.trigger_scan(plex_settings["server_url"], plex_settings["token"], plex_settings["section_name"])
    plex.invalidate_cache()
    return {"ok": True, "plex_reached": reached}


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


@app.get("/favicon.png")
def serve_favicon():
    favicon = os.path.join(FRONTEND_DIR, "favicon.png")
    if os.path.exists(favicon):
        return FileResponse(favicon)
    return JSONResponse({"error": "Favicon not found"}, status_code=404)


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
    """Record `artist` in discovery.known_djs and bump its set count.

    Called on every successful job — covers video-only downloads (no ID3 tags to
    scan) as well as audio. Pre-existing library entries are backfilled via the
    "Scan Audio Library" button (POST /api/discovery/scan-library).
    """
    artist = artist.strip()
    if not artist:
        return
    settings = load_settings()
    known = settings["discovery"]["known_djs"]
    canonical = next((n for n in known if n.lower() == artist.lower()), None)
    if canonical is None:
        known = library.merge_dj_names(known, [artist])
        settings["discovery"]["known_djs"] = known
        canonical = artist
    dj_counts = settings["discovery"]["dj_counts"]
    dj_counts[canonical] = dj_counts.get(canonical, 0) + 1
    save_settings(settings)


async def _run_job(job_id: str):
    job = jobs[job_id]
    job["status"] = "running"
    ops_by_key = {op["key"]: op for op in job["operations"]}

    def log(msg: str):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        job["logs"].append(f"[{ts}] {msg}")

    def start_op(key: str, indeterminate: bool = False):
        """Mark an operation's bar as running. `indeterminate` for steps with no % feed (e.g. tagging)."""
        op = ops_by_key.get(key)
        if op:
            op["status"] = "running"
            op["progress"] = None if indeterminate else 0

    def finish_op(key: str, status: str = "done"):
        op = ops_by_key.get(key)
        if op:
            op["status"] = status
            if status == "done" and op["progress"] is not None:
                op["progress"] = 100

    def make_progress_cb(key: str):
        """Progress callback for operation `key` — updates both its own bar and the
        legacy job.progress mirror (still used by the compact job-list card)."""
        def cb(percent: float):
            job["progress"] = percent
            op = ops_by_key.get(key)
            if op:
                op["progress"] = percent
        return cb

    settings = load_settings()
    mode = job.get("mode", "mix")
    video_enabled = _video_enabled_for(settings, mode)

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

        # 2. Resolve artist — always prefer the per-job override; fall back to
        # the YouTube channel name when it's blank.
        override = job.get("artist_override", "").strip() if job.get("artist_override") else ""
        artist = override or uploader
        job["artist"] = artist
        log(f"Artist:   {artist}")

        audio_path = None
        video_path = None

        # 3. Download video
        if video_enabled:
            job["stage"] = "Downloading video"
            start_op("video")
            log("Starting video download...")
            video_path = await _run_with_forbidden_retry(
                download_video,
                job["url"],
                settings["video"]["output_dir"],
                log,
                make_progress_cb("video"),
                log=log,
            )
            finish_op("video")
            if video_path:
                log(f"Video saved: {video_path}")
            else:
                log("ℹ Video saved (webm or other format — path capture skipped).")

        # 4. Download audio
        if settings["audio"]["enabled"]:
            if mode == "song":
                # Song mode keeps individual tracks out of the curated Sets
                # library — each goes into its own artist/channel folder
                # under songs.output_dir instead, e.g. songs_dir/bigbooty/.
                audio_output_dir = os.path.join(
                    settings["songs"]["output_dir"], _sanitize_folder_name(artist)
                )
            else:
                audio_output_dir = settings["audio"]["output_dir"]

            job["stage"] = "Downloading audio"
            start_op("audio")
            log("Starting audio download...")
            audio_path = await _run_with_forbidden_retry(
                download_audio,
                job["url"],
                audio_output_dir,
                log,
                make_progress_cb("audio"),
                log=log,
            )
            if audio_path:
                log(f"Audio saved: {audio_path}")
                finish_op("audio")
            else:
                finish_op("audio", "error")
                raise RuntimeError("Audio download failed — MP3 path not found.")

            # 5. Tag MP3 — near-instant, so no bar of its own; the "Audio" bar
            # is already done() at this point.
            job["stage"] = "Tagging"
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
        # Whatever operation was in flight when we failed didn't finish — mark it,
        # leaving its bar in place rather than resetting it (helps show where it broke).
        for op in job["operations"]:
            if op["status"] == "running":
                op["status"] = "error"

    finally:
        job["stage"] = None
        job["progress"] = None
        job["finished_at"] = datetime.utcnow().isoformat()
