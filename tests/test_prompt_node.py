import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prompt_runtime import generate_prompt
from llm_client import make_client


def controls(language="zh", mode="t2va", **options):
    return dict(mode=mode, lang=language, duration="5", ratio="16:9", model="deepseek",
                enrich_do_enrich=False, enrich_soundscape=False, enrich_music=False,
                auto_timestamps=False, fixed_camera=False, visual_stability=False,
                no_subtitles=False, anti_pop=False, **options)


def base(scene):
    return f"integrated_multimodal_description: [Shot 1] {scene}\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A"


class PromptTests(unittest.TestCase):
    def test_modes_and_languages(self):
        for language in ("zh", "en", "mixed"):
            scene = "女子站着。" if language == "zh" else "A woman stands still."
            for mode in ("t2va", "i2va", "fl2va", "l2va"):
                with self.subTest(language=language, mode=mode):
                    result = generate_prompt(scene, "offline", controls(language, mode))
                    self.assertTrue(result["valid"], result)
                    self.assertIn(scene, result["prompt"])
                    if mode in ("fl2va", "l2va"):
                        self.assertIn("5.00", result["prompt"])

    def test_ref_format(self):
        source = "subject_definitions:\n<Subject 1>：<Picture 1> 中的女子。\n\nsummary:\n[reference generation] 女子站着。\n\nretention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - 保留人物身份。\n\ndetailed_description:\n[Shot 1] <Subject 1> 站着。\n\noverall_soundscape:\nN/A\n\nnon_diegetic_music:\nN/A"
        self.assertTrue(generate_prompt(source, "format", controls(mode="ref2va"))["valid"])

    def test_ai_language_repair_and_failure(self):
        calls = []
        def llm(system, user):
            calls.append(user)
            return "DIALOGUE_COUNT: 0\n" + base("A woman stands still." if len(calls) == 1 else "女子站着。")
        result = generate_prompt("女子站着。", "ai", controls(), llm)
        self.assertTrue(result["valid"], result)
        self.assertEqual(len(calls), 2)
        self.assertIn("LANGUAGE REPAIR", calls[1])
        result = generate_prompt("女子站着。", "ai", controls(), lambda s, u: "DIALOGUE_COUNT: 0\n" + base("A woman stands still."))
        self.assertFalse(result["valid"])

    def test_option_isolation_and_dialogue(self):
        source = base("女子 (S1) 说：<d>[Chinese] 你好。</d>")
        enabled = controls()
        enabled.update(no_subtitles=True, anti_pop=True, visual_stability=True, fixed_camera=True)
        result = generate_prompt(source, "format", enabled)
        self.assertTrue(result["valid"], result)
        self.assertNotIn("<d>", result["prompt"])
        self.assertIn("[Chinese] 你好。", result["prompt"])
        plain = generate_prompt(source, "format", controls())
        self.assertIn("<d>", plain["prompt"])
        self.assertLess(len(plain["prompt"]), len(result["prompt"]))

    def test_errors_and_cancellation_propagate(self):
        class Interrupted(Exception):
            pass
        def fail(*args):
            raise Interrupted("stopped")
        with self.assertRaises(Interrupted):
            generate_prompt("女子站着。", "ai", controls(), fail)
        with self.assertRaises(ValueError):
            generate_prompt(" ", "offline", controls())

    def test_size_limit(self):
        result = generate_prompt(base("女子站着。" * 1500), "format", controls())
        self.assertFalse(result["valid"])

    def test_fl2va_timeline_repair(self):
        opts = controls("en", "fl2va")
        opts["auto_timestamps"] = True
        alignment = "How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the 5.00-second mark of the target video."
        calls = []
        def llm(system, user):
            calls.append(user)
            scene = "0.0-5.0s: A woman walks." if len(calls) == 1 else "0.0-2.0s: A woman walks. 2.0-5.0s: The woman continues walking."
            return "DIALOGUE_COUNT: 0\n" + alignment + "\n\n" + base(scene)
        result = generate_prompt("A woman walks.", "ai", opts, llm)
        self.assertTrue(result["valid"], result)
        self.assertEqual(len(calls), 2)
        self.assertIn("TIMELINE REPAIR", calls[1])

    def test_example_prompt_settings(self):
        graph = json.loads((ROOT / "examples/t2va.api.json").read_text(encoding="utf-8"))
        settings = graph["2"]["inputs"]
        opts = controls("en")
        opts["no_subtitles"] = settings["ai_no_subtitles"]
        self.assertTrue(generate_prompt(settings["ai_text"], settings["prompt_source"], opts)["valid"])

    def test_examples_all_modes_valid(self):
        """examples/ 的五种模式提示词输入必须在引擎内通过严格校验（2026-09-24 单节点示例）。"""
        for path in sorted((ROOT / "examples").glob("*.api.json")):
            gen = json.loads(path.read_text(encoding="utf-8"))["2"]["inputs"]
            opts = controls(gen["ai_language"], gen["ai_mode"])
            opts.update(enrich_do_enrich=gen["ai_enrich"], enrich_soundscape=gen["ai_soundscape"],
                        enrich_music=gen["ai_music"], auto_timestamps=gen["ai_auto_timestamps"],
                        fixed_camera=gen["ai_fixed_camera"], visual_stability=gen["ai_visual_stability"],
                        no_subtitles=gen["ai_no_subtitles"], anti_pop=gen["ai_anti_pop"])
            with self.subTest(example=path.name):
                result = generate_prompt(gen["ai_text"], gen["prompt_source"], opts)
                self.assertTrue(result["valid"], f"{path.name}: {result['report']}")

    def test_zh_offline_connectors_localized(self):
        """zh 手工路径的连接短语已本地化，不得泄漏英文模板短语（2026-09-23 修复回归）。"""
        needles = {"i2va": "中的主体与构图被完整保留",
                   "fl2va": "开场状态遵循 Picture 1",
                   "l2va": "运动元素在结尾精确落定为 <Picture 1> 所确立的状态与构图"}
        leaks = ("the subject and composition shown in", "the opening state follows",
                 "the motion settles into", "the moving elements settle into")
        for mode, needle in needles.items():
            with self.subTest(mode=mode):
                result = generate_prompt("她缓缓抬起头。", "offline", controls("zh", mode))
                self.assertTrue(result["valid"], result["report"])
                self.assertIn(needle, result["prompt"])
                for leak in leaks:
                    self.assertNotIn(leak, result["prompt"])
        # en 行为不变：英文连接短语仍在
        result = generate_prompt("She lifts her gaze.", "offline", controls("en", "fl2va"))
        self.assertTrue(result["valid"], result["report"])
        self.assertIn("the opening state follows Picture 1", result["prompt"])

    def test_zh_na_self_heal_uses_chinese_bed(self):
        """zh 模式勾选音景/配乐但输出 N/A 时，自愈注入中文床句而非英文（2026-09-23 修复回归）。"""
        opts = controls("zh")
        opts.update(enrich_soundscape=True, enrich_music=True, fixed_camera=True, no_subtitles=True)
        source = base("女子 (S1) 说：<d>[Chinese] 你好。</d>")
        result = generate_prompt(source, "format", opts)
        self.assertTrue(result["valid"], result["report"])
        self.assertIn("贴合画面环境的环境声自然延续", result["prompt"])
        self.assertIn("慢速而克制的钢琴独奏", result["prompt"])
        self.assertNotIn("Quiet subtle indoor room tone", result["prompt"])
        self.assertNotIn("A restrained solo piano score", result["prompt"])
        # en 行为不变：英文床句仍在
        en_opts = controls("en")
        en_opts.update(enrich_soundscape=True, enrich_music=True)
        result = generate_prompt(base("A woman (S1) says: <d>[English] Hello.</d>"), "format", en_opts)
        self.assertTrue(result["valid"], result["report"])
        self.assertIn("Quiet subtle indoor room tone", result["prompt"])


class Response:
    status_code = 200
    def __init__(self, lines): self.lines = lines
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_lines(self, **kwargs): return iter(self.lines)


class TransportTests(unittest.TestCase):
    def test_stream(self):
        lines = ['data: ' + json.dumps({"choices": [{"delta": {"content": "结果"}, "finish_reason": None}]}), 'data: [DONE]']
        with patch("llm_client.requests.post", return_value=Response(lines)) as post:
            result = make_client("custom", "test-secret", "https://example.test/chat/completions", "test")("system", "user")
            self.assertEqual(result, "结果")
            self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_truncated_or_http_error(self):
        for response in (Response([]), Response(['data: {"choices":[{"delta":{},"finish_reason":"length"}]}'])):
            with patch("llm_client.requests.post", return_value=response), self.assertRaises(RuntimeError):
                make_client("deepseek", "test-secret")("s", "u")
        response = Response([])
        response.status_code = 401
        with patch("llm_client.requests.post", return_value=response), self.assertRaisesRegex(RuntimeError, "401"):
            make_client("deepseek", "test-secret")("s", "u")


if __name__ == "__main__":
    unittest.main()
