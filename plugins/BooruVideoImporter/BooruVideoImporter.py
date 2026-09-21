#!/usr/bin/env python3
"""Standalone reverse-video metadata importer for Stash scenes.

The image Booru Importer is intentionally not imported or modified here.

Matching pipeline:
1. Exact video MD5 against e621 and Rule34.
2. Deep mode extracts representative frames from the Stash scene.
3. SauceNAO locates e621/Rule34 candidate posts from those frames.
4. Authenticated e621 ERIS is used as an additional e621-only frame locator.
5. Candidate posts must actually contain a video file.
6. Local and remote videos are compared across multiple perceptual frame hashes.
7. Only high-confidence video verification imports metadata; borderline results go
   to a separate Review queue.
"""
from __future__ import annotations

import base64
import email.utils
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from stash_client import Stash, fingerprint, primary_video
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
    extract_jpeg_frame,
    frame_hashes,
    probe_duration,
    verify_video_candidate,
)

VERSION = "1.2.0"
USER_AGENT = f"stash-booru-video-importer/{VERSION}"

E621_BASE = "https://e621.net"
RULE34_API = "https://api.rule34.xxx/index.php"
SAUCENAO_API = "https://saucenao.com/search.php"

STATUS_IMPORTED = "Booru Video Imported"
STATUS_UNRESOLVED = "Booru Video Unresolved"
STATUS_REVIEW = "Booru Video Review"
STATUS_NO_MATCH = "Booru Video No Match"
STATUS_RETRY = "Booru Video Retry Later"
STATUS_NAMES = {
    STATUS_IMPORTED,
    STATUS_UNRESOLVED,
    STATUS_REVIEW,
    STATUS_NO_MATCH,
    STATUS_RETRY,
}

VIDEO_EXTENSIONS = {"mp4", "webm", "m4v", "mov", "mkv", "avi"}
DISCOVERY_RATIOS = (0.12, 0.32, 0.52, 0.72, 0.88)
MAX_CANDIDATES = 8
E621_ERIS_DISCOVERY_SCORE = 60.0
SAUCENAO_DISCOVERY_SCORE = 80.0
VERIFY_FRAME_DISTANCE = 24
SOURCE_FIRST_VERIFY_FRAME_DISTANCE = 16
E621_SOURCE_PAGE_SIZE = 75
E621_SOURCE_MAX_CANDIDATES = 5
E621_SOURCE_EARLY_DISTANCE = 4

_LAST_E621_REQUEST = 0.0
_LAST_E621_ERIS = 0.0
_LAST_RULE34 = 0.0
_LAST_SAUCENAO = 0.0
_SAUCENAO_SHORT_LIMIT = 4.0
_ERIS_DISABLED_FOR_RUN = ""


def _prefix(level: str) -> str:
    char = {"DEBUG": "d", "INFO": "i", "WARNING": "w", "ERROR": "e"}.get(level.upper(), "i")
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


def _json_request(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = 45,
) -> Any:
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Provider returned non-JSON data: {' '.join(raw.split())[:240]}"
        ) from exc


