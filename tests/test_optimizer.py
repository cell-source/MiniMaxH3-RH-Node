"""h3kit.optimizer 的行为基线测试（去相似化重写的安全网）。

重写前先把当前行为写成黄金值规格，重写后必须逐条通过。
与 test_generation_api.py 一样，ComfyUI 运行时（folder_paths/server）用桩模块替身。
"""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_saved_modules = {}


def setUpModule():
    global opt
    names = ("folder_paths", "server", "h3q", "h3q.h3kit.optimizer", "h3q.h3kit.llm", "h3q.h3kit.rules")
    for name in names:
        _saved_modules[name] = sys.modules.get(name)
        sys.modules.pop(name, None)
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_input_directory = lambda: tempfile.gettempdir()
    folder_paths.get_annotated_filepath = lambda name: name
    sys.modules["folder_paths"] = folder_paths
    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(instance=None)
    sys.modules["server"] = server
    package = types.ModuleType("h3q")
    package.__path__ = [str(ROOT)]
    sys.modules["h3q"] = package
    opt = importlib.import_module("h3q.h3kit.optimizer")
    importlib.import_module("h3q.h3kit.llm")
    importlib.import_module("h3q.h3kit.rules")


def tearDownModule():
    for name, original in _saved_modules.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


class ConstantFingerprintTests(unittest.TestCase):
    """常量重排（换行/结构）后数据必须一字不差。"""

    def _fingerprint(self, value):
        import hashlib
        blob = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def test_runninghub_model_list_is_unchanged(self):
        self.assertEqual(len(opt.RH_MODEL_LIST), 61)
        self.assertEqual(self._fingerprint(list(opt.RH_MODEL_LIST)), "96c42276aefd10c5")

    def test_app_node_table_is_unchanged(self):
        self.assertEqual(self._fingerprint(opt.RH_APP_NODES), "b7a4c1f87625bef2")

    def test_base_settings_are_unchanged(self):
        self.assertEqual(self._fingerprint(opt.BASE_SETTINGS), "a027f42bd5792362")

    def test_endpoint_presets_are_unchanged(self):
        endpoints = {key: list(value) for key, value in opt.SERVICE_ENDPOINTS.items()}
        self.assertEqual(self._fingerprint(endpoints), "048b50e3f962beba")

    def test_generation_controls_are_unchanged(self):
        self.assertEqual(self._fingerprint(list(opt._H3_GENERATION_CONTROLS)), "0f2e2326bb20ab9c")

    def test_app_ids_and_detail_urls_are_unchanged(self):
        self.assertEqual(opt.RH_APP_ID, "2089252473927196673")
        self.assertEqual(opt.RH_OVERSEAS_APP_ID, "2090668262521675778")
        self.assertIn(opt.RH_APP_ID, opt.RH_DETAIL_URL)
        self.assertIn(opt.RH_OVERSEAS_APP_ID, opt.RH_OVERSEAS_DETAIL_URL)

    def test_no_rename_artifacts_survive_in_user_visible_strings(self):
        import ast
        source = (ROOT / "h3kit" / "optimizer.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn("_substitute", node.value)
                self.assertNotIn("鷴", node.value)


class HttpHintTests(unittest.TestCase):
    def test_hints_cover_the_expected_status_codes(self):
        self.assertEqual(
            sorted(opt._UPSTREAM_HTTP_HINTS),
            [400, 401, 403, 404, 422, 429],
        )

    def test_hints_are_readable_chinese(self):
        # 批量改名曾把「鉴权」打成生僻字，这里锁死。
        self.assertIn("鉴权失败", opt._UPSTREAM_HTTP_HINTS[401])
        self.assertIn("鉴权失败", opt._UPSTREAM_HTTP_HINTS[403])

    def test_label_limit_matches_the_truncation(self):
        long_label = opt._upstream_label("x" * (opt._UPSTREAM_LABEL_LIMIT + 50))
        self.assertEqual(len(long_label), opt._UPSTREAM_LABEL_LIMIT)


class SystemMessageContractTests(unittest.TestCase):
    """_system_message 是发往大模型的系统提示：锁住所有载荷性要求。"""

    def _message(self, **kwargs):
        params = {"task": "T2VA", "duration": 5.0, "labels": [], "output_language": "English",
                  "context": {}, "user_prompt": ""}
        params.update(kwargs)
        return opt._system_message(
            params["task"], params["duration"], params["labels"],
            params["output_language"], params["context"], params["user_prompt"],
        )

    def test_three_section_tasks_use_the_short_structure(self):
        text = self._message(task="I2VA")
        self.assertIn("Return exactly these three sections", text)
        self.assertIn("integrated_multimodal_description", text)
        self.assertIn("overall_soundscape", text)
        self.assertIn("non_diegetic_music", text)
        self.assertNotIn("Return exactly these six sections", text)

    def test_reference_tasks_use_the_six_section_structure(self):
        for task in ("REF2VA", "HYBRID"):
            text = self._message(task=task)
            self.assertIn("Return exactly these six sections", text)
            for section in ("subject_definitions", "summary", "retention_analysis",
                            "detailed_description", "overall_soundscape", "non_diegetic_music"):
                self.assertIn(section, text, task)

    def test_fl2va_requires_bare_picture_tokens(self):
        text = self._message(task="FL2VA", duration=7.5)
        self.assertIn("'picture 1' and 'picture 2'", text)
        self.assertIn("7.50s", text)

    def test_other_tasks_require_angle_bracket_labels(self):
        text = self._message(task="T2VA")
        self.assertIn("<picture 1>", text)
        self.assertNotIn("'picture 1' and 'picture 2'", text)

    def test_duration_and_labels_are_reported(self):
        text = self._message(duration=12.25, labels=["<picture 1>", "<audio 1>"])
        self.assertIn("duration=12.25s", text)
        self.assertIn("available labels=<picture 1>, <audio 1>", text)

    def test_missing_labels_are_rendered_as_none(self):
        self.assertIn("available labels=none", self._message())

    def test_keyframe_roles_come_from_the_context(self):
        text = self._message(context={"keyframes": [
            {"label": "<picture 1>", "role": "exact_first_frame"},
            {"label": "<picture 2>", "role": "exact_last_frame"},
            {"label": "<video 1>", "role": "keyframe"},
        ]}, duration=9.0)
        self.assertIn("<picture 1> is the exact first-frame anchor at 0.00s", text)
        self.assertIn("<picture 2> is the exact last-frame anchor at 9.00s", text)
        self.assertIn("<video 1> is a concrete visual keyframe anchor", text)

    def test_incomplete_keyframe_entries_are_skipped(self):
        text = self._message(context={"keyframes": [
            {"label": "<picture 1>"},
            {"role": "exact_first_frame"},
            "not-a-dict",
        ]})
        self.assertIn("No concrete keyframe role was supplied by the node", text)

    def test_audio_modes_select_distinct_rules(self):
        self.assertIn("original audio is preserved", self._message(context={"audio_mode": "lock_source"}))
        self.assertIn("reference-only", self._message(context={"audio_mode": "reference_only"}))
        self.assertIn("partial copying or remixing", self._message(context={"audio_mode": "remix_source"}))
        self.assertIn("automatic generation", self._message(context={"audio_mode": "native"}))
        unknown = self._message(context={"audio_mode": "weird"})
        self.assertIn("conservatively", unknown)

    def test_singing_trigger_accepts_chinese_and_english(self):
        for prompt in ("女孩唱一首歌", "a boy singing a song"):
            self.assertIn("singing or lip synchronization", self._message(user_prompt=prompt))

    def test_singing_rule_is_absent_otherwise(self):
        self.assertNotIn("singing or lip synchronization", self._message(user_prompt="一只猫走过"))

    def test_substitute_trigger_accepts_both_spellings(self):
        # 批量改名曾把 substitute 改成 _substitute，导致英文触发词失效。
        for prompt in ("replace the person", "substitute the subject", "做一个动作迁移"):
            text = self._message(user_prompt=prompt)
            self.assertIn("subject-substitute or motion-transfer request", text)
            self.assertIn("one-to-one substitute mapping", text)

    def test_transfer_rule_is_absent_otherwise(self):
        self.assertNotIn("motion-transfer request", self._message(user_prompt="一只猫走过"))

    def test_language_rule_switches_with_the_setting(self):
        chinese = self._message(output_language="中文")
        self.assertIn("输出语言强制为中文", chinese)
        english = self._message(output_language="English")
        self.assertIn("Output all natural-language content in English", english)

    def test_dialogue_and_shorthand_rules_are_always_present(self):
        text = self._message()
        self.assertIn("<d>[Chinese]中文原文</d>", text)
        self.assertIn("(S1), (S2), ... through (S20)", text)

    def test_system_prompt_output_is_byte_stable(self):
        """系统提示词是产品内容：重排代码结构时输出必须一字不差。"""
        import hashlib

        cases = [
            ("T2VA", 5.0, [], "English", {}, ""),
            ("FL2VA", 7.5, ["<picture 1>", "<picture 2>"], "中文",
             {"keyframes": [{"label": "<picture 1>", "role": "exact_first_frame"},
                            {"label": "<picture 2>", "role": "exact_last_frame"}]}, "女孩说：“你好世界”。"),
            ("REF2VA", 12.25, ["<video 1>", "<audio 1>"], "English",
             {"audio_mode": "lock_source", "keyframes": []}, "replace the person"),
            ("HYBRID", 3.0, ["<picture 1>"], "English", {"audio_mode": "remix_source"},
             "a boy singing a song"),
        ]
        expected = ["0303677d301c78b3", "686505f6da376e2e", "2ad8cd51d5e628d2", "22f746901f93b256"]
        for case, digest in zip(cases, expected):
            text = opt._system_message(*case)
            actual = hashlib.sha256(text.encode()).hexdigest()[:16]
            self.assertEqual(actual, digest, case[0])


class FormatHelperTests(unittest.TestCase):
    def test_drop_filenames_removes_media_names(self):
        text = opt._drop_filenames("cat.png is here")
        self.assertEqual(text, "is here")
        self.assertEqual(opt._drop_filenames("clip.mp4"), "")

    def test_drop_filenames_consumes_the_span_between_two_names(self):
        # 已知怪癖：字符集包含空格，两个文件名之间的描述会被一并吃掉。
        # 这是上游既有行为（宁可多剔也不漏文件名），此处锁住以免重写时静默改变。
        self.assertEqual(opt._drop_filenames("look at cat.png and clip.mp4 now"), "now")

    def test_drop_filenames_keeps_plain_words(self):
        self.assertEqual(opt._drop_filenames("hello world"), "hello world")

    def test_render_sections_puts_headings_on_their_own_line(self):
        text = opt._render_prompt_sections("subject_definitions: A cat.\n\nsummary: It walks.")
        self.assertIn("subject_definitions:\nA cat.", text)
        self.assertIn("summary:\nIt walks.", text)

    def test_render_sections_is_idempotent(self):
        once = opt._render_prompt_sections("summary: A cat walks.\n\noverall_soundscape: rain.")
        self.assertEqual(once, opt._render_prompt_sections(once))

    def test_expand_subject_short_parenthesizes_bare_numbers(self):
        self.assertEqual(opt._expand_subject_short("S1 waves at S12."), "(S1) waves at (S12).")

    def test_expand_subject_short_keeps_existing_parentheses(self):
        text = "(S1) waves at (S12)."
        self.assertEqual(opt._expand_subject_short(text), text)

    def test_expand_subject_short_ignores_other_tokens(self):
        for token in ("S21", "S0", "S1A", "AS1"):
            self.assertEqual(opt._expand_subject_short(token), token)


class DialogueTests(unittest.TestCase):
    def test_collect_dialogues_reads_quoted_speech(self):
        found = opt._collect_dialogues("女孩说：“你好世界”。")
        self.assertEqual(found, [("你好世界", "Chinese")])

    def test_collect_dialogues_tags_english_as_english(self):
        found = opt._collect_dialogues('The man says: "good morning".')
        self.assertEqual(found, [("good morning", "English")])

    def test_collect_dialogues_needs_a_speech_marker(self):
        self.assertEqual(opt._collect_dialogues("一只猫。"), [])

    def test_collect_dialogues_drops_contained_duplicates(self):
        # 先出现的短句胜出，后续包含它的长句被丢弃。
        found = opt._collect_dialogues('他说：“你好”。她又说：“你好世界”。')
        self.assertEqual([item[0] for item in found], ["你好"])

    def test_collect_dialogues_keeps_the_longer_form_when_it_comes_first(self):
        found = opt._collect_dialogues('女子说：“你好世界”。')
        self.assertEqual(found, [("你好世界", "Chinese")])

    def test_ensure_dialogue_lines_inserts_after_the_speaking_cue(self):
        base = ("integrated_multimodal_description: [Shot 1] The girl speaks. Then she leaves.\n\n"
                "overall_soundscape: N/A\n\nnon_diegetic_music: N/A")
        result = opt._ensure_dialogue_lines(base, "女孩说：“你好世界”。", "English")
        self.assertIn("<d>[Chinese]你好世界</d>", result)
        self.assertIn("(S1) says", result)
        self.assertLess(result.index("speaks."), result.index("<d>[Chinese]"))

    def test_ensure_dialogue_lines_reuses_an_existing_tag(self):
        base = ("integrated_multimodal_description: [Shot 1] The girl says: <d>[Chinese]你好世界</d>\n\n"
                "overall_soundscape: N/A\n\nnon_diegetic_music: N/A")
        result = opt._ensure_dialogue_lines(base, "女孩说：“你好世界”。", "English")
        self.assertEqual(result.count("<d>[Chinese]你好世界</d>"), 1)

    def test_ensure_dialogue_lines_moves_a_misplaced_tag(self):
        base = ("subject_definitions: A girl.\n\nsummary: <d>[Chinese]你好世界</d>\n\n"
                "detailed_description: [Shot 1] The girl speaks.\n\noverall_soundscape: N/A\n\n"
                "non_diegetic_music: N/A")
        result = opt._ensure_dialogue_lines(base, "女孩说：“你好世界”。", "English")
        self.assertEqual(result.count("<d>[Chinese]你好世界</d>"), 1)
        defined = result.index("detailed_description:")
        self.assertGreater(result.index("<d>[Chinese]你好世界</d>"), defined)

    def test_ensure_dialogue_lines_wraps_an_untagged_quote_in_place(self):
        base = ("integrated_multimodal_description: [Shot 1] The girl speaks: “你好世界”\n\n"
                "overall_soundscape: N/A\n\nnon_diegetic_music: N/A")
        result = opt._ensure_dialogue_lines(base, "女孩说：“你好世界”。", "English")
        self.assertIn("<d>[Chinese]你好世界</d>", result)
        self.assertNotIn("“你好世界”", result)

    def test_ensure_dialogue_lines_uses_a_chinese_sentence_for_chinese_output(self):
        base = ("integrated_multimodal_description: [Shot 1] 女孩开口说话。随后离开。\n\n"
                "overall_soundscape: N/A\n\nnon_diegetic_music: N/A")
        result = opt._ensure_dialogue_lines(base, "女孩说：“你好世界”。", "中文")
        self.assertIn("画面中的说话者 (S1) 说：", result)

    def test_ensure_fl2va_labels_only_touches_fl2va(self):
        text = "integrated_multimodal_description: a cat walks.\n\noverall_soundscape: N/A"
        self.assertEqual(opt._ensure_fl2va_labels(text, "T2VA", 5, "English"), text)

    def test_ensure_fl2va_labels_prepends_the_alignment_sentence(self):
        text = "integrated_multimodal_description: a cat walks.\n\noverall_soundscape: N/A"
        result = opt._ensure_fl2va_labels(text, "FL2VA", 6.5, "English")
        self.assertTrue(result.startswith("Reference-picture alignment:"))
        self.assertIn("picture 1", result)
        self.assertIn("6.50s", result)

    def test_ensure_fl2va_labels_is_idempotent_when_labels_exist(self):
        text = "picture 1 and picture 2 are both mentioned.\n"
        self.assertEqual(opt._ensure_fl2va_labels(text, "FL2VA", 5, "English"), text)

    def test_ensure_fl2va_labels_has_a_chinese_variant(self):
        result = opt._ensure_fl2va_labels("body", "FL2VA", 4.0, "中文")
        self.assertTrue(result.startswith("参考图像与目标视频对齐关系："))
        self.assertIn("4.00", result)


class ReplyParsingTests(unittest.TestCase):
    def test_openai_reply_reads_plain_content(self):
        data = {"choices": [{"message": {"content": "  hello  "}}]}
        self.assertEqual(opt._read_openai_reply(data), "hello")

    def test_openai_reply_joins_content_parts(self):
        data = {"choices": [{"message": {"content": [
            {"type": "text", "text": "a"}, {"type": "text", "text": {"value": "b"}}, "ignored",
        ]}}]}
        self.assertEqual(opt._read_openai_reply(data), "ab")

    def test_openai_reply_reports_reasoning_only_replies(self):
        data = {"choices": [{"message": {"content": "", "reasoning_content": "thinking"},
                             "finish_reason": "length"}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 900}}}
        with self.assertRaises(RuntimeError) as caught:
            opt._read_openai_reply(data)
        self.assertIn("仅返回了推理内容", str(caught.exception))
        self.assertIn("reasoning_tokens=900", str(caught.exception))

    def test_openai_reply_reports_empty_replies(self):
        data = {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}
        with self.assertRaises(RuntimeError) as caught:
            opt._read_openai_reply(data)
        self.assertIn("finish_reason=stop", str(caught.exception))

    def test_openai_reply_reports_missing_choices(self):
        with self.assertRaises(RuntimeError) as caught:
            opt._read_openai_reply({})
        self.assertIn("no optimized prompt", str(caught.exception))

    def test_responses_reply_prefers_output_text(self):
        self.assertEqual(opt._read_responses_reply({"output_text": " hi "}), "hi")

    def test_responses_reply_walks_the_output_tree(self):
        data = {"output": [
            {"content": [{"type": "output_text", "text": "a"}, {"type": "text", "text": {"value": "b"}},
                         {"type": "ignored", "text": "c"}]},
            "not-a-dict",
        ]}
        self.assertEqual(opt._read_responses_reply(data), "ab")

    def test_responses_reply_rejects_an_empty_payload(self):
        with self.assertRaises(RuntimeError) as caught:
            opt._read_responses_reply({})
        self.assertIn("no optimized prompt", str(caught.exception))


