"""
tagging.py — MP3 tagging via ffmpeg.

Writes ID3v2.3 tags to an MP3 in-place:
  - title, artist, album  (from metadata)
  - RELEASETYPE = album;live  (makes Plex treat this as a live album)

Preserves any embedded artwork already in the file.
"""

import os
import subprocess
import shutil
import tempfile
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")


def tag_mp3(
    src: str,
    title: str,
    artist: str,
    album: str,
    release_type: str = "album;live",
) -> str:
    """
    Tag the MP3 at `src` in-place.
    Returns the final path (same as src on success).
    Raises RuntimeError on failure.
    """
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(f"MP3 not found: {src}")

    # Write to a temp file in the same directory, then atomically replace
    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".mp3", dir=src_path.parent, prefix=".tmp_tagged_"
    )
    os.close(tmp_fd)

    try:
        _ffmpeg_tag(
            src=str(src_path),
            dst=tmp_path,
            title=_sanitize(title),
            artist=_sanitize(artist),
            album=_sanitize(album),
            release_type=release_type,
        )

        # Atomic replace
        shutil.move(tmp_path, str(src_path))
    except Exception:
        # Clean up temp file on failure
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    return str(src_path)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _ffmpeg_tag(src: str, dst: str, title: str, artist: str, album: str, release_type: str) -> None:
    cmd = [
        FFMPEG,
        "-y",
        "-i", src,
        "-c", "copy",
        "-id3v2_version", "3",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"album={album}",
        "-metadata", f"RELEASETYPE={release_type}",
        dst,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg tagging failed (exit {result.returncode}):\n"
            f"{result.stderr[-2000:]}"   # last 2 KB of stderr
        )


def _sanitize(value: str) -> str:
    """Remove characters that break ffmpeg -metadata values."""
    return value.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()
