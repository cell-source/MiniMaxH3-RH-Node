/* 面板 AI 区块静态一致性测试：
   1) AI_GROUP_WIDGETS 与面板 addAi/绑定清单一一对应（无遗漏、无多余）
   2) 前端 AI_PROVIDERS 与 llm_client.PROVIDERS 同步
   3) hideWidget 隐藏列表包含全部 AI widget
   4) 布局/配色基线存在（flex 流 + .ghh3-box 同款配色被 details 引用） */
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

/* 2. 面板区块为每个隐藏 widget 建立了控件绑定 */
for (const name of hiddenNames) {
    check(js.includes(`addAi("${name}",`),
        'panel binds ' + name);
}

/* 3. 两个 details 区块都挂在面板根上 */
check(js.includes('root.appendChild(aiDetails)'), 'ai details mounted');
check(js.includes('root.appendChild(advanced)'), 'advanced details mounted');
check(js.includes('root.appendChild(promptWrap)'), 'prompt wrap mounted');

/* 4. provider 清单前后端同步 */
const feProviders = js.match(/const AI_PROVIDERS = \[([^\]]*)\]/);
check(!!feProviders, 'frontend AI_PROVIDERS defined');
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
check(js.includes('advanced.open = true;') && js.includes('aiDetails.open = true;'),
    'accordions default to fully expanded');
check(js.includes('.mxv-advanced,.mxv-ai{border:1px solid #383e46;border-radius:6px;background:#1b1e23'),
    'details cards use mxv warm-charcoal baseline');
check(js.includes('border-left:3px solid #e8a33d'), 'prompt editor has amber accent bar');
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

/* 工作流恢复后必须显示恢复值，并重新计算来源联动。 */
const restoredControls = {
    prompt_source: { type: 'select-one', value: 'panel' },
    ai_text: { type: 'textarea', value: '' },
    ai_enrich: { type: 'checkbox', checked: true },
    ai_timeout: { type: 'number', value: '180' },
};
const restoredValues = { prompt_source: 'ai', ai_text: 'restored idea', ai_enrich: false, ai_timeout: 300 };
let resynced = 0;
const restoreContext = vm.createContext({
    aiRows: new Map(Object.entries(restoredControls).map(([name, control]) => [name, { querySelector: () => control }])),
    node: {}, widget: (_, name) => ({ value: restoredValues[name] }), syncAiRows: () => resynced++,
});
const restoreFunction = js.match(/const syncAiControls = \(\) => \{[\s\S]*?\n    \};/);
check(!!restoreFunction, 'AI restore synchronization exists');
vm.runInContext(restoreFunction[0] + '\nsyncAiControls();', restoreContext);
check(restoredControls.prompt_source.value === 'ai', 'restored source visible');
check(restoredControls.ai_text.value === 'restored idea', 'restored idea visible');
check(restoredControls.ai_enrich.checked === false, 'restored false toggle visible');
check(restoredControls.ai_timeout.value === '300', 'restored timeout visible');
check(resynced === 1, 'restored source visibility refreshed');

const modeContext = vm.createContext({
    state: { mode: 'text_keyframes' }, media: new Map(), aiMode: { value: 'auto' },
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
modeContext.aiMode.value = 'l2va';
check(vm.runInContext('nextSlot("image")', modeContext) === 'last_frame', 'last-frame mode routes images');
modeContext.state.mode = 'all_reference';
check(vm.runInContext('resolvedTaskType()', modeContext) === 'Ref2VA', 'empty reference mode stays reference');
for (const kind of ['image', 'video', 'audio']) {
    check(vm.runInContext(`nextSlot("${kind}")`, modeContext) === `ref_${kind}_1`, 'reference slot: ' + kind);
}
console.log(`PASS: ${assertions} assertions (panel AI section consistency)`);
