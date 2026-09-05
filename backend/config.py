import os
import yaml
from pathlib import Path

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/settings.yaml")


def load_settings() -> dict:
    path = Path(CONFIG_PATH)
    if not path.exists():
        return _defaults()
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    defaults = _defaults()
    # Deep merge defaults <- file values
    for section, values in defaults.items():
        if section not in data:
            data[section] = values
        elif isinstance(values, dict):
            for k, v in values.items():
                data[section].setdefault(k, v)
    return data


def save_settings(settings: dict) -> None:
    path = Path(CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(settings, f, default_flow_style=False)


def _defaults() -> dict:
    return {
        "audio": {
            "enabled": True,
            "output_dir": "/downloads/audio",
        },
        "video": {
            "enabled": True,
            "output_dir": "/downloads/video",
        },
        "songs": {
            # Base directory for "Song" mode downloads (see the Mix/Song
            # toggle on the Download page). A folder named after the
            # resolved artist/channel is created under here for each song,
            # e.g. output_dir/bigbooty/track.mp3 — kept separate from the
            # audio.output_dir "Sets" library.
            "output_dir": "/downloads/songs",
        },
        "discovery": {
            "youtube_api_key": "",
            "dismissed_ids": [],   # video IDs the user has dismissed from the review queue
            "known_djs": [],       # DJ names for the Discover dropdown — recorded as jobs
                                    # complete, or backfilled via "Scan Audio Library"
            "dj_counts": {},       # known_djs name -> set count, shown as "Name (Qty)"
        },
        "plex": {
            "server_url": "",   # e.g. http://192.168.1.50:32400 — Plex server scanning the same audio output dir
            "token": "",        # X-Plex-Token, used to look up library items for "Play in Plex"
            "section_name": "",  # optional: pins which music library to use when there's more than one
        },
    }
