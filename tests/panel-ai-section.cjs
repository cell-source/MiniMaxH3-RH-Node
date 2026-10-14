/* 面板隐藏 widget、服务商清单、布局及翻译的静态一致性检查。
   生成、恢复、撤销、互斥与取消行为由 panel-generation.cjs 执行生产函数验证。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const js = fs.readFileSync(path.join(root, 'web/js/h3kit_panel.js'), 'utf8');
const py = fs.readFileSync(path.join(root, 'h3kit/llm.py'), 'utf8');
let assertions = 0;
const check = (cond, label) => { assert.ok(cond, label); assertions++; };

/* 1. 隐藏清单完整 */
const hideBlock = js.match(/const AI_GROUP_WIDGETS = \[([\s\S]*?)\];/);
check(!!hideBlock, 'AI_GROUP_WIDGETS defined');
const hiddenNames = [...hideBlock[1].matchAll(/"([^"]+)"/g)].map(m => m[1]);
for (const name of ['prompt_origin', 'ai_idea', 'ai_locale', 'ai_task', 'ai_expand', 'ai_ambience',
    'ai_score', 'ai_timeline', 'ai_lock_camera', 'ai_stabilize', 'ai_subtitle_free',
    'ai_audio_guard', 'ai_strict_check', 'ai_platform', 'ai_key', 'ai_base_url', 'ai_model_name', 'ai_deadline']) {
    check(hiddenNames.includes(name), 'hidden list contains ' + name);
}
check(hiddenNames.length === 18, 'hidden list size = 18 (prompt_origin + 17 ai_*)');

/* 2. AI 区块已删除：ai_* widget 保持隐藏序列化，配置由统一弹窗/生成弹窗承担 */
check(!js.includes('details.h3k-ai') && !js.includes('addAi('), 'AI block removed from panel');
for (const name of hiddenNames) {
    check(js.includes(`"${name}"`), 'widget kept in hidden list: ' + name);
}

/* 3. 面板区块挂载（AI 区块已删除） */
check(js.includes('root.appendChild(advanced)'), 'advanced details mounted');
check(js.includes('root.appendChild(promptWrap)'), 'prompt wrap mounted');
check(!js.includes('root.appendChild(aiDetails)'), 'ai details no longer mounted');

/* 4. provider 清单前后端同步（AI 区块已删，清单由统一弹窗的 generationProviders 承担） */
const feProviders = js.match(/const generationProviders = \[([^\]]*)\]/);
check(!!feProviders, 'frontend generationProviders defined');
const feList = feProviders[1].match(/"([^"]+)"/g).map(s => s.replace(/"/g, '')).sort();
const pyBlock = py.match(/SERVICE_ENDPOINTS = \{([\s\S]*?)\n\};?\n/);
check(!!pyBlock, 'h3kit.llm SERVICE_ENDPOINTS block found');
const pyList = [...pyBlock[1].matchAll(/^    "([a-z_]+)":/gm)].map(m => m[1]).sort();
check(JSON.stringify(feList) === JSON.stringify(pyList),
    `providers sync fe=${feList.join(',')} py=${pyList.join(',')}`);

/* 5. 布局与配色基线（h3k 重设计：冷石墨 + 青绿） */
check(js.includes('display: "flex"') && js.includes('flexFlow: "column"'), 'panel root is column flex');
check(js.includes('measureContentHeight') && js.includes('node.setSize([WIDTH, nextHeight])'),
    'panel height auto-fits content (no scrollbar design)');
check(js.includes('advanced.open = true;'), 'advanced accordion defaults to fully expanded');
check(js.includes('.h3k-advanced,.h3k-ai{border:1px solid #2b3a49;border-radius:10px;background:#16202a'),
    'details cards use the cool graphite baseline');
check(js.includes('.h3k-prompt-wrap.focused{border-color:#38d3c6'), 'prompt editor shows a teal focus border');
check(!js.includes('#e8a33d') && !js.includes('rgba(232,163,61'),
    'warm amber accent fully replaced (no upstream palette left)');
check(js.includes('.h3k-header{display:flex') && js.includes('.h3k-modes{display:grid;grid-template-columns:auto auto'),
    'header bar merges the task badge with a compact mode switch');
check(js.indexOf('root.appendChild(promptWrap)') < js.indexOf('root.appendChild(advanced)'),
    'prompt editor sits above the advanced accordion');
