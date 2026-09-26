"""Exercise the production UI fill and execute's prompt resolution without loading GPU models."""
import ast
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import types
import unittest

from prompt_runtime import generate_prompt
from prompt_tags import apply_advanced_constraints

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js required for UI → execution integration")
class PanelRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / "video_nodes.py").read_text(encoding="utf-8"))
        execute = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "execute")
        execute.decorator_list = []
        end = next(i for i, statement in enumerate(execute.body) if isinstance(statement, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "first_frame" for target in statement.targets))
        execute.body = execute.body[:end] + [ast.Return(value=ast.Name(id="prompt", ctx=ast.Load()))]
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in {"_coerce_int", "_coerce_bool", "_restore_ui_state"}]
        module = ast.fix_missing_locations(ast.Module(body=helpers + [execute], type_ignores=[]))
        cls.generation_inputs = []
        cls.client_calls = []

        def fake_generate_prompt(text, operation, controls, llm=None, check_interrupt=lambda: None):
            # 非面板来源在运行期处理编辑器内容：记录收到的输入文本并回显。
            cls.generation_inputs.append(text)
            return {"prompt": f"<GENERATED:{text}>", "valid": True, "report": "ok"}

        def fake_make_client(*args, **kwargs):
            cls.client_calls.append(args)
            return lambda system, user: "unused"

        context = {
            "json": json, "DEFAULT_SAMPLER_NAME": "test", "DEFAULT_SCHEDULER_NAME": "test",
            # A4 守卫（video_nodes.execute）引用的选项表：沙盒默认值 "test" 必须在表内，
            # 否则守卫会把测试传入的采样器改写回默认。
            "SAMPLER_OPTIONS": ["test"], "SCHEDULER_OPTIONS": ["test"],
            "model_management": types.SimpleNamespace(throw_exception_if_processing_interrupted=lambda: None),
            "apply_advanced_constraints": apply_advanced_constraints,
            "make_client": fake_make_client, "generate_prompt": fake_generate_prompt,
        }
        exec(compile(module, "video_nodes.py prompt resolution", "exec"), context)
        cls.resolve = staticmethod(context["execute"])

    def test_generated_options_and_old_sources_survive_execution(self):
        source_text = "integrated_multimodal_description: [Shot 1] 女子站着。\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A"
        for source in ("panel", "ai", "offline", "format"):
            for enabled in (False, True):
                with self.subTest(source=source, sound=enabled):
                    self.generation_inputs.clear()
                    self.client_calls.clear()
                    result = generate_prompt(source_text, "format", {"mode": "t2va", "lang": "zh",
                        "enrich_soundscape": enabled, "enrich_music": enabled, "no_subtitles": False})
                    script = '''
const { host, run } = require('./tests/panel-generation.cjs');
const input = JSON.parse(process.argv[1]);
const c = host({prompt_source: input.source});
run(c, 'migrateGenerationOptions()');
for (const [name, value] of Object.entries({ai_soundscape: input.enabled, ai_music: input.enabled, ai_no_subtitles: false}))
    run(c, `setGenerationOption(${JSON.stringify(name)}, ${value})`);
c.result = input.prompt;
run(c, 'applyOptimizedPrompt(result, prompt.value)');
console.log(JSON.stringify(Object.fromEntries(c.node.widgets.map(w => [w.name,w.value]))));
'''
                    completed = subprocess.run(["node", "-e", script, json.dumps({"source": source, "enabled": enabled, "prompt": result["prompt"]})],
                                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True)
                    widgets = json.loads(completed.stdout)
                    # 迁移 v2：所有旧来源归一为 panel（编辑器内容永远优先）；
                    # ai_text 永远镜像编辑器内容。
                    self.assertEqual(widgets["prompt_source"], "panel")
                    self.assertEqual(widgets["ai_text"], widgets["prompt"])
                    args = {name: None for name, parameter in inspect.signature(self.resolve).parameters.items()
                            if parameter.default is inspect.Parameter.empty}
                    args.update({name: value for name, value in widgets.items() if name in inspect.signature(self.resolve).parameters})
                    args.update(main_mode="text_keyframes", duration_seconds=5, aspect="16:9",
                                gh_state_json=json.dumps({"mode": "text_keyframes", "prompts": {"text_keyframes": widgets["prompt"]}}))
                    actual = self.resolve(**args)
                    # 面板来源直接运行编辑器内容：不触发旧生成器或远程客户端。
                    self.assertEqual(self.generation_inputs, [])
                    self.assertEqual(self.client_calls, [])
                    self.assertIn("女子站着。", actual)
                    self.assertEqual("贴合画面环境的环境声自然延续" in actual, enabled)
                    self.assertEqual("慢速而克制的钢琴独奏" in actual, enabled)
                    self.assertNotIn("Every spoken line", actual)


if __name__ == "__main__":
    unittest.main()
