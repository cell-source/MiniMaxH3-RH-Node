# -*- coding: utf-8 -*-
"""
MiniMax H3 默认烧字幕 A/B 实验执行器
背景:
    H3 对「带对白的画面」有很强的烧字幕先验；提示词里写"不要字幕 / No subtitles"
    这类否定声明不但无效，其 token 本身可能反而是触发词。
    本实验用同一场景（与爆破音实验共用 apikey.txt 与 ref.png）对比 5 种提示词策略的字幕出现率。
用法:
    python subtitle_experiment.py [trials] [S0,S1,...]
    例: python subtitle_experiment.py 5 S0,S2,S3     # 推荐最小组合
        python subtitle_experiment.py 3              # 全部 5 个变体, 每个 3 条
前置:
    experiment/apikey.txt   MiniMax API Key（与爆破音实验共用，绝不打印）
    experiment/ref.png      参考图（与爆破音实验共用同一张）
输出:
    experiment/out_subtitle/<variant>_<trial>.mp4
    experiment/out_subtitle/results.json
    之后运行: python subtitle_analyze.py
变体设计（除 S4 外均含同一句中文对白，与爆破音实验 V3 同源，便于横向对照）:
    S0 基线-否定声明: 官方 <d> 格式 + detailed_description 末尾追加 "No subtitles..." 否定句（模拟用户现状）
    S1 删否定声明:    同 S0 但完全不含字幕字眼
    S2 正向约束句:    S1 + 工具「🔤 无字幕约束」注入的同款正向句（放在 [Shot 1] 之后）
    S3 电影感锚定:    S2 + 真人院线电影风格锚定句（再插一句正向风格描述）
    S4 无对白对照:    S1 的画面但无任何对白（测无语音时的字幕基线，隔离"对白触发"假设）
说明:
    - 与 run_experiment.py 相同: API 无 seed 参数, 靠重复生成 + 统计比例对抗种子依赖；幂等可断点续跑
    - 时长 5s、分辨率 768P、ratio=adaptive 控制成本；5 变体 × 5 条 = 25 条
    - 句式更新须与根 HTML 的 NO_SUBTITLES_CONSTRAINT 保持同步（S2 使用同款句子）
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
OUT_DIR = os.path.join(HERE, "out_subtitle")
RESULTS = os.path.join(OUT_DIR, "results.json")

DURATION = 5          # 秒
RESOLUTION = "768P"   # 实验用 768P 控制成本
POLL_SECONDS = 20
MAX_MINUTES = 25

# 与主工具 v6 完全一致（2026-09-28）：三句极简版——①全帧场景母版（含下三分之一与全部边缘，
# 说话与安静时同样完整）；②话语只进声轨（对白/语言标签/说话人编号仅指导声轨、画外保持画外、
# 口型按场景、画面不随话语措辞或节奏）；③场景文字保护（表面文字不擦除、语音不渲染为画面文字）。
# S3 仍额外添加电影风格锚定，比较其增益。句式必须与根 HTML 的 NO_SUBTITLES_CONSTRAINT 逐字一致。
NO_SUBTITLES_CONSTRAINT = (
    "The full frame, including the lower third and every edge, shows only the depicted scene throughout, as complete in speech as in silence. Dialogue quotes, language labels, and speaker identifiers are soundtrack instructions only; off-screen voices stay off-screen, visible mouths follow the scene, and the picture never follows speech wording or rhythm. Visible writing stays on scene surfaces such as signs, posters, and screens, never erased, and spoken words are never rendered as image text."
)
CINEMATIC_ANCHOR = (
    "The target video uses a realistic live-action theatrical film look with natural "
    "film lighting and cinematic color."
)

SHOT1_BODY = (
    "<Subject 1> (S1) speaks with a voice that grows increasingly tight, and she says: "
    "<d>[中文] 西洲，你别告诉我，你跟猎鹰基金有关系？</d> Her mouth is closed and not "
    "speaking at the very start of the shot, with only a brief moment of silence before "
    "she begins to speak."
)

PROMPTS = {
    "S0": """subject_definitions:
