"""plex.py — resolves DJDownload's local audio files to Plex Media Server
library items, so the Analytics page can deep-link straight into Plex's own
player instead of just showing a static thumbnail.

Matching is by filename: Plex exposes each track's underlying file path
(Media/Part/file), and since Plex is expected to be scanning the same audio
output directory DJDownload writes to, matching on basename is exact — no
path translation between containers needed.

A Plex token authenticates as an account against the whole server, not a
single library — it isn't "from" the music library specifically. What
narrows things to the right library is the section lookup below, which by
default just takes the *first* music (type "artist") section Plex reports.
If you have more than one music library, that's a real ambiguity: `section_name`
(Settings → Plex → Library Section) pins it to a specific one by title.

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
from typing import Dict, List, Optional

_CACHE_TTL_S = 60  # rebuild the filename->ratingKey index at most once a minute
_PAGE_SIZE = 100_000  # large enough to always get the whole library in one request


class PlexError(Exception):
    pass


_cache = {
    "server_url": None,
    "section_name": None,
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


def _list_music_sections(server_url: str, token: str) -> List[dict]:
    """Every library section of type "artist" (Plex's internal type for
    music libraries), as [{"key": ..., "title": ...}, ...]."""
    data = _get_json(f"{server_url}/library/sections", token)
    sections = data.get("MediaContainer", {}).get("Directory", [])
    return [{"key": s["key"], "title": s.get("title", "")} for s in sections if s.get("type") == "artist"]


def _resolve_music_section(server_url: str, token: str, section_name: str = "") -> dict:
    """The music section to use: the one named `section_name` if given
    (Settings → Plex → Library Section), otherwise whichever music library
    Plex lists first — which is a guess if you have more than one."""
    sections = _list_music_sections(server_url, token)
    if not sections:
        raise PlexError("no music (artist-type) library section found on this Plex server")
    if section_name:
        match = next((s for s in sections if s["title"].strip().lower() == section_name.strip().lower()), None)
        if not match:
            available = ", ".join(f'"{s["title"]}"' for s in sections)
            raise PlexError(f'no music library named "{section_name}" — available: {available}')
        return match
    return sections[0]


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


def _refresh_cache(server_url: str, token: str, section_name: str) -> None:
    section = _resolve_music_section(server_url, token, section_name)
    machine_identifier = _machine_identifier(server_url, token)
    file_index = _build_file_index(server_url, token, section["key"])
    _cache["server_url"] = server_url
    _cache["section_name"] = section_name
    _cache["built_at"] = time.time()
    _cache["machine_identifier"] = machine_identifier
    _cache["file_index"] = file_index
    _log(f'indexed {len(file_index)} track(s) from "{section["title"]}" (section {section["key"]})')


def invalidate_cache() -> None:
    """Force the next lookup to rebuild the index instead of trusting the
    cached one, even if it's within its TTL. Used after triggering a Plex
    scan so a freshly-scanned file can show up sooner than the normal TTL."""
    _cache["built_at"] = 0.0


def trigger_scan(server_url: str, token: str, section_name: str = "") -> bool:
    """Kick off an on-demand Plex scan of the music section, so newly
    downloaded files don't have to wait on Plex's own scan interval.
    Errors are logged, not raised, but the return value tells the caller
    whether Plex was actually reached — surfaced in the UI's sync indicator
    so a connectivity/auth problem doesn't just look identical to "still
    scanning"."""
    server_url = (server_url or "").rstrip("/")
    if not server_url or not token:
        return False
    try:
        section = _resolve_music_section(server_url, token, section_name)
        req = urllib.request.Request(
            f"{server_url}/library/sections/{section['key']}/refresh",
            headers={"X-Plex-Token": token},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        _log(f"triggered library scan (section {section['key']})")
        return True
    except Exception as exc:
        _log(f"failed to trigger library scan: {exc}")
        return False


def test_connection(
    server_url: str, token: str, section_name: str = "", local_filenames: Optional[List[str]] = None
) -> dict:
    """Runs the same steps `get_play_link` relies on, one at a time, and
    reports exactly which stage failed (or succeeded) — backs the Settings
    page's "Test Connection" button. Independent of the shared cache, so
    testing never leaves stale state behind for real lookups.

    If more than one music library exists and `section_name` isn't pinned,
    every one of them gets indexed and cross-checked against
    `local_filenames` individually — the whole point being to show which
    library (if any) actually holds DJDownload's files, since defaulting to
    "whichever Plex lists first" is a guess that's often wrong.
    """
    steps = []
    server_url = (server_url or "").rstrip("/")
    local_filenames = local_filenames or []

    if not server_url or not token:
        return {
            "ok": False,
            "steps": [{"label": "Configuration", "ok": False, "detail": "Enter both a server URL and a token."}],
        }

    try:
        data = _get_json(f"{server_url}/identity", token)
        container = data.get("MediaContainer", {})
        machine_id = container.get("machineIdentifier")
        version = container.get("version", "unknown version")
        if not machine_id:
            steps.append({"label": "Connect to Plex", "ok": False, "detail": "Connected, but got no machineIdentifier back — double-check the token."})
            return {"ok": False, "steps": steps}
        steps.append({"label": "Connect to Plex", "ok": True, "detail": f"Reached Plex Media Server {version}."})
    except PlexError as exc:
        steps.append({"label": "Connect to Plex", "ok": False, "detail": str(exc)})
        return {"ok": False, "steps": steps}

    try:
        sections = _list_music_sections(server_url, token)
    except PlexError as exc:
        steps.append({"label": "Find music library", "ok": False, "detail": str(exc)})
        return {"ok": False, "steps": steps}

    if not sections:
        steps.append({"label": "Find music library", "ok": False, "detail": "No music (artist-type) library found on this server."})
        return {"ok": False, "steps": steps}

    if section_name:
        sections = [s for s in sections if s["title"].strip().lower() == section_name.strip().lower()] or sections
        steps.append({"label": "Find music library", "ok": bool(sections) and sections[0]["title"].strip().lower() == section_name.strip().lower(),
                       "detail": f'Pinned to "{section_name}" in Settings.'})
    elif len(sections) > 1:
        titles = ", ".join(f'"{s["title"]}"' for s in sections)
        steps.append({"label": "Find music library", "ok": True,
                       "detail": f"Found {len(sections)} music libraries ({titles}) and no Library Section is pinned in Settings — "
                                 f"checking each below so you can see which one actually has these files."})
    else:
        steps.append({"label": "Find music library", "ok": True, "detail": f'Using "{sections[0]["title"]}" (only music library found).'})

    for section in sections:
        try:
            file_index = _build_file_index(server_url, token, section["key"])
        except PlexError as exc:
            steps.append({"label": f'Index "{section["title"]}"', "ok": False, "detail": str(exc)})
            continue

        label = f'Index "{section["title"]}"'
        if not local_filenames:
            steps.append({"label": label, "ok": True, "detail": f"Indexed {len(file_index)} track(s). No local files to check against yet."})
            continue

        matched = sum(1 for f in local_filenames if f in file_index)
        total = len(local_filenames)
        if matched == total:
            steps.append({"label": label, "ok": True, "detail": f"Indexed {len(file_index)} track(s) — all {total} local file(s) matched."})
        elif matched == 0:
            sample_local = local_filenames[0]
            sample_plex = next(iter(file_index), None)
            detail = f'Indexed {len(file_index)} track(s), but 0 of {total} local file(s) matched. e.g. local file "{sample_local}"'
            detail += f' vs. a Plex file like "{sample_plex}"' if sample_plex else " — this library's index is empty"
            detail += ". This library isn't scanning DJDownload's audio output folder."
            steps.append({"label": label, "ok": False, "detail": detail})
        else:
            steps.append({"label": label, "ok": True, "detail": f"Indexed {len(file_index)} track(s) — {matched} of {total} local file(s) matched (rest may still need a Plex scan)."})

    return {"ok": all(s["ok"] for s in steps), "steps": steps}


def get_play_link(server_url: str, token: str, filename: str, section_name: str = "") -> Optional[str]:
    """Deep link into Plex Web for `filename`'s matching library track, or
    None if Plex isn't configured, unreachable, or hasn't scanned this file
    in yet. Logs the specific reason to stdout either way."""
    server_url = (server_url or "").rstrip("/")
    if not server_url or not token:
        return None

    stale = (
        _cache["server_url"] != server_url
        or _cache["section_name"] != section_name
        or time.time() - _cache["built_at"] > _CACHE_TTL_S
    )
    if stale:
        try:
            _refresh_cache(server_url, token, section_name)
        except PlexError as exc:
            _log(f"lookup for {filename!r} failed — couldn't refresh index: {exc}")
            return None

    rating_key = _cache["file_index"].get(filename)
    if not rating_key:
        _log(f"{filename!r} not found in index ({len(_cache['file_index'])} track(s) indexed)")
        return None

    key = urllib.parse.quote(f"/library/metadata/{rating_key}", safe="")
    return f"{server_url}/web/index.html#!/server/{_cache['machine_identifier']}/details?key={key}"
