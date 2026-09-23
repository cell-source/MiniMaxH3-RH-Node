from __future__ import annotations

import math
import os
import json
import logging
import gc
import shutil
import subprocess
import tempfile
import threading
import time

import folder_paths
import nodes
import torch
import comfy.model_management as model_management
from comfy_api.latest import ComfyExtension, io
from comfy_api.latest._input_impl import VideoFromFile
from comfy_extras import nodes_audio, nodes_custom_sampler

from .conditioning import build_conditioning
from .core import align_frame_count_down
from .audio_ops import decode_av_latent
from .llm_client import make_client
from .prompt_runtime import generate_prompt
from .sampling import (
    DEFAULT_SAMPLER_NAME,
    DEFAULT_SCHEDULER_NAME,
    SAMPLER_OPTIONS,
    SCHEDULER_OPTIONS,
    setup_dual_clock_sampling_gh,
)
from .prompt_tags import apply_advanced_constraints


NODE_CATEGORY = "RH/MiniMax H3 Integration"
MAX_RESOLUTION = 16384
ASPECTS = {
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
DEFAULT_CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
DEFAULT_VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
DEFAULT_AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"


def _first_output(value):
    """Normalize a legacy/V3 node-call result to its first output value.

    Inlined ComfyUI standard nodes expose both V1 (tuple) and V3 (io.NodeOutput)
    call forms; this keeps the all-in-one node compatible with either runtime.
    """
    result = getattr(value, "result", None)
    if result is not None:
        return result[0]
    if isinstance(value, tuple):
        return value[0]
    return value


def _all_outputs(value):
    """Normalize a node-call result to the full tuple of its outputs."""
    result = getattr(value, "result", None)
    if result is not None:
        return tuple(result)
    if isinstance(value, tuple):
        return value
    return (value,)


def _schedule_background_model_cleanup() -> None:
    """Unload ComfyUI models after AV decode has returned its outputs.

    This is intentionally detached from node execution: decoded tensors are
    already materialized, while downstream nodes remain free to continue. A
    tiny grace period lets ComfyUI publish the node outputs before the global
    model cache is released.
    """
    def cleanup() -> None:
        try:
            time.sleep(0.05)
            unload_all = getattr(model_management, "unload_all_models", None)
            if callable(unload_all):
                unload_all()
            empty_cache = getattr(model_management, "soft_empty_cache", None)
            if callable(empty_cache):
                empty_cache(force=True)
            gc.collect()
            logging.info("MiniMax H3 AV decode background model cleanup completed")
        except Exception:
            logging.exception("MiniMax H3 AV decode background model cleanup failed")

    threading.Thread(
        target=cleanup,
        name="minimax-h3-av-decode-cleanup",
        daemon=True,
    ).start()


def _files(content_type: str):
    return sorted(folder_paths.filter_files_content_types(os.listdir(folder_paths.get_input_directory()), [content_type]))


def _image_files():
    return _files("image")


def _video_files():
    return _files("video")


def _audio_files():
    return _files("audio")


def _uploaded_media(name, tooltip=None, hidden=False):
    """Store a custom-uploaded filename without Combo enum validation.

    The DOM panel owns the upload control.  A fixed upload Combo cannot
    represent an unused slot: ComfyUI validates every submitted Combo value,
    so the old ``(none)`` sentinel made the last empty media slots invalid.
    Empty strings are valid optional STRING values and are converted to None
    by the existing file loaders.
    """
    return io.String.Input(
        name,
        default="",
        optional=True,
        tooltip=tooltip,
        extra_dict={"hidden": True} if hidden else None,
    )


def _hidden_combo(name, options, default=None):
    return io.Combo.Input(
        name,
        options=options,
        default=default,
        extra_dict={"hidden": True},
    )


def _hidden_boolean(name, default):
    return io.Boolean.Input(name, default=default, extra_dict={"hidden": True})


def _hidden_float(name, default, minimum, maximum, step, round_value=None):
    return io.Float.Input(
        name,
        default=default,
        min=minimum,
        max=maximum,
        step=step,
        round=round_value,
        extra_dict={"hidden": True},
    )


def _hidden_int(name, default, minimum, maximum, step=1):
    return io.Int.Input(
        name,
        default=default,
        min=minimum,
        max=maximum,
        step=step,
        extra_dict={"hidden": True},
    )


def _coerce_bool(value, default=True):
    """Legacy graphs can replay hidden widgets as "(none)" or raw strings."""
    if value is None or value == "" or value == "(none)":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def _coerce_int(value, default=0, minimum=None, maximum=None):
    """Accept legacy serialized widget values without leaking them downstream."""
    if value is None or value == "" or value == "(none)":
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


def _restore_ui_state(gh_state_json, main_mode, prompt, media_values):
    """Recover DOM-owned values from the workflow's serialized state."""
    if not gh_state_json or gh_state_json == "(none)":
        return main_mode, prompt, media_values
    try:
        state = json.loads(gh_state_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return main_mode, prompt, media_values
    if not isinstance(state, dict):
        return main_mode, prompt, media_values

    restored_mode = state.get("mode")
    if restored_mode in {"text_keyframes", "all_reference"}:
        main_mode = restored_mode

    prompts = state.get("prompts")
    if isinstance(prompts, dict) and isinstance(prompts.get(main_mode), str):
        prompt = prompts[main_mode]
    elif isinstance(state.get("prompt"), str):
        prompt = state["prompt"]

    # When the media list exists it is the source of truth, including removals.
    # This prevents stale hidden widget values from reviving deleted uploads.
    serialized_media = state.get("media")
    if isinstance(serialized_media, list):
        restored_media = {}
        for item in serialized_media:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            slot, entry = item
            if slot not in media_values or not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name and name != "(none)":
                restored_media[slot] = name
        media_values = {name: restored_media.get(name, "") for name in media_values}

    return main_mode, prompt, media_values


def _serialized_first_visual_name(gh_state_json, mode):
    if not gh_state_json or gh_state_json == "(none)":
        return None
    try:
        state = json.loads(gh_state_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    media = dict(state.get("media", [])) if isinstance(state, dict) else {}
    if mode == "text_keyframes":
        ordered_slots = ["first_frame", "last_frame"]
    else:
        # Ref2VA presents videos before pictures, regardless of upload time.
        ordered_slots = [
            *(f"ref_video_{i}" for i in range(1, 4)),
            *(f"ref_image_{i}" for i in range(1, 10)),
        ]
    for slot in ordered_slots:
        entry = media.get(slot)
        if not isinstance(entry, dict) or entry.get("kind") not in {"image", "video"}:
            continue
        name = entry.get("name")
        if isinstance(name, str) and name and name != "(none)":
            return name
    return None


def _serialized_muted_video_slots(gh_state_json):
    if not gh_state_json or gh_state_json == "(none)":
        return set()
    try:
        state = json.loads(gh_state_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    media = state.get("media", []) if isinstance(state, dict) else []
    return {
        slot for slot, entry in media
        if isinstance(slot, str) and slot.startswith("ref_video_")
        and isinstance(entry, dict) and bool(entry.get("muted"))
    }


def _serialized_audio_trim_ranges(gh_state_json):
    """Return saved non-destructive audio selections keyed by media slot."""
    if not gh_state_json or gh_state_json == "(none)":
        return {}
    try:
        state = json.loads(gh_state_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    media = state.get("media", []) if isinstance(state, dict) else []
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


def _default_model(options, preferred):
    return preferred if preferred in options else (options[0] if options else None)


def _release_text_encoder(clip) -> None:
    """Best-effort CLIP-only VRAM release across ComfyUI versions."""
    patcher = getattr(clip, "patcher", None)
    unloaded = False
    try:
        if patcher is not None:
            unload = getattr(model_management, "unload_model_and_clones", None)
            if not callable(unload):
                # Compatibility with ComfyUI builds that used the older name.
                unload = getattr(model_management, "unload_model_clones", None)
            if callable(unload):
                unload(patcher)
                unloaded = True
    except Exception as exc:
        logging.warning("MiniMax H3 could not explicitly unload the text encoder: %s", exc)
    finally:
        if patcher is not None and not unloaded:
            try:
                # Last-resort targeted offload. Never unload every model here:
                # both VAEs are still required by downstream nodes.
                detach = getattr(patcher, "detach", None)
                if callable(detach):
                    detach()
            except Exception as exc:
                logging.warning("MiniMax H3 could not detach the text encoder: %s", exc)
        try:
            empty_cache = getattr(model_management, "soft_empty_cache", None)
            if callable(empty_cache):
                empty_cache()
        except Exception as exc:
            logging.warning("MiniMax H3 could not clear released text-encoder cache: %s", exc)


def _round32(value: float) -> int:
    return max(32, int(value / 32 + 0.5) * 32)


def calculate_canvas(aspect: str, megapixels: float, source_width=None, source_height=None):
    ratio = ASPECTS.get(aspect, ASPECTS["16:9"])
    if ratio is None:
        try:
            ratio = float(source_width) / float(source_height)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = ASPECTS["16:9"]
    area = max(0.05, float(megapixels)) * 1024 * 1024
    # Round both axes independently from the requested aspect ratio. Using
    # area / rounded_width makes even 1:1 drift after the second rounding.
    width = _round32((area * ratio) ** 0.5)
    height = _round32((area / ratio) ** 0.5)
    if ratio == 1.0:
        height = width
    return min(MAX_RESOLUTION, width), min(MAX_RESOLUTION, height)


def calculate_length(duration_seconds: float) -> int:
    requested_frames = max(5, round(float(duration_seconds) * 24))
    return requested_frames + (5 - (requested_frames % 17)) % 17


def _load_image_file(value):
    if not value or value == "(none)":
        return None
    image, _mask = nodes.LoadImage().load_image(value)
    return image


def _load_audio_file(value):
    if not value or value == "(none)":
        return None
    try:
        return nodes_audio.LoadAudio.load(value)[0]
    except Exception as primary_error:
        # ComfyUI's PyAV loader can reject otherwise usable files (notably
        # some FLAC encoders or files containing a damaged metadata/frame
        # block).  Decode through FFmpeg to a plain PCM WAV as a compatibility
        # fallback.  This also broadens support to common formats such as
        # M4A/AAC, OGG/Opus, WMA and AIFF without changing H3's AUDIO contract.
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
                )
                if process.returncode != 0 or not os.path.isfile(wav_path):
                    details = (process.stderr or "").strip()[-1200:]
                    raise RuntimeError(details or f"FFmpeg exited with code {process.returncode}")
                waveform, sample_rate = nodes_audio.load(wav_path)
                logging.info(
                    "MiniMax H3 decoded audio through FFmpeg compatibility fallback: %s",
                    os.path.basename(source_path),
                )
                return {"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate}
        except Exception as fallback_error:
            raise RuntimeError(
                f"无法解码音频 {os.path.basename(str(source_path))}。"
                "节点支持常见音频格式，但该文件可能已损坏、扩展名与实际编码不符，"
                "或当前环境缺少可用的 FFmpeg。"
                f"\nComfyUI/PyAV: {primary_error}"
                f"\nFFmpeg fallback: {fallback_error}"
            ) from fallback_error


def _trim_audio_to_duration(audio, duration_seconds):
    """Return a same-rate AUDIO value exactly matching the requested duration."""
    if audio is None:
        return None
    waveform = audio["waveform"]
    sample_rate = int(audio["sample_rate"])
    sample_count = max(1, round(float(duration_seconds) * sample_rate))
    trimmed = waveform[..., :sample_count]
    if trimmed.shape[-1] < sample_count:
        trimmed = torch.nn.functional.pad(trimmed, (0, sample_count - trimmed.shape[-1]))
    return {"waveform": trimmed, "sample_rate": sample_rate}


def _apply_audio_selection(audio, selection):
    """Slice an AUDIO value by a saved UI range without changing its rate."""
    if audio is None or not selection:
        return audio
    waveform = audio["waveform"]
    sample_rate = int(audio["sample_rate"])
    start_seconds, end_seconds = selection
    start_sample = max(0, min(waveform.shape[-1], round(float(start_seconds) * sample_rate)))
    end_sample = max(start_sample + 1, min(waveform.shape[-1], round(float(end_seconds) * sample_rate)))
    return {"waveform": waveform[..., start_sample:end_sample], "sample_rate": sample_rate}


def _sample_video_frames(frames, source_fps, target_frames):
    """Convert the source to 24fps without changing playback speed.

    The caller supplies H3's maximum target frame count. Longer references are
    clipped to that window, while shorter references keep their own duration
    instead of repeating their final frame to fill the generated-video length.
    """
    if frames is None or frames.shape[0] <= 1:
        return frames
    source_fps = float(source_fps or 0)
    requested = max(1, int(target_frames))
    # Keep the old helper contract for direct callers that pass a legacy FPS
    # value. The node itself passes the calculated target frame count.
    if requested <= 30:
        target_fps = requested
        duration = min(frames.shape[0] / source_fps, 360 / 24) if source_fps > 0 else 0
        target_frames = min(360, max(48, round(duration * target_fps)))
        if source_fps <= 0:
            sampled = frames[:target_frames]
        else:
            source_end = min(frames.shape[0] - 1, max(0, math.ceil(duration * source_fps) - 1))
            indices = torch.linspace(0, source_end, target_frames).round().long()
            return frames[indices]
    else:
        if source_fps > 0:
            source_duration_frames = max(1, round(frames.shape[0] * 24 / source_fps))
            target_frames = min(requested, source_duration_frames)
        else:
            target_frames = min(requested, frames.shape[0])
    if source_fps <= 0:
        source_indices = torch.arange(target_frames, dtype=torch.long)
    else:
        # Preserve source playback rate: 60fps -> 24fps drops frames at the
        # 24fps cadence; 12fps -> 24fps repeats source frames at that cadence.
        source_indices = torch.floor(torch.arange(target_frames, dtype=torch.float32) * source_fps / 24).long()
    source_indices = source_indices.clamp(max=frames.shape[0] - 1)
    return frames[source_indices][:target_frames]


def _load_video_frames(value, target_frames):
    if not value or value == "(none)":
        return None, None
    path = folder_paths.get_annotated_filepath(value)
    components = VideoFromFile(path).get_components()
    frames = components.images
    frames = _sample_video_frames(frames, components.frame_rate, target_frames)
    return frames, components.audio


class MiniMaxH3IntegrationGH(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        image_files = _image_files()
        video_files = _video_files()
        audio_files = _audio_files()
        return io.Schema(
            node_id="MiniMaxH3IntegrationRH",
            category=NODE_CATEGORY,
            display_name="MiniMax H3 All-in-One (RH)",
            description=(
                "All-in-one MiniMax H3 node: optional AI/offline prompt generation, "
                "conditioning build, dual-clock sampling, and AV decode. "
                "The diffusion model remains external."
            ),
            inputs=[
                _hidden_combo("main_mode", ["text_keyframes", "all_reference"], "text_keyframes"),
                io.Combo.Input(
                    "clip_name",
                    options=folder_paths.get_filename_list("text_encoders"),
                    default=_default_model(folder_paths.get_filename_list("text_encoders"), DEFAULT_CLIP),
                ),
                io.Combo.Input(
                    "video_vae_name",
                    options=folder_paths.get_filename_list("vae"),
                    default=_default_model(folder_paths.get_filename_list("vae"), DEFAULT_VIDEO_VAE),
                ),
                io.Combo.Input(
                    "audio_vae_name",
                    options=folder_paths.get_filename_list("vae"),
                    default=_default_model(folder_paths.get_filename_list("vae"), DEFAULT_AUDIO_VAE),
                ),
                io.Combo.Input("aspect", options=list(ASPECTS), default="adaptive"),
                io.Float.Input("megapixels", default=0.5, min=0.2, max=2.0, step=0.1, round=0.1),
                io.Int.Input(
                    "duration_seconds",
                    default=5,
                    min=2,
                    max=30,
                    step=1,
                ),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True, default="", extra_dict={"hidden": True}),
                _uploaded_media("first_frame", tooltip="Optional first frame", hidden=True),
                _uploaded_media("last_frame", tooltip="Optional last frame", hidden=True),
                _uploaded_media("hybrid_audio", tooltip="Optional audio for keyframe Hybrid", hidden=True),
                _uploaded_media("ref_image_1", hidden=True),
                _uploaded_media("ref_image_2", hidden=True),
                _uploaded_media("ref_image_3", hidden=True),
                _uploaded_media("ref_image_4", hidden=True),
                _uploaded_media("ref_image_5", hidden=True),
                _uploaded_media("ref_image_6", hidden=True),
                _uploaded_media("ref_image_7", hidden=True),
                _uploaded_media("ref_image_8", hidden=True),
                _uploaded_media("ref_image_9", hidden=True),
                _uploaded_media("ref_video_1", hidden=True),
                _uploaded_media("ref_video_2", hidden=True),
                _uploaded_media("ref_video_3", hidden=True),
                _uploaded_media("ref_audio_1", hidden=True),
                _uploaded_media("ref_audio_2", hidden=True),
                _uploaded_media("ref_audio_3", hidden=True),
                _hidden_combo("task_type", ["auto", "t2va", "i2va", "l2va", "fl2va", "ref2va", "hybrid"], "auto"),
                _hidden_combo("audio_mode", ["native", "lock_source", "reference_only", "remix_source"], "lock_source"),
                _hidden_float("audio_denoise_strength", 0.0, 0.0, 1.0, 0.01, 0.01),
                # Match T8: 1 selects the first audio; 0 disables remapping.
                _hidden_int("drive_audio_ordinal", 1, 0, 6),
                _hidden_boolean("strict_prompt_tags", True),
                _hidden_combo("ref_image_size", ["match", "1.2x", "1.5x", "2x", "max"], "match"),
                # Keep this last so adding persistence does not shift legacy
                # widgets_values positions for any existing input.
                io.String.Input("gh_state_json", default="", optional=True, extra_dict={"hidden": True}),
                # Keep new inputs after every legacy widget so old workflow
                # widget-value positions remain unchanged.
                io.String.Input(
                    "prompt_override",
                    display_name="Prompt",
                    optional=True,
                    force_input=True,
                    tooltip="Optional upstream prompt; overrides the local prompt editor when connected",
                ),
                # RH advanced prompt constraints. Keep these last so legacy
                # workflow widget-value positions never shift.
                _hidden_boolean("no_subtitle", True),
                _hidden_boolean("soundscape", False),
                _hidden_boolean("music", False),
                # ===== 采样（原 DualClock + 标准 RandomNoise/BasicGuider/SamplerCustomAdvanced 内联，2026-09-24 整合）=====
                io.Model.Input("model"),
                io.Int.Input("steps", default=4, min=1, max=1000),
                io.Float.Input("shift_video", default=12.0, min=0.01, max=100.0, step=0.01, advanced=True),
                io.Float.Input("shift_audio", default=3.0, min=0.01, max=100.0, step=0.01, advanced=True),
                io.Combo.Input("sampler_name", options=SAMPLER_OPTIONS, default=DEFAULT_SAMPLER_NAME, optional=True),
                io.Combo.Input("scheduler", options=SCHEDULER_OPTIONS, default=DEFAULT_SCHEDULER_NAME, optional=True),
                io.Int.Input("noise_seed", default=0, min=0, max=0xffffffffffffffff, control_after_generate=True),
                # ===== 提示词生成（原独立 PromptGenerator 节点内联）=====
                io.Combo.Input(
                    "prompt_source",
                    options=["panel", "ai", "offline", "format"],
                    default="panel",
                    tooltip="panel=使用提示词编辑器/上游内容；ai=AI 在线生成；offline=离线原文包装；format=整理完整 H3 提示词",
                ),
                io.String.Input("ai_text", multiline=True, default="", dynamic_prompts=False),
                io.Combo.Input("ai_language", options=["zh", "mixed", "en"], default="zh"),
                io.Combo.Input("ai_mode", options=["auto", "t2va", "i2va", "fl2va", "l2va", "ref2va"], default="auto"),
                io.Boolean.Input("ai_enrich", default=False),
                io.Boolean.Input("ai_soundscape", default=False),
                io.Boolean.Input("ai_music", default=False),
                io.Boolean.Input("ai_auto_timestamps", default=True),
                io.Boolean.Input("ai_fixed_camera", default=True),
                io.Boolean.Input("ai_visual_stability", default=False),
                io.Boolean.Input("ai_no_subtitles", default=True),
                io.Boolean.Input("ai_anti_pop", default=True),
                io.Boolean.Input("ai_strict_validation", default=True),
                io.Combo.Input("ai_provider", options=["deepseek", "glm", "custom"], default="deepseek"),
                io.String.Input("ai_api_key", default="", optional=True, tooltip="优先使用服务端环境变量。此字段会保存到工作流，分享前请清空。"),
                io.String.Input("ai_endpoint", default="", optional=True),
                io.String.Input("ai_model", default="", optional=True),
                io.Int.Input("ai_timeout", default=180, min=15, max=600, optional=True),
            ],
            outputs=[
                io.Image.Output("frames"),
                io.Audio.Output("audio"),
                io.Latent.Output("video_latent"),
                io.String.Output("report"),
            ],
        )

    @classmethod
    def execute(cls, main_mode, aspect, megapixels, duration_seconds, clip_name, video_vae_name, audio_vae_name,
                prompt, first_frame, last_frame, hybrid_audio, ref_image_1, ref_image_2, ref_image_3,
                ref_image_4, ref_image_5, ref_image_6, ref_image_7, ref_image_8, ref_image_9,
                ref_video_1, ref_video_2, ref_video_3, ref_audio_1, ref_audio_2, ref_audio_3,
                task_type, audio_mode, audio_denoise_strength,
                drive_audio_ordinal, strict_prompt_tags, ref_image_size,
                model=None, steps=4, shift_video=12.0, shift_audio=3.0,
                sampler_name=DEFAULT_SAMPLER_NAME, scheduler=DEFAULT_SCHEDULER_NAME, noise_seed=0,
                prompt_source="panel", ai_text="", ai_language="zh", ai_mode="auto",
                ai_enrich=False, ai_soundscape=False, ai_music=False, ai_auto_timestamps=True,
                ai_fixed_camera=True, ai_visual_stability=False, ai_no_subtitles=True,
                ai_anti_pop=True, ai_strict_validation=True, ai_provider="deepseek",
                ai_api_key="", ai_endpoint="", ai_model="", ai_timeout=180,
                gh_state_json="", prompt_override=None,
                no_subtitle=True, soundscape=False, music=False):
        # ComfyUI can replay an older hidden-widget snapshot as the literal
        # string "(none)". T8 treats this control as an int, with 0 disabling
        # remapping, so normalize it before calling the shared conditioning.
        drive_audio_ordinal = _coerce_int(drive_audio_ordinal, default=1, minimum=0, maximum=6)
        media_values = {
            "first_frame": first_frame, "last_frame": last_frame, "hybrid_audio": hybrid_audio,
            "ref_image_1": ref_image_1, "ref_image_2": ref_image_2, "ref_image_3": ref_image_3,
            "ref_image_4": ref_image_4, "ref_image_5": ref_image_5, "ref_image_6": ref_image_6,
            "ref_image_7": ref_image_7, "ref_image_8": ref_image_8, "ref_image_9": ref_image_9,
            "ref_video_1": ref_video_1, "ref_video_2": ref_video_2, "ref_video_3": ref_video_3,
            "ref_audio_1": ref_audio_1, "ref_audio_2": ref_audio_2, "ref_audio_3": ref_audio_3,
        }
        main_mode, prompt, media_values = _restore_ui_state(
            gh_state_json, main_mode, prompt, media_values
        )
        # 提示词解析（2026-09-24 整合原 PromptGenerator 节点）：
        # panel → 提示词编辑器/上游内容（保留原有约束开关与 prompt_override 语义）；
        # ai/offline/format → 走提示词引擎生成后再进入条件构建。
        if prompt_source != "panel":
            interrupt = model_management.throw_exception_if_processing_interrupted
            interrupt()
            if duration_seconds > 15:
                raise ValueError("AI/离线/整理提示词生成仅支持 2–15 秒；更长时长请直接在提示词编辑器中撰写")
            gen_mode = ai_mode if ai_mode != "auto" else ("ref2va" if main_mode == "all_reference" else "t2va")
            client = (make_client(ai_provider, ai_api_key, ai_endpoint, ai_model, ai_timeout, interrupt)
                      if prompt_source == "ai" else None)
            controls = {"mode": gen_mode, "lang": ai_language, "duration": str(duration_seconds),
                        "ratio": aspect if aspect in {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"} else "16:9",
                        "model": ai_provider, "enrich_do_enrich": ai_enrich,
                        "enrich_soundscape": ai_soundscape, "enrich_music": ai_music,
                        "auto_timestamps": ai_auto_timestamps, "fixed_camera": ai_fixed_camera,
                        "visual_stability": ai_visual_stability, "no_subtitles": ai_no_subtitles,
                        "anti_pop": ai_anti_pop}
            result = generate_prompt(ai_text, prompt_source, controls, client, interrupt)
            if ai_strict_validation and not result["valid"]:
                raise ValueError("H3 提示词校验失败：" + result["report"] + "；可关闭 ai_strict_validation 输出文本供修改")
            prompt = result["prompt"]
            # 提示词模式决定素材通路：ref2va 走全参考，其余走首尾帧
            main_mode = "all_reference" if gen_mode == "ref2va" else "text_keyframes"
        elif prompt_override is not None:
            prompt = str(prompt_override)
        else:
            # A connected generator owns its language and sound/visual options.
            # Apply editor options only to prompts authored in this node.
            prompt = apply_advanced_constraints(
                prompt,
                no_subtitle=_coerce_bool(no_subtitle, True),
                soundscape=_coerce_bool(soundscape, False),
                music=_coerce_bool(music, False),
            )
        first_frame = media_values["first_frame"]
        last_frame = media_values["last_frame"]
        hybrid_audio = media_values["hybrid_audio"]
        ref_image_1, ref_image_2, ref_image_3 = (media_values[f"ref_image_{i}"] for i in range(1, 4))
        ref_image_4, ref_image_5, ref_image_6 = (media_values[f"ref_image_{i}"] for i in range(4, 7))
        ref_image_7, ref_image_8, ref_image_9 = (media_values[f"ref_image_{i}"] for i in range(7, 10))
        ref_video_1, ref_video_2, ref_video_3 = (media_values[f"ref_video_{i}"] for i in range(1, 4))
        ref_audio_1, ref_audio_2, ref_audio_3 = (media_values[f"ref_audio_{i}"] for i in range(1, 4))
        audio_trim_ranges = _serialized_audio_trim_ranges(gh_state_json)
        first = _load_image_file(first_frame)
        last = _load_image_file(last_frame)
        hybrid = _apply_audio_selection(
            _load_audio_file(hybrid_audio), audio_trim_ranges.get("hybrid_audio")
        )
        # Hybrid uses the same H3-aligned timeline for the mux track, target
        # audio latent, and reference-audio condition. This prevents a longer
        # source file from giving the reference condition a different clock.
        effective_duration = calculate_length(duration_seconds) / 24.0
        hybrid = _trim_audio_to_duration(hybrid, effective_duration)
        # The dedicated Hybrid upload is the GH equivalent of T8's first
        # autogrow ref_audio input. Keep it as a reference and also provide it
        # as the internal drive track so Hybrid works without an <Audio N> tag.
        refs = {
            f"ref_image_{i}": loaded
            for i, value in enumerate(
                [ref_image_1, ref_image_2, ref_image_3, ref_image_4, ref_image_5,
                 ref_image_6, ref_image_7, ref_image_8, ref_image_9],
                1,
            )
            if (loaded := _load_image_file(value)) is not None
        }
        ref_videos, video_audio = {}, {}
        muted_video_slots = _serialized_muted_video_slots(gh_state_json)
        for i, value in enumerate([ref_video_1, ref_video_2, ref_video_3], 1):
            # Sample at the source playback rate through H3's complete aligned
            # timeline (e.g. 124 frames / 5.167s for a displayed 5s).
            frames, soundtrack = _load_video_frames(value, calculate_length(duration_seconds))
            if frames is not None:
                ref_videos[f"ref_video_{i}"] = frames
                if soundtrack is not None and f"ref_video_{i}" not in muted_video_slots:
                    # Keep paired audio on the reference video's own retained
                    # timeline. A short reference must not grow to the target
                    # generation duration through audio padding either.
                    reference_duration = align_frame_count_down(int(frames.shape[0])) / 24.0
                    video_audio[f"ref_video_audio_{i}"] = _trim_audio_to_duration(
                        soundtrack, reference_duration
                    )
        audios = [
            _trim_audio_to_duration(
                _apply_audio_selection(
                    _load_audio_file(value), audio_trim_ranges.get(f"ref_audio_{index}")
                ),
                effective_duration,
            )
            for index, value in enumerate([ref_audio_1, ref_audio_2, ref_audio_3], 1)
        ]
        audios = {f"ref_audio_{i}": value for i, value in enumerate(audios, 1) if value is not None}
        ordered_drive_audios = [
            video_audio[f"ref_video_audio_{i}"]
            for i in range(1, 4)
            if f"ref_video_audio_{i}" in video_audio
        ] + list(audios.values())
        if (
            main_mode == "all_reference"
            and audio_mode == "lock_source"
            and drive_audio_ordinal == 0
            and ordered_drive_audios
        ):
            drive_audio_ordinal = 1
        selected_drive_audio = (
            ordered_drive_audios[drive_audio_ordinal - 1]
            if main_mode == "all_reference" and 0 < drive_audio_ordinal <= len(ordered_drive_audios)
            else None
        )
        internal_drive_audio = hybrid if main_mode == "text_keyframes" and hybrid is not None else selected_drive_audio

        visual_source = None
        serialized_first_visual = _serialized_first_visual_name(gh_state_json, main_mode)
        if main_mode == "text_keyframes":
            if serialized_first_visual and first_frame == serialized_first_visual:
                visual_source = first
            elif serialized_first_visual and last_frame == serialized_first_visual:
                visual_source = last
            else:
                visual_source = first if first is not None else last
        else:
            if serialized_first_visual:
                for slot, filename in ((f"ref_video_{i}", value) for i, value in enumerate(
                    [ref_video_1, ref_video_2, ref_video_3], 1
                )):
                    if filename == serialized_first_visual:
                        visual_source = ref_videos.get(slot)
                        break
                if visual_source is None:
                    for slot, filename in ((f"ref_image_{i}", value) for i, value in enumerate(
                        [ref_image_1, ref_image_2, ref_image_3, ref_image_4, ref_image_5, ref_image_6, ref_image_7, ref_image_8, ref_image_9], 1
                    )):
                        if filename == serialized_first_visual:
                            visual_source = refs.get(slot)
                            break
            ordered_visual_values = [
                ref_videos.get(f"ref_video_{i}") for i in range(1, 4)
            ] + [
                refs.get(f"ref_image_{i}") for i in range(1, 10)
            ]
            for value in (() if visual_source is not None else ordered_visual_values):
                if value is not None:
                    visual_source = value
                    break
        if visual_source is not None:
            source_height, source_width = int(visual_source.shape[1]), int(visual_source.shape[2])
        else:
            source_width = source_height = None
        width, height = calculate_canvas(aspect, megapixels, source_width, source_height)
        length = calculate_length(duration_seconds)
        clip = nodes.CLIPLoader().load_clip(clip_name, "minimax")[0]
        video_vae = nodes.VAELoader().load_vae(video_vae_name)[0]
        audio_vae = nodes.VAELoader().load_vae(audio_vae_name)[0]

        if main_mode == "text_keyframes":
            refs, ref_videos, video_audio = {}, {}, {}
            audios = {"ref_audio_1": hybrid} if hybrid is not None else {}
        elif main_mode == "all_reference":
            # Keep keyframe uploads in the workflow state so switching back
            # does not lose them, but do not feed them into Ref2VA execution.
            first, last, hybrid = None, None, None
        elif hybrid is not None:
            audios["ref_audio_1"] = hybrid

        # Only keyframe Hybrid has an internal drive track. Reference-only
        # mode must always let the model generate audio.
        if internal_drive_audio is None:
            audio_mode = "native"
        is_original_audio = audio_mode == "lock_source"
        effective_duration = length / 24.0
        final_audio = _trim_audio_to_duration(internal_drive_audio, effective_duration)

        try:
            result = build_conditioning(
                clip, video_vae, audio_vae, prompt, width, height, length,
                "auto", audio_mode, audio_denoise_strength, True,
                strict_prompt_tags, ref_image_size, internal_drive_audio, final_audio,
                first, last, refs, ref_videos, video_audio, audios,
            )
        finally:
            _release_text_encoder(clip)
            clip = None

        # ===== 双时钟采样（原 MiniMaxH3DualClockT8RH 内联，2026-09-24 整合）=====
        model_out, sampler_obj, sigmas = setup_dual_clock_sampling_gh(
            model, result[1], steps, shift_video, shift_audio, sampler_name, scheduler)
        # ===== 标准 custom-sampling 三件套内联（RandomNoise/BasicGuider/SamplerCustomAdvanced）=====
        noise = _first_output(nodes_custom_sampler.RandomNoise().get_noise(noise_seed))
        guider = _first_output(nodes_custom_sampler.BasicGuider().get_guider(model_out, result[0]))
        sampled = _all_outputs(nodes_custom_sampler.SamplerCustomAdvanced().sample(
            noise, guider, sampler_obj, sigmas, result[1]))
        # ===== AV 解码（原 MiniMaxH3AVDecodeT8RH 内联）=====
        decoded = decode_av_latent(sampled[0], video_vae, audio_vae)
        _schedule_background_model_cleanup()
        # 音频输出口按音频模式自动选择：锁定原声用源音轨（mux），否则用生成音轨。
        # 与原工作流两种接法（默认接 generated_audio / 锁定原声接 mux_audio）语义一致。
        mux_audio = result[2]
        audio = mux_audio if (is_original_audio and mux_audio is not None) else decoded[1]
        return io.NodeOutput(decoded[0], audio, decoded[2], result[5])


class MiniMaxH3IntegrationExtension(ComfyExtension):
    async def get_node_list(self):
        # 2026-09-24 整合：注册表 7 → 1（原 PromptGenerator/Integration/Adapter/
        # DualClock/AVDecode 合并为单个 All-in-One 节点；TiledSampler/LatentUpscaler
        # 实现保留在包内暂不注册，需要二段放大时再恢复）。
        return [
            MiniMaxH3IntegrationGH,
        ]


def comfy_entrypoint():
    return MiniMaxH3IntegrationExtension()
