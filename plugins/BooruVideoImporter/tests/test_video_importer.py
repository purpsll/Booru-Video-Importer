import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock
import sys


PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import video_match

spec = importlib.util.spec_from_file_location(
    "booru_video_importer", PLUGIN_DIR / "BooruVideoImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)

catalog_spec = importlib.util.spec_from_file_location(
    "e621_catalog_test", PLUGIN_DIR / "e621_catalog.py"
)
catalog_module = importlib.util.module_from_spec(catalog_spec)
assert catalog_spec.loader is not None
catalog_spec.loader.exec_module(catalog_module)


class BooruVideoImporterTests(unittest.TestCase):
    def test_duration_normalizes_to_exact_milliseconds(self):
        self.assertEqual(plugin.duration_milliseconds(60.000), 60000)
        self.assertEqual(plugin.duration_milliseconds(59.999), 59999)
        self.assertEqual(plugin.duration_milliseconds(60.001), 60001)

    def test_e621_file_info_supports_current_schema(self):
        post = {
            "files": {
                "meta": {
                    "ext": "webm",
                    "duration": 12.345,
                    "md5": "ABCDEF",
                },
                "original": {"url": "https://example/video.webm"},
            }
        }
        info = plugin.e621_file_info(post)
        self.assertEqual(info["ext"], "webm")
        self.assertEqual(info["duration"], 12.345)
        self.assertEqual(info["md5"], "abcdef")
        self.assertEqual(info["url"], "https://example/video.webm")

    def test_e621_after_page_uses_ascending_cursor(self):
        payload = [{
            "id": 101,
            "file": {
                "ext": "webm",
                "url": "https://example/101.webm",
                "duration": 5.0,
            },
        }]
        with mock.patch.object(plugin, "e621_request", return_value=payload) as req:
            rows = plugin.e621_video_posts_after(
                "webm", "user", "key", after_id=100, limit=75
            )
        self.assertEqual([row["id"] for row in rows], [101])
        self.assertIn("page=a100", req.call_args.args[0])
        self.assertIn("type%3Awebm", req.call_args.args[0])

    def test_sqlite_catalog_stores_duration_timecodes_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(pathlib.Path(temp) / "catalog.sqlite")
            catalog = catalog_module.E621VideoCatalog(path)
            catalog.upsert_videos([{
                "post_id": 10,
                "ext": "webm",
                "duration_ms": 60000,
                "url": "https://example/10.webm",
                "md5": "abcdef",
            }])
            catalog.set_hashes(
                10,
                ["0000000000000001", "0000000000000002", "0000000000000003"],
                [6000, 30000, 54000],
            )
            row = catalog.candidates_for_duration(60000)[0]
            self.assertEqual(row["post_id"], 10)
            self.assertEqual(row["time_10_ms"], 6000)
            self.assertEqual(row["time_50_ms"], 30000)
            self.assertEqual(row["time_90_ms"], 54000)
            self.assertEqual(row["phash_50"], "0000000000000002")
            self.assertEqual(catalog.find_md5("ABCDEF")["post_id"], 10)

    def test_catalog_cursor_is_persistent_per_format(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(pathlib.Path(temp) / "catalog.sqlite")
            catalog = catalog_module.E621VideoCatalog(path)
            catalog.set_cursor("webm", 123)
            catalog.set_cursor("mp4", 456)
            reopened = catalog_module.E621VideoCatalog(path)
            self.assertEqual(reopened.get_cursor("webm"), 123)
            self.assertEqual(reopened.get_cursor("mp4"), 456)

    def test_catalog_hash_failure_is_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(pathlib.Path(temp) / "catalog.sqlite")
            catalog = catalog_module.E621VideoCatalog(path)
            catalog.upsert_videos([{
                "post_id": 10,
                "ext": "webm",
                "duration_ms": 60000,
                "url": "https://example/10.webm",
            }])
            catalog.set_hash_error(10, "temporary failure")
            pending = catalog.rows_needing_hash(limit=10, max_attempts=3)
            self.assertEqual([row["post_id"] for row in pending], [10])

    def test_catalog_candidate_score_requires_three_close_phashes(self):
        local = [0, 0, 0]
        good = {
            "phash_10": plugin.phash_hex(1),
            "phash_50": plugin.phash_hex(3),
            "phash_90": plugin.phash_hex(7),
        }
        score = plugin.catalog_candidate_score(local, good)
        self.assertIsNotNone(score)
        self.assertEqual(score[2], [1, 2, 3])

        bad = {
            "phash_10": plugin.phash_hex((1 << 20) - 1),
            "phash_50": plugin.phash_hex((1 << 20) - 1),
            "phash_90": plugin.phash_hex((1 << 20) - 1),
        }
        self.assertIsNone(plugin.catalog_candidate_score(local, bad))

    def test_dynamic_stash_scope_supports_aliases(self):
        stash = mock.Mock()
        stash.all_tags.return_value = {
            "furry": {"id": "77", "name": "Furry"},
            "anthro": {"id": "77", "name": "Furry"},
        }
        self.assertEqual(
            plugin.resolve_stash_tag_filter(stash, "anthro"),
            ("77", "Furry"),
        )

    def test_local_phashes_are_cached_lazily(self):
        scene = {
            "id": "10",
            "files": [{
                "id": "f10",
                "path": "/video.mp4",
                "duration": 60.0,
                "size": 100,
                "mod_time": "2026-09-21T00:00:00Z",
            }],
        }
        cache = {"version": 3, "scenes": {}}
        with mock.patch.object(
            plugin, "frame_phashes", return_value=[1, 2, 3]
        ) as frame_phashes:
            first = plugin._prepare_local_entry(scene, cache, "ffmpeg")
            second = plugin._prepare_local_entry(scene, cache, "ffmpeg")
        self.assertEqual(first["phashes"], [1, 2, 3])
        self.assertEqual(second["phashes"], [1, 2, 3])
        self.assertEqual(frame_phashes.call_count, 1)

    def test_catalog_builder_resumes_and_saves_three_hashes(self):
        stash = mock.Mock()
        stash.ffmpeg_path.return_value = "ffmpeg"
        fake_catalog = mock.Mock()
        fake_catalog.count.side_effect = [0, 1, 1]
        fake_catalog.upsert_videos.return_value = 1
        fake_catalog.hashed_count.return_value = 1
        fake_catalog.failed_count.return_value = 0
        fake_catalog.duration_bucket_count.return_value = 1
        fake_catalog.rows_needing_hash.side_effect = [
            [],
            [{
                "post_id": 101,
                "ext": "webm",
                "duration_ms": 60000,
                "url": "https://example/101.webm",
                "hash_attempts": 0,
            }],
        ]
        fake_catalog.get_cursor.side_effect = lambda ext: 100 if ext == "webm" else 0

        post = {
            "id": 101,
            "updated_at": "2026-09-21T00:00:00Z",
            "file": {
                "ext": "webm",
                "url": "https://example/101.webm",
                "md5": "abc",
                "duration": 60.0,
            },
        }

        with mock.patch.object(plugin, "E621VideoCatalog", return_value=fake_catalog),              mock.patch.object(
                 plugin,
                 "e621_video_posts_after",
                 side_effect=[[post], [], []],
             ),              mock.patch.object(plugin, "frame_phashes", return_value=[1, 2, 3]),              mock.patch.object(
                 plugin,
                 "frame_timestamp_seconds",
                 side_effect=lambda duration, ratio: duration * ratio,
             ):
            stats = plugin.build_update_catalog(
                stash,
                {},
                {"page_limit": 1, "hash_limit": 1},
            )

        fake_catalog.upsert_videos.assert_called_once()
        fake_catalog.set_cursor.assert_called_with("webm", 101)
        fake_catalog.set_hashes.assert_called_once_with(
            101,
            [
                "0000000000000001",
                "0000000000000002",
                "0000000000000003",
            ],
            [6000, 30000, 54000],
            plugin.HASH_VERSION,
        )
        self.assertEqual(stats["hashes_generated"], 1)

    def test_md5_catalog_match_bypasses_phash_work(self):
        stash = mock.Mock()
        stash.ffmpeg_path.return_value = "ffmpeg"
        stash.all_tags.return_value = {}
        stash.all_performers.return_value = {}
        stash.all_studios.return_value = {}
        scene = {
            "id": "10",
            "organized": False,
            "urls": [],
            "tags": [],
            "performers": [],
            "studio": None,
            "date": None,
            "files": [{
                "path": "/local.mp4",
                "duration": 60.0,
                "fingerprints": [{"type": "md5", "value": "ABCDEF"}],
            }],
        }
        post = {
            "id": 999,
            "created_at": "2026-09-01T00:00:00Z",
            "tags": {},
            "sources": [],
            "file": {
                "ext": "webm",
                "url": "https://example/999.webm",
                "md5": "abcdef",
                "duration": 60.0,
            },
        }
        catalog = mock.Mock()
        catalog.count.return_value = 100
        catalog.hashed_count.return_value = 100
        catalog.find_md5.return_value = {"post_id": 999}

        with mock.patch.object(
            plugin, "eligible_local_scenes",
            return_value=([scene], {"eligible": 1, "organized_protected": 0}),
        ), mock.patch.object(
            plugin, "E621VideoCatalog", return_value=catalog
        ), mock.patch.object(
            plugin, "e621_post_by_id", return_value=post
        ), mock.patch.object(
            plugin, "_prepare_local_entry"
        ) as prepare, mock.patch.object(
            plugin, "frame_phash"
        ) as remote_frame, mock.patch.object(
            plugin, "apply_e621_metadata"
        ) as apply:
            stats = plugin.match_stash_against_catalog(
                stash,
                {},
                {"dry_run": False},
            )

        prepare.assert_not_called()
        remote_frame.assert_not_called()
        apply.assert_called_once()
        self.assertEqual(stats["md5_catalog_matches"], 1)

    def test_phash_match_uses_exact_duration_then_one_live_midpoint(self):
        stash = mock.Mock()
        stash.ffmpeg_path.return_value = "ffmpeg"
        stash.all_tags.return_value = {}
        stash.all_performers.return_value = {}
        stash.all_studios.return_value = {}
        scene = {
            "id": "10",
            "organized": False,
            "urls": [],
            "tags": [],
            "performers": [],
            "studio": None,
            "date": None,
            "files": [{
                "path": "/local.mp4",
                "duration": 60.0,
                "fingerprints": [],
            }],
        }
        row = {
            "post_id": 123,
            "duration_ms": 60000,
            "url": "https://example/123.webm",
            "phash_10": plugin.phash_hex(1),
            "phash_50": plugin.phash_hex(2),
            "phash_90": plugin.phash_hex(3),
        }
        post = {
            "id": 123,
            "created_at": "2026-09-01T00:00:00Z",
            "tags": {},
            "sources": [],
            "file": {
                "ext": "webm",
                "url": "https://example/123.webm",
                "duration": 60.0,
            },
        }
        catalog = mock.Mock()
        catalog.count.return_value = 100
        catalog.hashed_count.return_value = 100
        catalog.candidates_for_duration.return_value = [row]

        with mock.patch.object(
            plugin, "eligible_local_scenes",
            return_value=([scene], {"eligible": 1, "organized_protected": 0}),
        ), mock.patch.object(
            plugin, "E621VideoCatalog", return_value=catalog
        ), mock.patch.object(
            plugin, "load_cache", return_value={"version": 3, "scenes": {}}
        ), mock.patch.object(plugin, "save_cache"), mock.patch.object(
            plugin,
            "_prepare_local_entry",
            return_value={
                "scene": scene,
                "scene_id": "10",
                "path": "/local.mp4",
                "duration": 60.0,
                "duration_ms": 60000,
                "phashes": [0, 0, 0],
            },
        ), mock.patch.object(
            plugin, "frame_phash", return_value=2
        ) as remote_midpoint, mock.patch.object(
            plugin, "e621_post_by_id", return_value=post
        ), mock.patch.object(
            plugin, "apply_e621_metadata"
        ) as apply:
            stats = plugin.match_stash_against_catalog(
                stash,
                {},
                {"dry_run": False},
            )

        catalog.candidates_for_duration.assert_called_once_with(
            60000,
            require_hashes=True,
        )
        remote_midpoint.assert_called_once_with(
            "https://example/123.webm",
            60.0,
            plugin.HASH_RATIOS[1],
            ffmpeg_path="ffmpeg",
            timeout=90,
        )
        apply.assert_called_once()
        self.assertEqual(stats["midpoint_verified"], 1)

    def test_metadata_maps_tags_characters_artist_date_and_urls(self):
        post = {
            "id": 123,
            "created_at": "2026-09-01T12:34:56Z",
            "tags": {
                "artist": ["artist_one", "artist_two"],
                "character": ["character_one"],
                "general": ["tag_one"],
                "species": ["species_one"],
                "copyright": ["series_one"],
                "lore": ["lore_one"],
            },
            "sources": ["https://source.example/post"],
        }
        metadata = plugin.e621_post_metadata(post)
        self.assertEqual(metadata["artists"], ["artist_one", "artist_two"])
        self.assertEqual(metadata["characters"], ["character_one"])
        self.assertIn("tag_one", metadata["tags"])
        self.assertIn("species_one", metadata["tags"])
        self.assertIn("series_one", metadata["tags"])
        self.assertIn("lore_one", metadata["tags"])
        self.assertIn("artist_two", metadata["tags"])
        self.assertEqual(metadata["date"], "2026-09-01")
        self.assertIn("https://e621.net/posts/123", metadata["urls"])


    def test_phash_accepts_complete_frame_even_if_ffmpeg_exits_nonzero(self):
        complete = bytes([128]) * (32 * 32)
        proc = mock.Mock(
            returncode=1,
            stdout=complete,
            stderr=b"Decoding error: Invalid data found when processing input\n",
        )
        with mock.patch.object(video_match.subprocess, "run", return_value=proc) as run:
            value = video_match.frame_phash(
                "https://example/video.webm",
                60.0,
                0.5,
                ffmpeg_path="ffmpeg",
                timeout=10,
            )
        self.assertIsInstance(value, int)
        self.assertEqual(run.call_count, 1)

    def test_phash_falls_back_to_tolerant_decode_when_fast_seek_fails(self):
        failed = mock.Mock(
            returncode=1,
            stdout=b"",
            stderr=b"[dec:vp8] Invalid data found when processing input\n",
        )
        complete = bytes([64]) * (32 * 32)
        recovered = mock.Mock(
            returncode=0,
            stdout=complete,
            stderr=b"",
        )
        with mock.patch.object(
            video_match.subprocess,
            "run",
            side_effect=[failed, recovered],
        ) as run:
            value = video_match.frame_phash(
                "https://example/video.webm",
                60.0,
                0.5,
                ffmpeg_path="ffmpeg",
                timeout=10,
            )
        self.assertIsInstance(value, int)
        self.assertEqual(run.call_count, 2)
        tolerant_cmd = run.call_args_list[1].args[0]
        self.assertIn("ignore_err", tolerant_cmd)
        self.assertIn("+discardcorrupt", tolerant_cmd)
        self.assertLess(tolerant_cmd.index("-i"), tolerant_cmd.index("-ss"))

    def test_phash_failure_error_is_single_line(self):
        failed_fast = mock.Mock(
            returncode=1,
            stdout=b"",
            stderr=b"line one\nline two\n",
        )
        failed_tolerant = mock.Mock(
            returncode=1,
            stdout=b"",
            stderr=b"decoder detail\nInvalid data found when processing input\n",
        )
        with mock.patch.object(
            video_match.subprocess,
            "run",
            side_effect=[failed_fast, failed_tolerant],
        ):
            with self.assertRaises(RuntimeError) as caught:
                video_match.frame_phash(
                    "https://example/video.webm",
                    60.0,
                    0.5,
                    ffmpeg_path="ffmpeg",
                    timeout=10,
                )
        message = str(caught.exception)
        self.assertNotIn("\n", message)
        self.assertIn("Invalid data found when processing input", message)


if __name__ == "__main__":
    unittest.main()
