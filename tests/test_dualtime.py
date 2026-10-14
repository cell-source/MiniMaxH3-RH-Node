"""h3kit.dualtime 行为基线：双时钟更新数学、混合布局修正与采样器补丁契约。

这些用例需要一个 ComfyUI 运行时（``comfy.samplers`` / ``comfy.model_sampling``），
定位与跳过逻辑见 ``tests/comfy_env.py``。

采样器补丁是改动风险最高的部分，因此这里用桩模型把 ``build_dual_clock_setup``
的对外契约（打了哪些补丁、sampler 上挂了什么元数据、sigmas 从哪来）固定下来。
用法：``python -m pytest tests -o addopts='' -q``
"""
from __future__ import annotations

import math
import sys
import types
import unittest
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comfy_env  # noqa: E402
from comfy_env import COMFY, SKIP_REASON  # noqa: E402

if COMFY is not None:
    import comfy.nested_tensor

    import h3kit.dualtime as dt
    import h3kit.geometry as geo


@unittest.skipIf(COMFY is None, SKIP_REASON)
class SigmaMathTests(unittest.TestCase):
    def test_sigma_shift_matches_the_flow_formula(self):
        self.assertAlmostEqual(dt.sigma_shift(1.0, 3.0), 1.0)
        self.assertAlmostEqual(dt.sigma_shift(0.5, 1.0), 0.5)
        self.assertAlmostEqual(dt.sigma_shift(0.5, 3.0), 3 * 0.5 / (1 + 2 * 0.5))
        self.assertAlmostEqual(dt.sigma_shift(0.0, 12.0), 0.0)

    def test_shift_between_two_clocks_round_trips(self):
        for sigma in (0.0, 0.25, 0.5, 0.75, 1.0):
            with self.subTest(sigma=sigma):
                self.assertAlmostEqual(dt.flow_shift_sigma(sigma, 4.0, 4.0), sigma, places=6)

    def test_shift_between_two_clocks_is_monotonic(self):
        values = [dt.flow_shift_sigma(s, 12.0, 3.0) for s in (0.0, 0.5, 1.0)]
        self.assertEqual(values[0], 0.0)
        self.assertEqual(values[-1], 1.0)
        self.assertTrue(values[0] <= values[1] <= values[2])

    def test_slope_is_one_for_identical_clocks(self):
        for sigma in (0.0, 0.3, 1.0):
            with self.subTest(sigma=sigma):
                self.assertAlmostEqual(dt.flow_shift_slope(sigma, 5.0, 5.0), 1.0, places=6)

    def test_flow_sigmas_spans_one_to_zero(self):
        sigmas = dt.flow_sigmas(4, 12.0)
        self.assertEqual(tuple(sigmas.shape), (5,))
        self.assertAlmostEqual(float(sigmas[0]), 1.0)
        self.assertAlmostEqual(float(sigmas[-1]), 0.0)

    def test_audio_step_gain_covers_all_three_branches(self):
        self.assertAlmostEqual(dt._audio_step_gain(0.5, 0.25, 2.0, None, False), -0.125)
        self.assertAlmostEqual(dt._audio_step_gain(0.5, 0.25, 2.0, None, True), -0.25)
        blended = dt._audio_step_gain(0.5, 0.25, 2.0, 0.4, True)
        self.assertAlmostEqual(blended, -0.5 + 0.4 * (-0.25 + 0.5))


