const { host, run } = require("./tests/panel-generation.cjs");
const p = host();
p.loadOptimizerSettings = async () => p.optimizerSettings;
const trace = [];
p.tracePush = s => trace.push(s);
const origRun = run;
const polish = (async () => {
  try {
    // 分步执行润色前置检查
    p.tracePush("active:" + !!p.activePromptOperation);
    p.tracePush("panelRemoved:" + p.panelRemoved);
    p.tracePush("upstream:" + p.upstreamConnected());
    p.tracePush("graph:" + (p.node.graph === p.app.graph));
    p.tracePush("mode:" + p.node.mode);
    p.tracePush("prompt:" + p.prompt.value);
    const r = await origRun(p, "runPromptOptimization()");
    p.tracePush("returned:" + r);
  } catch (e) { p.tracePush("threw:" + e.message); }
})();
setTimeout(() => {
  console.log("trace:", JSON.stringify(trace));
  console.log("requests:", p.requests.length);
  console.log("alerts:", JSON.stringify(p.alerts));
  console.log("configPromptShown:", p.configPromptShown);
}, 400);
