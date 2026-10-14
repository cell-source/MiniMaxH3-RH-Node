"""h3kit.assembly 行为基线：任务类型判定、参考素材缩放、条件装配。

条件装配用桩 clip / VAE 跑通全链路，重点锁定：
- 返回值的结构与报告文本；
- ``conditioning`` 上写入的 ``minimax_keyframes`` / ``minimax_refs`` 契约；
- 音频模式对潜空间噪声掩码的影响；
- 各条错误路径。

需要 ComfyUI 运行时，定位与跳过逻辑见 ``tests/comfy_env.py``。
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
    import h3kit.assembly as asm

WIDTH, HEIGHT, LENGTH = 1344, 768, 124


class _StubClip:
    """记录每次 tokenize 的关键字参数，并返回最小可用的 conditioning 结构。"""

    def __init__(self):
        self.tokenize_calls = []
        self.encoded_tokens = None

    def tokenize(self, prompt, **kwargs):
        self.tokenize_calls.append((prompt, kwargs))
        return {"prompt": prompt, "kwargs": kwargs}

    def encode_from_tokens_scheduled(self, tokens):
        self.encoded_tokens = tokens
        return [[torch.zeros(1), {"token_count": 1}]]


class _StubVideoVae:
    def encode(self, image_or_frames):
        return torch.zeros(1, 24, 1, 8, 8)


class _StubAudioVae:
    audio_sample_rate = 16000

    def encode(self, waveform):
        return torch.zeros(1, 32, 2, 24)


def _audio(level=0.1, samples=16000, rate=16000):
    return {"waveform": torch.full((1, 2, samples), float(level)), "sample_rate": rate}


def _image(frames=1, size=64):
    return torch.rand(frames, size, size, 3)


def _conditions(**overrides):
    payload = dict(
        clip=_StubClip(),
        video_vae=_StubVideoVae(),
        audio_vae=_StubAudioVae(),
        prompt="integrated_multimodal_description: [Shot 1] 女子站着。\n\n"
               "overall_soundscape: N/A\n\nnon_diegetic_music: N/A",
        width=WIDTH,
        height=HEIGHT,
        length=LENGTH,
    )
    payload.update(overrides)
    return payload


@unittest.skipIf(COMFY is None, SKIP_REASON)
class TaskTypeTests(unittest.TestCase):
    def test_auto_infers_from_the_connected_media(self):
        cases = [
            (False, False, False, "t2va"),
            (True, False, False, "i2va"),
            (False, True, False, "l2va"),
            (True, True, False, "fl2va"),
            (False, False, True, "ref2va"),
            (True, False, True, "hybrid"),
            (False, True, True, "hybrid"),
        ]
        for first, last, refs, expected in cases:
            with self.subTest(first=first, last=last, refs=refs):
                self.assertEqual(
                    asm.pick_task_type("auto", _image() if first else None,
                                       _image() if last else None, refs),
                    expected,
                )

    def test_explicit_task_accepts_a_matching_wiring(self):
        self.assertEqual(asm.pick_task_type("i2va", _image(), None, False), "i2va")
        self.assertEqual(asm.pick_task_type("FL2VA", _image(), _image(), False), "fl2va")
        self.assertEqual(asm.pick_task_type("ref2va", None, None, True), "ref2va")
        self.assertEqual(asm.pick_task_type("hybrid", _image(), None, True), "hybrid")

    def test_explicit_task_rejects_a_mismatched_wiring(self):
        with self.assertRaises(ValueError):
            asm.pick_task_type("i2va", None, None, False)
        with self.assertRaises(ValueError):
            asm.pick_task_type("fl2va", _image(), None, False)
        with self.assertRaises(ValueError):
            asm.pick_task_type("ref2va", None, None, False)
        with self.assertRaises(ValueError):
            asm.pick_task_type("hybrid", None, None, True)
        with self.assertRaises(ValueError):
            asm.pick_task_type("t2va", None, None, True)
        with self.assertRaises(ValueError):
            asm.pick_task_type("no_such_task", None, None, False)


@unittest.skipIf(COMFY is None, SKIP_REASON)
class ReferenceSizingTests(unittest.TestCase):
    def test_pixel_cap_follows_the_scale_table(self):
        self.assertEqual(asm._ref_pixel_cap(1000, 1000, "match", video=False), 1_000_000)
        self.assertEqual(asm._ref_pixel_cap(1000, 1000, "1.5x", video=False), 1_500_000)
        self.assertEqual(asm._ref_pixel_cap(1000, 1000, "2x", video=False), 2_000_000)
        self.assertEqual(asm._ref_pixel_cap(1000, 1000, "max", video=False), 4 * 1024 * 1024)
        self.assertEqual(asm._ref_pixel_cap(1000, 1000, "max", video=True), 1_600_000)
        self.assertEqual(asm._ref_pixel_cap(4000, 4000, "2x", video=False), 4 * 1024 * 1024)

    def test_video_cap_applies_a_minimum_budget(self):
        self.assertEqual(asm._ref_pixel_cap(100, 100, "match", video=True), 300_000)

    def test_reference_media_is_never_upscaled(self):
        small = torch.rand(1, 64, 64, 3)
        resized, width, height = asm._fit_reference_media(small, 640 * 640)
        self.assertEqual((width, height), (64, 64))
        self.assertEqual(tuple(resized.shape), (1, 64, 64, 3))

    def test_reference_media_is_downscaled_onto_the_32_grid(self):
        large = torch.rand(1, 512, 512, 3)
        resized, width, height = asm._fit_reference_media(large, 256 * 256)
        self.assertEqual(width % 32, 0)
        self.assertEqual(height % 32, 0)
        self.assertLessEqual(width * height, 256 * 256)
        self.assertEqual(tuple(resized.shape), (1, height, width, 3))


@unittest.skipIf(COMFY is None, SKIP_REASON)
class ExtraCondsPolicyTests(unittest.TestCase):
    SOURCE_TEMPLATE = '''
class MiniMaxH3:
    def extra_conds(self, **kwargs):
        payload = {}
__BODY__
        return {"cond": payload}
'''

    def _policy(self, body):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model_base.py"
            path.write_text(self.SOURCE_TEMPLATE.replace("__BODY__", body), encoding="utf-8")
            segment = asm.load_extra_conds_source(path)
        return asm.probe_video_latents_policy(segment)

    def test_detects_concat_policy(self):
        body = '        payload["cond_video_latents"] = keyframes + refs'
        self.assertEqual(self._policy(body), asm.REF_MODE_APPEND)

    def test_detects_reference_overwrite_policy(self):
        body = '        payload["cond_video_latents"] = [x for x in refs]'
        self.assertEqual(self._policy(body), asm.REF_MODE_REPLACE)
        body = '        payload["cond_video_latents"] = ref_latents'
        self.assertEqual(self._policy(body), asm.REF_MODE_REPLACE)

    def test_detects_keyframe_only_policy(self):
        body = '        payload["cond_video_latents"] = [x for x in keyframes]'
        self.assertEqual(self._policy(body), "keyframes_only")

    def test_unknown_when_nothing_is_assigned(self):
        self.assertEqual(self._policy("        payload[\"other\"] = refs"), "unknown")

    def test_unparsable_source_is_unknown(self):
        self.assertEqual(asm.probe_video_latents_policy("def broken(:"), "unknown")

    def test_missing_extra_conds_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model_base.py"
            path.write_text("class MiniMaxH3:\n    pass\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                asm.load_extra_conds_source(path)

    def test_live_comfyui_reports_a_supported_policy(self):
        # 本机 ComfyUI 必须落在两种受支持策略之一，否则 Hybrid 会被主动禁用。
        self.assertIn(asm.verify_hybrid_layout(), {asm.REF_MODE_REPLACE, asm.REF_MODE_APPEND})


@unittest.skipIf(COMFY is None, SKIP_REASON)
class ComposeConditioningTests(unittest.TestCase):
    def test_t2va_builds_a_prompt_only_conditioning(self):
        clip = _StubClip()
        conditioning, latent, audio, prompt, media_map, report = asm.compose_conditioning(
            **_conditions(clip=clip, audio_source="native")
        )
        self.assertEqual(len(clip.tokenize_calls), 1)
        _prompt, kwargs = clip.tokenize_calls[0]
        self.assertEqual(set(kwargs), {"images"})
        self.assertEqual(kwargs["images"], [])
        self.assertNotIn("minimax_refs", conditioning[0][1])
        self.assertNotIn("minimax_keyframes", conditioning[0][1])
        self.assertIsNone(audio)
        self.assertEqual(latent["gh_h3_audio_mode"], "native")
        self.assertIn("task=t2va", report)
        self.assertIn("audio_source=native", report)
        self.assertIn("frames=124 (5.167s at 24fps)", report)
        self.assertIn("pictures=0, videos=0, audios=0", report)
        self.assertIsInstance(prompt, str)
        self.assertIn('"pictures"', media_map)

    def test_fl2va_registers_both_exact_keyframes(self):
        clip = _StubClip()
        conditioning, _latent, _audio, _prompt, _map, report = asm.compose_conditioning(
            **_conditions(clip=clip, audio_source="native", keyframe_open=_image(), keyframe_close=_image())
        )
        _prompt, kwargs = clip.tokenize_calls[0]
        self.assertEqual(set(kwargs), {"images"})
        self.assertEqual(len(kwargs["images"]), 2)
        keyframes = conditioning[0][1]["minimax_keyframes"]
        self.assertEqual([entry["resolved_frame_index"] for entry in keyframes], [0, 123])
        self.assertEqual(conditioning[0][1]["minimax_frame_count"], 124)
        self.assertIn("task=fl2va", report)

    def test_ref2va_passes_reference_items_and_blocks(self):
        clip = _StubClip()
        conditioning, _latent, _audio, _prompt, _map, report = asm.compose_conditioning(
            **_conditions(clip=clip, audio_source="native",
                          ref_images={"still_1": _image(), "still_2": _image()},
                          ref_videos={"footage_1": _image(frames=30)})
        )
        _prompt, kwargs = clip.tokenize_calls[0]
        self.assertEqual(set(kwargs), {"minimax_ref_items"})
        kinds = [item["type"] for item in kwargs["minimax_ref_items"]]
        self.assertEqual(kinds.count("image"), 2)
        self.assertEqual(kinds.count("video"), 1)
        refs = conditioning[0][1]["minimax_refs"]
        self.assertEqual([block["kind"] for block in refs], ["image", "image", "video"])
        self.assertIn("task=ref2va", report)
        self.assertIn("pictures=2, videos=1, audios=0", report)

    def test_hybrid_combines_keyframes_with_references(self):
        clip = _StubClip()
        policy = asm.verify_hybrid_layout()
        conditioning, _latent, _audio, _prompt, _map, report = asm.compose_conditioning(
            **_conditions(clip=clip, audio_source="native", keyframe_open=_image(),
                          ref_images={"still_1": _image()})
        )
        _prompt, kwargs = clip.tokenize_calls[0]
        self.assertEqual(set(kwargs), {"minimax_ref_items"})
        refs = conditioning[0][1]["minimax_refs"]
        if policy == asm.REF_MODE_REPLACE:
            self.assertEqual(refs[0]["kind"], asm.HYBRID_MARKER)
            self.assertEqual(len(refs), 2)
        else:
            self.assertEqual(len(refs), 1)
        self.assertEqual(len(conditioning[0][1]["minimax_keyframes"]), 1)
        self.assertIn("task=hybrid", report)

    def test_lock_source_freezes_the_source_audio(self):
        _c, latent, audio, _prompt, _map, _report = asm.compose_conditioning(
            **_conditions(drive_audio=_audio())
        )
        self.assertIsNotNone(audio)
        _, audio_mask = latent["noise_mask"].unbind()
        self.assertEqual(torch.count_nonzero(audio_mask), 0)

    def test_remix_source_uses_the_denoise_strength(self):
        _c, latent, _audio_out, _prompt, _map, _report = asm.compose_conditioning(
            **_conditions(drive_audio=_audio(), audio_source="remix_source",
                          audio_cleanup=0.35)
        )
        _, audio_mask = latent["noise_mask"].unbind()
        self.assertEqual(torch.count_nonzero(audio_mask == 0.35), audio_mask.numel())

    def test_native_mode_keeps_the_generated_audio_track(self):
        _c, latent, _audio_out, _prompt, _map, report = asm.compose_conditioning(
            **_conditions(audio_source="native")
        )
        self.assertNotIn("noise_mask", latent)
        self.assertIn("audio_source=native", report)

    def test_rejects_invalid_geometry_and_parameters(self):
        with self.assertRaises(ValueError):
            asm.compose_conditioning(**_conditions(width=1000))
        with self.assertRaises(ValueError):
            asm.compose_conditioning(**_conditions(audio_cleanup=1.5))
        with self.assertRaises(ValueError):
            asm.compose_conditioning(**_conditions(audio_source="no_such_mode"))
        with self.assertRaises(ValueError):
            asm.compose_conditioning(**_conditions(audio_source="remix_source"))

    def test_rejects_reference_overload_and_orphan_soundtracks(self):
        with self.assertRaises(ValueError):
            asm.compose_conditioning(**_conditions(
                ref_images={f"still_{i}": _image() for i in range(1, 11)}))
        with self.assertRaises(ValueError):
            asm.compose_conditioning(**_conditions(
                footage_audios={"footage_2": _audio()}))

    def test_short_reference_video_is_rejected(self):
        with self.assertRaises(ValueError):
            asm.compose_conditioning(**_conditions(ref_videos={"footage_1": _image(frames=4)}))

    def test_source_audio_is_added_as_a_reference_block(self):
        clip = _StubClip()
        conditioning, _latent, _audio_out, _prompt, _map, report = asm.compose_conditioning(
            **_conditions(clip=clip, drive_audio=_audio())
        )
        _prompt, kwargs = clip.tokenize_calls[0]
        kinds = [item["type"] for item in kwargs["minimax_ref_items"]]
        self.assertEqual(kinds, ["audio"])
        refs = conditioning[0][1]["minimax_refs"]
        self.assertEqual([block["kind"] for block in refs], ["audio"])
        # 自动附加的驱动音轨不算「用户指定的参考音频」：它不占音频序号，
        # 报告里的 audios 计数与提示词音频标签校验都不含它。
        self.assertIn("audios=0", report)
        self.assertIn("task=ref2va", report)

    def test_source_audio_is_not_duplicated_when_already_a_reference(self):
        shared = _audio()
        clip = _StubClip()
        _c, _latent, _audio_out, _prompt, _map, report = asm.compose_conditioning(
            **_conditions(clip=clip, drive_audio=shared, ref_videos={"footage_1": _image(frames=30)},
                          footage_audios={"footage_1": shared})
        )
        self.assertIn("audios=1", report)

    def test_reference_video_with_soundtrack_is_tagged_as_video_audio(self):
        clip = _StubClip()
        conditioning, _latent, _audio_out, _prompt, _map, report = asm.compose_conditioning(
            **_conditions(clip=clip, audio_source="native", ref_videos={"footage_1": _image(frames=30)},
                          footage_audios={"footage_1": _audio()})
        )
        refs = conditioning[0][1]["minimax_refs"]
        self.assertEqual([block["kind"] for block in refs], ["video_audio"])
        self.assertGreater(refs[0]["track_t"], 0)
        self.assertIn("audios=1", report)

    def test_standalone_reference_audio_is_registered(self):
        clip = _StubClip()
        conditioning, _latent, _audio_out, _prompt, _map, report = asm.compose_conditioning(
            **_conditions(clip=clip, audio_source="native",
                          ref_audios={"track_1": _audio(), "track_2": _audio()})
        )
        refs = conditioning[0][1]["minimax_refs"]
        self.assertEqual([block["kind"] for block in refs], ["audio", "audio"])
        self.assertIn("audios=2", report)

    def test_strict_prompt_tags_reports_warnings_instead_of_failing(self):
        clip = _StubClip()
        _c, _latent, _audio_out, _prompt, _map, report = asm.compose_conditioning(
            **_conditions(clip=clip, audio_source="native",
                          prompt="integrated_multimodal_description: 没有镜头标记的文本。",
                          tag_guard=False)
        )
        self.assertIsInstance(report, str)


if __name__ == "__main__":
    unittest.main()
