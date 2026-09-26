const assert = require("assert");
import("./tests/panel-generation.cjs").then(m => {
  const { host, run } = m;
  const value = (ctx, name) => ctx.node.widgets.find(w => w.name === name).value;
  const keys = host();
  keys.optimizerSettings = { provider: "deepseek", api_keys: { deepseek: "old", openai: "other" }, api_key: "old", has_api_key: true };
  keys.providerApiKeys = { deepseek: "old", openai: "other" };
  keys.key = { value: "replacement" }; keys.genKey = { value: "gen-key" };
  keys.genProvider = { value: "deepseek" }; keys.provider = { value: "deepseek" };
  run(keys, `providerApiKeys[provider.value] = key.value.trim(); setSharedKey(provider.value, key.value.trim());`);
  run(keys, `providerApiKeys[genProvider.value] = genKey.value.trim(); setSharedKey(genProvider.value, genKey.value.trim());`);
  run(keys, `setWidget(node, "ai_api_key", genKey.value.trim());`);
  console.log("chk3:", JSON.stringify(value(keys, "ai_api_key")));
  console.log("widget obj:", JSON.stringify(keys.node.widgets.find(w => w.name === "ai_api_key")));
});
