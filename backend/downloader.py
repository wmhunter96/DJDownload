"""
downloader.py — yt-dlp wrapper for DJDownload.

Responsibilities:
  - Fetch video metadata (title, uploader, thumbnail URL)
  - Download video (best quality)
  - Download audio as MP3 with embedded thumbnail
  - Return the final MP3 path for tagging
"""

import subprocess
import sys
import json
import os
import glob
import time
from pathlib import Path
from typing import Optional


YT_DLP = os.environ.get("YT_DLP_BIN", "yt-dlp")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def fetch_metadata(url: str) -> dict:
    """Return title, uploader, and thumbnail URL for a YouTube URL."""
    result = subprocess.run(
        [
            YT_DLP,
            "--dump-json",
            "--no-playlist",
            "--extractor-args", "youtube:player_client=default",
            url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata fetch failed:\n{result.stderr}")

    data = json.loads(result.stdout)
    return {
        "title": data.get("title", "Unknown Title").strip(),
        "uploader": data.get("uploader", data.get("channel", "Unknown Artist")).strip(),
        "thumbnail": data.get("thumbnail", ""),
    }


# ---------------------------------------------------------------------------
# Video download
# ---------------------------------------------------------------------------

def download_video(url: str, output_dir: str, log_callback=None) -> Optional[str]:
    """Download best-quality video. Returns output path or None on failure."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    cmd = [
        YT_DLP,
        "--extractor-args", "youtube:player_client=default",
        "--no-playlist",
        "-o", output_template,
        url,
    ]

    return _run_yt_dlp(cmd, output_dir, ext_filter="*.webm,*.mp4,*.mkv", log_callback=log_callback)


# ---------------------------------------------------------------------------
# Audio download
# ---------------------------------------------------------------------------

def download_audio(url: str, output_dir: str, log_callback=None) -> Optional[str]:
    """Download best-quality audio as MP3 with embedded thumbnail.
    Returns the final MP3 path."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    cmd = [
        YT_DLP,
        "--extractor-args", "youtube:player_client=default",
        "--no-playlist",
        "--embed-thumbnail",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", output_template,
        "--exec", "after_move:echo {}",
        url,
    ]

    return _run_yt_dlp(cmd, output_dir, ext_filter="*.mp3", log_callback=log_callback)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_yt_dlp(cmd: list, output_dir: str, ext_filter: str, log_callback=None) -> Optional[str]:
    start_time = time.time()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    last_printed_path: Optional[str] = None

    for line in process.stdout:
        line = line.rstrip()
        if log_callback:
            log_callback(line)
        else:
            print(line, flush=True)

        # yt-dlp --exec "after_move:echo {}" prints the final path
        stripped = line.strip()
        exts = tuple(ext_filter.replace("*", "").split(","))
        if any(stripped.endswith(e) for e in exts):
            last_printed_path = stripped

    process.wait()

    if process.returncode != 0:
        return None

    # Trust printed path first
    if last_printed_path and Path(last_printed_path).exists():
        return last_printed_path

    # Fallback: newest matching file written since we started
    cutoff = start_time - 5  # small buffer
    exts = ext_filter.split(",")
    candidates = [
        f for ext in exts
        for f in glob.glob(os.path.join(output_dir, ext))
        if os.path.getmtime(f) >= cutoff
    ]
    if candidates:
        return max(candidates, key=os.path.getmtime)

    return None


def update_yt_dlp(log_callback=None) -> bool:
    """Self-update yt-dlp. Returns True on success."""
    result = subprocess.run(
        [YT_DLP, "-U"],
        capture_output=True,
        text=True,
    )
    msg = result.stdout + result.stderr
    if log_callback:
        log_callback(msg)
    return result.returncode == 0
