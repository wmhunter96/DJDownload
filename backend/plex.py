"""plex.py — resolves DJDownload's local audio files to Plex Media Server
library items, so the Analytics page can deep-link straight into Plex's own
player instead of just showing a static thumbnail.

Matching is by filename: Plex exposes each track's underlying file path
(Media/Part/file), and since Plex is expected to be scanning the same audio
output directory DJDownload writes to, matching on basename is exact — no
path translation between containers needed.

Uses stdlib urllib rather than adding an HTTP client dependency, since this
is a handful of simple authenticated GETs against the Plex API.

Every failure is logged to stdout (`[plex] ...`, visible in `docker logs`)
rather than swallowed silently — a misconfigured server URL/token, or a
container that can't reach Plex over the network, otherwise looks identical
to "this specific file isn't in Plex yet" from the UI's point of view.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

_CACHE_TTL_S = 60  # rebuild the filename->ratingKey index at most once a minute
_PAGE_SIZE = 100_000  # large enough to always get the whole library in one request


class PlexError(Exception):
    pass


_cache = {
    "server_url": None,
    "built_at": 0.0,
    "machine_identifier": None,
    "file_index": {},  # basename -> ratingKey
}


def _log(msg: str) -> None:
    print(f"[plex] {msg}", flush=True)


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"X-Plex-Token": token, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise PlexError(f"request to {url} failed: {exc}") from exc


def _machine_identifier(server_url: str, token: str) -> str:
    data = _get_json(f"{server_url}/identity", token)
    machine_id = data.get("MediaContainer", {}).get("machineIdentifier")
    if not machine_id:
        raise PlexError("server didn't return a machine identifier")
    return machine_id


def _first_music_section(server_url: str, token: str) -> str:
    """First library section of type "artist" (Plex's internal type for music
    libraries). If you have more than one music library, whichever comes
    back first from Plex wins."""
    data = _get_json(f"{server_url}/library/sections", token)
    sections = data.get("MediaContainer", {}).get("Directory", [])
    for section in sections:
        if section.get("type") == "artist":
            return section["key"]
    seen_types = [s.get("type") for s in sections]
    raise PlexError(f"no music (artist-type) library section found — sections seen: {seen_types}")


def _build_file_index(server_url: str, token: str, section_key: str) -> Dict[str, str]:
    url = (
        f"{server_url}/library/sections/{section_key}/all"
        f"?type=10&X-Plex-Container-Start=0&X-Plex-Container-Size={_PAGE_SIZE}"
    )
    data = _get_json(url, token)
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
    machine_identifier = _machine_identifier(server_url, token)
    file_index = _build_file_index(server_url, token, section_key)
    _cache["server_url"] = server_url
    _cache["built_at"] = time.time()
    _cache["machine_identifier"] = machine_identifier
    _cache["file_index"] = file_index
    _log(f"indexed {len(file_index)} track(s) from section {section_key}")


def invalidate_cache() -> None:
    """Force the next lookup to rebuild the index instead of trusting the
    cached one, even if it's within its TTL. Used after triggering a Plex
    scan so a freshly-scanned file can show up sooner than the normal TTL."""
    _cache["built_at"] = 0.0


def trigger_scan(server_url: str, token: str) -> None:
    """Kick off an on-demand Plex scan of the music section, so newly
    downloaded files don't have to wait on Plex's own scan interval.
    Fire-and-forget: errors are logged, not raised — this is a best-effort
    nudge, not something the caller should block a page load on."""
    server_url = (server_url or "").rstrip("/")
    if not server_url or not token:
        return
    try:
        section_key = _first_music_section(server_url, token)
        req = urllib.request.Request(
            f"{server_url}/library/sections/{section_key}/refresh",
            headers={"X-Plex-Token": token},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        _log(f"triggered library scan (section {section_key})")
    except Exception as exc:
        _log(f"failed to trigger library scan: {exc}")


def get_play_link(server_url: str, token: str, filename: str) -> Optional[str]:
    """Deep link into Plex Web for `filename`'s matching library track, or
    None if Plex isn't configured, unreachable, or hasn't scanned this file
    in yet. Logs the specific reason to stdout either way."""
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
        except PlexError as exc:
            _log(f"lookup for {filename!r} failed — couldn't refresh index: {exc}")
            return None

    rating_key = _cache["file_index"].get(filename)
    if not rating_key:
        _log(f"{filename!r} not found in index ({len(_cache['file_index'])} track(s) indexed)")
        return None

    key = urllib.parse.quote(f"/library/metadata/{rating_key}", safe="")
    return f"{server_url}/web/index.html#!/server/{_cache['machine_identifier']}/details?key={key}"
