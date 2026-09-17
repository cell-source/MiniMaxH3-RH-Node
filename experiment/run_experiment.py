# -*- coding: utf-8 -*-
"""
MiniMax H3 边界爆破音 A/B 实验执行器
用法:
    python run_experiment.py [trials] [V0,V1,V2,...]
    例: python run_experiment.py 5 V0,V2        # 最小成本实验
        python run_experiment.py 3              # 全部 6 个变体, 每个 3 条
前置:
    experiment/apikey.txt   存放 MiniMax API Key（本脚本直接读取，绝不打印）
    experiment/ref.png      原始 A/B 测试使用的那张参考图
输出:
    experiment/out/<variant>_<trial>.mp4
    experiment/out/results.json
说明:
    - 国内端点默认 https://api.minimaxi.com，可用环境变量 MINIMAX_API_BASE 覆盖
    - API 无 seed 参数，每次生成都是独立随机采样；用多次重复 + 统计比例来对抗种子依赖
"""
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.com").rstrip("/")
KEY_FILE = os.path.join(HERE, "apikey.txt")
IMG_FILE = os.path.join(HERE, "ref.png")
OUT_DIR = os.path.join(HERE, "out")
RESULTS = os.path.join(OUT_DIR, "results.json")

DURATION = 5          # 秒
RESOLUTION = "768P"   # 实验用 768P 控制成本
POLL_SECONDS = 20
MAX_MINUTES = 25

PROMPTS = {
    "V0": """subject_definitions:
<Subject 1> is the woman whose voice is heard in the input, speaking with increasing tension.

summary:
[reference generation] A woman's voice grows increasingly tense as she addresses someone named Xizhou, demanding to know whether that person has any connection to the Falcon Fund.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's spoken line is preserved verbatim in translation, with her rising tension conveyed through the delivery description.

detailed_description:
[Shot 1] The woman (S1) speaks with a voice that grows increasingly tight, and she says: <d>[中文] 西洲，你别告诉我，你跟猎鹰基金有关系？</d> Her mouth is closed and not speaking at the very start of the shot, with only a brief moment of silence before she begins to speak.

overall_soundscape:
N/A

non_diegetic_music:
N/A""",

    "V1": """subject_definitions:
<Subject 1> is the woman whose voice is heard in the input, speaking with increasing tension.

summary:
[reference generation] A woman's voice grows increasingly tense as she addresses someone named Xizhou, demanding to know whether that person has any connection to the Falcon Fund.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's spoken line is preserved verbatim in translation, with her rising tension conveyed through the delivery description.

detailed_description:
[Shot 1] The woman (S1) speaks with a voice that grows increasingly tight, serious and suspicious, at a moderate-fast pace: [中文] 西洲，你别告诉我，你跟猎鹰基金有关系？ Her mouth is closed and not speaking at the very start of the shot, with only a brief moment of silence before she begins to speak.

overall_soundscape:
N/A

non_diegetic_music:
N/A""",

    "V2": """subject_definitions:
<Subject 1> is the woman whose voice is heard in the input, speaking with increasing tension.

summary:
[reference generation] A woman's voice grows increasingly tense as she addresses someone named Xizhou, demanding to know whether that person has any connection to the Falcon Fund.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's spoken line is preserved verbatim in translation, with her rising tension conveyed through the delivery description.

detailed_description:
[Shot 1] The woman (S1) speaks with a voice that grows increasingly tight, and she says: <d>[中文] 西洲，你别告诉我，你跟猎鹰基金有关系？</d> Her mouth is closed and not speaking at the very start of the shot, with only a brief moment of silence before she begins to speak.

overall_soundscape:
Quiet subtle indoor room tone runs continuously through the entire video, with the dialogue remaining clearly audible above it.

non_diegetic_music:
N/A""",

    "V3": """subject_definitions:
<Subject 1> is the woman in <Picture 1>, retaining her original facial features, hairstyle and outfit.

summary:
[reference generation] The target video shows <Subject 1> speaking with an increasingly tense vocal tone, questioning whether Xizhou has any connections with Falcon Fund.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's facial features, hairstyle and outfit from the reference image are retained, and her spoken line is preserved verbatim.

detailed_description:
[Shot 1] <Subject 1> (S1) speaks with a voice that grows increasingly tight, and she says: <d>[中文] 西洲，你别告诉我，你跟猎鹰基金有关系？</d> Her mouth is closed and not speaking at the very start of the shot, with only a brief moment of silence before she begins to speak.

overall_soundscape:
N/A

non_diegetic_music:
N/A""",

    "V4": """subject_definitions:
<Subject 1> is the woman in <Picture 1>, retaining her original facial features, hairstyle and outfit.

summary:
[reference generation] The target video shows <Subject 1> speaking with an increasingly tense voice, questioning whether Xizhou has any connection with Falcon Fund.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's facial features, hairstyle and outfit from the reference image are retained, and her rising tension is conveyed through the delivery.

detailed_description:
The target video uses a realistic style, indoor soft ambient lighting, a fixed lens, and a medium shot. [Shot 1] <Subject 1>'s facial muscles gradually tense, her jaw tightens slightly, subtle tension lines appear on her forehead, her breathing turns slightly quicker, and her pupils contract a little. Her voice grows increasingly tight and strained, speaking with serious, suspicious emotion at a moderate-fast pace: [中文] 西洲，你别告诉我，你跟猎鹰基金有关系？ Her brows draw slightly together as she finishes the sentence, with a slight chest rise and fall from tense breathing.

overall_soundscape:
Quiet subtle indoor room tone runs continuously through the whole video, with the dialogue remaining clearly audible.

non_diegetic_music:
N/A""",

    "A": """subject_definitions:
<Subject 1> is the woman in <Picture 1>, retaining her original facial features, hairstyle and outfit.
summary:
[reference generation] The target video shows <Subject 1> speaking with increasingly tense vocal tone, questioning whether Xizhou has connections with Falcon Fund.
retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - the woman's facial features, hairstyle and outfit from reference image are retained.
detailed_description:
The target video uses realistic style, indoor soft ambient lighting, fixed lens, medium shot.
[Shot 1] The woman <Subject 1>\u2019s facial muscles gradually tense, her jaw tightens slightly, subtle tension lines appear on her forehead, her breathing turns slightly quicker, pupils contract a little. Her voice grows increasingly tight and strained, speaking with serious, suspicious emotion, moderate\u2011fast speaking pace, [中文]西洲，你别告诉我，你跟猎鹰基金有关系？ Her brows draw slightly together as she finishes the sentence, slight chest rise and fall from tense breathing.
overall_soundscape:
Quiet subtle indoor background ambient sound runs through the whole clip, clear human voice takes priority.
non_diegetic_music:
N/A""",
}


