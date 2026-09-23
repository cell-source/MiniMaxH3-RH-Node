"""Read-only CPU smoke test against an existing ComfyUI checkout."""
import asyncio
import faulthandler
import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path

faulthandler.dump_traceback_later(45, repeat=False)

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
# Route registration needs only the route table; do not start a server, scan
# assets, or modify the user's ComfyUI database during a smoke test.
PromptServer.instance = SimpleNamespace(routes=web.RouteTableDef())
spec = importlib.util.spec_from_file_location("h3_rh_test", root / "__init__.py", submodule_search_locations=[str(root)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
classes = loop.run_until_complete(package.comfy_entrypoint().get_node_list())
assert len(classes) == 1, [cls.GET_SCHEMA().node_id for cls in classes]
schema = classes[0].GET_SCHEMA()
print(schema.node_id, "-", schema.display_name)
assert schema.node_id == "MiniMaxH3IntegrationRH"

# Check the examples against real node schemas, without loading model weights.
import nodes as comfy_nodes
from comfy_extras import nodes_custom_sampler, nodes_video
registry = {cls.GET_SCHEMA().node_id: cls for cls in classes}
registry.update(UNETLoader=comfy_nodes.UNETLoader,
    CreateVideo=nodes_video.CreateVideo, SaveVideo=nodes_video.SaveVideo)
examples = sorted((root / "examples").glob("*.api.json"))
assert examples, "examples/*.api.json not found"
for path in examples:
    graph = json.loads(path.read_text(encoding="utf-8"))
    for node_id, record in graph.items():
        cls = registry[record["class_type"]]
        schema = cls.INPUT_TYPES()
        inputs = {**schema.get("required", {}), **schema.get("optional", {})}
        assert set(record["inputs"]) <= inputs.keys(), (path.name, node_id, set(record["inputs"]) - inputs.keys())
        assert set(schema.get("required", {})) <= record["inputs"].keys(), (path.name, node_id)
        for name, value in record["inputs"].items():
            if not isinstance(value, list):
                continue
            source = registry[graph[value[0]]["class_type"]]
            if hasattr(source, "GET_SCHEMA"): source.GET_SCHEMA()
            assert source.RETURN_TYPES[value[1]] == inputs[name][0], (path.name, node_id, name)
    print(f"Example {path.name}: required inputs + linked port types PASS")
faulthandler.cancel_dump_traceback_later()
loop.close()