def _basic_auth(username: str, api_key: str) -> str:
    token = base64.b64encode(f"{username}:{api_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def e621_headers(username: str, api_key: str) -> Dict[str, str]:
    ua = f"BooruVideoImporter/{VERSION}"
    if username:
        ua += f" (by {username} on e621)"
    headers = {"User-Agent": ua, "Accept": "application/json"}
    if username and api_key:
        headers["Authorization"] = _basic_auth(username, api_key)
    return headers


def e621_request(url: str, username: str, api_key: str) -> Any:
    global _LAST_E621_REQUEST
    _LAST_E621_REQUEST = _wait(_LAST_E621_REQUEST, 1.0)
    return _json_request(url, headers=e621_headers(username, api_key))


def _e621_posts(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [post for post in payload if isinstance(post, dict)]
    if isinstance(payload, dict):
        posts = payload.get("posts")
        if isinstance(posts, list):
            return [post for post in posts if isinstance(post, dict)]
        post = payload.get("post")
        if isinstance(post, dict):
            return [post]
        if payload.get("id"):
            return [payload]
    return []


def e621_file_info(post: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize e621's legacy and current v2 file schemas."""
    legacy = post.get("file")
    if isinstance(legacy, dict):
        preview = post.get("preview") or {}
        return {
            "ext": str(legacy.get("ext") or "").casefold().lstrip("."),
            "url": str(legacy.get("url") or "").strip(),
            "md5": str(legacy.get("md5") or "").strip(),
            "duration": as_float(legacy.get("duration") or post.get("duration"), 0.0),
            "preview_url": str(
                preview.get("url") if isinstance(preview, dict) else ""
            ).strip(),
        }

    files = post.get("files")
    if isinstance(files, dict):
        meta = files.get("meta") or {}
        original = files.get("original") or {}
        preview = files.get("preview") or {}
        preview_url = ""
        if isinstance(preview, dict):
            preview_url = str(preview.get("jpg") or preview.get("webp") or "").strip()
        return {
            "ext": str(meta.get("ext") or "").casefold().lstrip("."),
            "url": str(original.get("url") if isinstance(original, dict) else "").strip(),
            "md5": str(meta.get("md5") or "").strip(),
            "duration": as_float(meta.get("duration"), 0.0),
            "preview_url": preview_url,
        }

    return {"ext": "", "url": "", "md5": "", "duration": 0.0, "preview_url": ""}


def e621_post_by_id(post_id: str, username: str, api_key: str) -> Optional[Dict[str, Any]]:
    params = urllib.parse.urlencode({"v2": "true", "mode": "extended"})
    payload = e621_request(
        f"{E621_BASE}/posts/{urllib.parse.quote(str(post_id))}.json?{params}",
        username,
        api_key,
    )
    posts = _e621_posts(payload)
    return posts[0] if posts else None


def e621_post_by_md5(md5: str, username: str, api_key: str) -> Optional[Dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"tags": f"md5:{md5}", "limit": "1", "v2": "true", "mode": "extended"}
    )
    payload = e621_request(f"{E621_BASE}/posts.json?{params}", username, api_key)
    for post in _e621_posts(payload):
        if str(e621_file_info(post).get("md5") or "").casefold() == md5.casefold():
            return post
    return None


def e621_video_posts_page(
    username: str,
    api_key: str,
    before_id: Optional[int] = None,
    limit: int = E621_SOURCE_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """Fetch one source-first page of current e621 WebM/MP4 posts."""
    merged: Dict[str, Dict[str, Any]] = {}
    limit = max(1, min(320, int(limit)))

    for ext in ("webm", "mp4"):
        params: Dict[str, str] = {
            "tags": f"type:{ext}",
            "limit": str(limit),
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
        for post in _e621_posts(payload):
            post_id = str(post.get("id") or "")
            if post_id and e621_file_info(post).get("ext") in {"webm", "mp4"}:
                merged[post_id] = post

    return sorted(
        merged.values(),
        key=lambda post: as_int(post.get("id"), 0),
        reverse=True,
    )


def _multipart(boundary: str, fields: Dict[str, str], data: bytes, filename: str) -> bytes:
    chunks: List[bytes] = []
    for key, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8")
        + data
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks)


def e621_eris_candidates(
    frame: bytes,
    username: str,
    api_key: str,
) -> List[Tuple[float, str]]:
    """Authenticated ERIS frame lookup with a run-level Cloudflare circuit breaker.

    ERIS is only a discovery fallback. Normal e621 post/MD5 API requests remain
    enabled even when reverse-image frame uploads are blocked by Cloudflare.
    """
    global _LAST_E621_ERIS, _ERIS_DISABLED_FOR_RUN
    if not (username and api_key) or _ERIS_DISABLED_FOR_RUN:
        return []

    _LAST_E621_ERIS = _wait(_LAST_E621_ERIS, 3.0)
    boundary = "----BooruVideoERISBoundary7MA4YWxkTrZu0gW"
    body = _multipart(
        boundary,
        {"score_cutoff": f"{E621_ERIS_DISCOVERY_SCORE:.1f}"},
        frame,
        "stash-video-frame.jpg",
    )
    headers = e621_headers(username, api_key)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    try:
        payload = _json_request(
            f"{E621_BASE}/iqdb_queries.json?v2=true",
            headers=headers,
            data=body,
            timeout=60,
        )
    except RuntimeError as exc:
        detail = str(exc)
        lowered = detail.casefold()
        if (
            "http 429" in lowered
            or "just a moment" in lowered
            or "cloudflare" in lowered
            or "cf-chl-" in lowered
        ):
            _ERIS_DISABLED_FOR_RUN = (
                "e621 ERIS returned an HTTP 429 / Cloudflare challenge"
            )
            log(
                "WARNING",
                "e621 ERIS frame search received HTTP 429/Cloudflare; "
                "disabling ERIS frame uploads for the rest of this run. "
                "SauceNAO and normal e621 API lookups will continue.",
            )
            return []
        raise

    rows = payload if isinstance(payload, list) else (
        payload.get("results") if isinstance(payload, dict) else None
    )
    if not isinstance(rows, list):
        return []
    out: List[Tuple[float, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            score = float(row.get("score") or row.get("similarity") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if 0 < score <= 1:
            score *= 100.0
        post_id = row.get("post_id")
        if post_id is None and isinstance(row.get("post"), dict):
            post_id = row["post"].get("id")
        if post_id and score >= E621_ERIS_DISCOVERY_SCORE:
            out.append((score, str(post_id)))
    return out


def _normalize_rule34_posts(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("post", "posts"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                return [value]
        if payload.get("id"):
            return [payload]
    return []


def rule34_request(params: Dict[str, str], api_key: str, user_id: str) -> Any:
    global _LAST_RULE34
    _LAST_RULE34 = _wait(_LAST_RULE34, 1.0)
    query = {
        "page": "dapi",
        "s": "post",
        "q": "index",
        "json": "1",
        "api_key": api_key,
        "user_id": user_id,
        **params,
    }
    return _json_request(
        f"{RULE34_API}?{urllib.parse.urlencode(query)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def rule34_post_by_id(post_id: str, api_key: str, user_id: str) -> Optional[Dict[str, Any]]:
    rows = _normalize_rule34_posts(
        rule34_request({"id": str(post_id), "limit": "1"}, api_key, user_id)
    )
    return rows[0] if rows else None


def rule34_post_by_md5(md5: str, api_key: str, user_id: str) -> Optional[Dict[str, Any]]:
    rows = _normalize_rule34_posts(
        rule34_request({"tags": f"md5:{md5}", "limit": "1"}, api_key, user_id)
    )
    for post in rows:
        if str(post.get("hash") or post.get("md5") or "").casefold() == md5.casefold():
            return post
    return rows[0] if rows else None


def _rule34_tag_types(
    names: Sequence[str],
    api_key: str,
    user_id: str,
) -> Dict[str, int]:
    """Best-effort category enrichment without one network request per tag."""
    global _LAST_RULE34
    result: Dict[str, int] = {}
    names = [str(x) for x in names if str(x).strip()]
    for start in range(0, len(names), 50):
        chunk = names[start:start + 50]
        _LAST_RULE34 = _wait(_LAST_RULE34, 1.0)
        query = {
            "page": "dapi",
            "s": "tag",
            "q": "index",
            "json": "1",
            "names": " ".join(chunk),
            "limit": "100",
            "api_key": api_key,
            "user_id": user_id,
        }
        try:
            payload = _json_request(
                f"{RULE34_API}?{urllib.parse.urlencode(query)}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except Exception:
            continue
        rows = payload if isinstance(payload, list) else (
            payload.get("tag") if isinstance(payload, dict) else None
        )
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            rows = []

        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            try:
                result[name] = int(row.get("type"))
                seen.add(name.casefold())
            except (TypeError, ValueError):
                continue

        # Rule34's DAPI does not consistently honor the multi-name parameter.
        # Fall back to a bounded number of single-tag lookups so one post cannot
        # fan out into an unbounded request storm.
        missing = [name for name in chunk if name.casefold() not in seen]
        for name in missing[:20]:
            _LAST_RULE34 = _wait(_LAST_RULE34, 1.0)
            single_query = {
                "page": "dapi",
                "s": "tag",
                "q": "index",
                "json": "1",
                "name": name,
                "limit": "1",
                "api_key": api_key,
                "user_id": user_id,
            }
            try:
                single_payload = _json_request(
                    f"{RULE34_API}?{urllib.parse.urlencode(single_query)}",
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
            except Exception:
                break
            single_rows = (
                single_payload
                if isinstance(single_payload, list)
                else single_payload.get("tag")
                if isinstance(single_payload, dict)
                else None
            )
            if isinstance(single_rows, dict):
                single_rows = [single_rows]
            if not isinstance(single_rows, list):
                continue
            for row in single_rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("name") or "").casefold() != name.casefold():
                    continue
                try:
                    result[name] = int(row.get("type"))
                except (TypeError, ValueError):
                    pass
                break
    return result


def _saucenao_wait(settings: Dict[str, Any]) -> None:
    global _LAST_SAUCENAO
    requested = as_float(settings.get("saucenao_requests_per_30_seconds"), 0.0)
    effective = requested if requested > 0 else max(1.0, _SAUCENAO_SHORT_LIMIT)
    _LAST_SAUCENAO = _wait(_LAST_SAUCENAO, 30.0 / effective)


def saucenao_candidates(
    frame: bytes,
    settings: Dict[str, Any],
) -> List[Tuple[float, str, str]]:
    """Return only e621/Rule34 source IDs; SauceNAO is discovery, never metadata authority."""
    global _SAUCENAO_SHORT_LIMIT
    api_key = str(settings.get("saucenao_api_key") or "").strip()
    if not api_key:
        return []
    _saucenao_wait(settings)
    boundary = "----BooruVideoSauceBoundary7MA4YWxkTrZu0gW"
    body = _multipart(
        boundary,
        {"api_key": api_key, "output_type": "2", "numres": "12", "db": "999"},
        frame,
        "stash-video-frame.jpg",
    )
    payload = _json_request(
        SAUCENAO_API,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        data=body,
        timeout=60,
    )
    if isinstance(payload, dict):
        header = payload.get("header") or {}
        try:
            limit = float(header.get("short_limit") or 0)
        except (TypeError, ValueError):
            limit = 0.0
        if limit > 0:
            _SAUCENAO_SHORT_LIMIT = limit

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []

    out: List[Tuple[float, str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            score = round(float((item.get("header") or {}).get("similarity") or 0), 1)
        except (TypeError, ValueError):
            score = 0.0
        if score < SAUCENAO_DISCOVERY_SCORE:
            continue
        data = item.get("data") or {}
        if not isinstance(data, dict):
            continue
        if data.get("e621_id"):
            out.append((score, "e621", str(data["e621_id"])))
        urls = data.get("ext_urls") or []
        if isinstance(urls, str):
            urls = [urls]
        for url in urls:
            text = str(url)
            m = re.search(r"e621\.net/posts/(\d+)", text)
            if m:
                out.append((score, "e621", m.group(1)))
            m = re.search(r"rule34\.xxx/.*[?&]id=(\d+)", text)
            if m:
                out.append((score, "rule34", m.group(1)))
    return out


def media_url(source: str, post: Dict[str, Any]) -> Optional[str]:
    if source == "e621":
        info = e621_file_info(post)
        ext = str(info.get("ext") or "").casefold().lstrip(".")
        url = str(info.get("url") or "").strip()
        return url if ext in VIDEO_EXTENSIONS and url else None
    url = str(post.get("file_url") or "").strip()
    if not url:
        return None
    path = urllib.parse.urlparse(url).path
    ext = path.rsplit(".", 1)[-1].casefold() if "." in path else ""
    return url if ext in VIDEO_EXTENSIONS else None


def canonical_post_url(source: str, post: Dict[str, Any]) -> str:
    post_id = str(post.get("id") or "")
    if source == "e621":
        return f"https://e621.net/posts/{post_id}"
    return f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}"


def _date_only(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("s") or value.get("date") or value.get("created_at")
    text = str(value or "").strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return None


def post_metadata(source: str, post: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    tags: List[str] = []
    artists: List[str] = []
    characters: List[str] = []
    if source == "e621":
        typed = post.get("tags") or {}
        if isinstance(typed, dict):
            artists = [str(x).strip() for x in typed.get("artist") or [] if str(x).strip()]
            characters = [str(x).strip() for x in typed.get("character") or [] if str(x).strip()]
            for category in ("general", "species", "copyright", "lore"):
                tags.extend(str(x).strip() for x in typed.get(category) or [] if str(x).strip())
        date = _date_only(post.get("created_at"))
        raw_sources = post.get("sources") or []
        if isinstance(raw_sources, str):
            raw_sources = [raw_sources]
    else:
        flat = [x for x in str(post.get("tags") or "").split() if x]
        api_key = str(settings.get("rule34_api_key") or "")
        user_id = str(settings.get("rule34_user_id") or "")
        typed = _rule34_tag_types(flat, api_key, user_id) if api_key and user_id else {}
        artists = [name for name in flat if typed.get(name) == 1]
        characters = [name for name in flat if typed.get(name) == 4]
        tags = [name for name in flat if typed.get(name) not in {1, 4}]
        date = _date_only(post.get("created_at"))
        raw_sources = re.split(r"\s+", str(post.get("source") or "").strip()) if post.get("source") else []

    urls = [canonical_post_url(source, post)]
    for item in raw_sources:
        url = str(item or "").strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
        if len(urls) >= 6:
            break

    # Additional artists remain visible as tags when Stash can only hold one studio.
    if len(artists) > 1:
        tags.extend(artists[1:])

    return {
        "tags": list(dict.fromkeys(tags)),
        "artists": list(dict.fromkeys(artists)),
        "characters": list(dict.fromkeys(characters)),
        "date": date,
        "urls": urls,
    }


def configured_stash_tag_scope(settings: Dict[str, Any]) -> str:
    value = settings.get("stash_tag_scope")
    if value in (None, ""):
        value = settings.get("stash_source_tag_filter")
    return " ".join(str(value or "").split())


def scene_has_tag_id(scene: Dict[str, Any], tag_id: Optional[str]) -> bool:
    if not tag_id:
        return True
    return any(
        str(tag.get("id") or "") == str(tag_id)
        for tag in scene.get("tags") or []
    )


def skip_organized_enabled(settings: Dict[str, Any]) -> bool:
    return as_bool(settings.get("skip_organized_scenes"), False)


def protected_organized_scene(scene: Dict[str, Any], settings: Dict[str, Any]) -> bool:
    return skip_organized_enabled(settings) and bool(scene.get("organized"))


def _status_names(scene: Dict[str, Any]) -> set[str]:
    return {
        str(tag.get("name") or "").casefold()
        for tag in scene.get("tags") or []
        if str(tag.get("name") or "") in STATUS_NAMES
    }


def ensure_tag(stash: Stash, name: str, cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    key = name.casefold()
    if key in cache:
        return cache[key]
    found = stash.find_tag_by_name(name)
    if not found:
        found = stash.create_tag(name)
    cache[key] = found
    return found


def ensure_entity(
    stash: Stash,
    name: str,
    cache: Dict[str, Dict[str, Any]],
    kind: str,
) -> Dict[str, Any]:
    key = name.casefold().strip()
    if key in cache:
        return cache[key]
    if kind == "studio":
        obj = stash.create_studio(name)
    else:
        obj = stash.create_performer(name)
    cache[key] = obj
    return obj


def transition_status(
    stash: Stash,
    scene: Dict[str, Any],
    status: str,
    tag_cache: Dict[str, Dict[str, Any]],
    extra_url: Optional[str] = None,
) -> None:
    marker = ensure_tag(stash, status, tag_cache)
    keep = [
        str(tag.get("id"))
        for tag in scene.get("tags") or []
        if str(tag.get("name") or "") not in STATUS_NAMES
    ]
    keep.append(str(marker["id"]))
    urls = list(scene.get("urls") or [])
    if extra_url and extra_url not in urls:
        urls.append(extra_url)
    stash.update_scene(str(scene["id"]), tag_ids=list(dict.fromkeys(keep)), urls=urls)
    scene["tags"] = [
        tag for tag in scene.get("tags") or []
        if str(tag.get("name") or "") not in STATUS_NAMES
    ] + [{"id": marker["id"], "name": status}]
    scene["urls"] = urls


def apply_metadata(
    stash: Stash,
    scene: Dict[str, Any],
    source: str,
    post: Dict[str, Any],
    settings: Dict[str, Any],
    tag_cache: Dict[str, Dict[str, Any]],
    performer_cache: Dict[str, Dict[str, Any]],
    studio_cache: Dict[str, Dict[str, Any]],
    dry_run: bool,
) -> None:
    if protected_organized_scene(scene, settings):
        log(
            "INFO",
            f"Scene {scene.get('id')}: Organized protection is enabled; metadata unchanged",
        )
        return

    metadata = post_metadata(source, post, settings)

    existing_tag_ids = [
        str(tag.get("id"))
        for tag in scene.get("tags") or []
        if str(tag.get("name") or "") not in STATUS_NAMES
    ]
    for name in metadata["tags"]:
        tag = ensure_tag(stash, name, tag_cache) if not dry_run else tag_cache.get(name.casefold())
        if tag and str(tag.get("id")) not in existing_tag_ids:
            existing_tag_ids.append(str(tag["id"]))

    marker = ensure_tag(stash, STATUS_IMPORTED, tag_cache) if not dry_run else None
    if marker:
        existing_tag_ids.append(str(marker["id"]))

    performer_ids = [str(x.get("id")) for x in scene.get("performers") or []]
    for name in metadata["characters"]:
        obj = performer_cache.get(name.casefold())
        if not obj and not dry_run:
            obj = ensure_entity(stash, name, performer_cache, "performer")
        if obj and str(obj.get("id")) not in performer_ids:
            performer_ids.append(str(obj["id"]))

    studio_id: Optional[str] = None
    if metadata["artists"] and not scene.get("studio"):
        name = metadata["artists"][0]
        obj = studio_cache.get(name.casefold())
        if not obj and not dry_run:
            obj = ensure_entity(stash, name, studio_cache, "studio")
        if obj:
            studio_id = str(obj["id"])

    urls = list(scene.get("urls") or [])
    for url in metadata["urls"]:
        if url not in urls:
            urls.append(url)

    date = None if scene.get("date") else metadata.get("date")

    if dry_run:
        return

    stash.update_scene(
        str(scene["id"]),
        tag_ids=list(dict.fromkeys(existing_tag_ids)),
        performer_ids=list(dict.fromkeys(performer_ids)),
        studio_id=studio_id,
        date=date,
        urls=urls,
    )


def fetch_candidate(
    source: str,
    post_id: str,
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if source == "e621":
        return e621_post_by_id(
            post_id,
            str(settings.get("e621_username") or ""),
            str(settings.get("e621_api_key") or ""),
        )
    api_key = str(settings.get("rule34_api_key") or "")
    user_id = str(settings.get("rule34_user_id") or "")
    if not (api_key and user_id):
        raise RuntimeError("Rule34 user ID and API key are required to resolve this candidate")
    return rule34_post_by_id(post_id, api_key, user_id)


def exact_candidates(md5: str, settings: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    try:
        post = e621_post_by_md5(
            md5,
            str(settings.get("e621_username") or ""),
            str(settings.get("e621_api_key") or ""),
        )
        if post and media_url("e621", post):
            out.append(("e621", post))
    except Exception as exc:
        log("WARNING", f"e621 exact video lookup failed: {exc}")

    api_key = str(settings.get("rule34_api_key") or "")
    user_id = str(settings.get("rule34_user_id") or "")
    if api_key and user_id:
        try:
            post = rule34_post_by_md5(md5, api_key, user_id)
            if post and media_url("rule34", post):
                out.append(("rule34", post))
        except Exception as exc:
            log("WARNING", f"Rule34 exact video lookup failed: {exc}")
    return out


def discover_candidates(
    local_path: str,
    duration: float,
    ffmpeg_path: str,
    settings: Dict[str, Any],
) -> Tuple[List[Tuple[str, str, int, float]], bool]:
    found: Dict[Tuple[str, str], Dict[str, float]] = {}
    had_error = False
    username = str(settings.get("e621_username") or "")
    api_key = str(settings.get("e621_api_key") or "")
    have_sauce = bool(str(settings.get("saucenao_api_key") or "").strip())
    have_eris = bool(username and api_key)

    if not have_sauce and not have_eris:
        return [], True

    eris_checked = False
    for ratio in DISCOVERY_RATIOS:
        try:
            frame = extract_jpeg_frame(local_path, duration, ratio, ffmpeg_path, timeout=45)
        except Exception as exc:
            log("WARNING", f"Could not extract discovery frame at {ratio:.0%}: {exc}")
            had_error = True
            continue

        frame_hits: Dict[Tuple[str, str], float] = {}
        if have_sauce:
            try:
                for score, source, post_id in saucenao_candidates(frame, settings):
                    key = (source, post_id)
                    frame_hits[key] = max(frame_hits.get(key, 0.0), float(score))
            except Exception as exc:
                log("WARNING", f"SauceNAO video-frame lookup failed: {exc}")
                had_error = True

        # SauceNAO is the primary multi-frame locator. ERIS gets at most one
        # representative frame per video so heavy reverse-image uploads cannot
        # hammer e621/Cloudflare.
        sauce_found_e621 = any(source == "e621" for source, _post_id in frame_hits)
        representative = abs(float(ratio) - 0.52) < 0.001
        if (
            have_eris
            and not eris_checked
            and representative
            and not _ERIS_DISABLED_FOR_RUN
            and not sauce_found_e621
        ):
            eris_checked = True
            try:
                for score, post_id in e621_eris_candidates(frame, username, api_key):
                    key = ("e621", post_id)
                    frame_hits[key] = max(frame_hits.get(key, 0.0), float(score))
            except Exception as exc:
                log("WARNING", f"e621 ERIS video-frame lookup failed: {exc}")
                if not have_sauce:
                    had_error = True

        for key, score in frame_hits.items():
            state = found.setdefault(key, {"hits": 0.0, "score": 0.0})
            state["hits"] += 1
            state["score"] = max(state["score"], score)

    ranked = [
        (source, post_id, int(state["hits"]), float(state["score"]))
        for (source, post_id), state in found.items()
    ]
    ranked.sort(key=lambda item: (item[2], item[3]), reverse=True)

    # If ERIS was the only configured frame locator and Cloudflare blocked it,
    # do not write a permanent No Match marker from an incomplete search.
    if _ERIS_DISABLED_FOR_RUN and not have_sauce:
        had_error = True

    return ranked[:MAX_CANDIDATES], had_error



def resolve_stash_tag_filter(
    stash: Stash,
    tag_name: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a Stash tag name or alias to its canonical tag ID/name."""
    tag_name = " ".join(str(tag_name or "").split())
    if not tag_name:
        return None, None

    tags = stash.all_tags()
    tag = tags.get(tag_name.casefold())
    if not tag:
        return None, None
    return str(tag.get("id") or ""), str(tag.get("name") or tag_name)


def build_local_source_index(
    stash: Stash,
    settings: Dict[str, Any],
    *,
    force: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Build or refresh the cached early-frame index for eligible local scenes."""
    ffmpeg_path = stash.ffmpeg_path()
    stash_tag_filter = configured_stash_tag_scope(settings)
    filter_tag_id: Optional[str] = None
    filter_tag_name: Optional[str] = None
    if stash_tag_filter:
        filter_tag_id, filter_tag_name = resolve_stash_tag_filter(
            stash,
            stash_tag_filter,
        )
        if not filter_tag_id:
            raise RuntimeError(
                f"Stash tag filter not found: {stash_tag_filter}"
            )
        log(
            "INFO",
            f"Local source-first scope: Stash tag '{filter_tag_name}'",
        )

    cache = load_cache()
    cached = cache.setdefault("scenes", {})
    if not isinstance(cached, dict):
        cached = {}
        cache["scenes"] = cached

    rows: List[Dict[str, Any]] = []
    active_ids: set[str] = set()
    stats = {
        "scenes_seen": 0,
        "indexed": 0,
        "reused": 0,
        "skipped": 0,
        "skipped_organized": 0,
        "errors": 0,
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
            sid = str(scene.get("id") or "")
            if not sid:
                stats["skipped"] += 1
                continue
            if protected_organized_scene(scene, settings):
                stats["skipped_organized"] += 1
                continue
            if STATUS_IMPORTED.casefold() in _status_names(scene):
                stats["skipped"] += 1
                continue
            if any("e621.net/posts/" in str(url) for url in scene.get("urls") or []):
                stats["skipped"] += 1
                continue

            video = primary_video(scene)
            if not video:
                stats["skipped"] += 1
                continue
            local_path = str(video.get("path") or "").strip()
            if not local_path:
                stats["skipped"] += 1
                continue

            signature = scene_signature(scene)
            duration = as_float(video.get("duration"), 0.0)
            entry = cached.get(sid) if isinstance(cached.get(sid), dict) else None
            hashes: List[int] = []

            if (
                not force
                and entry
                and str(entry.get("signature") or "") == signature
                and isinstance(entry.get("hashes"), list)
                and entry.get("hashes")
            ):
                try:
                    hashes = [int(value) for value in entry["hashes"]]
                    duration = as_float(entry.get("duration"), duration)
                    stats["reused"] += 1
                except (TypeError, ValueError):
                    hashes = []

            if not hashes:
                try:
                    if duration <= 0:
                        duration = probe_duration(local_path, ffmpeg_path, 30)
                    hashes = early_frame_hashes(
                        local_path,
                        duration,
                        ffmpeg_path=ffmpeg_path,
                        timeout=45,
                    )
                    cached[sid] = {
                        "signature": signature,
                        "duration": round(float(duration), 3),
                        "path": local_path,
                        "hashes": [int(value) for value in hashes],
                    }
                    stats["indexed"] += 1
                except Exception as exc:
                    stats["errors"] += 1
                    log("WARNING", f"Scene {sid}: could not build early-frame index: {exc}")
                    continue

            active_ids.add(sid)
            rows.append(
                {
                    "scene": scene,
                    "scene_id": sid,
                    "path": local_path,
                    "duration": float(duration),
                    "hashes": hashes,
                }
            )

        if count:
            progress(min(1.0, (page * per_page) / max(1, count)))
        if page * per_page >= count:
            break
        page += 1

    # Remove stale cache entries only after a complete scan. Protected/imported
    # scenes are intentionally omitted so toggling protection later simply rebuilds them.
    cache["scenes"] = {
        sid: cached[sid]
        for sid in active_ids
        if sid in cached and isinstance(cached[sid], dict)
    }
    try:
        save_cache(cache)
    except OSError as exc:
        log("WARNING", f"Could not save local video hash index: {exc}")

    log(
        "INFO",
        "Local source-first index: "
        f"{len(rows)} eligible"
        + (f" with Stash tag '{filter_tag_name}'" if filter_tag_name else "")
        + f"; {stats['indexed']} built; {stats['reused']} reused; "
        f"{stats['skipped_organized']} organized protected; {stats['errors']} errors",
    )
    return rows, stats


def _duration_delta(local_duration: float, remote_duration: float) -> float:
    local_duration = max(0.0, float(local_duration))
    remote_duration = max(0.0, float(remote_duration))
    if local_duration <= 0 or remote_duration <= 0:
        return 0.0
    return abs(local_duration - remote_duration) / max(local_duration, remote_duration)


def duration_milliseconds(value: Any) -> int:
    """Normalize a video duration to the nearest millisecond."""
    seconds = as_float(value, 0.0)
    if seconds <= 0:
        return 0
    return int(round(seconds * 1000.0))


def build_duration_buckets(
    local_index: Sequence[Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    """Group local Stash videos by exact normalized millisecond duration."""
    buckets: Dict[int, List[Dict[str, Any]]] = {}
    for entry in local_index:
        key = duration_milliseconds(entry.get("duration"))
        if key <= 0:
            continue
        buckets.setdefault(key, []).append(entry)
    return buckets


def source_first_duration_candidates(
    duration_buckets: Dict[int, List[Dict[str, Any]]],
    remote_duration: float,
) -> List[Dict[str, Any]]:
    """Return only local scenes with the exact same normalized duration."""
    key = duration_milliseconds(remote_duration)
    if key <= 0:
        return []
    return list(duration_buckets.get(key) or [])


def _source_cursor_scope(
    stash: Stash,
    settings: Dict[str, Any],
) -> Tuple[str, str]:
    scope_name = configured_stash_tag_scope(settings)
    if not scope_name:
        return "__all__", "all eligible Stash videos"
    tag_id, canonical = resolve_stash_tag_filter(stash, scope_name)
    if not tag_id:
        raise RuntimeError(f"Stash tag scope not found: {scope_name}")
    return f"tag:{tag_id}", canonical or scope_name


def _load_source_cursor(scope_key: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    state = load_scan_state()
    scopes = state.setdefault("scopes", {})
    if not isinstance(scopes, dict):
        scopes = {}
        state["scopes"] = scopes
    scope_state = scopes.get(scope_key)
    if not isinstance(scope_state, dict):
        scope_state = {}
        scopes[scope_key] = scope_state
    return state, scope_state


def _save_source_cursor(
    state: Dict[str, Any],
    scope_key: str,
    *,
    before_id: Optional[int],
    exhausted: bool,
) -> None:
    scopes = state.setdefault("scopes", {})
    scopes[scope_key] = {
        "before_id": int(before_id) if before_id else None,
        "exhausted": bool(exhausted),
        "updated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    save_scan_state(state)


def reset_source_cursor(
    stash: Stash,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    scope_key, scope_label = _source_cursor_scope(stash, settings)
    state = load_scan_state()
    scopes = state.setdefault("scopes", {})
    existed = scope_key in scopes
    scopes.pop(scope_key, None)
    save_scan_state(state)
    log("INFO", f"Reset e621 source-first cursor for {scope_label}")
    return {"cursor_reset": 1 if existed else 0, "scope": scope_label}



def source_first_local_candidates(
    local_index: Sequence[Dict[str, Any]],
    remote_hashes: Sequence[int],
    remote_duration: float,
) -> List[Tuple[Tuple[float, float, float], Dict[str, Any], Dict[str, object]]]:
    ranked: List[
        Tuple[Tuple[float, float, float], Dict[str, Any], Dict[str, object]]
    ] = []

    for entry in local_index:
        early = early_hash_candidate(
            entry.get("hashes") or [],
            remote_hashes,
            max_distance=E621_SOURCE_EARLY_DISTANCE,
        )
        if not early.get("candidate"):
            continue

        duration_delta = _duration_delta(
            as_float(entry.get("duration"), 0.0),
            remote_duration,
        )

        rank = (
            float(early.get("matched") or 0),
            -float(early.get("median_distance") or 64.0),
            -duration_delta,
        )
        ranked.append((rank, entry, early))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[:E621_SOURCE_MAX_CANDIDATES]


def source_first_e621(
    stash: Stash,
    settings: Dict[str, Any],
    args: Dict[str, Any],
) -> Dict[str, int]:
    """Scan e621 videos outward and compare their early frames to local Stash."""
    dry_run = as_bool(args.get("dry_run"), False)
    force_index = as_bool(args.get("force_index"), False)
    post_limit = max(0, as_int(args.get("source_post_limit"), 0))
    configured_pages = as_int(settings.get("e621_source_pages"), 5)
    source_pages = max(
        1,
        min(100, as_int(args.get("source_pages"), configured_pages or 5)),
    )
    username = str(settings.get("e621_username") or "")
    api_key = str(settings.get("e621_api_key") or "")
    ffmpeg_path = stash.ffmpeg_path()

    local_index, index_stats = build_local_source_index(
        stash,
        settings,
        force=force_index,
    )
    duration_buckets = build_duration_buckets(local_index)
    scope_key, scope_label = _source_cursor_scope(stash, settings)
    scan_state, cursor_state = _load_source_cursor(scope_key)
    cursor_before_id = as_int(cursor_state.get("before_id"), 0) or None
    cursor_exhausted = as_bool(cursor_state.get("exhausted"), False)

    tag_cache = stash.all_tags()
    performer_cache = stash.all_performers()
    studio_cache = stash.all_studios()

    stats = {
        "posts_seen": 0,
        "video_posts": 0,
        "duration_candidates": 0,
        "duration_filtered_posts": 0,
        "duration_unknown_posts": 0,
        "early_candidates": 0,
        "verified_matches": 0,
        "high_confidence_review": 0,
        "review_candidates": 0,
        "provider_errors": 0,
        "local_index_size": len(local_index),
        "organized_protected": int(index_stats.get("skipped_organized") or 0),
        "cursor_start_before_id": cursor_before_id or 0,
        "cursor_end_before_id": cursor_before_id or 0,
        "cursor_exhausted": 1 if cursor_exhausted else 0,
    }
    if not local_index:
        log("INFO", "e621 source-first scan stopped: no eligible local scenes in index")
        return stats
    if cursor_exhausted and not dry_run:
        log(
            "INFO",
            f"e621 source-first history is already exhausted for {scope_label}; "
            "run Reset e621 Source-First Cursor to start over",
        )
        return stats

    if dry_run:
        log(
            "INFO",
            f"Previewing e621 source-first from cursor {cursor_before_id or 'newest'} "
            f"for {scope_label}; cursor will not advance",
        )
    else:
        log(
            "INFO",
            f"Continuing e621 source-first from {cursor_before_id or 'newest'} "
            f"for {scope_label}",
        )

    before_id: Optional[int] = cursor_before_id
    stop = False
    exhausted = False
    for page_number in range(source_pages):
        try:
            posts = e621_video_posts_page(
                username,
                api_key,
                before_id=before_id,
                limit=E621_SOURCE_PAGE_SIZE,
            )
        except Exception as exc:
            stats["provider_errors"] += 1
            log("WARNING", f"e621 source-first page lookup failed: {exc}")
            break

        if not posts:
            exhausted = True
            break

        ids = [as_int(post.get("id"), 0) for post in posts if as_int(post.get("id"), 0) > 0]
        if ids:
            before_id = min(ids)

        last_processed_id: Optional[int] = None
        for post in posts:
            if post_limit and stats["posts_seen"] >= post_limit:
                stop = True
                break

            post_id_int = as_int(post.get("id"), 0)
            if post_id_int > 0:
                last_processed_id = post_id_int
            stats["posts_seen"] += 1
            remote_url = media_url("e621", post)
            if not remote_url:
                continue
            stats["video_posts"] += 1

            info = e621_file_info(post)
            remote_duration = as_float(info.get("duration"), 0.0)
            remote_duration_ms = duration_milliseconds(remote_duration)
            if remote_duration_ms <= 0:
                stats["duration_unknown_posts"] += 1
                log(
                    "INFO",
                    f"e621 #{post.get('id')}: no duration metadata; "
                    "skipping without opening the video",
                )
                if post_limit:
                    progress(stats["posts_seen"] / max(1, post_limit))
                continue

            duration_pool = source_first_duration_candidates(
                duration_buckets,
                remote_duration,
            )
            if not duration_pool:
                stats["duration_filtered_posts"] += 1
                log(
                    "INFO",
                    f"e621 #{post.get('id')}: duration {remote_duration_ms} ms "
                    "has no exact local Stash duration match; skipping before frame scan",
                )
                if post_limit:
                    progress(stats["posts_seen"] / max(1, post_limit))
                continue

            stats["duration_candidates"] += len(duration_pool)
            try:
                remote_early = early_frame_hashes(
                    remote_url,
                    remote_duration,
                    ffmpeg_path=ffmpeg_path,
                    timeout=60,
                )
            except Exception as exc:
                stats["provider_errors"] += 1
                log(
                    "WARNING",
                    f"e621 post {post.get('id')}: early-frame extraction failed: {exc}",
                )
                continue

            candidates = source_first_local_candidates(
                duration_pool,
                remote_early,
                remote_duration,
            )
            if not candidates:
                if post_limit:
                    progress(stats["posts_seen"] / max(1, post_limit))
                continue

            stats["early_candidates"] += len(candidates)

            try:
                remote_full_hashes = frame_hashes(
                    remote_url,
                    remote_duration,
                    ffmpeg_path=ffmpeg_path,
                    ratios=DEFAULT_RATIOS,
                    timeout=60,
                )
            except Exception as exc:
                stats["provider_errors"] += 1
                log(
                    "WARNING",
                    f"e621 post {post.get('id')}: full verification frames failed: {exc}",
                )
                continue

            best_review: Optional[
                Tuple[float, Dict[str, Any], Dict[str, object]]
            ] = None
            matched_entry: Optional[Dict[str, Any]] = None

            for _rank, entry, early in candidates:
                scene = entry["scene"]
                if protected_organized_scene(scene, settings):
                    continue

                try:
                    local_full_hashes = entry.get("_full_hashes")
                    if not isinstance(local_full_hashes, list) or not local_full_hashes:
                        local_full_hashes = frame_hashes(
                            str(entry["path"]),
                            float(entry["duration"]),
                            ffmpeg_path=ffmpeg_path,
                            ratios=DEFAULT_RATIOS,
                            timeout=45,
                        )
                        entry["_full_hashes"] = local_full_hashes

                    verification = verify_video_candidate(
                        str(entry["path"]),
                        remote_url,
                        ffmpeg_path=ffmpeg_path,
                        ratios=DEFAULT_RATIOS,
                        frame_distance=SOURCE_FIRST_VERIFY_FRAME_DISTANCE,
                        timeout=60,
                        local_duration=float(entry["duration"]),
                        local_hashes=local_full_hashes,
                        remote_duration=remote_duration,
                        remote_hashes=remote_full_hashes,
                        strict=True,
                    )
                except Exception as exc:
                    stats["provider_errors"] += 1
                    log(
                        "WARNING",
                        f"Scene {entry['scene_id']}: source-first verification failed "
                        f"against e621 #{post.get('id')}: {exc}",
                    )
                    continue

                matched_frames = int(verification.get("matched_frames") or 0)
                total_frames = int(verification.get("total_frames") or 0)
                median = float(verification.get("median_distance") or 64.0)
                log(
                    "INFO",
                    f"e621 #{post.get('id')} -> Scene {entry['scene_id']}: "
                    f"early distances {early.get('distances')}; aligned verification "
                    f"{matched_frames}/{total_frames}, median {median:.1f}, "
                    f"duration delta {float(verification.get('duration_delta') or 0.0):.1%}",
                )

                if verification.get("high"):
                    auto_import = as_bool(
                        settings.get("source_first_auto_import"),
                        False,
                    )
                    if dry_run:
                        stats["verified_matches"] += 1
                        log(
                            "INFO",
                            f"e621 source-first VERIFIED: post #{post.get('id')} -> "
                            f"Stash Scene {entry['scene_id']} (preview only)",
                        )
                    elif auto_import:
                        apply_metadata(
                            stash,
                            scene,
                            "e621",
                            post,
                            settings,
                            tag_cache,
                            performer_cache,
                            studio_cache,
                            False,
                        )
                        stats["verified_matches"] += 1
                        log(
                            "INFO",
                            f"e621 source-first MATCH: post #{post.get('id')} -> "
                            f"Stash Scene {entry['scene_id']}",
                        )
                    else:
                        transition_status(
                            stash,
                            scene,
                            STATUS_REVIEW,
                            tag_cache,
                            extra_url=canonical_post_url("e621", post),
                        )
                        stats["high_confidence_review"] += 1
                        log(
                            "INFO",
                            f"e621 source-first VERIFIED REVIEW: post #{post.get('id')} -> "
                            f"Stash Scene {entry['scene_id']} "
                            "(auto-import disabled)",
                        )
                    matched_entry = entry
                    break

                if verification.get("review"):
                    review_rank = matched_frames * 100.0 - median
                    if best_review is None or review_rank > best_review[0]:
                        best_review = (review_rank, entry, verification)

            if matched_entry is not None:
                # One local scene should not be repeatedly matched to multiple e621
                # posts during the same scan.
                local_index = [
                    entry
                    for entry in local_index
                    if entry.get("scene_id") != matched_entry.get("scene_id")
                ]
            elif best_review is not None:
                _, entry, verification = best_review
                stats["review_candidates"] += 1
                if not dry_run and not protected_organized_scene(entry["scene"], settings):
                    transition_status(
                        stash,
                        entry["scene"],
                        STATUS_REVIEW,
                        tag_cache,
                        extra_url=canonical_post_url("e621", post),
                    )
                log(
                    "INFO",
                    f"e621 source-first REVIEW: post #{post.get('id')} -> "
                    f"Scene {entry['scene_id']} "
                    f"({verification.get('matched_frames')}/{verification.get('total_frames')} frames)",
                )

            if post_limit:
                progress(stats["posts_seen"] / max(1, post_limit))

        if not dry_run and last_processed_id:
            before_id = last_processed_id
            _save_source_cursor(
                scan_state,
                scope_key,
                before_id=before_id,
                exhausted=False,
            )
            stats["cursor_end_before_id"] = before_id

        if stop:
            break
        if not ids:
            exhausted = True
            break
        if not post_limit:
            progress((page_number + 1) / source_pages)

    if exhausted and not dry_run:
        _save_source_cursor(
            scan_state,
            scope_key,
            before_id=before_id,
            exhausted=True,
        )
        stats["cursor_exhausted"] = 1
        log("INFO", f"e621 source-first history exhausted for {scope_label}")

    return stats


def build_source_index_task(
    stash: Stash,
    settings: Dict[str, Any],
    args: Dict[str, Any],
) -> Dict[str, int]:
    _rows, stats = build_local_source_index(
        stash,
        settings,
        force=as_bool(args.get("force_index"), False),
    )
    return stats


def process_scene(
    stash: Stash,
    scene: Dict[str, Any],
    settings: Dict[str, Any],
    *,
    deep: bool,
    dry_run: bool,
    tag_cache: Dict[str, Dict[str, Any]],
    performer_cache: Dict[str, Dict[str, Any]],
    studio_cache: Dict[str, Dict[str, Any]],
) -> str:
    sid = str(scene.get("id") or "")
    if protected_organized_scene(scene, settings):
        log("INFO", f"Scene {sid}: skipped because Stash marks it Organized")
        return "skipped_organized"

    video = primary_video(scene)
    if not video:
        return "skipped"
    local_path = str(video.get("path") or "").strip()
    if not local_path:
        return "skipped"

    md5 = fingerprint(scene, "md5")
    if md5:
        exact = exact_candidates(md5, settings)
        if exact:
            source, post = exact[0]
            log("INFO", f"Scene {sid}: exact video MD5 matched {source} #{post.get('id')}")
            if not dry_run:
                apply_metadata(
                    stash, scene, source, post, settings,
                    tag_cache, performer_cache, studio_cache, False,
                )
            return "imported"

    if not deep:
        log("INFO", f"Scene {sid}: no exact video match; queued for Deep Reverse Search")
        if not dry_run:
            transition_status(stash, scene, STATUS_UNRESOLVED, tag_cache)
        return "unresolved"

    ffmpeg_path = stash.ffmpeg_path()
    duration = as_float(video.get("duration"), 0.0)
    try:
        if duration <= 0:
            duration = probe_duration(local_path, ffmpeg_path, 30)
        local_hashes = frame_hashes(
            local_path, duration, ffmpeg_path, DEFAULT_RATIOS, timeout=45
        )
    except Exception as exc:
        log("WARNING", f"Scene {sid}: local video frame analysis failed: {exc}")
        if not dry_run:
            transition_status(stash, scene, STATUS_RETRY, tag_cache)
        return "retry_later"

    candidates, discovery_error = discover_candidates(
        local_path, duration, ffmpeg_path, settings
    )
    if not candidates:
        status = STATUS_RETRY if discovery_error else STATUS_NO_MATCH
        log("INFO", f"Scene {sid}: {'RETRY LATER' if discovery_error else 'NO MATCH'}; no video candidates found")
        if not dry_run:
            transition_status(stash, scene, status, tag_cache)
        return "retry_later" if discovery_error else "no_match"

    best_review: Optional[Tuple[float, str, str, Dict[str, Any], Dict[str, object]]] = None
    authoritative_checks = 0
    verification_errors = 0

    for source, post_id, hits, discovery_score in candidates:
        try:
            post = fetch_candidate(source, post_id, settings)
        except Exception as exc:
            log("WARNING", f"Scene {sid}: could not resolve {source} #{post_id}: {exc}")
            verification_errors += 1
            continue
        if not post:
            continue
        remote = media_url(source, post)
        if not remote:
            continue

        authoritative_checks += 1
        try:
            verification = verify_video_candidate(
                local_path,
                remote,
                ffmpeg_path=ffmpeg_path,
                ratios=DEFAULT_RATIOS,
                frame_distance=VERIFY_FRAME_DISTANCE,
                timeout=60,
                local_duration=duration,
                local_hashes=local_hashes,
            )
        except Exception as exc:
            log("WARNING", f"Scene {sid}: video verification failed for {source} #{post_id}: {exc}")
            verification_errors += 1
            continue

        matched_frames = int(verification.get("matched_frames") or 0)
        total_frames = int(verification.get("total_frames") or 0)
        median = float(verification.get("median_distance") or 64.0)
        log(
            "INFO",
            f"Scene {sid}: candidate {source} #{post_id} discovered on {hits} frame(s) "
            f"({discovery_score:.1f}% best); verification {matched_frames}/{total_frames} "
            f"frames, median distance {median:.1f}",
        )

        if verification.get("high"):
            log("INFO", f"Scene {sid}: MATCH {source} #{post_id}; importing authoritative metadata")
            if not dry_run:
                apply_metadata(
                    stash, scene, source, post, settings,
                    tag_cache, performer_cache, studio_cache, False,
                )
            return "imported"

        if verification.get("review"):
            rank = matched_frames * 100.0 - median
            candidate = (rank, source, post_id, post, verification)
            if best_review is None or candidate[0] > best_review[0]:
                best_review = candidate

    if best_review is not None:
        _, source, post_id, post, verification = best_review
        url = canonical_post_url(source, post)
        log(
            "INFO",
            f"Scene {sid}: REVIEW CANDIDATE {source} #{post_id}; "
            f"{verification.get('matched_frames')}/{verification.get('total_frames')} frames agree",
        )
        if not dry_run:
            transition_status(stash, scene, STATUS_REVIEW, tag_cache, extra_url=url)
        return "review_candidate"

    if verification_errors and authoritative_checks == 0:
        status = STATUS_RETRY
        result = "retry_later"
    else:
        status = STATUS_NO_MATCH
        result = "no_match"
    log("INFO", f"Scene {sid}: {'RETRY LATER' if result == 'retry_later' else 'NO MATCH'} after video verification")
    if not dry_run:
        transition_status(stash, scene, status, tag_cache)
    return result


def import_all(stash: Stash, settings: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, int]:
    deep = str(args.get("lookup_mode") or "fast").casefold() == "deep"
    dry_run = as_bool(args.get("dry_run"), False)
    only_review = as_bool(args.get("only_review"), False)
    only_no_match = as_bool(args.get("only_no_match"), False)
    limit = max(0, as_int(args.get("limit"), 0))

    tag_cache = stash.all_tags()
    performer_cache = stash.all_performers()
    studio_cache = stash.all_studios()

    scope_name = configured_stash_tag_scope(settings)
    scope_tag_id: Optional[str] = None
    scope_canonical_name: Optional[str] = None
    if scope_name:
        scope_tag = tag_cache.get(scope_name.casefold())
        if not scope_tag:
            raise RuntimeError(f"Stash tag scope not found: {scope_name}")
        scope_tag_id = str(scope_tag.get("id") or "")
        scope_canonical_name = str(scope_tag.get("name") or scope_name)
        log("INFO", f"Video tagger scope: Stash tag '{scope_canonical_name}'")

    target_tag_id: Optional[str] = None
    if only_review:
        tag = ensure_tag(stash, STATUS_REVIEW, tag_cache)
        target_tag_id = str(tag["id"])
    elif only_no_match:
        tag = ensure_tag(stash, STATUS_NO_MATCH, tag_cache)
        target_tag_id = str(tag["id"])
    elif deep:
        tag = ensure_tag(stash, STATUS_UNRESOLVED, tag_cache)
        target_tag_id = str(tag["id"])

    stats = {
        "seen": 0,
        "imported": 0,
        "unresolved": 0,
        "review_candidate": 0,
        "no_match": 0,
        "retry_later": 0,
        "skipped": 0,
        "skipped_organized": 0,
    }

    per_page = 100

    if target_tag_id:
        # Snapshot the queue before modifying statuses. This prevents page shifting
        # and prevents protected/no-match scenes that keep the same status from
        # being processed forever.
        queued: List[Dict[str, Any]] = []
        page = 1
        while True:
            count, scenes = stash.find_scenes(page, per_page, tag_id=target_tag_id)
            had_page_rows = bool(scenes)
            if scope_tag_id:
                scenes = [
                    scene for scene in scenes
                    if scene_has_tag_id(scene, scope_tag_id)
                ]
            queued.extend(scenes)
            if page * per_page >= count or not had_page_rows:
                break
            page += 1

        for scene in queued:
            if limit and stats["seen"] >= limit:
                break
            stats["seen"] += 1
            result = process_scene(
                stash,
                scene,
                settings,
                deep=deep,
                dry_run=dry_run,
                tag_cache=tag_cache,
                performer_cache=performer_cache,
                studio_cache=studio_cache,
            )
            stats[result] = stats.get(result, 0) + 1
            progress(stats["seen"] / max(1, min(len(queued), limit or len(queued))))
    else:
        page = 1
        stop = False
        while not stop:
            count, scenes = stash.find_scenes(
                page,
                per_page,
                tag_id=scope_tag_id,
            )
            if not scenes:
                break
            for scene in scenes:
                statuses = _status_names(scene)
                if statuses:
                    continue
                if limit and stats["seen"] >= limit:
                    stop = True
                    break
                stats["seen"] += 1
                result = process_scene(
                    stash,
                    scene,
                    settings,
                    deep=deep,
                    dry_run=dry_run,
                    tag_cache=tag_cache,
                    performer_cache=performer_cache,
                    studio_cache=studio_cache,
                )
                stats[result] = stats.get(result, 0) + 1
                if limit:
                    progress(stats["seen"] / max(1, limit))
                elif count:
                    progress(min(1.0, stats["seen"] / max(1, count)))
            if page * per_page >= count:
                break
            page += 1

    return stats


def main() -> None:
    payload = read_input()
    stash = Stash(payload.get("server_connection") or {})
    settings = stash.settings()
    args = payload.get("args") or {}
    mode = str(args.get("mode") or "import_all")
    if mode == "import_all":
        stats = import_all(stash, settings, args)
    elif mode == "build_source_index":
        stats = build_source_index_task(stash, settings, args)
    elif mode == "source_first_e621":
        stats = source_first_e621(stash, settings, args)
    elif mode == "reset_source_cursor":
        stats = reset_source_cursor(stash, settings)
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
