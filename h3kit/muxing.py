"""AV 潜空间的解码与裁剪、起始爆音修剪，以及多音轨混流。"""

from __future__ import annotations

import gc
import json

import torch
import torch.nn.functional as torch_functional
import torchaudio

from comfy import model_management
from comfy_extras.nodes_audio import vae_decode_audio

from .geometry import encode_waveform, av_streams, swap_audio_latent, check_waveform


# --- 起始爆音检测参数（窗口单位，默认窗口为 10 ms）---
_HEAD_ANALYSIS_SECONDS = 0.9
_HEAD_WINDOW_SECONDS = 0.01
_HEAD_MIN_WINDOWS = 20
_HEAD_PEAK_FLOOR = 0.01
_HEAD_ACTIVE_FLOOR = 0.004
_HEAD_ACTIVE_RATIO = 0.08
_HEAD_SILENCE_FLOOR = 0.0015
_HEAD_SILENCE_RATIO = 0.07
_HEAD_BURST_SCAN_LIMIT = 3        # 伪影只可能在最前面的几个窗口里
_HEAD_BURST_MIN_SPAN = 6
_HEAD_BURST_SEARCH_MIN = 8
_HEAD_BURST_SCAN_MAX = 32
_HEAD_QUIET_RUN = 3
_HEAD_BURST_MIN_ACTIVE = 5
_HEAD_BURST_MAX_ACTIVE = 30
_HEAD_PAUSE_WINDOWS = 12
_HEAD_PAUSE_MIN_WINDOWS = 6
_HEAD_PAUSE_RATIO = 0.42
_HEAD_ONSET_OFFSET = 8
_HEAD_ONSET_SCAN_MAX = 80
_HEAD_ONSET_WINDOWS = 7
_HEAD_ONSET_MIN_ACTIVE = 5
_HEAD_GAP_MIN_WINDOWS = 10
_HEAD_GAP_RATIO = 0.55

_DUCK_WINDOW_SECONDS = 0.02
_DUCK_REFERENCE_LEVEL = 0.05
_PEAK_FLOOR = 1e-8

_LATENT_MODES = {"lock", "remix"}
_RAW_AUDIO_MODES = {"native", "reference_only"}


def _head_rms(waveform: torch.Tensor, window_count: int, window_samples: int) -> torch.Tensor:
    """按固定长度窗口计算 RMS 包络（只统计分析窗内的样本）。"""
    analysis = waveform[..., : window_count * window_samples]
    squared = analysis.float().pow(2).mean(dim=(0, 1))
    return squared.reshape(window_count, window_samples).mean(dim=1).sqrt()


def _first_active_window(active: torch.Tensor, window_count: int):
    """最前面那几个窗口里第一个“活跃”窗口；没有则 None。"""
    limit = min(window_count, _HEAD_BURST_SCAN_LIMIT)
    hits = torch.nonzero(active[:limit], as_tuple=False).flatten()
    return None if hits.numel() == 0 else int(hits[0].item())


def _first_quiet_run(quiet: torch.Tensor, burst_start: int, window_count: int):
    """碎片之后第一个连续静音段的起点窗口；没有则 None。"""
    start = max(burst_start + _HEAD_BURST_MIN_SPAN, _HEAD_BURST_SEARCH_MIN)
    stop = min(window_count - _HEAD_QUIET_RUN, _HEAD_BURST_SCAN_MAX)
    for index in range(start, stop + 1):
        if bool(quiet[index : index + _HEAD_QUIET_RUN].all().item()):
            return index
    return None


def _first_sustained_window(active: torch.Tensor, burst_end: int, window_count: int):
    """静音段之后第一个“持续活跃”的窗口（即真正的节目起音）。"""
    start = burst_end + _HEAD_ONSET_OFFSET
    stop = min(window_count - _HEAD_ONSET_WINDOWS, _HEAD_ONSET_SCAN_MAX)
    for index in range(start, stop + 1):
        if int(active[index : index + _HEAD_ONSET_WINDOWS].sum().item()) >= _HEAD_ONSET_MIN_ACTIVE:
            return index
    return None


def _locate_head_artifact(rms, active, quiet, window_count, active_floor):
    """定位起始伪影，返回（静音起点窗口, 真实起音窗口）；未命中返回 None。"""
    burst_start = _first_active_window(active, window_count)
    if burst_start is None:
        return None

    burst_end = _first_quiet_run(quiet, burst_start, window_count)
    if burst_end is None:
        return None

    burst = rms[burst_start:burst_end]
    active_count = int((burst >= active_floor).sum().item())
    if not _HEAD_BURST_MIN_ACTIVE <= active_count <= _HEAD_BURST_MAX_ACTIVE:
        return None

    pause = rms[burst_end : min(window_count, burst_end + _HEAD_PAUSE_WINDOWS)]
    if pause.numel() < _HEAD_PAUSE_MIN_WINDOWS:
        return None
    burst_level = float(burst.mean().item())
    if burst_level <= 0.0 or float(pause.mean().item()) > burst_level * _HEAD_PAUSE_RATIO:
        return None

    onset = _first_sustained_window(active, burst_end, window_count)
    if onset is None:
        return None

    gap = quiet[burst_end:onset]
    if gap.numel() < _HEAD_GAP_MIN_WINDOWS or float(gap.float().mean().item()) < _HEAD_GAP_RATIO:
        return None
    return burst_end, onset


