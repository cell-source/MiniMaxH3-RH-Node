"""Bootstrap tool: derive prompt_engine from the original single-file HTML tool.

2026-09-26 起 `prompt_engine/rules.js` 以**仓库内容为真源**（直接在该文件上维护）。
参考 HTML 仅是最初的输入参考，不参与发布流程；打包也不重建产物（见
scripts/package_runninghub.py 的逐字节自校验）。本脚本只用于需要重新引导的场合：
目标产物与生成结果不一致时默认**拒绝覆盖**，需显式 `--force`。
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = ("// prompt_engine rules — maintained in this repository."
          " Bootstrapped from the reference HTML tool by scripts/build_prompt_engine.py;"
          " see prompt_engine/source.sha256.\n")


def build(force: bool = False) -> None:
    html = (ROOT / "MiniMax-H3-提示词生成器.html").read_text(encoding="utf-8")
    main = next(s for s in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "function i2vaAlignLine" in s)
    main = main.split("/* ==================== 初始化")[0]
    for name in ("_decodeKey", "_decodeKeyGlm"):
        main, count = re.subn(r"function " + name + r"\(\) \{.*?\n\}",
                             "function " + name + '() { return "server-managed"; }', main, flags=re.S)
        if count != 1:
            raise RuntimeError("Credential removal failed: " + name)
    defaults = {}
    for match in re.finditer(r'<(input|select|textarea|div|span|button|output)[^>]*\bid="([^"]+)"[^>]*>', html):
        value = re.search(r'value="([^"]*)"', match[0])
        defaults[match[2]] = {"value": value[1] if value else "", "checked": bool(re.search(r"\bchecked\b", match[0]))}
    output = ROOT / "prompt_engine"
    output.mkdir(exist_ok=True)
    rules_path = output / "rules.js"
    generated = HEADER + main
    if rules_path.exists() and not force:
        current = rules_path.read_text(encoding="utf-8")
        if current != generated:
            raise SystemExit(
                "prompt_engine/rules.js differs from the reference HTML.\n"
                "It is the maintained source of truth and must not be overwritten by the\n"
                "bootstrap tool: port your changes into the HTML first, or pass --force to\n"
                "discard the repository version."
            )
    rules_path.write_text(generated, encoding="utf-8")
    (output / "defaults.json").write_text(json.dumps(defaults, ensure_ascii=False), encoding="utf-8")
    (output / "source.sha256").write_text(hashlib.sha256(html.encode()).hexdigest() + "\n", encoding="ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="overwrite the maintained prompt_engine/rules.js with the HTML-derived version")
    build(force=parser.parse_args().force)
