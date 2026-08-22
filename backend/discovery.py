"""
discovery.py — YouTube Data API v3 client for the missed-set finder.

Two-step search, per the API:
  1. search.list   — find candidate videos matching a query (no duration)
  2. videos.list    — fetch duration (and snippet) for those candidate IDs

Uses stdlib urllib instead of adding an HTTP client dependency — this is a
low-volume, low-frequency call (a handful of requests per user-triggered
search), so there's no need for anything heavier.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, TypedDict

API_BASE = "https://www.googleapis.com/youtube/v3"


class SearchResult(TypedDict):
    video_id: str
    title: str
    channel: str
    duration_s: int
    thumbnail: str
    url: str


class YouTubeApiError(RuntimeError):
    """Raised on any non-2xx response from the YouTube Data API."""


def search_youtube(api_key: str, query: str, max_results: int = 25) -> List[SearchResult]:
    """Search YouTube for `query`, returning videos with resolved durations."""
    video_ids = _search_video_ids(api_key, query, max_results)
    if not video_ids:
        return []
    return _fetch_video_details(api_key, video_ids)


def _search_video_ids(api_key: str, query: str, max_results: int) -> List[str]:
    params = {
        "key": api_key,
        "q": query,
        "part": "id",
        "type": "video",
        "order": "relevance",
        "maxResults": str(max_results),
    }
    data = _get(f"{API_BASE}/search", params)
    return [item["id"]["videoId"] for item in data.get("items", []) if item.get("id", {}).get("videoId")]


def _fetch_video_details(api_key: str, video_ids: List[str]) -> List[SearchResult]:
    params = {
        "key": api_key,
        "id": ",".join(video_ids),
        "part": "snippet,contentDetails",
    }
    data = _get(f"{API_BASE}/videos", params)

    results: List[SearchResult] = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        content_details = item.get("contentDetails", {})
        duration = content_details.get("duration")
        if not duration:
            continue
        video_id = item["id"]
        thumbnails = snippet.get("thumbnails", {})
        thumbnail = (
            thumbnails.get("medium", {}).get("url")
            or thumbnails.get("default", {}).get("url")
            or ""
        )
        results.append({
            "video_id": video_id,
            "title": snippet.get("title", "Untitled"),
            "channel": snippet.get("channelTitle", "Unknown"),
            "duration_s": parse_iso8601_duration(duration),
            "thumbnail": thumbnail,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return results


_DURATION_RE = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def parse_iso8601_duration(duration: str) -> int:
    """Parse an ISO 8601 duration like 'PT1H5M30S' into total seconds."""
    match = _DURATION_RE.match(duration)
    if not match:
        return 0
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def _get(url: str, params: dict) -> dict:
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = _extract_error_message(body) or f"HTTP {exc.code}"
        raise YouTubeApiError(f"YouTube API error: {message}") from exc
    except urllib.error.URLError as exc:
        raise YouTubeApiError(f"YouTube API unreachable: {exc.reason}") from exc


def _extract_error_message(body: str) -> Optional[str]:
    try:
        data = json.loads(body)
        return data.get("error", {}).get("message")
    except (json.JSONDecodeError, AttributeError):
        return None
