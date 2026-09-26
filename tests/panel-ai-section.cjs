/* 面板隐藏 widget、服务商清单、布局及翻译的静态一致性检查。
   生成、恢复、撤销、互斥与取消行为由 panel-generation.cjs 执行生产函数验证。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const js = fs.readFileSync(path.join(root, 'web/js/minimax_h3_integration.js'), 'utf8');
const py = fs.readFileSync(path.join(root, 'llm_client.py'), 'utf8');
let assertions = 0;
const check = (cond, label) => { assert.ok(cond, label); assertions++; };

/* 1. 隐藏清单完整 */
const hideBlock = js.match(/const AI_GROUP_WIDGETS = \[([\s\S]*?)\];/);
check(!!hideBlock, 'AI_GROUP_WIDGETS defined');
const hiddenNames = [...hideBlock[1].matchAll(/"([^"]+)"/g)].map(m => m[1]);
for (const name of ['prompt_source', 'ai_text', 'ai_language', 'ai_mode', 'ai_enrich', 'ai_soundscape',
    'ai_music', 'ai_auto_timestamps', 'ai_fixed_camera', 'ai_visual_stability', 'ai_no_subtitles',
    'ai_anti_pop', 'ai_strict_validation', 'ai_provider', 'ai_api_key', 'ai_endpoint', 'ai_model', 'ai_timeout']) {
    check(hiddenNames.includes(name), 'hidden list contains ' + name);
}
check(hiddenNames.length === 18, 'hidden list size = 18 (prompt_source + 17 ai_*)');

/* 2. AI 区块已删除：ai_* widget 保持隐藏序列化，配置由统一弹窗/生成弹窗承担 */
check(!js.includes('details.mxv-ai') && !js.includes('addAi('), 'AI block removed from panel');
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
const pyBlock = py.match(/PROVIDERS = \{([\s\S]*?)\n\};?\n/);
check(!!pyBlock, 'llm_client PROVIDERS block found');
const pyList = [...pyBlock[1].matchAll(/^    "([a-z_]+)":/gm)].map(m => m[1]).sort();
check(JSON.stringify(feList) === JSON.stringify(pyList),
    `providers sync fe=${feList.join(',')} py=${pyList.join(',')}`);

/* 5. 布局与配色基线（mxv 重设计） */
check(js.includes('display: "flex"') && js.includes('flexFlow: "column"'), 'panel root is column flex');
check(js.includes('measureContentHeight') && js.includes('node.setSize([WIDTH, nextHeight])'),
    'panel height auto-fits content (no scrollbar design)');
check(js.includes('advanced.open = true;'), 'advanced accordion defaults to fully expanded');
check(js.includes('.mxv-advanced,.mxv-ai{border:1px solid #383e46;border-radius:6px;background:#1b1e23'),
    'details cards use mxv warm-charcoal baseline');
check(js.includes('.mxv-prompt-wrap.focused{border-color:#e8a33d'), 'prompt editor shows amber focus border');
check(!js.includes('border-left:3px solid #e8a33d'), 'old constant accent bar removed');
check(js.includes('.mxv-modes{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:3px'),
    'mode switcher is pill segmented style');
check(js.includes('.mxv-advanced>summary:before,.mxv-ai>summary:before'), 'accordion summaries have CSS ::before arrow');
check(!js.includes('mxv-disclose'), 'JS-injected arrow span removed (wiped by textContent updates)');
check(js.includes('flex-shrink:0}'), 'accordion and panel children protected from flex shrinking');
check(js.includes('root.className = "mxv-root"'), 'panel root has mxv-root class');
check(!/position:absolute;left:8px;right:8px;bottom:6px/.test(js), 'no absolute overlay for advanced');

/* 5b. 与 GHX 参考项目的指纹分离：不允许出现旧前缀与旧蓝系配色 */
check(!js.includes('ghh3') && !js.includes('gh_h3'), 'legacy ghh3 prefix fully removed');
for (const legacy of ['#111c27', '#0aa4d6', '#334a5d', '#1d2731', '#24384a']) {
    check(!js.includes(legacy), 'legacy GHX color removed: ' + legacy);
}

/* 6. 旧折叠实现已移除 */
check(!js.includes('installAiGroupFold'), 'old fold impl removed');
check(!js.includes('_h3OrigSizeFn'), 'old size cache removed');

/* 7. DOM 中文覆盖与节点选项保持一致；英文仍使用原文。 */
const vm = require('node:vm');
const translations = js.slice(js.indexOf('const DOM_TRANSLATIONS'), js.indexOf('function currentLocale'));
const translate = js.match(/function t\(text\) \{[^\n]+\}/)[0];
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
check(!js.includes('beginSection("Polish settings (') && !js.includes('beginSection("AI generation settings ('),
    'section titles stay short (zone hint lives in the translation)');
