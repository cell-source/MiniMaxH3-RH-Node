"""OpenAI-compatible streaming transport with bounded time and cancellation."""
import json
import os
import time
from urllib.parse import urlsplit

import requests

PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-flash", "DEEPSEEK_API_KEY", {"thinking": {"type": "disabled"}}),
    "glm": ("https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-5.3-flash", "GLM_API_KEY", {"reasoning_effort": "low"}),
    "custom": ("", "", "H3_LLM_API_KEY", {}),
}


def make_client(provider, api_key="", endpoint="", model="", timeout=180, check_interrupt=lambda: None):
    default_url, default_model, env_name, extra = PROVIDERS[provider]
    key = api_key.strip() or os.environ.get(env_name, "").strip() or os.environ.get("H3_LLM_API_KEY", "").strip()
    url = endpoint.strip() or default_url
    model = model.strip() or default_model
    if not key:
        raise ValueError(f"请设置服务端 {env_name}，或填写 api_key")
    if not key.isascii():
        # HTTP 头只允许 latin-1；环境变量被填成中文占位文字时给出可读的错误，
        # 而不是请求阶段的 'latin-1 codec can't encode' 崩溃。
        raise ValueError(
            f"环境变量 {env_name} 的值不是有效的 API Key（包含非 ASCII 字符），"
            "请替换为服务方签发的真实密钥，或从环境变量中移除后改填 api_key"
        )
    if urlsplit(url).scheme not in {"https", "http"} or not urlsplit(url).netloc or not model:
        raise ValueError("请填写完整的 chat/completions URL 和模型名称")
    deadline = time.monotonic() + timeout

    def complete(system, user):
        check_interrupt()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("AI 生成及纠正已超过总超时时间")
        body = {"model": model, "messages": [{"role": "system", "content": system},
                {"role": "user", "content": user}], "stream": True, "temperature": 0,
                "max_tokens": 6000, **extra}
        try:
            with requests.post(url, headers={"Authorization": f"Bearer {key}"}, json=body,
                               stream=True, timeout=(min(15, remaining), min(30, remaining)),
                               allow_redirects=False) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"AI 服务返回 HTTP {response.status_code}；请检查模型、额度及 Key")
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
                    item = json.loads(data)
                    if "error" in item:
                        raise RuntimeError("AI 服务流式响应包含错误")
                    for choice in item.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        reason = choice.get("finish_reason")
                        if reason in {"length", "content_filter"}:
                            raise RuntimeError("AI 输出被截断或过滤，请缩短输入后重试")
                        if reason == "stop":
                            finished = True
                        content = choice.get("delta", {}).get("content")
                        if isinstance(content, str):
                            chunks.append(content)
                if not finished:
                    raise RuntimeError("AI 连接提前结束，未返回完整结果")
                if not chunks:
                    raise RuntimeError("AI 返回内容为空")
                return "".join(chunks)
        except requests.RequestException:
            raise RuntimeError("AI 请求连接失败或超时，请检查服务地址和网络") from None

    return complete