<Subject 1> is the woman in <Picture 1>, retaining her original facial features, hairstyle and outfit.

summary:
[reference generation] The target video shows <Subject 1> speaking with an increasingly tense vocal tone, questioning whether Xizhou has any connection with the Falcon Fund.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's facial features, hairstyle and outfit from the reference image are retained, and her spoken line is preserved verbatim.

detailed_description:
[Shot 1] %BODY% No subtitles are shown at any point in the video. Do not render subtitles, captions, or any written form of the spoken words on screen.

overall_soundscape:
N/A

non_diegetic_music:
N/A""",

    "S1": """subject_definitions:
<Subject 1> is the woman in <Picture 1>, retaining her original facial features, hairstyle and outfit.

summary:
[reference generation] The target video shows <Subject 1> speaking with an increasingly tense vocal tone, questioning whether Xizhou has any connection with the Falcon Fund.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's facial features, hairstyle and outfit from the reference image are retained, and her spoken line is preserved verbatim.

detailed_description:
[Shot 1] %BODY%

overall_soundscape:
N/A

non_diegetic_music:
N/A""",

    "S2": """subject_definitions:
<Subject 1> is the woman in <Picture 1>, retaining her original facial features, hairstyle and outfit.

summary:
[reference generation] The target video shows <Subject 1> speaking with an increasingly tense vocal tone, questioning whether Xizhou has any connection with the Falcon Fund.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's facial features, hairstyle and outfit from the reference image are retained, and her spoken line is preserved verbatim.

detailed_description:
[Shot 1] %CONSTRAINT% %BODY%

overall_soundscape:
N/A

non_diegetic_music:
N/A""",

    "S3": """subject_definitions:
<Subject 1> is the woman in <Picture 1>, retaining her original facial features, hairstyle and outfit.

summary:
[reference generation] The target video shows <Subject 1> speaking with an increasingly tense vocal tone, questioning whether Xizhou has any connection with the Falcon Fund.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's facial features, hairstyle and outfit from the reference image are retained, and her spoken line is preserved verbatim.

detailed_description:
[Shot 1] %STYLE% %CONSTRAINT% %BODY%

overall_soundscape:
N/A

non_diegetic_music:
N/A""",

    "S4": """subject_definitions:
<Subject 1> is the woman in <Picture 1>, retaining her original facial features, hairstyle and outfit.

summary:
[reference generation] The target video shows <Subject 1> listening quietly with subtle changes of expression.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - The woman's facial features, hairstyle and outfit from the reference image are retained.

detailed_description:
[Shot 1] <Subject 1> remains still, her expression shifting from neutral to a faint, thoughtful look; she blinks slowly, lets her gaze drift aside, and settles back with a quiet breath.

overall_soundscape:
N/A

non_diegetic_music:
N/A""",
}


def _fill():
    PROMPTS["S0"] = PROMPTS["S0"].replace("%BODY%", SHOT1_BODY)
    PROMPTS["S1"] = PROMPTS["S1"].replace("%BODY%", SHOT1_BODY)
    PROMPTS["S2"] = PROMPTS["S2"].replace("%BODY%", SHOT1_BODY).replace("%CONSTRAINT%", NO_SUBTITLES_CONSTRAINT)
    PROMPTS["S3"] = (PROMPTS["S3"].replace("%BODY%", SHOT1_BODY)
                     .replace("%CONSTRAINT%", NO_SUBTITLES_CONSTRAINT)
                     .replace("%STYLE%", CINEMATIC_ANCHOR))
_fill()


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
        sys.exit("缺少 experiment/ref.png（与爆破音实验共用的参考图）")

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
    print("下一步: python subtitle_analyze.py")


if __name__ == "__main__":
    main()
