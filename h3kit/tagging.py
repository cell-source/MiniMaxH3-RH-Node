from __future__ import annotations

import json
import re


TAG_PATTERN = re.compile(
    r"<\s*(Image|Picture|Video|Audio)\s*(\d+)\s*>|"
    r"(?<![\w<])(Image|Picture|Video|Audio)\s*#?\s*(\d+)\b(?!\s*>)",
    re.IGNORECASE,
)
ANGLE_TAG_PATTERN = re.compile(r"<(Picture|Video|Audio)\s+(\d+)>", re.IGNORECASE)


def normalize_tags(prompt: str, task_hint: str | None = None) -> str:
    def _substitute(match: re.Match) -> str:
        media_type = (match.group(1) or match.group(3)).lower()
        ordinal = int(match.group(2) or match.group(4))
        if (task_hint or "").lower() == "fl2va" and media_type in {"image", "picture"}:
            return f"picture {ordinal}"
        official_type = "Picture" if media_type in {"image", "picture"} else media_type.title()
        return f"<{official_type} {ordinal}>"

    return TAG_PATTERN.sub(_substitute, prompt or "")


def build_prompt_body(
    prompt: str,
    counts: dict[str, int],
    strict: bool = True,
    task_hint: str | None = None,
    valid_ordinals: dict[str, set[int]] | None = None,
) -> tuple[str, list[str]]:
    normalized = normalize_tags(prompt, task_hint)

    warnings: list[str] = []
    limits = {
        "picture": int(counts.get("pictures", 0)),
        "video": int(counts.get("videos", 0)),
        "audio": int(counts.get("audios", 0)),
    }
    for match in ANGLE_TAG_PATTERN.finditer(normalized):
        media_type, ordinal = match.group(1).lower(), int(match.group(2))
        valid = valid_ordinals.get(media_type) if valid_ordinals else None
        missing = ordinal not in valid if valid is not None else ordinal < 1 or ordinal > limits[media_type]
        if missing:
            warnings.append(
                f"{match.group(0)} is not connected; available {media_type} ordinals are "
                f"{sorted(valid) if valid is not None else list(range(1, limits[media_type] + 1))}"
            )
    if strict and warnings:
        raise ValueError("当前提示词引用了不存在的素材标签，请引用正确的标签后重试")
    return normalized, warnings


def densify_tag_ordinals(prompt: str, ordinal_maps: dict[str, dict[int, int]]) -> str:
    """Map stable UI slot ordinals to the dense order used by MiniMax's tokenizer."""
    def _substitute(match: re.Match) -> str:
        media_type = match.group(1).lower()
        ordinal = int(match.group(2))
        packed = ordinal_maps.get(media_type, {}).get(ordinal, ordinal)
        return f"<{match.group(1).title()} {packed}>"

    return ANGLE_TAG_PATTERN.sub(_substitute, prompt or "")


def tag_map_json(pictures, videos, audios) -> str:
    def tagged(values):
        if isinstance(values, dict):
            return {str(index): label for index, label in sorted(values.items())}
        return {str(index + 1): label for index, label in enumerate(values)}

    return json.dumps(
        {
            "pictures": tagged(pictures),
            "videos": tagged(videos),
            "audios": tagged(audios),
        },
        ensure_ascii=False,
        indent=2,
    )


# --- RH deterministic prompt constraints (ported from the MiniMaxH3 tool) ---

# Positive phrasing on purpose: it avoids subtitle/caption negation trigger
# words and describes where visible lettering may still appear. Kept byte
# identical to the engine's NO_SUBTITLES_CONSTRAINT (v6, prompt_engine/rules.js)
# so the strip-then-rebuild contract stays idempotent across the panel and
# engine paths.
NO_SUBTITLES_CONSTRAINT = (
    "The full frame, including the lower third and every edge, shows only the depicted scene throughout, as complete in speech as in silence. Dialogue quotes, language labels, and speaker identifiers are soundtrack instructions only; off-screen voices stay off-screen, visible mouths follow the scene, and the picture never follows speech wording or rhythm. Visible writing stays on scene surfaces such as signs, posters, and screens, never erased, and spoken words are never rendered as image text."
)

