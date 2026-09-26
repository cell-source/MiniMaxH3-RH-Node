"""打包契约：prompt_engine 以仓库内容为真源，打包与引导脚本都不得静默覆盖它。

背景（2026-09-26 上线评估 A1）：rules.js 由 scripts/build_prompt_engine.py 从参考 HTML
生成，而引擎修复只改在产物上；打包时自动 build() 会把修复悄悄替换回旧实现。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

REFERENCE_HTML = """<html><body><script>
function i2vaAlignLine() { return 1; }
function _decodeKey() {
  return 'secret';
}
function _decodeKeyGlm() {
  return 'secret';
}
/* ==================== 初始化
</script></body></html>
"""


class PackagingContractTests(unittest.TestCase):
    def test_packaging_never_regenerates_the_engine(self):
        source = (ROOT / "scripts/package_runninghub.py").read_text(encoding="utf-8")
        self.assertNotIn("build_prompt_engine", source)
        self.assertNotIn("build()", source)
        self.assertIn("_verify_engine_matches_repo", source)

    def test_packaging_verifies_the_packaged_engine(self):
        import package_runninghub

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "prompt_engine").mkdir()
            (root / "prompt_engine/rules.js").write_bytes(b"// repo rules\n")
            (root / "prompt_engine/defaults.json").write_bytes(b"{}")
            output = root / "fake.zip"
            from zipfile import ZipFile, ZIP_DEFLATED
            with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                archive.writestr("MiniMaxH3-RH-Node/prompt_engine/rules.js", b"// stale rules\n")
                archive.writestr("MiniMaxH3-RH-Node/prompt_engine/defaults.json", b"{}")
            with patch.object(package_runninghub, "ROOT", root):
                with self.assertRaises(RuntimeError) as caught:
                    package_runninghub._verify_engine_matches_repo(output)
            self.assertIn("must never regenerate prompt_engine", str(caught.exception))

    def test_bootstrap_refuses_to_overwrite_the_maintained_rules(self):
        import build_prompt_engine

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "prompt_engine").mkdir()
            (root / "MiniMax-H3-提示词生成器.html").write_text(REFERENCE_HTML, encoding="utf-8")
            rules = root / "prompt_engine/rules.js"
            rules.write_text("// maintained in repo\n", encoding="utf-8")
            with patch.object(build_prompt_engine, "ROOT", root):
                with self.assertRaises(SystemExit) as caught:
                    build_prompt_engine.build()
                self.assertIn("--force", str(caught.exception))
                self.assertEqual(rules.read_text(encoding="utf-8"), "// maintained in repo\n")
                build_prompt_engine.build(force=True)
            rewritten = rules.read_text(encoding="utf-8")
            self.assertTrue(rewritten.startswith("// prompt_engine rules — maintained in this repository."))
            self.assertIn('return "server-managed";', rewritten)
            self.assertNotIn("secret", rewritten)

    def test_repository_rules_are_not_marked_as_generated(self):
        first_line = (ROOT / "prompt_engine/rules.js").read_text(encoding="utf-8").splitlines()[0]
        self.assertNotIn("do not edit", first_line)
        self.assertIn("maintained in this repository", first_line)


if __name__ == "__main__":
    unittest.main()
