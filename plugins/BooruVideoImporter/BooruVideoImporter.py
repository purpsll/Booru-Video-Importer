#!/usr/bin/env python3
"""Booru Video Importer using a persistent local e621 video catalog.

Catalog workflow:
- Crawl e621 WebM/MP4 posts once and store post ID, URL, MD5 and duration.
- Generate real 64-bit DCT perceptual hashes at 10%, 50% and 90% for each video.
- Resume safely from per-format ascending cursors and retry failed hash rows.

Match workflow:
- Primary Stash video MD5 first.
- Otherwise query SQLite for exact-duration e621 rows only.
- Compare the three cached pHashes locally and rank plausible candidates.
- Open only plausible candidate URLs and verify one midpoint frame at the same
  timecode before importing the authoritative e621 post metadata.
"""
from __future__ import annotations

import email.utils
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from e621_catalog import E621VideoCatalog, HASH_RATIOS, HASH_VERSION
from stash_client import Stash, primary_video
from source_index import load_cache, save_cache, scene_signature
from video_match import (
    frame_phash,
    frame_phashes,
    frame_timestamp_seconds,
    hamming_distance,
    phash_from_hex,
    phash_hex,
    probe_duration,
)

VERSION = "3.0.0"
E621_BASE = "https://e621.net"
E621_PAGE_SIZE = 75
VIDEO_EXTENSIONS = ("webm", "mp4")
STATUS_IMPORTED = "Booru Video Imported"

# Three cached 64-bit pHashes already provide a strong local candidate filter.
# Final remote verification then rechecks the midpoint frame at the same timecode.
CATALOG_PHASH_MAX_DISTANCE = 10
CATALOG_PHASH_MEDIAN_DISTANCE = 8.0
FINAL_MIDPOINT_DISTANCE = 8
HASH_RETRY_LIMIT = 3

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


def duration_milliseconds(value: Any) -> int:
    seconds = as_float(value, 0.0)
    if seconds <= 0:
        return 0
    return int(round(seconds * 1000.0))


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


def e621_post_by_id(
    post_id: int,
    username: str,
    api_key: str,
) -> Optional[Dict[str, Any]]:
    payload = e621_request(
        f"{E621_BASE}/posts/{int(post_id)}.json",
        username,
        api_key,
    )
    posts = _e621_posts(payload)
    return posts[0] if posts else None


def e621_post_by_md5(
    md5: str,
    username: str,
    api_key: str,
) -> Optional[Dict[str, Any]]:
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


def e621_video_posts_after(
    ext: str,
    username: str,
    api_key: str,
    after_id: int = 0,
    limit: int = E621_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """Enumerate one video format in ascending post-ID order."""
    ext = str(ext).casefold().lstrip(".")
    if ext not in VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported e621 video extension: {ext}")
    params = {
        "tags": f"type:{ext}",
        "limit": str(max(1, min(320, int(limit)))),
        "page": f"a{max(0, int(after_id))}",
        "v2": "true",
        "mode": "extended",
    }
    payload = e621_request(
        f"{E621_BASE}/posts.json?{urllib.parse.urlencode(params)}",
        username,
        api_key,
    )
    rows = []
    for post in _e621_posts(payload):
        info = e621_file_info(post)
        post_id = as_int(post.get("id"), 0)
        if (
            post_id > int(after_id)
            and info.get("ext") == ext
            and info.get("url")
        ):
            rows.append(post)
    rows.sort(key=lambda post: as_int(post.get("id"), 0))
    return rows


def video_file_fingerprint(
    video: Dict[str, Any],
    kind: str,
) -> Optional[str]:
    target = str(kind or "").casefold()
    for fingerprint in video.get("fingerprints") or []:
        if str(fingerprint.get("type") or "").casefold() == target:
            value = str(fingerprint.get("value") or "").strip()
            if value:
                return value
    return None


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
) -> Tuple[Optional[str], str]:
    requested = configured_stash_tag_scope(settings)
    if not requested:
        return None, "all eligible Stash videos"
    tag_id, canonical = resolve_stash_tag_filter(stash, requested)
    if not tag_id:
        raise RuntimeError(f"Stash tag scope not found: {requested}")
    return tag_id, canonical or requested