# Constraint sentences emitted by older versions (engine v3/v4/v4.1/v5 EN+ZH,
# the v6 zh block, and the pre-v6 condensed variant). They must strip cleanly,
# otherwise a prompt written before an upgrade (or generated in zh) ends up
# carrying two constraint blocks after the toggle is applied.
NO_SUBTITLES_LEGACY_CONSTRAINTS = (
    "Every spoken line lives only in the soundtrack: the words reach the audience exclusively as audible voice with naturally synchronized lip movement and mouth articulation, and exist nowhere on the image itself. The visual frame consists purely of photographic, in-camera scene content — performers, wardrobe, props, sets, practical lighting, and background depth — recorded by the physical camera as one continuous clean photographic image. The lower third of the frame stays fully occupied by clean scene content such as clothing, props, gestures, and background depth, while the upper frame carries only the established set, sky, or practical lighting. Any visible lettering exists solely on physical objects inside the scene, like signs, posters, or device screens already established in the description, photographed in-camera as part of the physical set. Render the shot as clean theatrical cinematography, free of broadcast graphics, channel packaging, and any on-screen graphic overlay layer.",
    "每句台词只存在于声轨之中：观众仅通过音频听到台词，口型与嘴部动作自然同步，台词绝不出现在画面上。画面完全由实拍的摄影内容构成——演员、服装、道具、布景、实拍灯光与背景纵深——由实体摄影机作为单一连续的干净画面记录。画面下三分之一完全由干净的场景内容占据，如服装、道具、手势与背景纵深；画面上部只呈现已确立的布景、天空或实拍灯光。任何可见文字只存在于场景内的实体物体上，如描述中已确立的标牌、海报或设备屏幕，作为实体布景的一部分被实拍记录。把本镜头渲染为干净的院线电影摄影质感，画面中不出现广播图形、频道包装或任何屏幕图形叠加层。",
    "From the first frame through the final frame, deliver a clean picture master: every part of the image belongs to the depicted scene, including the full lower third, bottom edge, upper edge, and corners. Every spoken word, including dialogue, narration, announcements, and off-screen voices, is an audio performance only; dialogue quotations, language labels, and speaker identifiers in this prompt are instructions for the soundtrack alone. For visible speakers, show only the mouth movement and performance specified by the scene; off-screen speech keeps its established off-screen source and leaves visible mouths in their described state. Throughout each utterance and the pauses before and after it, the lower third and all frame edges continuously retain the existing scene detail, texture, depth, and lighting, just as they do during silence. The visual image evolves solely through the actions and camera behavior already specified, independently of the wording, language, timing, and rhythm of speech; preserve the existing composition and scene objects. Visible writing is limited to text explicitly established on physical objects in the scene, attached to those surfaces with their perspective and occlusion, retaining its scene-defined content independently of spoken words. Treat the soundtrack and picture as separate production channels: all spoken wording is delivered to the ear, while the eye receives only the scene itself, continuously for the entire clip.",
    "从第一帧到最后一帧，输出干净的画面母版：画面每个区域都属于所描述的场景，包括完整的下三分之一、底边、上边和四角。每个说出的词语，包括对白、旁白、播报与画外音，都只作为声音表演存在；提示词中的台词引文、语言标签和说话人编号仅用于指导声轨。画内人物只呈现场景明确要求的口型与表演；画外声音保持既定的画外来源，画内人物的嘴部保持原描述状态。每句台词说出期间以及前后的停顿中，下三分之一与全部画面边缘始终连续呈现原有场景的细节、纹理、纵深和光线，与安静时一样完整。画面只随已明确的动作和镜头行为发展，独立于话语的措辞、语言、起止时机和节奏，保持既有构图与场景物体。可见文字仅限于输入明确设定在场景实体物体表面的文字，依附对应表面并遵循其透视和遮挡，内容始终由场景设定决定，独立于正在说出的台词。将声轨与画面作为两个独立的制作通道：所有台词措辞仅传递给耳朵，眼睛从头到尾持续看到的只有场景本身。",
    "From the first frame through the final frame, deliver a clean picture master: every part of the image belongs to the depicted scene, including the full lower third, bottom edge, upper edge, and corners. Every spoken word, including dialogue, narration, announcements, and off-screen voices, is an audio performance only; dialogue quotations, language labels, and speaker identifiers in this prompt are instructions for the soundtrack alone. For visible speakers, show only the mouth movement and performance specified by the scene; off-screen speech keeps its established off-screen source and leaves visible mouths in their described state. Throughout each utterance and the pauses before and after it, the lower third and all frame edges continuously retain the existing scene detail, texture, depth, and lighting, just as they do during silence. The visual image evolves solely through the actions and camera behavior already specified, independently of the wording, language, timing, and rhythm of speech; preserve the existing composition and scene objects. Visible writing lives on the physical surfaces of the depicted scene: text established in the description, along with naturally existing lettering on signs, posters, books, screens, packaging, and printed clothing, stays rendered on its own surface with true perspective and occlusion, its content set by the scene and independent of spoken words; in-scene lettering is never erased or faded, and spoken words are never rendered as writing on any part of the image. Treat the soundtrack and picture as separate production channels: all spoken wording is delivered to the ear, while the eye receives only the scene itself, continuously for the entire clip.",
    "从第一帧到最后一帧，输出干净的画面母版：画面每个区域都属于所描述的场景，包括完整的下三分之一、底边、上边和四角。每个说出的词语，包括对白、旁白、播报与画外音，都只作为声音表演存在；提示词中的台词引文、语言标签和说话人编号仅用于指导声轨。画内人物只呈现场景明确要求的口型与表演；画外声音保持既定的画外来源，画内人物的嘴部保持原描述状态。每句台词说出期间以及前后的停顿中，下三分之一与全部画面边缘始终连续呈现原有场景的细节、纹理、纵深和光线，与安静时一样完整。画面只随已明确的动作和镜头行为发展，独立于话语的措辞、语言、起止时机和节奏，保持既有构图与场景物体。可见文字只存在于场景实体物体的表面上：描述中确立的文字，以及招牌、海报、书本、屏幕、器物包装、印花衣物等场景自带的文字，都依附所在表面并遵循其透视与遮挡，内容始终由场景设定决定、独立于正在说出的台词；这些场景文字始终完整保留，不被擦除或淡化，说出的词语也绝不在画面任何区域被渲染为文字。将声轨与画面作为两个独立的制作通道：所有台词措辞仅传递给耳朵，眼睛从头到尾持续看到的只有场景本身。",
    "Render a clean picture master: the full frame, including the lower third and every edge, shows only the depicted scene in complete detail and lighting, as continuous during speech as in silence. Every spoken word exists as sound alone: visible speakers mouth only what the scene requires, off-screen voices keep their established source, dialogue quotes, language labels, and speaker identifiers in this prompt are instructions for the soundtrack, and the picture never follows the wording, language, or timing of speech. All writing stays on the surfaces of scene objects — signs, posters, screens, packaging, printed clothing, and text established in the description — never erased, while spoken words are never rendered as writing anywhere in the image.",
    "输出干净的画面母版：整幅画面（含下三分之一与全部边缘）自始至终只呈现所描述的场景，细节与光线在说话和安静时同样完整。每个说出的词语只作为声音存在：画内人物只按场景要求开口，画外声音保持既定的画外来源，提示词中的台词引文、语言标签和说话人编号只用于指导声轨，画面从不跟随话语的措辞、语言或节奏。可见文字只保留在场景物体的表面上（招牌、海报、屏幕、器物包装、印花衣物及描述中确立的文字），始终完整不被擦除；说出的词语绝不在画面任何区域被渲染为文字。",
    "画面（含下三分之一与全部边缘）自始至终只呈现所描述的场景，说话与安静时同样完整。对白、语言标签与说话人编号仅指导声轨；画外声音保持画外，口型只按场景要求，画面不随话语的措辞或节奏变化。可见文字只保留在招牌、屏幕等场景物体表面，不被擦除；说出的词语绝不渲染为画面文字。",
    "Every spoken line is delivered only as audible voice with naturally synchronized lip movement and mouth articulation, and spoken words never appear as written text anywhere in the frame. The lower third of the frame remains occupied by clean scene content such as clothing, props, gestures, and background depth, and any visible lettering exists solely on physical objects inside the scene, like signs, posters, or device screens already established in the description.",
)
# 声音/音乐字段的缺省句：开关打开而提示词里只有 N/A 时补上。
# 英文取值与 prompt_engine/rules.js 的 DEFAULT_SOUNDSCAPE_EN / DEFAULT_MUSIC_EN 逐字一致。
SOUND_FIELD_DEFAULTS = {
    "overall_soundscape": (
        "Subtle ambient sound appropriate to the depicted environment continues "
        "throughout the video."
    ),
    "non_diegetic_music": (
        "A restrained solo piano score at a slow tempo, with steady, widely spaced notes "
        "and quiet dynamics that fade out gently."
    ),
}
DEFAULT_SOUNDSCAPE = SOUND_FIELD_DEFAULTS["overall_soundscape"]
DEFAULT_MUSIC = SOUND_FIELD_DEFAULTS["non_diegetic_music"]

