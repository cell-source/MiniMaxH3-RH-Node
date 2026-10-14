"""H3 的尺寸/帧数规范化，以及 AV 潜空间与音频张量的读写工具。

对外契约（被 assembly / muxing / duality / node 以及测试依赖）：
``SIZE_STEP``、``VIDEO_FPS``、``snap_frames_up``、``snap_frames_down``、
``latent_time_len``、``time_shapes``、``fit_canvas``、``scale_image``、
``blank_av_latent``、``av_streams``、``separate_masks``、``swap_audio_latent``、
``check_waveform``、``encode_waveform``、``match_audio_latent``、
``ordered_dynamic_items``、``ordered_dynamic_values``。
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import torch
import torchaudio

import comfy.model_management
import comfy.nested_tensor
import comfy.utils


SIZE_STEP = 32            # 画布宽高必须落在它的整数倍网格上
DEFAULT_EDGE = 768        # 自适应画布的基准短边
PIXEL_BUDGET = 1920 * 1088
REF_EDGE = 2048
VIDEO_FPS = 24
AUDIO_FPS = 40
FRAME_FLOOR = 124
FRAME_CEIL = 362

_FRAME_STEP = 17          # H3 时间网格步长（17n+5）
_FRAME_BASE = 5           # 网格原点，也是允许的最小帧数
_VIDEO_LATENT_CHANNELS = 24
_AUDIO_LATENT_CHANNELS = 32
_SPATIAL_DOWNSCALE = 16
_FALLBACK_AUDIO_RATE = 32000
_UNRANKED_SLOT = 10_000


def _reject(message: str) -> None:
    """统一的校验出口：保持 ValueError，调用方无需区分具体类型。"""
    raise ValueError(message)


def _grid_offset(frame_count: int) -> int:
    """相对 H3 帧网格原点的偏移量（0 表示已对齐）。"""
    return (frame_count - _FRAME_BASE) % _FRAME_STEP


def snap_frames_up(frame_count: int) -> int:
    """向上对齐到 H3 的 17n+5 帧网格（下限 5 帧）。"""
    frame_count = max(_FRAME_BASE, int(frame_count))
    offset = _grid_offset(frame_count)
    return frame_count if offset == 0 else frame_count + _FRAME_STEP - offset


def snap_frames_down(frame_count: int) -> int:
    """向下对齐到同一网格；结果可能低于 5 帧，由调用方自行兜底。"""
    frame_count = int(frame_count)
    return frame_count - _grid_offset(frame_count)


def latent_time_len(frame_count: int) -> int:
    """视频 latent 的时间长度：每 17 帧出 5 个 latent 步，首尾另计 2 步。"""
    if frame_count <= _FRAME_BASE:
        return 2
    return 2 + 5 * ((frame_count - _FRAME_BASE) // _FRAME_STEP)


def time_shapes(length: int) -> tuple[int, int, int]:
    """把请求时长换算成（视频帧数、视频 latent 时间长度、音频 latent 长度）。"""
    frame_count = snap_frames_up(length)
    audio_t = round(frame_count / VIDEO_FPS * AUDIO_FPS)
    return frame_count, latent_time_len(frame_count), audio_t


def _snap_axis(span: float) -> int:
    """把一条边长量化到 SIZE_STEP 网格，并保证不小于一个步长。"""
    return max(SIZE_STEP, round(span / SIZE_STEP) * SIZE_STEP)


def fit_canvas(width: int, height: int) -> tuple[int, int]:
    """按短边基准把任意长宽比拟合进像素预算，然后对齐到 SIZE_STEP 网格。"""
    if width <= 0 or height <= 0:
        _reject("画布宽高必须为正数")
    ratio = width / height
    if ratio >= 1.0:
        span_w, span_h = DEFAULT_EDGE * ratio, float(DEFAULT_EDGE)
    else:
        span_w, span_h = float(DEFAULT_EDGE), DEFAULT_EDGE / ratio
    if span_w * span_h > PIXEL_BUDGET:
        shrink = math.sqrt(PIXEL_BUDGET / (span_w * span_h))
        span_w, span_h = span_w * shrink, span_h * shrink
    return _snap_axis(span_w), _snap_axis(span_h)


def scale_image(image: torch.Tensor, width: int, height: int, crop: str = "disabled") -> torch.Tensor:
    """把一张 BHWC 图像重采样到目标尺寸；只保留前三个通道。"""
    if image.ndim != 4:
        _reject(f"图像张量应为 [B,H,W,C]，实际为 {tuple(image.shape)}")
    planar = image[..., :3].movedim(-1, 1)
    return comfy.utils.common_upscale(planar, width, height, "lanczos", crop).movedim(1, -1)


def blank_av_latent(width: int, height: int, length: int) -> tuple[dict, int]:
    """生成全零的 AV 潜空间占位；形状由画布尺寸与请求时长决定。"""
    if width % SIZE_STEP or height % SIZE_STEP:
        _reject("MiniMax H3 的画布宽高必须是 32 的整数倍")
    frame_count, latent_t, audio_t = time_shapes(length)
    device = comfy.model_management.intermediate_device()
    video = torch.zeros(
        (1, _VIDEO_LATENT_CHANNELS, latent_t, height // _SPATIAL_DOWNSCALE, width // _SPATIAL_DOWNSCALE),
        device=device,
    )
    audio = torch.zeros((1, _AUDIO_LATENT_CHANNELS, 2, audio_t), device=device)
    return {"samples": comfy.nested_tensor.NestedTensor((video, audio))}, frame_count


def av_streams(av_latent: dict) -> tuple[torch.Tensor, torch.Tensor]:
    """从 AV 潜空间字典中拆出（视频, 音频）两部分，并校验形状契约。"""
    if not isinstance(av_latent, dict) or "samples" not in av_latent:
        _reject("需要一个 MiniMax H3 联合 AV LATENT")
    samples = av_latent["samples"]
    if not getattr(samples, "is_nested", False):
        _reject("需要一个嵌套的 MiniMax H3 视频/音频联合 latent")
    parts = tuple(samples.unbind())
    if len(parts) != 2:
        _reject(f"AV latent 应恰好包含两部分，实际为 {len(parts)}")
    video, audio = parts
    if video.ndim != 5 or audio.ndim != 4:
        _reject(f"AV latent 布局异常：video={tuple(video.shape)}, audio={tuple(audio.shape)}")
    if video.shape[0] != audio.shape[0] or video.shape[0] != 1:
        _reject("MiniMax H3 目前只支持 batch size 1")
    return video, audio


def check_waveform(audio: dict, name: str = "audio") -> tuple[torch.Tensor, int]:
    """校验一个 AUDIO 值，返回（波形, 采样率）。"""
    if not isinstance(audio, dict):
        _reject(f"{name} 必须是一个已连接的 AUDIO 值")
    waveform = audio.get("waveform")
    sample_rate = audio.get("sample_rate")
    if not isinstance(waveform, torch.Tensor) or sample_rate is None:
        _reject(f"{name} 缺少 waveform 或 sample_rate")
    if waveform.ndim != 3:
        _reject(f"{name} 应为 [batch,channels,samples]，实际为 {tuple(waveform.shape)}")
    if min(waveform.shape) < 1:
        _reject(f"{name} 为空")
    if waveform.shape[0] != 1:
        _reject(f"{name} 在 MiniMax H3 中必须为 batch size 1")
    return waveform, int(sample_rate)


def encode_waveform(audio_vae, audio: dict) -> torch.Tensor:
    """把 AUDIO 值编码成音频 VAE 的 latent（采样率不一致时先重采样）。"""
    waveform, sample_rate = check_waveform(audio)
    target_rate = int(getattr(audio_vae, "audio_sample_rate", _FALLBACK_AUDIO_RATE))
    if sample_rate != target_rate:
        waveform = torchaudio.functional.resample(waveform, sample_rate, target_rate)
    latent = audio_vae.encode(waveform[:1].movedim(1, -1))
    if not isinstance(latent, torch.Tensor) or latent.ndim != 4:
        _reject("音频 VAE 未返回 [B,C,stereo,T] 形状的 latent")
    return latent


def match_audio_latent(encoded_audio: torch.Tensor, template_audio: torch.Tensor) -> torch.Tensor:
    """把编码得到的音轨 latent 对齐到模板的形状、设备与精度。"""
    if encoded_audio.ndim != 4 or template_audio.ndim != 4:
        _reject("MiniMax H3 音轨 latent 必须是 [B,C,stereo,T]")
    if encoded_audio.shape[1:-1] != template_audio.shape[1:-1]:
        _reject(
            "音频 VAE latent 布局不匹配："
            f"得到 {tuple(encoded_audio.shape)}，目标 {tuple(template_audio.shape)}"
        )
    if encoded_audio.shape[0] != template_audio.shape[0]:
        if encoded_audio.shape[0] != 1:
            _reject("音轨 latent 的 batch 无法对齐到 AV latent")
        encoded_audio = encoded_audio.expand(template_audio.shape[0], -1, -1, -1)
    target_t = template_audio.shape[-1]
    current_t = encoded_audio.shape[-1]
    if current_t > target_t:
        encoded_audio = encoded_audio[..., :target_t]
    elif current_t < target_t:
        encoded_audio = torch.cat(
            [encoded_audio, encoded_audio.new_zeros(*encoded_audio.shape[:-1], target_t - current_t)],
            dim=-1,
        )
    return encoded_audio.to(device=template_audio.device, dtype=template_audio.dtype)


def _slot_number(key) -> int | None:
    """取 key 末尾下划线后的数字序号；无法解析时返回 None。"""
    try:
        return int(str(key).rsplit("_", 1)[-1])
    except ValueError:
        return None


def ordered_dynamic_items(values: Mapping | None) -> list[tuple[int, object]]:
    """按 key 末尾序号排序，丢弃空槽，返回（序号, 值）。

    序号不可解析的槽统一排在最后，并在真正入列时按当前结果长度补一个序号，
    以保留面板写入的先后关系（旧工作流可能带有这类自定义槽名）。
    """
    if not values:
        return []

    ranked = []
    for position, (key, value) in enumerate(values.items()):
        number = _slot_number(key)
        rank = _UNRANKED_SLOT if number is None else number
        # position 作为次键，复现 sorted() 的稳定排序语义
        ranked.append((rank, position, number, value))

    result: list[tuple[int, object]] = []
    for _rank, _position, number, value in sorted(ranked, key=lambda entry: entry[:2]):
        if value is None:
            continue
        result.append((number if number is not None else len(result) + 1, value))
    return result


def ordered_dynamic_values(values: Mapping | None) -> list:
    """只取值部分（保留同样的排序与空槽过滤）。"""
    return [value for _ordinal, value in ordered_dynamic_items(values)]


def separate_masks(av_latent: dict, video: torch.Tensor, audio: torch.Tensor):
    """拆出（视频掩码, 音频掩码）；没有掩码时返回 (None, None)。"""
    masks = av_latent.get("noise_mask")
    if masks is None:
        return None, None
    if getattr(masks, "is_nested", False):
        parts = tuple(masks.unbind())
        if len(parts) == 2:
            return parts
    if isinstance(masks, torch.Tensor):
        # 旧版只带视频掩码：必须原样传给下游，绝不能静默丢弃。
        return masks, None
    _reject("无法识别的 AV noise_mask 布局")


def swap_audio_latent(av_latent: dict, encoded_audio: torch.Tensor, denoise_strength: float) -> dict:
    """用新的音轨 latent 替换 AV 潜空间中的音频部分，并重建噪声掩码。"""
    video, template_audio = av_streams(av_latent)
    fitted = match_audio_latent(encoded_audio, template_audio)
    video_mask, _ = separate_masks(av_latent, video, template_audio)
    if video_mask is None:
        video_mask = torch.ones_like(video)
    audio_mask = torch.full_like(fitted, float(denoise_strength))
    updated = av_latent.copy()
    updated["samples"] = comfy.nested_tensor.NestedTensor((video, fitted))
    updated["noise_mask"] = comfy.nested_tensor.NestedTensor((video_mask, audio_mask))
    return updated
