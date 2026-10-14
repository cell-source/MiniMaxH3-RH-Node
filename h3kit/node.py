"""MiniMax H3 一体化节点：提示词 → 条件 → 双时钟采样 → 音视频解码。

本包对外只注册这一个节点（``MiniMaxH3IntegrationRH``）。分工：

* 素材读取、面板状态恢复与执行编排在本模块；
* 条件张量装配在 ``h3kit.assembly``，采样在 ``h3kit.dualtime``，AV 解码在 ``h3kit.muxing``；
* 提示词规则与 LLM 客户端在 ``h3kit.rules`` / ``h3kit.llm``。

⚠️ widget 的名称与顺序是工作流存档契约：改动会让旧工作流的 ``widgets_values``
整体错位。``tests/test_node.py`` 锁定了这份顺序。
"""

from __future__ import annotations

import gc
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile

import folder_paths
import nodes
import torch
import comfy.model_management as model_management
from comfy_api.latest import ComfyExtension, io
from comfy_api.latest._input_impl import VideoFromFile
from comfy_extras import nodes_audio, nodes_custom_sampler

from .assembly import compose_conditioning
from .dualtime import (
    PRIMARY_SAMPLER,
    PRIMARY_SCHEDULER,
    SAMPLER_CHOICES,
    SCHEDULER_CHOICES,
    build_dual_clock_setup,
)
from .geometry import snap_frames_down
from .llm import SERVICE_ENDPOINTS as LLM_PROVIDERS, make_client
from .muxing import unpack_av_latent
from .rules import generate_prompt
from .tagging import apply_advanced_constraints


MENU_CATEGORY = "RH/MiniMax H3 Integration"
RESOLUTION_CEILING = 16384
ASPECT_TABLE = {
    "adaptive": None,
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "3:2": 3 / 2,
    "2:3": 2 / 3,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "1:1": 1.0,
    "21:9": 21 / 9,
}
DEFAULT_TEXT_ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
DEFAULT_VIDEO_MODEL = "minimax_h3_video_vae_fp16.safetensors"
DEFAULT_AUDIO_MODEL = "minimax_h3_audio_vae_fp32.safetensors"

_FALLBACK_RATIO = 16 / 9
_SPEED_REFERENCE_FPS = 24
_FRAME_GRID = 17
_FRAME_BASE = 5
_HIDDEN_SENTINEL = "(none)"
_PANEL_MODES = ("text_keyframes", "all_reference")
_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})
_REF_VIDEO_SLOTS = tuple(f"footage_{i}" for i in range(1, 4))
_REF_IMAGE_SLOTS = tuple(f"still_{i}" for i in range(1, 10))
_REF_AUDIO_SLOTS = tuple(f"track_{i}" for i in range(1, 4))
_REF_VIDEO_AUDIO_SLOTS = tuple(f"footage_audio_{i}" for i in range(1, 4))


def _first_output(value):
    """兼容 V1（tuple）与 V3（io.NodeOutput）两种返回值，取第一个输出。"""
    result = getattr(value, "result", None)
    if result is not None:
        return result[0]
    if isinstance(value, tuple):
        return value[0]
    return value


def _all_outputs(value):
    """兼容两种调用形式，取完整输出元组。"""
    result = getattr(value, "result", None)
    if result is not None:
        return tuple(result)
    if isinstance(value, tuple):
        return value
    return (value,)


def _list_files(content_type: str):
    """列出 input 目录中指定类型的文件名（排序后交给 Combo）。"""
    entries = os.listdir(folder_paths.get_input_directory())
    return sorted(folder_paths.filter_files_content_types(entries, [content_type]))


def _list_images():
    return _list_files("image")


def _list_videos():
    return _list_files("video")


def _list_audio():
    return _list_files("audio")


def _media_input(name, tooltip=None, hidden=False):
    """自定义上传的媒体文件名：用可选 STRING 存放，而不是会做枚举校验的 Combo。

    DOM 面板自己管上传控件。固定的上传 Combo 无法表示“未使用”的槽位——
    ComfyUI 会校验每个提交上来的 Combo 值，旧的 ``(none)`` 哨兵值会让末尾的
    空槽位变成非法值。空字符串是合法的 optional STRING，交给文件加载器时
    会自然转成 None。
    """
    return io.String.Input(
        name,
        default="",
        optional=True,
        tooltip=tooltip,
        extra_dict={"hidden": True} if hidden else None,
    )


def _hidden_choice(name, options, default=None):
    """不渲染但会序列化的下拉选项。"""
    return io.Combo.Input(name, options=options, default=default, extra_dict={"hidden": True})


def _flag_input(name, default):
    return io.Boolean.Input(name, default=default, extra_dict={"hidden": True})


def _hidden_number(name, default, minimum, maximum, step, round_value=None):
    return io.Float.Input(
        name,
        default=default,
        min=minimum,
        max=maximum,
        step=step,
        round=round_value,
        extra_dict={"hidden": True},
    )


def _hidden_count(name, default, minimum, maximum, step=1):
    return io.Int.Input(
        name,
        default=default,
        min=minimum,
        max=maximum,
        step=step,
        extra_dict={"hidden": True},
    )


def _coerce_bool(value, default=True):
    """旧工作流可能把隐藏 widget 回放成 ``"(none)"`` 或字符串。"""
    if value is None or value == "" or value == _HIDDEN_SENTINEL:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


