"""OpenAI-compatible streaming transport with bounded time and cancellation."""
import json
import os
import time
from urllib.parse import urlsplit

import requests

# 服务商预设（参照 MiniMaxH3-Integration-GHX 的 PROVIDERS 模式扩充为 OpenAI
# 兼容 chat/completions 文本服务）：名称 → (端点, 默认模型, 环境变量, 额外参数)。
# 端点与模型都只是预设：用户在节点上选 provider 后仍可覆盖 ai_endpoint/ai_model；
# Key 由用户填写（ai_api_key）或从环境变量读取，不硬编码在包内。
PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-flash", "DEEPSEEK_API_KEY", {"thinking": {"type": "disabled"}}),
    "glm": ("https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-5.3-flash", "GLM_API_KEY", {"reasoning_effort": "low"}),
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4.1-mini", "OPENAI_API_KEY", {}),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash", "OPENROUTER_API_KEY", {}),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen-plus", "DASHSCOPE_API_KEY", {}),
    "siliconflow": ("https://api.siliconflow.cn/v1/chat/completions", "Qwen/Qwen2.5-72B-Instruct", "SILICONFLOW_API_KEY", {}),
    "custom": ("", "", "H3_LLM_API_KEY", {}),
}


class LLMConfigurationError(ValueError):
    """Safe, locally authored configuration message (never includes credentials)."""


class LLMServiceError(RuntimeError):
    """Safe transport error without upstream bodies or request headers."""


def make_client(provider, api_key="", endpoint="", model="", timeout=180, check_interrupt=lambda: None):
    preset = PROVIDERS.get(str(provider or "").strip().lower())
    if preset is None:
        known = ", ".join(sorted(PROVIDERS))
        raise LLMConfigurationError(f"未知的 AI 提供方；可选：{known}，或选择 custom 并自行填写端点与模型")
    default_url, default_model, env_name, extra = preset
    key = api_key.strip() or os.environ.get(env_name, "").strip() or os.environ.get("H3_LLM_API_KEY", "").strip()
    url = endpoint.strip() or default_url
    model = model.strip() or default_model
    if not key:
        raise LLMConfigurationError(f"请设置服务端 {env_name}，或填写 api_key")
    if not key.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in key):
        # HTTP 头只允许 latin-1；环境变量被填成中文占位文字时给出可读的错误，
        # 而不是请求阶段的 'latin-1 codec can't encode' 崩溃。
        raise LLMConfigurationError(
            f"API Key 无效（包含空白、控制字符或非 ASCII 字符；环境变量名：{env_name}），"
            "请替换为服务方签发的真实密钥，或从环境变量中移除后改填 api_key"
        )
    if urlsplit(url).scheme not in {"https", "http"} or not urlsplit(url).netloc or not model:
        raise LLMConfigurationError("请填写完整的 chat/completions URL 和模型名称")
    deadline = time.monotonic() + timeout

    def complete_once(system, user, remaining):
        body = {"model": model, "messages": [{"role": "system", "content": system},
                {"role": "user", "content": user}], "stream": True, "temperature": 0,
                "max_tokens": 6000, **extra}
        # 首 token 可能因平台队列/冷启动超过 30s（审查 M2）：连接窗口放宽到 60s。
        with requests.post(url, headers={"Authorization": f"Bearer {key}"}, json=body,
                               stream=True, timeout=(min(60, remaining), min(60, remaining)),
                               allow_redirects=False) as response:
                if response.status_code != 200:
                    raise LLMServiceError(f"AI 服务返回 HTTP {response.status_code}；请检查模型、额度及 Key。")
                response.encoding = "utf-8"
                chunks = []
                finished = False
                for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    check_interrupt()
                    if time.monotonic() > deadline:
                        raise TimeoutError("AI 生成及纠正已超过总超时时间")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        finished = True
                        break
                    if not data:
                        continue
                    try:
                        item = json.loads(data)
                    except json.JSONDecodeError:
                        # 网关 keepalive/截断行/非 JSON 噪声：跳过而不是炸掉整个生成（审查 M1）。
                        continue
                    if not isinstance(item, dict):
                        continue
                    if "error" in item:
                        raise LLMServiceError("AI 服务流式响应包含错误")
                    for choice in item.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        reason = choice.get("finish_reason")
                        if reason in {"length", "content_filter"}:
                            raise LLMServiceError("AI 输出被截断或过滤，请缩短输入后重试")
                        if reason == "stop":
                            finished = True
                        content = choice.get("delta", {}).get("content")
                        if isinstance(content, str):
                            chunks.append(content)
                if not finished:
                    raise LLMServiceError("AI 连接提前结束，未返回完整结果")
                if not chunks:
                    raise LLMServiceError("AI 返回内容为空")
                return "".join(chunks)

    def complete(system, user):
        check_interrupt()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("AI 生成及纠正已超过总超时时间")
        # 429/5xx/网络抖动退避重试（审查 M2）：引擎单次生成会发多次 LLM 调用，
        # 一次抖动不应废弃前面已花费的调用。
        last_error = None
        for attempt in range(3):
            check_interrupt()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("AI 生成及纠正已超过总超时时间")
            try:
                return complete_once(system, user, remaining)
            except requests.RequestException:
                # requests exceptions may include headers/URLs; never expose them.
                check_interrupt()
                raise LLMServiceError("AI 服务连接失败或超时，请检查网络及端点") from None
            except RuntimeError as error:
                message = str(error)
                last_error = error
                # 仅对可重试错误退避：HTTP 状态类与服务连接类。内容类错误（截断/过滤）不重试。
                retryable = ("HTTP 429" in message or "HTTP 5" in message
                             or "连接失败或超时" in message)
                if not retryable or attempt == 2:
                    raise
                retry_at = min(deadline, time.monotonic() + min(8, 2 ** (attempt + 1)))
                while time.monotonic() < retry_at:
                    check_interrupt()
                    time.sleep(min(0.1, max(0, retry_at - time.monotonic())))
        raise last_error or RuntimeError("AI 请求失败")

    return complete
