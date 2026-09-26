"""示例一致性：五套示例必须与生成脚本（含引擎）的输出逐字节一致。

背景（2026-09-26 上线评估 A3）：画布示例曾经只给 CreateVideo/SaveVideo 写
widgets_values，主节点与 UNETLoader 落成 null，导入后是空节点；文档却声称示例带
`SELECT_YOUR_*` 占位与 offline/format 来源。此处把“示例 = 生成结果”固化下来。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_example_workflow as generator  # noqa: E402

MODES = ("t2va", "i2va", "fl2va", "l2va", "ref2va")


class ExampleConsistencyTests(unittest.TestCase):
    def test_examples_match_the_generator_output(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            with patch.object(generator, "ROOT", target):
                generator.main()
            for name in sorted(p.name for p in (ROOT / "examples").glob("*.json")):
                with self.subTest(example=name):
                    self.assertEqual(
                        (target / "examples" / name).read_bytes(),
                        (ROOT / "examples" / name).read_bytes(),
                        f"{name} 与生成结果不一致：请运行 python scripts/build_example_workflow.py",
                    )

    def test_canvas_examples_carry_every_widget_and_no_credentials(self):
        order = generator.AIO_WIDGET_ORDER
        self.assertEqual(len(order), len(set(order)), "AIO_WIDGET_ORDER 不得有重复项")
        for mode in MODES:
            with self.subTest(mode=mode):
                data = json.loads((ROOT / f"examples/{mode}.workflow.json").read_text(encoding="utf-8"))
                node = next(n for n in data["nodes"] if n["type"] == "MiniMaxH3IntegrationRH")
                unet = next(n for n in data["nodes"] if n["type"] == "UNETLoader")
                values = node["widgets_values"]
                self.assertEqual(len(values), len(order), "画布示例的 widgets_values 长度必须等于节点 widget 数")
                mapped = dict(zip(order, values))
                self.assertEqual(mapped["prompt_source"], "panel")
                self.assertEqual(mapped["ai_api_key"], "")
                self.assertTrue(mapped["prompt"], "画布示例必须把完整提示词写进编辑器")
                self.assertFalse([v for v in values if v is None], "画布示例不得有 null 参数")
                self.assertEqual(unet["widgets_values"],
                                 ["SELECT_YOUR_H3_DIFFUSION_MODEL.safetensors", "default"])
                self.assertEqual(node["inputs"][0]["name"], "model")

    def test_media_placeholders_match_the_documented_wiring(self):
        expected = {
            "t2va": {},
            "i2va": {"first_frame": "SELECT_YOUR_FIRST_FRAME.png"},
            "fl2va": {"first_frame": "SELECT_YOUR_FIRST_FRAME.png",
                      "last_frame": "SELECT_YOUR_LAST_FRAME.png"},
            "l2va": {"last_frame": "SELECT_YOUR_LAST_FRAME.png"},
            "ref2va": {"ref_image_1": "SELECT_YOUR_REFERENCE_IMAGE_1.png",
                       "ref_video_1": "SELECT_YOUR_REFERENCE_VIDEO_1.mp4",
                       "ref_audio_1": "SELECT_YOUR_REFERENCE_AUDIO_1.wav"},
        }
        order = generator.AIO_WIDGET_ORDER
        slots = (["first_frame", "last_frame", "hybrid_audio"]
                 + [f"ref_image_{i}" for i in range(1, 10)]
                 + [f"ref_video_{i}" for i in range(1, 4)]
                 + [f"ref_audio_{i}" for i in range(1, 4)])
        self.assertFalse([slot for slot in slots if slot not in order], "素材槽必须都是节点 widget")
        for mode, wanted in expected.items():
            with self.subTest(mode=mode):
                data = json.loads((ROOT / f"examples/{mode}.workflow.json").read_text(encoding="utf-8"))
                node = next(n for n in data["nodes"] if n["type"] == "MiniMaxH3IntegrationRH")
                mapped = dict(zip(order, node["widgets_values"]))
                self.assertEqual({slot: mapped[slot] for slot in slots if mapped[slot]}, wanted)

    def test_api_examples_keep_the_engine_sources(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                data = json.loads((ROOT / f"examples/{mode}.api.json").read_text(encoding="utf-8"))
                node = next(v for v in data.values() if v.get("class_type") == "MiniMaxH3IntegrationRH")
                inputs = node["inputs"]
                self.assertEqual(inputs["prompt_source"], "format" if mode == "ref2va" else "offline")
                self.assertTrue(inputs["ai_text"], "API 示例必须带创意/待整理文本")
                self.assertEqual(inputs["ai_api_key"], "")

    def test_generator_rejects_unknown_widgets(self):
        values = dict(generator.AIO_BASE)
        for name in generator.AIO_WIDGET_ORDER:
            values.setdefault(name, generator.AIO_WIDGET_DEFAULTS.get(name))
        self.assertEqual(len(generator.aio_widget_values(values)), len(generator.AIO_WIDGET_ORDER))
        with self.assertRaises(RuntimeError):
            generator.aio_widget_values({"main_mode": "text_keyframes"})


if __name__ == "__main__":
    unittest.main()
