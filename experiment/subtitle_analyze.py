# -*- coding: utf-8 -*-
"""
MiniMax H3 字幕实验分析器
对 out_subtitle/*.mp4 以 1fps 抽帧，检测「画面下部字幕带」中是否出现台词文字，输出各变体命中率。

检测模式（自动选择，按优先级）:
    1. rapidocr-onnxruntime  -> pip install rapidocr-onnxruntime（推荐，中英文均可）
    2. pytesseract           -> 需另装 tesseract-ocr.exe（含 chi_sim 语言包）并配置 PATH
    3. 都不可用              -> 人工模式: 导出字幕带裁剪图到 out_subtitle/frames/，按打印的清单人工核对

另计算「底部/中部边缘密度比」作为粗筛信号（suspect）：说话人头肩、衣服纹理都会贡献底部边缘，
该信号仅用于排序与抽查定位，绝不作为结论；结论只看 OCR 或人工核对。

命中口径: 字幕带内出现与台词对应的文字 → 命中；场景内实体文字（标牌、霓虹灯、屏幕内容）不算。
用法:
    python subtitle_analyze.py
输出:
    experiment/out_subtitle/report.json
    experiment/out_subtitle/frames/<variant>_<trial>_f<i>.png   （人工模式导出全部；OCR 模式只导出命中帧）
"""
import json
import os
import re
import sys

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out_subtitle")
FRAMES_DIR = os.path.join(OUT_DIR, "frames")
REPORT = os.path.join(OUT_DIR, "report.json")

FPS = 1                        # 每秒抽 1 帧
BAND_Y0, BAND_Y1 = 0.70, 0.97  # 字幕带: 屏幕高度 70%~97%
BAND_X0, BAND_X1 = 0.12, 0.88  # 字幕带: 屏幕宽度 12%~88%
CENTER_Y0, CENTER_Y1 = 0.30, 0.60  # 参照带: 画面中部
EDGE_THRESH = 40               # 灰度梯度阈值
SUSPECT_RATIO = 2.5            # 底部/中部边缘密度比 ≥ 2.5 记为 suspect
SUSPECT_MIN_DENSITY = 0.015    # 且底部边缘密度 ≥ 0.015（实测灰底噪声≈0、带字幕条≈0.02）
MIN_TEXT_LEN = 2               # OCR 判定字幕的最少有效字符数（去空格后）
MIN_CONF = 0.5                 # OCR 置信度阈值

NAME_RE = re.compile(r"^([A-Za-z]+\d+)_(\d+)\.mp4$")


def make_ocr():
    """返回 ocr(bgr_array) -> [(text, conf), ...] 或 None。"""
    try:
        from rapidocr_onnxruntime import RapidOCR
        eng = RapidOCR()

        def rapid(img):
            result, _ = eng(img)
            out = []
            for item in (result or []):
                text, score = item[1], float(item[2])
                out.append((text, score))
            return out
        return rapid
    except Exception:
        pass
    try:
        import pytesseract
        from PIL import Image

        def tess(img):
            rgb = img[:, :, ::-1]  # BGR -> RGB
            data = pytesseract.image_to_data(
                Image.fromarray(rgb), lang="chi_sim+eng",
                output_type=pytesseract.Output.DICT)
            out = []
            for text, conf in zip(data["text"], data["conf"]):
                try:
                    c = float(conf) / 100.0
                except (TypeError, ValueError):
                    c = 0.0
                if text.strip():
                    out.append((text, c))
            return out
        return tess
    except Exception:
        return None


def edge_density(gray):
    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    count = int((gx > EDGE_THRESH).sum()) + int((gy > EDGE_THRESH).sum())
    return count / float(gray.size + gray.shape[0] + gray.shape[1])


def save_png(arr, path):
    """零依赖写 PNG（RGB），仅用于人工核对的裁剪图。"""
    import struct
    import zlib

    h, w, _ = arr.shape
    raw = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        f.write(chunk(b"IEND", b""))


def crop(img, y0, y1, x0, x1):
    h, w = img.shape[:2]
    return img[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]


def analyze_video(path, ocr, variant, trial, manual_mode):
    import imageio_ffmpeg
    reader = imageio_ffmpeg.read_frames(path, output_params=["-vf", f"fps={FPS}"])
    meta = next(reader)
    frames = []
    fi = 0
    for buf in reader:
        frame = np.frombuffer(buf, np.uint8)
        frame = frame.reshape((meta["size"][1], meta["size"][0], 3))
        gray = frame.mean(axis=2)
        band = crop(frame, BAND_Y0, BAND_Y1, BAND_X0, BAND_X1)
        band_gray = gray[int(gray.shape[0] * BAND_Y0):int(gray.shape[0] * BAND_Y1),
                         int(gray.shape[1] * BAND_X0):int(gray.shape[1] * BAND_X1)]
        center_gray = gray[int(gray.shape[0] * CENTER_Y0):int(gray.shape[0] * CENTER_Y1), :]
        d_band = edge_density(band_gray)
        d_center = max(edge_density(center_gray), 1e-6)
        ratio = d_band / d_center
        suspect = bool(ratio >= SUSPECT_RATIO and d_band >= SUSPECT_MIN_DENSITY)

        rec = {"frame": fi, "band_density": round(d_band, 4),
               "center_density": round(d_center, 4), "ratio": round(ratio, 2),
               "suspect": suspect, "ocr_hit": False, "texts": []}
        if ocr is not None:
            texts = ocr(band)
            hits = [(t, round(c, 2)) for t, c in texts
                    if len(t.replace(" ", "")) >= MIN_TEXT_LEN and c >= MIN_CONF]
            rec["texts"] = [t for t, _ in hits]
            rec["ocr_hit"] = bool(hits)
        # 人工模式导出全部字幕带；OCR 模式只导出命中帧供复核
        if manual_mode or rec["ocr_hit"]:
            fname = os.path.join(FRAMES_DIR, f"{variant}_{trial}_f{fi}.png")
            save_png(band[:, :, ::-1], fname)  # BGR -> RGB
            rec["band_image"] = os.path.relpath(fname, HERE)
        frames.append(rec)
        fi += 1
    try:
        reader.close()
    except Exception:
        pass
    return {
        "variant": variant, "trial": trial, "file": os.path.relpath(path, HERE),
        "frames_sampled": len(frames), "frames": frames,
        "frame_hits": sum(1 for f in frames if f["ocr_hit"]),
        "frame_suspects": sum(1 for f in frames if f["suspect"]),
        "video_hit": any(f["ocr_hit"] for f in frames),
        "video_suspect": any(f["suspect"] for f in frames),
    }


