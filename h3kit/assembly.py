"""把提示词与素材装配成 MiniMax H3 的条件张量。

职责：
* 任务类型判定（t2va / i2va / l2va / fl2va / ref2va / hybrid）；
* 首尾帧与参考素材的缩放、对齐与编码；
* 语义探测 ComfyUI ``MiniMaxH3.extra_conds`` 的打包策略（Hybrid 兼容）；
* 提示词标签的序号校验与稠密化，以及 AV 潜空间的最终装配。
"""

from __future__ import annotations

import ast
import inspect
import math
from pathlib import Path

import torch

import node_helpers
from comfy.ldm.minimax.model import PackedLayout
from comfy.model_base import MiniMaxH3 as MiniMaxH3BaseModel

from .geometry import (
    SIZE_STEP,
    VIDEO_FPS,
    av_streams,
    blank_av_latent,
    encode_waveform,
    match_audio_latent,
    ordered_dynamic_items,
    ordered_dynamic_values,
    scale_image,
    snap_frames_down,
    swap_audio_latent,
)
from .tagging import build_prompt_body, densify_tag_ordinals, tag_map_json


# 参考素材的像素预算：图片与视频各一档上限；视频另设下限，避免缩得过小。
REF_IMAGE_PIXEL_CAP = 4 * 1024 * 1024
REF_VIDEO_PIXEL_CAP = 1_600_000
REF_VIDEO_PIXEL_MIN = 300_000

# extra_conds 填充 cond_video_latents 的两种受支持语义
REF_MODE_REPLACE = "refs_overwrite"     # 参考素材列表覆盖关键帧
REF_MODE_APPEND = "concat"              # 关键帧与参考素材拼接
_POLICY_KEYFRAMES_ONLY = "keyframes_only"
_POLICY_UNKNOWN = "unknown"

# Hybrid 载荷标记：让 PackedLayout 忽略这条“并非参考素材”的条目
HYBRID_MARKER = "h3kit-hybrid-marker"

_REF_SCALE_TABLE = {"match": 1.0, "1.2x": 1.2, "1.5x": 1.5, "2x": 2.0}
_KEYFRAME_ITER_NAMES = frozenset({"keyframes", "kf", "keyframe"})
_REF_LIST_NAMES = frozenset({"refs", "ref_latents"})
_AUDIO_MODES = frozenset({"native", "reference_only", "lock_source", "remix_source"})
_LATENT_SPATIAL = 16
_MIN_REFERENCE_FRAMES = 5
_REF_LIMITS = {"images": 9, "videos": 3, "audios": 3}


def _literal_text(node) -> str | None:
    """取出字符串字面量的值；其他节点（含旧版 ast.Index 包装）返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Index):
        return _literal_text(node.value)
    return None


def _payload_targets_cond_video(target) -> bool:
    """判断赋值目标是否形如 ``payload["cond_video_latents"]``。"""
    if not isinstance(target, ast.Subscript):
        return False
    owner = target.value
    return (
        isinstance(owner, ast.Name)
        and owner.id == "payload"
        and _literal_text(target.slice) == "cond_video_latents"
    )


def _comprehension_target(node):
    """列表推导式的迭代变量名；不是简单列表推导式时返回 None。"""
    if not isinstance(node, ast.ListComp) or not node.generators:
        return None
    iterator = node.generators[0].iter
    return iterator.id if isinstance(iterator, ast.Name) else None


def load_extra_conds_source(path) -> str:
    """从 ComfyUI 源码文件中抽出 ``MiniMaxH3.extra_conds`` 的源码片段。"""
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "MiniMaxH3":
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "extra_conds":
                segment = ast.get_source_segment(text, item)
                if segment:
                    return segment
    raise RuntimeError(f"在 {path} 中找不到 MiniMaxH3.extra_conds")


def probe_video_latents_policy(source: str) -> str:
    """判定首尾帧与参考素材同时存在时，MiniMaxH3.extra_conds 如何填充 cond_video_latents。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _POLICY_UNKNOWN

    writes = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(_payload_targets_cond_video(target) for target in node.targets)
        ),
        key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)),
    )

    policy = _POLICY_UNKNOWN
    for node in writes:
        value = node.value
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            policy = REF_MODE_APPEND
            continue
        iter_name = _comprehension_target(value)
        if iter_name == "refs":
            policy = REF_MODE_REPLACE
        elif iter_name in _KEYFRAME_ITER_NAMES:
            policy = _POLICY_KEYFRAMES_ONLY
        elif isinstance(value, ast.Name) and value.id in _REF_LIST_NAMES:
            policy = REF_MODE_REPLACE
    return policy