# H3 解析提示词结构时认的顶层字段名；只用于判断“字段体到哪里结束”，
# 集合无序，拼正则前先排序以保证结果稳定。
_H3_FIELD_NAMES = frozenset({
    "integrated_multimodal_description",
    "detailed_description",
    "subject_definitions",
    "summary",
    "retention_analysis",
    "overall_soundscape",
    "non_diegetic_music",
})
# 主描述字段的锚点：“无字幕”约束只在它之后、[Shot 1] 处插入。
_MAIN_FIELD_ANCHOR_RE = re.compile(
    "|".join((r"(?:^|\n)integrated_multimodal_description:", r"(?:^|\n)detailed_description:"))
)
_SHOT_ONE_RE = re.compile(r"\[Shot\s+1\]")
_MUTE_SENTINEL_RE = re.compile(r"^n/?a\.?$", flags=re.IGNORECASE)
_SOUNDSCAPE_HEAD_RE = re.compile(r"\s*overall_soundscape:")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")
_TRAILING_BLANK_RE = re.compile(r"[ \t]+(?=\r?\n)")
_EXTRA_BLANK_LINES_RE = re.compile(r"(?:\r?\n[ \t]*){3,}")


def _normalize_blank_runs(text: str) -> str:
    """去掉行尾空白，并把 3 个以上连续换行折叠成一个空行。"""
    return _EXTRA_BLANK_LINES_RE.sub("\n\n", _TRAILING_BLANK_RE.sub("", text))


