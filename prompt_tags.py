from __future__ import annotations

import json
import re


MEDIA_TAG_RE = re.compile(
    r"<\s*(Image|Picture|Video|Audio)\s*(\d+)\s*>|"
    r"(?<![\w<])(Image|Picture|Video|Audio)\s*#?\s*(\d+)\b(?!\s*>)",
    re.IGNORECASE,
)
OFFICIAL_TAG_RE = re.compile(r"<(Picture|Video|Audio)\s+(\d+)>", re.IGNORECASE)


def canonicalize_media_tags(prompt: str, task_type: str | None = None) -> str:
    def replacement(match: re.Match) -> str:
        media_type = (match.group(1) or match.group(3)).lower()
        ordinal = int(match.group(2) or match.group(4))
        if (task_type or "").lower() == "fl2va" and media_type in {"image", "picture"}:
            return f"picture {ordinal}"
        official_type = "Picture" if media_type in {"image", "picture"} else media_type.title()
        return f"<{official_type} {ordinal}>"

    return MEDIA_TAG_RE.sub(replacement, prompt or "")


def prepare_prompt(
    prompt: str,
    counts: dict[str, int],
    strict: bool = True,
    task_type: str | None = None,
    valid_ordinals: dict[str, set[int]] | None = None,
) -> tuple[str, list[str]]:
    normalized = canonicalize_media_tags(prompt, task_type)

    warnings: list[str] = []
    limits = {
        "picture": int(counts.get("pictures", 0)),
        "video": int(counts.get("videos", 0)),
        "audio": int(counts.get("audios", 0)),
    }
    for match in OFFICIAL_TAG_RE.finditer(normalized):
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


def pack_media_tag_ordinals(prompt: str, ordinal_maps: dict[str, dict[int, int]]) -> str:
    """Map stable UI slot ordinals to the dense order used by MiniMax's tokenizer."""
    def replacement(match: re.Match) -> str:
        media_type = match.group(1).lower()
        ordinal = int(match.group(2))
        packed = ordinal_maps.get(media_type, {}).get(ordinal, ordinal)
        return f"<{match.group(1).title()} {packed}>"

    return OFFICIAL_TAG_RE.sub(replacement, prompt or "")


def media_map_json(pictures, videos, audios) -> str:
    def mapped(values):
        if isinstance(values, dict):
            return {str(index): label for index, label in sorted(values.items())}
        return {str(index + 1): label for index, label in enumerate(values)}

    return json.dumps(
        {
            "pictures": mapped(pictures),
            "videos": mapped(videos),
            "audios": mapped(audios),
        },
        ensure_ascii=False,
        indent=2,
    )


# --- RH deterministic prompt constraints (ported from the MiniMaxH3 tool) ---

# Positive phrasing on purpose: it avoids subtitle/caption negation trigger
# words and describes where visible lettering may still appear.
NO_SUBTITLES_CONSTRAINT = (
    "Every spoken line is delivered only as audible voice with naturally synchronized "
    "lip movement and mouth articulation, and spoken words never appear as written text "
    "anywhere in the frame. The lower third of the frame remains occupied by clean scene "
    "content such as clothing, props, gestures, and background depth, and any visible "
    "lettering exists solely on physical objects inside the scene, like signs, posters, "
    "or device screens already established in the description."
)
DEFAULT_SOUNDSCAPE = (
    "Subtle ambient sound appropriate to the depicted environment continues "
    "throughout the video."
)
DEFAULT_MUSIC = (
    "A restrained solo piano score at a slow tempo, with steady, widely spaced notes "
    "and quiet dynamics that fade out gently."
)

_H3_FIELD_NAMES = (
    "integrated_multimodal_description",
    "detailed_description",
    "subject_definitions",
    "summary",
    "retention_analysis",
    "overall_soundscape",
    "non_diegetic_music",
)
_MAIN_FIELD_ANCHOR_RE = re.compile(
    r"(?:^|\n)(?:integrated_multimodal_description|detailed_description):"
)
_SHOT_ONE_RE = re.compile(r"\[Shot\s+1\]")
_MUTE_SENTINEL_RE = re.compile(r"^n/?a\.?$", re.IGNORECASE)


def _normalize_blank_runs(text: str) -> str:
    text = re.sub(r"[ \t]+(\r?\n)", r"\1", text)
    text = re.sub(r"(?:\r?\n[ \t]*){3,}", "\n\n", text)
    return text


def _sound_field_span(text: str, field: str):
    """Locate a top-level H3 field; return (head_end, body_end) or None.

    The body stops at the first blank line or the next known field name so a
    rewrite can never swallow trailing prompt content that follows the block.
    """
    head = re.search(r"(?m)^" + field + r":[ \t]*", text)
    if head is None:
        return None
    others = "|".join(name for name in _H3_FIELD_NAMES if name != field)
    nxt = re.compile(r"(?m)^[ \t]*(?:" + others + r"):").search(text, head.end())
    limit = nxt.start() if nxt else len(text)
    blank = re.search(r"\n[ \t]*\n", text[head.end():limit])
    body_end = head.end() + blank.start() if blank else limit
    return head.end(), body_end


def _apply_sound_field(text: str, field: str, default: str, enabled: bool) -> str:
    span = _sound_field_span(text, field)
    if span is None:
        # Enabled leaves prompts without the field untouched; disabled still
        # guarantees a mute instruction by appending one.
        if enabled:
            return text
        base = text.rstrip()
        return (base + "\n\n" if base else "") + field + ": N/A"
    head_end, body_end = span
    body = text[head_end:body_end].strip()
    if enabled:
        # Keep real descriptions; only heal an explicit mute sentinel so the
        # toggle never silently rewrites a authored soundscape or score.
        if body and not _MUTE_SENTINEL_RE.match(body):
            return text
        replacement = default
    else:
        replacement = "N/A"
    head = re.search(r"(?m)^" + field + r":", text)
    return text[: head.start()] + field + ": " + replacement + text[body_end:]


def _inject_no_subtitle(text: str) -> str:
    anchor = _MAIN_FIELD_ANCHOR_RE.search(text)
    search_from = anchor.end() if anchor else 0
    shot = _SHOT_ONE_RE.search(text, search_from)
    if shot is None:
        # Reference contract: without a [Shot 1] anchor the constraint is only
        # stripped, never appended, so unstructured text stays untouched.
        return text
    tail = text[shot.end():].lstrip()
    return text[: shot.end()] + " " + NO_SUBTITLES_CONSTRAINT + "\n\n" + tail


def apply_advanced_constraints(
    prompt: str,
    *,
    no_subtitle: bool = True,
    soundscape: bool = False,
    music: bool = False,
) -> str:
    """Apply the RH advanced prompt constraints deterministically.

    Strip-then-rebuild keeps every call idempotent, so the user-visible prompt
    stays clean while execution always reflects the current toggle state.
    """
    text = str(prompt or "")
    if NO_SUBTITLES_CONSTRAINT in text:
        text = text.replace(NO_SUBTITLES_CONSTRAINT, "")
        text = _normalize_blank_runs(text)
    text = _apply_sound_field(text, "overall_soundscape", DEFAULT_SOUNDSCAPE, soundscape)
    text = _apply_sound_field(text, "non_diegetic_music", DEFAULT_MUSIC, music)
    text = _normalize_blank_runs(text)
    text = re.sub(r"\s*overall_soundscape:", "\n\noverall_soundscape:", text, count=1)
    if no_subtitle:
        text = _inject_no_subtitle(text)
    return text
