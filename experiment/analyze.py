# -*- coding: utf-8 -*-
"""
音频开头爆破音检测 + 波形图
用法:
    python analyze.py
输出:
    experiment/out/report.json                每个视频的量化指标
    experiment/out/wave_<variant>.png         每个变体的波形图（每 trial 一行）
判定口径:
    pop_likely = 开头 [0.0s, 0.3s] 内出现一个 40~250ms 的孤立高能脉冲，
                 且脉冲之后、正文声音开始之前有 >=80ms 的低能间隙。
                 辅助人工看波形图复核。
"""
import json
import os
import subprocess
import sys

import numpy as np

try:
    from imageio_ffmpeg import get_ffmpeg_exe
except ImportError:
    sys.exit("缺少依赖, 先执行: pip install imageio-ffmpeg numpy matplotlib")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "out")
REPORT = os.path.join(OUT_DIR, "report.json")

SR = 16000
HEAD_SECONDS = 2.0
FRAME_MS = 20
EPS = 1e-6


def load_head(path):
    cmd = [get_ffmpeg_exe(), "-v", "error", "-i", path, "-t", str(HEAD_SECONDS),
           "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return x


def envelope_db(x):
    n = int(SR * FRAME_MS / 1000)
    nf = len(x) // n
    if nf < 1:
        return np.array([]), []
    frames = x[:nf * n].reshape(nf, n)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    db = 20 * np.log10(rms + EPS)
    times = [(i + 0.5) * FRAME_MS / 1000.0 for i in range(nf)]
    return db, times


def frames_in(db, times, t0, t1):
    return [i for i, t in enumerate(times) if t0 <= t < t1]


def analyze_file(path):
    x = load_head(path)
    db, times = envelope_db(x)
    if len(db) == 0:
        return {"error": "no audio frames"}

    def seg_peak(t0, t1):
        idx = frames_in(db, times, t0, t1)
        return float(max(db[i] for i in idx)) if idx else None

    head_peak = seg_peak(0.01, 0.25)          # 开头残音窗口的峰值
    mid_peak = seg_peak(0.30, 0.60)           # 参考窗口峰值（对白开始前区段/早对白区段）
    body_peak = seg_peak(0.80, 1.50)          # 正文区段峰值
    mid_median = float(np.median([db[i] for i in frames_in(db, times, 0.30, 0.60)])) \
        if frames_in(db, times, 0.30, 0.60) else None

    # 突发脉冲检测
    thresh = max((mid_median if mid_median is not None else -90) + 10.0, -32.0)
    above = [i for i, t in enumerate(times) if t < 0.35 and db[i] > thresh]
    groups = []
    if above:
        cur = [above[0]]
        for i in above[1:]:
            if i - cur[-1] == 1:
                cur.append(i)
            else:
                groups.append(cur)
                cur = [i]
        groups.append(cur)

    pop_likely = False
    burst_ms = gap_ms = None
    if groups:
        g0 = groups[0]
        dur = len(g0) * FRAME_MS
        burst_ms = dur
        # 组触到分析窗口右边缘 => 声音在延续（正常对白开头），不是孤立脉冲
        truncated = times[g0[-1]] >= 0.34
        if 40 <= dur <= 250 and times[g0[0]] >= 0.0 and not truncated:
            if len(groups) == 1:
                # 0.35s 内只有这一组孤立高能帧 -> 残音
                pop_likely = True
            else:
                next_start = times[groups[1][0]]
                gap = (next_start - times[g0[-1]] - FRAME_MS / 1000) * 1000
                gap_ms = round(gap, 0)
                if gap >= 80:
                    pop_likely = True

    # 粗略找正文声音起点（> -30 dBFS 且持续）
    onset_idx = None
    for i, t in enumerate(times):
        if t >= 0.05 and db[i] > -30:
            onset_idx = i
            break

    return {
        "head_peak_db": round(head_peak, 1) if head_peak is not None else None,
        "mid_median_db": round(mid_median, 1) if mid_median is not None else None,
        "mid_peak_db": round(mid_peak, 1) if mid_peak is not None else None,
        "body_peak_db": round(body_peak, 1) if body_peak is not None else None,
        "head_mid_diff_db": round((head_peak - mid_median), 1) if head_peak is not None and mid_median is not None else None,
        "speech_onset_ms": int(times[onset_idx] * 1000) if onset_idx is not None else None,
        "burst_ms": burst_ms,
        "gap_ms": round(gap_ms, 0) if gap_ms is not None else None,
        "pop_likely": bool(pop_likely),
        "head_duration": HEAD_SECONDS,
    }


def plot_variant(variant, files, metrics_map):
    n = len(files)
    fig, axes = plt.subplots(n, 1, figsize=(14, 2.6 * n), squeeze=False)
    for ax, f in zip(axes[:, 0], files):
        m = metrics_map[f]
        x = load_head(f)
        db, times = envelope_db(x)
        ax.plot(times, db, color="#2b6cb0", lw=0.9)
        ax.axhline(-30, color="#888", lw=0.6, ls=":")
        if m.get("pop_likely"):
            ax.axvspan(0, 0.35, color="#e53e3e", alpha=0.12)
        label = os.path.splitext(os.path.basename(f))[0]
        verdict = "POP!" if m.get("pop_likely") else "clean"
        ax.set_title(f"{label}  [{verdict}]  head={m.get('head_peak_db')}dB  "
                     f"mid={m.get('mid_median_db')}dB  burst={m.get('burst_ms')}ms", fontsize=10)
        ax.set_xlim(0, 1.2)
        ax.set_ylim(-80, 0)
        ax.set_xlabel("time (s)")
        ax.set_ylabel("RMS (dBFS)")
    fig.tight_layout()
    out = os.path.join(OUT_DIR, f"wave_{variant}.png")
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(f"plot -> {out}")


def main():
    files = sorted(f for f in os.listdir(OUT_DIR) if f.endswith(".mp4"))
    if not files:
        sys.exit("out 目录下没有 mp4，请先运行 run_experiment.py")

    report = {}
    for f in files:
        path = os.path.join(OUT_DIR, f)
        print(f"analyze {f}")
        try:
            report[f] = analyze_file(path)
        except Exception as e:
            report[f] = {"error": str(e)}

    json.dump(report, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"report -> {REPORT}")

    variants = sorted({f.rsplit("_", 1)[0] for f in files})
    for v in variants:
        vfiles = [os.path.join(OUT_DIR, f) for f in files if f.startswith(v + "_")]
        plot_variant(v, vfiles, report)

    # 统计各变体爆破率
    print("\n===== pop rate =====")
    for v in variants:
        hits = sum(1 for f in files if f.startswith(v + "_") and report[f].get("pop_likely"))
        total = sum(1 for f in files if f.startswith(v + "_"))
        print(f"{v}: {hits}/{total}")


if __name__ == "__main__":
    main()