@unittest.skipIf(COMFY is None, SKIP_REASON)
class HelperTests(unittest.TestCase):
    def test_frames_from_latent_t_uses_the_minimax_frame_grid(self):
        self.assertEqual([dt._frames_from_latent_t(n) for n in (1, 2, 3, 5, 6)],
                         [1, 5, 9, 17, 18])
        with self.assertRaises(RuntimeError):
            dt._frames_from_latent_t(0)

    def test_model_has_raw_velocity_looks_at_the_base_model(self):
        raw = types.SimpleNamespace(model=types.SimpleNamespace(audio_scale=lambda: 1.0))
        plain = types.SimpleNamespace(model=types.SimpleNamespace())
        self.assertTrue(dt.model_has_raw_velocity(raw))
        self.assertFalse(dt.model_has_raw_velocity(plain))
        self.assertFalse(dt.model_has_raw_velocity(types.SimpleNamespace()))

    def test_clear_locked_inpaint_returns_the_latent_untouched(self):
        latent = torch.ones(1, 2, 3)
        self.assertIs(dt._clear_locked_inpaint(None, 0.5, torch.zeros(1), latent), latent)

    def test_has_audio_lock_detects_partially_denoised_audio(self):
        video = torch.ones(1, 24, 2, 4, 4)
        audio = torch.ones(1, 32, 2, 5)
        locked = comfy.nested_tensor.NestedTensor((video, torch.zeros_like(audio)))
        self.assertTrue(dt._has_audio_lock({"noise_mask": locked}))
        untouched = comfy.nested_tensor.NestedTensor((video, audio))
        self.assertFalse(dt._has_audio_lock({"noise_mask": untouched}))
        self.assertFalse(dt._has_audio_lock({}))
        self.assertFalse(dt._has_audio_lock({"noise_mask": torch.ones(1, 2)}))

    def test_hybrid_ref_offsets(self):
        self.assertEqual(dt._hybrid_ref_offset({"kind": dt.HYBRID_MARKER}), 0.0)
        self.assertEqual(dt._hybrid_ref_offset({"kind": "image"}), 1.0)
        self.assertEqual(dt._hybrid_ref_offset({"kind": "audio", "track_t": 7}), 7.0)
        # 视频按"音频长度 vs 像素帧数"取较大者
        video = dt._hybrid_ref_offset({"kind": "video", "latent_t": 5, "track_t": 0})
        self.assertAlmostEqual(video, (5.0 / 3.0) * dt._frames_from_latent_t(5))
        both = dt._hybrid_ref_offset({"kind": "video_audio", "latent_t": 1, "track_t": 9})
        self.assertEqual(both, 9.0)
        with self.assertRaises(RuntimeError):
            dt._hybrid_ref_offset({"kind": "unexpected"})

    def test_scale_keyframe_latent_resizes_each_temporal_slice(self):
        latent = torch.zeros(1, 24, 2, 8, 8)
        self.assertIs(dt._scale_keyframe_latent(latent, 8, 8), latent)
        resized = dt._scale_keyframe_latent(latent, 4, 6)
        self.assertEqual(tuple(resized.shape), (1, 24, 2, 4, 6))
        not_a_video_latent = torch.zeros(1, 2)
        self.assertIs(dt._scale_keyframe_latent(not_a_video_latent, 4, 4), not_a_video_latent)

    def test_rebuild_keyframe_reuses_matching_latents(self):
        item = {"latent": torch.zeros(1, 24, 2, 4, 4), "source_image": torch.zeros(1, 8, 8, 3)}
        self.assertIsNone(dt._rebuild_keyframe(item, 4, 4))

    def test_rebuild_keyframe_re_encodes_from_the_source_image(self):
        class StubVae:
            def __init__(self):
                self.seen = None

            def encode(self, image):
                self.seen = tuple(image.shape)
                return torch.zeros(1, 24, 2, 6, 8)

        vae = StubVae()
        item = {"latent": torch.zeros(1, 24, 2, 4, 4),
                "source_image": torch.zeros(1, 8, 8, 3), "video_vae": vae}
        rebuilt = dt._rebuild_keyframe(item, 6, 8)
        self.assertEqual(tuple(rebuilt.shape), (1, 24, 2, 6, 8))
        self.assertEqual(vae.seen, (1, 6 * 16, 8 * 16, 3))

    def test_rebuild_keyframe_gives_up_without_a_source_image(self):
        self.assertIsNone(dt._rebuild_keyframe({"latent": torch.zeros(1, 24, 2, 4, 4)}, 6, 8))
        self.assertIsNone(dt._rebuild_keyframe({"source_image": torch.zeros(1, 8, 8, 3)}, 6, 8))
        self.assertIsNone(dt._rebuild_keyframe("not a dict", 6, 8))

    def test_build_sigmas_prefers_the_patched_scheduler(self):
        sigmas = dt._build_sigmas(None, dt.PRIMARY_SCHEDULER, 4, 12.0)
        self.assertTrue(torch.equal(sigmas, dt.flow_sigmas(4, 12.0)))
        with self.assertRaises(ValueError):
            dt._build_sigmas(None, "no_such_scheduler", 4, 12.0)
        native = dt._build_sigmas(dt.TwoClockFlowSampling(None), "normal", 4, 12.0)
        self.assertEqual(native.shape[-1], 5)


