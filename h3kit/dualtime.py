"""MiniMax H3 双时钟联合采样：视频与音频各走一条 sigma 时钟。

随附三类为它服务的版本兼容补丁：
* ComfyUI 清理流程对已失效弱引用的判空（``_install_gc_guard``）；
* Hybrid 载荷下首/尾帧的时间轴与网格修正（``fix_hybrid_layout`` 等）；
* 锁定源音轨时恢复 FLOW_AV 之前的 inpaint 输入（``_clear_locked_inpaint``）。
"""

from __future__ import annotations

import logging
import math
import types

import torch
import torch.nn.functional as F

import comfy.model_management as model_management
import comfy.model_sampling
import comfy.samplers
import comfy.utils
from comfy.k_diffusion.sampling import to_d
from comfy.ldm.minimax import model as minimax_model

from .geometry import av_streams


# 节点内部使用的采样器/调度器标识，与面板下拉的取值保持一致
PRIMARY_SAMPLER = "h3_dual_clock"
PRIMARY_SCHEDULER = "h3_native_flow"
SAMPLER_CHOICES = [PRIMARY_SAMPLER]
if hasattr(comfy.model_sampling, "ModelSamplingAV"):
    SAMPLER_CHOICES.extend(
        name for name in comfy.samplers.SAMPLER_NAMES if name != PRIMARY_SAMPLER
    )
SCHEDULER_CHOICES = [
    PRIMARY_SCHEDULER,
    *(name for name in comfy.samplers.SCHEDULER_NAMES if name != PRIMARY_SCHEDULER),
]

HYBRID_MARKER = "h3kit-hybrid-marker"
HYBRID_PATCH_VER = 1
RESIZE_PATCH_VER = 1
GC_GUARD_VER = 1

_LATENT_SPATIAL = 16
_TRUNCATED_PASS_EPS = 1e-6


def _install_gc_guard() -> None:
    """规避 ComfyUI 0.33.x 清理流程调用已失效 ``real_model`` 弱引用的问题。

    动态换模（典型路径为 H3 → 潜空间放大 → H3）会留下 ``real_model`` 已被清空的
    LoadedModel 条目，而官方 ``cleanup_models_gc`` 不做判空，首次运行即抛
    ``NoneType is not callable``。该兼容补丁只作用于本包。
    """
    if getattr(model_management.cleanup_models_gc, "_gh_guard_version", None) == GC_GUARD_VER:
        return

    def cleanup_guard():
        # model_unload() 会清空 real_model，却把条目留在 current_loaded_models 里；
        # 不清掉它，下一次采样（high_sigmas → low_sigmas）就会在首轮直接失败。
        loaded = getattr(model_management, "current_loaded_models", ())
        stale = []
        needs_gc = False
        for cur in list(loaded):
            if not callable(getattr(cur, "real_model", None)):
                stale.append(cur)
                continue
            try:
                dead = cur.is_dead()
            except (TypeError, ReferenceError):
                # 弱引用已失效时保守处理：不触发全局清理。
                dead = False
            if dead:
                needs_gc = True
                break
        for cur in stale:
            try:
                loaded.remove(cur)
            except (ValueError, AttributeError):
                pass
        if needs_gc:
            model_management.gc.collect()
            model_management.soft_empty_cache()

    cleanup_guard._gh_guard_version = GC_GUARD_VER
    model_management.cleanup_models_gc = cleanup_guard


def _drop_model_after_sample(model) -> None:
    """采样结束后只释放本包持有的 H3 patcher。

    高低 sigma 分段采样必须这样处理：第一段要在 3D 潜空间放大网络加载之前
    交出 H3 占用；下一次采样时 ComfyUI 会按需重新加载 H3。
    """
    try:
        unload = getattr(model_management, "unload_model_and_clones", None)
        if not callable(unload):
            unload = getattr(model_management, "unload_model_clones", None)
        if callable(unload):
            unload(model)
        else:
            detach = getattr(model, "detach", None)
            if callable(detach):
                detach()
    except Exception as exc:
        # 清理失败绝不能把一次成功的采样变成失败的提示
        logging.debug("MiniMax H3 采样后卸载已跳过：%s", exc)
    try:
        empty_cache = getattr(model_management, "soft_empty_cache", None)
        if callable(empty_cache):
            empty_cache()
    except Exception:
        pass


