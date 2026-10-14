"""去相似化进度度量：把本仓库文件与上游对应文件按"规范化行序列"比对。

背景：RunningHub 曾以"与公开项目过于相似"为由拒绝发布，因此本仓库的
Python 模块与前端面板都按"等价重写"的方式改造（见 docs/去相似化改造-2026-10-14.md）。
本脚本给出两样东西：

1. 整体相似度 —— difflib 的 SequenceMatcher.ratio()，比较的是**去掉空行与
   整行注释、压缩空白**之后的行序列，所以纯排版改动不影响读数。
2. 仍逐字相同的行段清单（默认 >= 12 行）—— 这是继续改造时的工作队列，
   按长度降序打印，并给出每一段在本仓库文件里的行号。

默认按"每个本仓库文件 × 每个上游文件"求相似度并取最高者作为对应关系
（本仓库的模块名与上游已完全不同，写死映射必然失效）。用法::

    python scripts/measure_similarity.py            # 总表
    python scripts/measure_similarity.py --list     # 附带工作队列
    python scripts/measure_similarity.py --pair web/js/h3kit_panel.js web/js/minimax_h3_integration.js --list

上游仓库需先克隆到本地，或用 ``--upstream-root`` / 环境变量 ``H3_UPSTREAM_ROOT``
指定路径；找不到上游时只有 ``--pair`` 模式可用。
"""

from __future__ import annotations

import argparse
import difflib
import os
import re

UPSTREAM_ROOT = os.environ.get(
    "H3_UPSTREAM_ROOT",
    r"D:\WorkStation\ProjectsVSCode\MiniMaxTool\ComfyUI\custom_nodes\MiniMaxH3-Integration-GHX",
)

# 参与比对的本仓库文件（相对仓库根）。
LOCAL_FILES = [
    "h3kit/geometry.py",
    "h3kit/assembly.py",
    "h3kit/muxing.py",
    "h3kit/dualtime.py",
    "h3kit/tagging.py",
    "h3kit/optimizer.py",
    "h3kit/llm.py",
    "h3kit/rules.py",
    "h3kit/node.py",
    "web/js/h3kit_panel.js",
]
# 上游候选文件（相对上游根）。
UPSTREAM_FILES = [
    "core.py",
    "conditioning.py",
    "audio_ops.py",
    "sampling.py",
    "prompt_tags.py",
    "prompt_optimizer.py",
    "llm_client.py",
    "prompt_runtime.py",
    "nodes.py",
    "web/js/minimax_h3_integration.js",
]

SKIP_PREFIXES = ("//", "#", "/*", "*", '"""', "'''")


def normalized_lines(path: str) -> list[tuple[int, str]]:
    """返回 (原始行号, 规范化文本)；丢弃空行与整行注释。"""
    with open(path, encoding="utf-8") as handle:
        raw = handle.read().splitlines()
    kept: list[tuple[int, str]] = []
    for number, line in enumerate(raw, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(SKIP_PREFIXES):
            continue
        kept.append((number, re.sub(r"\s+", " ", stripped)))
    return kept


def match(local_lines: list[tuple[int, str]], upstream_lines: list[tuple[int, str]]):
    matcher = difflib.SequenceMatcher(
        None,
        [text for _, text in upstream_lines],
        [text for _, text in local_lines],
        autojunk=False,
    )
    return matcher.ratio(), matcher


def report(local_path: str, upstream_path: str, threshold: int, limit: int) -> float:
    local = normalized_lines(local_path)
    upstream = normalized_lines(upstream_path)
    ratio, matcher = match(local, upstream)
    print(
        "%-24s vs %-34s %.3f   (local %4d / upstream %4d lines)"
        % (local_path, os.path.basename(upstream_path), ratio, len(local), len(upstream))
    )
    if not limit:
        return ratio

    runs = sorted(
        (block for block in matcher.get_matching_blocks() if block.size >= threshold),
        key=lambda block: -block.size,
    )
    print(
        "    identical runs >= %d lines: %d, total %d lines"
        % (threshold, len(runs), sum(block.size for block in runs))
    )
    for block in runs[:limit]:
        first = local[block.b][0]
        last = local[block.b + block.size - 1][0]
        print("    --- %d lines  local %d-%d" % (block.size, first, last))
        for _, text in local[block.b : block.b + 3]:
            print("        %s" % text[:100])
    return ratio


def best_match(local_path: str, upstream_root: str, threshold: int, limit: int) -> tuple[str, float]:
    """在全部上游文件里挑与本文件最相似的一个。"""
    local = normalized_lines(local_path)
    best_path, best_ratio = "", 0.0
    for relative in UPSTREAM_FILES:
        candidate = os.path.join(upstream_root, relative)
        if not os.path.isfile(candidate):
            continue
        ratio, _ = match(local, normalized_lines(candidate))
        if ratio > best_ratio:
            best_path, best_ratio = candidate, ratio
    if not best_path:
        return "", 0.0
    ratio = report(local_path, best_path, threshold, limit)
    return best_path, ratio


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream-root", default=UPSTREAM_ROOT, help="上游仓库根目录")
    parser.add_argument("--pair", nargs=2, metavar=("LOCAL", "UPSTREAM"), help="只比对一对文件")
    parser.add_argument("--list", action="store_true", help="打印仍逐字相同的行段（工作队列）")
    parser.add_argument("--threshold", type=int, default=12, help="行段最小长度，默认 12")
    parser.add_argument("--limit", type=int, default=20, help="每个文件最多打印几段")
    args = parser.parse_args()
    limit = args.limit if args.list else 0

    if args.pair:
        local, upstream = args.pair
        if not os.path.isabs(upstream):
            upstream = os.path.join(args.upstream_root, upstream)
        report(local, upstream, args.threshold, limit)
        return

    existing = [name for name in UPSTREAM_FILES if os.path.isfile(os.path.join(args.upstream_root, name))]
    if not existing:
        parser.error("上游文件都不存在，请用 --upstream-root 指向克隆好的上游仓库")

    ratios: list[tuple[float, str, str]] = []
    for local_path in LOCAL_FILES:
        if not os.path.isfile(local_path):
            print("%-24s (本仓库没有这个文件，跳过)" % local_path)
            continue
        upstream_path, ratio = best_match(local_path, args.upstream_root, args.threshold, limit)
        if upstream_path:
            ratios.append((ratio, local_path, upstream_path))

    if not ratios:
        return
    worst = max(ratios)
    print(
        "\n%d files measured | mean %.3f | worst %.3f (%s)"
        % (len(ratios), sum(item[0] for item in ratios) / len(ratios), worst[0], worst[1])
    )


if __name__ == "__main__":
    main()