def _as_int(value, default=0, minimum=None, maximum=None):
    """把旧存档里的 widget 值安全地转成整数，并夹到给定区间。"""
    if value is None or value == "" or value == _HIDDEN_SENTINEL:
        result = int(default)
    else:
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            result = int(default)
    if minimum is not None:
        result = max(int(minimum), result)
    if maximum is not None:
        result = min(int(maximum), result)
    return result


def _parse_panel_state(panel_state):
    """解析面板写入的状态 JSON；不可用时返回 None。"""
    if not panel_state or panel_state == _HIDDEN_SENTINEL:
        return None
    try:
        state = json.loads(panel_state)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def _panel_media_items(state):
    """取面板媒体列表并统一成 (槽位, 条目) 序列；结构不可识别时返回 None。

    ``media`` 既可能是面板写入的二元组列表，也可能是旧版本或手编工作流里的
    dict，两种情况都拉平成键值对再消费。
    """
    media = state.get("media", [])
    if isinstance(media, dict):
        return list(media.items())
    if isinstance(media, list):
        return media
    return None


def _recover_panel_state(panel_state, panel_mode, prompt, media_values):
    """从工作流序列化的状态里恢复由 DOM 面板拥有的取值。"""
    state = _parse_panel_state(panel_state)
    if state is None:
        return panel_mode, prompt, media_values

    restored_mode = state.get("mode")
    if restored_mode in _PANEL_MODES:
        panel_mode = restored_mode

    prompts = state.get("prompts")
    if isinstance(prompts, dict) and isinstance(prompts.get(panel_mode), str):
        prompt = prompts[panel_mode]
    elif isinstance(state.get("prompt"), str):
        prompt = state["prompt"]

    # 媒体列表存在时它就是唯一真源（包括“删除”这个动作）：
    # 这样被删掉的素材不会因为隐藏 widget 里的旧值而复活。
    serialized_media = state.get("media")
    if isinstance(serialized_media, list):
        restored = {}
        for item in serialized_media:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            slot, entry = item
            if slot not in media_values or not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name and name != _HIDDEN_SENTINEL:
                restored[slot] = name
        media_values = {name: restored.get(name, "") for name in media_values}

    return panel_mode, prompt, media_values


def _panel_first_visual(panel_state, mode):
    """面板里第一个有效视觉素材的文件名，决定自适应画幅。"""
    state = _parse_panel_state(panel_state)
    if state is None:
        return None
    media = _panel_media_items(state)
    if media is None:
        return None
    if mode == "text_keyframes":
        ordered_slots = ["keyframe_open", "keyframe_close"]
    else:
        # Ref2VA 面板把视频排在图片前面，与上传先后无关。
        ordered_slots = [
            *(f"footage_{i}" for i in range(1, 4)),
            *(f"still_{i}" for i in range(1, 10)),
        ]
    for slot in ordered_slots:
        entry = next((item_entry for item_slot, item_entry in media if item_slot == slot), None)
        if not isinstance(entry, dict) or entry.get("kind") not in {"image", "video"}:
            continue
        name = entry.get("name")
        if isinstance(name, str) and name and name != _HIDDEN_SENTINEL:
            return name
    return None


def _panel_muted_slots(panel_state):
    """被面板静音的参考视频槽位集合。"""
    state = _parse_panel_state(panel_state)
    if state is None:
        return set()
    media = _panel_media_items(state)
    if media is None:
        return set()
    return {
        slot for slot, entry in media
        if isinstance(slot, str) and slot.startswith("footage_")
        and isinstance(entry, dict) and bool(entry.get("muted"))
    }


def _panel_trim_ranges(panel_state):
    """面板保存的非破坏性音频选区，按媒体槽位索引。"""
    state = _parse_panel_state(panel_state)
    if state is None:
        return {}
    media = _panel_media_items(state)
    if media is None:
        return {}
    ranges = {}
    for item in media:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        slot, entry = item
        if not isinstance(slot, str) or not isinstance(entry, dict) or entry.get("kind") != "audio":
            continue
        try:
            start = max(0.0, float(entry.get("trimStart", 0) or 0))
            raw_end = entry.get("trimEnd")
            end = float(raw_end) if raw_end is not None else None
        except (TypeError, ValueError):
            continue
        if end is not None and end > start:
            ranges[slot] = (start, end)
    return ranges


def _pick_default_model(options, preferred):
    """优先选首选模型；不在列表中时退回第一项。"""
    return preferred if preferred in options else (options[0] if options else None)


def _drop_text_encoder(clip) -> None:
    """尽力只释放文本编码器占用的显存，不碰下游还要用的两个 VAE。"""
    patcher = getattr(clip, "patcher", None)
    unloaded = False
    try:
        if patcher is not None:
            unload = getattr(model_management, "unload_model_and_clones", None)
            if not callable(unload):
                # 兼容仍在使用旧名字的 ComfyUI 版本
                unload = getattr(model_management, "unload_model_clones", None)
            if callable(unload):
                unload(patcher)
                unloaded = True
    except Exception as exc:
        logging.warning("MiniMax H3 无法显式卸载文本编码器：%s", exc)
    finally:
        if patcher is not None and not unloaded:
            try:
                # 最后兜底：只脱离这一个 patcher。绝不能在这里做全局卸载——
                # 两个 VAE 仍要被下游节点使用。
                detach = getattr(patcher, "detach", None)
                if callable(detach):
                    detach()
            except Exception as exc:
                logging.warning("MiniMax H3 无法脱离文本编码器：%s", exc)
        try:
            empty_cache = getattr(model_management, "soft_empty_cache", None)
            if callable(empty_cache):
                empty_cache()
        except Exception as exc:
            logging.warning("MiniMax H3 无法清理文本编码器缓存：%s", exc)


