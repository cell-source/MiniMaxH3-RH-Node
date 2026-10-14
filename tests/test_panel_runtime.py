"""Exercise the production UI fill and execute's prompt resolution without loading GPU models."""
import ast
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import types
import unittest

from h3kit.rules import generate_prompt
from h3kit.tagging import apply_advanced_constraints

ROOT = Path(__file__).resolve().parents[1]


def _module_constants(tree):
    """从模块 AST 里求值所有顶层常量赋值。

    沙盒只 exec 被抽出的函数，因此它们引用的模块级常量必须显式注入；
    自动收集可以避免以后新增常量就让这个测试悄悄失效。
    """
    constants = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if not isinstance(target, ast.Name):
                continue
            try:
                expression = ast.Expression(body=statement.value)
                constants[target.id] = eval(
                    compile(ast.fix_missing_locations(expression), "<constant>", "eval"),
                    {"frozenset": frozenset, "set": set, "tuple": tuple, "list": list},
                    {},
                )
            except Exception:
                continue
    return constants


@unittest.skipUnless(shutil.which("node"), "Node.js required for UI → execution integration")
class PanelRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / "h3kit/node.py").read_text(encoding="utf-8"))
        execute = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "execute")
        execute.decorator_list = []
        end = next(i for i, statement in enumerate(execute.body) if isinstance(statement, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "keyframe_open" for target in statement.targets))
        execute.body = execute.body[:end] + [ast.Return(value=ast.Name(id="prompt", ctx=ast.Load()))]
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in {"_as_int", "_coerce_bool", "_parse_panel_state", "_recover_panel_state"}]
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
            # 模块级常量（采样器选项、哨兵值、状态模式…）由 AST 自动收集注入
            **_module_constants(tree),
            "json": json, "PRIMARY_SAMPLER": "test", "PRIMARY_SCHEDULER": "test",
            "SAMPLER_CHOICES": ["test"], "SCHEDULER_CHOICES": ["test"],
            "model_management": types.SimpleNamespace(throw_exception_if_processing_interrupted=lambda: None),
            "apply_advanced_constraints": apply_advanced_constraints,
            "make_client": fake_make_client, "generate_prompt": fake_generate_prompt,
        }
        exec(compile(module, "h3kit/node.py prompt resolution", "exec"), context)
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
const c = host({prompt_origin: input.source});
run(c, 'migrateGenerationOptions()');
for (const [name, value] of Object.entries({ai_ambience: input.enabled, ai_score: input.enabled, ai_subtitle_free: false}))
    run(c, `setGenerationOption(${JSON.stringify(name)}, ${value})`);
c.result = input.prompt;
run(c, 'applyOptimizedPrompt(result, prompt.value)');
console.log(JSON.stringify(Object.fromEntries(c.node.widgets.map(w => [w.name,w.value]))));
'''
                    completed = subprocess.run(["node", "-e", script, json.dumps({"source": source, "enabled": enabled, "prompt": result["prompt"]})],
                                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True)
                    widgets = json.loads(completed.stdout)
                    # 迁移 v2：所有旧来源归一为 panel（编辑器内容永远优先）；
                    # ai_idea 永远镜像编辑器内容。
                    self.assertEqual(widgets["prompt_origin"], "panel")
                    self.assertEqual(widgets["ai_idea"], widgets["prompt"])
                    args = {name: None for name, parameter in inspect.signature(self.resolve).parameters.items()
                            if parameter.default is inspect.Parameter.empty}
                    args.update({name: value for name, value in widgets.items() if name in inspect.signature(self.resolve).parameters})
                    args.update(panel_mode="text_keyframes", clip_seconds=5, frame_ratio="16:9",
                                panel_state=json.dumps({"mode": "text_keyframes", "prompts": {"text_keyframes": widgets["prompt"]}}))
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
