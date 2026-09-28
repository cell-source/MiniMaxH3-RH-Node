"""Allowlist-only distribution: never ship the original HTML or local API keys.

prompt_engine 以仓库内容为真源（2026-09-26）：打包**不再重建**它，并在打包后校验 zip
内的产物与仓库文件逐字节一致——之前每次打包都会用参考 HTML 覆盖 rules.js，把已修的
引擎缺陷静默放回发布包。
"""
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FILES = ["__init__.py", "prompt_runtime.py", "llm_client.py", "video_nodes.py",
         "core.py", "conditioning.py", "audio_ops.py", "sampling.py", "tiled_sampler.py", "latent_upscaler.py",
         "latent_upscaler_core.py", "prompt_optimizer.py", "prompt_tags.py", "requirements.txt", "LICENSE",
         "README.md", "NOTICE.md"]
DOC_FILES = ["docs/RELEASE-TO-RUNNINGHUB.md"]
FOLDERS = ["prompt_engine", "web", "locales", "examples"]


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
    for name in FOLDERS:
        paths.extend(p for p in (ROOT / name).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
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