def _next_field_start(text: str, offset: int, field: str) -> int | None:
    """下一个已知字段（跳过自己）的起始位置；找不到返回 None。"""
    names = sorted(name for name in _H3_FIELD_NAMES if name != field)
    found = re.compile(r"(?m)^[ \t]*(?:" + "|".join(names) + r"):").search(text, offset)
    return found.start() if found else None


def _sound_field_span(text: str, field: str) -> tuple[int, int] | None:
    """定位某个顶层字段体，返回 (字段头结束, 字段体结束)，找不到返回 None。

    字段体止于首个空行或下一个已知字段名，以免写入时吞掉后面的提示词内容。
    """
    head = re.search(r"(?m)^" + field + r":[ \t]*", text)
    if head is None:
        return None
    following = _next_field_start(text, head.end(), field)
    limit = following if following is not None else len(text)
    blank = _BLANK_LINE_RE.search(text, head.end(), limit)
    return head.end(), blank.start() if blank else limit


def _apply_sound_field(text: str, field: str, default: str, enabled: bool) -> str:
    """按开关写入或清除某个声音字段。"""
    span = _sound_field_span(text, field)
    if span is None:
        if enabled:
            return text
        # 关闭时提示词里即使没有这个字段，也要留下一条明确的静音指令。
        base = text.rstrip()
        return (base + "\n\n" if base else "") + field + ": N/A"
    head_end, body_end = span
    body = text[head_end:body_end].strip()
    if enabled:
        # 已有真实描述就原样保留，只有显式 N/A 才补缺省值，
        # 避免开关静默覆盖作者已写好的声音设计。
        if body and not _MUTE_SENTINEL_RE.match(body):
            return text
        written = default
    else:
        written = "N/A"
    head = re.search(r"(?m)^" + field + r":", text)
    return text[: head.start()] + field + ": " + written + text[body_end:]


def _inject_no_subtitle(text: str) -> str:
    """把“无字幕”约束插到 [Shot 1] 之后；找不到锚点则原样返回。

    参考实现的口径：没有 [Shot 1] 就只剥离不追加，保证非结构化文本不被改写。
    """
    anchor = _MAIN_FIELD_ANCHOR_RE.search(text)
    shot = _SHOT_ONE_RE.search(text, anchor.end() if anchor else 0)
    if shot is None:
        return text
    tail = text[shot.end():].lstrip()
    return text[: shot.end()] + " " + NO_SUBTITLES_CONSTRAINT + "\n\n" + tail


def apply_advanced_constraints(
    prompt: str,
    *,
    subtitle_free: bool = True,
    soundscape: bool = False,
    music: bool = False,
) -> str:
    """Apply the RH advanced prompt constraints deterministically.

    Strip-then-rebuild keeps every call idempotent, so the user-visible prompt
    stays clean while execution always reflects the current toggle state.
    """
    text = str(prompt or "")
    removed = False
    for block in (*NO_SUBTITLES_LEGACY_CONSTRAINTS, NO_SUBTITLES_CONSTRAINT):
        if block in text:
            text = text.replace(block, "")
            removed = True
    if removed:
        text = _normalize_blank_runs(text)
    for field, default, enabled in (
        ("overall_soundscape", DEFAULT_SOUNDSCAPE, soundscape),
        ("non_diegetic_music", DEFAULT_MUSIC, music),
    ):
        text = _apply_sound_field(text, field, default, enabled)
    text = _normalize_blank_runs(text)
    text = _SOUNDSCAPE_HEAD_RE.sub("\n\noverall_soundscape:", text, count=1)
    if subtitle_free:
        text = _inject_no_subtitle(text)
    return text
