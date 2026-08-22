"""
library.py — scans the audio output directory to answer "what do we already have".

Used by the discovery/missed-set-finder feature (backend/discovery.py) to:
  - list the DJs already in the library (for the DJ picker)
  - dedupe YouTube search candidates against sets already downloaded

Also used by the analytics feature (GET /api/analytics) to build the
per-DJ leaderboard (set count, total duration/size, most recent set).
"""

import glob
import os
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Optional, TypedDict

from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp3 import MP3


class LibraryEntry(TypedDict):
    artist: str
    title: str
    youtube_id: Optional[str]
    duration_s: Optional[float]
    size_bytes: Optional[int]
    mtime: Optional[float]


class ArtistAnalytics(TypedDict):
    artist: str
    count: int
    total_duration_s: float
    total_size_bytes: int
    most_recent_added: Optional[str]   # ISO 8601 date/time, or None


def scan_library(audio_dir: str) -> List[LibraryEntry]:
    """Read artist/title/video-id tags off every MP3 in `audio_dir`.

    Files that can't be read (missing tags, corrupt ID3, etc.) are skipped
    rather than failing the whole scan. Duration/size/mtime are best-effort —
    used only by analytics, so a failure there just leaves those fields None
    rather than dropping the entry.
    """
    entries: List[LibraryEntry] = []
    for path in glob.glob(os.path.join(audio_dir, "*.mp3")):
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            continue
        except Exception:
            continue

        artist = _first_text(tags, "TPE1")
        title = _first_text(tags, "TIT2")
        if not artist or not title:
            continue

        youtube_id = _first_text(tags, "TXXX:YOUTUBE_ID")

        duration_s: Optional[float] = None
        try:
            duration_s = MP3(path).info.length
        except Exception:
            pass

        size_bytes: Optional[int] = None
        mtime: Optional[float] = None
        try:
            stat = os.stat(path)
            size_bytes = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            pass

        entries.append({
            "artist": artist,
            "title": title,
            "youtube_id": youtube_id or None,
            "duration_s": duration_s,
            "size_bytes": size_bytes,
            "mtime": mtime,
        })

    return entries


def analytics_by_artist(audio_dir: str) -> List[ArtistAnalytics]:
    """Aggregate the library into one row per DJ/artist for the Analytics tab.

    "Date added" isn't a tag `tag_mp3` writes, so it's approximated with the
    file's mtime (unaffected by tagging, since tags.save() rewrites in place).
    """
    canonical: Dict[str, str] = {}
    stats: Dict[str, dict] = {}

    for entry in scan_library(audio_dir):
        key = entry["artist"].strip().lower()
        if not key:
            continue
        canonical.setdefault(key, entry["artist"].strip())
        s = stats.setdefault(key, {"count": 0, "total_duration_s": 0.0, "total_size_bytes": 0, "most_recent_mtime": None})
        s["count"] += 1
        if entry["duration_s"]:
            s["total_duration_s"] += entry["duration_s"]
        if entry["size_bytes"]:
            s["total_size_bytes"] += entry["size_bytes"]
        if entry["mtime"] is not None and (s["most_recent_mtime"] is None or entry["mtime"] > s["most_recent_mtime"]):
            s["most_recent_mtime"] = entry["mtime"]

    results: List[ArtistAnalytics] = []
    for key, s in stats.items():
        most_recent = (
            datetime.fromtimestamp(s["most_recent_mtime"]).isoformat()
            if s["most_recent_mtime"] is not None else None
        )
        results.append({
            "artist": canonical[key],
            "count": s["count"],
            "total_duration_s": s["total_duration_s"],
            "total_size_bytes": s["total_size_bytes"],
            "most_recent_added": most_recent,
        })
    return results


def list_artists(audio_dir: str) -> List[str]:
    """Distinct artists in the library, sorted case-insensitively."""
    seen = {}
    for entry in scan_library(audio_dir):
        key = entry["artist"].strip().lower()
        if key and key not in seen:
            seen[key] = entry["artist"].strip()
    return sorted(seen.values(), key=str.lower)


def count_by_artist(audio_dir: str) -> Dict[str, int]:
    """Number of MP3s per artist in the library, keyed by canonical (first-seen) casing."""
    canonical: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    for entry in scan_library(audio_dir):
        key = entry["artist"].strip().lower()
        if not key:
            continue
        canonical.setdefault(key, entry["artist"].strip())
        counts[key] = counts.get(key, 0) + 1
    return {canonical[key]: count for key, count in counts.items()}


def merge_dj_names(existing: List[str], new_names: List[str]) -> List[str]:
    """Case-insensitively dedupe `new_names` into `existing`, sorted case-insensitively.

    Keeps whichever casing was already on record for a name that's already present.
    """
    by_key = {name.strip().lower(): name.strip() for name in existing if name.strip()}
    for name in new_names:
        key = name.strip().lower()
        if key and key not in by_key:
            by_key[key] = name.strip()
    return sorted(by_key.values(), key=str.lower)


# Noise commonly appended to DJ set titles that shouldn't affect duplicate
# detection, e.g. "Fisher (AUS) - Boiler Room [4K]" vs "Fisher - Boiler Room".
_BRACKETED_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Lowercase, strip bracketed noise and punctuation, collapse whitespace."""
    s = title.lower()
    s = _BRACKETED_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


def is_duplicate(
    candidate_title: str,
    candidate_video_id: Optional[str],
    library_entries: List[LibraryEntry],
    threshold: float = 0.72,
) -> bool:
    """True if `candidate` looks like something already in `library_entries`.

    Checks exact YouTube video ID first (only possible for library entries
    downloaded after video-ID tagging was added), then falls back to fuzzy
    title matching for everything else.
    """
    if candidate_video_id:
        for entry in library_entries:
            if entry["youtube_id"] and entry["youtube_id"] == candidate_video_id:
                return True

    normalized_candidate = normalize_title(candidate_title)
    for entry in library_entries:
        ratio = SequenceMatcher(None, normalized_candidate, normalize_title(entry["title"])).ratio()
        if ratio >= threshold:
            return True

    return False


def _first_text(tags: ID3, frame_id: str) -> Optional[str]:
    frame = tags.get(frame_id)
    if not frame or not getattr(frame, "text", None):
        return None
    return str(frame.text[0]).strip() or None
