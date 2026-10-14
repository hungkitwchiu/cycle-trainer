"""Paths and persistent settings.

User content (workouts, recorded activities) lives in ``<app>/data`` so it sits next
to the app (e.g. in Google Drive). Credentials/tokens live in a per-machine folder
(%LOCALAPPDATA%\\CycleTrainer on Windows) so they are never synced anywhere.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
SAMPLE_WORKOUTS_DIR = APP_DIR / "sample_workouts"

DATA_DIR = Path(os.environ.get("CYCLETRAINER_DATA", APP_DIR / "data"))
WORKOUTS_DIR = DATA_DIR / "workouts"
ACTIVITIES_DIR = DATA_DIR / "activities"

if os.name == "nt":
    _local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    PRIVATE_DIR = _local / "CycleTrainer"
else:
    PRIVATE_DIR = Path.home() / ".cycletrainer"
PRIVATE_DIR = Path(os.environ.get("CYCLETRAINER_PRIVATE", PRIVATE_DIR))

SETTINGS_FILE = DATA_DIR / "settings.json"
SECRETS_FILE = PRIVATE_DIR / "secrets.json"
GARMIN_TOKEN_DIR = PRIVATE_DIR / "garmin_tokens"

PORT = int(os.environ.get("CYCLETRAINER_PORT", "8765"))

DEFAULT_SETTINGS: dict[str, Any] = {
    "ftp": 200,
    "weight_kg": 75.0,
    "auto_upload_garmin": False,
    "auto_pause": True,
    # Remembered sensors: role -> {"address": ..., "name": ...}
    "devices": {},
}

DEFAULT_SECRETS: dict[str, Any] = {
    "garmin": {"email": ""},
}

_lock = threading.Lock()


def ensure_dirs() -> None:
    for d in (DATA_DIR, WORKOUTS_DIR, ACTIVITIES_DIR, PRIVATE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load(path: Path, defaults: dict) -> dict:
    data = json.loads(json.dumps(defaults))  # deep copy
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            for k, v in stored.items():
                if isinstance(v, dict) and isinstance(data.get(k), dict):
                    data[k].update(v)
                else:
                    data[k] = v
        except (OSError, ValueError):
            pass
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_settings() -> dict:
    with _lock:
        return _load(SETTINGS_FILE, DEFAULT_SETTINGS)


def save_settings(data: dict) -> None:
    with _lock:
        _save(SETTINGS_FILE, data)


def update_settings(**changes: Any) -> dict:
    s = load_settings()
    s.update(changes)
    save_settings(s)
    return s


def load_secrets() -> dict:
    with _lock:
        return _load(SECRETS_FILE, DEFAULT_SECRETS)


def save_secrets(data: dict) -> None:
    with _lock:
        _save(SECRETS_FILE, data)