def _floor_to_32(value: float) -> int:
    """把一条边长四舍五入到最近的 32 网格线，且不小于一个步长。"""
    return max(32, int(value / 32 + 0.5) * 32)


def _adaptive_ratio(source_width, source_height) -> float:
    """素材宽高比；退化输入（缺失 / 0 / 非有限值）退回 16:9。"""
    try:
        ratio = float(source_width) / float(source_height)
    except (TypeError, ValueError, ZeroDivisionError):
        return _FALLBACK_RATIO
    if not math.isfinite(ratio) or ratio <= 0:
        return _FALLBACK_RATIO
    return ratio


def resolve_canvas(frame_ratio: str, pixel_budget: float, source_width=None, source_height=None):
    """按画幅预设（或素材自适应）与目标百万像素算出画布尺寸。"""
    ratio = ASPECT_TABLE.get(frame_ratio, _FALLBACK_RATIO)
    if ratio is None:
        ratio = _adaptive_ratio(source_width, source_height)
    area = max(0.05, float(pixel_budget)) * 1024 * 1024
    # 两条边各自按目标宽高比取整：若用 area / 已取整宽度，连 1:1 都会在
    # 第二次取整后漂移。
    width = _floor_to_32((area * ratio) ** 0.5)
    height = _floor_to_32((area / ratio) ** 0.5)
    if ratio == 1.0:
        height = width
    return min(RESOLUTION_CEILING, width), min(RESOLUTION_CEILING, height)


def resolve_frame_length(clip_seconds: float) -> int:
    """把时长换算成落在 17n+5 帧网格上的帧数。"""
    requested = max(_FRAME_BASE, round(float(clip_seconds) * _SPEED_REFERENCE_FPS))
    return requested + (_FRAME_BASE - requested % _FRAME_GRID) % _FRAME_GRID


def _read_image_asset(value):
    """读取一张上传图像；空槽位返回 None。"""
    if not value or value == _HIDDEN_SENTINEL:
        return None
    image, _mask = nodes.LoadImage().load_image(value)
    return image


def _read_audio_asset(value):
    """读取一条上传音轨；ComfyUI 加载器失败时走 FFmpeg 兜底。"""
    if not value or value == _HIDDEN_SENTINEL:
        return None
    try:
        return nodes_audio.LoadAudio.load(value)[0]
    except Exception as primary_error:
        return _decode_audio_via_ffmpeg(value, primary_error)


