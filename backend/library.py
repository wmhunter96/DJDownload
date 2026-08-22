"""
library.py — scans the audio output directory to answer "what do we already have".

Used by the discovery/missed-set-finder feature (backend/discovery.py) to:
  - list the DJs already in the library (for the DJ picker)
  - dedupe YouTube search candidates against sets already downloaded
"""

import glob
import os
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, TypedDict

from mutagen.id3 import ID3, ID3NoHeaderError


class LibraryEntry(TypedDict):
    artist: str
    title: str
    youtube_id: Optional[str]


def scan_library(audio_dir: str) -> List[LibraryEntry]:
    """Read artist/title/video-id tags off every MP3 in `audio_dir`.

    Files that can't be read (missing tags, corrupt ID3, etc.) are skipped
    rather than failing the whole scan.
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
        entries.append({"artist": artist, "title": title, "youtube_id": youtube_id or None})

    return entries


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
