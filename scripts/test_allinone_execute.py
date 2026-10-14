"""Direct-execute test of the merged all-in-one node inside a ComfyUI runtime context.

No H3 weights are installed, so execution cannot complete — but the merged
execute() handles prompts BEFORE any model loading. We verify:
  1. prompt_origin="ai" without a key  -> explicit DEEPSEEK key error (AI path wired)
  2. prompt_origin="ai" duration>15    -> explicit guard error (15s cap)
  3. prompt_origin="offline"           -> runs quickjs engine, then fails at clip
                                          loading (proves engine + validation ran)
  4. prompt_origin="format" ref2va     -> same as 3 for the format path
Bootstrap mirrors scripts/check_comfy_registration.py (PromptServer stub only).
"""
import asyncio
import faulthandler
import importlib.util
import sys
from pathlib import Path

faulthandler.dump_traceback_later(180, repeat=False)

root = Path(__file__).resolve().parents[1]
comfy = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(comfy), str(root)]
sys.argv = [sys.argv[0], "--cpu"]
import comfy.options
comfy.options.enable_args_parsing()
from server import PromptServer
from aiohttp import web

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
from types import SimpleNamespace
PromptServer.instance = SimpleNamespace(routes=web.RouteTableDef())
spec = importlib.util.spec_from_file_location("h3_rh_exec_test", root / "__init__.py", submodule_search_locations=[str(root)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)

classes = loop.run_until_complete(package.comfy_entrypoint().get_node_list())
node_cls = classes[0]
assert node_cls.GET_SCHEMA().node_id == "MiniMaxH3IntegrationRH"
print("node:", node_cls.GET_SCHEMA().node_id)

BASE = dict(
    panel_mode="text_keyframes", frame_ratio="16:9", pixel_budget=0.5, clip_seconds=5,
    text_encoder="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    video_decoder="minimax_h3_video_vae_fp16.safetensors",
    audio_decoder="minimax_h3_audio_vae_fp32.safetensors",
    prompt="", keyframe_open="", keyframe_close="", keyframe_audio="",
    still_1="", still_2="", still_3="", still_4="", still_5="",
    still_6="", still_7="", still_8="", still_9="",
    footage_1="", footage_2="", footage_3="", track_1="", track_2="", track_3="",
    task_hint="t2va", audio_source="native", audio_cleanup=0.0,
    driving_track=0, tag_guard=True, asset_scale="match",
    model=None, sample_steps=4, video_sigma_shift=12.0, audio_sigma_shift=3.0, seed_value=42,
    prompt_origin="panel", ai_idea="", ai_locale="en", ai_task="auto",
    ai_expand=False, ai_ambience=False, ai_score=False, ai_timeline=False,
    ai_lock_camera=False, ai_stabilize=False, ai_subtitle_free=True,
    ai_audio_guard=False, ai_strict_check=True, ai_platform="deepseek",
    ai_key="", ai_base_url="", ai_model_name="", ai_deadline=30,
    panel_state="", upstream_prompt=None,
    subtitle_free=True, soundscape=False, music=False,
)

failures = []


def expect(name, kwargs, needle, strip_env=False):
    import os
    saved = {k: os.environ.get(k) for k in ("DEEPSEEK_API_KEY", "GLM_API_KEY", "H3_LLM_API_KEY")}
    try:
        if strip_env:
            for k in saved:
                os.environ.pop(k, None)
        try:
            node_cls.execute(**{**BASE, **kwargs})
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if needle in message:
                print(f"[PASS] {name}: {type(exc).__name__}: {message[:130]}")
            else:
                print(f"[FAIL] {name}: 期望 {needle!r}，实际: {message[:200]}")
                failures.append(name)
        else:
            print(f"[FAIL] {name}: 未抛错（期望 {needle!r}）")
            failures.append(name)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# 1) AI + 占位符环境变量（本机 DEEPSEEK_API_KEY=中文占位文字）：修复后应报明确的 Key 无效错误
expect("ai-占位Key环境变量", dict(prompt_origin="ai", ai_idea="A woman stands still.", ai_task="t2va"),
       "API Key 无效")

# 1b) AI 无 Key（清空环境变量）：应报“请设置服务端 DEEPSEEK_API_KEY”
expect("ai-无Key", dict(prompt_origin="ai", ai_idea="A woman stands still.", ai_task="t2va"),
       "DEEPSEEK_API_KEY", strip_env=True)

# 2) 时长守卫：引擎生成仅支持 2–15 秒
expect("ai-超时长", dict(prompt_origin="ai", ai_idea="A woman stands still.", clip_seconds=20),
       "2–15 秒")

# 3) offline：quickjs 引擎执行 + 严格校验通过后，失败点应为文本编码器缺失
expect("offline-引擎后加载失败", dict(prompt_origin="offline", ai_idea="A woman stands still.", ai_locale="en"),
       "not found")

# 4) format ref2va：format 路径同样先于模型加载
ref2va_text = (
    "subject_definitions:\n<Subject 1> is the woman in <Picture 1>, with long dark hair.\n\n"
    "summary:\n[reference generation] The target video shows <Subject 1> standing still.\n\n"
    "retention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - identity retained.\n\n"
    "detailed_description:\n[Shot 1] <Subject 1> stands still in the scene from <Picture 1>.\n\n"
    "overall_soundscape:\nN/A\n\nnon_diegetic_music:\nN/A"
)
expect("format-ref2va", dict(prompt_origin="format", ai_idea=ref2va_text,
                             ai_task="ref2va", panel_mode="all_reference", task_hint="ref2va"),
       "not found")

faulthandler.cancel_dump_traceback_later()
loop.close()
print("RESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
