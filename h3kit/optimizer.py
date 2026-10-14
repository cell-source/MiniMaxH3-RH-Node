from __future__ import annotations

# 标准库
import asyncio
import base64
import gc
import json
import logging
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from difflib import SequenceMatcher
from pathlib import Path

# ComfyUI 运行时依赖
import aiohttp
import folder_paths
from aiohttp import web
from server import PromptServer

_LOGGER = logging.getLogger("minimax_h3.prompt_optimizer")

# 上游把凭据放在查询串时（Gemini 的 ?key=），异常文本与响应正文都会带上它，因此
# 任何准备写入日志或返回前端的文本都必须先脱敏（审查 M4）。
_CREDENTIAL_QUERY = re.compile(
    r"(?<![A-Za-z0-9_])(key|api_key|api-key|access_token|token)=[^&\s'\"]+",
    re.IGNORECASE,
)
# 短错误标签保留（脱敏 + 截断），完整响应正文只进日志。
_UPSTREAM_LABEL_LIMIT = 200
# 按状态码给出可执行提示，避免依赖上游正文才能定位问题。
_UPSTREAM_HTTP_HINTS = {
    400: "上游拒绝了请求（模型名或参数不受支持）",
    401: "鉴权失败：API Key 无效",
    403: "鉴权失败：Key 无权限或未开通该模型",
    404: "端点或模型不存在",
    422: "上游不接受请求格式",
    429: "触发上游限流或额度不足",
}


def _redact_credentials(text: object) -> str:
    return _CREDENTIAL_QUERY.sub(r"\1=***", str(text))


def _upstream_label(value: object) -> str:
    """短错误标签（如 errorMessage/msg）：服务方已概括过的原因，脱敏截断后可用。"""
    collapsed = " ".join(_redact_credentials(value).split())
    return collapsed[:_UPSTREAM_LABEL_LIMIT]


def _logged_upstream_error(public: str, detail: object) -> RuntimeError:
    """上游细节只进服务端日志，返回前端的只有自撰文案（审查 M4）。"""
    trimmed = _redact_credentials(detail)[:1000]
    _LOGGER.warning("%s | upstream detail: %s", public, trimmed)
    return RuntimeError(public)


