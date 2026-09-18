"""
tagging.py — MP3 tagging via mutagen.

Writes ID3v2.3 tags directly to the MP3 in-place without re-encoding,
preserving any embedded artwork already added by yt-dlp.

Tags written:
  - TIT2  (title)
  - TPE1  (artist)
  - TPE2  (album artist — Plex groups a music library by album artist, not
           track artist, so this must be set or Plex will file the track
           under "Various Artists"/the channel name instead of the artist
           the user provided)
  - TALB  (album)
  - TRCK  (track number — only set for Album-mode playlist tracks, so
           players/Plex keep them in playlist order)
  - TXXX:RELEASETYPE = album;live  (Plex live album detection — only for
                       mix downloads; omitted for single songs and album
                       tracks, which aren't live albums)
  - TXXX:YOUTUBE_ID  (source video ID — lets the missed-set finder dedupe
                       exactly instead of relying on fuzzy title matching)
"""

from pathlib import Path
from typing import Optional
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TXXX, ID3NoHeaderError


def tag_mp3(
    src: str,
    title: str,
    artist: str,
    album: str,
    release_type: Optional[str] = "album;live",
    youtube_id: Optional[str] = None,
    track_number: Optional[int] = None,
) -> str:
    """
    Tag the MP3 at `src` in-place using mutagen.
    Returns the final path (same as src on success).
    Raises RuntimeError on failure.
    """
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(f"MP3 not found: {src}")

    title  = _sanitize(title)
    artist = _sanitize(artist)
    album  = _sanitize(album)

    try:
        try:
            tags = ID3(str(src_path))
        except ID3NoHeaderError:
            tags = ID3()

        tags["TIT2"] = TIT2(encoding=3, text=title)
        tags["TPE1"] = TPE1(encoding=3, text=artist)
        tags["TPE2"] = TPE2(encoding=3, text=artist)
        tags["TALB"] = TALB(encoding=3, text=album)
        if release_type:
            tags["TXXX:RELEASETYPE"] = TXXX(encoding=3, desc="RELEASETYPE", text=release_type)
        elif "TXXX:RELEASETYPE" in tags:
            # Re-tagging a file that previously got the live-album marker
            # (e.g. re-run in song mode) — remove the stale tag instead of
            # leaving it in place.
            del tags["TXXX:RELEASETYPE"]
        if youtube_id:
            tags["TXXX:YOUTUBE_ID"] = TXXX(encoding=3, desc="YOUTUBE_ID", text=youtube_id)
        if track_number is not None:
            tags["TRCK"] = TRCK(encoding=3, text=str(track_number))

        tags.save(str(src_path), v2_version=3)

    except Exception as exc:
        raise RuntimeError(f"mutagen tagging failed: {exc}") from exc

    return str(src_path)


def _sanitize(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()
