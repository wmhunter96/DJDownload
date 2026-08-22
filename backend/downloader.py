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
import re
import time
from pathlib import Path
from typing import Optional


YT_DLP = os.environ.get("YT_DLP_BIN", "yt-dlp")

# Force IPv4: some googlevideo.com edge servers resolve to IPv6 addresses that
# Docker/Unraid hosts often can't actually route, producing
# "[Errno 101] Network is unreachable" mid-download even though metadata
# fetches (which hit a different endpoint) succeed.
NETWORK_ARGS = ["--force-ipv4"]


class YtDlpForbiddenError(RuntimeError):
    """Raised when yt-dlp returns an HTTP 403 Forbidden.

    This usually means the installed yt-dlp binary is stale relative to
    YouTube's current player-client requirements and needs to be updated.
    """


def _check_forbidden(output: str) -> None:
    """Raise YtDlpForbiddenError if `output` looks like a 403 Forbidden response."""
    if "403" in output and "forbidden" in output.lower():
        raise YtDlpForbiddenError(output)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def fetch_metadata(url: str) -> dict:
    """Return title, uploader, and thumbnail URL for a YouTube URL."""
    result = subprocess.run(
        [
            YT_DLP,
            *NETWORK_ARGS,
            "--dump-json",
            "--no-playlist",
            "--extractor-args", "youtube:player_client=default",
            url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _check_forbidden(result.stderr)
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

def download_video(url: str, output_dir: str, log_callback=None, progress_callback=None) -> Optional[str]:
    """Download best-quality video. Returns output path or None on failure."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    cmd = [
        YT_DLP,
        *NETWORK_ARGS,
        "--extractor-args", "youtube:player_client=default",
        "--no-playlist",
        "-o", output_template,
        url,
    ]

    return _run_yt_dlp(
        cmd, output_dir, ext_filter="*.webm,*.mp4,*.mkv",
        log_callback=log_callback, progress_callback=progress_callback,
    )


# ---------------------------------------------------------------------------
# Audio download
# ---------------------------------------------------------------------------

def download_audio(url: str, output_dir: str, log_callback=None, progress_callback=None) -> Optional[str]:
    """Download best-quality audio as MP3 with embedded thumbnail.
    Returns the final MP3 path."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    cmd = [
        YT_DLP,
        *NETWORK_ARGS,
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

    return _run_yt_dlp(
        cmd, output_dir, ext_filter="*.mp3",
        log_callback=log_callback, progress_callback=progress_callback,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches yt-dlp's progress line, e.g.:
#   [download]  42.7% of   10.00MiB at    1.23MiB/s ETA 00:05
_PROGRESS_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")


def _run_yt_dlp(cmd: list, output_dir: str, ext_filter: str, log_callback=None, progress_callback=None) -> Optional[str]:
    start_time = time.time()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    last_printed_path: Optional[str] = None
    captured_lines: list = []

    for line in process.stdout:
        line = line.rstrip()
        captured_lines.append(line)
        if log_callback:
            log_callback(line)
        else:
            print(line, flush=True)

        if progress_callback:
            match = _PROGRESS_RE.search(line)
            if match:
                try:
                    progress_callback(float(match.group(1)))
                except ValueError:
                    pass

        # yt-dlp --exec "after_move:echo {}" prints the final path
        stripped = line.strip()
        exts = tuple(ext_filter.replace("*", "").split(","))
        if any(stripped.endswith(e) for e in exts):
            last_printed_path = stripped

    process.wait()

    if process.returncode != 0:
        _check_forbidden("\n".join(captured_lines))
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
