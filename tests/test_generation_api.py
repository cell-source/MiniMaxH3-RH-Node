"""面板「AI 生成 / H3 格式整理」HTTP 出口的离线单测。

prompt_optimizer 依赖 ComfyUI 运行时（folder_paths/server），测试环境用桩模块
替身后导入；路由处理器直接以假 request 调用，不起真实 HTTP 服务。
"""
import asyncio
import importlib
import json
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_saved_modules = {}


def setUpModule():
    global prompt_optimizer, _generation_payload_controls, _run_generation_operation
    global format_prompt_api, generate_prompt_api
    names = ("folder_paths", "server", "h3pkg", "h3pkg.prompt_optimizer", "h3pkg.llm_client", "h3pkg.prompt_runtime")
    for name in names:
        _saved_modules[name] = sys.modules.get(name)
        sys.modules.pop(name, None)
    sys.modules["folder_paths"] = types.ModuleType("folder_paths")
    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(instance=None)
    sys.modules["server"] = server
    package = types.ModuleType("h3pkg")
    package.__path__ = [str(ROOT)]
    sys.modules["h3pkg"] = package
    prompt_optimizer = importlib.import_module("h3pkg.prompt_optimizer")
    importlib.import_module("h3pkg.llm_client")
    importlib.import_module("h3pkg.prompt_runtime")
    _generation_payload_controls = prompt_optimizer._generation_payload_controls
    _run_generation_operation = prompt_optimizer._run_generation_operation
    format_prompt_api = prompt_optimizer.format_prompt_api
    generate_prompt_api = prompt_optimizer.generate_prompt_api


def tearDownModule():
    for name, original in _saved_modules.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


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
    def test_booleans_and_numeric_boundaries(self):
        self.assertFalse(_generation_payload_controls({"controls": {"fixed_camera": "false"}})["fixed_camera"])
        self.assertTrue(_generation_payload_controls({"controls": {"fixed_camera": "true"}})["fixed_camera"])
        for value in ("(none)", "nan", "inf", True, 0, 16, {}, []):
            with self.subTest(value=value), self.assertRaises(prompt_optimizer.GenerationInputError):
                _generation_payload_controls({"controls": {"duration": value}})
        for value in ("no", 1, [], {}):
            with self.subTest(value=value), self.assertRaises(prompt_optimizer.GenerationInputError):
                _generation_payload_controls({"controls": {"fixed_camera": value}})
        for value in (2, 15):
            self.assertEqual(_generation_payload_controls({"controls": {"duration": value}})["duration"], str(value))

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
            "controls": {"mode": "hack", "lang": "fr", "duration": "15", "ratio": "ultrawide"},
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
    def test_errors_never_echo_credentials(self):
        sentinel = "private-key-sentinel"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "prefix\n" + sentinel}), patch("h3pkg.llm_client.requests.post") as post:
            response = _post(generate_prompt_api, {"prompt": "雨夜"})
            self.assertEqual(response.status, 400)
            self.assertNotIn(sentinel, response.text)
            post.assert_not_called()
        for error in (RuntimeError(sentinel), ValueError(sentinel)):
            with patch("h3pkg.prompt_runtime.generate_prompt", side_effect=error):
                response = _post(format_prompt_api, {"prompt": "雨夜"})
                self.assertEqual(response.status, 400)
                self.assertNotIn(sentinel, response.text)
        upstream = types.SimpleNamespace(status_code=401, text=sentinel)
        class Response:
            def __enter__(self): return upstream
            def __exit__(self, *args): pass
        with patch("h3pkg.llm_client.requests.post", return_value=Response()):
            response = _post(generate_prompt_api, {"prompt": "雨夜", "api_key": sentinel})
            self.assertEqual(response.status, 400)
            self.assertIn("401", response.text)
            self.assertNotIn(sentinel, response.text)

    def test_invalid_timeout_and_input_are_rejected(self):
        for payload in ({"prompt": "x", "timeout": 601}, {"prompt": "x", "timeout": "(none)"},
                        {"prompt": "x", "controls": {"duration": 30}}, {"prompt": ["x"]},
                        {"prompt": "x" * 50001}, {"prompt": "x", "controls": []}):
            with self.subTest(payload=str(payload)[:100]):
                self.assertEqual(_post(generate_prompt_api, payload).status, 400)

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
    def test_all_boolean_options_reach_engine(self):
        for enabled in (False, True):
            controls = {name: enabled for name in prompt_optimizer._H3_GENERATION_CONTROLS}
            with patch("h3pkg.prompt_runtime.generate_prompt", return_value={"prompt": "x"}) as generate:
                _run_generation_operation({"prompt": "x", "controls": controls}, "format")
            for name in controls:
                self.assertIs(generate.call_args.args[2][name], enabled)

    def test_real_engine_sound_options(self):
        for enabled in (False, True):
            result = _run_generation_operation({"prompt": base("女子站着。"), "controls": {
                "enrich_soundscape": enabled, "enrich_music": enabled, "lang": "zh",
            }}, "format")
            self.assertEqual("贴合画面环境的环境声自然延续" in result["prompt"], enabled)
            self.assertEqual("慢速而克制的钢琴独奏" in result["prompt"], enabled)

    def test_format_operation_passes_none_client(self):
        with patch("h3pkg.prompt_runtime.generate_prompt", return_value={"prompt": "x", "valid": True, "report": ""}) as gen:
            _run_generation_operation({"prompt": "t", "controls": {}}, "format")
            self.assertIsNone(gen.call_args[0][3])

    def test_ai_operation_builds_client_with_timeout(self):
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
                {"prompt": "雨夜", "provider": "deepseek", "api_key": "sk", "timeout": 600,
                 "controls": {"mode": "t2va"}},
                "ai",
            )
        self.assertTrue(result["valid"])
        self.assertEqual(captured["timeout"], 600)
        self.assertEqual(captured["api_key"], "sk")

    def test_unknown_operation_rejected(self):
        with self.assertRaises(ValueError):
            _run_generation_operation({"prompt": "t"}, "template")


class GenerationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        routes = web.RouteTableDef()
        with patch.object(prompt_optimizer, "PromptServer", types.SimpleNamespace(instance=types.SimpleNamespace(routes=routes))), patch.object(prompt_optimizer, "_ROUTES_REGISTERED", False):
            self.assertTrue(prompt_optimizer.register_prompt_optimizer_routes())
        app = web.Application()
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        workers = list(prompt_optimizer._GENERATION_REQUESTS.values())
        for _, event in workers:
            event.set()
        if workers:
            await asyncio.wait_for(asyncio.gather(*(task for task, _ in workers), return_exceptions=True), 2)

    async def test_registered_format_and_bad_json(self):
        response = await self.client.post("/rh/minimax-h3/prompt-optimizer/format", json={"prompt": base("女子站着。")})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["valid"])
        response = await self.client.post("/rh/minimax-h3/prompt-optimizer/generate", data="broken-json")
        self.assertEqual(response.status, 400)

    async def test_cancel_reaches_thread_and_capacity_is_bounded(self):
        started, stopped = threading.Event(), threading.Event()
        def slow(payload, operation, check_interrupt):
            started.set()
            try:
                while not stopped.wait(0.005):
                    check_interrupt()
            finally:
                stopped.set()
        with patch.object(prompt_optimizer, "_run_generation_operation", side_effect=slow), patch.object(prompt_optimizer, "_MAX_GENERATION_REQUESTS", 1):
            pending = asyncio.create_task(self.client.post("/rh/minimax-h3/prompt-optimizer/generate", json={"request_id": "cancel-test", "prompt": "x"}))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            busy = await self.client.post("/rh/minimax-h3/prompt-optimizer/generate", json={"prompt": "x"})
            self.assertEqual(busy.status, 429)
            cancel = await self.client.post("/rh/minimax-h3/prompt-optimizer/cancel", json={"request_id": "cancel-test"})
            self.assertTrue((await cancel.json())["cancelled"])
            self.assertEqual((await pending).status, 409)
            self.assertTrue(await asyncio.to_thread(stopped.wait, 2))
        await asyncio.sleep(0)
        self.assertNotIn("cancel-test", prompt_optimizer._GENERATION_REQUESTS)

    async def test_disconnect_cancels_thread(self):
        started, stopped = threading.Event(), threading.Event()
        def slow(payload, operation, check_interrupt):
            started.set()
            try:
                while not stopped.wait(0.005):
                    check_interrupt()
            finally:
                stopped.set()
        with patch.object(prompt_optimizer, "_run_generation_operation", side_effect=slow):
            pending = asyncio.create_task(self.client.post("/rh/minimax-h3/prompt-optimizer/generate", json={"request_id": "disconnect-test", "prompt": "x"}))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(await asyncio.to_thread(stopped.wait, 2))


class NormalizeConfigTests(unittest.TestCase):
    """润色（✦）配置归一：面板端点/模型留空时按预设补齐；未知 provider 不回退 RunningHub。"""

    def test_generation_provider_presets_resolve_when_widget_empty(self):
        # 润色沿用主区生成平台：非 custom 平台 ai_endpoint/ai_model 常为空。
        for provider, url, model, protocol in (
            ("deepseek", "https://api.deepseek.com", "deepseek-flash", "openai"),
            ("glm", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flash", "openai"),
            ("openai", "https://api.openai.com/v1", "gpt-4.1-mini", "openai"),
            ("gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash", "gemini"),
        ):
            config = prompt_optimizer._normalize_config({"provider": provider, "api_key": "k", "protocol": "openai" if provider != "gemini" else None})
            self.assertEqual(config["api_url"], url, provider)
            self.assertEqual(config["model"], model, provider)
            self.assertEqual(config["protocol"], protocol, provider)

    def test_unknown_provider_keeps_empty_url_and_model(self):
        # 回归：此前回退到 RunningHub 预设，把 DeepSeek 的 Key 发往 RunningHub。
        config = prompt_optimizer._normalize_config({"provider": "no-such", "api_key": "k"})
        self.assertEqual(config["api_url"], "")
        self.assertEqual(config["model"], "")

    def test_custom_explicit_url_and_model_pass_through(self):
        config = prompt_optimizer._normalize_config({"provider": "custom", "api_url": "https://example.invalid/v1", "model": "m1", "api_key": "k"})
        self.assertEqual(config["api_url"], "https://example.invalid/v1")
        self.assertEqual(config["model"], "m1")

    def test_runninghub_preset_still_resolves(self):
        config = prompt_optimizer._normalize_config({})
        self.assertEqual(config["provider"], "runninghub")
        self.assertEqual(config["api_url"], "https://www.runninghub.cn/openapi/v2")
        self.assertEqual(config["model"], "openai/gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
