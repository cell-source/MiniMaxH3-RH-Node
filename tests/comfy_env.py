"""测试用的 ComfyUI 运行时引导。

定位 ComfyUI 检出目录、强制 CPU 模式、把仓库根目录加入 ``sys.path``，供需要
真实 ComfyUI 依赖（``comfy.utils`` / ``comfy.model_management`` /
``comfy_extras``）的测试复用。

定位顺序：环境变量 ``COMFYUI_PATH`` → 同级目录 ``../ComfyUI`` →
上两级目录下的 ``ComfyUI``。都找不到时 ``COMFY`` 为 ``None``，
使用方应以 ``skipUnless``/``skipIf`` 整体跳过，而不是让收集阶段报错。

注意：在没有 CUDA 的机器上，ComfyUI 默认参数会让 ``comfy.model_management``
在导入时尝试初始化 CUDA 并直接抛错。ComfyUI 只在**首次导入 ``comfy.cli_args``**
时解析命令行，因此这里把 ``--cpu`` 交给它，并在解析完成后立即恢复测试框架
自己的 ``sys.argv``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _locate_comfy() -> Path | None:
    override = os.environ.get("COMFYUI_PATH")
    if override and (Path(override) / "comfy").is_dir():
        return Path(override)
    for guess in (ROOT.parent / "ComfyUI", ROOT.parent.parent / "ComfyUI"):
        if (guess / "comfy").is_dir():
            return guess
    return None


COMFY = _locate_comfy()
READY = False

if COMFY is not None:
    sys.path[:0] = [str(COMFY), str(ROOT)]
    _test_argv = sys.argv
    sys.argv = [sys.argv[0], "--cpu"]
    try:
        import comfy.options

        comfy.options.enable_args_parsing()
        import comfy.cli_args  # noqa: F401  触发解析（此刻 argv 带 --cpu）

        READY = True
    finally:
        sys.argv = _test_argv

SKIP_REASON = "ComfyUI checkout not found (set COMFYUI_PATH to run this test)"