class TwoClockFlowSampling(
    comfy.model_sampling.ModelSamplingDiscreteFlow,
    comfy.model_sampling.CONST,
):
    @property
    def audio_scale(self):
        # 音频由本采样器自己的 sigma 时钟推进，因此把 ComfyUI 的 FLOW_AV
        # 携带项保持中性，避免音频变换被应用两次。
        return 1.0


def model_has_raw_velocity(model) -> bool:
    """模型是否自带 raw 音频速度（决定音频 delta 要不要除以斜率）。"""
    base_model = getattr(model, "model", None)
    return callable(getattr(base_model, "audio_scale", None))


def _unshift(base_sigma, from_shift: float):
    """把某个 shift 时钟下的 sigma 还原到 shift=1 的基准时钟。"""
    return base_sigma / (from_shift + base_sigma * (1.0 - from_shift))


def sigma_shift(base_sigma, shift: float):
    """把基准时钟的 sigma 变换到指定 shift 的时钟。"""
    return shift * base_sigma / (1.0 + (shift - 1.0) * base_sigma)


def flow_shift_sigma(sigma, from_shift: float, to_shift: float):
    """在两个 shift 时钟之间换算 sigma。"""
    return sigma_shift(_unshift(sigma, from_shift), to_shift)


def flow_shift_slope(sigma, from_shift: float, to_shift: float):
    """两个时钟之间 phi 的斜率，即音频速度的换算系数。"""
    base_sigma = _unshift(sigma, from_shift)
    numerator = to_shift * (1.0 + (from_shift - 1.0) * base_sigma) ** 2
    denominator = from_shift * (1.0 + (to_shift - 1.0) * base_sigma) ** 2
    return numerator / denominator


def flow_sigmas(sample_steps: int, video_sigma_shift: float) -> torch.Tensor:
    """线性 sigma 表按视频时钟做 shift 之后的调度。"""
    base_sigmas = torch.linspace(1.0, 0.0, sample_steps + 1, dtype=torch.float32)
    return sigma_shift(base_sigmas, video_sigma_shift)


def _build_sigmas(model_sampling, scheduler_id: str, sample_steps: int, video_sigma_shift: float):
    """本包自有的调度器直接计算，其余交给 ComfyUI。"""
    if scheduler_id == PRIMARY_SCHEDULER:
        return flow_sigmas(sample_steps, video_sigma_shift)
    if scheduler_id not in comfy.samplers.SCHEDULER_NAMES:
        raise ValueError(f"未知的调度器：{scheduler_id}")
    return comfy.samplers.calculate_sigmas(model_sampling, scheduler_id, sample_steps).cpu()


def _build_sampling(model, original_sampling, video_sigma_shift, audio_sigma_shift, use_native_av):
    """构造采样用的 model_sampling：原生 FLOW_AV 或本包的双时钟实现。"""
    if use_native_av:
        native_av_cls = getattr(comfy.model_sampling, "ModelSamplingAV", None)
        if native_av_cls is None or not model_has_raw_velocity(model):
            raise RuntimeError(
                "所选采样器需要当前版本 MiniMax H3 的 FLOW_AV 支持；"
                "跨版本请使用 h3_dual_clock。"
            )

        class TwoClockNativeAVSampling(native_av_cls, comfy.model_sampling.CONST):
            pass

        model_sampling = TwoClockNativeAVSampling(model.model.model_config)
        model_sampling.set_parameters(shift=video_sigma_shift, audio_shift=audio_sigma_shift)
    else:
        model_sampling = TwoClockFlowSampling(model.model.model_config)
        model_sampling.set_parameters(shift=video_sigma_shift)

    if hasattr(original_sampling, "noise_scale"):
        model_sampling.set_noise_scale(original_sampling.noise_scale)
    return model_sampling