def eligible_local_scenes(
    stash: Stash,
    settings: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    filter_tag_id, scope_label = _scope(stash, settings)
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
    """Merge authoritative e621 metadata without overwriting existing date/studio."""
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
            obj = _ensure_entity(stash, name, performer_cache, "performer")
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
    """Load/create exact duration and three local pHashes lazily."""
    scene_id = str(scene.get("id") or "")
    video = primary_video(scene)
    if not video:
        raise RuntimeError(f"Scene {scene_id} has no video file")
    path = str(video.get("path") or "").strip()
    if not path:
        raise RuntimeError(f"Scene {scene_id} has no local video path")

    signature = scene_signature(scene)
    cached_scenes = cache.setdefault("scenes", {})
    cached = (
        cached_scenes.get(scene_id)
        if isinstance(cached_scenes.get(scene_id), dict)
        else None
    )

    duration = as_float(video.get("duration"), 0.0)
    if duration <= 0:
        duration = probe_duration(path, ffmpeg_path, 30)
    duration_ms = duration_milliseconds(duration)
    if duration_ms <= 0:
        raise RuntimeError(f"Scene {scene_id} has no usable duration")

    phashes: List[int] = []
    if (
        cached
        and str(cached.get("signature") or "") == signature
        and as_int(cached.get("duration_ms"), 0) == duration_ms
        and isinstance(cached.get("phashes"), list)
        and len(cached["phashes"]) == 3
    ):
        phashes = [phash_from_hex(str(value)) for value in cached["phashes"]]

    if not phashes:
        phashes = frame_phashes(
            path,
            duration,
            ffmpeg_path=ffmpeg_path,
            ratios=HASH_RATIOS,
            timeout=45,
        )
        cached_scenes[scene_id] = {
            "signature": signature,
            "duration": round(float(duration), 6),
            "duration_ms": duration_ms,
            "path": path,
            "phashes": [phash_hex(value) for value in phashes],
        }

    return {
        "scene": scene,
        "scene_id": scene_id,
        "path": path,
        "duration": float(duration),
        "duration_ms": duration_ms,
        "phashes": phashes,
    }


def _catalog_row_from_post(post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    info = e621_file_info(post)
    post_id = as_int(post.get("id"), 0)
    if (
        post_id <= 0
        or info.get("ext") not in VIDEO_EXTENSIONS
        or not info.get("url")
    ):
        return None
    return {
        "post_id": post_id,
        "ext": info["ext"],
        "duration_ms": duration_milliseconds(info.get("duration")),
        "url": info["url"],
        "md5": info.get("md5") or "",
        "updated_at": str(post.get("updated_at") or ""),
    }


def _hash_catalog_row(
    catalog: E621VideoCatalog,
    row: Dict[str, Any],
    ffmpeg_path: str,
) -> bool:
    post_id = as_int(row.get("post_id"), 0)
    url = str(row.get("url") or "")
    duration_ms = as_int(row.get("duration_ms"), 0)
    try:
        duration = duration_ms / 1000.0 if duration_ms > 0 else 0.0
        if duration <= 0:
            duration = probe_duration(url, ffmpeg_path, 60)
            duration_ms = duration_milliseconds(duration)
            if duration_ms <= 0:
                raise RuntimeError("video duration unavailable")
            catalog.set_duration(post_id, duration_ms)

        hashes = frame_phashes(
            url,
            duration,
            ffmpeg_path=ffmpeg_path,
            ratios=HASH_RATIOS,
            timeout=90,
        )
        timecodes_ms = [
            int(round(frame_timestamp_seconds(duration, ratio) * 1000.0))
            for ratio in HASH_RATIOS
        ]
        catalog.set_hashes(
            post_id,
            [phash_hex(value) for value in hashes],
            timecodes_ms,
            HASH_VERSION,
        )
        return True
    except Exception as exc:
        catalog.set_hash_error(post_id, str(exc))
        log(
            "WARNING",
            f"e621 #{post_id}: pHash generation failed: {exc}",
        )
        return False


def build_update_catalog(
    stash: Stash,
    settings: Dict[str, Any],
    args: Dict[str, Any],
) -> Dict[str, int]:
    """Crawl e621 videos once and incrementally populate the persistent catalog."""
    username = str(settings.get("e621_username") or "")
    api_key = str(settings.get("e621_api_key") or "")
    page_limit = max(0, as_int(args.get("page_limit"), 0))
    hash_limit = max(0, as_int(args.get("hash_limit"), 0))
    catalog = E621VideoCatalog()
    ffmpeg_path = stash.ffmpeg_path()

    stats = {
        "catalog_rows_before": catalog.count(),
        "metadata_posts_seen": 0,
        "metadata_rows_upserted": 0,
        "hashes_generated": 0,
        "hash_failures": 0,
        "provider_errors": 0,
    }

    # Retry old un-hashed rows first, so an earlier transient failure does not
    # become permanently stranded behind the metadata cursor.
    retry_limit = hash_limit if hash_limit > 0 else 1000000
    for row in catalog.rows_needing_hash(
        limit=max(1, retry_limit),
        max_attempts=HASH_RETRY_LIMIT,
    ):
        if hash_limit and stats["hashes_generated"] + stats["hash_failures"] >= hash_limit:
            break
        if _hash_catalog_row(catalog, row, ffmpeg_path):
            stats["hashes_generated"] += 1
        else:
            stats["hash_failures"] += 1

    pages_used = 0
    for ext in VIDEO_EXTENSIONS:
        cursor = catalog.get_cursor(ext)
        while page_limit <= 0 or pages_used < page_limit:
            try:
                posts = e621_video_posts_after(
                    ext,
                    username,
                    api_key,
                    after_id=cursor,
                    limit=E621_PAGE_SIZE,
                )
            except Exception as exc:
                stats["provider_errors"] += 1
                log(
                    "WARNING",
                    f"e621 {ext} catalog crawl failed after #{cursor}: {exc}",
                )
                break

            if not posts:
                catalog.set_up_to_date(ext, True)
                break

            pages_used += 1
            rows = []
            for post in posts:
                stats["metadata_posts_seen"] += 1
                row = _catalog_row_from_post(post)
                if row:
                    rows.append(row)
            upserted = catalog.upsert_videos(rows)
            stats["metadata_rows_upserted"] += as_int(upserted, 0)

            last_id = max(
                (as_int(post.get("id"), 0) for post in posts),
                default=cursor,
            )
            if last_id <= cursor:
                stats["provider_errors"] += 1
                log(
                    "WARNING",
                    f"e621 {ext} catalog crawl did not advance beyond #{cursor}",
                )
                break

            cursor = last_id
            catalog.set_cursor(ext, cursor)
            catalog.set_up_to_date(ext, False)
            log(
                "INFO",
                f"Catalog {ext}: indexed metadata through e621 #{cursor}; "
                f"{catalog.count()} total video rows",
            )

            # Hash newly discovered rows immediately. This remains resumable:
            # metadata is already committed and rows with failures stay pending.
            pending = catalog.rows_needing_hash(
                limit=1000000 if hash_limit <= 0 else max(1, hash_limit),
                max_attempts=HASH_RETRY_LIMIT,
            )
            for row in pending:
                if (
                    hash_limit
                    and stats["hashes_generated"] + stats["hash_failures"] >= hash_limit
                ):
                    break
                if _hash_catalog_row(catalog, row, ffmpeg_path):
                    stats["hashes_generated"] += 1
                else:
                    stats["hash_failures"] += 1

            if hash_limit and stats["hashes_generated"] + stats["hash_failures"] >= hash_limit:
                break

        if page_limit > 0 and pages_used >= page_limit:
            break
        if hash_limit and stats["hashes_generated"] + stats["hash_failures"] >= hash_limit:
            break

    stats["catalog_rows_after"] = catalog.count()
    stats["catalog_hashed"] = catalog.hashed_count()
    stats["catalog_hash_failed"] = catalog.failed_count()
    stats["duration_buckets"] = catalog.duration_bucket_count()
    log("INFO", f"e621 video catalog update finished: {stats}")
    return stats


def catalog_candidate_score(
    local_hashes: Sequence[int],
    row: Dict[str, Any],
) -> Optional[Tuple[float, int, List[int]]]:
    try:
        remote = [
            phash_from_hex(str(row["phash_10"])),
            phash_from_hex(str(row["phash_50"])),
            phash_from_hex(str(row["phash_90"])),
        ]
    except (KeyError, TypeError, ValueError):
        return None
    if len(local_hashes) != 3:
        return None

    distances = [
        hamming_distance(local_hashes[index], remote[index])
        for index in range(3)
    ]
    median = float(statistics.median(distances))
    maximum = max(distances)
    if median > CATALOG_PHASH_MEDIAN_DISTANCE:
        return None
    if maximum > CATALOG_PHASH_MAX_DISTANCE:
        return None
    return median, maximum, distances


def _apply_matched_post(
    stash: Stash,
    scene: Dict[str, Any],
    post: Dict[str, Any],
    settings: Dict[str, Any],
    tag_cache: Dict[str, Dict[str, Any]],
    performer_cache: Dict[str, Dict[str, Any]],
    studio_cache: Dict[str, Dict[str, Any]],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    apply_e621_metadata(
        stash,
        scene,
        post,
        settings,
        tag_cache,
        performer_cache,
        studio_cache,
    )


def match_stash_against_catalog(
    stash: Stash,
    settings: Dict[str, Any],
    args: Dict[str, Any],
) -> Dict[str, int]:
    dry_run = as_bool(args.get("dry_run"), False)
    local_limit = max(0, as_int(args.get("local_limit"), 0))
    username = str(settings.get("e621_username") or "")
    api_key = str(settings.get("e621_api_key") or "")

    scenes, local_stats = eligible_local_scenes(stash, settings)
    if local_limit:
        scenes = scenes[:local_limit]

    catalog = E621VideoCatalog()
    catalog_rows = catalog.count()
    catalog_hashed = catalog.hashed_count()

    stats = {
        "eligible_local_videos": local_stats["eligible"],
        "organized_protected": local_stats["organized_protected"],
        "catalog_rows": catalog_rows,
        "catalog_hashed": catalog_hashed,
        "local_videos_started": 0,
        "local_videos_matched": 0,
        "md5_catalog_matches": 0,
        "md5_api_matches": 0,
        "duration_candidates": 0,
        "phash_candidates": 0,
        "midpoint_verified": 0,
        "unmatched": 0,
        "provider_errors": 0,
    }

    if not scenes:
        log("INFO", "No eligible local videos to match")
        return stats

    if catalog_rows == 0:
        log(
            "WARNING",
            "e621 video catalog is empty. Run 'Build / Update e621 Video Catalog' "
            "before pHash matching. Direct MD5 lookups will still be attempted.",
        )

    ffmpeg_path = stash.ffmpeg_path()
    local_cache = load_cache()
    tag_cache = stash.all_tags()
    performer_cache = stash.all_performers()
    studio_cache = stash.all_studios()

    for scene in scenes:
        scene_id = str(scene.get("id") or "")
        stats["local_videos_started"] += 1
        video = primary_video(scene) or {}
        local_md5 = video_file_fingerprint(video, "md5")

        # Fastest path: exact byte-identical file from the local catalog.
        post: Optional[Dict[str, Any]] = None
        if local_md5:
            cached_md5 = catalog.find_md5(local_md5)
            if cached_md5:
                try:
                    post = e621_post_by_id(
                        as_int(cached_md5.get("post_id"), 0),
                        username,
                        api_key,
                    )
                except Exception as exc:
                    stats["provider_errors"] += 1
                    log(
                        "WARNING",
                        f"Scene {scene_id}: cached MD5 post fetch failed: {exc}",
                    )
                if post:
                    stats["md5_catalog_matches"] += 1

            # The catalog may not yet include today's newest post, so retain a
            # direct MD5 lookup as a zero-ambiguity safety path.
            if post is None:
                try:
                    post = e621_post_by_md5(local_md5, username, api_key)
                except Exception as exc:
                    stats["provider_errors"] += 1
                    log(
                        "WARNING",
                        f"Scene {scene_id}: direct e621 MD5 lookup failed: {exc}",
                    )
                if post:
                    stats["md5_api_matches"] += 1

        if post is not None:
            stats["local_videos_matched"] += 1
            log(
                "INFO",
                f"Scene {scene_id}: exact MD5 match e621 #{post.get('id')}",
            )
            _apply_matched_post(
                stash,
                scene,
                post,
                settings,
                tag_cache,
                performer_cache,
                studio_cache,
                dry_run,
            )
            if dry_run:
                break
            continue

        # pHash path.
        try:
            entry = _prepare_local_entry(scene, local_cache, ffmpeg_path)
            if not dry_run:
                save_cache(local_cache)
        except Exception as exc:
            stats["provider_errors"] += 1
            log("WARNING", f"Scene {scene_id}: local pHash preparation failed: {exc}")
            if dry_run:
                break
            continue

        rows = catalog.candidates_for_duration(
            entry["duration_ms"],
            require_hashes=True,
        )
        stats["duration_candidates"] += len(rows)

        ranked = []
        for row in rows:
            score = catalog_candidate_score(entry["phashes"], row)
            if score is None:
                continue
            ranked.append((score[0], score[1], as_int(row.get("post_id"), 0), row, score[2]))

        ranked.sort(key=lambda item: (item[0], item[1], -item[2]))
        stats["phash_candidates"] += len(ranked)

        matched = False
        for median, maximum, post_id, row, distances in ranked:
            url = str(row.get("url") or "")
            if not url:
                continue
            try:
                remote_midpoint = frame_phash(
                    url,
                    entry["duration"],
                    HASH_RATIOS[1],
                    ffmpeg_path=ffmpeg_path,
                    timeout=90,
                )
                midpoint_distance = hamming_distance(
                    entry["phashes"][1],
                    remote_midpoint,
                )
            except Exception as exc:
                stats["provider_errors"] += 1
                log(
                    "WARNING",
                    f"Scene {scene_id}: e621 #{post_id} midpoint verification "
                    f"failed: {exc}",
                )
                continue

            if midpoint_distance > FINAL_MIDPOINT_DISTANCE:
                log(
                    "INFO",
                    f"Scene {scene_id}: e621 #{post_id} cached pHash candidate "
                    f"rejected by live midpoint frame (distance {midpoint_distance})",
                )
                continue

            try:
                post = e621_post_by_id(post_id, username, api_key)
            except Exception as exc:
                stats["provider_errors"] += 1
                log(
                    "WARNING",
                    f"Scene {scene_id}: e621 #{post_id} metadata fetch failed: {exc}",
                )
                continue
            if not post:
                continue

            matched = True
            stats["midpoint_verified"] += 1
            stats["local_videos_matched"] += 1
            log(
                "INFO",
                f"Scene {scene_id}: MATCH e621 #{post_id}; exact duration, "
                f"cached pHash distances {distances}, live midpoint "
                f"distance {midpoint_distance}",
            )
            _apply_matched_post(
                stash,
                scene,
                post,
                settings,
                tag_cache,
                performer_cache,
                studio_cache,
                dry_run,
            )
            break

        if not matched:
            stats["unmatched"] += 1
            log(
                "INFO",
                f"Scene {scene_id}: no verified catalog match for exact duration "
                f"{entry['duration_ms']} ms",
            )

        if dry_run:
            break

    log("INFO", f"Catalog match finished: {stats}")
    return stats


def catalog_status() -> Dict[str, int]:
    catalog = E621VideoCatalog()
    return {
        "catalog_rows": catalog.count(),
        "catalog_hashed": catalog.hashed_count(),
        "catalog_hash_failed": catalog.failed_count(),
        "duration_buckets": catalog.duration_bucket_count(),
        "webm_cursor": catalog.get_cursor("webm"),
        "mp4_cursor": catalog.get_cursor("mp4"),
    }


def main() -> None:
    payload = read_input()
    stash = Stash(payload.get("server_connection") or {})
    settings = stash.settings()
    args = payload.get("args") or {}
    mode = str(args.get("mode") or "match")

    if mode == "catalog":
        stats = build_update_catalog(stash, settings, args)
    elif mode == "catalog_status":
        stats = catalog_status()
        log("INFO", f"e621 video catalog status: {stats}")
    elif mode == "match":
        stats = match_stash_against_catalog(stash, settings, args)
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
