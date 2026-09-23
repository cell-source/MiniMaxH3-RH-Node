"""Build matching canvas and API examples for all five H3 modes.

2026-09-24 整合：四节点图 = UNETLoader + MiniMaxH3IntegrationRH（All-in-One，
提示词生成/条件构建/双时钟采样/AV 解码）+ CreateVideo + SaveVideo。
Each mode ships `<mode>.workflow.json` (canvas) and `<mode>.api.json` (API prompt
format). Media slots that the mode requires carry SELECT_YOUR_* placeholder
filenames so the intended wiring is obvious; users replace them with real
uploads before running, exactly like the UNETLoader diffusion-model placeholder.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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
    sizes = {1: [315, 106], 2: [520, 780], 3: [315, 266], 4: [315, 266]}
    widgets_by_node = {3: [24.0, "auto", "sRGB", "none"], 4: ["video/MiniMaxH3-RH", "mp4", "h264"]}

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