def _clear_locked_inpaint(self, sigma, noise, latent_image, **kwargs):
    """为锁定音轨的 AV 区域恢复 FLOW_AV 之前的 inpainting 输入。

    ComfyUI 通用 FLOW inpaint 会在 KSampler 施加降噪掩码之前把噪声混入
    latent_image，锁定源音轨时 H3 便无法稳定看到干净的驱动音频 latent。
    在采样器克隆出的模型上直接返回 latent_image，即可恢复原始 H3 行为。
    """
    return latent_image


def _has_audio_lock(av_latent: dict) -> bool:
    """AV latent 是否带“部分/完全锁定”的音频掩码。"""
    masks = av_latent.get("noise_mask")
    if not getattr(masks, "is_nested", False):
        return False
    parts = tuple(masks.unbind())
    if len(parts) != 2:
        return False
    return bool(torch.any(parts[1] < 1.0).item())


def _frames_from_latent_t(latent_t: int) -> int:
    """latent 时间长度换算成像素帧数（按 MiniMax H3 的帧网格）。"""
    if latent_t < 1:
        raise RuntimeError("MiniMax H3 目标视频的 latent 时间长度必须为正")
    spans = getattr(minimax_model, "FRAME_PER_TOKEN", (1, 4, 4, 4, 4))
    return sum(spans[index % len(spans)] for index in range(latent_t))


def _hybrid_ref_offset(ref: dict) -> float:
    """一条参考素材在目标时间轴上占据的跨度。"""
    kind = ref.get("kind")
    if kind == HYBRID_MARKER:
        return 0.0
    if kind == "image":
        return 1.0
    if kind == "audio":
        return float(ref.get("track_t", 0))
    if kind in {"video", "video_audio"}:
        frame_rescale = float(getattr(minimax_model, "FRAME_RESCALE", 5.0 / 3.0))
        video_span = frame_rescale * _frames_from_latent_t(int(ref.get("latent_t", 0)))
        return max(float(ref.get("track_t", 0)), video_span)
    raise RuntimeError(f"不支持的 MiniMax H3 Hybrid 参考类型：{kind!r}")


def _packed_layout(out: dict):
    """取出 conditioning 上已有的打包布局对象。"""
    cond = out.get("minimax_payload")
    payload = getattr(cond, "cond", None) if cond is not None else None
    if not isinstance(payload, dict):
        raise RuntimeError("MiniMax H3 Hybrid 补丁取不到打包后的 payload")
    layout = payload.get("layout")
    if layout is None or not hasattr(layout, "signature") or not hasattr(layout, "segments"):
        raise RuntimeError("MiniMax H3 Hybrid 的打包布局缺失或版本不兼容")
    return layout


def _keyframe_cond_t(pixel_index: int, frame_count: int, text_len: int,
                     target_span: float, frame_rescale: float) -> float:
    """把首/尾帧的像素下标换算成目标时间轴上的条件位置。"""
    if pixel_index == 0:
        return float(text_len)
    if pixel_index == frame_count - 1:
        return float(text_len) + target_span - frame_rescale
    raise RuntimeError("MiniMax H3 Hybrid 兼容路径目前只支持精确首帧/尾帧")