class ChatEndpointTests(unittest.TestCase):
    def _endpoint(self, **kwargs):
        config = {"api_url": "https://example.test/v1", "model": "m/x", "protocol": "openai"}
        config.update(kwargs)
        return opt._chat_endpoint(config)

    def test_openai_appends_the_path_once(self):
        self.assertEqual(self._endpoint(), "https://example.test/v1/chat/completions")
        self.assertEqual(
            self._endpoint(api_url="https://example.test/v1/chat/completions"),
            "https://example.test/v1/chat/completions",
        )

    def test_trailing_slashes_are_trimmed(self):
        self.assertEqual(self._endpoint(api_url="https://example.test/v1///"),
                         "https://example.test/v1/chat/completions")

    def test_gemini_quotes_the_model(self):
        url = self._endpoint(api_url="https://g.test/v1beta", protocol="gemini", model="gemini-2.5-flash")
        self.assertEqual(url, "https://g.test/v1beta/models/gemini-2.5-flash:generateContent")

    def test_gemini_keeps_an_explicit_generate_content_url(self):
        url = "https://g.test/v1beta/models/m:generateContent?alt=sse"
        self.assertEqual(self._endpoint(api_url=url, protocol="gemini"), url)

    def test_responses_protocol_appends_responses(self):
        self.assertEqual(self._endpoint(protocol="responses"), "https://example.test/v1/responses")
        self.assertEqual(self._endpoint(api_url="https://example.test/v1/responses", protocol="responses"),
                         "https://example.test/v1/responses")


