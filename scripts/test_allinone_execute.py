"""Direct-execute test of the merged all-in-one node inside a ComfyUI runtime context.

No H3 weights are installed, so execution cannot complete — but the merged
execute() handles prompts BEFORE any model loading. We verify:
  1. prompt_source="ai" without a key  -> explicit DEEPSEEK key error (AI path wired)
  2. prompt_source="ai" duration>15    -> explicit guard error (15s cap)
  3. prompt_source="offline"           -> runs quickjs engine, then fails at clip
                                          loading (proves engine + validation ran)
  4. prompt_source="format" ref2va     -> same as 3 for the format path
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
    main_mode="text_keyframes", aspect="16:9", megapixels=0.5, duration_seconds=5,
    clip_name="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    video_vae_name="minimax_h3_video_vae_fp16.safetensors",
    audio_vae_name="minimax_h3_audio_vae_fp32.safetensors",
    prompt="", first_frame="", last_frame="", hybrid_audio="",
    ref_image_1="", ref_image_2="", ref_image_3="", ref_image_4="", ref_image_5="",
    ref_image_6="", ref_image_7="", ref_image_8="", ref_image_9="",
    ref_video_1="", ref_video_2="", ref_video_3="", ref_audio_1="", ref_audio_2="", ref_audio_3="",
    task_type="t2va", audio_mode="native", audio_denoise_strength=0.0,
    drive_audio_ordinal=0, strict_prompt_tags=True, ref_image_size="match",
    model=None, steps=4, shift_video=12.0, shift_audio=3.0, noise_seed=42,
    prompt_source="panel", ai_text="", ai_language="en", ai_mode="auto",
    ai_enrich=False, ai_soundscape=False, ai_music=False, ai_auto_timestamps=False,
    ai_fixed_camera=False, ai_visual_stability=False, ai_no_subtitles=True,
    ai_anti_pop=False, ai_strict_validation=True, ai_provider="deepseek",
    ai_api_key="", ai_endpoint="", ai_model="", ai_timeout=30,
    gh_state_json="", prompt_override=None,
    no_subtitle=True, soundscape=False, music=False,
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
expect("ai-占位Key环境变量", dict(prompt_source="ai", ai_text="A woman stands still.", ai_mode="t2va"),
       "API Key 无效")

# 1b) AI 无 Key（清空环境变量）：应报“请设置服务端 DEEPSEEK_API_KEY”
expect("ai-无Key", dict(prompt_source="ai", ai_text="A woman stands still.", ai_mode="t2va"),
       "DEEPSEEK_API_KEY", strip_env=True)

# 2) 时长守卫：引擎生成仅支持 2–15 秒
expect("ai-超时长", dict(prompt_source="ai", ai_text="A woman stands still.", duration_seconds=20),
       "2–15 秒")

# 3) offline：quickjs 引擎执行 + 严格校验通过后，失败点应为文本编码器缺失
expect("offline-引擎后加载失败", dict(prompt_source="offline", ai_text="A woman stands still.", ai_language="en"),
       "not found")

# 4) format ref2va：format 路径同样先于模型加载
ref2va_text = (
    "subject_definitions:\n<Subject 1> is the woman in <Picture 1>, with long dark hair.\n\n"
    "summary:\n[reference generation] The target video shows <Subject 1> standing still.\n\n"
    "retention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - identity retained.\n\n"
    "detailed_description:\n[Shot 1] <Subject 1> stands still in the scene from <Picture 1>.\n\n"
    "overall_soundscape:\nN/A\n\nnon_diegetic_music:\nN/A"
)
expect("format-ref2va", dict(prompt_source="format", ai_text=ref2va_text,
                             ai_mode="ref2va", main_mode="all_reference", task_type="ref2va"),
       "not found")

faulthandler.cancel_dump_traceback_later()
loop.close()
print("RESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
