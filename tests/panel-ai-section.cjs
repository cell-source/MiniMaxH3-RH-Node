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
    check(js.includes(`addAi("${name}"`) || js.includes(`aiSelect("${name}"`) || js.includes(`aiCheck("${name}"`)
        || new RegExp(`setWidget\\(node, "${name}"`).test(js) || new RegExp(`"${name}"`).test(js),
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

console.log(`PASS: ${assertions} assertions (panel AI section consistency)`);
