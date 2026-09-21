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


    def test_duration_prefilter_only_keeps_exact_millisecond_bucket(self):
        local_index = [
            {"scene_id": "1", "duration": 60.000, "hashes": [1, 2]},
            {"scene_id": "2", "duration": 60.001, "hashes": [1, 2]},
            {"scene_id": "3", "duration": 59.999, "hashes": [1, 2]},
        ]
        buckets = plugin.build_duration_buckets(local_index)
        rows = plugin.source_first_duration_candidates(buckets, 60.000)
        self.assertEqual([row["scene_id"] for row in rows], ["1"])

    def test_duration_prefilter_rejects_unknown_remote_duration(self):
        buckets = plugin.build_duration_buckets(
            [{"scene_id": "1", "duration": 100.0, "hashes": [1, 2]}]
        )
        rows = plugin.source_first_duration_candidates(buckets, 0.0)
        self.assertEqual(rows, [])

    def test_duration_millisecond_normalization(self):
        self.assertEqual(plugin.duration_milliseconds(60.0), 60000)
        self.assertEqual(plugin.duration_milliseconds(60.001), 60001)
        self.assertEqual(plugin.duration_milliseconds(59.999), 59999)


    def test_dynamic_stash_tag_scope_accepts_any_name_or_alias(self):
        stash = mock.Mock()
        stash.all_tags.return_value = {
            "furry": {"id": "77", "name": "Furry"},
            "anthro": {"id": "77", "name": "Furry"},
            "custom future tag": {"id": "88", "name": "Custom Future Tag"},
        }

        self.assertEqual(
            plugin.resolve_stash_tag_filter(stash, "anthro"),
            ("77", "Furry"),
        )
        self.assertEqual(
            plugin.resolve_stash_tag_filter(stash, "Custom Future Tag"),
            ("88", "Custom Future Tag"),
        )

    def test_scene_scope_membership_uses_tag_id(self):
        scene = {
            "tags": [
                {"id": "77", "name": "Furry"},
                {"id": "12", "name": "Other"},
            ]
        }
        self.assertTrue(plugin.scene_has_tag_id(scene, "77"))
        self.assertFalse(plugin.scene_has_tag_id(scene, "999"))
        self.assertTrue(plugin.scene_has_tag_id(scene, None))

    def test_strict_source_first_requires_all_seven_frames(self):
        local = [0, 1, 2, 3, 4, 5, 6]
        remote = [0, 1, 2, 3, 4, 5, (1 << 256) - 1]
        result = video_match.verify_video_candidate(
            "local.mp4",
            "remote.mp4",
            local_duration=100.0,
            remote_duration=100.0,
            local_hashes=local,
            remote_hashes=remote,
            frame_distance=16,
            strict=True,
        )
        self.assertFalse(result["high"])

    def test_strict_source_first_accepts_identical_aligned_frames_and_duration(self):
        hashes = [
            int.from_bytes(bytes([value]) * 32, "big")
            for value in (0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66)
        ]
        result = video_match.verify_video_candidate(
            "local.mp4",
            "remote.mp4",
            local_duration=100.0,
            remote_duration=100.5,
            local_hashes=hashes,
            remote_hashes=list(hashes),
            frame_distance=16,
            strict=True,
        )
        self.assertTrue(result["high"])
        self.assertEqual(result["matched_frames"], 7)


    def test_source_first_candidate_rejects_nonexact_duration_even_with_same_hashes(self):
        rows = plugin.source_first_local_candidates(
            [{"scene_id": "1", "duration": 60.001, "hashes": [0, 15]}],
            [0, 15],
            60.000,
        )
        self.assertEqual(rows, [])

    def test_source_cursor_is_scoped_by_stash_tag(self):
        stash = mock.Mock()
        settings = {"stash_tag_scope": "furry"}
        with mock.patch.object(
            plugin,
            "resolve_stash_tag_filter",
            return_value=("77", "Furry"),
        ):
            key, label = plugin._source_cursor_scope(stash, settings)
        self.assertEqual(key, "tag:77")
        self.assertEqual(label, "Furry")

        key, label = plugin._source_cursor_scope(stash, {})
        self.assertEqual(key, "__all__")
        self.assertIn("all eligible", label)

    def test_reset_source_cursor_removes_only_current_scope(self):
        stash = mock.Mock()
        settings = {"stash_tag_scope": "furry"}
        state = {
            "version": 1,
            "scopes": {
                "tag:77": {"before_id": 123, "exhausted": False},
                "tag:88": {"before_id": 456, "exhausted": False},
            },
        }
        with mock.patch.object(
            plugin,
            "resolve_stash_tag_filter",
            return_value=("77", "Furry"),
        ), mock.patch.object(
            plugin,
            "load_scan_state",
            return_value=state,
        ), mock.patch.object(
            plugin,
            "save_scan_state",
        ) as save:
            result = plugin.reset_source_cursor(stash, settings)

        self.assertEqual(result["cursor_reset"], 1)
        self.assertNotIn("tag:77", state["scopes"])
        self.assertIn("tag:88", state["scopes"])
        save.assert_called_once()


    def test_stash_first_checks_next_exact_duration_candidate_until_match(self):
        stash = mock.Mock()
        stash.ffmpeg_path.return_value = "ffmpeg"
        stash.all_tags.return_value = {}
        stash.all_performers.return_value = {}
        stash.all_studios.return_value = {}

        scene = {
            "id": "10",
            "tags": [],
            "performers": [],
            "urls": [],
            "studio": None,
            "date": None,
        }
        local_entry = {
            "scene": scene,
            "scene_id": "10",
            "path": "/local.mp4",
            "duration": 60.0,
            "hashes": [1, 2],
        }
        first = {
            "id": 200,
            "file": {
                "ext": "webm",
                "url": "https://e621.example/200.webm",
                "duration": 60.0,
            },
        }
        second = {
            "id": 199,
            "file": {
                "ext": "webm",
                "url": "https://e621.example/199.webm",
                "duration": 60.0,
            },
        }

        high = {
            "high": True,
            "review": False,
            "matched_frames": 7,
            "total_frames": 7,
            "median_distance": 0.0,
        }

        with mock.patch.object(
            plugin, "build_local_source_index",
            return_value=([local_entry], {"skipped_organized": 0}),
        ), mock.patch.object(
            plugin, "_source_cursor_scope",
            return_value=("__all__", "all eligible Stash videos"),
        ), mock.patch.object(
            plugin, "_load_source_cursor",
            return_value=({"scopes": {}}, {"completed_scene_ids": []}),
        ), mock.patch.object(
            plugin, "_save_source_cursor",
        ), mock.patch.object(
            plugin, "e621_video_posts_page",
            return_value=[first, second],
        ), mock.patch.object(
            plugin, "early_frame_hashes",
            side_effect=[[9, 9], [1, 2]],
        ), mock.patch.object(
            plugin, "early_hash_candidate",
            side_effect=[
                {"candidate": False, "distances": [9, 9]},
                {"candidate": True, "distances": [0, 0]},
            ],
        ) as early, mock.patch.object(
            plugin, "frame_hashes",
            side_effect=[[10] * 7, [10] * 7],
        ), mock.patch.object(
            plugin, "verify_video_candidate",
            return_value=high,
        ), mock.patch.object(
            plugin, "apply_metadata",
        ) as apply:
            stats = plugin.stash_first_e621(
                stash,
                {"e621_source_pages": 5},
                {"dry_run": False},
            )

        self.assertEqual(early.call_count, 2)
        self.assertEqual(stats["e621_exact_duration_candidates"], 2)
        self.assertEqual(stats["local_scenes_matched"], 1)
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[3]["id"], 199)

    def test_stash_first_exhausts_one_local_video_then_restarts_next_from_newest(self):
        stash = mock.Mock()
        stash.ffmpeg_path.return_value = "ffmpeg"
        stash.all_tags.return_value = {}
        stash.all_performers.return_value = {}
        stash.all_studios.return_value = {}

        entries = [
            {
                "scene": {"id": "10", "tags": [], "performers": [], "urls": [], "studio": None, "date": None},
                "scene_id": "10",
                "path": "/one.mp4",
                "duration": 60.0,
                "hashes": [1, 2],
            },
            {
                "scene": {"id": "11", "tags": [], "performers": [], "urls": [], "studio": None, "date": None},
                "scene_id": "11",
                "path": "/two.mp4",
                "duration": 70.0,
                "hashes": [3, 4],
            },
        ]
        wrong = {
            "id": 300,
            "file": {
                "ext": "webm",
                "url": "https://e621.example/300.webm",
                "duration": 60.0,
            },
        }
        right = {
            "id": 400,
            "file": {
                "ext": "webm",
                "url": "https://e621.example/400.webm",
                "duration": 70.0,
            },
        }
        high = {
            "high": True,
            "review": False,
            "matched_frames": 7,
            "total_frames": 7,
            "median_distance": 0.0,
        }

        calls = []
        def page_lookup(_user, _key, before_id=None, limit=75):
            calls.append(before_id)
            if len(calls) == 1:
                return [wrong]
            if len(calls) == 2:
                return []
            if len(calls) == 3:
                return [right]
            return []

        with mock.patch.object(
            plugin, "build_local_source_index",
            return_value=(entries, {"skipped_organized": 0}),
        ), mock.patch.object(
            plugin, "_source_cursor_scope",
            return_value=("__all__", "all eligible Stash videos"),
        ), mock.patch.object(
            plugin, "_load_source_cursor",
            return_value=({"scopes": {}}, {"completed_scene_ids": []}),
        ), mock.patch.object(
            plugin, "_save_source_cursor",
        ), mock.patch.object(
            plugin, "e621_video_posts_page",
            side_effect=page_lookup,
        ), mock.patch.object(
            plugin, "early_frame_hashes",
            side_effect=[[8, 8], [3, 4]],
        ), mock.patch.object(
            plugin, "early_hash_candidate",
            side_effect=[
                {"candidate": False, "distances": [8, 8]},
                {"candidate": True, "distances": [0, 0]},
            ],
        ), mock.patch.object(
            plugin, "frame_hashes",
            side_effect=[[10] * 7, [20] * 7, [20] * 7],
        ), mock.patch.object(
            plugin, "verify_video_candidate",
            return_value=high,
        ), mock.patch.object(
            plugin, "apply_metadata",
        ) as apply:
            stats = plugin.stash_first_e621(
                stash,
                {"e621_source_pages": 5},
                {"dry_run": False},
            )

        self.assertEqual(calls[:3], [None, 300, None])
        self.assertEqual(stats["local_scenes_no_match"], 1)
        self.assertEqual(stats["local_scenes_matched"], 1)
        self.assertEqual(apply.call_args.args[1]["id"], "11")


if __name__ == "__main__":
    unittest.main()