class ComposeRequestTests(unittest.TestCase):
    def _compose(self, **overrides):
        config = opt._merge_settings({"provider": "openai", "api_key": "k", "read_media": False})
        config.update(overrides)
        payload = {"prompt": "雨夜街道", "task": "T2VA", "duration": 5, "media": []}
        return opt._compose_request(config, payload)

    def test_openai_body_shape(self):
        url, headers, body = self._compose()
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(headers["Authorization"], "Bearer k")
        self.assertEqual(body["temperature"], 0.35)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1]["role"], "user")
        self.assertIn("雨夜街道", body["messages"][1]["content"][0]["text"])

    def test_max_tokens_is_clamped(self):
        for value, expected in ((1, 512), (99999, 8192), (4096, 4096)):
            _url, _headers, body = self._compose(max_tokens=value)
            self.assertEqual(body["max_tokens"], expected)

    def test_gemini_moves_the_key_into_the_query_string(self):
        url, headers, body = self._compose(protocol="gemini", api_url="https://g.test/v1beta",
                                           model="gemini-2.5-flash", api_key="SECRET KEY")
        self.assertIn("key=SECRET%20KEY", url)
        self.assertNotIn("Authorization", headers)
        self.assertIn("system_instruction", body)
        self.assertEqual(body["contents"][0]["role"], "user")

    def test_gemini_converts_images_to_inline_data(self):
        config = opt._merge_settings({"provider": "openai", "api_key": "k", "read_media": True})
        config.update({"protocol": "gemini", "api_url": "https://g.test/v1beta", "model": "m"})
        payload = {"prompt": "p", "task": "T2VA", "duration": 5, "media": [
            {"label": "<picture 1>", "kind": "image", "images": ["data:image/png;base64,AAAA"]},
        ]}
        _url, _headers, body = opt._compose_request(config, payload)
        parts = body["contents"][0]["parts"]
        inline = [part for part in parts if "inline_data" in part]
        self.assertEqual(inline, [{"inline_data": {"mime_type": "image/png", "data": "AAAA"}}])

    def test_responses_body_shape(self):
        url, headers, body = self._compose(protocol="responses")
        self.assertTrue(url.endswith("/responses"))
        self.assertIn("instructions", body)
        self.assertNotIn("messages", body)
        self.assertEqual(headers["Authorization"], "Bearer k")

    def test_responses_maps_image_parts(self):
        config = opt._merge_settings({"provider": "openai", "api_key": "k", "read_media": True})
        config.update({"protocol": "responses"})
        payload = {"prompt": "p", "task": "T2VA", "duration": 5, "media": [
            {"label": "<picture 1>", "kind": "image", "images": ["data:image/png;base64,AAAA"]},
        ]}
        _url, _headers, body = opt._compose_request(config, payload)
        kinds = {part["type"] for part in body["input"][0]["content"]}
        self.assertEqual(kinds, {"input_text", "input_image"})
        image = [part for part in body["input"][0]["content"] if part["type"] == "input_image"][0]
        self.assertEqual(image["detail"], "low")

    def test_siliconflow_reasoning_models_disable_thinking(self):
        config = opt._merge_settings({"provider": "siliconflow", "api_key": "k", "read_media": False})
        config.update({"api_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-VL-72B"})
        _url, _headers, body = opt._compose_request(
            config, {"prompt": "p", "task": "T2VA", "duration": 5, "media": []})
        self.assertIs(body["enable_thinking"], False)

    def test_other_hosts_do_not_get_the_thinking_flag(self):
        config = opt._merge_settings({"provider": "dashscope", "api_key": "k", "read_media": False})
        config.update({"model": "qwen3-max"})
        _url, _headers, body = opt._compose_request(
            config, {"prompt": "p", "task": "T2VA", "duration": 5, "media": []})
        self.assertNotIn("enable_thinking", body)

    def test_read_media_toggles_image_parts(self):
        payload = {"prompt": "p", "task": "T2VA", "duration": 5, "media": [
            {"label": "<picture 1>", "kind": "image", "images": ["data:image/png;base64,AAAA"]},
        ]}
        on = opt._merge_settings({"provider": "openai", "api_key": "k", "read_media": True})
        _url, _headers, body = opt._compose_request(on, payload)
        self.assertIn("image_url", {part["type"] for part in body["messages"][1]["content"]})
        off = opt._merge_settings({"provider": "openai", "api_key": "k", "read_media": False})
        _url, _headers, body = opt._compose_request(off, payload)
        self.assertNotIn("image_url", {part["type"] for part in body["messages"][1]["content"]})

    def test_audio_media_is_label_only(self):
        parts = opt._user_message("p", [{"label": "<audio 1>", "kind": "audio"}], True)
        joined = " ".join(part["text"] for part in parts if part["type"] == "text")
        self.assertIn("content not transmitted", joined)


