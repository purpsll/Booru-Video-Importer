"""Persistent local frame-hash cache for source-first video matching."""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict

CACHE_VERSION = 3
SCAN_STATE_VERSION = 3
_PLUGIN_DIR = os.path.dirname(__file__)
_PLUGINS_DIR = os.path.dirname(_PLUGIN_DIR)
_STASH_CONFIG_DIR = (
    os.path.dirname(_PLUGINS_DIR)
    if os.path.basename(_PLUGINS_DIR).casefold() == "plugins"
    else _PLUGIN_DIR
)
DEFAULT_DATA_DIR = os.path.join(_STASH_CONFIG_DIR, "booru-video-importer")
DEFAULT_CACHE_PATH = os.path.join(DEFAULT_DATA_DIR, "booru_video_hash_index.json")
DEFAULT_SCAN_STATE_PATH = os.path.join(DEFAULT_DATA_DIR, "booru_video_scan_state.json")


def load_cache(path: str = DEFAULT_CACHE_PATH) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return {"version": CACHE_VERSION, "scenes": {}}
        scenes = data.get("scenes")
        if not isinstance(scenes, dict):
            data["scenes"] = {}
        return data
    except (OSError, json.JSONDecodeError):
        return {"version": CACHE_VERSION, "scenes": {}}


def save_cache(data: Dict[str, Any], path: str = DEFAULT_CACHE_PATH) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = dict(data)
    payload["version"] = CACHE_VERSION
    payload.setdefault("scenes", {})

    fd, temp_path = tempfile.mkstemp(prefix=".booru-video-index-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass


def load_scan_state(path: str = DEFAULT_SCAN_STATE_PATH) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if (
            not isinstance(data, dict)
            or data.get("version") != SCAN_STATE_VERSION
        ):
            return {"version": SCAN_STATE_VERSION, "scopes": {}}
        scopes = data.get("scopes")
        if not isinstance(scopes, dict):
            data["scopes"] = {}
        data["version"] = SCAN_STATE_VERSION
        return data
    except (OSError, json.JSONDecodeError):
        return {"version": SCAN_STATE_VERSION, "scopes": {}}


def save_scan_state(data: Dict[str, Any], path: str = DEFAULT_SCAN_STATE_PATH) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = dict(data)
    payload["version"] = SCAN_STATE_VERSION
    payload.setdefault("scopes", {})

    fd, temp_path = tempfile.mkstemp(prefix=".booru-video-scan-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass


def scene_signature(scene: Dict[str, Any]) -> str:
    files = scene.get("files") or []
    if not files:
        return ""
    file_obj = files[0]
    return "|".join(
        [
            str(file_obj.get("id") or ""),
            str(file_obj.get("size") or ""),
            str(file_obj.get("mod_time") or ""),
            f"{float(file_obj.get('duration') or 0):.3f}",
            str(file_obj.get("path") or ""),
        ]
    )
