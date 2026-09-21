#!/usr/bin/env python3
"""Booru Video Importer: exhaustive Stash-first e621 video matcher.

Primary workflow:
1. Select one eligible local Stash video (optionally scoped by any Stash tag/alias).
2. Read its exact normalized duration in milliseconds.
3. Walk the complete e621 WebM history, then MP4 history, newest to oldest.
4. Reject every e621 post whose API-reported duration is not exactly equal before
   opening any remote media.
5. For exact-duration candidates, compare frames at identical relative timecodes.
6. If a candidate fails, continue to the next exact-duration candidate.
7. On the first strict verified match, merge authoritative e621 metadata.
8. If both e621 video histories are exhausted, move to the next local Stash video.

Progress and local comparison hashes are persisted so long scans can resume safely.
"""
from __future__ import annotations

import email.utils
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from stash_client import Stash, primary_video
from source_index import (
    load_cache,
    load_scan_state,
    save_cache,
    save_scan_state,
    scene_signature,
)
from video_match import (
    DEFAULT_RATIOS,
    early_frame_hashes,
    early_hash_candidate,
    frame_hashes,
    probe_duration,
    verify_video_candidate,
)

VERSION = "2.1.0"
USER_AGENT = f"stash-booru-video-importer/{VERSION}"
E621_BASE = "https://e621.net"
E621_PAGE_SIZE = 75
VIDEO_EXTENSIONS = ("webm", "mp4")
EARLY_FRAME_DISTANCE = 4
VERIFY_FRAME_DISTANCE = 16
STATUS_IMPORTED = "Booru Video Imported"

_LAST_E621_REQUEST = 0.0


def _prefix(level: str) -> str:
    char = {"DEBUG": "d", "INFO": "i", "WARNING": "w", "ERROR": "e"}.get(
        level.upper(), "i"
    )
    return "\x01" + char + "\x02"


def log(level: str, message: str) -> None:
    print(f"{_prefix(level)}{message}", file=sys.stderr, flush=True)


def progress(value: float) -> None:
    value = max(0.0, min(1.0, float(value)))
    print(f"\x01p\x02{value:.6f}", file=sys.stderr, flush=True)


def read_input() -> Dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _wait(last: float, interval: float) -> float:
    now = time.monotonic()
    remaining = last + max(0.0, float(interval)) - now
    if remaining > 0:
        time.sleep(remaining)
    return time.monotonic()


def _json_request(url: str, headers: Optional[Dict[str, str]] = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"e621 returned non-JSON data: {' '.join(raw.split())[:240]}"
        ) from exc