def http_json(method, url, key, body=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {detail[:500]}")


def create_task(key, prompt, image_data_uri):
    body = {
        "model": "MiniMax-H3",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_uri}, "role": "reference_image"},
        ],
        "resolution": RESOLUTION,
        "duration": DURATION,
        "ratio": "adaptive",
    }
    resp = http_json("POST", BASE + "/v2/video_generation", key, body)
    return resp["task_id"]


def query_task(key, task_id):
    return http_json("GET", BASE + f"/v2/query/video_generation/{task_id}", key).get("task", {})


def main():
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    variants = sys.argv[2].split(",") if len(sys.argv) > 2 else list(PROMPTS.keys())
    for v in variants:
        if v not in PROMPTS:
            sys.exit(f"未知变体: {v} (可选: {','.join(PROMPTS.keys())})")

    if not os.path.exists(KEY_FILE):
        sys.exit("缺少 experiment/apikey.txt（第一行放 MiniMax API Key）")
    if not os.path.exists(IMG_FILE):
        sys.exit("缺少 experiment/ref.png（原始 A/B 测试的参考图）")

    key = open(KEY_FILE, encoding="utf-8").read().strip()
    ext = os.path.splitext(IMG_FILE)[1].lower().lstrip(".")
    mime = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp"}.get(ext, "png")
    img_b64 = base64.b64encode(open(IMG_FILE, "rb").read()).decode("ascii")
    img_uri = f"data:image/{mime};base64,{img_b64}"
    if len(img_uri) > 48 * 1024 * 1024:
        sys.exit("图片 Base64 过大（请求体限制 64MB），请换一张更小的参考图")

    os.makedirs(OUT_DIR, exist_ok=True)
    results = json.load(open(RESULTS, encoding="utf-8")) if os.path.exists(RESULTS) else []
    done = {(r.get("variant"), r.get("trial")) for r in results if r.get("file")}

    # 第一阶段: 提交全部任务
    pending = []
    for v in variants:
        for i in range(1, trials + 1):
            if (v, i) in done:
                print(f"[skip] {v}_{i} 已完成")
                continue
            print(f"[create] {v}_{i}")
            try:
                tid = create_task(key, PROMPTS[v], img_uri)
            except RuntimeError as e:
                print(f"[create FAILED] {v}_{i}: {e}")
                results.append({"variant": v, "trial": i, "status": "create_failed",
                                "error": str(e)[:300], "file": None})
                json.dump(results, open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                continue
            pending.append({"variant": v, "trial": i, "task_id": tid, "submitted_at": time.time()})
            json.dump(results, open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 第二阶段: 轮询 + 下载
    while pending:
        time.sleep(POLL_SECONDS)
        still = []
        for p in pending:
            if time.time() - p["submitted_at"] > MAX_MINUTES * 60 * 2:
                print(f"[poll TIMEOUT] {p['variant']}_{p['trial']} ({p['task_id']})")
                results = [r for r in results if not (r.get("variant") == p["variant"] and r.get("trial") == p["trial"])]
                results.append({"variant": p["variant"], "trial": p["trial"], "task_id": p["task_id"],
                                "status": "poll_timeout", "error": "轮询超时", "file": None})
                json.dump(results, open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
                continue
            try:
                task = query_task(key, p["task_id"])
            except RuntimeError as e:
                print(f"[query FAILED] {p['variant']}_{p['trial']}: {e}")
                still.append(p)
                continue
            status = task.get("status")
            print(f"[{status}] {p['variant']}_{p['trial']} ({p['task_id']})")
            if status in ("succeeded", "failed", "cancelled"):
                rec = {"variant": p["variant"], "trial": p["trial"], "task_id": p["task_id"],
                       "status": status, "file": None}
                if status == "succeeded":
                    url = (task.get("content") or {}).get("url")
                    rec["url"] = url
                    rec["duration"] = task.get("duration")
                    rec["ratio"] = task.get("ratio")
                    if url:
                        fname = os.path.join(OUT_DIR, f"{p['variant']}_{p['trial']}.mp4")
                        try:
                            urllib.request.urlretrieve(url, fname)
                            rec["file"] = fname
                            print(f"    downloaded -> {fname}")
                        except Exception as e:
                            rec["error"] = f"download: {e}"[:300]
                else:
                    rec["error"] = json.dumps(task.get("error"), ensure_ascii=False)[:300]
                results = [r for r in results if not (r.get("variant") == p["variant"] and r.get("trial") == p["trial"])]
                results.append(rec)
                json.dump(results, open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            else:
                still.append(p)
        pending = still

    print("全部任务处理完毕 ->", RESULTS)


if __name__ == "__main__":
    main()
