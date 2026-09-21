"""Persistent SQLite catalog of e621 video durations, URLs and MD5 hashes."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_CATALOG_PATH = os.path.join(
    os.path.dirname(__file__),
    "e621_video_catalog.sqlite",
)


class E621VideoCatalog:
    def __init__(self, path: str = DEFAULT_CATALOG_PATH):
        self.path = path
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    post_id INTEGER PRIMARY KEY,
                    ext TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    md5 TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_videos_duration
                ON videos(duration_ms)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_videos_md5
                ON videos(md5)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def get_cursor(self, ext: str) -> int:
        key = f"cursor:{str(ext).casefold()}"
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (key,),
            ).fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def set_cursor(self, ext: str, post_id: int) -> None:
        key = f"cursor:{str(ext).casefold()}"
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(max(0, int(post_id)))),
            )

    def upsert_videos(self, rows: Iterable[Dict[str, Any]]) -> int:
        prepared = []
        for row in rows:
            post_id = int(row.get("post_id") or 0)
            duration_ms = int(row.get("duration_ms") or 0)
            ext = str(row.get("ext") or "").casefold()
            url = str(row.get("url") or "").strip()
            if post_id <= 0 or duration_ms <= 0 or not ext or not url:
                continue
            prepared.append(
                (
                    post_id,
                    ext,
                    duration_ms,
                    url,
                    str(row.get("md5") or "").strip().casefold() or None,
                    str(row.get("updated_at") or ""),
                )
            )
        if not prepared:
            return 0
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO videos(
                    post_id, ext, duration_ms, url, md5, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    ext = excluded.ext,
                    duration_ms = excluded.duration_ms,
                    url = excluded.url,
                    md5 = excluded.md5,
                    updated_at = excluded.updated_at
                """,
                prepared,
            )
        return len(prepared)

    def candidates_for_duration(self, duration_ms: int) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT post_id, ext, duration_ms, url, md5
                FROM videos
                WHERE duration_ms = ?
                ORDER BY post_id DESC
                """,
                (int(duration_ms),),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_md5(self, md5: str) -> Optional[Dict[str, Any]]:
        md5 = str(md5 or "").strip().casefold()
        if not md5:
            return None
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT post_id, ext, duration_ms, url, md5
                FROM videos
                WHERE md5 = ?
                ORDER BY post_id DESC
                LIMIT 1
                """,
                (md5,),
            ).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM videos").fetchone()
        return int(row["count"] if row else 0)

    def duration_bucket_count(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT duration_ms) AS count FROM videos"
            ).fetchone()
        return int(row["count"] if row else 0)
