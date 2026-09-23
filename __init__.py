"""RunningHub H3 all-in-one node: prompts, conditioning, sampling, AV decode.

2026-09-24 整合：原 7 个自定义节点合并为单个 MiniMaxH3IntegrationRH。
提示词引擎（prompt_runtime/llm_client）与可选二段放大实现（tiled_sampler/
latent_upscaler，暂未注册）仍随包分发。
"""

WEB_DIRECTORY = "./web"


def comfy_entrypoint():
    from . import prompt_optimizer
    from .video_nodes import MiniMaxH3IntegrationExtension

    prompt_optimizer.register_prompt_optimizer_routes()
    return MiniMaxH3IntegrationExtension()
