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

/* 5. 布局与配色基线 */
check(js.includes('display: "flex"') && js.includes('flexFlow: "column"'), 'panel root is column flex');
check(js.includes('overflowY: "auto"'), 'panel scrolls vertically');
check(js.includes('background:#111c27;border:1px solid #334a5d;border-radius:8px'),
    'details cards use .ghh3-box baseline colors');
check(!/position:absolute;left:8px;right:8px;bottom:6px/.test(js), 'no absolute overlay for advanced');

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

console.log(`PASS: ${assertions} assertions (panel AI section consistency)`);
