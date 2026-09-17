# -*- coding: utf-8 -*-
"""
离线自测：不调用 MiniMax API，验证提示词约束、请求体构造与爆破音检测器。
用法: python selftest.py
"""
import base64
import importlib.util
import json
import os
import subprocess
import sys

from imageio_ffmpeg import get_ffmpeg_exe

HERE = os.path.dirname(os.path.abspath(__file__))
SELFTEST = os.path.join(HERE, "selftest")
os.makedirs(SELFTEST, exist_ok=True)
FFMPEG = get_ffmpeg_exe()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


run = load_module("run_experiment", os.path.join(HERE, "run_experiment.py"))

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if detail else ""))
    if not ok:
        failures.append(name)


# ---- 1. 提示词约束 ----
for v, p in run.PROMPTS.items():
    check(f"prompt {v} <= 7000 chars", len(p) <= 7000, f"{len(p)} chars")
    check(f"prompt {v} keeps dialogue verbatim", "西洲，你别告诉我，你跟猎鹰基金有关系？" in p)
    check(f"prompt {v} has 6 ref2va sections", all(s in p for s in
          ("subject_definitions", "summary", "retention_analysis",
           "detailed_description", "overall_soundscape", "non_diegetic_music")))
check("V0 keeps <d> tag", "<d>" in run.PROMPTS["V0"])
check("V1 drops <d> tag", "<d>" not in run.PROMPTS["V1"])
check("V2 has ambient bed", "runs continuously" in run.PROMPTS["V2"])
check("V0/V1 soundscape is N/A", "overall_soundscape:\nN/A" in run.PROMPTS["V0"])

# ---- 2. 请求体构造 ----
from PIL import Image
img_path = os.path.join(SELFTEST, "tiny.png")
Image.new("RGB", (64, 64), (128, 128, 128)).save(img_path)
b64 = base64.b64encode(open(img_path, "rb").read()).decode("ascii")
uri = f"data:image/png;base64,{b64}"
for v, p in run.PROMPTS.items():
    body = {
        "model": "MiniMax-H3",
        "content": [
            {"type": "text", "text": p},
            {"type": "image_url", "image_url": {"url": uri}, "role": "reference_image"},
        ],
        "resolution": "768P",
        "duration": 5,
        "ratio": "adaptive",
    }
    s = json.dumps(body, ensure_ascii=False).encode("utf-8")
    check(f"body {v} json ok", True, f"{len(s)} bytes")
    check(f"body {v} < 64MB", len(s) < 64 * 1024 * 1024, f"{len(s)} bytes")

# ---- 3. 合成音频检测 ----
# bundled ffmpeg 4.2.2 的 eval 没有 gt/gte 比较函数, 改用 numpy 生成 WAV 再封装
import numpy as np
import wave

SR = 16000
DUR = 2.0


def make_clip(name, fn):
    t = np.arange(int(SR * DUR)) / SR
    x = np.clip(fn(t), -1, 1)
    x16 = (x * 32767).astype("<i2")
    wav = os.path.join(SELFTEST, name.replace(".mp4", ".wav"))
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(x16.tobytes())
    out = os.path.join(SELFTEST, name)
    subprocess.run([
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", "color=black:s=320x240:d=2:r=24",
        "-i", wav, "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", out
    ], check=True)
    return out


pop = make_clip("pop_test.mp4",
    lambda t: np.where((t >= 0.10) & (t <= 0.20), 0.9 * np.sin(2 * np.pi * 1000 * t),
              np.where((t >= 0.60) & (t <= 1.50), 0.3 * np.sin(2 * np.pi * 440 * t), 0.0)))
clean = make_clip("clean_test.mp4",
    lambda t: np.where((t >= 0.50) & (t <= 1.50), 0.3 * np.sin(2 * np.pi * 440 * t), 0.0))
speech = make_clip("speech_start_test.mp4",
    lambda t: np.where(t >= 0.05, 0.3 * np.sin(2 * np.pi * 440 * t), 0.0))

analyze = load_module("analyze", os.path.join(HERE, "analyze.py"))
analyze.OUT_DIR = SELFTEST
r_pop = analyze.analyze_file(pop)
r_clean = analyze.analyze_file(clean)
r_speech = analyze.analyze_file(speech)

check("pop clip flagged", r_pop.get("pop_likely") is True, json.dumps(r_pop, ensure_ascii=False))
check("clean clip not flagged", r_clean.get("pop_likely") is False, json.dumps(r_clean, ensure_ascii=False))
check("speech-from-0 not flagged", r_speech.get("pop_likely") is False, json.dumps(r_speech, ensure_ascii=False))
check("pop head peak high", (r_pop.get("head_peak_db") or -99) > -10, str(r_pop.get("head_peak_db")))
check("clean head peak low", (r_clean.get("head_peak_db") or -99) < -40, str(r_clean.get("head_peak_db")))

# ---- 4. 波形图生成 ----
analyze.plot_variant("selftest", [pop, clean, speech], {pop: r_pop, clean: r_clean, speech: r_speech})
check("plot png generated", os.path.exists(os.path.join(SELFTEST, "wave_selftest.png")))

print("\n" + ("ALL PASS" if not failures else "FAILURES: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