class GgufHelperTests(unittest.TestCase):
    def test_quantization_suffixes_are_stripped(self):
        for name in ("model-Q4_K_M.gguf", "model.Q8_0.gguf", "model-f16.gguf", "model_iq3_xxs.gguf"):
            self.assertEqual(opt._gguf_digest(name), "model", name)

    def test_publishers_and_spacing_are_normalized(self):
        self.assertEqual(opt._gguf_digest("Qwen3-VL-8B-Instruct.Q4_K_M.gguf"),
                         opt._gguf_digest("qwen3_vl_8b_instruct.gguf"))

    def test_mmproj_prefixes_are_stripped_only_for_projectors(self):
        self.assertEqual(opt._gguf_digest("mmproj-model-f16.gguf", projector=True),
                         opt._gguf_digest("model.gguf"))
        self.assertNotEqual(opt._gguf_digest("mmproj-model-f16.gguf"),
                            opt._gguf_digest("model.gguf"))

    def test_model_key_requires_family_and_size(self):
        self.assertEqual(opt._gguf_model_key("Qwen3-VL-8B-Instruct-Q4.gguf"), ("qwen3vl", "8b"))
        self.assertIsNone(opt._gguf_model_key("some-random-model.gguf"))
        self.assertIsNone(opt._gguf_model_key("Qwen3-VL-Instruct.gguf"))

    def test_pipe_names_are_limited_to_supported_families(self):
        self.assertEqual(opt._gguf_pipe_name(Path("Qwen3.5-VL-8B.gguf")), "qwen35")
        self.assertEqual(opt._gguf_pipe_name(Path("Qwen3-VL-8B.gguf")), "qwen3vl")
        self.assertIsNone(opt._gguf_pipe_name(Path("Llama-3-8B.gguf")))

    def test_mmproj_detection_needs_gguf_and_a_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "mmproj-model-f16.gguf"
            good.write_bytes(b"")
            plain = root / "model-f16.gguf"
            plain.write_bytes(b"")
            not_gguf = root / "mmproj.bin"
            not_gguf.write_bytes(b"")
            self.assertTrue(opt._looks_like_mmproj(good))
            self.assertFalse(opt._looks_like_mmproj(plain))
            self.assertFalse(opt._looks_like_mmproj(not_gguf))
            self.assertFalse(opt._looks_like_mmproj(root / "missing.gguf"))

    def test_walk_model_files_qualifies_cross_root_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "root_a", Path(tmp) / "root_b"
            first = a / "sub" / "m.gguf"
            second = b / "sub" / "m.gguf"
            for path in (first, second):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")
            walked = opt._walk_model_files([(a, first), (b, second)])
            self.assertEqual([item[2] for item in walked], ["root_a/sub/m.gguf", "root_b/sub/m.gguf"])

    def test_walk_model_files_keeps_unique_names_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alone = root / "only.gguf"
            alone.write_bytes(b"")
            walked = opt._walk_model_files([(root, alone)])
            self.assertEqual(walked[0][2], "only.gguf")

    def test_visual_detection_needs_markers_and_a_processor(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual = Path(tmp) / "vision-model"
            visual.mkdir()
            (visual / "config.json").write_text(json.dumps({"model_type": "qwen2_vl"}))
            (visual / "processor_config.json").write_text("{}")
            self.assertTrue(opt._looks_visual(visual))
            (visual / "processor_config.json").unlink()
            self.assertFalse(opt._looks_visual(visual))

    def test_visual_detection_accepts_the_tokenizer_config_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual = Path(tmp) / "vision-model"
            visual.mkdir()
            (visual / "config.json").write_text(json.dumps({"model_type": "gemma3"}))
            (visual / "tokenizer_config.json").write_text("{}")
            self.assertTrue(opt._looks_visual(visual))

    def test_visual_detection_rejects_non_visual_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "text-model"
            plain.mkdir()
            (plain / "config.json").write_text(json.dumps({"model_type": "llama"}))
            (plain / "processor_config.json").write_text("{}")
            self.assertFalse(opt._looks_visual(plain))

    def test_visual_detection_survives_broken_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken"
            broken.mkdir()
            (broken / "config.json").write_text("{not json")
            self.assertFalse(opt._looks_visual(broken))

    def test_cuda_label_formats_the_torch_version(self):
        _cuda = opt._cuda_label()
        self.assertTrue(_cuda is None or (_cuda.startswith("cu") and _cuda[2:].isdigit()))


class RhModelPageTests(unittest.TestCase):
    def _page(self, models):
        payload = ["ignored", json.dumps([models]), {"x": 1}]
        return "<script id=\"__NUXT_DATA__\" type=\"application/json\">" + json.dumps(payload) + "</script>"

    def test_models_are_extracted_from_the_nuxt_payload(self):
        models = ["openai/gpt-5.6-sol", "glm-5", "qwen/qwen3.8-max", "xai/grok-4.6", "glm-5-turbo"]
        self.assertEqual(opt._rh_models_from_html(self._page(models)), tuple(models))

    def test_duplicate_choices_are_removed(self):
        models = ["openai/gpt-5.6-sol", "glm-5", "qwen/qwen3.8-max", "xai/grok-4.6", "glm-5"]
        self.assertEqual(opt._rh_models_from_html(self._page(models)),
                         ("openai/gpt-5.6-sol", "glm-5", "qwen/qwen3.8-max", "xai/grok-4.6"))

    def test_pages_without_the_script_yield_nothing(self):
        self.assertEqual(opt._rh_models_from_html("<html></html>"), ())

    def test_broken_json_yields_nothing(self):
        self.assertEqual(
            opt._rh_models_from_html('<script id="__NUXT_DATA__">{not json}</script>'), ())

    def test_short_candidate_lists_are_ignored(self):
        self.assertEqual(opt._rh_models_from_html(self._page(["a/b", "c/d", "e/f"])), ())


class PostJsonTests(unittest.TestCase):
    class _Response:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def text(self):
            return self._body

    class _Session:
        def __init__(self, status, body, error=None):
            self._status = status
            self._body = body
            self._error = error

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            if self._error is not None:
                raise self._error
            return PostJsonTests._Response(self._status, self._body)

    def _run(self, session):
        config = opt._merge_settings({"provider": "openai", "api_key": "k", "read_media": False})
        payload = {"prompt": "p", "task": "T2VA", "duration": 5, "media": []}
        with patch.object(opt.aiohttp, "ClientTimeout", lambda **kwargs: None), \
                patch.object(opt.aiohttp, "ClientSession", lambda *a, **k: session):
            return asyncio.run(opt._post_json(config, payload))

    def test_successful_openai_reply(self):
        session = self._Session(200, json.dumps({"choices": [{"message": {"content": "ok"}}]}))
        self.assertEqual(self._run(session), "ok")

    def test_known_status_codes_use_the_hint_table(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._Session(401, "{}"))
        self.assertIn(opt._UPSTREAM_HTTP_HINTS[401], str(caught.exception))

    def test_unknown_status_codes_get_a_generic_hint(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._Session(418, "{}"))
        self.assertIn("上游拒绝了请求", str(caught.exception))
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._Session(503, "{}"))
        self.assertIn("上游服务异常", str(caught.exception))

    def test_connection_errors_are_redacted(self):
        error = opt.aiohttp.ClientConnectionError("failed for https://x.test/?key=SECRET")
        with self.assertRaises(RuntimeError) as caught:
            self._run(self._Session(200, "{}", error=error))
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIn("无法连接上游", str(caught.exception))

    def test_gemini_candidates_are_joined(self):
        config = opt._merge_settings({"provider": "gemini", "api_key": "k", "read_media": False})
        payload = {"prompt": "p", "task": "T2VA", "duration": 5, "media": []}
        body = json.dumps({"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]})
        with patch.object(opt.aiohttp, "ClientTimeout", lambda **kwargs: None), \
                patch.object(opt.aiohttp, "ClientSession", lambda *a, **k: self._Session(200, body)):
            self.assertEqual(asyncio.run(opt._post_json(config, payload)), "ab")

    def test_gemini_empty_candidates_raise(self):
        config = opt._merge_settings({"provider": "gemini", "api_key": "k", "read_media": False})
        payload = {"prompt": "p", "task": "T2VA", "duration": 5, "media": []}
        with patch.object(opt.aiohttp, "ClientTimeout", lambda **kwargs: None), \
                patch.object(opt.aiohttp, "ClientSession", lambda *a, **k: self._Session(200, "{}")):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(opt._post_json(config, payload))
        self.assertIn("Gemini", str(caught.exception))


class RouteRegistrationTests(unittest.TestCase):
    EXPECTED = [
        ("GET", "/rh/minimax-h3/prompt-optimizer/config"),
        ("GET", "/rh/minimax-h3/prompt-optimizer/models"),
        ("GET", "/rh/minimax-h3/prompt-optimizer/runninghub-models"),
        ("POST", "/rh/minimax-h3/prompt-optimizer/config"),
        ("POST", "/rh/minimax-h3/prompt-optimizer/optimize"),
        ("POST", "/rh/minimax-h3/prompt-optimizer/start"),
        ("GET", "/rh/minimax-h3/prompt-optimizer/status"),
        ("POST", "/rh/minimax-h3/prompt-optimizer/cancel"),
        ("POST", "/rh/minimax-h3/prompt-optimizer/generate"),
        ("POST", "/rh/minimax-h3/prompt-optimizer/format"),
    ]

    def test_routes_are_bound_once_with_the_expected_paths(self):
        from h3q.h3kit import optimizer as module

        recorded = []

        class Routes:
            def get(self, path):
                def wrap(handler):
                    recorded.append(("GET", path))
                    return handler
                return wrap

            def post(self, path):
                def wrap(handler):
                    recorded.append(("POST", path))
                    return handler
                return wrap

        previous = module._ROUTES_BOUND
        try:
            module._ROUTES_BOUND = False
            with patch.object(module.PromptServer, "instance", types.SimpleNamespace(routes=Routes())):
                self.assertTrue(module.register_fanout_routes())
                self.assertTrue(module.register_fanout_routes())
        finally:
            module._ROUTES_BOUND = previous
        self.assertEqual(recorded, self.EXPECTED)

    def test_registration_without_a_server_instance_reports_false(self):
        from h3q.h3kit import optimizer as module

        previous = module._ROUTES_BOUND
        try:
            module._ROUTES_BOUND = False
            with patch.object(module.PromptServer, "instance", None):
                self.assertFalse(module.register_fanout_routes())
        finally:
            module._ROUTES_BOUND = previous


if __name__ == "__main__":
    unittest.main()