def fix_hybrid_layout(out: dict, kwargs: dict) -> dict:
    """打包参考素材之后，把精确首/尾帧重新对齐到目标时间轴。

    当前 ComfyUI 先排布参考素材、再排布目标 AV 序列，但首/尾帧的 RoPE 位置
    仍停留在没有参考素材的时间轴上。只有 Hybrid 载荷需要这一步修正，
    普通 FL2VA 不受影响。
    """
    keyframes = list(kwargs.get("minimax_keyframes") or [])
    refs = list(kwargs.get("minimax_refs") or [])
    if not keyframes or not refs:
        return out

    layout = _packed_layout(out)
    frame_count_value = kwargs.get("minimax_frame_count")
    if frame_count_value is None:
        raise RuntimeError("MiniMax H3 Hybrid conditioning 缺少 minimax_frame_count")
    frame_count = int(frame_count_value)

    text_len, latent_t = int(layout.signature[0]), int(layout.signature[1])
    frame_rescale = float(getattr(minimax_model, "FRAME_RESCALE", 5.0 / 3.0))
    target_span = frame_rescale * _frames_from_latent_t(latent_t)
    ref_offset = sum(_hybrid_ref_offset(ref) for ref in refs)

    cond_segments = [
        (int(start), int(stop))
        for start, stop, kind in layout.segments
        if kind == "cond"
    ]
    if len(cond_segments) != len(keyframes):
        raise RuntimeError("MiniMax H3 Hybrid 的首尾帧数量与布局不一致，已拒绝该兼容补丁")

    for (start, stop), keyframe in zip(cond_segments, keyframes):
        cond_t = _keyframe_cond_t(
            int(keyframe["resolved_frame_index"]), frame_count, text_len, target_span, frame_rescale
        )
        layout.position_ids[start:stop, 0] = cond_t + ref_offset
    return out


def _bind_hybrid_layout(model) -> None:
    """在 extra_conds 之后追加 Hybrid 时间轴修正（幂等）。"""
    original_extra_conds = model.get_model_object("extra_conds")
    if getattr(original_extra_conds, "_gh_hybrid_layout_patch_version", None) is not None:
        return

    def _hooked_extra_conds(_self, **kwargs):
        out = original_extra_conds(**kwargs)
        return fix_hybrid_layout(out, kwargs)

    _hooked_extra_conds._gh_hybrid_layout_patch_version = HYBRID_PATCH_VER
    model.add_object_patch(
        "extra_conds", types.MethodType(_hooked_extra_conds, model.model)
    )


def _scale_keyframe_latent(latent, target_h: int, target_w: int):
    """把 T8 首/尾帧 latent 缩放到目标视频的 latent 网格。

    二段放大改变了目标 H/W，而首/尾帧是按第一段的尺寸编码的。H3 的 ``cond``
    行必须与目标视频使用同一套空间 patch 网格，因此逐时间片缩放，
    同时保持时间长度与通道布局不变。
    """
    if not isinstance(latent, torch.Tensor) or latent.ndim != 5:
        return latent
    if latent.shape[-2:] == (target_h, target_w):
        return latent
    batch, channels, frames, height, width = latent.shape
    per_frame = latent.permute(0, 2, 1, 3, 4).reshape(batch * frames, channels, height, width)
    resized = F.interpolate(per_frame, size=(target_h, target_w), mode="bilinear", align_corners=False)
    return resized.reshape(batch, frames, channels, target_h, target_w).permute(0, 2, 1, 3, 4).contiguous()


def _rebuild_keyframe(item, target_h: int, target_w: int):
    """按目标像素尺寸从原始图像重建首/尾帧 latent；无法重建时返回 None。"""
    if not isinstance(item, dict):
        return None
    existing = item.get("latent")
    if isinstance(existing, torch.Tensor) and existing.ndim == 5:
        # 第一段网格通常已与条件 latent 一致，直接复用可省掉一次 VAE 编码。
        if tuple(existing.shape[-2:]) == (int(target_h), int(target_w)):
            return None
    image = item.get("source_image")
    vae = item.get("video_vae")
    if not isinstance(image, torch.Tensor) or vae is None or image.ndim != 4:
        return None
    pixels_h, pixels_w = int(target_h) * _LATENT_SPATIAL, int(target_w) * _LATENT_SPATIAL
    planar = image[..., :3].movedim(-1, 1)
    upscaled = comfy.utils.common_upscale(planar, pixels_w, pixels_h, "lanczos", "center")
    return vae.encode(upscaled.movedim(1, -1))