check(js.includes('"Segoe UI",system-ui,sans-serif') && !js.includes('Arial,sans-serif'),
    'panel typography switched off the upstream font stack');
check(js.includes('.h3k-advanced>summary:before,.h3k-ai>summary:before'), 'accordion summaries have CSS ::before arrow');
check(!js.includes('h3k-disclose'), 'JS-injected arrow span removed (wiped by textContent updates)');
check(js.includes('flex-shrink:0}'), 'accordion and panel children protected from flex shrinking');
check(js.includes('root.className = "h3k-root"'), 'panel root has h3k-root class');
check(!/position:absolute;left:8px;right:8px;bottom:6px/.test(js), 'no absolute overlay for advanced');

/* 5b. 与 GHX 参考项目的指纹分离：不允许出现旧前缀与旧蓝系配色 */
/* 旧前缀只允许出现在读旧键的迁移常量里，不得用于类名/CSS/选择器 */
const legacyUses = (js.match(/ghh3/g) || []).length;
check(legacyUses === 2 && js.includes('LEGACY_STATE_KEYS') && js.includes('LEGACY_KEYCHAIN_KEYS'),
    `legacy ghh3 prefix survives only in the migration constants (found ${legacyUses})`);
check(!js.includes('gh_h3'), 'legacy gh_h3 prefix fully removed');
check(!/className\s*=\s*"[^"]*ghh3/.test(js), 'no legacy prefix inside class names');
for (const legacy of ['#111c27', '#0aa4d6', '#334a5d', '#1d2731', '#24384a']) {
    check(!js.includes(legacy), 'legacy GHX color removed: ' + legacy);
}

/* 6. 旧折叠实现已移除 */
check(!js.includes('installAiGroupFold'), 'old fold impl removed');
check(!js.includes('_h3OrigSizeFn'), 'old size cache removed');