def _decode_audio_via_ffmpeg(value, primary_error):
    """FFmpeg 兼容兜底：把源文件解成 PCM WAV 再交给 ComfyUI 的加载器。

    ComfyUI 的 PyAV 加载器会拒绝一些实际可用的文件（少数 FLAC 编码器，或带
    损坏元数据/帧块的文件）。经 FFmpeg 转一道还能顺带支持 M4A/AAC、OGG/Opus、
    WMA、AIFF 等常见格式，同时不改变 H3 的 AUDIO 契约。
    """
    source_path = folder_paths.get_annotated_filepath(value)
    try:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

        with tempfile.TemporaryDirectory(prefix="minimax_h3_audio_") as temp_dir:
            wav_path = os.path.join(temp_dir, "decoded.wav")
            process = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner", "-loglevel", "error", "-y",
                    "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
                    "-i", source_path,
                    "-vn", "-map", "0:a:0",
                    "-c:a", "pcm_s16le", wav_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                # 损坏媒体可让 ffmpeg 挂死：超时中断，不占死 worker。
                timeout=120,
            )
            if process.returncode != 0 or not os.path.isfile(wav_path):
                details = (process.stderr or "").strip()[-1200:]
                raise RuntimeError(details or f"FFmpeg 退出码 {process.returncode}")
            waveform, sample_rate = nodes_audio.load(wav_path)
            logging.info(
                "MiniMax H3 已通过 FFmpeg 兼容兜底解码音频：%s",
                os.path.basename(source_path),
            )
            return {"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate}
    except subprocess.TimeoutExpired as timeout_error:
        raise RuntimeError(
            f"音频解码超时（120 秒）：{os.path.basename(str(source_path))}。文件可能损坏或过大。"
        ) from timeout_error
    except Exception as fallback_error:
        raise RuntimeError(
            f"无法解码音频 {os.path.basename(str(source_path))}。"
            "节点支持常见音频格式，但该文件可能已损坏、扩展名与实际编码不符，"
            "或当前环境缺少可用的 FFmpeg。"
            f"\nComfyUI/PyAV：{primary_error}"
            f"\nFFmpeg 兜底：{fallback_error}"
        ) from fallback_error


def _clip_audio_length(audio, clip_seconds):
    """把 AUDIO 值裁/补到正好等于请求时长（采样率不变）。"""
    if audio is None:
        return None
    waveform = audio["waveform"]
    sample_rate = int(audio["sample_rate"])
    sample_count = max(1, round(float(clip_seconds) * sample_rate))
    trimmed = waveform[..., :sample_count]
    if trimmed.shape[-1] < sample_count:
        trimmed = torch.nn.functional.pad(trimmed, (0, sample_count - trimmed.shape[-1]))
    return {"waveform": trimmed, "sample_rate": sample_rate}


def _apply_sound_choice(audio, selection):
    """按面板保存的选区裁剪 AUDIO 值，采样率不变。"""
    if audio is None or not selection:
        return audio
    waveform = audio["waveform"]
    sample_rate = int(audio["sample_rate"])
    start_seconds, end_seconds = selection
    start_sample = max(0, min(waveform.shape[-1], round(float(start_seconds) * sample_rate)))
    end_sample = max(start_sample + 1, min(waveform.shape[-1], round(float(end_seconds) * sample_rate)))
    return {"waveform": waveform[..., start_sample:end_sample], "sample_rate": sample_rate}


def _resample_indices(source_fps: float, target_frames: int) -> torch.Tensor:
    """按源播放速率在 24fps 节拍上取出帧下标。"""
    if source_fps <= 0:
        return torch.arange(target_frames, dtype=torch.long)
    # 保持源播放速率：60fps → 24fps 按 24fps 节拍丢帧；12fps → 24fps 按节拍重复源帧。
    positions = torch.arange(target_frames, dtype=torch.float32) * source_fps / _SPEED_REFERENCE_FPS
    return torch.floor(positions).long()


def _pick_video_frames(frames, source_fps, target_frames):
    """把源视频按 24fps 采样，且不改变播放速度。

    调用方传入 H3 的目标帧数上限：更长的参考素材裁到这个窗口，
    更短的素材保留自身时长，而不是重复末帧去填满生成时长。
    """
    if frames is None or frames.shape[0] <= 1:
        return frames
    source_fps = float(source_fps or 0)
    requested = max(1, int(target_frames))

    if requested <= 30:
        # 旧调用方传的是帧率而不是帧数：按源时长推算目标帧数。
        duration = min(frames.shape[0] / source_fps, 360 / _SPEED_REFERENCE_FPS) if source_fps > 0 else 0
        target_frames = min(360, max(48, round(duration * requested)))
        if source_fps > 0:
            source_end = min(frames.shape[0] - 1, max(0, math.ceil(duration * source_fps) - 1))
            indices = torch.linspace(0, source_end, target_frames).round().long()
            return frames[indices]
    elif source_fps > 0:
        # 保持源播放速度时，目标帧数不能超过源时长能提供的帧数。
        source_duration_frames = max(1, round(frames.shape[0] * _SPEED_REFERENCE_FPS / source_fps))
        target_frames = min(requested, source_duration_frames)
    else:
        target_frames = min(requested, frames.shape[0])

    source_indices = _resample_indices(source_fps, target_frames).clamp(max=frames.shape[0] - 1)
    return frames[source_indices][:target_frames]


# 参考视频解码内存上限（字节）：1080p 单帧约 6.2MB，差不多 320 帧满窗；
# 窗口外的内容与“只取前 target_frames 帧”的语义无关，提前裁窗防 OOM。
_MAX_VIDEO_DECODE_BYTES = 2 * 1024 * 1024 * 1024
_VIDEO_WINDOW_SECONDS = 30  # 解码窗口上限（秒），已覆盖最大 360 帧 @24fps（15 秒）
_BYTES_PER_PIXEL = 3


def _probe_video_meta(path):
    """尽力探测视频元数据，返回（宽, 高, 帧率, 时长秒）；失败返回 None。"""
    try:
        import av
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            fps = float(stream.average_fps) if stream.average_fps else 0.0
            duration = float(stream.duration or 0)
            if stream.duration is None and container.duration:
                duration = container.duration / 1_000_000
            return (stream.codec_context.width, stream.codec_context.height, fps, duration)
    except Exception:
        return None


def _decode_window(path):
    """估算全量解码内存；超限时返回只解码前段的参数。"""
    meta = _probe_video_meta(path)
    if meta is None:
        return {}
    width, height, fps, duration = meta
    if not width or not height or duration <= 0:
        return {}
    estimate = width * height * _BYTES_PER_PIXEL * max(
        1, round(duration * (fps or _SPEED_REFERENCE_FPS))
    )
    if estimate <= _MAX_VIDEO_DECODE_BYTES:
        return {}
    # 超限：只解码前 _VIDEO_WINDOW_SECONDS 秒（不足该窗口时按实际时长解码）。
    return {"duration": min(_VIDEO_WINDOW_SECONDS, duration)}


def _read_video_frames(value, target_frames):
    """读取参考视频的帧与随附音轨；空槽位返回 (None, None)。"""
    if not value or value == _HIDDEN_SENTINEL:
        return None, None
    path = folder_paths.get_annotated_filepath(value)
    components = VideoFromFile(path, **_decode_window(path)).get_components()
    frames = _pick_video_frames(components.images, components.frame_rate, target_frames)
    return frames, components.audio


def _pick_visual_source(panel_mode, serialized_first_visual, keyframe_open, keyframe_close,
                        first, last, refs, ref_videos, footage_names, still_names):
    """选出决定自适应画幅的第一张视觉素材。

    面板记录的首个视觉素材优先（顺序：关键帧模式看首/尾帧，参考模式先视频后图片）；
    面板信息缺失或对应素材加载失败时，退回“按同样顺序找到的第一张”。
    """
    if panel_mode == "text_keyframes":
        if serialized_first_visual and keyframe_open == serialized_first_visual:
            return first
        if serialized_first_visual and keyframe_close == serialized_first_visual:
            return last
        return first if first is not None else last

    selected = None
    if serialized_first_visual:
        for slot, filename in zip(_REF_VIDEO_SLOTS, footage_names):
            if filename == serialized_first_visual:
                selected = ref_videos.get(slot)
                break
        if selected is None:
            for slot, filename in zip(_REF_IMAGE_SLOTS, still_names):
                if filename == serialized_first_visual:
                    selected = refs.get(slot)
                    break
    if selected is not None:
        return selected

    ordered = [ref_videos.get(slot) for slot in _REF_VIDEO_SLOTS]
    ordered += [refs.get(slot) for slot in _REF_IMAGE_SLOTS]
    for value in ordered:
        if value is not None:
            return value
    return None


def _load_reference_audios(values, trim_ranges, effective_duration):
    """按槽位读取、裁剪并过滤独立参考音轨，返回 {槽位: AUDIO}。"""
    loaded = {}
    for index, value in enumerate(values, 1):
        slot = f"track_{index}"
        audio = _clip_audio_length(
            _apply_sound_choice(_read_audio_asset(value), trim_ranges.get(slot)),
            effective_duration,
        )
        if audio is not None:
            loaded[slot] = audio
    return loaded


def _select_drive_audio(panel_mode, audio_source, driving_track, video_audio, audios):
    """挑出驱动口型/锁定音轨的那一条音频。

    返回（最终序号, 选中的音轨）。序号 0 表示不驱动；但在参考模式的 lock_source
    下会退化为 1，而不是静默变成原生生成音频。
    """
    ordered = [
        video_audio[slot] for slot in _REF_VIDEO_AUDIO_SLOTS if slot in video_audio
    ]
    ordered += list(audios.values())
    if (
        panel_mode == "all_reference"
        and audio_source == "lock_source"
        and driving_track == 0
        and ordered
    ):
        driving_track = 1
    selected = (
        ordered[driving_track - 1]
        if panel_mode == "all_reference" and 0 < driving_track <= len(ordered)
        else None
    )
    return driving_track, selected


def _load_models(text_encoder, video_decoder, audio_decoder):
    """加载文本编码器与两个 VAE（沿用 ComfyUI 标准节点的加载路径）。"""
    clip = nodes.CLIPLoader().load_clip(text_encoder, "minimax")[0]
    video_vae = nodes.VAELoader().load_vae(video_decoder)[0]
    audio_vae = nodes.VAELoader().load_vae(audio_decoder)[0]
    return clip, video_vae, audio_vae


class AllInOneNode(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        image_files = _list_images()
        video_files = _list_videos()
        audio_files = _list_audio()
        return io.Schema(
            node_id="MiniMaxH3IntegrationRH",
            category=MENU_CATEGORY,
            display_name="MiniMax H3 All-in-One (RH)",
            description=(
                "All-in-one MiniMax H3 node: optional AI/offline prompt generation, "
                "conditioning build, dual-clock sampling, and AV decode. "
                "The diffusion model remains external."
            ),
            inputs=[
                _hidden_choice("panel_mode", ["text_keyframes", "all_reference"], "text_keyframes"),
                io.Combo.Input(
                    "text_encoder",
                    options=folder_paths.get_filename_list("text_encoders"),
                    default=_pick_default_model(folder_paths.get_filename_list("text_encoders"), DEFAULT_TEXT_ENCODER),
                ),
                io.Combo.Input(
                    "video_decoder",
                    options=folder_paths.get_filename_list("vae"),
                    default=_pick_default_model(folder_paths.get_filename_list("vae"), DEFAULT_VIDEO_MODEL),
                ),
                io.Combo.Input(
                    "audio_decoder",
                    options=folder_paths.get_filename_list("vae"),
                    default=_pick_default_model(folder_paths.get_filename_list("vae"), DEFAULT_AUDIO_MODEL),
                ),
                io.Combo.Input("frame_ratio", options=list(ASPECT_TABLE), default="adaptive"),
                io.Float.Input("pixel_budget", default=0.5, min=0.2, max=2.0, step=0.1, round=0.1),
                io.Int.Input(
                    "clip_seconds",
                    default=5,
                    min=2,
                    max=30,
                    step=1,
                ),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True, default="", extra_dict={"hidden": True}),
                _media_input("keyframe_open", tooltip="Optional first frame", hidden=True),
                _media_input("keyframe_close", tooltip="Optional last frame", hidden=True),
                _media_input("keyframe_audio", tooltip="Optional audio for keyframe Hybrid", hidden=True),
                _media_input("still_1", hidden=True),
                _media_input("still_2", hidden=True),
                _media_input("still_3", hidden=True),
                _media_input("still_4", hidden=True),
                _media_input("still_5", hidden=True),
                _media_input("still_6", hidden=True),
                _media_input("still_7", hidden=True),
                _media_input("still_8", hidden=True),
                _media_input("still_9", hidden=True),
                _media_input("footage_1", hidden=True),
                _media_input("footage_2", hidden=True),
                _media_input("footage_3", hidden=True),
                _media_input("track_1", hidden=True),
                _media_input("track_2", hidden=True),
                _media_input("track_3", hidden=True),
                _hidden_choice("task_hint", ["auto", "t2va", "i2va", "l2va", "fl2va", "ref2va", "hybrid"], "auto"),
                _hidden_choice("audio_source", ["native", "lock_source", "reference_only", "remix_source"], "lock_source"),
                _hidden_number("audio_cleanup", 0.0, 0.0, 1.0, 0.01, 0.01),
                # Match T8: 1 selects the first audio; 0 disables remapping.
                _hidden_count("driving_track", 1, 0, 6),
                _flag_input("tag_guard", True),
                _hidden_choice("asset_scale", ["match", "1.2x", "1.5x", "2x", "max"], "match"),
                # Keep this last so adding persistence does not shift legacy
                # widgets_values positions for any existing input.
                io.String.Input("panel_state", default="", optional=True, extra_dict={"hidden": True}),
                # RH advanced prompt constraints. Keep these last so legacy
                # workflow widget-value positions never shift.
                _flag_input("subtitle_free", True),
                _flag_input("soundscape", False),
                _flag_input("music", False),
                # ===== 采样（原 DualClock + 标准 RandomNoise/BasicGuider/SamplerCustomAdvanced 内联，2026-09-24 整合）=====
                io.Model.Input("model"),
                io.Int.Input("sample_steps", default=4, min=1, max=1000),
                io.Float.Input("video_sigma_shift", default=12.0, min=0.01, max=100.0, step=0.01, advanced=True),
                io.Float.Input("audio_sigma_shift", default=3.0, min=0.01, max=100.0, step=0.01, advanced=True),
                io.Combo.Input("sampler_id", options=SAMPLER_CHOICES, default=PRIMARY_SAMPLER, optional=True),
                io.Combo.Input("scheduler_id", options=SCHEDULER_CHOICES, default=PRIMARY_SCHEDULER, optional=True),
                io.Int.Input("seed_value", default=0, min=0, max=0xffffffffffffffff, control_after_generate=True),
                # ===== 提示词生成（原独立 PromptGenerator 节点内联）=====
                io.Combo.Input(
                    "prompt_origin",
                    options=["panel", "ai", "offline", "format"],
                    default="panel",
                    tooltip="panel=使用提示词编辑器/上游内容；ai=AI 在线生成；offline=离线原文包装；format=整理完整 H3 提示词",
                ),
                io.String.Input("ai_idea", multiline=True, default="", dynamic_prompts=False),
                io.Combo.Input("ai_locale", options=["zh", "mixed", "en"], default="zh"),
                io.Combo.Input("ai_task", options=["auto", "t2va", "i2va", "fl2va", "l2va", "ref2va"], default="auto"),
                io.Boolean.Input("ai_expand", default=False),
                io.Boolean.Input("ai_ambience", default=False),
                io.Boolean.Input("ai_score", default=False),
                io.Boolean.Input("ai_timeline", default=True),
                io.Boolean.Input("ai_lock_camera", default=True),
                io.Boolean.Input("ai_stabilize", default=False),
                io.Boolean.Input("ai_subtitle_free", default=True),
                io.Boolean.Input("ai_audio_guard", default=True),
                io.Boolean.Input("ai_strict_check", default=True),
                io.Combo.Input(
                    "ai_platform",
                    options=sorted(LLM_PROVIDERS),
                    default="deepseek",
                    tooltip="大模型服务预设：选定后自动使用其端点与默认模型；仍可用 ai_base_url/ai_model_name 覆盖。Key 在 ai_key 填写（或用对应环境变量）。",
                ),
                io.String.Input("ai_key", default="", optional=True, tooltip="由用户自行填写的大模型 API Key；留空时读取对应服务商环境变量（如 DEEPSEEK_API_KEY/OPENAI_API_KEY 等）。此字段会保存到工作流，分享前请清空。"),
                io.String.Input("ai_base_url", default="", optional=True),
                io.String.Input("ai_model_name", default="", optional=True),
                io.Int.Input("ai_deadline", default=180, min=15, max=600, optional=True),
                # Optional upstream prompt（forceInput）。2026-09-26 审查 A4：forceInput 输入在前端
                # 加载时会占一个 widgets_values「幻影槽位」（保存时不占，反而由面板 DOM widget 的
                # 尾值回填）。它曾排在 panel_state 之后，导致每次「保存→重开」
                # sampler_id/scheduler_id/ai_* 整体左移一位直至被冲空。移到输入列表末位后，
                # 幻影槽位落在末尾：所有具名 widget 的加载/保存位置对称，旧工作流也能正确对齐。
                io.String.Input(
                    "upstream_prompt",
                    display_name="Prompt",
                    optional=True,
                    force_input=True,
                    tooltip="Optional upstream prompt; overrides the local prompt editor when connected",
                ),
            ],
            outputs=[
                io.Image.Output("frames"),
                io.Audio.Output("audio"),
                io.Latent.Output("video_latent"),
                io.String.Output("report"),
            ],
        )

    @classmethod
    def execute(cls, panel_mode, frame_ratio, pixel_budget, clip_seconds, text_encoder, video_decoder, audio_decoder,
                prompt, keyframe_open, keyframe_close, keyframe_audio, still_1, still_2, still_3,
                still_4, still_5, still_6, still_7, still_8, still_9,
                footage_1, footage_2, footage_3, track_1, track_2, track_3,
                task_hint, audio_source, audio_cleanup,
                driving_track, tag_guard, asset_scale,
                model=None, sample_steps=4, video_sigma_shift=12.0, audio_sigma_shift=3.0,
                sampler_id=PRIMARY_SAMPLER, scheduler_id=PRIMARY_SCHEDULER, seed_value=0,
                prompt_origin="panel", ai_idea="", ai_locale="zh", ai_task="auto",
                ai_expand=False, ai_ambience=False, ai_score=False, ai_timeline=True,
                ai_lock_camera=True, ai_stabilize=False, ai_subtitle_free=True,
                ai_audio_guard=True, ai_strict_check=True, ai_platform="deepseek",
                ai_key="", ai_base_url="", ai_model_name="", ai_deadline=180,
                panel_state="", upstream_prompt=None,
                subtitle_free=True, soundscape=False, music=False):
        # ComfyUI can replay an older hidden-widget snapshot as the literal
        # string "(none)". T8 treats this control as an int, with 0 disabling
        # remapping, so normalize it before calling the shared conditioning.
        driving_track = _as_int(driving_track, default=1, minimum=0, maximum=6)
        # A4（2026-09-26）兑底：位移曾把 sampler_id/scheduler_id 冲成空串、ai_deadline 冲成 ""。
        # 空组合无法采样、空超时会在 make_client 内 TypeError；这里回 schema 默认。
        if sampler_id not in SAMPLER_CHOICES:
            sampler_id = PRIMARY_SAMPLER
        if scheduler_id not in SCHEDULER_CHOICES:
            scheduler_id = PRIMARY_SCHEDULER
        ai_deadline = _as_int(ai_deadline, default=180, minimum=15, maximum=600)
        media_values = {
            "keyframe_open": keyframe_open, "keyframe_close": keyframe_close, "keyframe_audio": keyframe_audio,
            "still_1": still_1, "still_2": still_2, "still_3": still_3,
            "still_4": still_4, "still_5": still_5, "still_6": still_6,
            "still_7": still_7, "still_8": still_8, "still_9": still_9,
            "footage_1": footage_1, "footage_2": footage_2, "footage_3": footage_3,
            "track_1": track_1, "track_2": track_2, "track_3": track_3,
        }
        panel_mode, prompt, media_values = _recover_panel_state(
            panel_state, panel_mode, prompt, media_values
        )
        # 提示词解析（2026-09-24 整合原 PromptGenerator 节点）：
        # panel → 提示词编辑器/上游内容（保留原有约束开关与 upstream_prompt 语义）；
        # ai/offline/format → 走提示词引擎生成后再进入条件构建。
        if prompt_origin != "panel":
            interrupt = model_management.throw_exception_if_processing_interrupted
            interrupt()
            if clip_seconds > 15:
                raise ValueError("AI/离线/整理提示词生成仅支持 2–15 秒；更长时长请直接在提示词编辑器中撰写")
            first_present = bool(media_values.get("keyframe_open") and media_values["keyframe_open"] != "(none)")
            last_present = bool(media_values.get("keyframe_close") and media_values["keyframe_close"] != "(none)")
            inferred_mode = ("ref2va" if panel_mode == "all_reference" else
                             "fl2va" if first_present and last_present else
                             "i2va" if first_present else "l2va" if last_present else "t2va")
            gen_mode = ai_task if ai_task != "auto" else inferred_mode
            client = (make_client(ai_platform, ai_key, ai_base_url, ai_model_name, ai_deadline, interrupt)
                      if prompt_origin == "ai" else None)
            controls = {"mode": gen_mode, "lang": ai_locale, "duration": str(clip_seconds),
                        "ratio": frame_ratio if frame_ratio in {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"} else "16:9",
                        "model": ai_platform, "enrich_do_enrich": ai_expand,
                        "enrich_soundscape": ai_ambience, "enrich_music": ai_score,
                        "auto_timestamps": ai_timeline, "fixed_camera": ai_lock_camera,
                        "visual_stability": ai_stabilize, "no_subtitles": ai_subtitle_free,
                        "anti_pop": ai_audio_guard}
            result = generate_prompt(ai_idea, prompt_origin, controls, client, interrupt)
            if ai_strict_check and not result["valid"]:
                raise ValueError("H3 提示词校验失败：" + result["report"] + "；可关闭 ai_strict_check 输出文本供修改")
            prompt = result["prompt"]
            # 提示词模式决定素材通路：ref2va 走全参考，其余走首尾帧
            panel_mode = "all_reference" if gen_mode == "ref2va" else "text_keyframes"
        elif upstream_prompt is not None:
            prompt = str(upstream_prompt)
        else:
            # A connected generator owns its language and sound/visual options.
            # Apply editor options only to prompts authored in this node.
            prompt = apply_advanced_constraints(
                prompt,
                subtitle_free=_coerce_bool(subtitle_free, True),
                soundscape=_coerce_bool(soundscape, False),
                music=_coerce_bool(music, False),
            )
        keyframe_open = media_values["keyframe_open"]
        keyframe_close = media_values["keyframe_close"]
        keyframe_audio = media_values["keyframe_audio"]
        still_1, still_2, still_3 = (media_values[f"still_{i}"] for i in range(1, 4))
        still_4, still_5, still_6 = (media_values[f"still_{i}"] for i in range(4, 7))
        still_7, still_8, still_9 = (media_values[f"still_{i}"] for i in range(7, 10))
        footage_1, footage_2, footage_3 = (media_values[f"footage_{i}"] for i in range(1, 4))
        track_1, track_2, track_3 = (media_values[f"track_{i}"] for i in range(1, 4))
        audio_trim_ranges = _panel_trim_ranges(panel_state)
        first = _read_image_asset(keyframe_open)
        last = _read_image_asset(keyframe_close)
        hybrid = _apply_sound_choice(
            _read_audio_asset(keyframe_audio), audio_trim_ranges.get("keyframe_audio")
        )
        # Hybrid uses the same H3-aligned timeline for the mux track, target
        # audio latent, and reference-audio condition. This prevents a longer
        # source file from giving the reference condition a different clock.
        effective_duration = resolve_frame_length(clip_seconds) / 24.0
        hybrid = _clip_audio_length(hybrid, effective_duration)
        # The dedicated Hybrid upload is the GH equivalent of T8's first
        # autogrow ref_audio input. Keep it as a reference and also provide it
        # as the internal drive track so Hybrid works without an <Audio N> tag.
        refs = {
            f"still_{i}": loaded
            for i, value in enumerate(
                [still_1, still_2, still_3, still_4, still_5,
                 still_6, still_7, still_8, still_9],
                1,
            )
            if (loaded := _read_image_asset(value)) is not None
        }
        ref_videos, video_audio = {}, {}
        muted_video_slots = _panel_muted_slots(panel_state)
        for i, value in enumerate([footage_1, footage_2, footage_3], 1):
            # Sample at the source playback rate through H3's complete aligned
            # timeline (e.g. 124 frames / 5.167s for a displayed 5s).
            frames, soundtrack = _read_video_frames(value, resolve_frame_length(clip_seconds))
            if frames is not None:
                ref_videos[f"footage_{i}"] = frames
                if soundtrack is not None and f"footage_{i}" not in muted_video_slots:
                    # Keep paired audio on the reference video's own retained
                    # timeline. A short reference must not grow to the target
                    # generation duration through audio padding either.
                    reference_duration = snap_frames_down(int(frames.shape[0])) / 24.0
                    video_audio[f"footage_audio_{i}"] = _clip_audio_length(
                        soundtrack, reference_duration
                    )
        audios = _load_reference_audios(
            [track_1, track_2, track_3], audio_trim_ranges, effective_duration
        )
        driving_track, selected_drive_audio = _select_drive_audio(
            panel_mode, audio_source, driving_track, video_audio, audios
        )
        internal_drive_audio = hybrid if panel_mode == "text_keyframes" and hybrid is not None else selected_drive_audio

        visual_source = _pick_visual_source(
            panel_mode,
            _panel_first_visual(panel_state, panel_mode),
            keyframe_open, keyframe_close, first, last, refs, ref_videos,
            [footage_1, footage_2, footage_3],
            [still_1, still_2, still_3, still_4, still_5,
             still_6, still_7, still_8, still_9],
        )
        if visual_source is not None:
            source_height, source_width = int(visual_source.shape[1]), int(visual_source.shape[2])
        else:
            source_width = source_height = None
        width, height = resolve_canvas(frame_ratio, pixel_budget, source_width, source_height)
        length = resolve_frame_length(clip_seconds)
        clip, video_vae, audio_vae = _load_models(text_encoder, video_decoder, audio_decoder)

        # 各模式实际参与条件的素材：关键帧上传会继续留在工作流状态里，
        # 但 all_reference 模式下不喂给 Ref2VA 执行。
        if panel_mode == "text_keyframes":
            refs, ref_videos, video_audio = {}, {}, {}
            audios = {"track_1": hybrid} if hybrid is not None else {}
        elif panel_mode == "all_reference":
            first, last, hybrid = None, None, None
        elif hybrid is not None:
            audios["track_1"] = hybrid

        # Only keyframe Hybrid has an internal drive track. Reference-only
        # mode must always let the model generate audio.
        if internal_drive_audio is None:
            audio_source = "native"
        is_original_audio = audio_source == "lock_source"
        effective_duration = length / 24.0
        final_audio = _clip_audio_length(internal_drive_audio, effective_duration)

        try:
            result = compose_conditioning(
                clip, video_vae, audio_vae, prompt, width, height, length,
                "auto", audio_source, audio_cleanup, True,
                tag_guard, asset_scale, internal_drive_audio, final_audio,
                first, last, refs, ref_videos, video_audio, audios,
            )
        finally:
            _drop_text_encoder(clip)
            clip = None

        # ===== 双时钟采样（原 MiniMaxH3DualClockT8RH 内联，2026-09-24 整合）=====
        model_out, sampler_obj, sigmas = build_dual_clock_setup(
            model, result[1], sample_steps, video_sigma_shift, audio_sigma_shift, sampler_id, scheduler_id)
        # ===== 标准 custom-sampling 三件套内联（RandomNoise/BasicGuider/SamplerCustomAdvanced）=====
        noise = _first_output(nodes_custom_sampler.RandomNoise().get_noise(seed_value))
        guider = _first_output(nodes_custom_sampler.BasicGuider().get_guider(model_out, result[0]))
        sampled = _all_outputs(nodes_custom_sampler.SamplerCustomAdvanced().sample(
            noise, guider, sampler_obj, sigmas, result[1]))
        # ===== AV 解码（原 MiniMaxH3AVDecodeT8RH 内联）=====
        decoded = unpack_av_latent(sampled[0], video_vae, audio_vae)
        # 审查 S4：不再后台线程 50ms 全局 unload_all_models——这是全局操作，
        # 会击中同工作流后续分支/队列中下一任务的模型加载（CUDA 崩溃）。
        # 只做进程级 gc + 软清缓存（不释放权重），显存交由 ComfyUI LRU 管理。
        gc.collect()
        soft_cache = getattr(model_management, "soft_empty_cache", None)
        if callable(soft_cache):
            soft_cache()
        # 音频输出口按音频模式自动选择：锁定原声用源音轨（mux），否则用生成音轨。
        # 与原工作流两种接法（默认接 generated_audio / 锁定原声接 mux_audio）语义一致。
        mux_audio = result[2]
        audio = mux_audio if (is_original_audio and mux_audio is not None) else decoded[1]
        return io.NodeOutput(decoded[0], audio, decoded[2], result[5])


class AllInOneExtension(ComfyExtension):
    async def get_node_list(self):
        # 2026-09-24 整合：注册表 7 → 1（原 PromptGenerator/Integration/Adapter/
        # DualClock/AVDecode 合并为单个 All-in-One 节点；TiledSampler/LatentUpscaler
        # 实现保留在包内暂不注册，需要二段放大时再恢复）。
        return [
            AllInOneNode,
        ]


def comfy_entrypoint():
    return AllInOneExtension()
