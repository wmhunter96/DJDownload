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
        "artist": {
            "mode": "channel",   # "channel" | "custom"
            "custom_name": "",
        },
    }
