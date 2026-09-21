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


class BooruVideoImporterTests(unittest.TestCase):
    def test_duration_is_exact_to_normalized_millisecond(self):
        self.assertEqual(plugin.duration_milliseconds(60.000), 60000)
        self.assertEqual(plugin.duration_milliseconds(59.999), 59999)
        self.assertEqual(plugin.duration_milliseconds(60.001), 60001)
        self.assertEqual(plugin.duration_milliseconds(0), 0)

    def test_e621_file_info_supports_legacy_and_current_schema(self):
        legacy = {
            "file": {
                "ext": "webm",
                "url": "https://example/1.webm",
                "duration": 60.0,
            }
        }
        current = {
            "files": {
                "meta": {"ext": "mp4", "duration": 12.345},
                "original": {"url": "https://example/2.mp4"},
            }
        }
        self.assertEqual(plugin.e621_file_info(legacy)["ext"], "webm")
        self.assertEqual(plugin.e621_file_info(legacy)["duration"], 60.0)
        self.assertEqual(plugin.e621_file_info(current)["ext"], "mp4")
        self.assertEqual(plugin.e621_file_info(current)["duration"], 12.345)

    def test_e621_video_page_is_format_specific_and_uses_before_cursor(self):
        payload = [{
            "id": 123,
            "file": {
                "ext": "webm",
                "url": "https://example/123.webm",
                "duration": 10.0,
            },
        }]
        with mock.patch.object(plugin, "e621_request", return_value=payload) as request:
            rows = plugin.e621_video_posts_page(
                "webm", "user", "key", before_id=500, limit=75
            )
        self.assertEqual([row["id"] for row in rows], [123])
        url = request.call_args.args[0]
        self.assertIn("type%3Awebm", url)
        self.assertIn("page=b500", url)

    def test_dynamic_stash_scope_accepts_any_tag_or_alias(self):
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

    def test_eligible_local_scenes_respects_scope_and_organized_protection(self):
        stash = mock.Mock()
        stash.all_tags.return_value = {
            "furry": {"id": "77", "name": "Furry"},
        }
        stash.find_scenes.return_value = (
            2,
            [
                {
                    "id": "1",
                    "organized": False,
                    "urls": [],
                    "files": [{"path": "/one.mp4"}],
                },
                {
                    "id": "2",
                    "organized": True,
                    "urls": [],
                    "files": [{"path": "/two.mp4"}],
                },
            ],
        )
        rows, stats = plugin.eligible_local_scenes(
            stash,
            {"stash_tag_scope": "furry", "skip_organized_scenes": True},
        )
        self.assertEqual([row["id"] for row in rows], ["1"])
        self.assertEqual(stats["organized_protected"], 1)
        self.assertEqual(
            stash.find_scenes.call_args.kwargs["tag_id"],
            "77",
        )

    def test_local_frame_index_is_lazy_and_reused(self):
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
        cache = {"version": 2, "scenes": {}}
        with mock.patch.object(
            plugin, "early_frame_hashes", return_value=[11, 22]
        ) as hashes:
            first = plugin._prepare_local_entry(scene, cache, "ffmpeg")
            second = plugin._prepare_local_entry(scene, cache, "ffmpeg")
        self.assertEqual(first["early_hashes"], [11, 22])
        self.assertEqual(second["early_hashes"], [11, 22])
        self.assertEqual(hashes.call_count, 1)

    def test_nonexact_duration_never_opens_remote_video(self):
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
                "id": "f10",
                "path": "/local.mp4",
                "duration": 60.000,
                "size": 100,
                "mod_time": "x",
            }],
        }
        post = {
            "id": 200,
            "file": {
                "ext": "webm",
                "url": "https://example/200.webm",
                "duration": 60.001,
            },
        }
        with mock.patch.object(
            plugin, "eligible_local_scenes",
            return_value=([scene], {
                "eligible": 1,
                "organized_protected": 0,
            }),
        ), mock.patch.object(
            plugin, "_scope",
            return_value=(None, "__all__", "all"),
        ), mock.patch.object(
            plugin, "_load_scope_state",
            return_value=({"scopes": {}}, {"completed_scene_ids": []}),
        ), mock.patch.object(
            plugin, "load_cache",
            return_value={"version": 2, "scenes": {}},
        ), mock.patch.object(
            plugin, "save_cache",
        ), mock.patch.object(
            plugin, "_prepare_local_entry",
            return_value={
                "scene": scene,
                "scene_id": "10",
                "path": "/local.mp4",
                "duration": 60.0,
                "duration_ms": 60000,
                "early_hashes": [1, 2],
            },
        ), mock.patch.object(
            plugin, "e621_video_posts_page",
            side_effect=[[post], []],
        ), mock.patch.object(
            plugin, "early_frame_hashes",
        ) as remote_frames:
            plugin.match_stash_against_e621(
                stash,
                {"e621_pages_per_run": 2},
                {"dry_run": True, "pages_per_run": 2, "local_limit": 1},
            )
        remote_frames.assert_not_called()

    def test_exact_duration_candidates_are_checked_in_order_until_match(self):
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
            "files": [{"path": "/local.mp4", "duration": 60.0}],
        }
        first = {
            "id": 200,
            "created_at": "2026-09-01T00:00:00Z",
            "tags": {},
            "sources": [],
            "file": {
                "ext": "webm",
                "url": "https://example/200.webm",
                "duration": 60.0,
            },
        }
        second = {
            "id": 199,
            "created_at": "2026-09-01T00:00:00Z",
            "tags": {},
            "sources": [],
            "file": {
                "ext": "webm",
                "url": "https://example/199.webm",
                "duration": 60.0,
            },
        }
        with mock.patch.object(
            plugin, "eligible_local_scenes",
            return_value=([scene], {
                "eligible": 1,
                "organized_protected": 0,
            }),
        ), mock.patch.object(
            plugin, "_scope",
            return_value=(None, "__all__", "all"),
        ), mock.patch.object(
            plugin, "_load_scope_state",
            return_value=({"scopes": {}}, {"completed_scene_ids": []}),
        ), mock.patch.object(
            plugin, "load_cache",
            return_value={"version": 2, "scenes": {}},
        ), mock.patch.object(plugin, "save_cache"), mock.patch.object(
            plugin, "_save_scope_state",
        ), mock.patch.object(
            plugin, "_prepare_local_entry",
            return_value={
                "scene": scene,
                "scene_id": "10",
                "path": "/local.mp4",
                "duration": 60.0,
                "duration_ms": 60000,
                "early_hashes": [1, 2],
            },
        ), mock.patch.object(
            plugin, "e621_video_posts_page",
            return_value=[first, second],
        ), mock.patch.object(
            plugin, "early_frame_hashes",
            side_effect=[[9, 9], [1, 2]],
        ), mock.patch.object(
            plugin, "early_hash_candidate",
            side_effect=[
                {"candidate": False},
                {"candidate": True},
            ],
        ) as early, mock.patch.object(
            plugin, "frame_hashes",
            side_effect=[[7] * 7, [7] * 7],
        ), mock.patch.object(
            plugin, "verify_video_candidate",
            return_value={
                "high": True,
                "matched_frames": 7,
                "total_frames": 7,
            },
        ), mock.patch.object(
            plugin, "apply_e621_metadata",
        ) as apply:
            stats = plugin.match_stash_against_e621(
                stash,
                {"e621_pages_per_run": 1},
                {"dry_run": False, "pages_per_run": 1},
            )
        self.assertEqual(early.call_count, 2)
        self.assertEqual(stats["exact_duration_candidates"], 2)
        self.assertEqual(stats["local_videos_matched"], 1)
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[2]["id"], 199)

    def test_webm_exhaustion_switches_to_mp4_from_newest(self):
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
            "files": [{"path": "/local.mp4", "duration": 60.0}],
        }
        calls = []

        def pages(ext, _user, _key, before_id=None, limit=75):
            calls.append((ext, before_id))
            if ext == "webm":
                return []
            return []

        with mock.patch.object(
            plugin, "eligible_local_scenes",
            return_value=([scene], {
                "eligible": 1,
                "organized_protected": 0,
            }),
        ), mock.patch.object(
            plugin, "_scope",
            return_value=(None, "__all__", "all"),
        ), mock.patch.object(
            plugin, "_load_scope_state",
            return_value=({"scopes": {}}, {"completed_scene_ids": []}),
        ), mock.patch.object(
            plugin, "load_cache",
            return_value={"version": 2, "scenes": {}},
        ), mock.patch.object(plugin, "save_cache"), mock.patch.object(
            plugin, "_save_scope_state",
        ), mock.patch.object(
            plugin, "_prepare_local_entry",
            return_value={
                "scene": scene,
                "scene_id": "10",
                "path": "/local.mp4",
                "duration": 60.0,
                "duration_ms": 60000,
                "early_hashes": [1, 2],
            },
        ), mock.patch.object(
            plugin, "e621_video_posts_page", side_effect=pages
        ):
            stats = plugin.match_stash_against_e621(
                stash,
                {"e621_pages_per_run": 2},
                {"dry_run": False, "pages_per_run": 2},
            )
        self.assertEqual(calls[:2], [("webm", None), ("mp4", None)])
        self.assertEqual(stats["local_videos_exhausted"], 1)

    def test_saved_state_resumes_same_scene_and_format_cursor(self):
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
            "files": [{"path": "/local.mp4", "duration": 60.0}],
        }
        with mock.patch.object(
            plugin, "eligible_local_scenes",
            return_value=([scene], {
                "eligible": 1,
                "organized_protected": 0,
            }),
        ), mock.patch.object(
            plugin, "_scope",
            return_value=(None, "__all__", "all"),
        ), mock.patch.object(
            plugin, "_load_scope_state",
            return_value=(
                {"scopes": {}},
                {
                    "scene_id": "10",
                    "ext": "mp4",
                    "before_id": 555,
                    "completed_scene_ids": [],
                },
            ),
        ), mock.patch.object(
            plugin, "load_cache",
            return_value={"version": 2, "scenes": {}},
        ), mock.patch.object(plugin, "save_cache"), mock.patch.object(
            plugin, "_prepare_local_entry",
            return_value={
                "scene": scene,
                "scene_id": "10",
                "path": "/local.mp4",
                "duration": 60.0,
                "duration_ms": 60000,
                "early_hashes": [1, 2],
            },
        ), mock.patch.object(
            plugin, "e621_video_posts_page", return_value=[]
        ) as pages, mock.patch.object(
            plugin, "_save_scope_state",
        ):
            plugin.match_stash_against_e621(
                stash,
                {"e621_pages_per_run": 1},
                {"dry_run": False, "pages_per_run": 1},
            )
        self.assertEqual(pages.call_args.args[0], "mp4")
        self.assertEqual(pages.call_args.kwargs["before_id"], 555)

    def test_reset_progress_only_clears_current_scope(self):
        stash = mock.Mock()
        state = {
            "version": 2,
            "scopes": {
                "tag:77": {"scene_id": "10"},
                "tag:88": {"scene_id": "20"},
            },
        }
        with mock.patch.object(
            plugin, "_scope",
            return_value=("77", "tag:77", "Furry"),
        ), mock.patch.object(
            plugin, "load_scan_state", return_value=state
        ), mock.patch.object(plugin, "save_scan_state") as save:
            result = plugin.reset_progress(stash, {"stash_tag_scope": "furry"})
        self.assertEqual(result["progress_reset"], 1)
        self.assertNotIn("tag:77", state["scopes"])
        self.assertIn("tag:88", state["scopes"])
        save.assert_called_once()

    def test_e621_metadata_maps_characters_artists_tags_and_urls(self):
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
        }
        metadata = plugin.e621_post_metadata(post)
        self.assertIn("tag_one", metadata["tags"])
        self.assertIn("artist_two", metadata["tags"])
        self.assertEqual(metadata["artists"], ["artist_one", "artist_two"])
        self.assertEqual(metadata["characters"], ["character_one"])
        self.assertIn("https://e621.net/posts/123", metadata["urls"])
        self.assertEqual(metadata["date"], "2026-09-01")


    def test_zero_pages_runs_continuously_until_both_histories_exhaust(self):
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
            "files": [{"path": "/local.mp4", "duration": 60.0}],
        }
        page_one = [{
            "id": 300,
            "file": {
                "ext": "webm",
                "url": "https://example/300.webm",
                "duration": 59.0,
            },
        }]
        page_two = [{
            "id": 200,
            "file": {
                "ext": "webm",
                "url": "https://example/200.webm",
                "duration": 61.0,
            },
        }]
        calls = []

        def pages(ext, _user, _key, before_id=None, limit=75):
            calls.append((ext, before_id))
            if len(calls) == 1:
                return page_one
            if len(calls) == 2:
                return page_two
            return []

        with mock.patch.object(
            plugin, "eligible_local_scenes",
            return_value=([scene], {"eligible": 1, "organized_protected": 0}),
        ), mock.patch.object(
            plugin, "_scope", return_value=(None, "__all__", "all"),
        ), mock.patch.object(
            plugin, "_load_scope_state",
            return_value=({"scopes": {}}, {"completed_scene_ids": []}),
        ), mock.patch.object(
            plugin, "load_cache", return_value={"version": 2, "scenes": {}},
        ), mock.patch.object(plugin, "save_cache"), mock.patch.object(
            plugin, "_save_scope_state",
        ), mock.patch.object(
            plugin, "_prepare_local_entry",
            return_value={
                "scene": scene,
                "scene_id": "10",
                "path": "/local.mp4",
                "duration": 60.0,
                "duration_ms": 60000,
                "early_hashes": [1, 2],
            },
        ), mock.patch.object(
            plugin, "e621_video_posts_page", side_effect=pages
        ):
            stats = plugin.match_stash_against_e621(
                stash,
                {"e621_pages_per_run": 0},
                {"dry_run": False, "pages_per_run": 0},
            )

        self.assertEqual(
            calls,
            [("webm", None), ("webm", 300), ("webm", 200), ("mp4", None)],
        )
        self.assertEqual(stats["local_videos_exhausted"], 1)
        self.assertEqual(stats["e621_posts_seen"], 2)

    def test_positive_page_limit_still_stops_and_saves_progress(self):
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
            "files": [{"path": "/local.mp4", "duration": 60.0}],
        }
        page = [{
            "id": 300,
            "file": {
                "ext": "webm",
                "url": "https://example/300.webm",
                "duration": 59.0,
            },
        }]
        with mock.patch.object(
            plugin, "eligible_local_scenes",
            return_value=([scene], {"eligible": 1, "organized_protected": 0}),
        ), mock.patch.object(
            plugin, "_scope", return_value=(None, "__all__", "all"),
        ), mock.patch.object(
            plugin, "_load_scope_state",
            return_value=({"scopes": {}}, {"completed_scene_ids": []}),
        ), mock.patch.object(
            plugin, "load_cache", return_value={"version": 2, "scenes": {}},
        ), mock.patch.object(plugin, "save_cache"), mock.patch.object(
            plugin, "_prepare_local_entry",
            return_value={
                "scene": scene,
                "scene_id": "10",
                "path": "/local.mp4",
                "duration": 60.0,
                "duration_ms": 60000,
                "early_hashes": [1, 2],
            },
        ), mock.patch.object(
            plugin, "e621_video_posts_page", return_value=page
        ) as pages, mock.patch.object(
            plugin, "_save_scope_state",
        ) as save:
            plugin.match_stash_against_e621(
                stash,
                {"e621_pages_per_run": 1},
                {"dry_run": False, "pages_per_run": 1},
            )

        self.assertEqual(pages.call_count, 1)
        self.assertEqual(save.call_args.kwargs["before_id"], 300)


if __name__ == "__main__":
    unittest.main()
