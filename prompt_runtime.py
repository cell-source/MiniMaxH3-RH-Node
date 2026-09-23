"""Execute the original H3 rules without a browser or a Node.js installation."""
from __future__ import annotations

import json
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "prompt_engine"


def generate_prompt(text, operation, controls, llm=None, check_interrupt=lambda: None):
    import quickjs

    if not text.strip():
        raise ValueError("请先输入提示词")
    if operation not in {"ai", "offline", "format"}:
        raise ValueError("Unknown operation")
    context = quickjs.Context()
    context.set_memory_limit(64 * 1024 * 1024)
    context.set_max_stack_size(2 * 1024 * 1024)
    context.set("__defaults", (ASSETS / "defaults.json").read_text(encoding="utf-8"))
    failure = []

    def call(payload):
        try:
            check_interrupt()
            if llm is None:
                raise ValueError("AI 模式需要配置服务端 API Key")
            request = json.loads(payload)
            content = llm(request["system"], request["user"])
            check_interrupt()
            return json.dumps({"content": content})
        except Exception as exc:
            failure.append(exc)
            return json.dumps({"error": "AI 请求失败"})

    context.add_callable("__llm", call)
    for name in ("adapter.js", "rules.js", "entry.js"):
        context.eval((ASSETS / name).read_text(encoding="utf-8"))
    options = json.dumps({"text": text, "operation": operation, "controls": controls}, ensure_ascii=False)
    context.eval("var __result = null; var __error = null; runNode(" + options +
                 ").then(r => {__result=JSON.stringify(r)}, e => {__error=String(e.stack || e)});")
    while context.execute_pending_job():
        check_interrupt()
    if failure:
        raise failure[0]
    error = context.eval("__error")
    if error:
        raise RuntimeError(error)
    result = context.eval("__result")
    if result is None:
        raise RuntimeError("提示词引擎未完成执行")
    return json.loads(result)