def e621_headers(username: str, api_key: str) -> Dict[str, str]:
    headers = {
        "User-Agent": (
            f"BooruVideoImporter/{VERSION}"
            + (f" (by {username} on e621)" if username else "")
        ),
        "Accept": "application/json",
    }
    if username and api_key:
        import base64
        token = base64.b64encode(
            f"{username}:{api_key}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def e621_request(url: str, username: str, api_key: str) -> Any:
    global _LAST_E621_REQUEST
    _LAST_E621_REQUEST = _wait(_LAST_E621_REQUEST, 1.0)
    return _json_request(url, e621_headers(username, api_key))


def _e621_posts(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("posts")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        post = payload.get("post")
        if isinstance(post, dict):
            return [post]
        if payload.get("id"):
            return [payload]
    return []


def e621_file_info(post: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize both legacy and current e621 file schemas."""
    legacy = post.get("file")
    if isinstance(legacy, dict):
        return {
            "ext": str(legacy.get("ext") or "").casefold().lstrip("."),
            "url": str(legacy.get("url") or "").strip(),
            "md5": str(legacy.get("md5") or "").strip().casefold(),
            "duration": as_float(
                legacy.get("duration") or post.get("duration"), 0.0
            ),
        }

    files = post.get("files")
    if isinstance(files, dict):
        meta = files.get("meta") or {}
        original = files.get("original") or {}
        return {
            "ext": str(meta.get("ext") or "").casefold().lstrip("."),
            "url": str(
                original.get("url") if isinstance(original, dict) else ""
            ).strip(),
            "md5": str(meta.get("md5") or "").strip().casefold(),
            "duration": as_float(meta.get("duration"), 0.0),
        }

    return {"ext": "", "url": "", "md5": "", "duration": 0.0}


def e621_post_by_md5(
    md5: str,
    username: str,
    api_key: str,
) -> Optional[Dict[str, Any]]:
    """Return an exact e621 video post for a byte-identical local file."""
    md5 = str(md5 or "").strip().casefold()
    if not md5:
        return None

    params = urllib.parse.urlencode(
        {
            "tags": f"md5:{md5}",
            "limit": "1",
            "v2": "true",
            "mode": "extended",
        }
    )
    payload = e621_request(
        f"{E621_BASE}/posts.json?{params}",
        username,
        api_key,
    )
    for post in _e621_posts(payload):
        info = e621_file_info(post)
        if (
            info.get("md5") == md5
            and info.get("ext") in VIDEO_EXTENSIONS
            and info.get("url")
        ):
            return post
    return None


def video_file_fingerprint(
    video: Dict[str, Any],
    kind: str,
) -> Optional[str]:
    """Read a fingerprint from the exact Stash file currently being processed."""
    target = str(kind or "").casefold()
    for fingerprint in video.get("fingerprints") or []:
        if str(fingerprint.get("type") or "").casefold() == target:
            value = str(fingerprint.get("value") or "").strip()
            if value:
                return value
    return None


def e621_video_posts_page(
    ext: str,
    username: str,
    api_key: str,
    before_id: Optional[int] = None,
    limit: int = E621_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """Fetch one page for one video format so no interleaved history is skipped."""
    ext = str(ext).casefold().lstrip(".")
    if ext not in VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported e621 video extension: {ext}")

    params: Dict[str, str] = {
        "tags": f"type:{ext}",
        "limit": str(max(1, min(320, int(limit)))),
        "v2": "true",
        "mode": "extended",
    }
    if before_id:
        params["page"] = f"b{int(before_id)}"

    payload = e621_request(
        f"{E621_BASE}/posts.json?{urllib.parse.urlencode(params)}",
        username,
        api_key,
    )
    rows = []
    for post in _e621_posts(payload):
        info = e621_file_info(post)
        if info.get("ext") == ext and info.get("url"):
            rows.append(post)
    rows.sort(key=lambda post: as_int(post.get("id"), 0), reverse=True)
    return rows


def duration_milliseconds(value: Any) -> int:
    seconds = as_float(value, 0.0)
    if seconds <= 0:
        return 0
    return int(round(seconds * 1000.0))


def _date_only(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value or "").strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).date().isoformat()
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return None


def e621_post_metadata(post: Dict[str, Any]) -> Dict[str, Any]:
    typed = post.get("tags") or {}
    artists: List[str] = []
    characters: List[str] = []
    tags: List[str] = []

    if isinstance(typed, dict):
        artists = [
            str(value).strip()
            for value in typed.get("artist") or []
            if str(value).strip()
        ]
        characters = [
            str(value).strip()
            for value in typed.get("character") or []
            if str(value).strip()
        ]
        for category in ("general", "species", "copyright", "lore"):
            tags.extend(
                str(value).strip()
                for value in typed.get(category) or []
                if str(value).strip()
            )

    # Stash has one Studio field, so preserve additional artists as tags.
    if len(artists) > 1:
        tags.extend(artists[1:])

    post_id = str(post.get("id") or "")
    urls = [f"https://e621.net/posts/{post_id}"]
    raw_sources = post.get("sources") or []
    if isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    for value in raw_sources:
        url = str(value or "").strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
        if len(urls) >= 8:
            break

    return {
        "tags": list(dict.fromkeys(tags)),
        "artists": list(dict.fromkeys(artists)),
        "characters": list(dict.fromkeys(characters)),
        "date": _date_only(post.get("created_at")),
        "urls": urls,
    }


def configured_stash_tag_scope(settings: Dict[str, Any]) -> str:
    return " ".join(str(settings.get("stash_tag_scope") or "").split())


def protected_organized_scene(
    scene: Dict[str, Any],
    settings: Dict[str, Any],
) -> bool:
    return (
        as_bool(settings.get("skip_organized_scenes"), False)
        and bool(scene.get("organized"))
    )


def resolve_stash_tag_filter(
    stash: Stash,
    tag_name: str,
) -> Tuple[Optional[str], Optional[str]]:
    tag_name = " ".join(str(tag_name or "").split())
    if not tag_name:
        return None, None
    tag = stash.all_tags().get(tag_name.casefold())
    if not tag:
        return None, None
    return str(tag.get("id") or ""), str(tag.get("name") or tag_name)


def _scope(
    stash: Stash,
    settings: Dict[str, Any],
) -> Tuple[Optional[str], str, str]:
    requested = configured_stash_tag_scope(settings)
    if not requested:
        return None, "__all__", "all eligible Stash videos"

    tag_id, canonical = resolve_stash_tag_filter(stash, requested)
    if not tag_id:
        raise RuntimeError(f"Stash tag scope not found: {requested}")
    return tag_id, f"tag:{tag_id}", canonical or requested


def eligible_local_scenes(
    stash: Stash,
    settings: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Return eligible local videos. Frame hashes are intentionally lazy."""
    filter_tag_id, _scope_key, scope_label = _scope(stash, settings)
    rows: List[Dict[str, Any]] = []
    stats = {
        "scenes_seen": 0,
        "eligible": 0,
        "organized_protected": 0,
        "already_matched": 0,
        "no_video": 0,
    }

    page = 1
    per_page = 100
    while True:
        count, scenes = stash.find_scenes(
            page,
            per_page,
            tag_id=filter_tag_id,
        )
        if not scenes:
            break

        for scene in scenes:
            stats["scenes_seen"] += 1
            if protected_organized_scene(scene, settings):
                stats["organized_protected"] += 1
                continue
            if any(
                "e621.net/posts/" in str(url)
                for url in scene.get("urls") or []
            ):
                stats["already_matched"] += 1
                continue
            video = primary_video(scene)
            if not video or not str(video.get("path") or "").strip():
                stats["no_video"] += 1
                continue
            rows.append(scene)

        if page * per_page >= count:
            break
        page += 1

    rows.sort(key=lambda scene: as_int(scene.get("id"), 0))
    stats["eligible"] = len(rows)
    log(
        "INFO",
        f"Local scope '{scope_label}': {len(rows)} eligible videos; "
        f"{stats['organized_protected']} Organized protected; "
        f"{stats['already_matched']} already have e621 metadata",
    )
    return rows, stats


def _ensure_tag(
    stash: Stash,
    name: str,
    cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    key = name.casefold()
    if key in cache:
        return cache[key]
    found = stash.find_tag_by_name(name)
    if not found:
        found = stash.create_tag(name)
    cache[key] = found
    return found


def _ensure_entity(
    stash: Stash,
    name: str,
    cache: Dict[str, Dict[str, Any]],
    kind: str,
) -> Dict[str, Any]:
    key = name.casefold().strip()
    if key in cache:
        return cache[key]
    obj = (
        stash.create_studio(name)
        if kind == "studio"
        else stash.create_performer(name)
    )
    cache[key] = obj
    return obj


def apply_e621_metadata(
    stash: Stash,
    scene: Dict[str, Any],
    post: Dict[str, Any],
    settings: Dict[str, Any],
    tag_cache: Dict[str, Dict[str, Any]],
    performer_cache: Dict[str, Dict[str, Any]],
    studio_cache: Dict[str, Dict[str, Any]],
) -> None:
    if protected_organized_scene(scene, settings):
        log(
            "INFO",
            f"Scene {scene.get('id')}: Organized protection became active; "
            "metadata unchanged",
        )
        return

    metadata = e621_post_metadata(post)

    tag_ids = [str(tag.get("id")) for tag in scene.get("tags") or []]
    for name in metadata["tags"]:
        tag = _ensure_tag(stash, name, tag_cache)
        if str(tag.get("id")) not in tag_ids:
            tag_ids.append(str(tag["id"]))

    imported = _ensure_tag(stash, STATUS_IMPORTED, tag_cache)
    if str(imported.get("id")) not in tag_ids:
        tag_ids.append(str(imported["id"]))

    performer_ids = [
        str(performer.get("id"))
        for performer in scene.get("performers") or []
    ]
    for name in metadata["characters"]:
        obj = performer_cache.get(name.casefold())
        if not obj:
            obj = _ensure_entity(
                stash, name, performer_cache, "performer"
            )
        if str(obj.get("id")) not in performer_ids:
            performer_ids.append(str(obj["id"]))

    studio_id: Optional[str] = None
    if metadata["artists"] and not scene.get("studio"):
        artist = metadata["artists"][0]
        obj = studio_cache.get(artist.casefold())
        if not obj:
            obj = _ensure_entity(stash, artist, studio_cache, "studio")
        studio_id = str(obj["id"])

    urls = list(scene.get("urls") or [])
    for url in metadata["urls"]:
        if url not in urls:
            urls.append(url)

    date = None if scene.get("date") else metadata.get("date")

    stash.update_scene(
        str(scene["id"]),
        tag_ids=list(dict.fromkeys(tag_ids)),
        performer_ids=list(dict.fromkeys(performer_ids)),
        studio_id=studio_id,
        date=date,
        urls=urls,
    )


def _prepare_local_entry(
    scene: Dict[str, Any],
    cache: Dict[str, Any],
    ffmpeg_path: str,
) -> Dict[str, Any]:
    """Load or lazily create the local comparison-frame cache."""
    scene_id = str(scene.get("id") or "")
    video = primary_video(scene)
    if not video:
        raise RuntimeError(f"Scene {scene_id} has no video file")

    local_path = str(video.get("path") or "").strip()
    if not local_path:
        raise RuntimeError(f"Scene {scene_id} has no local video path")

    signature = scene_signature(scene)
    cached_scenes = cache.setdefault("scenes", {})
    cached = (
        cached_scenes.get(scene_id)
        if isinstance(cached_scenes.get(scene_id), dict)
        else None
    )

    duration = as_float(video.get("duration"), 0.0)
    early_hashes: List[int] = []
    if (
        cached
        and str(cached.get("signature") or "") == signature
        and isinstance(cached.get("hashes"), list)
        and len(cached["hashes"]) >= 2
    ):
        duration = as_float(cached.get("duration"), duration)
        early_hashes = [int(value) for value in cached["hashes"][:2]]

    if duration <= 0:
        duration = probe_duration(local_path, ffmpeg_path, 30)

    if not early_hashes:
        early_hashes = early_frame_hashes(
            local_path,
            duration,
            ffmpeg_path=ffmpeg_path,
            timeout=45,
        )
        cached_scenes[scene_id] = {
            "signature": signature,
            "duration": round(float(duration), 3),
            "path": local_path,
            "hashes": [int(value) for value in early_hashes],
        }

    duration_ms = duration_milliseconds(duration)
    if duration_ms <= 0:
        raise RuntimeError(f"Scene {scene_id} has no usable duration")

    return {
        "scene": scene,
        "scene_id": scene_id,
        "path": local_path,
        "duration": float(duration),
        "duration_ms": duration_ms,
        "early_hashes": early_hashes,
    }


def _load_scope_state(
    scope_key: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    state = load_scan_state()
    scopes = state.setdefault("scopes", {})
    if not isinstance(scopes, dict):
        scopes = {}
        state["scopes"] = scopes
    scope_state = scopes.get(scope_key)
    if not isinstance(scope_state, dict):
        scope_state = {}
        scopes[scope_key] = scope_state
    scope_state.setdefault("completed_scene_ids", [])
    return state, scope_state


def _save_scope_state(
    state: Dict[str, Any],
    scope_key: str,
    *,
    scene_id: Optional[str],
    ext: str,
    before_id: Optional[int],
    completed_scene_ids: Sequence[str],
) -> None:
    scopes = state.setdefault("scopes", {})
    scopes[scope_key] = {
        "scene_id": str(scene_id) if scene_id else None,
        "ext": ext if ext in VIDEO_EXTENSIONS else "webm",
        "before_id": int(before_id) if before_id else None,
        "completed_scene_ids": sorted(
            {str(value) for value in completed_scene_ids}
        ),
        "updated_at": (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
    }
    save_scan_state(state)


def _ordered_remaining_scenes(
    scenes: Sequence[Dict[str, Any]],
    completed_scene_ids: Sequence[str],
    active_scene_id: Optional[str],
) -> List[Dict[str, Any]]:
    completed = {str(value) for value in completed_scene_ids}
    remaining = [
        scene
        for scene in scenes
        if str(scene.get("id") or "") not in completed
    ]
    if active_scene_id:
        for index, scene in enumerate(remaining):
            if str(scene.get("id") or "") == str(active_scene_id):
                return (
                    [scene]
                    + remaining[:index]
                    + remaining[index + 1 :]
                )
    return remaining


def reset_progress(
    stash: Stash,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    _tag_id, scope_key, scope_label = _scope(stash, settings)
    state = load_scan_state()
    scopes = state.setdefault("scopes", {})
    existed = scope_key in scopes
    scopes.pop(scope_key, None)
    save_scan_state(state)
    log("INFO", f"Reset matching progress for '{scope_label}'")
    return {"progress_reset": 1 if existed else 0, "scope": scope_label}


def match_stash_against_e621(
    stash: Stash,
    settings: Dict[str, Any],
    args: Dict[str, Any],
) -> Dict[str, int]:
    """Run the plugin's primary Stash-first exhaustive matching workflow."""
    dry_run = as_bool(args.get("dry_run"), False)
    username = str(settings.get("e621_username") or "")
    api_key = str(settings.get("e621_api_key") or "")
    configured_pages = as_int(
        args.get("pages_per_run"),
        as_int(settings.get("e621_pages_per_run"), 0),
    )
    # 0 or blank means continuous scanning for the active local video until a
    # verified match is found or both e621 WebM and MP4 histories are exhausted.
    continuous_scan = configured_pages <= 0
    pages_per_run = 0 if continuous_scan else max(1, configured_pages)
    local_limit = max(0, as_int(args.get("local_limit"), 0))

    scenes, local_stats = eligible_local_scenes(stash, settings)
    _tag_id, scope_key, scope_label = _scope(stash, settings)
    state, scope_state = _load_scope_state(scope_key)

    completed_scene_ids = [
        str(value)
        for value in scope_state.get("completed_scene_ids") or []
    ]
    active_scene_id = str(scope_state.get("scene_id") or "") or None
    active_ext = str(scope_state.get("ext") or "webm").casefold()
    if active_ext not in VIDEO_EXTENSIONS:
        active_ext = "webm"
    active_before_id = as_int(scope_state.get("before_id"), 0) or None

    remaining = _ordered_remaining_scenes(
        scenes,
        completed_scene_ids,
        active_scene_id,
    )
    if local_limit:
        remaining = remaining[:local_limit]

    stats = {
        "eligible_local_videos": local_stats["eligible"],
        "organized_protected": local_stats["organized_protected"],
        "local_videos_started": 0,
        "local_videos_matched": 0,
        "local_videos_exhausted": 0,
        "md5_checked": 0,
        "md5_exact_matches": 0,
        "md5_unavailable": 0,
        "md5_lookup_errors": 0,
        "e621_posts_seen": 0,
        "duration_filtered": 0,
        "duration_unknown": 0,
        "exact_duration_candidates": 0,
        "frame_rejected": 0,
        "verified_rejected": 0,
        "provider_errors": 0,
    }

    if not remaining:
        log("INFO", f"No remaining eligible local videos for '{scope_label}'")
        return stats

    ffmpeg_path = stash.ffmpeg_path()
    local_cache = load_cache()
    tag_cache = stash.all_tags()
    performer_cache = stash.all_performers()
    studio_cache = stash.all_studios()

    for scene in remaining:
        scene_id = str(scene.get("id") or "")
        resume_this_scene = scene_id == active_scene_id
        current_ext = active_ext if resume_this_scene else "webm"
        before_id = active_before_id if resume_this_scene else None

        stats["local_videos_started"] += 1

        video = primary_video(scene)
        local_md5 = video_file_fingerprint(video or {}, "md5")
        if local_md5:
            stats["md5_checked"] += 1
            try:
                exact_post = e621_post_by_md5(
                    local_md5,
                    username,
                    api_key,
                )
            except Exception as exc:
                exact_post = None
                stats["md5_lookup_errors"] += 1
                stats["provider_errors"] += 1
                log(
                    "WARNING",
                    f"Scene {scene_id}: direct e621 MD5 lookup failed: {exc}; "
                    "falling back to duration/frame search",
                )

            if exact_post is not None:
                post_id = as_int(exact_post.get("id"), 0)
                stats["md5_exact_matches"] += 1
                stats["local_videos_matched"] += 1
                if dry_run:
                    log(
                        "INFO",
                        f"Scene {scene_id}: exact file MD5 matched e621 "
                        f"#{post_id} (preview only; no changes)",
                    )
                    break

                apply_e621_metadata(
                    stash,
                    scene,
                    exact_post,
                    settings,
                    tag_cache,
                    performer_cache,
                    studio_cache,
                )
                completed_scene_ids.append(scene_id)
                _save_scope_state(
                    state,
                    scope_key,
                    scene_id=None,
                    ext="webm",
                    before_id=None,
                    completed_scene_ids=completed_scene_ids,
                )
                log(
                    "INFO",
                    f"Scene {scene_id}: exact file MD5 matched e621 "
                    f"#{post_id}; metadata attached without history scan",
                )
                active_scene_id = None
                active_ext = "webm"
                active_before_id = None
                continue
        else:
            stats["md5_unavailable"] += 1
            log(
                "INFO",
                f"Scene {scene_id}: no MD5 fingerprint on primary video file; "
                "using duration/frame search",
            )

        try:
            entry = _prepare_local_entry(
                scene,
                local_cache,
                ffmpeg_path,
            )
            if not dry_run:
                save_cache(local_cache)
        except Exception as exc:
            stats["provider_errors"] += 1
            log("WARNING", f"Scene {scene_id}: local preparation failed: {exc}")
            break

        log(
            "INFO",
            f"Scene {scene_id}: searching all e621 video history for exact "
            f"duration {entry['duration_ms']} ms; starting {current_ext} "
            f"at {before_id or 'newest'}",
        )

        local_full_hashes: Optional[List[int]] = None
        matched = False
        exhausted_scene = False
        pages_used = 0

        while (
            (continuous_scan or pages_used < pages_per_run)
            and not matched
            and not exhausted_scene
        ):
            try:
                posts = e621_video_posts_page(
                    current_ext,
                    username,
                    api_key,
                    before_id=before_id,
                    limit=E621_PAGE_SIZE,
                )
            except Exception as exc:
                stats["provider_errors"] += 1
                log(
                    "WARNING",
                    f"Scene {scene_id}: e621 {current_ext} history request "
                    f"failed: {exc}",
                )
                break

            if not posts:
                if current_ext == "webm":
                    current_ext = "mp4"
                    before_id = None
                    log(
                        "INFO",
                        f"Scene {scene_id}: WebM history exhausted; "
                        "continuing with MP4 from newest",
                    )
                    if not dry_run:
                        _save_scope_state(
                            state,
                            scope_key,
                            scene_id=scene_id,
                            ext=current_ext,
                            before_id=None,
                            completed_scene_ids=completed_scene_ids,
                        )
                    continue

                exhausted_scene = True
                break

            pages_used += 1
            last_post_id: Optional[int] = None

            for post in posts:
                post_id = as_int(post.get("id"), 0)
                if post_id > 0:
                    last_post_id = post_id
                stats["e621_posts_seen"] += 1

                info = e621_file_info(post)
                remote_duration_ms = duration_milliseconds(
                    info.get("duration")
                )

                # Mandatory first gate: do not open remote video data unless
                # duration is known and exactly equal to the active local file.
                if remote_duration_ms <= 0:
                    stats["duration_unknown"] += 1
                    continue
                if remote_duration_ms != entry["duration_ms"]:
                    stats["duration_filtered"] += 1
                    continue

                remote_url = str(info.get("url") or "").strip()
                if not remote_url:
                    continue

                stats["exact_duration_candidates"] += 1
                log(
                    "INFO",
                    f"Scene {scene_id}: e621 #{post_id} has exact duration "
                    f"{remote_duration_ms} ms; comparing same-timecode frames",
                )

                try:
                    remote_early = early_frame_hashes(
                        remote_url,
                        float(info["duration"]),
                        ffmpeg_path=ffmpeg_path,
                        timeout=60,
                    )
                except Exception as exc:
                    stats["provider_errors"] += 1
                    log(
                        "WARNING",
                        f"Scene {scene_id}: e621 #{post_id} frame extraction "
                        f"failed: {exc}; checking next exact-duration file",
                    )
                    continue

                early = early_hash_candidate(
                    entry["early_hashes"],
                    remote_early,
                    max_distance=EARLY_FRAME_DISTANCE,
                )
                if not early.get("candidate"):
                    stats["frame_rejected"] += 1
                    log(
                        "INFO",
                        f"Scene {scene_id}: e621 #{post_id} duration matched "
                        f"but same-timecode frames did not; checking next file",
                    )
                    continue

                try:
                    if local_full_hashes is None:
                        local_full_hashes = frame_hashes(
                            entry["path"],
                            entry["duration"],
                            ffmpeg_path=ffmpeg_path,
                            ratios=DEFAULT_RATIOS,
                            timeout=45,
                        )
                    remote_full_hashes = frame_hashes(
                        remote_url,
                        float(info["duration"]),
                        ffmpeg_path=ffmpeg_path,
                        ratios=DEFAULT_RATIOS,
                        timeout=60,
                    )
                    verification = verify_video_candidate(
                        entry["path"],
                        remote_url,
                        ffmpeg_path=ffmpeg_path,
                        ratios=DEFAULT_RATIOS,
                        frame_distance=VERIFY_FRAME_DISTANCE,
                        timeout=60,
                        local_duration=entry["duration"],
                        local_hashes=local_full_hashes,
                        remote_duration=float(info["duration"]),
                        remote_hashes=remote_full_hashes,
                        strict=True,
                    )
                except Exception as exc:
                    stats["provider_errors"] += 1
                    log(
                        "WARNING",
                        f"Scene {scene_id}: e621 #{post_id} verification "
                        f"failed: {exc}; checking next exact-duration file",
                    )
                    continue

                if not verification.get("high"):
                    stats["verified_rejected"] += 1
                    log(
                        "INFO",
                        f"Scene {scene_id}: e621 #{post_id} rejected after "
                        f"{verification.get('matched_frames')}/"
                        f"{verification.get('total_frames')} aligned frames; "
                        "checking next exact-duration file",
                    )
                    continue

                matched = True
                stats["local_videos_matched"] += 1
                if dry_run:
                    log(
                        "INFO",
                        f"Scene {scene_id}: VERIFIED e621 #{post_id} "
                        "(preview only; no changes)",
                    )
                else:
                    apply_e621_metadata(
                        stash,
                        scene,
                        post,
                        settings,
                        tag_cache,
                        performer_cache,
                        studio_cache,
                    )
                    completed_scene_ids.append(scene_id)
                    _save_scope_state(
                        state,
                        scope_key,
                        scene_id=None,
                        ext="webm",
                        before_id=None,
                        completed_scene_ids=completed_scene_ids,
                    )
                    log(
                        "INFO",
                        f"Scene {scene_id}: MATCH e621 #{post_id}; "
                        "all authoritative metadata attached",
                    )
                break

            if matched:
                break

            if last_post_id:
                before_id = last_post_id
                if not dry_run:
                    _save_scope_state(
                        state,
                        scope_key,
                        scene_id=scene_id,
                        ext=current_ext,
                        before_id=before_id,
                        completed_scene_ids=completed_scene_ids,
                    )
            else:
                # A non-empty page should have IDs, but do not risk looping.
                stats["provider_errors"] += 1
                log(
                    "WARNING",
                    f"Scene {scene_id}: e621 page contained no usable post IDs; "
                    "stopping without advancing progress",
                )
                break

        if dry_run:
            # Preview intentionally examines one local file and never saves state.
            break

        if matched:
            active_scene_id = None
            active_ext = "webm"
            active_before_id = None
            continue

        if exhausted_scene:
            stats["local_videos_exhausted"] += 1
            completed_scene_ids.append(scene_id)
            _save_scope_state(
                state,
                scope_key,
                scene_id=None,
                ext="webm",
                before_id=None,
                completed_scene_ids=completed_scene_ids,
            )
            log(
                "INFO",
                f"Scene {scene_id}: no verified match after complete e621 "
                "WebM + MP4 history; moving to next local Stash video",
            )
            active_scene_id = None
            active_ext = "webm"
            active_before_id = None
            continue

        # If we are here, the scene did not match and did not exhaust history.
        # Either a finite page budget was reached or a provider/runtime issue
        # interrupted a continuous scan. Persist a safe resume cursor.
        _save_scope_state(
            state,
            scope_key,
            scene_id=scene_id,
            ext=current_ext,
            before_id=before_id,
            completed_scene_ids=completed_scene_ids,
        )
        if continuous_scan:
            log(
                "INFO",
                f"Scene {scene_id}: continuous scan stopped before history "
                f"was exhausted; next run resumes this same file in "
                f"{current_ext} at {before_id or 'newest'}",
            )
        else:
            log(
                "INFO",
                f"Scene {scene_id}: configured page budget reached; next run "
                f"resumes this same file in {current_ext} at "
                f"{before_id or 'newest'}",
            )
        break

    return stats


def main() -> None:
    payload = read_input()
    stash = Stash(payload.get("server_connection") or {})
    settings = stash.settings()
    args = payload.get("args") or {}
    mode = str(args.get("mode") or "match")

    if mode == "match":
        stats = match_stash_against_e621(stash, settings, args)
    elif mode == "reset":
        stats = reset_progress(stash, settings)
    else:
        raise RuntimeError(f"Unsupported mode: {mode}")

    log("INFO", f"Finished Booru Video Importer: {stats}")
    print(json.dumps({"output": "ok", "stats": stats}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("ERROR", str(exc))
        print(json.dumps({"output": "error", "error": str(exc)}))
        sys.exit(1)