@unittest.skipIf(COMFY is None, SKIP_REASON)
class HybridLayoutTests(unittest.TestCase):
    @staticmethod
    def _payload(latent_t=2, cond_segments=2, rows=6):
        layout = types.SimpleNamespace(
            signature=(10, latent_t),
            segments=[(index, index + 1, "cond") for index in range(cond_segments)],
            position_ids=torch.zeros(rows, 2),
        )
        return {"minimax_payload": types.SimpleNamespace(cond={"layout": layout})}, layout

    def _keyframes(self, frame_count=124):
        return {
            "minimax_keyframes": [{"resolved_frame_index": 0}, {"resolved_frame_index": frame_count - 1}],
            "minimax_refs": [{"kind": "image"}],
            "minimax_frame_count": frame_count,
        }

    def test_first_and_last_keyframes_are_offset_onto_the_target_timeline(self):
        out, layout = self._payload()
        kwargs = self._keyframes()
        self.assertIs(dt.fix_hybrid_layout(out, kwargs), out)
        frame_rescale = float(getattr(dt.minimax_model, "FRAME_RESCALE", 5.0 / 3.0))
        span = frame_rescale * dt._frames_from_latent_t(2)
        self.assertAlmostEqual(float(layout.position_ids[0, 0]), 10.0 + 1.0, places=5)
        self.assertAlmostEqual(
            float(layout.position_ids[1, 0]), 10.0 + span - frame_rescale + 1.0, places=5
        )

    def test_without_keyframes_or_refs_the_payload_is_untouched(self):
        out, layout = self._payload()
        self.assertIs(dt.fix_hybrid_layout(out, {}), out)
        self.assertEqual(torch.count_nonzero(layout.position_ids), 0)
        only_refs = {"minimax_refs": [{"kind": "image"}]}
        self.assertIs(dt.fix_hybrid_layout(out, only_refs), out)

    def test_rejects_a_payload_that_cannot_be_patched(self):
        kwargs = self._keyframes()
        with self.assertRaises(RuntimeError):
            dt.fix_hybrid_layout({}, kwargs)
        out, layout = self._payload()
        out["minimax_payload"] = types.SimpleNamespace(cond={})
        with self.assertRaises(RuntimeError):
            dt.fix_hybrid_layout(out, kwargs)

    def test_rejects_a_missing_frame_count_or_mismatched_keyframes(self):
        out, _layout = self._payload()
        kwargs = self._keyframes()
        del kwargs["minimax_frame_count"]
        with self.assertRaises(RuntimeError):
            dt.fix_hybrid_layout(out, kwargs)

        out, _layout = self._payload(cond_segments=1)
        with self.assertRaises(RuntimeError):
            dt.fix_hybrid_layout(out, self._keyframes())

    def test_rejects_keyframes_that_are_neither_first_nor_last(self):
        out, _layout = self._payload()
        kwargs = self._keyframes()
        kwargs["minimax_keyframes"] = [{"resolved_frame_index": 0}, {"resolved_frame_index": 7}]
        with self.assertRaises(RuntimeError):
            dt.fix_hybrid_layout(out, kwargs)


@unittest.skipIf(COMFY is None, SKIP_REASON)
class DualClockEulerTests(unittest.TestCase):
    """零去噪器 + 双时钟同参时，两路都应严格乘上 sigma 比值。"""

    PACKED, AUDIO = 10, 4

    def _run(self, sigmas, model=None, callback=None, mask=None, raw=True, audio=None):
        x = torch.ones(1, 1, self.PACKED)
        extra = {} if mask is None else {"denoise_mask": mask}
        if model is None:
            def model(inp, sigma, **kwargs):
                return torch.zeros_like(inp)
        return dt.run_dual_clock_euler(
            model, x, sigmas, extra_args=extra, callback=callback, disable=None,
            audio_values=self.AUDIO if audio is None else audio,
            video_sigma_shift=4.0, audio_sigma_shift=4.0, audio_velocity_is_raw=raw,
        )

    def test_both_streams_follow_the_sigma_ratio(self):
        out = self._run(torch.tensor([1.0, 0.5]))
        self.assertTrue(torch.allclose(out, torch.full((1, 1, self.PACKED), 0.5)))

    def test_multiple_steps_compound(self):
        out = self._run(torch.tensor([1.0, 0.5, 0.25]))
        self.assertTrue(torch.allclose(out, torch.full((1, 1, self.PACKED), 0.25)))

    def test_rejects_a_packed_latent_shorter_than_the_audio_stream(self):
        with self.assertRaises(ValueError):
            self._run(torch.tensor([1.0, 0.5]), audio=self.PACKED)

    def test_rejects_a_denoise_mask_of_the_wrong_width(self):
        with self.assertRaises(ValueError):
            self._run(torch.tensor([1.0, 0.5]), mask=torch.ones(1, 1, 3))

    def test_full_mask_behaves_like_no_mask(self):
        baseline = self._run(torch.tensor([1.0, 0.5]))
        masked = self._run(torch.tensor([1.0, 0.5]), mask=torch.ones(1, 1, self.PACKED))
        self.assertTrue(torch.allclose(baseline, masked))

    def test_callback_receives_the_endpoint_correction(self):
        seen = []
        self._run(torch.tensor([1.0, 0.5]), callback=lambda info: seen.append(info))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["i"], 0)
        self.assertAlmostEqual(float(seen[0]["sigma"]), 1.0)
        self.assertAlmostEqual(float(seen[0]["sigma_hat"]), 1.0)
        # 音频端点的"x0 预测"被修正成与音频时钟一致：零去噪器下回到 0
        self.assertEqual(torch.count_nonzero(seen[0]["denoised"][..., self.AUDIO:]), 0)


