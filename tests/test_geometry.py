"""h3kit.geometry 行为基线：帧数网格、画布拟合、AV 潜空间与音频张量工具。

这些用例是"重写前后必须完全一致"的规格说明。它们需要 ComfyUI 运行时
（导入 comfy.utils / comfy.model_management），定位与跳过逻辑见 ``tests/comfy_env.py``：
- 优先读环境变量 ``COMFYUI_PATH``；
- 否则尝试同级目录 ``../ComfyUI``；
- 都不可用时整体 skip（不影响纯离线测试）。

用法：``python -m pytest tests -o addopts='' -q``
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comfy_env  # noqa: E402
from comfy_env import COMFY, SKIP_REASON  # noqa: E402

if COMFY is not None:
    import comfy.nested_tensor
    import h3kit.geometry as geo


@unittest.skipIf(COMFY is None, "ComfyUI checkout not found (set COMFYUI_PATH)")
class FrameGridTests(unittest.TestCase):
    def test_snap_frames_up_hits_the_17n_plus_5_grid(self):
        for requested, expected in [(5, 5), (6, 22), (21, 22), (22, 22), (23, 39),
                                    (100, 107), (124, 124), (362, 362)]:
            with self.subTest(requested=requested):
                self.assertEqual(geo.snap_frames_up(requested), expected)

    def test_snap_frames_up_never_goes_below_five(self):
        for requested in (0, 1, -10, 4):
            with self.subTest(requested=requested):
                self.assertEqual(geo.snap_frames_up(requested), 5)

    def test_snap_frames_down_uses_the_same_grid(self):
        for requested, expected in [(124, 124), (130, 124), (139, 124), (140, 124),
                                    (145, 141), (362, 362)]:
            with self.subTest(requested=requested):
                self.assertEqual(geo.snap_frames_down(requested), expected)

    def test_latent_time_len(self):
        for frames, expected in [(5, 2), (22, 7), (39, 12), (124, 37), (362, 107)]:
            with self.subTest(frames=frames):
                self.assertEqual(geo.latent_time_len(frames), expected)

    def test_time_shapes_matches_the_video_and_audio_clocks(self):
        self.assertEqual(geo.time_shapes(124), (124, 37, 207))
        self.assertEqual(geo.time_shapes(1), (5, 2, 8))
        self.assertEqual(geo.time_shapes(362), (362, 107, 603))


@unittest.skipIf(COMFY is None, "ComfyUI checkout not found (set COMFYUI_PATH)")
class CanvasTests(unittest.TestCase):
    def test_fit_canvas_landscape_and_portrait(self):
        self.assertEqual(geo.fit_canvas(1920, 1088), (1344, 768))
        self.assertEqual(geo.fit_canvas(1088, 1920), (768, 1344))
        self.assertEqual(geo.fit_canvas(1, 1), (768, 768))

    def test_fit_canvas_respects_the_pixel_budget(self):
        self.assertEqual(geo.fit_canvas(10000, 100), (14464, 160))

    def test_fit_canvas_edges_stay_on_the_32_grid(self):
        for width, height in [(1920, 1088), (1088, 1920), (2048, 858), (17, 901)]:
            with self.subTest(size=(width, height)):
                out_w, out_h = geo.fit_canvas(width, height)
                self.assertEqual(out_w % 32, 0)
                self.assertEqual(out_h % 32, 0)
                self.assertGreaterEqual(out_w, 32)
                self.assertGreaterEqual(out_h, 32)

    def test_fit_canvas_rejects_non_positive_sides(self):
        for width, height in [(0, 100), (100, 0), (-1, 100)]:
            with self.subTest(size=(width, height)):
                with self.assertRaises(ValueError):
                    geo.fit_canvas(width, height)


@unittest.skipIf(COMFY is None, "ComfyUI checkout not found (set COMFYUI_PATH)")
class LatentTests(unittest.TestCase):
    def test_blank_av_latent_shapes(self):
        latent, frames = geo.blank_av_latent(1920, 1088, 124)
        video, audio = geo.av_streams(latent)
        self.assertEqual(frames, 124)
        self.assertEqual(tuple(video.shape), (1, 24, 37, 1088 // 16, 1920 // 16))
        self.assertEqual(tuple(audio.shape), (1, 32, 2, 207))
        self.assertEqual(torch.count_nonzero(video), 0)
        self.assertEqual(torch.count_nonzero(audio), 0)

    def test_blank_av_latent_rejects_off_grid_sizes(self):
        with self.assertRaises(ValueError):
            geo.blank_av_latent(1000, 1088, 124)

    def test_av_streams_rejects_malformed_input(self):
        with self.assertRaises(ValueError):
            geo.av_streams([])
        with self.assertRaises(ValueError):
            geo.av_streams({"samples": torch.zeros(1, 2, 3)})
        nested = comfy.nested_tensor.NestedTensor((torch.zeros(1, 24, 2, 4, 4),))
        with self.assertRaises(ValueError):
            geo.av_streams({"samples": nested})
        batched = comfy.nested_tensor.NestedTensor(
            (torch.zeros(2, 24, 2, 4, 4), torch.zeros(2, 32, 2, 5))
        )
        with self.assertRaises(ValueError):
            geo.av_streams({"samples": batched})

    def test_separate_masks_handles_every_layout(self):
        video = torch.zeros(1, 24, 2, 4, 4)
        audio = torch.zeros(1, 32, 2, 5)
        self.assertEqual(geo.separate_masks({"samples": video}, video, audio), (None, None))
        nested = comfy.nested_tensor.NestedTensor((video, audio))
        mask_video, mask_audio = geo.separate_masks({"noise_mask": nested}, video, audio)
        self.assertIs(mask_video, video)
        self.assertIs(mask_audio, audio)
        legacy = torch.ones(1, 24, 2, 4, 4)
        self.assertEqual(geo.separate_masks({"noise_mask": legacy}, video, audio), (legacy, None))
        with self.assertRaises(ValueError):
            geo.separate_masks({"noise_mask": [1, 2]}, video, audio)

    def test_swap_audio_latent_rewrites_samples_and_mask(self):
        latent, _ = geo.blank_av_latent(1920, 1088, 124)
        _, template = geo.av_streams(latent)
        replacement = torch.ones_like(template)[..., : template.shape[-1] - 3]
        output = geo.swap_audio_latent(latent, replacement, 0.4)
        video, fitted = geo.av_streams(output)
        self.assertEqual(tuple(fitted.shape), tuple(template.shape))
        self.assertTrue(torch.all(fitted[..., : replacement.shape[-1]] == 1))
        self.assertTrue(torch.all(fitted[..., replacement.shape[-1]:] == 0))
        mask_video, mask_audio = output["noise_mask"].unbind()
        self.assertEqual(torch.count_nonzero(mask_video == 1), mask_video.numel())
        self.assertEqual(torch.count_nonzero(mask_audio == 0.4), mask_audio.numel())


@unittest.skipIf(COMFY is None, "ComfyUI checkout not found (set COMFYUI_PATH)")
class AudioTensorTests(unittest.TestCase):
    @staticmethod
    def _waveform(samples=16000, rate=16000, channels=2):
        return {"waveform": torch.zeros(1, channels, samples), "sample_rate": rate}

    def test_match_audio_latent_resizes_the_time_axis(self):
        template = torch.zeros(1, 8, 2, 10)
        long_one = torch.ones(1, 8, 2, 14)
        self.assertEqual(tuple(geo.match_audio_latent(long_one, template).shape), (1, 8, 2, 10))
        short_one = torch.ones(1, 8, 2, 6)
        padded = geo.match_audio_latent(short_one, template)
        self.assertEqual(tuple(padded.shape), (1, 8, 2, 10))
        self.assertEqual(torch.count_nonzero(padded[..., 6:]), 0)

    def test_match_audio_latent_expands_a_single_batch(self):
        template = torch.zeros(3, 8, 2, 10)
        expanded = geo.match_audio_latent(torch.ones(1, 8, 2, 10), template)
        self.assertEqual(tuple(expanded.shape), (3, 8, 2, 10))

    def test_match_audio_latent_rejects_mismatches(self):
        with self.assertRaises(ValueError):
            geo.match_audio_latent(torch.zeros(1, 8, 10), torch.zeros(1, 8, 2, 10))
        with self.assertRaises(ValueError):
            geo.match_audio_latent(torch.zeros(1, 4, 2, 10), torch.zeros(1, 8, 2, 10))
        with self.assertRaises(ValueError):
            geo.match_audio_latent(torch.zeros(3, 8, 2, 10), torch.zeros(2, 8, 2, 10))

    def test_check_waveform_accepts_a_single_batch(self):
        waveform, rate = geo.check_waveform(self._waveform())
        self.assertEqual(tuple(waveform.shape), (1, 2, 16000))
        self.assertEqual(rate, 16000)

    def test_check_waveform_rejects_bad_values(self):
        cases = [
            ("not a dict", []),
            ("missing fields", {}),
            ("wrong rank", {"waveform": torch.zeros(2, 2), "sample_rate": 16000}),
            ("empty", {"waveform": torch.zeros(1, 2, 0), "sample_rate": 16000}),
            ("batched", {"waveform": torch.zeros(2, 2, 100), "sample_rate": 16000}),
        ]
        for label, value in cases:
            with self.subTest(case=label), self.assertRaises(ValueError):
                geo.check_waveform(value)

    def test_encode_waveform_resamples_to_the_vae_rate(self):
        seen = {}

        class StubVae:
            audio_sample_rate = 32000

            def encode(self, waveform):
                seen["shape"] = tuple(waveform.shape)
                return torch.zeros(1, 8, 2, 20)

        latent = geo.encode_waveform(StubVae(), self._waveform(samples=16000, rate=16000))
        self.assertEqual(tuple(latent.shape), (1, 8, 2, 20))
        self.assertEqual(seen["shape"], (1, 32000, 2))

    def test_encode_waveform_rejects_a_wrong_latent_rank(self):
        class StubVae:
            audio_sample_rate = 16000

            def encode(self, waveform):
                return torch.zeros(1, 8, 20)

        with self.assertRaises(ValueError):
            geo.encode_waveform(StubVae(), self._waveform())


@unittest.skipIf(COMFY is None, "ComfyUI checkout not found (set COMFYUI_PATH)")
class DynamicSlotTests(unittest.TestCase):
    def test_ordered_dynamic_items_sorts_by_trailing_number(self):
        values = {"still_2": "b", "still_1": "a", "still_10": "j"}
        self.assertEqual(geo.ordered_dynamic_items(values),
                         [(1, "a"), (2, "b"), (10, "j")])

    def test_ordered_dynamic_items_falls_back_for_unparseable_keys(self):
        self.assertEqual(geo.ordered_dynamic_items({"misc_slot": "m", "still_10": "j"}),
                         [(10, "j"), (2, "m")])

    def test_ordered_dynamic_items_skips_empty_values(self):
        self.assertEqual(geo.ordered_dynamic_items({"still_1": None}), [])
        self.assertEqual(geo.ordered_dynamic_items(None), [])
        self.assertEqual(geo.ordered_dynamic_values({}), [])

    def test_ordered_dynamic_values_drops_the_ordinals(self):
        self.assertEqual(geo.ordered_dynamic_values({"still_2": "b", "still_1": "a"}),
                         ["a", "b"])


@unittest.skipIf(COMFY is None, "ComfyUI checkout not found (set COMFYUI_PATH)")
class ImageScaleTests(unittest.TestCase):
    def test_scale_image_returns_bhwc_rgb(self):
        image = torch.rand(1, 8, 8, 4)
        scaled = geo.scale_image(image, 16, 4)
        self.assertEqual(tuple(scaled.shape), (1, 4, 16, 3))

    def test_scale_image_rejects_a_non_image_tensor(self):
        with self.assertRaises(ValueError):
            geo.scale_image(torch.zeros(8, 8, 3), 16, 16)


if __name__ == "__main__":
    unittest.main()
