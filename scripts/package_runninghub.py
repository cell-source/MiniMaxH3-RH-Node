"""Allowlist-only distribution: never ship the original HTML or local API keys."""
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FILES = ["__init__.py", "prompt_runtime.py", "llm_client.py", "video_nodes.py",
         "core.py", "conditioning.py", "audio_ops.py", "sampling.py", "tiled_sampler.py", "latent_upscaler.py",
         "latent_upscaler_core.py", "prompt_optimizer.py", "prompt_tags.py", "requirements.txt", "LICENSE",
         "README.md", "NOTICE.md", "UPSTREAM-GHX-README.md", "RELEASE-TO-RUNNINGHUB.md"]
FOLDERS = ["prompt_engine", "web", "locales", "examples"]


def package():
    from build_prompt_engine import build
    build()
    paths = [ROOT / name for name in FILES]
    for name in FOLDERS:
        paths.extend(p for p in (ROOT / name).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    output = dist / "MiniMaxH3-RH-Node.zip"
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, "MiniMaxH3-RH-Node/" + path.relative_to(ROOT).as_posix())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(digest + "  " + output.name + "\n", encoding="ascii")
    print(f"Package: {output} ({output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    package()
