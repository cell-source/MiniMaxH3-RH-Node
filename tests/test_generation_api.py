"""面板「AI 生成 / H3 格式整理」HTTP 出口的离线单测。

prompt_optimizer 依赖 ComfyUI 运行时（folder_paths/server），测试环境用桩模块
替身后导入；路由处理器直接以假 request 调用，不起真实 HTTP 服务。
"""
import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ComfyUI 运行时桩：仅保证 prompt_optimizer 可导入，不参与被测逻辑。
for name in ("folder_paths", "server"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
sys.modules["server"].PromptServer = types.SimpleNamespace(instance=None)

# prompt_optimizer 使用包内相对导入（.llm_client/.prompt_runtime）：
# 目录名含连字符无法常规导入，这里构造一个合成包别名再加载。
_package = types.ModuleType("h3pkg")
_package.__path__ = [str(ROOT)]
sys.modules["h3pkg"] = _package
prompt_optimizer = importlib.import_module("h3pkg.prompt_optimizer")

_generation_payload_controls = prompt_optimizer._generation_payload_controls
_run_generation_operation = prompt_optimizer._run_generation_operation
format_prompt_api = prompt_optimizer.format_prompt_api
generate_prompt_api = prompt_optimizer.generate_prompt_api
from prompt_runtime import generate_prompt  # noqa: E402,F401  (冒烟：直接导入路径可用)


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _post(handler, payload):
    return asyncio.run(handler(_FakeRequest(payload)))


def base(scene):
    return f"integrated_multimodal_description: [Shot 1] {scene}\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A"


class PayloadControlsTests(unittest.TestCase):
    def test_defaults_and_whitelists(self):
        result = _generation_payload_controls({})
        self.assertEqual(result["mode"], "t2va")
        self.assertEqual(result["lang"], "zh")
        self.assertEqual(result["duration"], "5")
        self.assertEqual(result["ratio"], "16:9")
        for name in ("enrich_do_enrich", "auto_timestamps", "no_subtitles"):
            self.assertIn(name, result)

    def test_invalid_values_fall_back(self):
        result = _generation_payload_controls({
            "controls": {"mode": "hack", "lang": "fr", "duration": "999", "ratio": "ultrawide"},
        })
        self.assertEqual(result["mode"], "t2va")
        self.assertEqual(result["lang"], "zh")
        self.assertEqual(result["duration"], "15")
        self.assertEqual(result["ratio"], "16:9")

    def test_valid_values_pass_through(self):
        result = _generation_payload_controls({
            "controls": {"mode": "fl2va", "lang": "en", "duration": 8, "ratio": "9:16",
                         "auto_timestamps": True},
        })
        self.assertEqual(result["mode"], "fl2va")
        self.assertEqual(result["lang"], "en")
        self.assertEqual(result["duration"], "8")
        self.assertEqual(result["ratio"], "9:16")
        self.assertTrue(result["auto_timestamps"])

    def test_model_hint_included_only_when_present(self):
        self.assertNotIn("model", _generation_payload_controls({}))
        self.assertEqual(_generation_payload_controls({"controls": {"model": "deepseek"}})["model"], "deepseek")


class FormatEndpointTests(unittest.TestCase):
    def test_format_offline_success(self):
        response = _post(format_prompt_api, {"prompt": base("女子站着。"),
                                             "controls": {"mode": "t2va"}})
        self.assertEqual(response.status, 200)
        body = json.loads(response.text)
        self.assertTrue(body["valid"], body.get("report"))
        self.assertIn("女子站着。", body["prompt"])

    def test_empty_prompt_rejected(self):
        response = _post(format_prompt_api, {"prompt": "   "})
        self.assertEqual(response.status, 400)
        self.assertIn("请先输入提示词", json.loads(response.text)["error"])

    def test_non_object_payload_rejected(self):
        response = _post(format_prompt_api, ["list"])
        self.assertEqual(response.status, 400)


class GenerateEndpointTests(unittest.TestCase):
    def test_generate_uses_client_and_returns_prompt(self):
        calls = {}

        def fake_make_client(provider, api_key="", endpoint="", model="", timeout=180, check_interrupt=None):
            calls["provider"] = provider
            calls["timeout"] = timeout
            def llm(system, user):
                calls["n"] = calls.get("n", 0) + 1
                # 首轮回包正文为英文时引擎会发起 LANGUAGE REPAIR 二次调用：
                # 第二次回中文，模拟真实多轮收敛（同 test_prompt_node）。
                scene = "女子站着。" if calls["n"] > 1 else "A woman stands still."
                return "DIALOGUE_COUNT: 0\n" + base(scene)
            return llm

        payload = {"prompt": "雨夜霓虹街头", "provider": "deepseek", "api_key": "sk-test",
                   "timeout": 90, "controls": {"mode": "t2va", "lang": "zh"}}
        with patch("h3pkg.llm_client.make_client", side_effect=fake_make_client):
            response = asyncio.run(generate_prompt_api(_FakeRequest(payload)))
        self.assertEqual(response.status, 200)
        body = json.loads(response.text)
        self.assertTrue(body["valid"], body.get("report"))
        self.assertEqual(calls["provider"], "deepseek")
        self.assertEqual(calls["timeout"], 90)

    def test_generate_requires_key_when_provider_unknown(self):
        payload = {"prompt": "雨夜", "provider": "no-such-provider"}
        response = asyncio.run(generate_prompt_api(_FakeRequest(payload)))
        self.assertEqual(response.status, 400)
        self.assertIn("AI 提供方", json.loads(response.text)["error"])


class RunGenerationOperationTests(unittest.TestCase):
    def test_format_operation_passes_none_client(self):
        with patch("h3pkg.prompt_runtime.generate_prompt", return_value={"prompt": "x", "valid": True, "report": ""}) as gen:
            _run_generation_operation({"prompt": "t", "controls": {}}, "format")
            self.assertIsNone(gen.call_args[0][3])

    def test_ai_operation_builds_client_with_timeout_clamp(self):
        captured = {}

        def fake_make_client(provider, api_key="", endpoint="", model="", timeout=180, check_interrupt=None):
            captured.update(provider=provider, api_key=api_key, timeout=timeout)
            calls = {"n": 0}
            def llm(system, user):
                calls["n"] += 1
                scene = "雨夜" if calls["n"] > 1 else "A rainy night."
                return "DIALOGUE_COUNT: 0\n" + base(scene)
            return llm

        with patch("h3pkg.llm_client.make_client", side_effect=fake_make_client):
            result = _run_generation_operation(
                {"prompt": "雨夜", "provider": "deepseek", "api_key": "sk", "timeout": 99999,
                 "controls": {"mode": "t2va"}},
                "ai",
            )
        self.assertTrue(result["valid"])
        self.assertEqual(captured["timeout"], 600)
        self.assertEqual(captured["api_key"], "sk")

    def test_unknown_operation_rejected(self):
        with self.assertRaises(ValueError):
            _run_generation_operation({"prompt": "t"}, "template")


if __name__ == "__main__":
    unittest.main()
