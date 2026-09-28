"""打包契约：prompt_engine 以仓库内容为真源，打包流程不得重建或静默覆盖它。

背景（2026-09-26 上线评估 A1）：引擎修复曾只改在生成产物上，打包时自动重建会把
修复悄悄替换回旧实现；此后打包改为白名单复制 + 逐字节校验。参考生成器 HTML 已
移出本仓库（维护于上游主项目 MiniMaxH3），历史引导脚本 build_prompt_engine.py
随之删除：引擎与参考 HTML 的差异（维护修复）只能通过移植 hunk 同步，整文件重建
会回退全部修复。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


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

    def test_repository_rules_are_not_marked_as_generated(self):
        first_line = (ROOT / "prompt_engine/rules.js").read_text(encoding="utf-8").splitlines()[0]
        self.assertNotIn("do not edit", first_line)
        self.assertIn("maintained in this repository", first_line)


if __name__ == "__main__":
    unittest.main()