def _mute_head(waveform: torch.Tensor, silence_end: int, fade_end: int) -> torch.Tensor:
    """静音 [0, silence_end)，并在到 fade_end 之间做 sin² 淡入。"""
    cleaned = waveform.clone()
    cleaned[..., :silence_end] = 0
    ramp = torch.linspace(
        0.0, torch.pi / 2, fade_end - silence_end,
        device=cleaned.device, dtype=cleaned.dtype,
    )
    cleaned[..., silence_end:fade_end] *= torch.sin(ramp).square()
    return cleaned


def trim_audio_head(audio: dict) -> dict:
    """去掉 H3 音频开头那段特征性的“截断词”爆音。"""
    waveform, sample_rate = check_waveform(audio, "decoded_audio")
    window_samples = max(1, round(sample_rate * _HEAD_WINDOW_SECONDS))
    analysis_samples = min(waveform.shape[-1], round(sample_rate * _HEAD_ANALYSIS_SECONDS))
    window_count = analysis_samples // window_samples
    if window_count < _HEAD_MIN_WINDOWS:
        return audio

    rms = _head_rms(waveform, window_count, window_samples)
    peak = float(rms.max().item())
    if peak < _HEAD_PEAK_FLOOR:
        return audio

    # 合法起音可能很轻：活跃阈值比旧检测器宽松，静音阈值再低一档，
    # 两者之间留出缓冲区，避免把轻声开头误判为伪影。
    active_floor = max(_HEAD_ACTIVE_FLOOR, peak * _HEAD_ACTIVE_RATIO)
    silence_ceiling = max(_HEAD_SILENCE_FLOOR, peak * _HEAD_SILENCE_RATIO)
    span = _locate_head_artifact(
        rms, rms >= active_floor, rms <= silence_ceiling, window_count, active_floor
    )
    if span is None:
        return audio

    silence_window, onset_window = span
    silence_end = min(waveform.shape[-1], silence_window * window_samples)
    fade_end = min(waveform.shape[-1], onset_window * window_samples)
    if fade_end <= silence_end:
        return audio
    return {**audio, "waveform": _mute_head(waveform, silence_end, fade_end)}


def embed_audio_latent(av_latent: dict, source_audio: dict, audio_vae, mode: str, strength: float):
    """把 source_audio 编码进 AV 潜空间。

    ``lock`` 完全冻结原音轨（降噪强度 0），``remix`` 按 ``strength`` 混合。
    """
    normalized = mode.lower()
    if normalized not in _LATENT_MODES:
        raise ValueError("音轨控制模式只能是 lock 或 remix")
    if not 0.0 <= strength <= 1.0:
        raise ValueError("strength 必须落在 0 与 1 之间")
    denoise = 0.0 if normalized == "lock" else strength
    return swap_audio_latent(av_latent, encode_waveform(audio_vae, source_audio), denoise), source_audio


def unpack_av_latent(av_latent: dict, video_vae, audio_vae):
    """解码 AV 潜空间，返回（帧, 音频, 视频 latent, 音频 latent）。

    两个 latent 会保留通道等附属键（含拆开后的 noise_mask），供下游二段放大或
    二次采样复用。
    """
    try:
        video, audio = av_streams(av_latent)
        images = video_vae.decode(video)
        if images.ndim == 5:
            # 视频 VAE 返回 [B,T,H,W,C]，节点对外统一用压平的帧序列
            images = images.reshape(-1, *images.shape[-3:])
        decoded_audio = vae_decode_audio(audio_vae, {"samples": audio})
        if av_latent.get("gh_h3_audio_mode") in _RAW_AUDIO_MODES:
            decoded_audio = trim_audio_head(decoded_audio)

        extras = {key: value for key, value in av_latent.items() if key not in {"samples", "noise_mask"}}
        video_latent = {**extras, "samples": video}
        audio_latent = {**extras, "samples": audio}
        masks = av_latent.get("noise_mask")
        if getattr(masks, "is_nested", False):
            video_latent["noise_mask"], audio_latent["noise_mask"] = masks.unbind()
        return images, decoded_audio, video_latent, audio_latent
    finally:
        # 只回收解码期的临时分配：不卸载 VAE，也不让已返回的张量失效。
        gc.collect()
        model_management.soft_empty_cache()


def _resample_rate(waveform: torch.Tensor, source_rate: int, target_rate: int) -> torch.Tensor:
    """采样率不同时才重采样，相同时原样返回（避免多余的拷贝）。"""
    if source_rate == target_rate:
        return waveform
    return torchaudio.functional.resample(waveform, source_rate, target_rate)