BASE_SETTINGS = {
    "mode": "api",
    "provider": "runninghub",
    "api_url": "https://www.runninghub.cn/openapi/v2",
    "api_key": "",
    "model": "openai/gpt-5.6-sol",
    "protocol": "runninghub",
    "read_media": True,
    "output_language": "中文",
    "local_model": "",
    "local_mmproj": "",
    "local_device": "cuda",
    "max_tokens": 4096,
    "auto_optimize": False,
}
# 润色（✦）沿用生成平台（h3kit.llm 的 7 家预设）：面板端点/模型留空时由这里的
# 预设补齐。端点是 base URL——_chat_endpoint() 会追加 /chat/completions；缺预设会把
# DeepSeek/GLM 的请求连同 Key 一起打到 RunningHub（2026-09-26 审查）。模型与
# h3kit.llm 保持一致。DeepSeek 无视觉 API：勾选「读取视觉素材」时会被
# _reject_images_without_vision 提前拦截（GLM 可在 ai_model 填 glm-5v-turbo 等视觉模型）。
# 每行 =（provider, base URL, 默认模型, 协议）。
_ENDPOINT_ROWS = (
    ("openai", "https://api.openai.com/v1", "gpt-4.1-mini", "openai"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash", "gemini"),
    ("openrouter", "https://openrouter.ai/api/v1", "google/gemini-2.5-flash", "openai"),
    ("dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-vl-max", "openai"),
    ("siliconflow", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-VL-72B-Instruct", "openai"),
    ("deepseek", "https://api.deepseek.com", "deepseek-flash", "openai"),
    ("glm", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flash", "openai"),
    ("runninghub", "https://www.runninghub.cn/openapi/v2", "openai/gpt-5.6-sol", "runninghub"),
    ("runninghub_overseas", "https://www.runninghub.ai/openapi/v2", "openai/gpt-5.6-sol", "runninghub"),
)
SERVICE_ENDPOINTS = {row[0]: (row[1], row[2], row[3]) for row in _ENDPOINT_ROWS}
RH_APP_ID = "2089252473927196673"
RH_OVERSEAS_APP_ID = "2090668262521675778"

# 国内版与海外版是同一张应用的两套字段 id：视频槽位与模型槽位一致，只有图像槽位、
# 系统/用户提示词槽位与 max_tokens 槽位不同。
_RH_COMMON_NODES = {"video": "11", "model": "1"}
RH_APP_NODES = {
    "runninghub": {
        **_RH_COMMON_NODES,
        "images": ("2", "12", "13", "14", "15", "16", "17", "18"),
        "system_prompt": "9",
        "user_prompt": "10",
        "max_tokens": "19",
    },
    "runninghub_overseas": {
        **_RH_COMMON_NODES,
        "images": ("20", "12", "13", "14", "15", "16", "17", "18"),
        "system_prompt": "10",
        "user_prompt": "9",
        "max_tokens": "21",
    },
}
# RunningHub 应用详情页的模型下拉项（内置于包内，页面抓取失败时作为回退）。
RH_MODEL_LIST = (
    "openai/gpt-5.6-sol", "openai/gpt-5.6-sol-saver",
    "openai/gpt-5.6-terra", "openai/gpt-5.6-terra-saver",
    "openai/gpt-5.5", "openai/gpt-5.5-saver",
    "openai/gpt-5.6-luna", "openai/gpt-5.6-luna-saver",
    "google/gemini-3.1-flash-lite-preview", "google/gemini-3.5-flash",
    "openai/gpt-5.5-pro", "anthropic/claude-fable-5",
    "openai/gpt-5.4-pro", "anthropic/claude-opus-5",
    "anthropic/claude-opus-4.8", "anthropic/claude-opus-4.7",
    "glm-5.2", "anthropic/claude-opus-4.6",
    "openai/gpt-5.4", "openai/gpt-5.3-codex",
    "glm-5.1", "glm-5-turbo",
    "qwen/qwen3.8-max", "anthropic/claude-sonnet-4.6",
    "glm-5", "anthropic/claude-sonnet-5",
    "qwen/qwen3.7-max", "glm-5v-turbo",
    "qwen/qwen3.7-plus", "deepseek/deepseek-v4-pro",
    "xai/grok-4.6", "xai/grok-4.5",
    "xai/grok-4.3", "qwen/qwen3.6-plus",
    "google/gemini-3.1-pro-preview", "bytedance/doubao-seed-evolving",
    "bytedance/doubao-seed-2.1-pro", "anthropic/claude-sonnet-4.5",
    "bytedance/doubao-seed-2.1-turbo", "anthropic/claude-opus-4.5",
    "bytedance/doubao-seed-2.0-pro", "bytedance/doubao-seed-2.0-code",
    "deepseek/deepseek-v4-flash", "qwen/qwen3.6-flash",
    "openai/gpt-5.4-mini", "openai/gpt-5.4-nano",
    "google/gemini-3-flash-preview", "google/gemini-2.5-flash",
    "bytedance/doubao-seed-2.0-lite", "bytedance/doubao-seed-2.0-mini",
    "minimax/minimax-m2.7", "anthropic/claude-haiku-4.5",
    "qwen/qwen3.6-max-preview", "anthropic/claude-haiku-4.5-saver",
    "anthropic/claude-opus-4.6-saver", "anthropic/claude-opus-4.7-saver",
    "anthropic/claude-opus-4.8-saver", "anthropic/claude-sonnet-4.6-saver",
    "google/gemini-2.5-pro", "google/gemini-3.5-flash-lite",
    "google/gemini-3.6-flash",
)
RH_DETAIL_URL = f"https://www.runninghub.cn/call-api/api-detail/{RH_APP_ID}?apiType=4"
RH_OVERSEAS_DETAIL_URL = f"https://www.runninghub.ai/zh-cn/call-api/api-detail/{RH_OVERSEAS_APP_ID}?apiType=4"
_RH_MODEL_CACHE: tuple[str, ...] = RH_MODEL_LIST
_RH_OVERSEAS_MODEL_CACHE: tuple[str, ...] = RH_MODEL_LIST
_RH_MODEL_MUTEX = asyncio.Lock()
_ROUTES_BOUND = False
_ACTIVE_REQUESTS: dict[str, asyncio.Task] = {}
_ACTIVE_CANCEL_EVENTS: dict[str, threading.Event] = {}
_ACTIVE_RH_TASKS: dict[str, tuple[str, str, str]] = {}
_ASYNC_OPTIMIZER_JOBS: dict[str, asyncio.Task] = {}
_GGUF_MUTEX = threading.RLock()
_GGUF_INSTANCE = None
_GGUF_PIPE = None
_GGUF_SETTINGS: tuple[str, str, str] | None = None


def _llm_root_list() -> list[Path]:
    """模型根目录候选（大小写不敏感）。

    本包位于 ComfyUI/custom_nodes/<package> 下，因此模型目录按相对位置推导，
    桌面版与托管版一致。Linux 区分大小写而 Windows 不区分，托管用户常建
    ``models/LLM`` 而不是文档里的 ``models/llm``，故这里按 casefold 匹配；
    两者同时存在时（托管 Linux 常见）都返回。
    """
    models_root = Path(__file__).resolve().parents[2] / "models"
    canonical = models_root / "llm"
    if not models_root.is_dir():
        return [canonical]
    try:
        siblings = [item for item in models_root.iterdir() if item.is_dir()]
    except OSError:
        return [canonical]
    matched = [item for item in siblings if item.name.casefold() == "llm"]
    if not matched:
        # 目录还没建：返回规范路径，调用方仍可安全地报告「模型列表为空」。
        return [canonical]
    return sorted(matched, key=lambda item: (item.name != "llm", item.name))


def _primary_llm_root() -> Path:
    """兼容旧调用方：只取第一个候选根目录。"""
    candidates = _llm_root_list()
    return candidates[0]


_VISUAL_CONFIG_MARKERS = (
    "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qwen-vl", "qwen2_vl", "qwen2_5_vl", "qwen3_vl",
    "vision", "visual", "gemma3", "gemma-3", "minicpm-v", "internvl",
)
_VISUAL_PROCESSOR_FILES = ("processor_config.json", "preprocessor_config.json", "tokenizer_config.json")


def _looks_visual(path: Path) -> bool:
    """目录内是否有带视觉标记的 config.json，且带处理器配置文件。"""
    if not path.is_dir():
        return False
    config_file = path / "config.json"
    if not config_file.is_file():
        return False
    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    flattened = json.dumps(raw, ensure_ascii=False).lower()
    if not any(marker in flattened for marker in _VISUAL_CONFIG_MARKERS):
        return False
    return any((path / name).exists() for name in _VISUAL_PROCESSOR_FILES)


_MMPROJ_MARKERS = ("mmproj", "mm-projector", "projector")


def _looks_like_mmproj(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() != ".gguf":
        return False
    lowered = path.name.lower()
    return any(marker in lowered for marker in _MMPROJ_MARKERS)


_MMPROJ_STEM_PREFIX = re.compile(r"^(?:mmproj|mm-projector|projector)[-_. ]*", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _gguf_digest(name: str, *, projector: bool = False) -> str:
    """把文件名归一成可比较的指纹（去掉量化后缀与所有分隔符）。"""
    stem = Path(name).stem
    if projector:
        stem = _MMPROJ_STEM_PREFIX.sub("", stem)
    stripped = _GGUF_QUANT_SUFFIX.sub("", stem)
    return _NON_ALNUM.sub("", stripped.lower())


_GGUF_FAMILY = re.compile(r"(qwen\s*-?\s*3(?:[.\-]?\d+)?(?:\s*-?\s*vl)?)")
_GGUF_PARAM_SIZE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*b(?![a-z])")


def _gguf_model_key(name: str) -> tuple[str, str] | None:
    """从松散的文件名里抽出「模型族 + 参数量」；两者缺一则不成键。"""
    normalized = Path(name).stem.lower().replace("_", "-")
    family = _GGUF_FAMILY.search(normalized)
    size = _GGUF_PARAM_SIZE.search(normalized)
    if family is None or size is None:
        return None
    return _NON_ALNUM.sub("", family.group(1)), f"{size.group(1)}b"


_GGUF_QUANT_SUFFIX = re.compile(
    r"(?:[-_.](?:q\d+(?:[_-][a-z0-9]+)*|iq\d+(?:[_-][a-z0-9]+)*|f16|f32|fp16|fp32|bf16))+$",
    re.IGNORECASE,
)


_MMPROJ_MIN_RATIO = 0.72


def _scan_projectors() -> list[Path]:
    """扫出所有候选 mmproj 投影器。"""
    return [
        candidate
        for root in _llm_root_list() if root.is_dir()
        for candidate in root.rglob("*.gguf")
        if _looks_like_mmproj(candidate)
    ]


def _mmproj_score(identity: str, candidate_name: str) -> tuple[int, float]:
    """两段式打分：先看名称是否互相包含，再用相似度比值兜底。"""
    candidate = _gguf_digest(candidate_name, projector=True)
    wraps = 1 if identity in candidate or candidate in identity else 0
    return wraps, SequenceMatcher(None, identity, candidate).ratio()


def _paired_mmproj(model_path: Path) -> list[Path]:
    """给主模型挑配套的 mmproj 投影器：先精确指纹，再按族/参数量收敛。"""
    identity = _gguf_digest(model_path.name)
    projectors = _scan_projectors()
    exact = [candidate for candidate in projectors
             if _gguf_digest(candidate.name, projector=True) == identity]
    if exact:
        return sorted(exact, key=lambda candidate: candidate.name.lower())

    # 投影器常只带模型代号与参数量，而主模型会多出 UD、发布者、量化等词。
    # 先限定到同族同参数量，再按名字挑最接近的。
    signature = _gguf_model_key(model_path.name)
    compatible = [candidate for candidate in projectors
                  if signature and _gguf_model_key(candidate.name) == signature]
    scored = [(_mmproj_score(identity, candidate.name), candidate)
              for candidate in (compatible or projectors)]
    if not scored:
        return []
    best = max(score for score, _candidate in scored)
    # 没有族/参数量匹配时保守处理，弱相似的名称一律拒绝。
    if not compatible and best[0] == 0 and best[1] < _MMPROJ_MIN_RATIO:
        return []
    return sorted([candidate for score, candidate in scored if score == best],
                  key=lambda candidate: candidate.name.lower())


_QWEN35_NAME = re.compile(r"qwen[-_. ]?3[._-]?(?:5|6|8)")


def _gguf_pipe_name(path: Path) -> str | None:
    """按文件名判断该 GGUF 走哪个 llama-cpp chat handler。"""
    lowered = path.name.lower()
    if _QWEN35_NAME.search(lowered):
        return "qwen35"
    if "qwen3-vl" in lowered or "qwen3vl" in lowered:
        return "qwen3vl"
    return None


def _walk_model_files(paths: list[tuple[Path, Path]]) -> list[tuple[Path, Path, str]]:
    """给每个文件分配稳定的相对 ID；只有跨根目录重名时才加根目录前缀。"""
    relatives = [(root, path, path.relative_to(root).as_posix()) for root, path in paths]
    seen: dict[str, int] = {}
    for _root, _path, relative in relatives:
        key = relative.casefold()
        seen[key] = seen.get(key, 0) + 1
    return [
        (root, path, f"{root.name}/{relative}" if seen[relative.casefold()] > 1 else relative)
        for root, path, relative in relatives
    ]


# 模型扫描缓存（审查 M11）：rglob 全目录开销大且 /config 每次打开弹窗都会触发，
# 按目录指纹（各根目录直接子项的 mtime/size 聚合）缓存，未变化时零 IO。
_SCAN_CACHE: dict[str, tuple[tuple, list[dict]]] = {}


def _llm_roots_fingerprint(roots) -> tuple:
    """为每个模型根目录取一个廉价的「直接子项」指纹（名称/mtime/大小）。"""
    snapshot = []
    for root in roots:
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.lower())
            entries = tuple(
                (item.name, int(item.stat().st_mtime), int(item.stat().st_size)) for item in children
            )
        except OSError:
            entries = ()
        snapshot.append((str(root), entries))
    return tuple(snapshot)


def _cached_scan(cache_key: str, roots, compute):
    """目录指纹未变时直接复用上次扫描结果（避免每次弹窗都 rglob）。"""
    fingerprint = _llm_roots_fingerprint(roots)
    hit = _SCAN_CACHE.get(cache_key)
    if hit is not None and hit[0] == fingerprint:
        return hit[1]
    scanned = compute()
    _SCAN_CACHE[cache_key] = (fingerprint, scanned)
    return scanned


def _collect_model_paths() -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    """一次遍历模型根目录，分出 transformers 视觉目录与受支持的 GGUF 文件。"""
    directories: list[tuple[Path, Path]] = []
    gguf_files: list[tuple[Path, Path]] = []
    for root in _llm_root_list():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if _looks_visual(child):
                directories.append((root, child))
        for candidate in sorted(root.rglob("*.gguf"), key=lambda item: str(item).lower()):
            if _looks_like_mmproj(candidate) or _gguf_pipe_name(candidate) is None:
                continue
            gguf_files.append((root, candidate))
    return directories, gguf_files


def _compute_visual_models() -> list[dict]:
    directories, gguf_files = _collect_model_paths()
    models = [
        {"name": path.name, "path": str(path), "relative_path": relative, "format": "transformers"}
        for _root, path, relative in _walk_model_files(directories)
    ]
    mmproj_index = {str(Path(item["path"]).absolute()): item["relative_path"] for item in _list_mmproj_models()}
    for _root, path, relative in _walk_model_files(gguf_files):
        paired = []
        for candidate in _paired_mmproj(path):
            key = str(candidate.absolute())
            if key in mmproj_index:
                paired.append(mmproj_index[key])
        models.append({
            "name": path.name,
            "path": str(path),
            "relative_path": relative,
            "format": "gguf",
            "mmproj_candidates": paired,
        })
    return models


def _list_visual_models() -> list[dict]:
    roots = _llm_root_list()
    return _cached_scan("visual", roots, _compute_visual_models)


def _compute_mmproj_models() -> list[dict]:
    found: list[tuple[Path, Path]] = []
    for root in _llm_root_list():
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*.gguf"), key=lambda item: str(item).lower()):
            if _looks_like_mmproj(candidate):
                found.append((root, candidate))
    return [
        {"name": path.name, "path": str(path), "relative_path": relative}
        for _root, path, relative in _walk_model_files(found)
    ]


def _list_mmproj_models() -> list[dict]:
    roots = _llm_root_list()
    return _cached_scan("mmproj", roots, _compute_mmproj_models)


def _locate_visual_model(selected: str) -> dict | None:
    wanted = str(selected or "")
    for item in _list_visual_models():
        if item["relative_path"] == wanted:
            return item
    return None


def _as_mapping(value) -> dict:
    """只接受 dict；None/列表/字符串一律当作空配置。"""
    return value if isinstance(value, dict) else {}


def _clamp_tokens(value, fallback: int = 4096) -> int:
    """max_tokens 解析 + 夹紧到 [512, 8192]。"""
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = fallback
    return max(512, min(8192, requested))


def _merge_settings(data: dict | None) -> dict:
    """把面板/工作流传来的配置归一成完整设置（缺项用 BASE_SETTINGS 补齐）。"""
    current = BASE_SETTINGS
    raw = _as_mapping(data)
    provider = str(raw.get("provider") or current["provider"]).lower()
    preset = SERVICE_ENDPOINTS.get(provider)
    api_keys = _as_mapping(raw.get("api_keys"))
    # RunningHub 国内版与海外版是两个账号、两把 Key。优先用当前 provider 名下保存的
    # Key，避免切换平台或恢复工作流后把旧 Key 发到另一个区域；同时保留旧的通用
    # api_key 字段作为老存档的回退。
    scoped_key = api_keys.get(provider)
    if scoped_key is not None:
        api_key = scoped_key
    elif "api_key" in raw:
        api_key = raw.get("api_key")
    else:
        api_key = current["api_key"]
    provider_models = _as_mapping(raw.get("provider_models"))
    mode = str(raw.get("mode") or current.get("mode") or "api").lower()
    language = str(raw.get("output_language") or current.get("output_language") or "中文").lower()
    device = str(raw.get("local_device") or current.get("local_device") or "cuda").lower()
    tokens = _clamp_tokens(raw.get("max_tokens", current.get("max_tokens", 4096)))
    # 未知 provider（如 custom）且未显式给端点/模型时保持为空，让 _build_polish_request
    # 的守卫报「请先配置提示词优化 API」——不能回退到 RunningHub 预设，否则其他
    # 平台的 Key 会发往 RunningHub。
    return {
        "mode": "local" if mode == "local" else "api",
        "provider": provider,
        "api_url": str(raw.get("api_url") or (preset[0] if preset else "")).strip(),
        "api_key": str(api_key or ""),
        "api_keys": {str(key): str(value or "") for key, value in api_keys.items()},
        "model": str(provider_models.get(provider) or raw.get("model") or (preset[1] if preset else "")).strip(),
        "provider_models": {str(key): str(value or "") for key, value in provider_models.items()},
        # 平台预设优先于请求体里的 protocol：前端不必知道协议细节，也不必与后端
        # 预设保持一致（审查 L6）。无预设（custom/未知 provider）时才用请求值，
        # 且不再继承 RunningHub 默认协议——否则自定义端点会被误路由到 RunningHub。
        "protocol": str((preset[2] if preset else None) or raw.get("protocol") or "openai").lower(),
        "read_media": bool(raw.get("read_media", current["read_media"])),
        "output_language": "中文" if language in {"中文", "chinese", "zh"} else "English",
        "local_model": str(raw.get("local_model") or current.get("local_model") or "").strip(),
        "local_mmproj": str(raw.get("local_mmproj") or current.get("local_mmproj") or "").strip(),
        "local_device": device,
        "max_tokens": tokens,
        "auto_optimize": bool(raw.get("auto_optimize", current.get("auto_optimize", False))),
    }


def _client_settings(config: dict) -> dict:
    """发往前端的配置副本：抹掉明文 Key，只留「是否已配置」与模型清单。"""
    public = dict(config)
    public.update({
        "api_key": "",
        "has_api_key": bool(config.get("api_key")),
        "runninghub_models": list(_RH_MODEL_CACHE),
        "runninghub_overseas_models": list(_RH_OVERSEAS_MODEL_CACHE),
    })
    return public


_NUXT_PAYLOAD = re.compile(
    r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>([\s\S]*?)</script>',
    re.IGNORECASE,
)
# 真正的模型下拉项至少要有这么多个选项、且其中这么多项带 "/"（发布者前缀）。
_RH_MODEL_MIN_CHOICES = 5
_RH_MODEL_MIN_SLASHED = 3


def _rh_models_from_html(html: str) -> tuple[str, ...]:
    """从 RunningHub 公开详情页的 Nuxt 载荷里抽出节点 1 的模型下拉项。"""
    match = _NUXT_PAYLOAD.search(html)
    if match is None:
        return ()
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, list):
        return ()
    candidates = []
    for value in payload:
        if not isinstance(value, str) or not value.startswith("[["):
            continue
        try:
            field_data = json.loads(value)
        except (TypeError, ValueError):
            continue
        if not isinstance(field_data, list) or not field_data or not isinstance(field_data[0], list):
            continue
        models = [str(item).strip() for item in field_data[0] if isinstance(item, str) and str(item).strip()]
        if len(models) < _RH_MODEL_MIN_CHOICES:
            continue
        if sum("/" in item for item in models) < _RH_MODEL_MIN_SLASHED:
            continue
        candidates.append(models)
    if not candidates:
        return ()
    return tuple(dict.fromkeys(max(candidates, key=len)))


async def _reload_rh_models(provider: str = "runninghub") -> tuple[str, ...]:
    """联网重新读取 RunningHub 应用详情页里的模型清单，并写回缓存。"""
    global _RH_MODEL_CACHE, _RH_OVERSEAS_MODEL_CACHE
    overseas = provider == "runninghub_overseas"
    detail_url = RH_OVERSEAS_DETAIL_URL if overseas else RH_DETAIL_URL
    timeout = aiohttp.ClientTimeout(total=15)
    async with _RH_MODEL_MUTEX:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(detail_url) as response:
                if response.status != 200:
                    raise RuntimeError(f"RunningHub 模型清单读取失败（HTTP {response.status}）")
                page = await response.text()
    models = _rh_models_from_html(page)
    if not models:
        raise RuntimeError("RunningHub 应用详情页里没有找到模型清单")
    if overseas:
        _RH_OVERSEAS_MODEL_CACHE = models
    else:
        _RH_MODEL_CACHE = models
    return models


def _escapes_input_root(value: str) -> bool:
    """文件名里是否含穿越/绝对路径/家目录等越界写法。"""
    if os.path.isabs(value) or value.startswith("~"):
        return True
    return ".." in value.replace("\\", "/").split("/")


def _input_candidates(value: str, input_root: str):
    """按优先级给出候选路径：ComfyUI 标注路径，然后 input 目录内拼接。"""
    annotated = folder_paths.get_annotated_filepath(value)
    if annotated:
        yield Path(annotated)
    yield Path(input_root) / value


def _confine_input_path(name: str) -> Path:
    value = str(name or "").strip()
    if not value:
        raise ValueError("参考素材文件名为空")
    # 路径穿越防护：工作流 JSON 可被人为构造，媒体文件名只允许指向 input
    # 目录内部（拒绝 .. 序列与绝对路径），防止恶意工作流把本机任意文件经
    # ffmpeg 解码后上传外传。
    if _escapes_input_root(value):
        raise ValueError(f"参考素材文件名不合法: {value}")
    input_root = os.path.realpath(folder_paths.get_input_directory())
    for candidate in _input_candidates(value, input_root):
        if not candidate.is_file():
            continue
        resolved = os.path.realpath(candidate)
        if os.path.commonpath([resolved, input_root]) != input_root:
            raise ValueError(f"参考素材路径越界: {value}")
        return Path(resolved)
    raise FileNotFoundError(f"找不到参考素材: {value}")


_FFMPEG_REQUIRED_HINT = "RunningHub 视频预处理需要 FFmpeg，但当前 ComfyUI 环境未找到 FFmpeg"


def _ffmpeg_binary() -> str:
    """优先用 PATH 里的 ffmpeg，其次用 imageio-ffmpeg 自带的二进制。"""
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as error:
        raise RuntimeError(_FFMPEG_REQUIRED_HINT) from error
    return bundled


# fps=1 每秒源视频取一帧，不改变播放速度；音频在同一点截断并按原速保留。
_RH_PREVIEW_FILTER = (
    "fps=1:start_time=0:round=down,"
    "scale=512:512:force_original_aspect_ratio=decrease:force_divisible_by=2"
)
_RH_FFMPEG_TIMEOUT = 120


def _rh_preview_command(ffmpeg: str, source: Path, limit: float, output: Path) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-t", f"{limit:.3f}",
        "-vf", _RH_PREVIEW_FILTER,
        "-r", "1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", str(output),
    ]


async def _rh_prepare_video(source_name: str, duration: float, output_path: Path) -> None:
    source = _confine_input_path(source_name)
    limit = max(0.1, float(duration or 5))
    command = _rh_preview_command(_ffmpeg_binary(), source, limit, output_path)
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        # 超时保护：取消/异常时 kill 子进程，避免 ffmpeg 孤儿进程占死 worker。
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=_RH_FFMPEG_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("RunningHub 参考视频预处理超时（120 秒），已终止 FFmpeg")
    if process.returncode:
        tail = stderr.decode("utf-8", "replace")[-1000:]
        raise RuntimeError("RunningHub 参考视频预处理失败: " + tail)


def _media_form_field(form: aiohttp.FormData, *, path: Path | None, data_url: str | None,
                      filename: str) -> None:
    """把本地文件或 data URL 写成一个 multipart 字段。"""
    if path is not None:
        form.add_field("file", path.read_bytes(), filename=path.name, content_type="video/mp4")
        return
    try:
        header, encoded = str(data_url or "").split(",", 1)
        mime = header.split(";", 1)[0].split(":", 1)[1]
        payload = base64.b64decode(encoded)
    except Exception as error:
        raise ValueError("RunningHub 参考图片数据无效") from error
    form.add_field("file", payload, filename=filename, content_type=mime)


async def _rh_upload(session: aiohttp.ClientSession, api_key: str, *, path: Path | None = None,
                     data_url: str | None = None, filename: str = "reference.jpg",
                     api_base: str = "https://www.runninghub.cn/openapi/v2") -> str:
    form = aiohttp.FormData()
    _media_form_field(form, path=path, data_url=data_url, filename=filename)
    async with session.post(
        f"{api_base}/media/upload/binary",
        headers={"Authorization": f"Bearer {api_key}"}, data=form,
    ) as response:
        body = await response.text()
        if response.status >= 400:
            raise _logged_upstream_error(f"RunningHub 素材上传失败（HTTP {response.status}）", body)
        data = json.loads(body)
    if data.get("code") not in (0, "0", None):
        detail = data.get("message") or data.get("msg") or data
        raise _logged_upstream_error(f"RunningHub 素材上传失败：{_upstream_label(detail)}", data)
    result = data.get("data") or {}
    value = result.get("fileName") or result.get("download_url")
    if not value:
        raise RuntimeError("RunningHub 素材上传响应缺少 fileName")
    return str(value)


async def _rh_abort(api_key: str, task_id: str, host: str = "https://www.runninghub.cn") -> None:
    if not api_key or not task_id:
        return
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{host}/task/openapi/cancel",
            headers=headers, json={"apiKey": api_key, "taskId": task_id},
        ) as response:
            await response.read()


def _inline_text_result(results: list) -> str | None:
    """有些任务把文本直接放在 results[].text 里，优先取它。"""
    for item in results:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            return str(item["text"]).strip()
    return None


def _txt_download_url(item: dict) -> str | None:
    """只把 outputType/fileType 为 txt 的项当作文本结果。"""
    kind = str(item.get("outputType") or item.get("fileType") or "").lower().lstrip(".")
    if kind not in {"txt", "text"}:
        return None
    url = item.get("url") or item.get("fileUrl")
    return str(url) if url else None


def _decode_text_body(body: bytes) -> str | None:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return body.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return None


async def _rh_result_text(session: aiohttp.ClientSession, results: list) -> str:
    """RunningHub 结果页：先找内联文本，再下载 TXT 文件。"""
    inline = _inline_text_result(results)
    if inline is not None:
        return inline
    for item in results:
        if not isinstance(item, dict):
            continue
        url = _txt_download_url(item)
        if url is None:
            continue
        async with session.get(url) as response:
            body = await response.read()
            if response.status >= 400:
                raise RuntimeError(f"RunningHub TXT 结果下载失败 ({response.status})")
        decoded = _decode_text_body(body)
        if decoded is not None:
            return decoded
    raise RuntimeError("RunningHub 返回结果中没有文本或 TXT 文件")


_LOCAL_MODEL_MISSING = "所选本地模型不存在，或不是可识别的视觉模型"


def _chosen_local_model(config: dict) -> dict:
    """按配置里的 relative_path 找到本地视觉模型条目。"""
    selected = str(config.get("local_model") or "").strip()
    found = _locate_visual_model(selected)
    if found is None:
        raise ValueError(_LOCAL_MODEL_MISSING)
    return found


def _local_model_file(config: dict) -> Path:
    if not str(config.get("local_model") or "").strip():
        raise ValueError("请先选择本地视觉模型")
    return Path(_chosen_local_model(config)["path"])


def _mmproj_index() -> dict[str, dict]:
    return {item["relative_path"]: item for item in _list_mmproj_models()}


def _resolve_mmproj_entry(relative_path: str) -> dict:
    entry = _mmproj_index().get(relative_path)
    if entry is None:
        raise ValueError("已选择的mmproj视觉模型不存在，请重新选择")
    if not Path(entry["path"]).is_file():
        raise ValueError("已选择的mmproj文件不存在")
    return entry


def _chosen_mmproj(config: dict, model: dict) -> Path:
    """显式选择优先；否则用主模型自动匹配到的候选（唯一时才自动选定）。"""
    selected = str(config.get("local_mmproj") or "").strip()
    if selected:
        return Path(_resolve_mmproj_entry(selected)["path"])
    candidates = list(model.get("mmproj_candidates") or [])
    if not candidates:
        raise ValueError("未找到与当前GGUF模型匹配的mmproj视觉模型")
    if len(candidates) > 1:
        raise ValueError("检测到多个匹配的mmproj视觉模型，请在配置页面选择一个版本")
    picked = _mmproj_index().get(candidates[0])
    if picked is None or not Path(picked["path"]).is_file():
        raise ValueError("自动匹配的mmproj视觉模型不存在，请重新选择")
    return Path(picked["path"])


_CUDA_VERSION = re.compile(r"(\d+)\.(\d+)")


def _cuda_label() -> str | None:
    """把 torch 的 CUDA 版本号转成 wheel 命名里的 cuXXX 标签。"""
    try:
        import torch
        raw = str(torch.version.cuda or "")
    except Exception:
        raw = ""
    match = _CUDA_VERSION.match(raw)
    if match is None:
        return None
    return f"cu{match.group(1)}{match.group(2)}"


_GGUF_DEPENDENCY_STATUS_CACHE: dict | None = None


def _gguf_deps_state() -> dict:
    """依赖状态只探测一次：llama_cpp 可用性与平台信息进程内不变，
    GitHub 探测结果一并缓存（匿名 API 60 次/小时限流，审查 M11）。"""
    global _GGUF_DEPENDENCY_STATUS_CACHE
    if _GGUF_DEPENDENCY_STATUS_CACHE is None:
        _GGUF_DEPENDENCY_STATUS_CACHE = dict(_compute_gguf_dependency_status())
    return dict(_GGUF_DEPENDENCY_STATUS_CACHE)


_GGUF_RELEASE_PAGE = "https://github.com/JamePeng/llama-cpp-python/releases"
_GGUF_RELEASE_API = "https://api.github.com/repos/JamePeng/llama-cpp-python/releases?per_page=30"
_GGUF_PROBE_TIMEOUT = 8


def _wheel_platform_tag(system: str, machine: str) -> str | None:
    """x86_64 才有预编译 wheel：Windows 用 win_amd64，Linux 用 linux_x86_64。"""
    if machine not in {"amd64", "x86_64"}:
        return None
    if system == "windows":
        return "win_amd64"
    if system == "linux":
        return "linux_x86_64"
    return None


def _probe_gguf_wheel(status: dict, cuda_tag: str, platform_tag: str, python_tag: str) -> None:
    """在线探测 JamePeng release 里与当前 CUDA/Python/平台匹配的 wheel。"""
    request = urllib.request.Request(
        _GGUF_RELEASE_API, headers={"User-Agent": "MiniMaxH3-Integration-RH"},
    )
    with urllib.request.urlopen(request, timeout=_GGUF_PROBE_TIMEOUT) as response:
        releases = json.load(response)
    prefix = "win" if str(status["system"]).lower() == "windows" else "linux"
    marker = f"-{cuda_tag}-{prefix}-"
    for release in releases:
        if marker not in str(release.get("tag_name") or ""):
            continue
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            if python_tag not in name or platform_tag not in name:
                continue
            status["wheel_name"] = name
            status["wheel_url"] = str(asset.get("browser_download_url") or "")
            status["release_url"] = str(release.get("html_url") or status["release_url"])
            return


def _compute_gguf_dependency_status() -> dict:
    """判断 llama-cpp-python（含 Qwen3.5 chat handler）是否可用，并找对应 wheel。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    python_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    cuda_tag = _cuda_label() or ""
    platform_tag = _wheel_platform_tag(system, machine)
    status = {
        "available": False,
        "reason": "",
        "system": platform.system(),
        "machine": platform.machine(),
        "python_tag": python_tag,
        "cuda_tag": cuda_tag,
        "release_url": _GGUF_RELEASE_PAGE,
        "wheel_url": "",
        "wheel_name": "",
    }
    try:
        import llama_cpp
        from llama_cpp.llama_chat_format import Qwen35ChatHandler  # noqa: F401
    except Exception as error:
        status["reason"] = str(error)
    else:
        status["available"] = True
        status["version"] = str(getattr(llama_cpp, "__version__", "unknown"))
        return status
    if system == "darwin":
        status["release_url"] = _GGUF_RELEASE_PAGE
        return status
    if not cuda_tag or not platform_tag:
        return status
    # JamePeng 的 release 资产名遵循稳定的 版本/CUDA/Python/平台 命名规则。
    try:
        _probe_gguf_wheel(status, cuda_tag, platform_tag, python_tag)
    except Exception:
        pass
    return status


def _empty_cuda_cache() -> None:
    """尽力归还显存；无 torch / 无 CUDA 时静默跳过。"""
    try:
        import torch
    except Exception:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _release_gguf() -> bool:
    """释放已加载的 llama.cpp 实例及其 chat handler，并归还显存。"""
    global _GGUF_INSTANCE, _GGUF_PIPE, _GGUF_SETTINGS
    with _GGUF_MUTEX:
        loaded = any(value is not None for value in (_GGUF_INSTANCE, _GGUF_PIPE))
        for target in (_GGUF_INSTANCE, getattr(_GGUF_PIPE, "_exit_stack", None)):
            if target is None:
                continue
            try:
                target.close()
            except Exception:
                pass
        _GGUF_INSTANCE = _GGUF_PIPE = _GGUF_SETTINGS = None
    gc.collect()
    _empty_cuda_cache()
    return loaded


def _inline_image(data_url: str):
    """把 data URL 解成 PIL RGB 图像（本地 transformers 模型路径用）。"""
    from io import BytesIO
    from PIL import Image

    _meta, encoded = str(data_url).split(",", 1)
    raw = base64.b64decode(encoded)
    return Image.open(BytesIO(raw)).convert("RGB")


def _free_comfy_cache() -> None:
    """本地视觉模型运行前后释放 ComfyUI 托管的模型与缓存显存。"""
    from comfy import model_management

    model_management.unload_all_models()
    gc.collect()
    model_management.soft_empty_cache()


def _run_local_model(config: dict, payload: dict) -> str:
    """跑一次本地视觉模型请求，前后都释放 ComfyUI 的模型占用。"""
    _free_comfy_cache()
    try:
        return _local_model_body(config, payload)
    finally:
        _free_comfy_cache()


def _cancel_probe(payload: dict):
    """取该请求的取消信号（threading.Event），没有则返回 None。"""
    return payload.get("_cancel_event")


def _local_system_prompt(config: dict, payload: dict, user_prompt: str) -> str:
    """本地路径与云端共用同一套系统提示词装配。"""
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    return _system_message(
        str(payload.get("task") or "T2VA"),
        float(payload.get("duration") or 5),
        [str(item.get("label")) for item in media if item.get("label")],
        str(config.get("output_language") or "English"),
        payload.get("context") if isinstance(payload.get("context"), dict) else {},
        user_prompt,
    )


def _local_chat_content(payload: dict, user_prompt: str) -> list[dict]:
    """transformers 多模态分片：图片就地内联，音频只留文字标注。"""
    content: list[dict] = [{"type": "text", "text": "User prompt:\n" + user_prompt}]
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    for item in media:
        label = str(item.get("label") or "")
        if str(item.get("kind") or "") == "audio":
            note = f"{label} is an uploaded audio reference; audio data is not transmitted."
            content.append({"type": "text", "text": note})
            continue
        for data_url in item.get("images") or []:
            content.append({"type": "image", "image": _inline_image(data_url)})
            note = f"This visual belongs to {label}; use the label, never a filename."
            content.append({"type": "text", "text": note})
    return content


def _local_model_body(config: dict, payload: dict) -> str:
    """本次请求内加载本地 transformers 视觉模型，用完即释放。"""
    model_path = _local_model_file(config)
    if model_path.suffix.lower() == ".gguf":
        return _run_gguf(config, payload)
    model = processor = inputs = output = generated = None
    try:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor, StoppingCriteria, StoppingCriteriaList

        cancel_event = _cancel_probe(payload)

        class _CancelledCriteria(StoppingCriteria):
            """取消信号置位时让 generate 尽快停下。"""

            def __call__(self, input_ids, scores, **kwargs):
                return bool(cancel_event and cancel_event.is_set())

        device = config.get("local_device")
        if device not in {"cpu", "cuda"}:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if device == "cuda" else torch.float32
        processor = AutoProcessor.from_pretrained(str(model_path), trust_remote_code=True)
        model = AutoModelForImageTextToText.from_pretrained(
            str(model_path),
            torch_dtype=dtype,
            device_map="auto" if device == "cuda" else None,
            trust_remote_code=True,
        )
        if device == "cpu":
            model.to("cpu")

        user_prompt = str(payload.get("prompt") or "")
        content = _local_chat_content(payload, user_prompt)
        system = _local_system_prompt(config, payload, user_prompt)
        messages = [
            {"role": "system", "content": [{"type": "text", "text": system}]},
            {"role": "user", "content": content},
        ]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        images = [part["image"] for part in content if part.get("type") == "image"]
        inputs = processor(text=[text], images=images or None, padding=True, return_tensors="pt")
        target = next(model.parameters()).device
        inputs = {key: value.to(target) if hasattr(value, "to") else value for key, value in inputs.items()}
        with torch.inference_mode():
            output = model.generate(
                **inputs,
                max_new_tokens=int(config.get("max_tokens") or 4096),
                do_sample=False,
                stopping_criteria=StoppingCriteriaList([_CancelledCriteria()]),
            )
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("提示词优化已取消")
        generated = output[:, inputs["input_ids"].shape[1]:]
        return processor.batch_decode(generated, skip_special_tokens=True)[0].strip()
    finally:
        # 显式断开引用，确保权重对象能立即被 GC 回收。
        model = processor = inputs = output = generated = None
        gc.collect()
        _empty_cuda_cache()


def _gguf_dependency_hint(dependency: dict) -> str:
    """把依赖缺失信息整理成可执行提示（优先给 wheel 直链）。"""
    if dependency.get("wheel_url"):
        tail = f"\n下载地址: {dependency['wheel_url']}"
    else:
        tail = f"\n发布页面: {dependency['release_url']}"
    environment = (
        f"{dependency['system']} {dependency['machine']} / "
        f"{dependency['python_tag']} / {dependency.get('cuda_tag') or '无CUDA'}"
    )
    return f"GGUF视觉模型依赖不可用。当前环境: {environment}。{tail}\n安装后请重启ComfyUI。"


def _gguf_chat_handler(handler_name: str, mmproj_path: Path):
    """按模型族构造带投影器的 llama.cpp chat handler。"""
    from llama_cpp.llama_chat_format import Qwen35ChatHandler, Qwen3VLChatHandler

    handler_cls = Qwen35ChatHandler if handler_name == "qwen35" else Qwen3VLChatHandler
    options = {"clip_model_path": str(mmproj_path), "verbose": False}
    if handler_name == "qwen35":
        options["enable_thinking"] = False
    else:
        options["force_reasoning"] = False
    return handler_cls(**options)


def _ensure_gguf_instance(handler_name: str, model_path: Path, mmproj_path: Path) -> None:
    """复用已加载实例；只有模型或投影器变了才重建。"""
    global _GGUF_INSTANCE, _GGUF_PIPE, _GGUF_SETTINGS
    from llama_cpp import Llama

    wanted = (str(model_path), str(mmproj_path), handler_name)
    with _GGUF_MUTEX:
        if _GGUF_INSTANCE is not None and _GGUF_SETTINGS == wanted:
            return
        _release_gguf()
        _GGUF_PIPE = _gguf_chat_handler(handler_name, mmproj_path)
        _GGUF_INSTANCE = Llama(
            model_path=str(model_path), chat_handler=_GGUF_PIPE,
            n_gpu_layers=-1, n_ctx=16384, verbose=False,
        )
        _GGUF_SETTINGS = wanted


def _gguf_chat_content(payload: dict, user_prompt: str) -> list[dict]:
    """llama.cpp 的 user 内容：先文字，后 image_url 分片（音频只留标注）。"""
    content: list[dict] = [{"type": "text", "text": "User prompt:\n" + user_prompt}]
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    for item in media:
        label = str(item.get("label") or "")
        if str(item.get("kind") or "") == "audio":
            note = f"{label} is an uploaded audio reference; audio data is not transmitted."
            content.append({"type": "text", "text": note})
            continue
        for index, data_url in enumerate(item.get("images") or []):
            content.append({"type": "text", "text": f"{label} visual {index + 1}."})
            content.append({"type": "image_url", "image_url": {"url": data_url}})
    return content


def _run_gguf(config: dict, payload: dict) -> str:
    """走 llama.cpp 跑一次 GGUF 视觉模型请求。"""
    dependency = _gguf_deps_state()
    if not dependency.get("available"):
        raise RuntimeError(_gguf_dependency_hint(dependency))
    model = _chosen_local_model(config)
    model_path = Path(model["path"])
    mmproj_path = _chosen_mmproj(config, model)
    handler_name = _gguf_pipe_name(model_path)
    if handler_name not in {"qwen35", "qwen3vl"}:
        raise ValueError("当前GGUF视觉模型类型暂不支持")
    cancel_event = _cancel_probe(payload)
    try:
        _ensure_gguf_instance(handler_name, model_path, mmproj_path)
        user_prompt = str(payload.get("prompt") or "")
        system = _local_system_prompt(config, payload, user_prompt)
        content = _gguf_chat_content(payload, user_prompt)
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("提示词优化已取消")
        with _GGUF_MUTEX:
            result = _GGUF_INSTANCE.create_chat_completion(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                max_tokens=int(config.get("max_tokens") or 4096),
                temperature=0.2,
                top_p=0.9,
            )
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("提示词优化已取消")
        return str(result["choices"][0]["message"]["content"] or "").removeprefix(": ").strip()
    finally:
        _release_gguf()


_GEMINI_GENERATE = re.compile(r":generateContent(?:\?|$)")


def _chat_endpoint(config: dict) -> str:
    """把 base URL 补成真正的调用端点（三种协议规则不同）。"""
    base = config["api_url"].rstrip("/")
    if config["protocol"] == "gemini":
        if _GEMINI_GENERATE.search(base):
            return base
        model = urllib.parse.quote(config["model"], safe='-_.')
        return f"{base}/models/{model}:generateContent"
    if config["protocol"] == "responses":
        return base if base.endswith("/responses") else f"{base}/responses"
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


_KEYFRAME_ROLE_TEXT = {
    "exact_first_frame": "is the exact first-frame anchor at 0.00s",
    "keyframe": "is a concrete visual keyframe anchor",
}


def _keyframe_role_text(role: object, duration: float) -> str:
    """把节点传来的角色名翻成一句可嵌入提示词的描述。"""
    key = str(role)
    if key == "exact_last_frame":
        return f"is the exact last-frame anchor at {duration:.2f}s"
    return _KEYFRAME_ROLE_TEXT.get(key, "is a visual reference")


def _keyframe_rule(context: dict, duration: float) -> str:
    """把节点给出的关键帧角色写成一条约束（没给就明确禁止编造）。"""
    described = []
    for item in context.get("keyframes") or []:
        if not isinstance(item, dict) or not item.get("label") or not item.get("role"):
            continue
        described.append(f"{item['label']} {_keyframe_role_text(item['role'], duration)}")
    if not described:
        return "No concrete keyframe role was supplied by the node; do not invent one."
    return "Keyframe roles supplied by the node: " + "; ".join(described) + ". Preserve these roles exactly."


def _system_message(
    task: str,
    duration: float,
    labels: list[str],
    output_language: str = "English",
    context: dict | None = None,
    user_prompt: str = "",
) -> str:
    task_upper = task.upper()
    fl2va = task_upper == "FL2VA"
    full_reference = task_upper in {"REF2VA", "HYBRID"}
    context = context if isinstance(context, dict) else {}
    label_rule = (
        "FL2VA picture labels must be bare lowercase: picture 1 and picture 2, never angle brackets. "
        "This is a mandatory content requirement, not merely a label-format example: the final prompt must explicitly "
        "contain both exact tokens 'picture 1' and 'picture 2'. The first line must state that picture 1 is the exact "
        f"opening-frame anchor at 0.00s and picture 2 is the exact ending-frame anchor at {duration:.2f}s. Never omit, "
        "rename, translate, capitalize, or replace either token, even when the requested output language is Chinese."
        if fl2va else
        "All media labels must use angle brackets exactly, for example <picture 1>, <video 1>, <audio 1>."
    )
    if full_reference:
        structure = (
                        'Return exactly these six sections in this order, with each heading followed by a colon: '
            'subject_definitions, summary, retention_analysis, detailed_description, overall_soundscape, '
            'non_diegetic_music. Formatting is strict: every section heading must occupy its own line; put a '
            "newline immediately after the colon, write that section's content on the following line, and put one "
            'blank line between sections. Never place a heading and its content on the same line, and never join '
            'multiple sections into one paragraph. In subject_definitions, define reusable visible content as '
            '<Subject N> and cite its source label using one short sentence and no more than four identifying '
            'visual traits; define standalone <Picture N> only for a concrete frame anchor. In summary, use one '
            'sentence beginning with a square-bracketed combination of only the relationships that apply: '
            'keyframe completion, reference generation, video editing, video continuation, audio reuse, or audio '
            'reference. In retention_analysis, write one compact line per label and use fully_preserved, '
            'partially_preserved, attribute_transfer, or weak_reference for visual content, and fully_copy, '
            'partially_copy, reference, or weak_reference for audio. A relationship marker alone is never a '
            "complete entry. Every line must use the exact form '<label>: marker - explanation' and add a "
            'concise, concrete explanation after the hyphen. For fully_preserved, state which identity, '
            'appearance, scene, composition, or structural properties remain unchanged in the target. For '
            'attribute_transfer, state exactly which appearance, motion, scene, camera, timing, or structural '
            'attributes transfer from the reference and which target receives them. For audio, state whether the '
            'complete signal is copied unchanged, only selected portions or layers are copied, or only timbre, '
            'rhythm, delivery, or other specified attributes are referenced. Use enough information to make each '
            'reference relationship operational, normally one full sentence per label, but do not repeat an '
            'exhaustive appearance inventory already stated in subject_definitions. Never output bare entries '
            "such as '<picture 1>: fully_preserved', '<video 1>: attribute_transfer', or '<audio 1>: fully_copy'. "
            'Make detailed_description action-first, chronological, and production-ready. Give the scene a clear '
            'dramatic progression—establishment, development, a meaningful visual or emotional beat, and a '
            "resolved ending—without changing the user's intended event. Describe subject blocking, observable "
            'state changes, composition, camera movement, and the final visual landing. Use [Shot 1] and timed '
            'later shots only for motivated real cuts that reveal new information; otherwise design a fluid '
            'continuous shot. Normally allow about 350-700 Chinese characters or 220-450 English words for the '
            'complete six-section result, and use more when duration, dialogue, multiple subjects, or genuine '
            'shot complexity requires it.'
        )
    else:
        structure = (
                        'Return exactly these three sections in order: integrated_multimodal_description, overall_soundscape, '
            'non_diegetic_music. Formatting is strict: every section heading must occupy its own line; put a '
            "newline immediately after the colon, write that section's content on the following line, and put one "
            'blank line between sections. Never place a heading and its content on the same line. Build a '
            "complete audiovisual progression rather than merely paraphrasing the user's sentence. Use [Shot 1] "
            'for the opening and timestamps only for motivated real later cuts. For I2VA, anchor the supplied '
            'picture at 0.00s and develop forward. For L2VA, infer a plausible opening and converge exactly to '
            'the supplied picture at the end. For FL2VA, describe one continuous, observable motion path from '
            'picture 1 at 0.00s to picture 2 at the target end time. Give the main description enough detail to '
            'establish the opening composition, action onset, intermediate development, camera evolution, and a '
            'visually resolved ending within the supplied duration. In FL2VA, mention picture 1 again when '
            'establishing the opening state and picture 2 again when describing the final visual landing; both '
            "labels are compulsory and must not be replaced by generic phrases such as 'the first image' or 'the "
            "last image'."
        )

# 关键帧约束由下方模块级的 _keyframe_rule() 生成。

    keyframe_rule = _keyframe_rule(context, duration)

    audio_mode = str(context.get("audio_mode") or "native")
    audio_rule = {
        "lock_source": (
                        "Audio mode is original-audio output. State only that the source video's original audio is preserved "
            'unchanged as the complete target soundtrack and mark it fully_copy. Because the optimizer does not '
            'receive or hear the audio binary, do not identify, describe, classify, or invent any dialogue, '
            'music, ambience, sound effect, instrument, language, rhythm, or other audible content. In '
            "overall_soundscape, write only the selected-language equivalent of 'Preserve the source video's "
            "original audio unchanged.' Do not add any new sound, voice, animal vocalization, ambience, or music."
        ),
        "reference_only": (
            "Audio mode is reference-only: treat supplied audio as audio reference; do not claim its waveform "
            "is copied into the final soundtrack."
        ),
        "remix_source": (
            "Audio mode is source-audio remix: describe only partial copying or remixing of the supplied audio, "
            "never a 1:1 complete copy."
        ),
        "native": "Audio mode is automatic generation: do not claim any uploaded audio is copied into the final output.",
    }.get(audio_mode, "Describe the audio relationship conservatively and do not invent how it is used.")

    singing_rule = ""
    if re.search(
        r"唱|歌曲|歌词|歌唱|演唱|口型|嘴型|对口型|lip\s*sync|sing(?:ing|s)?|lyrics?|vocal",
        user_prompt,
        flags=re.IGNORECASE,
    ):
        singing_rule = (
                        ' The request involves singing or lip synchronization. Require accurate phoneme-level lip and jaw '
            'synchronization throughout the vocals: mouth movement should follow syllable onset and release, '
            'clear consonant closures, natural vowel shapes, pauses and breaths, and sustained notes; the mouth '
            'rests naturally during non-vocal spans. Also require stable facial identity and continuous mouth '
            'motion without generic loops, visible delay, frozen lips, or facial deformation. Do not reduce a '
            'singing performance to lip movement while the rest of the body remains still. Unless the user '
            'explicitly requests restrained or static performance, design evolving full-body or upper-body '
            'performance across the timeline: shifting weight, torso and shoulder rhythm, head movement, gaze '
            'changes, expressive but natural hand and arm phrasing, and transitions between distinct gestures. '
            'Make these movements non-repetitive and responsive to musical phrases, accents, pauses, intensity '
            'changes, and vocal emotion without inventing exact unheard beats. Coordinate purposeful camera '
            'motion and changing shot emphasis with the performance so the image does not feel like a static '
            'portrait with moving lips. Keep lip-sync requirements compact, but give the visible performance and '
            'camera progression enough concrete detail to guide a lively result. Because the audio binary is not '
            'transmitted to the optimizer, never invent exact lyrics, notes, beats, or timestamps and never claim '
            'to have analyzed or heard the file.'
        )
    transfer_rule = ""
    if re.search(
        r"替换|置换|换成|变成|动作迁移|模仿.*动作|复刻.*动作|跟随.*动作|"
        r"replace|substitute|swap|motion\s*(?:transfer|reference)|imitate.*motion|copy.*motion",
        user_prompt,
        flags=re.IGNORECASE,
    ):
        transfer_rule = (
                        ' This is a subject-substitute or motion-transfer request. The following is a strict task-specific '
            'exception that overrides the general instructions to describe actions, timing, performance, camera, '
            'sound, and shot-by-shot events. Describe only the source-to-target relationship: which subject from '
            "<picture N> replaces which subject in <video N>, while the source video's complete motion, timing, "
            'scene, props, composition, camera movement, cuts, and temporal structure remain unchanged. Never '
            'name, infer, enumerate, paraphrase, or timestamp any concrete source-video action, gesture, pose '
            'change, facial expression, dialogue, object interaction, prop function, story event, sound, '
            'ambience, or music, even if it appears visible or inferable in the sampled frames. Use the sampled '
            'frames only to distinguish the subjects that must be replaced. Define substitute subjects only with '
            'official numbered labels such as <Subject 1> and <Subject 2>, never semantic labels such as <cat> or '
            'a Chinese name. Give each subject only the minimum clearly visible identity cues needed to avoid '
            'mismatch: subject type or gender, approximate age when reliable, main clothing and color, hairstyle '
            'or fur pattern, hat, and glasses. Do not infer an occupation or describe pose, expression, action, '
            'scene details, lighting, or unrelated appearance. Do not define the original subjects being removed '
            'as target <Subject N> entries unless needed only to make an unambiguous one-to-one substitute '
            'mapping. In detailed_description, keep a single [Shot 1] for the source timeline, but write a '
            'complete operational paragraph rather than a bare one-sentence relationship. State which source '
            'subject or scene is replaced by which referenced target, which target identity and major appearance '
            'or environment attributes remain stable, and which source-video properties remain unchanged: motion '
            'performance, facial-expression timing, blocking, screen direction, camera trajectory, framing, cuts, '
            'pacing, and duration when applicable. Explain how replaced subjects remain spatially integrated with '
            "preserved props and scene elements, but never invent or enumerate the source video's concrete "
            'actions, dialogue, props, events, or sounds; do not expand the source timeline or reconstruct it '
            'from sampled frames. Treat the three sampled images from one <video N> as observation frames of that '
            'single reference video, never as separate target shots or evidence of cuts.'
        )
    language_rule = (
        "输出语言强制为中文：所有自然语言说明、主体描述、摘要、镜头描述、声音说明和音乐说明必须使用简体中文，"
        "不得输出英文句子。只有官方固定字段名 subject_definitions、summary、retention_analysis、"
        "detailed_description、integrated_multimodal_description、overall_soundscape、non_diegetic_music，"
        "媒体与主体标签、[Shot N]，以及 fully_preserved、partially_preserved、attribute_transfer、"
        "weak_reference、fully_copy、partially_copy、reference 等固定关系标记保留英文。返回前必须检查并将"
        "其他英文自然语言全部改写成中文。"
        if output_language == "中文" else
        "Output all natural-language content in English. Keep supplied dialogue, lyrics, and visible text in their "
        "original language."
    )
    dialogue_rule = (
                'Official dialogue formatting is mandatory in every task and is independent of the selected narration '
        'language. Put every user-supplied spoken line or lyric inside exactly one <d>[Language]...</d> '
        "block, using the dialogue's actual language name in English. In particular, Chinese speech must use "
        'the exact form <d>[Chinese]中文原文</d>; never write Chinese dialogue only inside quotation marks, and '
        'never omit [Chinese]. Keep the speaker description, stable speaker ID such as (S1), speaking action, '
        "and delivery outside the <d> block; inside it preserve only the user's exact words and punctuation "
        'without translation or rewriting. The output-language setting controls narration only: an English '
        'prompt must still retain supplied Chinese dialogue as <d>[Chinese]...</d>. Placement is as mandatory '
        'as formatting: every <d> block must be embedded inside integrated_multimodal_description or '
        'detailed_description at the exact chronological moment when the speaker speaks or sings, immediately '
        "after that speaker's speaking/singing action and delivery description. Never collect dialogue into a "
        'list, appendix, footer, or separate paragraph after the official sections. Never place a <d> block '
        'in subject_definitions, summary, retention_analysis, overall_soundscape, or non_diegetic_music. The '
        'final section is always non_diegetic_music, and absolutely no text or <d> block may follow that '
        "section's content. For multiple supplied lines, preserve their original order and place each line at "
        'its corresponding point in the shot timeline, with intervening actions and pauses described between '
        "lines when the user's request implies them. Before returning, verify that every supplied line "
        'appears exactly once inside the main shot timeline and nowhere else. Do not wrap paraphrased '
        'soundscape descriptions, ambient sounds, or dialogue that the user did not explicitly supply in <d> '
        'tags.'
    )
    subject_shorthand_rule = (
                'Subject shorthand formatting is mandatory and globally consistent. If a stable short subject ID is '
        'used, it must always be written with parentheses as (S1), (S2), ... through (S20). Never output a '
        'bare S1, S2, or any other unparenthesized S-number in narration, shot descriptions, actions, '
        'dialogue attribution, or sound descriptions. <Subject N> definitions remain unchanged; (S1) is only '
        'the shorthand used to refer back to a defined subject. Before returning, scan the complete result '
        'and replace every standalone bare S1-S20 with its exact parenthesized form.'
    )
    return (
        "You are a professional prompt writer for the open-source MiniMax H3 audiovisual model. Return only the "
        "final production-ready prompt without explanations, markdown fences, filenames, or invented media. "
        "Preserve the user's intent and every supplied dialogue, lyric, and visible-text word verbatim; do not "
        "invent dialogue or lyrics that were not provided. This is a video-generation prompt, not image captioning "
        "or visual reverse-prompting. Reference images are already passed into model conditioning, so identify each "
        "visual subject only with the minimum distinctive cues needed to disambiguate it: usually gender or subject "
        "type, main clothing, main hairstyle, and scene. Do not describe facial features, lighting, pose, background "
        "objects, or composition exhaustively unless they are essential to the requested motion or must change over "
        "time. Spend most words on what happens after the reference frame: causally connected actions, reactions, "
        "story progression, timing, performance, camera behavior, and audio-visual synchronization. Develop the "
        "user's idea into concrete visible beats that fit the duration: establish the situation, let the central "
        "action evolve through meaningful intermediate changes, add a restrained climax or reveal when appropriate, "
        "and finish on a clear visual result rather than an abrupt stop. Do not introduce unrelated characters, props, "
        "locations, conflicts, or plot twists. Avoid repeating the same static trait across sections, avoid decorative "
        "adjectives, and never turn quality requirements into a long negative-prompt checklist. Use professional but "
        "executable cinematography with dynamic video direction by default. Unless the user explicitly requests a static pose, locked-off camera, "
        "minimal movement, or a specific fixed composition, do not let the subject remain nearly motionless after the "
        "opening frame. Build visible motion in successive phases with clear variation and continuity: changes in body "
        "weight, posture, orientation, gesture, interaction, expression, spatial position, or object state. Avoid one "
        "gesture repeated mechanically throughout the clip. For music, singing, dance, performance, fashion, or other "
        "rhythmic scenes, let the body express phrase changes through varied natural gestures and coordinated torso, "
        "shoulder, head, hand, and footwork where framing permits; align movement energy and camera emphasis with broad "
        "musical progression without fabricating unheard exact beats. Treat the reference image as a starting anchor, "
        "not the main subject of the description: spend only enough text to identify it, then prioritize motion design, "
        "performance evolution, kinetic atmosphere, and a visually active ending. For each shot, integrate the useful framing or shot scale, subject blocking, and "
        "camera path into the action. When motion benefits the scene, choose a motivated Push In, Pull Out, Pan, Tilt, "
        "Truck, Pedestal, Arc Shot, Tracking Shot, or controlled Zoom, and specify subtle/large amplitude and slow/fast "
        "speed only when meaningful. Let camera movement reveal information, follow motion, emphasize a transformation, "
        "or land on the final state; do not stack random camera terms or use constant movement without purpose. Vary "
        "composition and visual emphasis over time so the video feels directed rather than static, while preserving "
        "spatial continuity, screen direction, subject identity, and reference constraints. Expand into continuous "
        "actions, camera choreography, synchronized audible events, ambience, and music with enough detail to guide "
        "generation effectively. Prefer a well-designed continuous shot when it can express the event clearly, but "
        "allow a small number of motivated cuts when they materially improve narrative clarity or reveal new visual "
        "information. Do not create labels outside the supplied label list, renumber labels, "
        "or replace labels with filenames. "
        f"Task={task}; duration={duration:.2f}s; available labels={', '.join(labels) or 'none'}. "
        f"{label_rule} {keyframe_rule} {audio_rule} {structure}{singing_rule}{transfer_rule} {dialogue_rule} "
        f"{subject_shorthand_rule} "
        "Audio items are label-only references; never claim you listened to them. Before returning, silently check "
        "that every required section is present in the correct order, every used label exists, no filename appears, "
        "no audio content was fabricated, all timing fits the target duration, and the audio relationship matches "
        f"the supplied mode. {language_rule}"
    )


def _user_message(prompt: str, media: list[dict], read_media: bool) -> list[dict]:
    """拼 OpenAI 兼容口径的 user 内容分片。"""
    parts: list[dict] = [{"type": "text", "text": "User prompt:\n" + prompt}]
    for item in media:
        label = str(item.get("label") or "")
        kind = str(item.get("kind") or "")
        if kind == "audio":
            parts.append({"type": "text", "text": f"{label} is an uploaded audio reference (content not transmitted)."})
            continue
        if not read_media:
            parts.append({"type": "text", "text": f"{label} is an uploaded {kind} reference."})
            continue
        for index, data_url in enumerate(item.get("images") or []):
            parts.append({"type": "text", "text": f"{label} visual {index + 1}."})
            parts.append({"type": "image_url", "image_url": {"url": data_url, "detail": "low"}})
    return parts


# 无视觉能力的平台（与 SERVICE_ENDPOINTS 同源维护）。
_TEXT_ONLY_PROVIDERS = {"deepseek"}
# 模型名标记：出现即认为用户显式选用了视觉模型（如 glm-5v-turbo、deepseek-vl2）。
_VISUAL_MODEL_MARKERS = ("vl", "vision", "omni")


# 官方固定字段名：渲染时强制每个标题独占一行。
_PROMPT_SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)
_SECTION_HEADING = re.compile(
    r"\s*(" + "|".join(map(re.escape, _PROMPT_SECTIONS)) + r")\s*:\s*",
    re.IGNORECASE,
)
# 媒体文件名（含图片/视频/音频扩展名）。
_FILENAME_PATTERN = re.compile(
    r"(?<![\w/\\])[\w .()\-\u4e00-\u9fff]+\."
    r"(?:png|jpe?g|webp|bmp|gif|mp4|mov|webm|mkv|avi|mp3|wav|flac|m4a|ogg|aac)(?!\w)",
    re.IGNORECASE,
)
# 裸写的 S1-S20 简写（前面不能是字母/数字/括号，后面不能接字母/数字）。
_BARE_SUBJECT = re.compile(
    r"(?<![A-Za-z0-9_(<（])S([1-9]|1\d|20)(?![A-Za-z0-9_)>）])",
    re.IGNORECASE,
)


def _has_image_parts(parts: list[dict]) -> bool:
    return any(part.get("type") == "image_url" for part in parts)


def _model_declares_vision(model: object) -> bool:
    """模型名里出现 vl/vision/omni 即视作使用者已显式选了视觉模型。"""
    lowered = str(model or "").lower()
    return any(marker in lowered for marker in _VISUAL_MODEL_MARKERS)


def _reject_images_without_vision(config: dict, parts: list[dict]) -> None:
    """纯文本平台收到图像分片时提前报错，给出可执行指引。

    勾选「读取视觉素材」时图片会以 image_url 分片发出，无视觉能力的平台只能以
    400 拒绝，且上游报错完全看不出是配置问题（2026-09-26 审查）。
    """
    if not _has_image_parts(parts):
        return
    provider = str(config.get("provider") or "").lower()
    if provider not in _TEXT_ONLY_PROVIDERS:
        return
    if _model_declares_vision(config.get("model")):
        return
    raise RuntimeError(
        f"当前平台（{provider}）不支持图像理解：请在 ⚙ 配置中关闭「读取视觉素材」后重试，"
        "或改填支持视觉的模型"
    )


def _drop_filenames(text: str) -> str:
    return _FILENAME_PATTERN.sub("", text).strip()


def _render_prompt_sections(text: str) -> str:
    """把官方固定字段名单独成行，不动正文内容。"""
    formatted = _SECTION_HEADING.sub(
        lambda match: f"\n\n{match.group(1).lower()}:\n", str(text)
    )
    return formatted.strip()


def _expand_subject_short(text: str) -> str:
    """把裸写的 S1-S20 补成官方括号形式。"""
    return _BARE_SUBJECT.sub(lambda match: f"(S{match.group(1)})", str(text))


# 说话动词（中英）：只有它后面跟着引号或冒号时才算「用户明确给的对白」。
_SPEECH_MARKER = r"(?:说|说道|说着|喊|喊道|问|问道|回答|答道|台词|对白|says?|speaks?|shouts?|asks?|replies?)"
_DIALOGUE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"{_SPEECH_MARKER}[^\n“”‘’\"']{{0,20}}[：:]?\s*[“\"]([^”\"\n]+)[”\"]",
        rf"{_SPEECH_MARKER}[^\n“”‘’\"']{{0,20}}[：:]?\s*[‘']([^’'\n]+)[’']",
        rf"{_SPEECH_MARKER}\s*[：:]\s*([^\n；;]+)",
    )
)
_HAN = re.compile(r"[\u3400-\u9fff]")


def _collect_dialogues(prompt: str) -> list[tuple[str, str]]:
    """只收集用户明确给出的对白，并保留其精确文字。"""
    source = str(prompt or "")
    spoken = []
    for pattern in _DIALOGUE_PATTERNS:
        for match in pattern.finditer(source):
            value = match.group(1).strip().strip("“”‘’\"'").strip()
            if not value:
                continue
            known = {item[0] for item in spoken}
            if value in known or any(value in item or item in value for item in known):
                continue
            language = "Chinese" if _HAN.search(value) else "English"
            spoken.append((value, language))
    return spoken


_MAIN_HEADING = re.compile(
    r"(?:integrated_multimodal_description|detailed_description)\s*:\s*", re.IGNORECASE
)
_TAIL_HEADING = re.compile(r"(?:overall_soundscape|non_diegetic_music)\s*:\s*", re.IGNORECASE)
_SPEAKING_CUE = re.compile(
    r"(?:开口说话|开始说话|随后说|继续说|说话|演唱|唱着|begins? speaking|starts? speaking|speaks?|says?|sings?)",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[。！？.!?](?:\s|$)")
_SHOT_ONE = re.compile(r"\[Shot 1\]\s*", re.IGNORECASE)
_TRAILING_SPACES = re.compile(r"[ \t]+\n")
_CHINESE_OUTPUTS = {"中文", "chinese", "zh", "zh-cn"}


def _timeline_span(text: str) -> tuple[int, int]:
    """定位主镜头时间轴的起止（两个官方区块之间）。"""
    heading = _MAIN_HEADING.search(text)
    start = heading.end() if heading else 0
    tail = _TAIL_HEADING.search(text[start:])
    end = start + tail.start() if tail else len(text)
    return start, end


def _replace_timeline(text: str, transform) -> str:
    start, end = _timeline_span(text)
    return text[:start] + transform(text[start:end]) + text[end:]


def _wrap_quoted_dialogue(timeline: str, dialogue: str, tag: str) -> str:
    """优先替换带引号的形态，退而直接替换裸文本，只替换第一次出现。"""
    for quoted in (f"“{dialogue}”", f"‘{dialogue}’", f'"{dialogue}"', f"'{dialogue}'"):
        if quoted in timeline:
            return timeline.replace(quoted, tag, 1)
    return timeline.replace(dialogue, tag, 1)


def _dialogue_sentences(entries: list[tuple[int, str]], output_language: str) -> str:
    chinese = str(output_language).lower() in _CHINESE_OUTPUTS
    lines = []
    for order, (index, tag) in enumerate(entries):
        if chinese:
            action = "说" if order == 0 else "随后继续说"
            lines.append(f"画面中的说话者 (S{index}) {action}：{tag}")
        else:
            action = "says" if order == 0 else "then continues"
            lines.append(f"The on-screen speaker (S{index}) {action}: {tag}")
    return " " + " ".join(lines) + " "


def _dialogue_insert_offset(timeline: str) -> int:
    """有说话动作提示时插到该句末尾，否则插到 [Shot 1] 之后。"""
    cue = _SPEAKING_CUE.search(timeline)
    if cue is None:
        shot = _SHOT_ONE.search(timeline)
        return shot.end() if shot else 0
    boundary = _SENTENCE_END.search(timeline[cue.end():])
    return cue.end() + boundary.end() if boundary else cue.end()


def _ensure_dialogue_lines(text: str, user_prompt: str, output_language: str) -> str:
    """保证用户给的对白只出现一次，且落在主镜头时间轴内。"""
    result = str(text)
    pending: list[tuple[int, str]] = []
    for index, (dialogue, language) in enumerate(_collect_dialogues(user_prompt), start=1):
        tag = f"<d>[{language}]{dialogue}</d>"
        start, end = _timeline_span(result)
        timeline = result[start:end]

        if tag in timeline:
            # 时间轴里已经有了：清掉轴外可能出现的重复，保留轴内那一处。
            result = result[:start].replace(tag, "") + timeline + result[end:].replace(tag, "")
            continue

        # 轴外带着合法标签属于结构错误，应当搬进来而不是复制。
        result = result.replace(tag, "")
        start, end = _timeline_span(result)
        timeline = result[start:end]
        if dialogue in timeline:
            result = result[:start] + _wrap_quoted_dialogue(timeline, dialogue, tag) + result[end:]
            continue
        pending.append((index, tag))

    if pending:
        sentence = _dialogue_sentences(pending, output_language)

        def insert_dialogue(timeline: str) -> str:
            offset = _dialogue_insert_offset(timeline)
            return timeline[:offset] + sentence + timeline[offset:]

        result = _replace_timeline(result, insert_dialogue)
    return _TRAILING_SPACES.sub("\n", result).strip()


def _fl2va_alignment(duration: float, output_language: str) -> str:
    if str(output_language).lower() in _CHINESE_OUTPUTS:
        return (
            "参考图像与目标视频对齐关系：picture 1 对齐目标视频的 0.00 秒首帧；"
            f"picture 2 对齐目标视频的 {duration:.2f} 秒尾帧。"
        )
    return (
        "Reference-picture alignment: picture 1 is the target video's exact opening frame at 0.00s; "
        f"picture 2 is its exact ending frame at {duration:.2f}s."
    )


def _ensure_fl2va_labels(text: str, task: str, duration: float, output_language: str) -> str:
    """FL2VA 结果里必须同时出现 picture 1 与 picture 2 两个官方裸标签。"""
    if str(task).upper() != "FL2VA":
        return text
    lowered = str(text).lower()
    if "picture 1" in lowered and "picture 2" in lowered:
        return text
    return f"{_fl2va_alignment(duration, output_language)}\n\n{text}".strip()


def _openai_text(content) -> str:
    """OpenAI 的 content 可能是纯文本，也可能是分片数组。"""
    if isinstance(content, list):
        pieces = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            pieces.append(str(text.get("value") if isinstance(text, dict) else text or ""))
        return "".join(pieces).strip()
    return str(content or "").strip()


def _reasoning_token_count(data: dict):
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    details = usage.get("completion_tokens_details")
    return details.get("reasoning_tokens") if isinstance(details, dict) else None


def _read_openai_reply(data: dict) -> str:
    """OpenAI 兼容口径的回复解析（含「只回了思考内容」的专门报错）。"""
    try:
        choice = data["choices"][0]
        message = choice["message"]
        text = _openai_text(message.get("content"))
        if text:
            return text
        finish_reason = str(choice.get("finish_reason") or "unknown")
        reasoning = str(message.get("reasoning_content") or "").strip()
        reasoning_tokens = _reasoning_token_count(data)
        if reasoning or reasoning_tokens:
            raise RuntimeError(
                "API 请求已完成，但模型仅返回了推理内容，没有最终提示词"
                f"（finish_reason={finish_reason}, reasoning_tokens={reasoning_tokens or 'unknown'}）。"
                "请关闭模型思考模式或提高输出 token 上限。"
            )
        raise RuntimeError(f"API 请求成功但返回内容为空（finish_reason={finish_reason}）")
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("The API returned no optimized prompt")


def _responses_text_parts(data: dict) -> list[str]:
    collected = []
    for output in data.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if not isinstance(content, dict) or content.get("type") not in {"output_text", "text"}:
                continue
            value = content.get("text")
            if isinstance(value, dict):
                value = value.get("value")
            if value:
                collected.append(str(value))
    return collected


def _read_responses_reply(data: dict) -> str:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    collected = _responses_text_parts(data)
    if collected:
        return "".join(collected).strip()
    raise RuntimeError("OpenAI Responses returned no optimized prompt")


def _gemini_parts(parts: list[dict]) -> list[dict]:
    """Gemini 的 parts：文字走 text，图片走 inline_data。"""
    converted = []
    for part in parts:
        if part["type"] == "text":
            converted.append({"text": part["text"]})
            continue
        header, encoded = part["image_url"]["url"].split(",", 1)
        mime = header.split(";")[0].split(":", 1)[1]
        converted.append({"inline_data": {"mime_type": mime, "data": encoded}})
    return converted


def _responses_parts(parts: list[dict]) -> list[dict]:
    """Responses API 的 input 分片。"""
    converted = []
    for part in parts:
        if part["type"] == "text":
            converted.append({"type": "input_text", "text": part["text"]})
            continue
        converted.append({
            "type": "input_image",
            "image_url": part["image_url"]["url"],
            "detail": part["image_url"].get("detail", "low"),
        })
    return converted


# 这些模型在 SiliconFlow 上会先花光输出预算做思考，必须显式关掉。
_SILICONFLOW_THINKING_MODELS = (
    "qwen3", "deepseek", "glm-4.5", "glm-4.6", "glm-4.7", "glm-5", "hunyuan-a13b",
)


def _siliconflow_needs_thinking_off(config: dict) -> bool:
    host = urllib.parse.urlparse(str(config.get("api_url") or "")).hostname or ""
    if host.lower() != "api.siliconflow.cn":
        return False
    model = str(config.get("model") or "").lower()
    return any(marker in model for marker in _SILICONFLOW_THINKING_MODELS)


def _compose_request(config: dict, payload: dict):
    """按协议拼出 (url, headers, body)。"""
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    user_prompt = str(payload.get("prompt") or "")
    system = _local_system_prompt(config, payload, user_prompt)
    parts = _user_message(user_prompt, media, bool(config.get("read_media")))
    _reject_images_without_vision(config, parts)
    max_tokens = _clamp_tokens(config.get("max_tokens") or 4096)
    headers = {"Content-Type": "application/json"}

    if config["protocol"] == "gemini":
        url = _chat_endpoint(config)
        separator = "&" if "?" in url else "?"
        url += separator + "key=" + urllib.parse.quote(config["api_key"])
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": _gemini_parts(parts)}],
            "generationConfig": {"temperature": 0.35, "maxOutputTokens": max_tokens},
        }
        return url, headers, body

    if config["protocol"] == "responses":
        body = {
            "model": config["model"],
            "instructions": system,
            "input": [{"role": "user", "content": _responses_parts(parts)}],
            "temperature": 0.35,
            "max_output_tokens": max_tokens,
        }
    else:
        body = {
            "model": config["model"],
            "temperature": 0.35,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": parts},
            ],
        }
        # SiliconFlow 的思考型模型会把输出预算全花在 reasoning_content 上，返回空
        # 正文却照常计费。润色只要最终答案，所以在该兼容端点上显式关闭思考。
        if _siliconflow_needs_thinking_off(config):
            body["enable_thinking"] = False
    headers["Authorization"] = f"Bearer {config['api_key']}"
    return _chat_endpoint(config), headers, body


async def _upload_rh_media(session, api_key: str, *, media: list, app_nodes: dict, video_node: str,
                           image_nodes: tuple, node_values: dict, duration: float, temp_dir: Path,
                           api_base: str) -> list[str]:
    """把参考素材按槽位上传，返回给模型看的标签映射说明。"""
    notes: list[str] = []
    image_index = 0
    video_uploaded = False
    for item in media:
        kind = str(item.get("kind") or "")
        label = str(item.get("label") or "")
        if kind == "image":
            images = item.get("images") or []
            if image_index < len(image_nodes) and images:
                node_id = image_nodes[image_index]
                node_values[node_id] = await _rh_upload(
                    session, api_key, data_url=images[0],
                    filename=f"reference_{image_index + 1}.jpg", api_base=api_base,
                )
                notes.append(f"{label} corresponds to uploaded image {image_index + 1}.")
                image_index += 1
            else:
                notes.append(f"{label} exists as an image reference label, but its visual file was not transmitted.")
            continue
        if kind == "video" and not item.get("labelOnly"):
            if not video_uploaded and item.get("source_name"):
                processed = temp_dir / "reference_1fps.mp4"
                await _rh_prepare_video(str(item["source_name"]), duration, processed)
                node_values[video_node] = await _rh_upload(
                    session, api_key, path=processed, api_base=api_base
                )
                notes.append(f"{label} corresponds to the uploaded 1 FPS reference video.")
                video_uploaded = True
            else:
                notes.append(f"{label} exists as a video reference label, but its visual file was not transmitted.")
            continue
        if kind == "audio":
            notes.append(f"{label} is an audio reference label; audio data is not separately transmitted.")
    return notes


def _rh_node_info(app_nodes: dict, node_values: dict, video_node: str, image_nodes: tuple,
                  config: dict, model_cache: tuple, system_prompt: str, user_prompt: str) -> list[dict]:
    """拼 RunningHub 应用的 nodeInfoList（视频/图片/模型/两条提示词/max_tokens）。"""
    return [
        {"nodeId": video_node, "fieldName": "file", "fieldValue": node_values[video_node]},
        *[
            {"nodeId": node_id, "fieldName": "image", "fieldValue": node_values[node_id]}
            for node_id in image_nodes
        ],
        {"nodeId": str(app_nodes["model"]), "fieldName": "model",
         "fieldValue": str(config.get("model") or model_cache[0])},
        {"nodeId": str(app_nodes["system_prompt"]), "fieldName": "Text", "fieldValue": system_prompt},
        {"nodeId": str(app_nodes["user_prompt"]), "fieldName": "Text", "fieldValue": user_prompt},
        {"nodeId": str(app_nodes["max_tokens"]), "fieldName": "value",
         "fieldValue": str(int(config.get("max_tokens") or 4096))},
    ]


# 国内版 / 海外版：版本文案、域名与 appId 一一对应。
_RH_EDITIONS = {
    "runninghub": ("国内版", "https://www.runninghub.cn", RH_APP_ID),
    "runninghub_overseas": ("海外版", "https://www.runninghub.ai", RH_OVERSEAS_APP_ID),
}


async def _rh_start_task(session, api_key: str, api_base: str, app_id: str,
                         node_info: list[dict]) -> tuple[str, dict, dict, str]:
    """启动 RunningHub 应用；返回 (taskId, data, 原始解析结果, 原始响应体)。"""
    async with session.post(
        f"{api_base}/run/ai-app/{app_id}",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"nodeInfoList": node_info, "instanceType": "default", "usePersonalQueue": False},
    ) as response:
        body = await response.text()
        if response.status >= 400:
            raise _logged_upstream_error(f"RunningHub 应用启动失败（HTTP {response.status}）", body)
        started = json.loads(body)
    if started.get("code") not in (0, "0", None):
        raise _logged_upstream_error(
            f"RunningHub 应用启动失败：{_upstream_label(started.get('msg') or started)}", started
        )
    start_data = started.get("data") if isinstance(started.get("data"), dict) else started
    return str(start_data.get("taskId") or ""), start_data, started, body


def _rh_start_failure(edition: str, start_data: dict, started: dict, raw_body: str) -> RuntimeError:
    """启动响应里没有 taskId 时，区分「上游给了错误码」与「Key 用错了版本」。"""
    error_code = str(start_data.get("errorCode") or started.get("errorCode") or "").strip()
    error_message = str(start_data.get("errorMessage") or started.get("errorMessage") or "").strip()
    if error_code or error_message:
        details = ": ".join(part for part in (error_code, error_message) if part)
        return _logged_upstream_error(f"RunningHub {edition}应用启动失败：{_upstream_label(details)}", details)
    return _logged_upstream_error(
        f"RunningHub {edition}应用启动响应缺少 taskId；请确认使用的是{edition}专用 API Key。",
        raw_body,
    )


async def _rh_poll_until_done(session, api_key: str, api_base: str, task_id: str, cancel_event):
    """轮询云端任务直到 SUCCESS；失败/取消时抛错。"""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        await asyncio.sleep(2)
        async with session.post(
            f"{api_base}/query",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"taskId": task_id},
        ) as response:
            body = await response.text()
            if response.status >= 400:
                raise _logged_upstream_error(f"RunningHub 任务查询失败（HTTP {response.status}）", body)
            queried = json.loads(body)
        if queried.get("code") not in (0, "0", None):
            raise _logged_upstream_error(
                f"RunningHub 任务查询失败：{_upstream_label(queried.get('msg') or queried)}", queried
            )
        query_data = queried.get("data") if isinstance(queried.get("data"), dict) else queried
        status = str(query_data.get("status") or query_data.get("taskStatus") or "").upper()
        if status == "SUCCESS":
            return await _rh_result_text(session, query_data.get("results") or [])
        if status in {"FAILED", "CANCELLED", "CANCELED"}:
            message = query_data.get("errorMessage") or query_data.get("failedReason") or status
            raise _logged_upstream_error(f"RunningHub 提示词优化失败：{_upstream_label(message)}", message)


async def _call_runninghub(config: dict, payload: dict) -> str:
    """RunningHub 应用（国内版/海外版）的异步任务流程。"""
    api_key = str(config.get("api_key") or "")
    provider = str(config.get("provider") or "runninghub").lower()
    overseas = provider == "runninghub_overseas"
    edition, rh_host, rh_app_id = _RH_EDITIONS["runninghub_overseas" if overseas else "runninghub"]
    rh_api_base = f"{rh_host}/openapi/v2"
    app_nodes = RH_APP_NODES["runninghub_overseas" if overseas else "runninghub"]
    model_cache = _RH_OVERSEAS_MODEL_CACHE if overseas else _RH_MODEL_CACHE
    request_id = str(payload.get("request_id") or "")
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    duration = float(payload.get("duration") or 5)
    user_prompt = str(payload.get("prompt") or "")
    system_prompt = _local_system_prompt(config, payload, user_prompt)
    cancel_event = _cancel_probe(payload)

    video_node = str(app_nodes["video"])
    image_nodes = tuple(str(node_id) for node_id in app_nodes["images"])
    node_values = {video_node: "None", **{node_id: "None" for node_id in image_nodes}}
    timeout = aiohttp.ClientTimeout(total=200)
    temp_dir = Path(tempfile.mkdtemp(prefix="gh_h3_rh_"))
    task_id = ""
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            notes = await _upload_rh_media(
                session, api_key, media=media, app_nodes=app_nodes, video_node=video_node,
                image_nodes=image_nodes, node_values=node_values, duration=duration,
                temp_dir=temp_dir, api_base=rh_api_base,
            )
            prompt_text = "User prompt:\n" + user_prompt
            if notes:
                prompt_text += "\n\nReference mapping:\n" + "\n".join(notes)
            node_info = _rh_node_info(
                app_nodes, node_values, video_node, image_nodes, config,
                model_cache, system_prompt, prompt_text,
            )
            task_id, start_data, started, raw_body = await _rh_start_task(
                session, api_key, rh_api_base, rh_app_id, node_info
            )
            if not task_id:
                raise _rh_start_failure(edition, start_data, started, raw_body)
            if request_id:
                _ACTIVE_RH_TASKS[request_id] = (api_key, task_id, rh_host)
            return await _rh_poll_until_done(session, api_key, rh_api_base, task_id, cancel_event)
    except asyncio.CancelledError:
        active = _ACTIVE_RH_TASKS.get(request_id)
        if active:
            await _rh_abort(*active)
        elif task_id:
            await _rh_abort(api_key, task_id, rh_host)
        raise
    except asyncio.TimeoutError as error:
        if task_id:
            await _rh_abort(api_key, task_id, rh_host)
        raise RuntimeError("RunningHub 提示词优化超过200秒，云端任务已取消") from error
    finally:
        if request_id:
            _ACTIVE_RH_TASKS.pop(request_id, None)
        shutil.rmtree(temp_dir, ignore_errors=True)


def _summarize_http_status(status: int) -> str:
    """把状态码翻译成可执行提示；未知码按 5xx/4xx 分档。"""
    hint = _UPSTREAM_HTTP_HINTS.get(status)
    if hint:
        return hint
    return "上游服务异常" if status >= 500 else "上游拒绝了请求"


async def _post_upstream(url: str, headers: dict, body: dict) -> dict:
    """发一次上游请求并返回解析后的 JSON 正文。"""
    timeout = aiohttp.ClientTimeout(total=200)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=body) as response:
            text = await response.text()
            if response.status >= 400:
                hint = _summarize_http_status(response.status)
                raise _logged_upstream_error(f"提示词优化失败：{hint}（HTTP {response.status}）", text)
            return json.loads(text)


async def _post_json(config: dict, payload: dict) -> str:
    """统一出口：本地模型 / RunningHub 应用 / 自建端点三选一。"""
    if config.get("mode") == "local":
        model_path = _local_model_file(config)
        if model_path.suffix.lower() != ".gguf" and _local_missing_deps():
            raise RuntimeError("本地视觉模型依赖缺失: " + ", ".join(_local_missing_deps()))
        try:
            return await asyncio.wait_for(asyncio.to_thread(_run_local_model, config, payload), timeout=200)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # 线程不会随协程取消而停止，先置位让模型在下一个 token 处退出。
            cancel_event = _cancel_probe(payload)
            if cancel_event:
                cancel_event.set()
            raise
    if config.get("provider") == "runninghub" or config.get("protocol") == "runninghub":
        return await _call_runninghub(config, payload)
    url, headers, body = _compose_request(config, payload)
    try:
        data = await _post_upstream(url, headers, body)
    except asyncio.CancelledError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        # 异常文本可能含完整 URL（Gemini 的 Key 在查询串）→ 只进日志，不回传。
        raise _logged_upstream_error("提示词优化失败：无法连接上游，或返回内容无法解析", error) from error
    if config["protocol"] == "gemini":
        try:
            return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"]).strip()
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("Gemini returned no optimized prompt")
    if config["protocol"] == "responses":
        return _read_responses_reply(data)
    return _read_openai_reply(data)


async def _local_model_report() -> dict:
    """模型清单 + 依赖状态的组合（全部离线扫描，放线程里跑）。"""
    return {
        "models": await asyncio.to_thread(_list_visual_models),
        "mmproj_models": await asyncio.to_thread(_list_mmproj_models),
        "missing_dependencies": await asyncio.to_thread(_local_missing_deps),
        "gguf_dependency": await asyncio.to_thread(_gguf_deps_state),
    }


async def get_polish_config(_request):
    # 扫描可能触发全目录 rglob（模型目录大时数秒）：to_thread 防止冻结事件循环（审查 M11）。
    config = _client_settings(BASE_SETTINGS)
    config.update(await _local_model_report())
    return web.json_response(config)


# ===== 面板「AI 生成 / H3 格式整理」：提示词引擎的 HTTP 出口（2026-09-25 收编）=====
# AI 区块删除后，生成不再只发生在工作流执行期：面板弹窗基于编辑器当前内容
# 调用同一套 quickjs 引擎（operation=ai / format），保证面板与运行期结果一致。

# 面板 AI 生成/整理弹窗支持的布尔控件（顺序 = 前端发送顺序）。
_H3_GENERATION_CONTROLS = (
    "enrich_do_enrich",
    "enrich_soundscape",
    "enrich_music",
    "auto_timestamps",
    "fixed_camera",
    "visual_stability",
    "no_subtitles",
    "anti_pop",
)
_H3_GENERATION_MODES = {"t2va", "i2va", "fl2va", "l2va", "ref2va"}
_H3_GENERATION_LANGS = {"zh", "mixed", "en"}
_H3_GENERATION_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
_H3_GENERATION_TIMEOUT = (180, 15, 600)
_MAX_GENERATION_PROMPT_CHARS = 50000
_GENERATION_REQUEST_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_GENERATION_REQUESTS: dict[str, tuple[asyncio.Task, threading.Event]] = {}
_MAX_GENERATION_REQUESTS = 4


class GenerationInputError(ValueError):
    """请求参数不合法（返回 400，正文可直接展示给用户）。"""


class GenerationCancelled(RuntimeError):
    """用户主动取消或客户端断开（返回 409）。"""


def _generation_bool(value, name, default=False):
    """只接受 true/false（含字符串形态），其余报错，不静默改行为。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise GenerationInputError(f"{name} 必须是布尔值")


def _generation_number(value, name, default, minimum, maximum):
    """范围外 / 非有限 / 布尔值都拒绝，避免静默改变时长。"""
    if value is None:
        return default
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError
        return number
    except (TypeError, ValueError, OverflowError):
        raise GenerationInputError(f"{name} 必须是 {minimum}–{maximum} 范围内的数值") from None


def _generation_choice(value, allowed: set, fallback: str) -> str:
    """白名单之外一律回落默认值。"""
    picked = str(value or fallback).lower()
    return picked if picked in allowed else fallback


def _generation_payload_controls(payload: dict) -> dict:
    """枚举使用兼容默认值；布尔值/数值拒绝脏数据，不静默改变时长。"""
    if "controls" in payload and not isinstance(payload["controls"], dict):
        raise GenerationInputError("controls 必须是 JSON 对象")
    controls = payload.get("controls") if isinstance(payload.get("controls"), dict) else {}
    result = {name: _generation_bool(controls.get(name), name) for name in _H3_GENERATION_CONTROLS}
    result["mode"] = _generation_choice(controls.get("mode"), _H3_GENERATION_MODES, "t2va")
    result["lang"] = _generation_choice(controls.get("lang"), _H3_GENERATION_LANGS, "zh")
    duration = _generation_number(controls.get("duration"), "duration", 5, 2, 15)
    result["duration"] = f"{duration:g}"
    ratio = str(controls.get("ratio") or "16:9")
    result["ratio"] = ratio if ratio in _H3_GENERATION_RATIOS else "16:9"
    model = str(controls.get("model") or "").strip()
    if model:
        result["model"] = model
    return result


def _generation_prompt_text(payload: dict) -> str:
    text = payload.get("prompt", "")
    if not isinstance(text, str) or not text.strip():
        raise GenerationInputError("请先输入提示词（prompt 必须是非空文本）")
    if len(text) > _MAX_GENERATION_PROMPT_CHARS:
        raise GenerationInputError(f"提示词输入不能超过 {_MAX_GENERATION_PROMPT_CHARS} 字符")
    return text


def _run_generation_operation(payload: dict, operation: str, check_interrupt=lambda: None) -> dict:
    """在工作线程里跑一次引擎操作（operation = ai 或 format）。"""
    from .llm import make_client
    from .rules import generate_prompt

    text = _generation_prompt_text(payload)
    controls = _generation_payload_controls(payload)
    client = None
    if operation == "ai":
        timeout = _generation_number(payload.get("timeout"), "timeout", *_H3_GENERATION_TIMEOUT)
        client = make_client(
            str(payload.get("provider") or "deepseek"),
            str(payload.get("api_key") or ""),
            str(payload.get("endpoint") or ""),
            str(payload.get("model") or ""),
            timeout,
            check_interrupt,
        )
    check_interrupt()
    result = generate_prompt(text, operation, controls, client, check_interrupt)
    check_interrupt()
    return result


async def generate_prompt_api(request):
    return await _generation_api(request, "ai")


async def format_prompt_api(request):
    return await _generation_api(request, "format")


def _generation_request_id(payload: dict) -> str:
    request_id = payload.get("request_id") or str(uuid.uuid4())
    if not isinstance(request_id, str) or not _GENERATION_REQUEST_ID.fullmatch(request_id):
        raise GenerationInputError("request_id 格式无效")
    return request_id


async def _generation_api(request, operation):
    """面板 AI 生成 / 格式整理的 HTTP 出口（限流 + 取消 + 断连感知）。"""
    from .llm import LLMConfigurationError, LLMServiceError

    cancel_event = None
    try:
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            raise GenerationInputError("请求体必须是有效 JSON") from None
        if not isinstance(payload, dict):
            raise GenerationInputError("请求体必须是 JSON 对象")
        timeout = payload.get("timeout")
        budget = _generation_number(timeout, "timeout", *_H3_GENERATION_TIMEOUT) if operation == "ai" else 15
        request_id = _generation_request_id(payload)
        if request_id in _GENERATION_REQUESTS or request_id in _ACTIVE_REQUESTS:
            return web.json_response({"error": "请求仍在执行，请等待或取消后重试"}, status=409)
        if len(_GENERATION_REQUESTS) >= _MAX_GENERATION_REQUESTS:
            return web.json_response({"error": "生成任务繁忙，请稍后重试"}, status=429)
        cancel_event = threading.Event()
        deadline = time.monotonic() + budget

        def check_interrupt():
            if cancel_event.is_set():
                raise GenerationCancelled("生成已取消")
            if time.monotonic() >= deadline:
                raise TimeoutError("生成超时")

        # 槽位要等线程真正退出才释放：取消 asyncio.to_thread 并不会停掉线程。
        worker = asyncio.create_task(
            asyncio.to_thread(_run_generation_operation, payload, operation, check_interrupt)
        )
        _GENERATION_REQUESTS[request_id] = (worker, cancel_event)

        def finished(task):
            if _GENERATION_REQUESTS.get(request_id, (None,))[0] is task:
                _GENERATION_REQUESTS.pop(request_id, None)
            if not task.cancelled():
                task.exception()  # 断连后没人取结果，这里消费掉避免告警

        worker.add_done_callback(finished)
        while True:
            check_interrupt()
            transport = getattr(request, "transport", None)
            if hasattr(request, "transport") and (transport is None or transport.is_closing()):
                raise GenerationCancelled("生成已取消")
            done, _ = await asyncio.wait({worker}, timeout=0.1)
            if done:
                result = worker.result()
                check_interrupt()
                return web.json_response(result)
    except GenerationCancelled:
        return web.json_response({"error": "生成已取消"}, status=409)
    except TimeoutError:
        return web.json_response({"error": "生成超时，请缩短输入或调整超时设置"}, status=504)
    except (GenerationInputError, LLMConfigurationError, LLMServiceError) as error:
        return web.json_response({"error": str(error)}, status=400)
    except Exception:
        # 未知异常可能带环境 Key、请求头或上游正文，一律换成自撰文案。
        return web.json_response({"error": "提示词处理失败，请检查输入、模型配置及服务状态"}, status=400)
    finally:
        if cancel_event is not None:
            cancel_event.set()


_LOCAL_MODEL_REQUIREMENTS = ("torch", "transformers", "PIL", "accelerate", "safetensors")


def _local_missing_deps() -> list[str]:
    """本地 transformers 视觉模型需要的包，返回缺失项。"""
    import importlib.util

    return [name for name in _LOCAL_MODEL_REQUIREMENTS if importlib.util.find_spec(name) is None]


async def list_polish_models(_request):
    return web.json_response(await _local_model_report())


async def reload_runninghub_models(_request):
    """联网刷新 RunningHub 模型清单；海外版读不到时回退内置列表并附 warning。"""
    provider = str(_request.query.get("provider") or "runninghub").lower()
    if provider not in {"runninghub", "runninghub_overseas"}:
        provider = "runninghub"
    try:
        models = await _reload_rh_models(provider)
        return web.json_response({
            "provider": provider,
            "runninghub_models": list(models) if provider == "runninghub" else list(_RH_MODEL_CACHE),
            "runninghub_overseas_models": (
                list(models) if provider == "runninghub_overseas" else list(_RH_OVERSEAS_MODEL_CACHE)
            ),
        })
    except Exception as error:
        if str(_request.query.get("provider") or "").lower() == "runninghub_overseas":
            return web.json_response({
                "provider": "runninghub_overseas",
                "runninghub_models": list(_RH_MODEL_CACHE),
                "runninghub_overseas_models": list(_RH_OVERSEAS_MODEL_CACHE),
                "warning": f"RunningHub 海外版模型页暂时无法公开读取，继续使用内置列表: {error}",
            })
        return web.json_response({"error": str(error)}, status=502)


async def save_polish_config(request):
    incoming = await request.json()
    return web.json_response(_client_settings(_merge_settings(incoming)))


def _validate_local_choice(config: dict) -> None:
    """本地模式：提前把模型/投影器/依赖查完，别等加载到一半才报错。"""
    model_path = _local_model_file(config)
    if model_path.suffix.lower() == ".gguf":
        _chosen_mmproj(config, _chosen_local_model(config))
        return
    missing = _local_missing_deps()
    if missing:
        raise RuntimeError("本地视觉模型依赖缺失: " + ", ".join(missing))


def _build_polish_request(payload: dict) -> tuple[dict, threading.Event]:
    """合并配置、校验可用性，并把取消信号挂到 payload 上。"""
    config = _merge_settings(_as_mapping(payload.get("config")))
    if config.get("mode") == "local":
        _validate_local_choice(config)
    elif not config.get("api_url") or not config.get("model") or not config.get("api_key"):
        raise ValueError("请先配置提示词优化 API")
    cancel_event = threading.Event()
    payload["_cancel_event"] = cancel_event
    return config, cancel_event


def _postprocess_prompt(text: str, payload: dict, config: dict) -> str:
    """落库前统一收口：去文件名、拉直区块、补 FL2VA 标签与对白、补括号。"""
    language = str(config.get("output_language") or "English")
    polished = _render_prompt_sections(_drop_filenames(text))
    polished = _ensure_fl2va_labels(
        polished, str(payload.get("task") or "T2VA"), float(payload.get("duration") or 5), language
    )
    polished = _ensure_dialogue_lines(polished, str(payload.get("prompt") or ""), language)
    return _expand_subject_short(polished)


async def _polish_once(config: dict, payload: dict) -> str:
    result = await _post_json(config, payload)
    if not result:
        raise RuntimeError("API 返回了空提示词")
    return _postprocess_prompt(result, payload, config)


def _release_polish(request_id: str) -> None:
    """清掉该 request_id 名下登记的任务与取消信号。"""
    _ACTIVE_REQUESTS.pop(request_id, None)
    _ACTIVE_CANCEL_EVENTS.pop(request_id, None)


def _track_task(request_id: str, task: asyncio.Task, cancel_event: threading.Event) -> None:
    """登记任务；同 request_id 的旧任务先取消，避免两路并发。"""
    stale_cancel = _ACTIVE_CANCEL_EVENTS.pop(request_id, None)
    if stale_cancel is not None:
        stale_cancel.set()
    stale_task = _ACTIVE_REQUESTS.pop(request_id, None)
    if stale_task is not None:
        stale_task.cancel()
    _ACTIVE_CANCEL_EVENTS[request_id] = cancel_event
    _ACTIVE_REQUESTS[request_id] = task


async def polish_prompt(request):
    """同步等待的润色入口（短请求用；长请求请走 start/status）。"""
    request_id = ""
    try:
        payload = await request.json()
        request_id = str(payload.get("request_id") or "")
        config, cancel_event = _build_polish_request(payload)
        task = asyncio.create_task(_polish_once(config, payload))
        if request_id:
            _track_task(request_id, task, cancel_event)
        formatted = await task
        return web.json_response({"prompt": formatted})
    except asyncio.CancelledError:
        return web.json_response({"error": "提示词优化已取消"}, status=499)
    except Exception as error:
        return web.json_response({"error": str(error)}, status=400)
    finally:
        if request_id:
            _release_polish(request_id)


async def start_polish(request):
    """只登记任务并立刻返回 202：避免托管环境的反向代理把长请求掉线。"""
    try:
        payload = await request.json()
        request_id = str(payload.get("request_id") or "")
        if not request_id:
            raise ValueError("提示词优化请求缺少 request_id")
        config, cancel_event = _build_polish_request(payload)
        task = asyncio.create_task(_polish_once(config, payload))
        stale = _ASYNC_OPTIMIZER_JOBS.pop(request_id, None)
        if stale is not None:
            stale.cancel()
        _ASYNC_OPTIMIZER_JOBS[request_id] = task
        _track_task(request_id, task, cancel_event)
        return web.json_response({"request_id": request_id, "status": "running"}, status=202)
    except Exception as error:
        return web.json_response({"error": str(error)}, status=400)


async def polish_status(request):
    request_id = str(request.query.get("request_id") or "")
    task = _ASYNC_OPTIMIZER_JOBS.get(request_id)
    if task is None:
        return web.json_response({"error": "找不到提示词优化任务"}, status=404)
    if not task.done():
        return web.json_response({"status": "running"})
    _ASYNC_OPTIMIZER_JOBS.pop(request_id, None)
    _release_polish(request_id)
    try:
        return web.json_response({"status": "success", "prompt": task.result()})
    except asyncio.CancelledError:
        return web.json_response({"error": "提示词优化已取消"}, status=499)
    except Exception as error:
        return web.json_response({"error": str(error)}, status=400)


async def cancel_polish(request):
    """取消一个 request_id 名下所有形态的任务（面板生成 / 润色 / 异步 / 云端）。"""
    payload = await request.json()
    request_id = str(payload.get("request_id") or "")
    generation = _GENERATION_REQUESTS.get(request_id)
    if generation is not None:
        generation[1].set()
    task = _ACTIVE_REQUESTS.pop(request_id, None)
    async_job = _ASYNC_OPTIMIZER_JOBS.pop(request_id, None)
    cancel_event = _ACTIVE_CANCEL_EVENTS.pop(request_id, None)
    if cancel_event is not None:
        cancel_event.set()
    if task is not None:
        task.cancel()
    if async_job is not None and async_job is not task:
        async_job.cancel()
    remote = _ACTIVE_RH_TASKS.pop(request_id, None)
    if remote is not None:
        try:
            await _rh_abort(*remote)
        except Exception:
            pass
    cancelled = any(item is not None for item in (generation, task, async_job, cancel_event, remote))
    return web.json_response({"cancelled": cancelled})


# 路由表：（HTTP 方法, 路径, 处理函数）。新增端点只改这里。
_ROUTE_TABLE = (
    ("get", "/rh/minimax-h3/prompt-optimizer/config", get_polish_config),
    ("get", "/rh/minimax-h3/prompt-optimizer/models", list_polish_models),
    ("get", "/rh/minimax-h3/prompt-optimizer/runninghub-models", reload_runninghub_models),
    ("post", "/rh/minimax-h3/prompt-optimizer/config", save_polish_config),
    ("post", "/rh/minimax-h3/prompt-optimizer/optimize", polish_prompt),
    ("post", "/rh/minimax-h3/prompt-optimizer/start", start_polish),
    ("get", "/rh/minimax-h3/prompt-optimizer/status", polish_status),
    ("post", "/rh/minimax-h3/prompt-optimizer/cancel", cancel_polish),
    ("post", "/rh/minimax-h3/prompt-optimizer/generate", generate_prompt_api),
    ("post", "/rh/minimax-h3/prompt-optimizer/format", format_prompt_api),
)


def register_fanout_routes() -> bool:
    """把润色/生成相关路由挂到 ComfyUI 的 aiohttp 实例上（幂等）。"""
    global _ROUTES_BOUND
    if _ROUTES_BOUND:
        return True
    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        return False
    routes = instance.routes
    for method, path, handler in _ROUTE_TABLE:
        getattr(routes, method)(path)(handler)
    _ROUTES_BOUND = True
    return True


register_fanout_routes()
