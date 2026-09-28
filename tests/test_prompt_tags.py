"""prompt_tags no-subtitle constraint sync with the prompt_engine (v6).

背景（2026-09-28 v6 同步）：panel 路径的运行期后处理 apply_advanced_constraints
与 quickjs 引擎各自维护约束句。若两侧文本不一致，引擎生成（或历史版本遗留）的
约束块无法被剥离，开关一次就会累积两条约束。v6 起两侧英文常量逐字一致，且
LEGACY 变体（v3/v4/v4.1/v5 中英 + v6 中文 + 旧精简版）全部可剥离。
"""
import re
import unittest
from pathlib import Path

import prompt_tags
from prompt_tags import NO_SUBTITLES_CONSTRAINT, NO_SUBTITLES_LEGACY_CONSTRAINTS, apply_advanced_constraints

ROOT = Path(__file__).resolve().parents[1]


class NoSubtitleConstraintSyncTests(unittest.TestCase):
    @staticmethod
    def _engine_constants():
        rules = (ROOT / "prompt_engine/rules.js").read_text(encoding="utf-8")
        en = re.search(r"const NO_SUBTITLES_CONSTRAINT = '(.+?)';", rules).group(1)
        zh = re.search(r"const NO_SUBTITLES_CONSTRAINT_ZH = '(.+?)';", rules).group(1)
        legacy_block = re.search(r"const LEGACY_NO_SUBTITLES_CONSTRAINTS = \[(.*?)\];", rules, re.S).group(1)
        return en, zh, re.findall(r'"((?:[^"\\]|\\.)*)"', legacy_block)

    def test_python_constant_is_byte_identical_to_the_engine(self):
        en, _, _ = self._engine_constants()
        self.assertEqual(NO_SUBTITLES_CONSTRAINT, en)

    def test_engine_legacy_variants_all_strip_cleanly(self):
        _, zh, legacy = self._engine_constants()
        for block in legacy + [zh]:
            self.assertIn(block, NO_SUBTITLES_LEGACY_CONSTRAINTS)

    def test_enabled_toggle_never_accumulates_two_constraint_blocks(self):
        prompt = ("integrated_multimodal_description: [Shot 1] 女子站着。 "
                  + NO_SUBTITLES_LEGACY_CONSTRAINTS[2]  # 引擎 v4 英文块
                  + "\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A")
        once = apply_advanced_constraints(prompt, no_subtitle=True)
        self.assertNotIn(NO_SUBTITLES_LEGACY_CONSTRAINTS[2], once)
        self.assertEqual(once.count(NO_SUBTITLES_CONSTRAINT), 1)
        self.assertEqual(apply_advanced_constraints(once, no_subtitle=True), once)

    def test_disabled_toggle_removes_every_known_variant(self):
        prompt = ("integrated_multimodal_description: [Shot 1] 女子站着。 "
                  + NO_SUBTITLES_CONSTRAINT
                  + "\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A")
        stripped = apply_advanced_constraints(prompt, no_subtitle=False)
        self.assertNotIn(NO_SUBTITLES_CONSTRAINT, stripped)
        for block in NO_SUBTITLES_LEGACY_CONSTRAINTS:
            self.assertNotIn(block, apply_advanced_constraints(
                prompt.replace(NO_SUBTITLES_CONSTRAINT, block), no_subtitle=False))


if __name__ == "__main__":
    unittest.main()