def _current_extra_conds_source() -> str | None:
    """当前已加载模型类上的 extra_conds 源码。"""
    try:
        return inspect.getsource(inspect.unwrap(MiniMaxH3BaseModel.extra_conds))
    except (OSError, TypeError):
        return None


def _bound_extra_conds_source() -> str | None:
    """磁盘上 comfy/model_base.py 里 extra_conds 的源码（应对运行期已打补丁的情况）。"""
    try:
        import comfy.model_base as model_base
    except Exception:
        return None
    try:
        return load_extra_conds_source(inspect.getfile(model_base))
    except Exception:
        return None


def _assert_marker_ignored() -> None:
    """确认 PackedLayout 会忽略 Hybrid 标记条目（否则版本已变，必须禁用该路径）。"""
    keyframe = {"resolved_frame_index": 0, "latent": torch.zeros(1)}
    baseline = PackedLayout(1, 2, 2, 2, 1, keyframes=[keyframe], frame_count=5)
    hybrid = PackedLayout(
        1,
        2,
        2,
        2,
        1,
        keyframes=[keyframe],
        refs=[{"kind": HYBRID_MARKER, "latent": torch.zeros(1)}],
        frame_count=5,
    )
    if baseline.segments != hybrid.segments or baseline.seq_len != hybrid.seq_len:
        raise RuntimeError(
            "当前 ComfyUI 版本改变了 MiniMax H3 的 PackedLayout 参考处理，"
            "为避免条件张量错乱，精确首尾帧 + 参考素材的兼容路径已停用。"
        )


def verify_hybrid_layout() -> str:
    """读取当前生效的 Hybrid 打包策略；无法安全使用时直接报错。

    在本包支持的 ComfyUI 版本上，两类载荷同时存在时，首尾帧条件 latent 会被
    参考 latent 列表覆盖。此时用载荷标记把首尾帧张量补回列表中，而 PackedLayout
    会忽略它不认识的参考类型。

    这里做的是语义检查（解析 extra_conds 的 AST），不是脆弱的源码子串匹配，
    因此 ComfyUI 侧调整格式或注释都不会误判为“不支持 Hybrid”。
    """
    policy = _POLICY_UNKNOWN
    for source in (_current_extra_conds_source(), _bound_extra_conds_source()):
        if not source:
            continue
        policy = probe_video_latents_policy(source)
        if policy != _POLICY_UNKNOWN:
            break

    if policy == REF_MODE_REPLACE:
        _assert_marker_ignored()
        return policy
    if policy == REF_MODE_APPEND:
        return policy
    raise RuntimeError(
        "当前 ComfyUI 版本改变了 MiniMax H3 参考/首尾帧 latent 的装配顺序，"
        "T8 Hybrid 路径在重新校验前已停用。"
    )


# (匹配条件, 任务名)，顺序即优先级
_AUTO_TASK_RULES = (
    (lambda first, last, refs: refs and (first or last), "hybrid"),
    (lambda first, last, refs: refs, "ref2va"),
    (lambda first, last, refs: first and last, "fl2va"),
    (lambda first, last, refs: first, "i2va"),
    (lambda first, last, refs: last, "l2va"),
)
_AUTO_TASK_FALLBACK = "t2va"

# 显式任务所需的连接：(需要首帧, 需要尾帧, 需要参考素材)；None 表示不作要求
_TASK_REQUIREMENTS = {
    "t2va": (False, False, False),
    "i2va": (True, False, False),
    "fl2va": (True, True, False),
    "l2va": (False, True, False),
    "ref2va": (False, False, True),
    "hybrid": (None, None, True),
}
_REF_MEDIA_TASKS = frozenset({"ref2va", "hybrid"})


