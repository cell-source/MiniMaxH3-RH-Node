"""h3kit.muxing 行为基线：起始爆音修剪、音轨混流、AV 解码与裁剪。

这些用例是"重写前后必须完全一致"的规格说明。需要 ComfyUI 运行时
（``comfy_extras.nodes_audio`` / ``comfy.model_management``），
定位与跳过逻辑见 ``tests/comfy_env.py``。

用法：``python -m pytest tests -o addopts='' -q``
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comfy_env  # noqa: E402
from comfy_env import COMFY, SKIP_REASON  # noqa: E402

if COMFY is not None:
    import h3kit.geometry as geo
    import h3kit.muxing as mux

RATE = 32000
WINDOW = 320                       # 10 ms
WINDOW_COUNT = 90


def _windowed_waveform(levels):
    """按 10 ms 窗口拼接恒定幅度，方便精确控制 RMS 包络。"""
    return torch.cat([torch.full((1, 1, WINDOW), float(level)) for level in levels], dim=-1)


def _audio(waveform, sample_rate=RATE):
    return {"waveform": waveform, "sample_rate": sample_rate}


class _StubAudioVae:
    audio_sample_rate = 16000

    def __init__(self, latent_shape=(1, 32, 2, 5), decoded=None):
        self.latent_shape = latent_shape
        self.decoded = decoded
        self.encoded_shape = None

    def encode(self, waveform):
        self.encoded_shape = tuple(waveform.shape)
        return torch.zeros(*self.latent_shape)

    def decode(self, latent):
        return self.decoded


class _StubVideoVae:
    def __init__(self, frames):
        self.frames = frames

    def decode(self, latent):
        return self.frames


@unittest.skipIf(COMFY is None, SKIP_REASON)
class TrimAudioHeadTests(unittest.TestCase):
    """起始爆音 = 一个 80–280 ms 的碎片 + 静音间隔 + 真正的节目起音。"""

    @staticmethod
    def _artifact():
        levels = [0.12] * 5 + [0.05] * 3 + [0.001] * 32 + [0.12] * 50
        assert len(levels) == WINDOW_COUNT
        return _audio(_windowed_waveform(levels))

    def test_removes_the_burst_and_fades_into_the_real_onset(self):
        audio = self._artifact()
        original = audio["waveform"]
        cleaned = mux.trim_audio_head(audio)
        self.assertIsNot(cleaned, audio)
        self.assertEqual(cleaned["sample_rate"], RATE)
        result = cleaned["waveform"]
        self.assertEqual(tuple(result.shape), tuple(original.shape))
        # 碎片与静音段被清零，直到第 8 个窗口
        self.assertEqual(torch.count_nonzero(result[..., : 8 * WINDOW]), 0)
        # 第 40 个窗口起是真正的节目内容，必须逐值保留
        self.assertTrue(torch.equal(result[..., 40 * WINDOW:], original[..., 40 * WINDOW:]))
        # 过渡带被衰减但非零
        transition = result[..., 8 * WINDOW:40 * WINDOW]
        self.assertGreater(torch.count_nonzero(transition), 0)
        self.assertLess(float(transition.abs().max()), 0.12)

    def test_keeps_audio_without_the_artifact(self):
        steady = _audio(_windowed_waveform([0.12] * WINDOW_COUNT))
        self.assertIs(mux.trim_audio_head(steady), steady)

    def test_keeps_silence_and_very_short_clips(self):
        silence = _audio(torch.zeros(1, 1, WINDOW * WINDOW_COUNT))
        self.assertIs(mux.trim_audio_head(silence), silence)
        short = _audio(torch.full((1, 1, 100), 0.2))
        self.assertIs(mux.trim_audio_head(short), short)


@unittest.skipIf(COMFY is None, SKIP_REASON)
class EmbedAudioLatentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.latent, _ = geo.blank_av_latent(1920, 1088, 124)
        cls.source = _audio(torch.ones(1, 2, 16000), 16000)

    def test_lock_freezes_the_denoise_mask(self):
        out, returned = mux.embed_audio_latent(self.latent, self.source, _StubAudioVae(), "lock", 0.7)
        self.assertIs(returned, self.source)
        _, audio_mask = out["noise_mask"].unbind()
        self.assertEqual(torch.count_nonzero(audio_mask), 0)

    def test_remix_uses_the_requested_strength(self):
        out, _ = mux.embed_audio_latent(self.latent, self.source, _StubAudioVae(), "remix", 0.7)
        _, audio_mask = out["noise_mask"].unbind()
        self.assertEqual(torch.count_nonzero(audio_mask == 0.7), audio_mask.numel())

    def test_rejects_unknown_modes_and_strengths(self):
        with self.assertRaises(ValueError):
            mux.embed_audio_latent(self.latent, self.source, _StubAudioVae(), "copy", 0.5)
        with self.assertRaises(ValueError):
            mux.embed_audio_latent(self.latent, self.source, _StubAudioVae(), "remix", 1.5)


@unittest.skipIf(COMFY is None, SKIP_REASON)
class UnpackAvLatentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.latent, _ = geo.blank_av_latent(1920, 1088, 124)

    def test_flattens_a_batched_video_decode(self):
        # 视频 VAE 实际返回 [B,T,H,W,C]；节点把它压平成 [B*T,H,W,C]
        video_vae = _StubVideoVae(torch.zeros(1, 2, 8, 8, 3))
        audio_vae = _StubAudioVae(decoded=torch.zeros(1, 8, 2))
        images, decoded, video_latent, audio_latent = mux.unpack_av_latent(self.latent, video_vae, audio_vae)
        self.assertEqual(tuple(images.shape), (2, 8, 8, 3))
        self.assertEqual(tuple(decoded["waveform"].shape), (1, 2, 8))
        self.assertEqual(decoded["sample_rate"], 16000)
        self.assertEqual(video_latent["samples"].ndim, 5)
        self.assertEqual(audio_latent["samples"].ndim, 4)
        self.assertNotIn("noise_mask", video_latent)

    def test_keeps_an_already_flat_video_decode_and_splits_masks(self):
        latent = geo.swap_audio_latent(
            self.latent, torch.zeros(1, 32, 2, 9), 0.3
        )
        latent["gh_h3_audio_mode"] = "native"
        video_vae = _StubVideoVae(torch.zeros(2, 8, 8, 3))
        audio_vae = _StubAudioVae(decoded=torch.zeros(1, 8, 2))
        images, _decoded, video_latent, audio_latent = mux.unpack_av_latent(latent, video_vae, audio_vae)
        self.assertEqual(tuple(images.shape), (2, 8, 8, 3))
        self.assertEqual(tuple(video_latent["noise_mask"].shape), tuple(video_latent["samples"].shape))
        self.assertEqual(tuple(audio_latent["noise_mask"].shape), tuple(audio_latent["samples"].shape))


@unittest.skipIf(COMFY is None, SKIP_REASON)
class ResampleAndChannelTests(unittest.TestCase):
    def test_resample_is_a_no_op_at_the_same_rate(self):
        waveform = torch.ones(1, 2, 100)
        self.assertIs(mux._resample_rate(waveform, 16000, 16000), waveform)

    def test_align_channels_handles_mono_sterile_and_multichannel(self):
        mono = torch.ones(1, 1, 10)
        self.assertEqual(mux._align_channels(mono, 1).shape, (1, 1, 10))
        expanded = mux._align_channels(mono, 2)
        self.assertEqual(expanded.shape, (1, 2, 10))
        stereo = torch.ones(1, 2, 10)
        self.assertEqual(mux._align_channels(stereo, 1).shape, (1, 1, 10))
        surround = torch.ones(1, 4, 10)
        downmixed = mux._align_channels(surround, 2)
        self.assertEqual(downmixed.shape, (1, 2, 10))


@unittest.skipIf(COMFY is None, SKIP_REASON)
class BlendAudioTests(unittest.TestCase):
    @staticmethod
    def _track(level, samples=1000, rate=16000, channels=1):
        return {"waveform": torch.full((1, channels, samples), float(level)), "sample_rate": rate}

    def test_sums_the_tracks_at_zero_gain(self):
        out = mux.blend_audio(self._track(1.0), self._track(0.0),
                              source_gain_db=0.0, generated_gain_db=0.0,
                              duck_generated=0.0, peak_limit_dbfs=0.0)
        self.assertEqual(out["sample_rate"], 16000)
        self.assertTrue(torch.allclose(out["waveform"], torch.ones(1, 1, 1000)))

    def test_peak_limiter_scales_the_mix(self):
        out = mux.blend_audio(self._track(2.0), self._track(0.0),
                              source_gain_db=0.0, generated_gain_db=0.0,
                              duck_generated=0.0, peak_limit_dbfs=0.0)
        self.assertTrue(torch.allclose(out["waveform"], torch.ones(1, 1, 1000)))

    def test_pads_to_the_longest_track(self):
        out = mux.blend_audio(self._track(0.25, samples=1000), self._track(0.5, samples=1600),
                              source_gain_db=0.0, generated_gain_db=0.0,
                              duck_generated=0.0, peak_limit_dbfs=0.0)
        waveform = out["waveform"]
        self.assertEqual(tuple(waveform.shape), (1, 1, 1600))
        self.assertTrue(torch.allclose(waveform[..., :1000], torch.full((1, 1, 1000), 0.75)))
        self.assertTrue(torch.allclose(waveform[..., 1000:], torch.full((1, 1, 600), 0.5)))

    def test_resamples_and_matches_channel_counts(self):
        out = mux.blend_audio(self._track(0.1, samples=16000, rate=16000, channels=1),
                              self._track(0.1, samples=32000, rate=32000, channels=2),
                              source_gain_db=0.0, generated_gain_db=-60.0,
                              duck_generated=0.0, output_sample_rate="source", peak_limit_dbfs=6.0)
        self.assertEqual(out["sample_rate"], 16000)
        self.assertEqual(tuple(out["waveform"].shape), (1, 2, 16000))

    def test_ducking_lowers_the_generated_track_under_speech(self):
        without = mux.blend_audio(self._track(0.2, samples=4000), self._track(0.2, samples=4000),
                                  source_gain_db=0.0, generated_gain_db=0.0,
                                  duck_generated=0.0, peak_limit_dbfs=6.0)
        ducked = mux.blend_audio(self._track(0.2, samples=4000), self._track(0.2, samples=4000),
                                 source_gain_db=0.0, generated_gain_db=0.0,
                                 duck_generated=0.5, peak_limit_dbfs=6.0)
        middle = slice(1000, 3000)
        self.assertTrue(torch.allclose(without["waveform"][..., middle], torch.full((1, 1, 2000), 0.4)))
        self.assertTrue(torch.allclose(ducked["waveform"][..., middle], torch.full((1, 1, 2000), 0.3)))


@unittest.skipIf(COMFY is None, SKIP_REASON)
class ClipAvTailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames = torch.arange(10 * 2 * 2 * 3, dtype=torch.float32).reshape(10, 2, 2, 3)

    def test_trims_frames_and_pads_short_audio(self):
        frames, audio, report = mux.clip_av_tail(
            self.frames, 0.0, 0.25, _audio(torch.ones(1, 2, 1000), 8000), fps=24.0
        )
        self.assertEqual(tuple(frames.shape), (6, 2, 2, 3))
        self.assertTrue(torch.equal(frames, self.frames[:6]))
        self.assertEqual(tuple(audio["waveform"].shape), (1, 2, 2000))
        self.assertEqual(float(audio["waveform"][..., :1000].sum()), 1000.0 * 2)
        self.assertEqual(float(audio["waveform"][..., 1000:].sum()), 0.0)
        payload = json.loads(report)
        self.assertEqual(payload["start_frame"], 0)
        self.assertEqual(payload["frame_count"], 6)
        self.assertEqual(payload["actual_video_duration_seconds"], 0.25)

    def test_audio_is_optional(self):
        frames, audio, _report = mux.clip_av_tail(self.frames, 0.0, 0.25)
        self.assertEqual(tuple(frames.shape), (6, 2, 2, 3))
        self.assertIsNone(audio)

    def test_rejects_bad_ranges(self):
        with self.assertRaises(ValueError):
            mux.clip_av_tail(self.frames[0], 0.0, 0.25)
        with self.assertRaises(ValueError):
            mux.clip_av_tail(self.frames, -0.1, 0.25)
        with self.assertRaises(ValueError):
            mux.clip_av_tail(self.frames, 0.0, 0.0)
        with self.assertRaises(ValueError):
            mux.clip_av_tail(self.frames, 0.0, 0.25, fps=0.0)
        with self.assertRaises(ValueError):
            mux.clip_av_tail(self.frames, 0.0, 1.0)


if __name__ == "__main__":
    unittest.main()