class _StubPatchModel:
    """记录 add_object_patch 调用，供 build_dual_clock_setup 的契约断言使用。"""

    def __init__(self, sampling, extras=None):
        self._sampling = sampling
        self._extras = extras or {}
        self.model = types.SimpleNamespace(model_config=types.SimpleNamespace(sampling_settings={}))
        self.model_options = {}
        self.patches = []

    def clone(self):
        clone = _StubPatchModel(self._sampling, self._extras)
        clone.model = self.model
        clone.model_options = {key: value for key, value in self.model_options.items()}
        clone.origin = self
        return clone

    def get_model_object(self, name):
        if name == "model_sampling":
            return self._sampling
        return self._extras.get(name)

    def add_object_patch(self, name, value):
        self.patches.append((name, value))


@unittest.skipIf(COMFY is None, SKIP_REASON)
class DualClockSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.latent, _ = geo.blank_av_latent(1920, 1088, 124)

    def _setup(self, **overrides):
        kwargs = dict(model=_StubPatchModel(types.SimpleNamespace(noise_scale=1.0)),
                      av_latent=self.latent, sample_steps=4, video_sigma_shift=12.0, audio_sigma_shift=3.0)
        kwargs.update(overrides)
        return dt.build_dual_clock_setup(**kwargs)

    def test_returns_the_patched_model_sampler_and_sigmas(self):
        model, sampler, sigmas = self._setup()
        self.assertTrue(hasattr(model, "patches"))
        # 采样模型与首/尾帧条件钩子都要挂上；锁定音轨时才追加 inpaint 补丁
        self.assertEqual([name for name, _ in model.patches], ["model_sampling", "extra_conds"])
        self.assertTrue(torch.equal(sigmas, dt.flow_sigmas(4, 12.0)))
        self.assertIs(model.origin.model, model.model)

    def test_sampler_carries_the_dual_clock_metadata(self):
        _model, sampler, _sigmas = self._setup()
        self.assertTrue(getattr(sampler, "_h3_dual_clock"))
        self.assertAlmostEqual(sampler._h3_shift_video, 12.0)
        self.assertAlmostEqual(sampler._h3_shift_audio, 3.0)
        self.assertFalse(sampler._h3_audio_velocity_is_raw)
        self.assertEqual(sampler.sampler_function.__name__, "run_dual_clock_euler")

    def test_locked_audio_also_patches_the_inpaint_entry(self):
        latent = geo.swap_audio_latent(self.latent, torch.zeros(1, 32, 2, 9), 0.0)
        model, _sampler, _sigmas = self._setup(av_latent=latent)
        self.assertIn("scale_latent_inpaint", [name for name, _ in model.patches])

    def test_shift_options_are_exposed_to_the_model(self):
        model, _sampler, _sigmas = self._setup()
        options = model.model_options["transformer_options"]
        self.assertAlmostEqual(options["h3kit_sigma_shift_video"], 12.0)
        self.assertAlmostEqual(options["h3kit_sigma_shift_audio"], 3.0)

    def test_rejects_unknown_samplers_and_wrong_channels(self):
        with self.assertRaises(ValueError):
            self._setup(sampler_id="no_such_sampler")
        bad = {"samples": comfy.nested_tensor.NestedTensor(
            (torch.zeros(1, 12, 2, 4, 4), torch.zeros(1, 32, 2, 5)))}
        with self.assertRaises(ValueError):
            self._setup(av_latent=bad)


if __name__ == "__main__":
    unittest.main()