def _reorder_cond_video_latents(out: dict, kwargs: dict, keyframes) -> None:
    """按布局段序重建 cond_video_latents，使图像行与模型的布尔掩码一一对应。"""
    cond = out.get("minimax_payload")
    payload = getattr(cond, "cond", None) if cond is not None else None
    if not isinstance(payload, dict):
        return
    layout = payload.get("layout")
    if layout is None:
        return

    refs = list(kwargs.get("minimax_refs") or [])
    ref_latents = [
        ref.get("latent") for ref in refs
        if ref.get("kind") != HYBRID_MARKER and ref.get("latent") is not None
    ]
    ordered = []
    keyframe_index = ref_index = 0
    for _start, _stop, kind in layout.segments:
        if kind == "cond" and keyframe_index < len(keyframes):
            ordered.append(keyframes[keyframe_index]["latent"])
            keyframe_index += 1
        elif kind == "ref_img" and ref_index < len(ref_latents):
            ordered.append(ref_latents[ref_index])
            ref_index += 1
    if ordered:
        payload["cond_video_latents"] = ordered


def _bind_keyframe_conditioning(model) -> None:
    """让首/尾帧条件跟随当前（可能已放大）的目标网格（幂等）。"""
    original_extra_conds = model.get_model_object("extra_conds")
    if getattr(original_extra_conds, "_gh_keyframe_resize_patch_version", None) is not None:
        return

    def _hooked_extra_conds(_self, **kwargs):
        keyframes = kwargs.get("minimax_keyframes")
        latent_shapes = kwargs.get("latent_shapes")
        adjusted_keyframes = list(keyframes or [])
        if keyframes and latent_shapes:
            try:
                target_h = int(latent_shapes[0][3])
                target_w = int(latent_shapes[0][4])
            except (TypeError, IndexError, ValueError):
                target_h = target_w = 0
            if target_h > 0 and target_w > 0:
                adjusted = []
                changed = False
                for item in keyframes:
                    latent = item.get("latent") if isinstance(item, dict) else None
                    resized = _rebuild_keyframe(item, target_h, target_w)
                    if resized is None:
                        resized = _scale_keyframe_latent(latent, target_h, target_w)
                    if resized is not latent:
                        changed = True
                        item = dict(item)
                        item["latent"] = resized
                    adjusted.append(item)
                if changed:
                    kwargs = dict(kwargs)
                    kwargs["minimax_keyframes"] = adjusted
                    adjusted_keyframes = adjusted

        out = original_extra_conds(**kwargs)
        # 官方 MiniMaxH3.extra_conds 处理完首/尾帧后可能用 minimax_refs 覆盖
        # cond_video_latents，这里按布局段序把列表重建回去。
        if adjusted_keyframes and isinstance(out, dict):
            _reorder_cond_video_latents(out, kwargs, adjusted_keyframes)
        return fix_hybrid_layout(out, kwargs)

    _hooked_extra_conds._gh_keyframe_resize_patch_version = RESIZE_PATCH_VER
    model.add_object_patch("extra_conds", types.MethodType(_hooked_extra_conds, model.model))


def _audio_step_gain(sigma_video, sigma_audio, slope_audio, denoise_mask, audio_velocity_is_raw: bool):
    """音频端点在 x0 预测中使用的增益。

    无掩码时直接跟随音频时钟；带掩码时按锁定程度在“视频增益/音频增益”之间插值。
    """
    flat_gain = -sigma_video
    dual_gain = -sigma_audio if audio_velocity_is_raw else -sigma_audio / slope_audio
    if denoise_mask is None:
        return dual_gain
    return flat_gain + denoise_mask * (dual_gain - flat_gain)


def _split_packed(x, audio_values: int):
    """把打包 latent 的最后一维切成（视频, 音频）两段，返回两段长度。"""
    packed_values = int(x.shape[-1])
    video_values = packed_values - int(audio_values)
    if video_values <= 0:
        raise ValueError(
            "MiniMax H3 的打包 latent 相对于音频流过短："
            f"packed={packed_values}, audio={audio_values}"
        )
    return packed_values, video_values