def _align_channels(waveform: torch.Tensor, target_channels: int) -> torch.Tensor:
    """把声道数对齐到目标值：单声道可复制、多声道先归一再展开。"""
    channels = waveform.shape[1]
    if channels == target_channels:
        return waveform
    if channels == 1:
        return waveform.expand(-1, target_channels, -1)
    if target_channels == 1:
        return waveform.mean(dim=1, keepdim=True)
    # 少见的多声道输入统一降成确定性的立体声可用混音，而不是截取前两路。
    return waveform.mean(dim=1, keepdim=True).expand(-1, target_channels, -1)


def _db_to_gain(decibels: float) -> float:
    return 10.0 ** (decibels / 20.0)


def _pick_output_rate(choice, source_rate: int, generated_rate: int) -> int:
    if choice == "source":
        return source_rate
    if choice == "generated":
        return generated_rate
    return int(choice)


def _speech_activity(source: torch.Tensor, rate: int, length: int) -> torch.Tensor:
    """源音轨的平滑活跃度曲线（0–1），用于压低背景生成音轨。"""
    envelope = source.abs().mean(dim=1, keepdim=True)
    window = max(1, round(rate * _DUCK_WINDOW_SECONDS))
    envelope = torch_functional.avg_pool1d(envelope, window, stride=1, padding=window // 2)
    return (envelope[..., :length] / _DUCK_REFERENCE_LEVEL).clamp(0.0, 1.0)


def _limit_peak(mixed: torch.Tensor, ceiling_dbfs: float) -> torch.Tensor:
    """按峰值天花板整体缩放，避免削波引入失真。"""
    ceiling = _db_to_gain(ceiling_dbfs)
    peak = mixed.abs().amax(dim=(1, 2), keepdim=True).clamp_min(_PEAK_FLOOR)
    scale = torch.minimum(torch.ones_like(peak), torch.full_like(peak, ceiling) / peak)
    return mixed * scale


def blend_audio(
    source_audio: dict,
    generated_audio: dict,
    source_gain_db: float = 0.0,
    generated_gain_db: float = -6.0,
    duck_generated: float = 0.5,
    output_sample_rate: str = "source",
    peak_limit_dbfs: float = -1.0,
):
    """按增益叠加两条音轨，可选根据源音轨活跃度压低生成音轨，并做峰值限制。"""
    source, source_rate = check_waveform(source_audio, "source_audio")
    generated, generated_rate = check_waveform(generated_audio, "generated_audio")
    target_rate = _pick_output_rate(output_sample_rate, source_rate, generated_rate)

    source = _resample_rate(source, source_rate, target_rate)
    generated = _resample_rate(generated, generated_rate, target_rate)
    channels = max(source.shape[1], generated.shape[1])
    source = _align_channels(source, channels)
    generated = _align_channels(generated, channels)

    length = max(source.shape[-1], generated.shape[-1])
    source = torch_functional.pad(source, (0, length - source.shape[-1]))
    generated = torch_functional.pad(generated, (0, length - generated.shape[-1]))
    source = source * _db_to_gain(source_gain_db)
    generated = generated * _db_to_gain(generated_gain_db)

    if duck_generated > 0:
        generated = generated * (1.0 - float(duck_generated) * _speech_activity(source, target_rate, length))

    mixed = _limit_peak(source + generated, peak_limit_dbfs)
    return {"waveform": mixed, "sample_rate": target_rate}


def clip_av_tail(frames: torch.Tensor, start_seconds: float, clip_seconds: float, audio=None, fps: float = 24.0):
    """按起始秒数与时长裁出视频帧（可选音频），并附带执行报告 JSON。"""
    if frames.ndim != 4:
        raise ValueError(f"frames 应为 IMAGE [N,H,W,C]，实际为 {tuple(frames.shape)}")
    if start_seconds < 0 or clip_seconds <= 0 or fps <= 0:
        raise ValueError("裁剪起点不能为负；时长与帧率必须为正")

    start_frame = round(start_seconds * fps)
    frame_count = max(1, round(clip_seconds * fps))
    end_frame = start_frame + frame_count
    if end_frame > frames.shape[0]:
        raise ValueError(
            f"请求的输出帧区间 [{start_frame}:{end_frame}] 超出已解码帧数 {frames.shape[0]}"
        )

    trimmed_audio = None
    if audio is not None:
        waveform, sample_rate = check_waveform(audio)
        start_sample = round(start_seconds * sample_rate)
        sample_count = round(clip_seconds * sample_rate)
        sliced = waveform[..., start_sample : start_sample + sample_count]
        if sliced.shape[-1] < sample_count:
            sliced = torch_functional.pad(sliced, (0, sample_count - sliced.shape[-1]))
        trimmed_audio = {"waveform": sliced, "sample_rate": sample_rate}

    report = json.dumps(
        {
            "start_seconds": start_seconds,
            "requested_duration_seconds": clip_seconds,
            "fps": fps,
            "start_frame": start_frame,
            "frame_count": frame_count,
            "actual_video_duration_seconds": frame_count / fps,
        },
        ensure_ascii=False,
        indent=2,
    )
    return frames[start_frame:end_frame], trimmed_audio, report