def pick_task_type(task_hint: str, keyframe_open, keyframe_close, has_refs: bool) -> str:
    """解析最终任务类型；显式指定时会校验素材连接是否匹配。"""
    first = keyframe_open is not None
    last = keyframe_close is not None
    requested = (task_hint or "auto").lower()

    if requested == "auto":
        for accepts, task in _AUTO_TASK_RULES:
            if accepts(first, last, has_refs):
                return task
        return _AUTO_TASK_FALLBACK

    if requested not in _TASK_REQUIREMENTS:
        raise ValueError(f"未知的 MiniMax H3 任务类型：{task_hint}")
    need_first, need_last, need_refs = _TASK_REQUIREMENTS[requested]
    if need_first is not None and first != need_first:
        raise ValueError(f"{requested.upper()} 的首帧连接与所选任务不匹配")
    if need_last is not None and last != need_last:
        raise ValueError(f"{requested.upper()} 的尾帧连接与所选任务不匹配")
    if need_refs and not has_refs:
        raise ValueError(f"{requested.upper()} 至少需要一个参考素材输入")
    if requested == "hybrid" and not (first or last):
        raise ValueError("HYBRID 需要首帧和/或尾帧")
    if requested not in _REF_MEDIA_TASKS and has_refs:
        raise ValueError(f"{requested.upper()} 不接受参考素材，请改用 Auto 或 Hybrid")
    return requested


def _ref_pixel_cap(width: int, height: int, mode: str, *, video: bool) -> int:
    """参考素材的目标像素数（受对应上限约束，视频另设下限）。"""
    ceiling = REF_VIDEO_PIXEL_CAP if video else REF_IMAGE_PIXEL_CAP
    if mode == "max":
        return ceiling
    desired = width * height * _REF_SCALE_TABLE.get(mode, 1.0)
    if video:
        desired = max(REF_VIDEO_PIXEL_MIN, desired)
    return min(ceiling, round(desired))