def main():
    if not os.path.isdir(OUT_DIR):
        sys.exit(f"缺少目录 {OUT_DIR}（先运行 subtitle_experiment.py）")
    videos = []
    for name in sorted(os.listdir(OUT_DIR)):
        m = NAME_RE.match(name)
        if m:
            videos.append((m.group(1), int(m.group(2)), os.path.join(OUT_DIR, name)))
    if not videos:
        sys.exit(f"{OUT_DIR} 中没有 <variant>_<trial>.mp4 文件")

    ocr = make_ocr()
    if ocr is not None:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            mode = "ocr-rapidocr"
        except Exception:
            mode = "ocr-tesseract"
        print(f"[模式] OCR 自动判定（{mode}）")
    else:
        mode = "manual"
        os.makedirs(FRAMES_DIR, exist_ok=True)
        print("[模式] 未检测到 OCR，切换人工模式：已导出字幕带裁剪图到 out_subtitle/frames/")
        print("       启用自动判定: pip install rapidocr-onnxruntime")

    records = []
    for variant, trial, path in videos:
        print(f"[analyze] {variant}_{trial}")
        try:
            records.append(analyze_video(path, ocr, variant, trial, mode == "manual"))
        except Exception as e:
            print(f"    FAILED: {e}"[:300])
            records.append({"variant": variant, "trial": trial,
                            "file": os.path.relpath(path, HERE), "error": str(e)[:300]})

    # 汇总
    summary = {}
    for v in sorted({r["variant"] for r in records if "error" not in r}):
        rs = [r for r in records if r["variant"] == v and "error" not in r]
        n_frames = sum(r["frames_sampled"] for r in rs)
        n_hits = sum(r["frame_hits"] for r in rs)
        n_sus = sum(r["frame_suspects"] for r in rs)
        summary[v] = {
            "videos": len(rs),
            "videos_hit": sum(1 for r in rs if r["video_hit"]),
            "videos_suspect": sum(1 for r in rs if r["video_suspect"]),
            "frames": n_frames,
            "frame_hits": n_hits,
            "frame_suspects": n_sus,
            "frame_hit_rate": round(n_hits / n_frames, 3) if n_frames else None,
            "video_hit_rate": round(sum(1 for r in rs if r["video_hit"]) / len(rs), 3) if rs else None,
        }

    report = {"mode": mode, "params": {
        "fps": FPS, "band_y": [BAND_Y0, BAND_Y1], "band_x": [BAND_X0, BAND_X1],
        "suspect_ratio": SUSPECT_RATIO, "min_text_len": MIN_TEXT_LEN, "min_conf": MIN_CONF,
    }, "summary": summary, "records": records}
    json.dump(report, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("\n===== 字幕命中率汇总（结论以 OCR/人工核对为准，suspect 仅供参考）=====")
    print(f"{'变体':<6}{'视频':>6}{'命中视频':>8}{'视频命中率':>10}{'抽帧':>6}{'命中帧':>8}{'疑似帧':>8}")
    for v, s in summary.items():
        print(f"{v:<6}{s['videos']:>6}{s['videos_hit']:>8}"
              f"{(str(round(s['video_hit_rate'] * 100, 1)) + '%') if s['video_hit_rate'] is not None else '-':>10}"
              f"{s['frames']:>6}{s['frame_hits']:>8}{s['frame_suspects']:>8}")
    print("\n报告 ->", REPORT)
    if mode == "manual":
        print("\n===== 人工核对清单 =====")
        print("逐张查看 out_subtitle/frames/ 下的裁剪图：画面下部出现台词文字=命中，")
        print("空白或仅场景实体文字（标牌/霓虹灯）=未命中。统计后把每变体的命中数填回 report.json 或实验记录。")
        for v in sorted(summary.keys()):
            files = []
            for r in records:
                if r["variant"] != v or "error" in r:
                    continue
                files += [os.path.basename(f["band_image"]) for f in r["frames"] if "band_image" in f]
            if files:
                print(f"  {v}: {len(files)} 张 -> {', '.join(files[:8])}{' ...' if len(files) > 8 else ''}")


if __name__ == "__main__":
    main()