def _audio_denoise_mask(extra_args, packed_values: int, video_values: int):
    """取出音频段的降噪掩码；长度不符时直接报错。"""
    mask = extra_args.get("denoise_mask")
    if mask is None:
        return None
    if mask.shape[-1] != packed_values:
        raise ValueError("MiniMax H3 的降噪掩码与打包 AV latent 长度不一致")
    return mask[..., video_values:]


def _report_dual_clock_step(callback, x, derivative, video_values, step, sigma_video,
                            endpoint_gain, denoised) -> None:
    """回调前把音频端点的 x0 预测修正到与音频时钟一致。"""
    audio_slice = slice(video_values, None)
    denoised[..., audio_slice] = x[..., audio_slice] + derivative[..., audio_slice] * endpoint_gain
    callback({
        "x": x,
        "i": step,
        "sigma": sigma_video,
        "sigma_hat": sigma_video,
        "denoised": denoised,
    })


def run_dual_clock_euler(
    model,
    x,
    sigmas,
    extra_args=None,
    callback=None,
    disable=None,
    *,
    audio_values: int,
    video_sigma_shift: float,
    audio_sigma_shift: float,
    audio_velocity_is_raw: bool = False,
):
    """视频按 sigmas 推进、音频按 audio_sigma_shift 时钟推进的联合欧拉采样。"""
    extra_args = {} if extra_args is None else extra_args
    packed_values, video_values = _split_packed(x, audio_values)
    audio_mask = _audio_denoise_mask(extra_args, packed_values, video_values)

    s_in = x.new_ones([x.shape[0]])
    for step in comfy.utils.model_trange(len(sigmas) - 1, disable=disable):
        sigma_video = sigmas[step]
        sigma_video_next = sigmas[step + 1]
        denoised = model(x, sigma_video * s_in, **extra_args)
        derivative = to_d(x, sigma_video, denoised)

        sigma_audio = flow_shift_sigma(sigma_video, video_sigma_shift, audio_sigma_shift)
        sigma_audio_next = flow_shift_sigma(sigma_video_next, video_sigma_shift, audio_sigma_shift)
        slope_audio = flow_shift_slope(sigma_video, video_sigma_shift, audio_sigma_shift)

        video_delta = sigma_video_next - sigma_video
        audio_delta = sigma_audio_next - sigma_audio
        if not audio_velocity_is_raw:
            audio_delta = audio_delta / slope_audio
        if audio_mask is not None:
            audio_delta = video_delta + audio_mask * (audio_delta - video_delta)

        if callback is not None:
            _report_dual_clock_step(
                callback, x, derivative, video_values, step, sigma_video,
                _audio_step_gain(sigma_video, sigma_audio, slope_audio, audio_mask, audio_velocity_is_raw),
                denoised,
            )

        x = torch.cat((
            x[..., :video_values] + derivative[..., :video_values] * video_delta,
            x[..., video_values:] + derivative[..., video_values:] * audio_delta,
        ), dim=-1)
    return x