const nodeDefs = JSON.parse(fs.readFileSync(path.join(root, 'locales/zh/nodeDefs.json'), 'utf8'));
const definition = Object.values(nodeDefs).find(d => d.inputs?.prompt_source);
check(!!definition, 'localized integration node found');
for (const name of ['prompt_source', 'ai_language', 'ai_mode', 'ai_provider']) {
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
    widget: (_, name) => ({ value: name === 'ai_mode' ? 'auto' : '' }),
    imageSlots: ['first_frame', 'last_frame', 'ref_image_1'], videoSlots: ['ref_video_1'],
    audioSlots: ['hybrid_audio', 'ref_audio_1'],
});
vm.runInContext(js.slice(js.indexOf('    function nextSlot(kind)'), js.indexOf('    const taskLabels')), modeContext);
for (const [slots, task] of [[[], 'T2VA'], [['first_frame'], 'I2VA'], [['last_frame'], 'L2VA'],
    [['first_frame', 'last_frame'], 'FL2VA'], [['first_frame', 'last_frame', 'hybrid_audio'], 'Hybrid']]) {
    modeContext.media = new Map(slots.map(slot => [slot, {}]));
    check(vm.runInContext('resolvedTaskType()', modeContext) === task, 'material mode: ' + task);
}
modeContext.media = new Map();
check(vm.runInContext('nextSlot("audio")', modeContext) === 'hybrid_audio', 'keyframe audio stays visible');
modeContext.widget = (_, name) => ({ value: name === 'ai_mode' ? 'l2va' : '' });
check(vm.runInContext('nextSlot("image")', modeContext) === 'last_frame', 'last-frame mode routes images');
modeContext.state.mode = 'all_reference';
check(vm.runInContext('resolvedTaskType()', modeContext) === 'Ref2VA', 'empty reference mode stays reference');
for (const kind of ['image', 'video', 'audio']) {
    check(vm.runInContext(`nextSlot("${kind}")`, modeContext) === `ref_${kind}_1`, 'reference slot: ' + kind);
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
for (const name of ['ai_enrich', 'ai_soundscape', 'ai_music', 'ai_auto_timestamps', 'ai_fixed_camera',
    'ai_no_subtitles', 'ai_anti_pop', 'ai_strict_validation']) {
    check(dialogBody.includes(`"${name}"`), 'config dialog switch writes hidden widget: ' + name);
}
check(dialogBody.includes('actions.append(cancel, save)'), 'config dialog is pure config (cancel/save only)');
check(!dialogBody.includes('polishButton') && !dialogBody.includes('runButton') && !dialogBody.includes('formatButton'),
    'no action buttons left in the config dialog');
check(!dialogBody.includes('legacyIdea') && !js.includes('Idea / prompt to rewrite'), 'legacy idea textarea removed from the config dialog');
check(dialogBody.includes('source.onchange = () => { setWidget(node, "prompt_source", source.value); promptRevision++; mirrorSourceIdea(); persistState(); }'),
    'source switch mirrors the editor content into the runtime input');
check(js.includes('const route = operation === "ai" ? "generate" : "format";')
    && js.includes('`${OPTIMIZER_ROUTE}/${route}`'), 'toolbar actions post to generate/format endpoints');
check(dialogBody.includes('genRow("Generation language", genLanguage)') && dialogBody.includes('genRow("AI timeout", genTimeout)'),
    'generation language/timeout laid out flat in the unified dialog');
check(dialogBody.includes('genLanguage.onchange = () => setWidget(node, "ai_language", genLanguage.value)')
    && dialogBody.includes('setWidget(node, "ai_timeout", value)'), 'dialog keeps language/timeout in widgets');
check(!js.includes('mxv-gen-more') && !dialogBody.includes('make("details")'),
    'no collapse wrapper inside the config dialog');
check(js.includes('function mirrorSourceIdea()') && js.includes('function syncEditorSource()')
    && !js.includes('useEditorPrompt'), 'editor content mirrors into the runtime source input (actions never flip the source)');
check(js.split('mirrorSourceIdea()').length - 1 >= 6, 'mirror runs on edit/undo/fill/reset/source-switch/restore');

/* 9. 批次 B：高级选项去重——无字幕/音景/配乐三行已删除，严格提示词标签保留 */
check(!js.includes('addAdvanced("no_subtitle"') && !js.includes('addAdvanced("soundscape"') && !js.includes('addAdvanced("music"'),
    'duplicated advanced rows removed (no_subtitle/soundscape/music)');
check(js.includes('addAdvanced("strict_prompt_tags", "Strict prompt tags"'), 'strict prompt tags stays in advanced');
check(!js.includes('"no_subtitle", "soundscape", "music"]'), 'visibility lists no longer reference removed rows');

/* 10. 批次 B：服务端引擎出口——generate（在线）/format（离线）路由与引擎 controls 对齐 */
const optimizerPy = fs.readFileSync(path.join(root, 'prompt_optimizer.py'), 'utf8');
check(optimizerPy.includes('routes.post("/rh/minimax-h3/prompt-optimizer/generate")(generate_prompt_api)'),
    'backend registers /generate route');
check(optimizerPy.includes('routes.post("/rh/minimax-h3/prompt-optimizer/format")(format_prompt_api)'),
    'backend registers /format route');
const controlsNames = optimizerPy.match(/_H3_GENERATION_CONTROLS = \(([\s\S]*?)\)/);
check(!!controlsNames, 'engine controls tuple defined');
const controlsList = controlsNames[1].match(/"([a-z_]+)"/g).map(s => s.replace(/"/g, ''));
check(JSON.stringify(controlsList) === JSON.stringify(['enrich_do_enrich', 'enrich_soundscape', 'enrich_music',
    'auto_timestamps', 'fixed_camera', 'visual_stability', 'no_subtitles', 'anti_pop']),
    'engine controls match the video_nodes ai path');
check(optimizerPy.includes('asyncio.to_thread(_run_generation_operation, payload, operation, check_interrupt)'),
    'generation runs off the event loop (to_thread)');

console.log(`PASS: ${assertions} assertions (panel AI section consistency)`);
