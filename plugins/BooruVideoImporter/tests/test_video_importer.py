import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

spec = importlib.util.spec_from_file_location(
    "booru_video_importer", PLUGIN_DIR / "BooruVideoImporter.py"
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(plugin)

import video_match


class VideoMatchTests(unittest.TestCase):
    def test_hamming_distance(self):
        self.assertEqual(video_match.hamming_distance(0b1010, 0b1010), 0)
        self.assertEqual(video_match.hamming_distance(0b1010, 0b0011), 2)

    def test_high_confidence_requires_multiple_agreeing_frames(self):
        local = [0, 1, 2, 3, 4, 5, 6]
        remote = [0, 1, 2, 3, 4, 5, 63]
        with mock.patch.object(video_match, "probe_duration", return_value=100.0), \
             mock.patch.object(video_match, "frame_hashes", side_effect=[local, remote]):
            result = video_match.verify_video_candidate(
                "local.mp4", "remote.mp4", local_duration=100.0
            )

        self.assertTrue(result["high"])
        self.assertFalse(result["review"])
        self.assertGreaterEqual(result["matched_frames"], 5)

    def test_unrelated_video_does_not_match(self):
        local = [0x0000000000000000] * 7
        remote = [0xFFFFFFFFFFFFFFFF] * 7
        with mock.patch.object(video_match, "probe_duration", return_value=100.0), \
             mock.patch.object(video_match, "frame_hashes", side_effect=[local, remote]):
            result = video_match.verify_video_candidate(
                "local.mp4", "remote.mp4", local_duration=100.0
            )

        self.assertFalse(result["high"])
        self.assertFalse(result["review"])
        self.assertEqual(result["matched_frames"], 0)

    def tearDown(self):
        plugin._ERIS_DISABLED_FOR_RUN = ""

    def test_eris_429_disables_only_reverse_image_fallback(self):
        plugin._ERIS_DISABLED_FOR_RUN = ""
        with mock.patch.object(
            plugin,
            "_json_request",
            side_effect=RuntimeError(
                "HTTP 429: <html><title>Just a moment...</title>Cloudflare</html>"
            ),
        ), mock.patch.object(plugin, "_wait", return_value=0.0), \
             mock.patch.object(plugin, "log") as logger:
            first = plugin.e621_eris_candidates(b"frame", "user", "key")
            second = plugin.e621_eris_candidates(b"frame", "user", "key")

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertTrue(plugin._ERIS_DISABLED_FOR_RUN)
        self.assertEqual(logger.call_count, 1)

    def test_saucenao_e621_hit_skips_eris_for_that_frame(self):
        settings = {
            "saucenao_api_key": "sauce",
            "e621_username": "user",
            "e621_api_key": "key",
        }
        with mock.patch.object(plugin, "DISCOVERY_RATIOS", (0.5,)), \
             mock.patch.object(plugin, "extract_jpeg_frame", return_value=b"frame"), \
             mock.patch.object(
                 plugin,
                 "saucenao_candidates",
                 return_value=[(95.0, "e621", "123")],
             ), \
             mock.patch.object(plugin, "e621_eris_candidates") as eris:
            rows, had_error = plugin.discover_candidates(
                "local.mp4", 100.0, "ffmpeg", settings
            )

        self.assertFalse(had_error)
        self.assertEqual(rows[0][:3], ("e621", "123", 1))
        eris.assert_not_called()

    def test_e621_video_media_filter(self):
        post = {"file": {"ext": "webm", "url": "https://static.example/video.webm"}}
        self.assertEqual(
            plugin.media_url("e621", post),
            "https://static.example/video.webm",
        )
        image = {"file": {"ext": "jpg", "url": "https://static.example/image.jpg"}}
        self.assertIsNone(plugin.media_url("e621", image))

    def test_e621_metadata_maps_artist_character_tags_and_urls(self):
        post = {
            "id": 123,
            "created_at": "2026-09-01T12:34:56Z",
            "tags": {
                "artist": ["artist_one", "artist_two"],
                "character": ["character_one"],
                "general": ["tag_one"],
                "species": ["species_one"],
                "copyright": ["series_one"],
                "lore": [],
            },
            "sources": ["https://source.example/post"],
            "file": {"ext": "webm", "url": "https://static.example/video.webm"},
        }
        metadata = plugin.post_metadata("e621", post, {})

        self.assertEqual(metadata["artists"], ["artist_one", "artist_two"])
        self.assertEqual(metadata["characters"], ["character_one"])
        self.assertIn("tag_one", metadata["tags"])
        self.assertIn("species_one", metadata["tags"])
        self.assertIn("series_one", metadata["tags"])
        self.assertIn("artist_two", metadata["tags"])
        self.assertEqual(metadata["date"], "2026-09-01")
        self.assertEqual(metadata["urls"][0], "https://e621.net/posts/123")
        self.assertIn("https://source.example/post", metadata["urls"])


    def test_early_hash_candidate_requires_strong_agreement(self):
        result = video_match.early_hash_candidate(
            [0x0000000000000000, 0x000000000000000F],
            [0x0000000000000001, 0x000000000000000E],
            max_distance=4,
        )
        self.assertTrue(result["candidate"])
        self.assertEqual(result["matched"], 2)

    def test_current_e621_files_schema_is_supported(self):
        post = {
            "id": 321,
            "files": {
                "meta": {"ext": "webm", "md5": "abc", "duration": 12.5},
                "original": {"url": "https://static.example/321.webm"},
                "preview": {"jpg": "https://static.example/321-preview.jpg"},
            },
        }
        info = plugin.e621_file_info(post)
        self.assertEqual(info["ext"], "webm")
        self.assertEqual(info["md5"], "abc")
        self.assertEqual(info["duration"], 12.5)
        self.assertEqual(
            plugin.media_url("e621", post),
            "https://static.example/321.webm",
        )

    def test_e621_source_page_merges_webm_and_mp4(self):
        webm = [{
            "id": 20,
            "files": {
                "meta": {"ext": "webm", "md5": "a"},
                "original": {"url": "https://static.example/20.webm"},
            },
        }]
        mp4 = [{
            "id": 21,
            "files": {
                "meta": {"ext": "mp4", "md5": "b"},
                "original": {"url": "https://static.example/21.mp4"},
            },
        }]
        with mock.patch.object(
            plugin, "e621_request", side_effect=[webm, mp4]
        ) as request:
            rows = plugin.e621_video_posts_page("user", "key", before_id=99, limit=10)

        self.assertEqual([row["id"] for row in rows], [21, 20])
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertIn("v2=true", call.args[0])
            self.assertIn("mode=extended", call.args[0])
            self.assertIn("page=b99", call.args[0])

    def test_organized_toggle_skips_scene_before_matching(self):
        scene = {
            "id": "44",
            "organized": True,
            "tags": [],
            "files": [{
                "id": "f44",
                "path": "/video.mp4",
                "duration": 10.0,
                "fingerprints": [{"type": "md5", "value": "abc"}],
            }],
        }
        stash = mock.Mock()
        settings = {"skip_organized_scenes": True}
        with mock.patch.object(plugin, "exact_candidates") as exact:
            result = plugin.process_scene(
                stash,
                scene,
                settings,
                deep=False,
                dry_run=False,
                tag_cache={},
                performer_cache={},
                studio_cache={},
            )

        self.assertEqual(result, "skipped_organized")
        exact.assert_not_called()
        stash.update_scene.assert_not_called()

    def test_apply_metadata_respects_organized_protection(self):
        scene = {
            "id": "45",
            "organized": True,
            "tags": [],
            "performers": [],
            "urls": [],
            "studio": None,
            "date": None,
        }
        stash = mock.Mock()
        plugin.apply_metadata(
            stash,
            scene,
            "e621",
            {
                "id": 1,
                "tags": {"general": ["new_tag"]},
                "sources": [],
                "created_at": "2026-09-01T00:00:00Z",
            },
            {"skip_organized_scenes": True},
            {},
            {},
            {},
            False,
        )
        stash.update_scene.assert_not_called()

    def test_source_first_local_candidates_prefers_matching_early_hashes(self):
        local_index = [
            {
                "scene_id": "1",
                "duration": 10.0,
                "hashes": [0x0000, 0x000F],
            },
            {
                "scene_id": "2",
                "duration": 10.0,
                "hashes": [0xFFFF, 0xFFF0],
            },
        ]
        rows = plugin.source_first_local_candidates(
            local_index,
            [0x0001, 0x000E],
            10.0,
        )
        self.assertTrue(rows)
        self.assertEqual(rows[0][1]["scene_id"], "1")


    def test_permuted_frames_do_not_pass_aligned_verification(self):
        patterns = [
            int.from_bytes(bytes([value]) * 32, "big")
            for value in (0x00, 0x0F, 0x33, 0x55, 0xAA, 0xCC, 0xFF)
        ]
        result = video_match.verify_video_candidate(
            "local.mp4",
            "remote.mp4",
            local_duration=100.0,
            remote_duration=100.0,
            local_hashes=patterns,
            remote_hashes=list(reversed(patterns)),
            frame_distance=24,
        )

        self.assertFalse(result["high"])
        self.assertFalse(result["review"])
        self.assertLess(result["matched_frames"], 5)

    def test_single_early_frame_is_never_a_candidate(self):
        result = video_match.early_hash_candidate(
            [0x0000000000000000],
            [0x0000000000000000],
            max_distance=6,
        )
        self.assertFalse(result["candidate"])
        self.assertEqual(result["matched"], 0)

    def test_two_early_frames_must_match_same_positions(self):
        local = [0x0000000000000000, 0xFFFFFFFFFFFFFFFF]
        remote = [0xFFFFFFFFFFFFFFFF, 0x0000000000000000]
        result = video_match.early_hash_candidate(local, remote, max_distance=6)
        self.assertFalse(result["candidate"])


    def test_source_first_rejects_large_duration_mismatch(self):
        local_index = [{
            "scene_id": "1",
            "duration": 100.0,
            "hashes": [0x0000, 0x000F],
        }]
        rows = plugin.source_first_local_candidates(
            local_index,
            [0x0001, 0x000E],
            60.0,
        )
        self.assertEqual(rows, [])


    def test_duration_prefilter_only_keeps_same_length_scenes(self):
        local_index = [
            {"scene_id": "1", "duration": 100.0, "hashes": [1, 2]},
            {"scene_id": "2", "duration": 102.1, "hashes": [1, 2]},
            {"scene_id": "3", "duration": 101.9, "hashes": [1, 2]},
        ]
        rows = plugin.source_first_duration_candidates(
            local_index,
            100.0,
            2.0,
        )
        self.assertEqual(
            [row["scene_id"] for row in rows],
            ["1", "3"],
        )

    def test_duration_prefilter_rejects_unknown_remote_duration(self):
        rows = plugin.source_first_duration_candidates(
            [{"scene_id": "1", "duration": 100.0, "hashes": [1, 2]}],
            0.0,
            2.0,
        )
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
