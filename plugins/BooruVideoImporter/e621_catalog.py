"""Persistent SQLite e621 video metadata and perceptual-frame catalog."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence

DEFAULT_CATALOG_PATH = os.path.join(
    os.path.dirname(__file__),
    "e621_video_catalog.sqlite",
)
HASH_VERSION = 1
HASH_RATIOS = (0.10, 0.50, 0.90)


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
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    url TEXT NOT NULL,
                    md5 TEXT,
                    updated_at TEXT,
                    phash_10 TEXT,
                    phash_50 TEXT,
                    phash_90 TEXT,
                    hash_version INTEGER NOT NULL DEFAULT 0,
                    hash_error TEXT,
                    hash_attempts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Migrate catalogs created by the early metadata-only implementation.
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(videos)").fetchall()
            }
            migrations = {
                "phash_10": "TEXT",
                "phash_50": "TEXT",
                "phash_90": "TEXT",
                "hash_version": "INTEGER NOT NULL DEFAULT 0",
                "hash_error": "TEXT",
                "hash_attempts": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE videos ADD COLUMN {name} {definition}"
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
                CREATE INDEX IF NOT EXISTS idx_videos_hash_status
                ON videos(hash_version, hash_attempts)
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

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (str(key),),
            ).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(key), str(value)),
            )

    def get_cursor(self, ext: str) -> int:
        try:
            return int(self.get_meta(f"cursor:{str(ext).casefold()}", "0"))
        except (TypeError, ValueError):
            return 0

    def set_cursor(self, ext: str, post_id: int) -> None:
        self.set_meta(
            f"cursor:{str(ext).casefold()}",
            max(0, int(post_id)),
        )

    def set_up_to_date(self, ext: str, value: bool) -> None:
        self.set_meta(
            f"up_to_date:{str(ext).casefold()}",
            "1" if value else "0",
        )

    def is_up_to_date(self, ext: str) -> bool:
        return self.get_meta(
            f"up_to_date:{str(ext).casefold()}",
            "0",
        ) == "1"

    def upsert_videos(self, rows: Iterable[Dict[str, Any]]) -> int:
        prepared = []
        for row in rows:
            post_id = int(row.get("post_id") or 0)
            duration_ms = max(0, int(row.get("duration_ms") or 0))
            ext = str(row.get("ext") or "").casefold()
            url = str(row.get("url") or "").strip()
            if post_id <= 0 or not ext or not url:
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
                    updated_at = excluded.updated_at,
                    phash_10 = CASE
                        WHEN videos.url = excluded.url
                         AND COALESCE(videos.md5, '') = COALESCE(excluded.md5, '')
                        THEN videos.phash_10 ELSE NULL END,
                    phash_50 = CASE
                        WHEN videos.url = excluded.url
                         AND COALESCE(videos.md5, '') = COALESCE(excluded.md5, '')
                        THEN videos.phash_50 ELSE NULL END,
                    phash_90 = CASE
                        WHEN videos.url = excluded.url
                         AND COALESCE(videos.md5, '') = COALESCE(excluded.md5, '')
                        THEN videos.phash_90 ELSE NULL END,
                    hash_version = CASE
                        WHEN videos.url = excluded.url
                         AND COALESCE(videos.md5, '') = COALESCE(excluded.md5, '')
                        THEN videos.hash_version ELSE 0 END,
                    hash_error = CASE
                        WHEN videos.url = excluded.url
                         AND COALESCE(videos.md5, '') = COALESCE(excluded.md5, '')
                        THEN videos.hash_error ELSE NULL END,
                    hash_attempts = CASE
                        WHEN videos.url = excluded.url
                         AND COALESCE(videos.md5, '') = COALESCE(excluded.md5, '')
                        THEN videos.hash_attempts ELSE 0 END
                """,
                prepared,
            )
        return len(prepared)

    def row(self, post_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM videos WHERE post_id = ?",
                (int(post_id),),
            ).fetchone()
        return dict(row) if row else None

    def set_duration(self, post_id: int, duration_ms: int) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE videos SET duration_ms = ? WHERE post_id = ?",
                (max(0, int(duration_ms)), int(post_id)),
            )

    def set_hashes(
        self,
        post_id: int,
        hashes: Sequence[str],
        hash_version: int = HASH_VERSION,
    ) -> None:
        values = list(hashes)
        if len(values) != 3:
            raise ValueError("Exactly three perceptual hashes are required")
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE videos
                SET phash_10 = ?,
                    phash_50 = ?,
                    phash_90 = ?,
                    hash_version = ?,
                    hash_error = NULL,
                    hash_attempts = hash_attempts + 1
                WHERE post_id = ?
                """,
                (
                    str(values[0]),
                    str(values[1]),
                    str(values[2]),
                    int(hash_version),
                    int(post_id),
                ),
            )

    def set_hash_error(self, post_id: int, error: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE videos
                SET hash_error = ?,
                    hash_attempts = hash_attempts + 1
                WHERE post_id = ?
                """,
                (str(error)[:500], int(post_id)),
            )

    def candidates_for_duration(
        self,
        duration_ms: int,
        require_hashes: bool = True,
    ) -> List[Dict[str, Any]]:
        where = "duration_ms = ?"
        params: List[Any] = [int(duration_ms)]
        if require_hashes:
            where += (
                " AND hash_version = ?"
                " AND phash_10 IS NOT NULL"
                " AND phash_50 IS NOT NULL"
                " AND phash_90 IS NOT NULL"
            )
            params.append(HASH_VERSION)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT post_id, ext, duration_ms, url, md5,
                       phash_10, phash_50, phash_90,
                       hash_version, hash_error, hash_attempts
                FROM videos
                WHERE {where}
                ORDER BY post_id DESC
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_md5(self, md5: str) -> Optional[Dict[str, Any]]:
        md5 = str(md5 or "").strip().casefold()
        if not md5:
            return None
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT post_id, ext, duration_ms, url, md5,
                       phash_10, phash_50, phash_90, hash_version
                FROM videos
                WHERE md5 = ?
                ORDER BY post_id DESC
                LIMIT 1
                """,
                (md5,),
            ).fetchone()
        return dict(row) if row else None

    def rows_needing_hash(
        self,
        limit: int = 100,
        max_attempts: int = 3,
    ) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT post_id, ext, duration_ms, url, md5,
                       hash_error, hash_attempts
                FROM videos
                WHERE (
                    hash_version != ?
                    OR phash_10 IS NULL
                    OR phash_50 IS NULL
                    OR phash_90 IS NULL
                )
                AND hash_attempts < ?
                ORDER BY post_id ASC
                LIMIT ?
                """,
                (HASH_VERSION, int(max_attempts), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM videos"
            ).fetchone()
        return int(row["count"] if row else 0)

    def hashed_count(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM videos
                WHERE hash_version = ?
                  AND phash_10 IS NOT NULL
                  AND phash_50 IS NOT NULL
                  AND phash_90 IS NOT NULL
                """,
                (HASH_VERSION,),
            ).fetchone()
        return int(row["count"] if row else 0)

    def failed_count(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM videos
                WHERE hash_error IS NOT NULL
                """
            ).fetchone()
        return int(row["count"] if row else 0)

    def duration_bucket_count(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT duration_ms) AS count
                FROM videos
                WHERE duration_ms > 0
                """
            ).fetchone()
        return int(row["count"] if row else 0)
