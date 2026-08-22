"""plex.py — resolves DJDownload's local audio files to Plex Media Server
library items, so the Analytics page can deep-link straight into Plex's own
player instead of just showing a static thumbnail.

Matching is by filename: Plex exposes each track's underlying file path
(Media/Part/file), and since Plex is expected to be scanning the same audio
output directory DJDownload writes to, matching on basename is exact — no
path translation between containers needed.

Uses stdlib urllib rather than adding an HTTP client dependency, since this
is a handful of simple authenticated GETs against the Plex API.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

_CACHE_TTL_S = 300  # rebuild the filename->ratingKey index at most every 5 minutes


class PlexError(Exception):
    pass


_cache = {
    "server_url": None,
    "built_at": 0.0,
    "machine_identifier": None,
    "file_index": {},  # basename -> ratingKey
}


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"X-Plex-Token": token, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise PlexError(f"Plex request to {url} failed: {exc}") from exc


def _machine_identifier(server_url: str, token: str) -> str:
    data = _get_json(f"{server_url}/identity", token)
    machine_id = data.get("MediaContainer", {}).get("machineIdentifier")
    if not machine_id:
        raise PlexError("Plex server didn't return a machine identifier")
    return machine_id


def _first_music_section(server_url: str, token: str) -> str:
    """First library section of type "artist" (Plex's internal type for music
    libraries). If you have more than one music library, whichever comes
    back first from Plex wins."""
    data = _get_json(f"{server_url}/library/sections", token)
    for section in data.get("MediaContainer", {}).get("Directory", []):
        if section.get("type") == "artist":
            return section["key"]
    raise PlexError("No music library section found on this Plex server")


def _build_file_index(server_url: str, token: str, section_key: str) -> Dict[str, str]:
    data = _get_json(f"{server_url}/library/sections/{section_key}/all?type=10", token)
    index: Dict[str, str] = {}
    for track in data.get("MediaContainer", {}).get("Metadata", []):
        rating_key = track.get("ratingKey")
        if not rating_key:
            continue
        for media in track.get("Media", []):
            for part in media.get("Part", []):
                path = part.get("file")
                if path:
                    index[os.path.basename(path)] = rating_key
    return index


def _refresh_cache(server_url: str, token: str) -> None:
    section_key = _first_music_section(server_url, token)
    _cache["server_url"] = server_url
    _cache["built_at"] = time.time()
    _cache["machine_identifier"] = _machine_identifier(server_url, token)
    _cache["file_index"] = _build_file_index(server_url, token, section_key)


def get_play_link(server_url: str, token: str, filename: str) -> Optional[str]:
    """Deep link into Plex Web for `filename`'s matching library track, or
    None if Plex isn't configured, unreachable, or hasn't scanned this file
    in yet."""
    server_url = (server_url or "").rstrip("/")
    if not server_url or not token:
        return None

    stale = (
        _cache["server_url"] != server_url
        or time.time() - _cache["built_at"] > _CACHE_TTL_S
    )
    if stale:
        try:
            _refresh_cache(server_url, token)
        except PlexError:
            return None

    rating_key = _cache["file_index"].get(filename)
    if not rating_key:
        return None

    key = urllib.parse.quote(f"/library/metadata/{rating_key}", safe="")
    return f"{server_url}/web/index.html#!/server/{_cache['machine_identifier']}/details?key={key}"