/* 7. DOM 中文覆盖与节点选项保持一致；英文仍使用原文。 */
const vm = require('node:vm');
const translations = js.slice(js.indexOf('const DOM_TRANSLATIONS'), js.indexOf('function currentLocale'));
/* t() 允许单行或换行写法，这里只取函数体本身。 */
const translate = js.match(/function t\(text\) \{[\s\S]*?\n\}/)[0];
const context = vm.createContext({ chinese: true });
vm.runInContext(translations + '\nfunction isChineseLocale() { return chinese; }\n' + translate, context);
for (const match of js.matchAll(/\bt\("((?:[^"\\]|\\.)*)"\)/g)) {
    const label = JSON.parse('"' + match[1] + '"');
    context.label = label;
    check(vm.runInContext('Object.hasOwn(DOM_TRANSLATIONS, label)', context), 'translated DOM label: ' + label);
}
for (const match of js.matchAll(/(?:addAi|addAdvanced)\("[^"]+", "([^"]+)"|(?:row|checkboxRow|timeBox)\("([^"]+)"/g)) {
    context.label = match[1] || match[2];
    check(vm.runInContext('Object.hasOwn(DOM_TRANSLATIONS, label)', context), 'translated indirect label: ' + context.label);
}
/* beginSection 的分区标题同样必须走 t()：防止英文标题漏翻译再次上线。 */
for (const match of js.matchAll(/beginSection\("([^"]+)"\)/g)) {
    context.label = match[1];
    check(vm.runInContext('Object.hasOwn(DOM_TRANSLATIONS, label)', context), 'translated section title: ' + context.label);
}
check(!js.includes('beginSection("Rewriting settings (') && !js.includes('beginSection("AI drafting settings ('),
    'section titles stay short (zone hint lives in the translation)');
const nodeDefs = JSON.parse(fs.readFileSync(path.join(root, 'locales/zh/nodeDefs.json'), 'utf8'));
const definition = Object.values(nodeDefs).find(d => d.inputs?.prompt_origin);
check(!!definition, 'localized integration node found');
for (const name of ['ai_locale', 'ai_task', 'ai_platform']) {
    for (const [value, label] of Object.entries(definition.inputs[name].options)) {
        context.label = value;
        check(vm.runInContext('t(label)', context) === label, name + ' localized option: ' + value);
        context.chinese = false;
        check(vm.runInContext('t(label)', context) === value, name + ' English option preserved: ' + value);
        context.chinese = true;
    }
}

/* 恢复行为由 panel-generation.cjs 执行验证，不再匹配注释中的空桩调用。 */
check(!js.includes('aiRows'), 'deleted AI row registry stays deleted');

const modeContext = vm.createContext({
    state: { mode: 'text_keyframes' }, media: new Map(), node: {},
    widget: (_, name) => ({ value: name === 'ai_task' ? 'auto' : '' }),
    imageSlots: ['keyframe_open', 'keyframe_close', 'still_1'], videoSlots: ['footage_1'],
    audioSlots: ['keyframe_audio', 'track_1'],
});
vm.runInContext(js.slice(js.indexOf('    function nextSlot(kind)'), js.indexOf('    const taskLabels')), modeContext);
for (const [slots, task] of [[[], 'T2VA'], [['keyframe_open'], 'I2VA'], [['keyframe_close'], 'L2VA'],
    [['keyframe_open', 'keyframe_close'], 'FL2VA'], [['keyframe_open', 'keyframe_close', 'keyframe_audio'], 'Hybrid']]) {
    modeContext.media = new Map(slots.map(slot => [slot, {}]));
    check(vm.runInContext('resolvedTaskType()', modeContext) === task, 'material mode: ' + task);
}
modeContext.media = new Map();
check(vm.runInContext('nextSlot("audio")', modeContext) === 'keyframe_audio', 'keyframe audio stays visible');
modeContext.widget = (_, name) => ({ value: name === 'ai_task' ? 'l2va' : '' });
check(vm.runInContext('nextSlot("image")', modeContext) === 'keyframe_close', 'last-frame mode routes images');
modeContext.state.mode = 'all_reference';
check(vm.runInContext('resolvedTaskType()', modeContext) === 'Ref2VA', 'empty reference mode stays reference');
for (const [kind, prefix] of [['image', 'still'], ['video', 'footage'], ['audio', 'track']]) {
    check(vm.runInContext(`nextSlot("${kind}")`, modeContext) === `${prefix}_1`, 'reference slot: ' + kind);
}

/* 8. AI 生成/整理/润色一键直出 + 统一配置界面（2026-09-26）：配置只管配置，动作全在工具条 */
check(js.includes('const aiGeneratePrompt = make("button", {}, "✨")'), 'AI generate button created');
check(js.includes('const aiFormatPrompt = make("button", {}, "≡")'), 'offline format button created');
check(js.includes('const polishPrompt = make("button", {}, "✦")'), 'polish button restored on toolbar');
check(js.includes('promptTools.append(elapsedPrompt, optimizerModelName, resetPrompt, polishPrompt, aiFormatPrompt, aiGeneratePrompt, optimizerGear)'),
    'toolbar hosts ↻/✦/≡/✨/⚙ direct actions');
check(!js.includes('const optimizePrompt'), 'old polish button variable removed');
check(js.includes('aiGeneratePrompt.onclick = () => { if (ensureGenerationKey()) runGeneration("ai"); }'), '✨ generates directly per config');
check(js.includes('aiFormatPrompt.onclick = () => runGeneration("format");'), '≡ formats directly per config');
check(js.includes('if (optimizing) { await cancelOptimization(); return; }'), '✦ re-click cancels a running polish');
check(!js.includes('function openAiGenerateDialog'), 'separate AI generation dialog removed');
const dialogBody = js.slice(js.indexOf('function openOptimizerSettings()'), js.indexOf('function showOptimizerConfigPrompt()'));
check(!!dialogBody, 'unified config dialog defined');
for (const name of ['ai_expand', 'ai_ambience', 'ai_score', 'ai_timeline', 'ai_lock_camera',
    'ai_subtitle_free', 'ai_audio_guard', 'ai_strict_check']) {
    check(dialogBody.includes(`"${name}"`), 'config dialog switch writes hidden widget: ' + name);
}
check(dialogBody.includes('actions.append(cancel, save)'), 'config dialog is pure config (cancel/save only)');
check(!dialogBody.includes('polishButton') && !dialogBody.includes('runButton') && !dialogBody.includes('formatButton'),
    'no action buttons left in the config dialog');
check(!dialogBody.includes('legacyIdea') && !js.includes('Idea / prompt to rewrite'), 'legacy idea textarea removed from the config dialog');
check(!dialogBody.includes('row("Source"') && !dialogBody.includes('source.onchange'),
    'source dropdown removed (the editor content is always the input)');
check(js.includes('generationOptionsVersion < 2 && String(widget(node, "prompt_origin")?.value || "panel") !== "panel"'),
    'migration v2 normalizes legacy sources to panel');
check(js.includes('const route = operation === "ai" ? "generate" : "format";')
    && js.includes('`${OPTIMIZER_ROUTE}/${route}`'), 'toolbar actions post to generate/format endpoints');
check(dialogBody.includes('genRow("Drafting language", genLanguage)') && dialogBody.includes('genRow("AI timeout", genTimeout)'),
    'generation language/timeout laid out flat in the unified dialog');
check(dialogBody.includes('genLanguage.onchange = () => setWidget(node, "ai_locale", genLanguage.value)')
    && dialogBody.includes('setWidget(node, "ai_deadline", value)'), 'dialog keeps language/timeout in widgets');
check(!js.includes('h3k-gen-more') && !dialogBody.includes('make("details")'),
    'no collapse wrapper inside the config dialog');
check(js.includes('function mirrorSourceIdea()') && js.includes('function syncEditorSource()')
    && !js.includes('useEditorPrompt'), 'editor content mirrors into the runtime source input (actions never flip the source)');
check(js.split('mirrorSourceIdea()').length - 1 >= 6, 'mirror runs on edit/undo/fill/reset/source-switch/restore');

/* 8b. 润色（✦ 子功能）沿用主区生成配置（2026-09-26）：无独立平台/Key/语言配置 */
const polishBody = js.slice(js.indexOf('async function runPromptOptimization('), js.indexOf('const autoOptimizeBeforeQueue ='));
check(!!polishBody, 'polish runner defined');
// 单一真源（2026-09-26 审查修复）：平台/模型/端点/语言只从生成 widget 读。
check(js.includes('const polishText = value => (typeof value === "string" ? cleanPrompt(value) : "")')
    && js.includes('const polishProviderValue = () => (polishText(widget(node, "ai_platform")?.value) || "deepseek").toLowerCase()')
    && js.includes('const polishModelValue = () => polishText(widget(node, "ai_model_name")?.value)')
    && js.includes('const polishEndpointValue = () => polishText(widget(node, "ai_base_url")?.value)'),
    'polish value helpers read the main generation widgets and ignore non-string values');
check(polishBody.includes('const polishProvider = polishProviderValue()')
    && polishBody.includes('const polishModel = polishModelValue()')
    && polishBody.includes('const polishEndpoint = polishEndpointValue()'),
    'polish config built from the main generation widgets');
check(polishBody.includes('const polishOutputLanguage = polishOutputLanguageValue()'),
    'polish output language follows the main generation language');
check(!dialogBody.includes('"Rewriting platform"') && !dialogBody.includes('row("Output language"'),
    'polish-only provider/language rows removed from the config dialog');
check(!dialogBody.includes('runninghubModel') && !dialogBody.includes('localModel'),
    'runninghub/local vision model config removed with the polish provider rows');
check(dialogBody.includes('polishCheckRow("Send visuals to the model", readMedia)')
    && dialogBody.includes('polishCheckRow("Rewrite wording automatically before each run", autoOptimize)'),
    'polish behavior toggles stay in the config dialog');

/* 8c. 润色请求正确性（2026-09-26 审查修复）：协议预设优先、custom 守卫、缓存签名读真实配置源 */
check(polishBody.includes('...(polishProvider === "custom" ? { protocol: "openai" } : {})'),
    'protocol is left to the backend preset except for custom');
check(!/protocol:\s*polishProvider ===/.test(polishBody), 'polish no longer ships its own protocol map');
check(polishBody.includes('polishProvider === "custom" && (!polishEndpoint || !polishModel)'),
    'custom polish guard requires both endpoint and model');
const signatureBody = js.slice(js.indexOf('function optimizerContextSignature('), js.indexOf('function optimizerTaskContext('));
check(signatureBody.includes('polishProviderValue(), polishModelValue(), polishEndpointValue(), polishOutputLanguageValue()')
    && !signatureBody.includes('optimizerSettings.provider')
    && !signatureBody.includes('optimizerSettings.model'),
    'polish cache signature reads generation widgets (not stale optimizerSettings)');
/* 8d. 陈旧 optimizerSettings 不再参与润色（2026-09-26 审查） */
const mediaPayloadBody = js.slice(js.indexOf('async function optimizerMediaPayload('), js.indexOf('function optimizerContextSignature('));
check(mediaPayloadBody.includes('const runninghub = ["runninghub", "runninghub_overseas"].includes(polishProviderValue())')
    && !mediaPayloadBody.includes('optimizerSettings?.provider'),
    'runninghub media branch decided by the generation widget, not stale optimizerSettings');
const optimizerNameBody = js.slice(js.indexOf('const configuredOptimizerName = () =>'), js.indexOf('const refreshOptimizerName = () =>'));
check(optimizerNameBody.includes('const rawName = polishModelValue().split("/").pop()')
    && optimizerNameBody.includes('})[polishProviderValue()] || "API"')
    && !optimizerNameBody.includes('optimizerSettings'),
    'toolbar model badge shows the actual polish provider/model');

/* 8f. 加载修复（2026-09-26 审查 A4）：位移冲空的采样器组合在 onConfigure 时回填默认 */
check(js.includes('for (const name of ["sampler_id", "scheduler_id"]) {')
    && js.includes('combo.value = combo.options.values[0]')
    && js.includes('timeoutWidget.value = 180'),
    'empty sampler/scheduler_id/timeout are repaired to schema defaults on configure');

/* 9. 批次 B：更多设置去重——无字幕/音景/配乐三行已删除，标签格式强校验保留 */
check(!js.includes('addAdvanced("subtitle_free"') && !js.includes('addAdvanced("soundscape"') && !js.includes('addAdvanced("music"'),
    'duplicated advanced rows removed (subtitle_free/soundscape/music)');
check(js.includes('addAdvanced("tag_guard", "Tag format enforced"'), 'strict prompt tags stays in advanced');
check(!js.includes('"subtitle_free", "soundscape", "music"]'), 'visibility lists no longer reference removed rows');

/* 10. 批次 B：服务端引擎出口——generate（在线）/format（离线）路由与引擎 controls 对齐 */
const optimizerPy = fs.readFileSync(path.join(root, 'h3kit/optimizer.py'), 'utf8');
check(optimizerPy.includes('("deepseek", "https://api.deepseek.com", "deepseek-flash", "openai")')
    && optimizerPy.includes('("glm", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flash", "openai")'),
    'polish backend has deepseek/glm presets aligned with h3kit.llm');
check(optimizerPy.includes('(preset[0] if preset else "")') && optimizerPy.includes('(preset[1] if preset else "")'),
    'unknown provider no longer falls back to the runninghub endpoint/model');
check(optimizerPy.includes('str((preset[2] if preset else None) or raw.get("protocol") or "openai")'),
    'preset protocol wins over the client value (unknown provider defaults to openai)');
check(optimizerPy.includes('_logged_upstream_error') && !/\{(?:body|text)\[:\d+\]/.test(optimizerPy),
    'upstream response bodies are logged, not returned to the client');
/* 8e. 遗留本地视觉模型模式归一（2026-09-26 审查 L7） */
const optimizerDefaultMigration = js.slice(js.indexOf('const migrateLegacyOptimizerDefault ='), js.indexOf('const loadOptimizerSettings ='));
check(optimizerDefaultMigration.includes('if (settings.mode === "local") settings = { ...settings, mode: "api" }'),
    'legacy local optimizer mode is normalized to api');
check(optimizerPy.includes('("post", "/rh/minimax-h3/prompt-optimizer/generate", generate_prompt_api)'),
    'backend registers /generate route');
check(optimizerPy.includes('("post", "/rh/minimax-h3/prompt-optimizer/format", format_prompt_api)'),
    'backend registers /format route');
const controlsNames = optimizerPy.match(/_H3_GENERATION_CONTROLS = \(([\s\S]*?)\)/);
check(!!controlsNames, 'engine controls tuple defined');
const controlsList = controlsNames[1].match(/"([a-z_]+)"/g).map(s => s.replace(/"/g, ''));
check(JSON.stringify(controlsList) === JSON.stringify(['enrich_do_enrich', 'enrich_soundscape', 'enrich_music',
    'auto_timestamps', 'fixed_camera', 'visual_stability', 'no_subtitles', 'anti_pop']),
    'engine controls match the node ai path');
check(optimizerPy.includes('asyncio.to_thread(_run_generation_operation, payload, operation, check_interrupt)'),
    'generation runs off the event loop (to_thread)');

console.log(`PASS: ${assertions} assertions (panel AI section consistency)`);
