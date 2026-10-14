"""h3kit.node 行为基线：画布/时长换算、遗留 widget 值、面板状态恢复与节点契约。

``execute()`` 的提示词解析路径由 ``tests/test_panel_runtime.py`` 覆盖，端到端
错误路径由 ``scripts/test_allinone_execute.py`` 覆盖；这里补上两者都不涉及的
纯逻辑与 schema 契约（widget 顺序、输出名、节点 ID）。

需要 ComfyUI 运行时，定位与跳过逻辑见 ``tests/comfy_env.py``。
用法：``python -m pytest tests -o addopts='' -q``
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comfy_env  # noqa: E402
from comfy_env import COMFY, SKIP_REASON  # noqa: E402

if COMFY is not None:
    import h3kit.node as node

# widget 顺序是工作流存档的硬契约：改动会让旧工作流的 widgets_values 错位。
EXPECTED_WIDGET_ORDER = (
    "panel_mode", "text_encoder", "video_decoder", "audio_decoder", "frame_ratio", "pixel_budget",
    "clip_seconds", "prompt",
    "keyframe_open", "keyframe_close", "keyframe_audio",
    "still_1", "still_2", "still_3", "still_4", "still_5",
    "still_6", "still_7", "still_8", "still_9",
    "footage_1", "footage_2", "footage_3",
    "track_1", "track_2", "track_3",
    "task_hint", "audio_source", "audio_cleanup", "driving_track",
    "tag_guard", "asset_scale", "panel_state",
    "subtitle_free", "soundscape", "music",
    "sample_steps", "video_sigma_shift", "audio_sigma_shift", "sampler_id", "scheduler_id", "seed_value",
    "prompt_origin", "ai_idea", "ai_locale", "ai_task",
    "ai_expand", "ai_ambience", "ai_score", "ai_timeline", "ai_lock_camera",
    "ai_stabilize", "ai_subtitle_free", "ai_audio_guard", "ai_strict_check",
    "ai_platform", "ai_key", "ai_base_url", "ai_model_name", "ai_deadline",
    "upstream_prompt",
)
EXPECTED_OUTPUTS = ("frames", "audio", "video_latent", "report")


@unittest.skipIf(COMFY is None, SKIP_REASON)
class CanvasAndDurationTests(unittest.TestCase):
    def test_floor_to_32_rounds_to_the_nearest_grid_line(self):
        for value, expected in [(0, 32), (16, 32), (32, 32), (40, 32), (48, 64), (100, 96)]:
            with self.subTest(value=value):
                self.assertEqual(node._floor_to_32(value), expected)

    def test_known_aspect_ratios(self):
        self.assertEqual(node.resolve_canvas("16:9", 0.5), (960, 544))
        self.assertEqual(node.resolve_canvas("1:1", 0.5), (736, 736))
        self.assertEqual(node.resolve_canvas("21:9", 2.0), (2208, 960))

    def test_adaptive_ratio_falls_back_to_16_9(self):
        self.assertEqual(node.resolve_canvas("adaptive", 0.5, 1920, 1088), (960, 544))
        self.assertEqual(node.resolve_canvas("adaptive", 0.5), (960, 544))
        self.assertEqual(node.resolve_canvas("adaptive", 0.5, 0, 100), (960, 544))
        self.assertEqual(node.resolve_canvas("no_such_aspect", 0.5), (960, 544))

    def test_square_canvas_stays_square_after_rounding(self):
        width, height = node.resolve_canvas("1:1", 1.0)
        self.assertEqual(width, height)

    def test_canvas_stays_on_grid_and_within_limits(self):
        for frame_ratio in ("16:9", "9:16", "1:1", "21:9", "3:2"):
            for pixel_budget in (0.2, 0.5, 1.0, 2.0):
                with self.subTest(frame_ratio=frame_ratio, pixel_budget=pixel_budget):
                    width, height = node.resolve_canvas(frame_ratio, pixel_budget)
                    self.assertEqual(width % 32, 0)
                    self.assertEqual(height % 32, 0)
                    self.assertGreaterEqual(min(width, height), 32)
                    self.assertLessEqual(max(width, height), node.RESOLUTION_CEILING)

    def test_frame_length_snaps_onto_the_17n_plus_5_grid(self):
        for duration, expected in [(0.1, 5), (1, 39), (2, 56), (5, 124), (15, 362), (30, 736)]:
            with self.subTest(duration=duration):
                self.assertEqual(node.resolve_frame_length(duration), expected)
                self.assertEqual((node.resolve_frame_length(duration) - 5) % 17, 0)


@unittest.skipIf(COMFY is None, SKIP_REASON)
class LegacyValueTests(unittest.TestCase):
    def test_as_int_accepts_legacy_serialized_values(self):
        self.assertEqual(node._as_int(None, default=1), 1)
        self.assertEqual(node._as_int("", default=1), 1)
        self.assertEqual(node._as_int("(none)", default=1), 1)
        self.assertEqual(node._as_int("2.9"), 2)
        self.assertEqual(node._as_int("junk", default=7), 7)
        self.assertEqual(node._as_int(5, maximum=1), 1)
        self.assertEqual(node._as_int(-5, minimum=0), 0)

    def test_coerce_bool_handles_booleans_numbers_and_words(self):
        for value, expected in [("true", True), ("1", True), ("YES", True), ("on", True),
                                ("false", False), ("0", False), ("No", False), ("off", False),
                                (2, True), (0, False), (True, True), (False, False)]:
            with self.subTest(value=value):
                self.assertIs(node._coerce_bool(value, default=None), expected)

    def test_coerce_bool_keeps_the_default_for_unknown_values(self):
        for value in (None, "", "(none)", "maybe"):
            with self.subTest(value=value):
                self.assertTrue(node._coerce_bool(value, default=True))
                self.assertFalse(node._coerce_bool(value, default=False))

    def test_pick_default_model_prefers_then_falls_back(self):
        self.assertEqual(node._pick_default_model(["a", "b"], "b"), "b")
        self.assertEqual(node._pick_default_model(["a", "b"], "z"), "a")
        self.assertIsNone(node._pick_default_model([], "z"))

    def test_node_call_results_are_normalized_for_both_apis(self):
        payload = types.SimpleNamespace(result=[1, 2])
        self.assertEqual(node._first_output(payload), 1)
        self.assertEqual(node._all_outputs(payload), (1, 2))
        self.assertEqual(node._first_output((3, 4)), 3)
        self.assertEqual(node._all_outputs((3, 4)), (3, 4))
        self.assertEqual(node._first_output(5), 5)
        self.assertEqual(node._all_outputs(5), (5,))


@unittest.skipIf(COMFY is None, SKIP_REASON)
class PanelStateTests(unittest.TestCase):
    @staticmethod
    def _state(media):
        return json.dumps({"mode": "all_reference", "media": media})

    def test_first_visual_prefers_keyframes_in_text_mode(self):
        state = self._state([
            ["still_1", {"kind": "image", "name": "ref.png"}],
            ["keyframe_open", {"kind": "image", "name": "first.png"}],
        ])
        self.assertEqual(node._panel_first_visual(state, "text_keyframes"), "first.png")

    def test_first_visual_presents_videos_before_pictures_in_reference_mode(self):
        state = self._state([
            ["still_1", {"kind": "image", "name": "ref.png"}],
            ["footage_2", {"kind": "video", "name": "clip.mp4"}],
        ])
        self.assertEqual(node._panel_first_visual(state, "all_reference"), "clip.mp4")

    def test_first_visual_accepts_dict_shaped_and_skips_empty_entries(self):
        state = json.dumps({"media": {"keyframe_open": {"kind": "image", "name": "(none)"},
                                      "keyframe_close": {"kind": "image", "name": "last.png"}}})
        self.assertEqual(node._panel_first_visual(state, "text_keyframes"), "last.png")
        self.assertIsNone(node._panel_first_visual("not json", "text_keyframes"))
        self.assertIsNone(node._panel_first_visual(None, "text_keyframes"))
        self.assertIsNone(node._panel_first_visual("(none)", "text_keyframes"))
        self.assertIsNone(node._panel_first_visual(json.dumps({"media": 5}), "text_keyframes"))

    def test_muted_slots_only_covers_reference_videos(self):
        state = self._state([
            ["footage_1", {"kind": "video", "name": "a.mp4", "muted": True}],
            ["footage_2", {"kind": "video", "name": "b.mp4", "muted": False}],
            ["still_1", {"kind": "image", "name": "c.png", "muted": True}],
        ])
        self.assertEqual(node._panel_muted_slots(state), {"footage_1"})
        self.assertEqual(node._panel_muted_slots(None), set())
        self.assertEqual(node._panel_muted_slots(json.dumps({"media": 5})), set())

    def test_trim_ranges_only_covers_audio_with_a_valid_window(self):
        state = self._state([
            ["track_1", {"kind": "audio", "name": "a.wav", "trimStart": 1.5, "trimEnd": 3.0}],
            ["track_2", {"kind": "audio", "name": "b.wav", "trimEnd": 1.0}],
            ["track_3", {"kind": "audio", "name": "c.wav", "trimStart": -4, "trimEnd": 2}],
            ["still_1", {"kind": "image", "name": "d.png", "trimStart": 0, "trimEnd": 9}],
        ])
        self.assertEqual(node._panel_trim_ranges(state),
                         {"track_1": (1.5, 3.0), "track_2": (0.0, 1.0),
                          "track_3": (0.0, 2.0)})
        self.assertEqual(node._panel_trim_ranges(None), {})
        self.assertEqual(node._panel_trim_ranges(json.dumps({"media": 5})), {})


@unittest.skipIf(COMFY is None, SKIP_REASON)
class AudioSlicingTests(unittest.TestCase):
    @staticmethod
    def _audio(samples=1000, rate=8000):
        return {"waveform": torch.ones(1, 2, samples), "sample_rate": rate}

    def test_clip_audio_length_pads_a_short_track(self):
        clipped = node._clip_audio_length(self._audio(), 0.25)
        self.assertEqual(tuple(clipped["waveform"].shape), (1, 2, 2000))
        self.assertEqual(clipped["sample_rate"], 8000)
        self.assertEqual(float(clipped["waveform"][..., 1000:].sum()), 0.0)

    def test_clip_audio_length_truncates_a_long_track(self):
        clipped = node._clip_audio_length(self._audio(samples=8000), 0.25)
        self.assertEqual(tuple(clipped["waveform"].shape), (1, 2, 2000))

    def test_clip_audio_length_passes_through_none(self):
        self.assertIsNone(node._clip_audio_length(None, 1.0))

    def test_apply_sound_choice_slices_without_changing_the_rate(self):
        sliced = node._apply_sound_choice(self._audio(samples=8000), (0.1, 0.3))
        self.assertEqual(tuple(sliced["waveform"].shape), (1, 2, 1600))
        self.assertEqual(sliced["sample_rate"], 8000)

    def test_apply_sound_choice_is_a_no_op_without_a_selection(self):
        audio = self._audio()
        self.assertIs(node._apply_sound_choice(audio, None), audio)
        self.assertIs(node._apply_sound_choice(audio, ()), audio)
        self.assertIsNone(node._apply_sound_choice(None, (0.0, 1.0)))


@unittest.skipIf(COMFY is None, SKIP_REASON)
class VideoFramePickTests(unittest.TestCase):
    @staticmethod
    def _frames(count):
        return torch.arange(count, dtype=torch.float32).reshape(count, 1, 1, 1)

    def test_short_inputs_pass_through(self):
        self.assertIsNone(node._pick_video_frames(None, 24.0, 124))
        single = self._frames(1)
        self.assertIs(node._pick_video_frames(single, 24.0, 124), single)

    def test_target_frame_count_clips_a_long_reference(self):
        picked = node._pick_video_frames(self._frames(200), 24.0, 124)
        self.assertEqual(tuple(picked.shape), (124, 1, 1, 1))
        self.assertEqual(float(picked[0]), 0.0)
        self.assertEqual(float(picked[-1]), 123.0)

    def test_higher_source_fps_drops_frames_at_the_24fps_cadence(self):
        picked = node._pick_video_frames(self._frames(200), 48.0, 124)
        self.assertEqual(tuple(picked.shape), (100, 1, 1, 1))
        self.assertEqual(float(picked[0]), 0.0)
        self.assertEqual(float(picked[1]), 2.0)

    def test_lower_source_fps_repeats_frames_at_the_24fps_cadence(self):
        picked = node._pick_video_frames(self._frames(50), 12.0, 124)
        # 12fps 的 50 帧相当于 24fps 的 100 帧，每一帧输出两次
        self.assertEqual(tuple(picked.shape), (100, 1, 1, 1))
        self.assertEqual(float(picked[0]), 0.0)
        self.assertEqual(float(picked[1]), 0.0)
        self.assertEqual(float(picked[2]), 1.0)

    def test_unknown_source_fps_takes_the_leading_frames(self):
        picked = node._pick_video_frames(self._frames(100), 0.0, 12)
        self.assertEqual(tuple(picked.shape), (48, 1, 1, 1))
        self.assertEqual(float(picked[-1]), 47.0)


@unittest.skipIf(COMFY is None, SKIP_REASON)
class VisualSourceTests(unittest.TestCase):
    """自适应画幅取的是“第一张有效视觉素材”，顺序与面板展示一致。"""

    VIDEO_NAMES = ("v1.mp4", "", "")
    IMAGE_NAMES = ("i1.png", "i2.png", "", "", "", "", "", "", "")

    def _pick(self, panel_mode, serialized, first=None, last=None, refs=None, ref_videos=None,
              video_names=None, image_names=None):
        return node._pick_visual_source(
            panel_mode, serialized, "first.png", "last.png", first, last,
            refs or {}, ref_videos or {},
            self.VIDEO_NAMES if video_names is None else video_names,
            self.IMAGE_NAMES if image_names is None else image_names,
        )

    def test_keyframe_mode_follows_the_panel_choice(self):
        first, last = object(), object()
        self.assertIs(self._pick("text_keyframes", "first.png", first, last), first)
        self.assertIs(self._pick("text_keyframes", "last.png", first, last), last)
        self.assertIs(self._pick("text_keyframes", None, first, last), first)
        self.assertIs(self._pick("text_keyframes", None, None, last), last)
        self.assertIsNone(self._pick("text_keyframes", None))

    def test_reference_mode_prefers_videos_over_pictures(self):
        refs = {"still_1": "image-tensor"}
        ref_videos = {"footage_1": "video-tensor"}
        self.assertEqual(self._pick("all_reference", None, refs=refs, ref_videos=ref_videos),
                         "video-tensor")

    def test_reference_mode_honours_the_serialized_first_visual(self):
        refs = {"still_2": "image-tensor"}
        ref_videos = {"footage_1": "video-tensor"}
        picked = self._pick("all_reference", "i2.png", refs=refs, ref_videos=ref_videos)
        self.assertEqual(picked, "image-tensor")

    def test_reference_mode_falls_back_when_the_recorded_media_failed_to_load(self):
        # 面板记的是 footage_1，但那段视频没能解码：退回按顺序找第一张可用素材。
        refs = {"still_1": "image-tensor"}
        picked = self._pick("all_reference", "v1.mp4", refs=refs, ref_videos={})
        self.assertEqual(picked, "image-tensor")

    def test_reference_mode_without_any_visual_media(self):
        self.assertIsNone(self._pick("all_reference", "v1.mp4"))


@unittest.skipIf(COMFY is None, SKIP_REASON)
class DriveAudioTests(unittest.TestCase):
    def test_lock_source_promotes_ordinal_zero_in_reference_mode(self):
        ordinal, selected = node._select_drive_audio("all_reference", "lock_source", 0,
                                                     {}, {"track_1": "a"})
        self.assertEqual(ordinal, 1)
        self.assertEqual(selected, "a")

    def test_video_soundtracks_come_before_standalone_audios(self):
        video_audio = {"footage_audio_1": "clip", "footage_audio_3": "third"}
        audios = {"track_1": "a", "track_2": "b"}
        self.assertEqual(
            node._select_drive_audio("all_reference", "native", 2, video_audio, audios),
            (2, "third"),
        )
        self.assertEqual(
            node._select_drive_audio("all_reference", "native", 3, video_audio, audios),
            (3, "a"),
        )

    def test_out_of_range_ordinal_selects_nothing(self):
        ordinal, selected = node._select_drive_audio("all_reference", "native", 5, {}, {"track_1": "a"})
        self.assertEqual(ordinal, 5)
        self.assertIsNone(selected)

    def test_keyframe_mode_never_uses_a_reference_track(self):
        ordinal, selected = node._select_drive_audio("text_keyframes", "lock_source", 0, {}, {"track_1": "a"})
        self.assertEqual(ordinal, 0)
        self.assertIsNone(selected)


@unittest.skipIf(COMFY is None, SKIP_REASON)
class SchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = node.AllInOneNode.define_schema()

    @staticmethod
    def _name(item):
        return getattr(item, "id", None) or getattr(item, "name", None)

    def test_node_identity(self):
        self.assertEqual(self.schema.node_id, "MiniMaxH3IntegrationRH")
        self.assertEqual(self.schema.category, "RH/MiniMax H3 Integration")
        self.assertIn("MiniMax H3", self.schema.display_name)

    def test_widget_order_is_stable(self):
        # model 是连线输入，不占 widgets_values 槽位
        widgets = [self._name(item) for item in self.schema.inputs if self._name(item) != "model"]
        self.assertEqual(tuple(widgets), EXPECTED_WIDGET_ORDER)

    def test_output_names(self):
        self.assertEqual(tuple(self._name(item) for item in self.schema.outputs), EXPECTED_OUTPUTS)

    def test_prompt_override_stays_last(self):
        # forceInput 的「幻影槽位」必须固定在末尾，否则具名 widget 会整体错位（A4）
        last = self.schema.inputs[-1]
        self.assertEqual(self._name(last), "upstream_prompt")

    def test_sampler_and_scheduler_defaults_match_the_runtime(self):
        inputs = {self._name(item): item for item in self.schema.inputs}
        self.assertEqual(inputs["sampler_id"].default, node.PRIMARY_SAMPLER)
        self.assertEqual(inputs["scheduler_id"].default, node.PRIMARY_SCHEDULER)
        self.assertEqual(tuple(inputs["sampler_id"].options), tuple(node.SAMPLER_CHOICES))
        self.assertEqual(tuple(inputs["scheduler_id"].options), tuple(node.SCHEDULER_CHOICES))


if __name__ == "__main__":
    unittest.main()
