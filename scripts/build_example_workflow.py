"""Build matching canvas and API examples for all five H3 modes.

2026-09-24 整合：四节点图 = UNETLoader + MiniMaxH3IntegrationRH（All-in-One，
提示词生成/条件构建/双时钟采样/AV 解码）+ CreateVideo + SaveVideo。
Each mode ships `<mode>.workflow.json` (canvas) and `<mode>.api.json` (API prompt
format). Media slots that the mode requires carry SELECT_YOUR_* placeholder
filenames so the intended wiring is obvious; users replace them with real
uploads before running, exactly like the UNETLoader diffusion-model placeholder.

2026-09-26（上线评估 A3）：
- 画布示例此前只给节点 3/4 写 `widgets_values`，主节点与 UNETLoader 落成 `null`，
  导入后是空节点；现在两节点都写入完整参数（含 `SELECT_YOUR_*` 占位）。
- 画布示例的来源固定为 `panel`，并在 `prompt` 写入**引擎校验通过**的完整提示词：
  面板的迁移 v2 会把任何非 `panel` 来源改写为 `panel`，沿用 `offline` 只会得到
  “来源被改写 + 编辑器为空”。`offline`/`format` 仅保留给 API 示例（`.api.json`）。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prompt_runtime import generate_prompt  # noqa: E402  （与运行期同一套 quickjs 引擎）

# Ref2VA 不能用 offline（离线包装不生成六 section），示例按 README 指引使用
# format 操作整理一份完整、可通过严格校验的六 section 提示词。
REF2VA_FORMAT_TEXT = (
    "subject_definitions:\n"
    "<Subject 1> is the woman in <Picture 1>, with long dark hair and a light-grey coat.\n"
    "\n"
    "summary:\n"
    "[reference generation] The target video shows <Subject 1> standing still in the referenced scene.\n"
    "\n"
    "retention_analysis:\n"
    "<Subject 1> (appears in [Shot 1]): fully_preserved - identity, hairstyle, and coat are retained.\n"
    "\n"
    "detailed_description:\n"
    "[Shot 1] <Subject 1> stands still in the scene referenced from <Picture 1>.\n"
    "\n"
    "overall_soundscape:\n"
    "N/A\n"
    "\n"
    "non_diegetic_music:\n"
    "N/A"
)

NODE_ID = "MiniMaxH3IntegrationRH"

# All-in-One 节点的公共输入（按 schema 顺序；model 是连线不进 widgets）
AIO_BASE = dict(
    main_mode="text_keyframes",
    clip_name="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    video_vae_name="minimax_h3_video_vae_fp16.safetensors",
    audio_vae_name="minimax_h3_audio_vae_fp32.safetensors",
    aspect="16:9", megapixels=0.5, duration_seconds=5,
    prompt="", first_frame="", last_frame="", hybrid_audio="",
    task_type="t2va", audio_mode="native", audio_denoise_strength=0.0,
    drive_audio_ordinal=0, strict_prompt_tags=True, ref_image_size="match",
    gh_state_json="",
    no_subtitle=True, soundscape=False, music=False,
    steps=4, shift_video=12.0, shift_audio=3.0, noise_seed=42,
    prompt_source="offline", ai_language="en",
    ai_enrich=False, ai_soundscape=False, ai_music=False, ai_auto_timestamps=False,
    ai_fixed_camera=False, ai_visual_stability=False, ai_no_subtitles=True,
    ai_anti_pop=False, ai_strict_validation=True,
    ai_provider="deepseek", ai_api_key="", ai_endpoint="", ai_model="", ai_timeout=180,
)

MODES = {
    "t2va": dict(text="A woman stands still.", mode="t2va", main_mode="text_keyframes", media={}),
    "i2va": dict(text="A woman beside a rain-covered train window slowly lifts her gaze.",
                 mode="i2va", main_mode="text_keyframes",
                 media={"first_frame": "SELECT_YOUR_FIRST_FRAME.png"}),
    "fl2va": dict(text="She raises the umbrella above her shoulder and holds it open.",
                  mode="fl2va", main_mode="text_keyframes",
                  media={"first_frame": "SELECT_YOUR_FIRST_FRAME.png",
                         "last_frame": "SELECT_YOUR_LAST_FRAME.png"}),
    "l2va": dict(text="The glass tips, falls, and settles into the broken arrangement of the final frame.",
                 mode="l2va", main_mode="text_keyframes",
                 media={"last_frame": "SELECT_YOUR_LAST_FRAME.png"}),
    "ref2va": dict(text=REF2VA_FORMAT_TEXT, mode="ref2va", main_mode="all_reference",
                   prompt_source="format",
                   media={"ref_image_1": "SELECT_YOUR_REFERENCE_IMAGE_1.png",
                          "ref_video_1": "SELECT_YOUR_REFERENCE_VIDEO_1.mp4",
                          "ref_audio_1": "SELECT_YOUR_REFERENCE_AUDIO_1.wav"}),
}

# ComfyUI 序列化 `widgets_values` 的位置对应节点 widget 的创建顺序。
# 2026-09-26 实测（浏览器端以哨兵数组逐个校准）：
# - `prompt_override` 是 forceInput 输入，**加载时会占一个槽位**（面板上不出现 widget，
#   但值必须保留在数组里）；缺了它，从其位置起的整段（sampler_name/scheduler/ai_*）会+1 偏移，
#   实测表现为 ai_model 拿到 ai_timeout 的值。
# - `mxv_panel` 是面板注入的 DOM widget（serialize:false），**不属于** widgets_values。
# video_nodes.py 增删 widget 时 aio_widget_values() 会报错，提醒同步更新。
AIO_WIDGET_ORDER = (
    "main_mode", "clip_name", "video_vae_name", "audio_vae_name", "aspect", "megapixels",
    "duration_seconds", "prompt", "task_type", "audio_mode", "audio_denoise_strength",
    "drive_audio_ordinal", "strict_prompt_tags", "ref_image_size", "no_subtitle", "soundscape",
    "music", "steps", "shift_video", "shift_audio", "noise_seed",
    "control_after_generate",  # ComfyUI 为 seed 注入的控件
    "prompt_source", "ai_text", "ai_language", "ai_mode", "ai_enrich", "ai_soundscape", "ai_music",
    "ai_auto_timestamps", "ai_fixed_camera", "ai_visual_stability", "ai_no_subtitles", "ai_anti_pop",
    "ai_strict_validation", "ai_provider", "first_frame", "last_frame", "hybrid_audio",
) + tuple(f"ref_image_{i}" for i in range(1, 10)) + tuple(
    f"ref_video_{i}" for i in range(1, 4)) + tuple(
    f"ref_audio_{i}" for i in range(1, 4)) + (
    "gh_state_json",
    "prompt_override",  # forceInput 输入占位（面板不显示 widget，但必须占数组位置）
    "sampler_name", "scheduler", "ai_api_key", "ai_endpoint", "ai_model", "ai_timeout",
)

# 不在 AIO_BASE / spec 内的 widget：取 schema 默认值（见 sampling.py）或前端注入值。
AIO_WIDGET_DEFAULTS = {
    "control_after_generate": "fixed",  # 示例固定种子，便于复现
    "prompt_override": "",  # forceInput：永不连接时保持空字符串
    "sampler_name": "dual_clock_euler",
    "scheduler": "native_flow",
    **{f"ref_image_{i}": "" for i in range(1, 10)},
    **{f"ref_video_{i}": "" for i in range(1, 4)},
    **{f"ref_audio_{i}": "" for i in range(1, 4)},
}


def aio_widget_values(values: dict) -> list:
    """按 ComfyUI 的 widget 顺序展开为 widgets_values；schema 变化时立即报错而非静默错位。"""
    missing = sorted(name for name in AIO_WIDGET_ORDER if name not in values and name not in AIO_WIDGET_DEFAULTS)
    if missing:
        raise RuntimeError("AIO 节点的 widget 与 AIO_WIDGET_ORDER 不同步，请先同步脚本：" + ", ".join(missing))
    ordered = [values[name] if name in values else AIO_WIDGET_DEFAULTS[name] for name in AIO_WIDGET_ORDER]
    if len(ordered) != len(AIO_WIDGET_ORDER) or len(set(AIO_WIDGET_ORDER)) != len(AIO_WIDGET_ORDER):
        raise RuntimeError("AIO_WIDGET_ORDER 存在重复项或长度异常")
    return ordered


def canvas_prompt(spec: dict) -> str:
    """用运行期同一套引擎把示例文本整理成完整提示词（画布示例以编辑器内容为准）。"""
    values = {**AIO_BASE, "ai_mode": spec["mode"], "main_mode": spec["main_mode"]}
    controls = {
        "mode": spec["mode"], "lang": values["ai_language"], "duration": str(values["duration_seconds"]),
        "ratio": values["aspect"], "model": values["ai_provider"],
        "enrich_do_enrich": values["ai_enrich"], "enrich_soundscape": values["ai_soundscape"],
        "enrich_music": values["ai_music"], "auto_timestamps": values["ai_auto_timestamps"],
        "fixed_camera": values["ai_fixed_camera"], "visual_stability": values["ai_visual_stability"],
        "no_subtitles": values["ai_no_subtitles"], "anti_pop": values["ai_anti_pop"],
    }
    operation = spec.get("prompt_source", "offline")
    result = generate_prompt(spec["text"], operation, controls, None, lambda: None)
    if not result["valid"]:
        raise RuntimeError(f"{spec['mode']} 的 {operation} 结果未通过引擎严格校验：{result['report']}")
    return result["prompt"]


def build_graph(spec):
    values = dict(AIO_BASE)
    values["ai_text"] = spec["text"]
    values["ai_mode"] = spec["mode"]
    values["main_mode"] = spec["main_mode"]
    values["task_type"] = spec["mode"]
    values["prompt_source"] = spec.get("prompt_source", "offline")
    values.update(spec["media"])

    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "SELECT_YOUR_H3_DIFFUSION_MODEL.safetensors", "weight_dtype": "default"},
            "_meta": {"title": "UNETLoader"}},
        "2": {"class_type": NODE_ID, "inputs": {
            "model": ["1", 0], **values},
            "_meta": {"title": NODE_ID}},
        "3": {"class_type": "CreateVideo", "inputs": {
            "images": ["2", 0], "fps": 24.0, "audio": ["2", 1]},
            "_meta": {"title": "CreateVideo"}},
        "4": {"class_type": "SaveVideo", "inputs": {
            "video": ["3", 0], "filename_prefix": "video/MiniMaxH3-RH", "format": "mp4", "codec": "h264"},
            "_meta": {"title": "SaveVideo"}},
    }

    # 画布：节点布局与连线
    outputs_by_node = {
        1: [("MODEL", "MODEL")],
        2: [("frames", "IMAGE"), ("audio", "AUDIO"), ("video_latent", "LATENT"), ("report", "STRING")],
        3: [("VIDEO", "VIDEO")],
        4: [],
    }
    positions = {1: [0, 0], 2: [420, 0], 3: [1240, 0], 4: [1620, 0]}
    sizes = {1: [315, 106], 2: [520, 900], 3: [315, 266], 4: [315, 266]}
    # 画布示例与 API 示例参数一致（评估 A3）：主节点与 UNETLoader 此前均为 null。
    canvas_values = dict(values)
    canvas_values["prompt_source"] = "panel"       # 面板迁移 v2 会把非 panel 来源改写为 panel
    canvas_values["prompt"] = canvas_prompt(spec)  # 引擎校验过的完整提示词（编辑器内容）
    canvas_values["ai_text"] = ""                  # panel 路径下 ai_text 只是编辑器镜像
    widgets_by_node = {
        1: [graph["1"]["inputs"]["unet_name"], graph["1"]["inputs"]["weight_dtype"]],
        2: aio_widget_values(canvas_values),
        3: [24.0, "auto", "sRGB", "none"],
        4: ["video/MiniMaxH3-RH", "mp4", "h264"],
    }

    nodes, links = [], []
    for node_id, output_defs in outputs_by_node.items():
        record = graph[str(node_id)]
        nodes.append(dict(id=node_id, type=record["class_type"], pos=positions[node_id],
            size=sizes[node_id], flags={}, order=node_id - 1, mode=0, inputs=[],
            outputs=[dict(name=name, type=kind, links=[], slot_index=i) for i, (name, kind) in enumerate(output_defs)],
            properties={"Node name for S&R": record["class_type"]},
            widgets_values=widgets_by_node.get(node_id)))
    for target in nodes:
        for name, value in graph[str(target["id"])]["inputs"].items():
            if not isinstance(value, list): continue
            source, slot = int(value[0]), value[1]
            output = nodes[source - 1]["outputs"][slot]
            link_id = len(links) + 1
            socket = dict(name=name, type=output["type"], link=link_id)
            links.append([link_id, source, slot, target["id"], len(target["inputs"]), output["type"]])
            target["inputs"].append(socket)
            output["links"].append(link_id)
    for target in nodes:
        for output in target["outputs"]:
            if not output["links"]: output["links"] = None
    canvas = dict(last_node_id=4, last_link_id=len(links), nodes=nodes, links=links, groups=[], config={},
        extra={"ds": {"scale": 0.65, "offset": [30, 40]}}, version=0.4)
    return graph, canvas


def main():
    folder = ROOT / "examples"
    folder.mkdir(exist_ok=True)
    # 清理历史示例（旧图结构已失效，防残留误导）
    for stale in folder.glob("*.json"):
        stale.unlink()
    for mode, spec in MODES.items():
        graph, canvas = build_graph(spec)
        (folder / f"{mode}.workflow.json").write_text(json.dumps(canvas, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (folder / f"{mode}.api.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{mode}: workflow + api written")


if __name__ == "__main__":
    main()
