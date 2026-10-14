"""Allowlist-only distribution: never ship the original HTML or local API keys.

发布包只包含运行期真正需要的文件：顶层入口 + ``h3kit`` 子包 + 静态资源。
``optional/``（二段放大与分块采样，未注册）与 ``tests``/``scripts``/``experiment``
明确不入包，避免把未启用的实现一并分发。

prompt_engine 以仓库内容为真源（2026-09-26）：打包**不再重建**它，并在打包后校验 zip
内的产物与仓库文件逐字节一致——之前每次打包都会用参考 HTML 覆盖 rules.js，把已修的
引擎缺陷静默放回发布包。
"""
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FILES = ["__init__.py", "requirements.txt", "LICENSE", "README.md", "NOTICE.md"]
DOC_FILES = ["docs/RELEASE-TO-RUNNINGHUB.md"]
FOLDERS = ["h3kit", "prompt_engine", "web", "locales", "examples"]


def _verify_engine_matches_repo(output: Path) -> None:
    """Fail loudly if the packaged engine differs from the repository copy (2026-09-26 评估 A1）。"""
    with ZipFile(output) as archive:
        for relative in ("prompt_engine/rules.js", "prompt_engine/defaults.json"):
            packed = archive.read("MiniMaxH3-RH-Node/" + relative)
            if packed != (ROOT / relative).read_bytes():
                raise RuntimeError(
                    f"packaged {relative} differs from the repository copy; "
                    "packaging must never regenerate prompt_engine"
                )
    print("prompt_engine verified byte-identical to the repository copy")


def package():
    paths = [ROOT / name for name in FILES + DOC_FILES]
    # 白名单是整目录收录：编辑器备份/临时脚本若落在 h3kit/ 等目录内会被带进发布包
    # （曾发生 optimizer.py.bak 入包、zip 从 268 KB 涨到 351 KB 的事故），这里防御性排除。
    blocked_parts = {"__pycache__", ".git", ".vscode", ".pytest_cache"}
    blocked_suffixes = (".bak", ".tmp", ".orig", ".rej")
    for name in FOLDERS:
        paths.extend(
            p for p in (ROOT / name).rglob("*")
            if p.is_file()
            and not (blocked_parts & set(p.parts))
            and not p.name.endswith(blocked_suffixes)
            and not p.name.startswith("_tmp")
        )
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    output = dist / "MiniMaxH3-RH-Node.zip"
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, "MiniMaxH3-RH-Node/" + path.relative_to(ROOT).as_posix())
    _verify_engine_matches_repo(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    print(f"Package: {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    package()