def _attach_sampler(patched_model, model, sampler_id, use_native_av,
                    audio, video_sigma_shift, audio_sigma_shift):
    """原生 FLOW_AV 直接用 ComfyUI 的采样器；否则装本包的双时钟欧拉采样器。"""
    if use_native_av:
        return comfy.samplers.sampler_object(sampler_id)

    audio_velocity_is_raw = model_has_raw_velocity(model)
    # 音频长度在空间二段放大中保持不变：采样时从实际打包的 latent 反推
    # 视频/音频切分点，使同一个采样器也能处理放大后的视频流。
    audio_values = math.prod(audio.shape[1:])

    def sampler_impl(model_wrap, x, sigmas, extra_args=None, callback=None, disable=None):
        try:
            return run_dual_clock_euler(
                model_wrap,
                x,
                sigmas,
                extra_args=extra_args,
                callback=callback,
                disable=disable,
                audio_values=audio_values,
                video_sigma_shift=video_sigma_shift,
                audio_sigma_shift=audio_sigma_shift,
                audio_velocity_is_raw=audio_velocity_is_raw,
            )
        finally:
            # 只有被截断的第一段（高 sigma）需要在加载潜空间放大网络前主动卸载；
            # 跑到 sigma 0 的完整采样必须保持模型生命周期不变。
            try:
                truncated = float(sigmas[-1]) > _TRUNCATED_PASS_EPS
            except (TypeError, IndexError, ValueError):
                truncated = False
            if truncated:
                _drop_model_after_sample(patched_model)

    sampler_impl.__name__ = "run_dual_clock_euler"
    sampler = comfy.samplers.KSAMPLER(sampler_impl)
    # 供二段分块采样器读取的元数据：让它用同一套双时钟更新并同步各分块。
    # optional/tiling.py 未随包发布，但符号仍按本仓库命名约定用 _h3_ 前缀。
    sampler._h3_dual_clock = True
    sampler._h3_shift_video = float(video_sigma_shift)
    sampler._h3_shift_audio = float(audio_sigma_shift)
    sampler._h3_audio_velocity_is_raw = bool(audio_velocity_is_raw)
    return sampler


def build_dual_clock_setup(
    model,
    av_latent: dict,
    sample_steps: int,
    video_sigma_shift: float,
    audio_sigma_shift: float,
    sampler_id: str = PRIMARY_SAMPLER,
    scheduler_id: str = PRIMARY_SCHEDULER,
):
    """装配双时钟采样：模型补丁、采样器对象与 sigma 表。"""
    _install_gc_guard()
    video, audio = av_streams(av_latent)
    if video.shape[1] != 24 or audio.shape[1] != 32 or audio.shape[2] != 2:
        raise ValueError(
            f"MiniMax H3 的 AV 通道数异常：video={tuple(video.shape)}, audio={tuple(audio.shape)}"
        )
    if sampler_id not in SAMPLER_CHOICES:
        raise ValueError(f"未知的采样器：{sampler_id}")

    use_native_av = sampler_id != PRIMARY_SAMPLER
    patched_model = model.clone()
    original_sampling = model.get_model_object("model_sampling")
    model_sampling = _build_sampling(
        model, original_sampling, video_sigma_shift, audio_sigma_shift, use_native_av
    )
    patched_model.add_object_patch("model_sampling", model_sampling)

    # Hybrid 参考素材排在前方时，把首/尾帧保持在目标 AV 时间轴上；
    # 补丁只作用于采样器克隆出的 MODEL，并兼顾二段放大带来的网格变化。
    _bind_keyframe_conditioning(patched_model)

    # 为锁定/重混源音轨恢复原始行为：通用 FLOW_AV inpaint 可能在施加降噪掩码
    # 之前把噪声混入源音轨 latent，破坏 lock_source 的口型同步。补丁只作用于
    # 真正带锁定音频掩码的 AV latent，原生生成音轨保持当前 ComfyUI 行为不变。
    if _has_audio_lock(av_latent):
        patched_model.add_object_patch(
            "scale_latent_inpaint",
            types.MethodType(_clear_locked_inpaint, patched_model.model),
        )

    transformer_options = patched_model.model_options.get("transformer_options", {}).copy()
    transformer_options["h3kit_sigma_shift_video"] = video_sigma_shift
    transformer_options["h3kit_sigma_shift_audio"] = audio_sigma_shift
    patched_model.model_options["transformer_options"] = transformer_options

    sampler = _attach_sampler(
        patched_model, model, sampler_id, use_native_av, audio, video_sigma_shift, audio_sigma_shift
    )
    sigmas = _build_sigmas(model_sampling, scheduler_id, sample_steps, video_sigma_shift)
    return patched_model, sampler, sigmas