def _fit_reference_media(media, desired_pixels: int):
    """把参考素材缩到预算内并裁齐到 32 网格；只缩不放。"""
    height, width = int(media.shape[1]), int(media.shape[2])
    scale = min(1.0, math.sqrt(desired_pixels / (width * height)))
    scaled_width = max(SIZE_STEP, round(width * scale))
    scaled_height = max(SIZE_STEP, round(height * scale))
    target_width = max(SIZE_STEP, math.floor(scaled_width / SIZE_STEP) * SIZE_STEP)
    target_height = max(SIZE_STEP, math.floor(scaled_height / SIZE_STEP) * SIZE_STEP)
    resized = scale_image(media, scaled_width, scaled_height)
    left = max(0, (scaled_width - target_width) // 2)
    top = max(0, (scaled_height - target_height) // 2)
    return resized[:, top:top + target_height, left:left + target_width], target_width, target_height


def _fit_reference_image(image, width: int, height: int, asset_scale: str):
    """按图片预算缩放一张参考图（只用第一帧）。"""
    budget = _ref_pixel_cap(width, height, asset_scale, video=False)
    return _fit_reference_media(image[:1], budget)


def _fit_reference_video(frames, width: int, height: int, asset_scale: str):
    """按视频预算缩放参考视频帧序列。"""
    budget = _ref_pixel_cap(width, height, asset_scale, video=True)
    return _fit_reference_media(frames, budget)


def _encode_reference_track(audio_vae, audio: dict):
    """编码一条参考音轨，并返回其 latent 时间长度。"""
    latent = encode_waveform(audio_vae, audio)
    return latent, int(latent.shape[-1])


def _encode_keyframes(video_vae, keyframe_open, keyframe_close, width: int, height: int, frame_count: int):
    """编码精确首/尾帧，返回（关键帧描述, 送入 CLIP 的图像, 图片标签）。"""
    keyframes: list[dict] = []
    images: list = []
    labels: list[str] = []
    if keyframe_open is not None:
        image = scale_image(keyframe_open[:1], width, height, "center")
        images.append(image)
        labels.append("keyframe_open (exact frame 0)")
        keyframes.append({
            "resolved_frame_index": 0,
            "latent": video_vae.encode(image),
            # 保留编码前的图像与 VAE 引用：二段放大时需要按新的目标分辨率
            # 重新编码，而不是对已经降采样过的 latent 做插值。
            "source_image": keyframe_open[:1].detach().cpu(),
            "video_vae": video_vae,
        })
    if keyframe_close is not None:
        image = scale_image(keyframe_close[:1], width, height, "center")
        images.append(image)
        labels.append(f"keyframe_close (exact frame {frame_count - 1})")
        keyframes.append({
            "resolved_frame_index": frame_count - 1,
            "latent": video_vae.encode(image),
            "source_image": keyframe_close[:1].detach().cpu(),
            "video_vae": video_vae,
        })
    return keyframes, images, labels


def _encode_reference_images(video_vae, entries, width: int, height: int, asset_scale: str):
    """编码参考图片，返回（CLIP 参考项, 打包块, 图片标签）。"""
    items: list[dict] = []
    blocks: list[dict] = []
    labels: list[str] = []
    for ordinal, image in entries:
        resized, ref_width, ref_height = _fit_reference_image(image, width, height, asset_scale)
        items.append({"type": "image", "data": resized})
        blocks.append({
            "kind": "image",
            "latent_h": ref_height // _LATENT_SPATIAL,
            "latent_w": ref_width // _LATENT_SPATIAL,
            "latent": video_vae.encode(resized),
        })
        labels.append(f"still_{ordinal}")
    return items, blocks, labels


def _encode_reference_videos(video_vae, audio_vae, entries, soundtracks, width: int, height: int,
                             asset_scale: str, frame_count: int):
    """编码参考视频（含同名音轨），返回（CLIP 参考项, 打包块, 视频标签, 音频标签）。"""
    items: list[dict] = []
    blocks: list[dict] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    for index, (ordinal, frames) in enumerate(entries, 1):
        if frames.ndim != 4 or frames.shape[0] < _MIN_REFERENCE_FRAMES:
            raise ValueError(f"footage_{index} 至少需要 {_MIN_REFERENCE_FRAMES} 帧图像")
        frames, canvas_width, canvas_height = _fit_reference_video(frames, width, height, asset_scale)
        # 对齐官方 H3 语义：参考素材超出生成窗口时截断，
        # 但不靠重复末帧把过短的素材补长。
        frames = frames[:frame_count]
        aligned_count = snap_frames_down(int(frames.shape[0]))
        if aligned_count < _MIN_REFERENCE_FRAMES:
            raise ValueError(f"footage_{index} 按 17n+5 对齐后过短")
        frames = frames[:aligned_count]
        encoded_video = video_vae.encode(frames)

        soundtrack = soundtracks.get(ordinal)
        encoded_soundtrack, soundtrack_t = None, 0
        if soundtrack is not None:
            encoded_soundtrack, soundtrack_t = _encode_reference_track(audio_vae, soundtrack)
            items.append({"type": "audio"})
            audio_labels.append(f"footage_audio_{ordinal}")

        sample_indices = list(range(0, frames.shape[0], VIDEO_FPS // 2))
        items.append({
            "type": "video",
            "data": frames[sample_indices],
            "timestamps": [sample_index / VIDEO_FPS for sample_index in sample_indices],
        })
        blocks.append({
            "kind": "video_audio" if soundtrack_t else "video",
            "latent_t": int(encoded_video.shape[2]),
            "latent_h": canvas_height // _LATENT_SPATIAL,
            "latent_w": canvas_width // _LATENT_SPATIAL,
            "track_t": soundtrack_t,
            "latent": encoded_video,
            "audio_latent": encoded_soundtrack,
        })
        video_labels.append(f"footage_{ordinal}")
    return items, blocks, video_labels, audio_labels


def _encode_standalone_audios(audio_vae, values):
    """编码独立参考音轨，返回（CLIP 参考项, 打包块, 音频标签）。"""
    items: list[dict] = []
    blocks: list[dict] = []
    labels: list[str] = []
    for index, audio in enumerate(values, 1):
        encoded_audio, audio_t = _encode_reference_track(audio_vae, audio)
        items.append({"type": "audio"})
        blocks.append({"kind": "audio", "track_t": audio_t, "audio_latent": encoded_audio})
        labels.append(f"track_{index}")
    return items, blocks, labels


def _tag_ordinals(keyframe_images, still_entries, footage_entries, audio_labels):
    """计算提示词校验用的有效序号，以及送给引擎的稠密化映射。"""
    picture = {
        *range(1, len(keyframe_images) + 1),
        *(ordinal + len(keyframe_images) for ordinal, _ in still_entries),
    }
    video = {ordinal for ordinal, _ in footage_entries}
    if not keyframe_images:
        picture = {ordinal for ordinal, _ in still_entries}
    if not footage_entries:
        video = set()
    audio = set(range(1, len(audio_labels) + 1))
    valid = {"picture": picture, "video": video, "audio": audio}
    packed = {
        name: {ordinal: slot for slot, ordinal in enumerate(sorted(values), 1)}
        for name, values in valid.items()
    }
    return valid, packed


def _apply_audio_mode(latent: dict, mode: str, encoded_source, denoise_strength: float) -> dict:
    """锁定/重混源音轨时改写潜空间的音频部分；其余模式原样返回。"""
    if mode == "lock_source":
        return swap_audio_latent(latent, encoded_source, 0.0)
    if mode == "remix_source":
        return swap_audio_latent(latent, encoded_source, denoise_strength)
    return latent


def _build_media_map(keyframe_images, picture_labels, still_entries, footage_entries, audio_labels):
    """生成面板用的媒体映射 JSON。"""
    picture_map = {
        **{index: label for index, label in enumerate(picture_labels[: len(keyframe_images)], 1)},
        **{
            ordinal + len(keyframe_images): f"still_{ordinal}"
            for ordinal, _ in still_entries
        },
    }
    video_map = {ordinal: f"footage_{ordinal}" for ordinal, _ in footage_entries}
    return tag_map_json(picture_map, video_map, audio_labels)


def compose_conditioning(
    clip,
    video_vae,
    audio_vae,
    prompt: str,
    width: int,
    height: int,
    length: int,
    task_hint: str = "auto",
    audio_source: str = "lock_source",
    audio_cleanup: float = 0.0,
    add_source_as_reference: bool = True,
    tag_guard: bool = True,
    asset_scale: str = "match",
    drive_audio=None,
    final_audio=None,
    keyframe_open=None,
    keyframe_close=None,
    ref_images=None,
    ref_videos=None,
    footage_audios=None,
    ref_audios=None,
):
    if width % 32 or height % 32:
        raise ValueError("MiniMax H3 的画布宽高必须是 32 的整数倍")
    if not 0.0 <= audio_cleanup <= 1.0:
        raise ValueError("audio_cleanup 必须落在 0 与 1 之间")

    still_entries = ordered_dynamic_items(ref_images)
    footage_entries = ordered_dynamic_items(ref_videos)
    track_values = ordered_dynamic_values(ref_audios)
    footage_audio_by_ordinal = dict(ordered_dynamic_items(footage_audios))

    if (len(still_entries) > _REF_LIMITS["images"]
            or len(footage_entries) > _REF_LIMITS["videos"]
            or len(track_values) > _REF_LIMITS["audios"]):
        raise ValueError("MiniMax H3 的参考上限为 9 张图片、3 段视频、3 条独立音频")

    footage_ordinals = {ordinal for ordinal, _ in footage_entries}
    orphan_soundtracks = sorted(set(footage_audio_by_ordinal) - footage_ordinals)
    if orphan_soundtracks:
        raise ValueError(
            "以下参考视频音轨找不到同号视频：" + "、".join(map(str, orphan_soundtracks))
        )

    mode = audio_source.lower()
    if mode not in _AUDIO_MODES:
        raise ValueError(f"未知的音频模式：{audio_source}")
    if mode != "native" and drive_audio is None:
        raise ValueError(f"音频模式 {audio_source} 需要连接 drive_audio")
    if drive_audio is None:
        add_source_as_reference = False

    latent, frame_count = blank_av_latent(width, height, length)
    latent["gh_h3_audio_mode"] = mode
    _, template_audio = av_streams(latent)

    keyframes, keyframe_images, picture_labels = _encode_keyframes(
        video_vae, keyframe_open, keyframe_close, width, height, frame_count
    )

    ref_items, ref_blocks, image_labels = _encode_reference_images(
        video_vae, still_entries, width, height, asset_scale
    )
    picture_labels.extend(image_labels)

    video_items, video_blocks, video_labels, audio_labels = _encode_reference_videos(
        video_vae, audio_vae, footage_entries, footage_audio_by_ordinal,
        width, height, asset_scale, frame_count,
    )
    ref_items.extend(video_items)
    ref_blocks.extend(video_blocks)

    encoded_source = None
    if drive_audio is not None:
        encoded_source = match_audio_latent(encode_waveform(audio_vae, drive_audio), template_audio)
        already_referenced = any(
            drive_audio is candidate
            for candidate in list(footage_audio_by_ordinal.values()) + track_values
        )
        if add_source_as_reference and not already_referenced:
            ref_items.append({"type": "audio"})
            ref_blocks.append({
                "kind": "audio",
                "track_t": int(encoded_source.shape[-1]),
                "audio_latent": encoded_source,
            })

    audio_items, audio_blocks, standalone_labels = _encode_standalone_audios(audio_vae, track_values)
    ref_items.extend(audio_items)
    ref_blocks.extend(audio_blocks)
    audio_labels.extend(standalone_labels)

    has_refs = bool(ref_blocks)
    resolved_task = pick_task_type(task_hint, keyframe_open, keyframe_close, has_refs)
    counts = {
        "pictures": len(picture_labels),
        "videos": len(video_labels),
        "audios": len(audio_labels),
    }
    valid_ordinals, packed_ordinals = _tag_ordinals(
        keyframe_images, still_entries, footage_entries, audio_labels
    )
    conditioned_prompt, prompt_warnings = build_prompt_body(
        prompt,
        counts,
        strict=tag_guard,
        task_hint=resolved_task,
        valid_ordinals=valid_ordinals,
    )
    packed_prompt = densify_tag_ordinals(conditioned_prompt, packed_ordinals)

    if keyframes and ref_blocks:
        # 首尾帧 + 参考素材：按当前 ComfyUI 的打包策略决定载荷形式
        hybrid_policy = verify_hybrid_layout()
        prompt_items = [
            {"type": "image", "data": image} for image in keyframe_images
        ] + ref_items
        if hybrid_policy == REF_MODE_REPLACE:
            refs = [
                {"kind": HYBRID_MARKER, "latent": keyframe["latent"]}
                for keyframe in keyframes
            ] + ref_blocks
        else:
            refs = ref_blocks
        tokens = clip.tokenize(packed_prompt, minimax_ref_items=prompt_items)
    elif ref_blocks:
        refs = ref_blocks
        tokens = clip.tokenize(packed_prompt, minimax_ref_items=ref_items)
    else:
        refs = []
        tokens = clip.tokenize(packed_prompt, images=keyframe_images)

    conditioning = clip.encode_from_tokens_scheduled(tokens)
    values = {}
    if keyframes:
        values.update({"minimax_keyframes": keyframes, "minimax_frame_count": frame_count})
    if refs:
        values["minimax_refs"] = refs
    if values:
        conditioning = node_helpers.conditioning_set_values(conditioning, values)

    latent = _apply_audio_mode(latent, mode, encoded_source, audio_cleanup)

    media_map = _build_media_map(
        keyframe_images, picture_labels, still_entries, footage_entries, audio_labels
    )
    report_lines = [
        f"task={resolved_task}",
        f"audio_source={mode}",
        f"frames={frame_count} ({frame_count / VIDEO_FPS:.3f}s at 24fps)",
        f"pictures={len(picture_labels)}, videos={len(video_labels)}, audios={len(audio_labels)}",
    ]
    report_lines.extend(f"warning: {warning}" for warning in prompt_warnings)
    output_audio = final_audio if final_audio is not None else drive_audio
    return conditioning, latent, output_audio, conditioned_prompt, media_map, "\n".join(report_lines)
