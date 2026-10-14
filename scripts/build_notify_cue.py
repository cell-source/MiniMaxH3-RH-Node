# -*- coding: utf-8 -*-
"""生成面板「提示词生成完成」提示音。

本资源是本仓库自制的合成音（两音上行铃声），与上游项目不共用任何音频文件。
运行：python scripts/build_notify_cue.py
输出：web/audio/ready-cue.wav（22.05 kHz / 16-bit / 单声道）
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "audio" / "ready-cue.wav"

RATE = 22050
DURATION = 0.46
# (起始秒, 持续秒, 基频 Hz, 峰值幅度)
NOTES = [
    (0.00, 0.16, 1046.50, 0.62),   # C6
    (0.11, 0.35, 1567.98, 0.70),   # G6
]


def _envelope(t: float, span: float) -> float:
    """指数衰减 + 3 ms 起音 / 30 ms 收尾，避免爆音。"""
    attack = 0.003 if t < 0.003 else 1.0
    release = 1.0
    if span - t < 0.03:
        release = max(0.0, (span - t) / 0.03)
    return attack * release * math.exp(-5.5 * t)


def render() -> list[float]:
    total = int(RATE * DURATION)
    buffer = [0.0] * total
    for start, span, freq, amp in NOTES:
        begin = int(RATE * start)
        for index in range(int(RATE * span)):
            position = begin + index
            if position >= total:
                break
            t = index / RATE
            value = math.sin(2 * math.pi * freq * t)
            value += 0.22 * math.sin(2 * math.pi * freq * 2 * t)   # 二次谐波，声音更有质感
            buffer[position] += amp * value * _envelope(t, span)
    peak = max(abs(v) for v in buffer) or 1.0
    scale = 0.82 / peak
    return [v * scale for v in buffer]


def main() -> None:
    samples = render()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767)) for v in samples))
    print(f"cue written: {OUT} ({OUT.stat().st_size:,} bytes, {DURATION:.2f}s)")


if __name__ == "__main__":
    main()
