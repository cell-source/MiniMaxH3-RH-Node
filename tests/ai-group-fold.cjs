/* 离线验证 installAiGroupFold 的状态机：panel 折叠 / ai 展开 / 手动切换 / 恢复原始尺寸 */
'use strict';
const fs = require('fs');
const src = fs.readFileSync('D:/WorkStation/ProjectsVSCode/MiniMaxTool/MiniMaxH3-RH-Node/web/js/minimax_h3_integration.js', 'utf8');
const start = src.indexOf('const AI_GROUP_WIDGETS');
const end = src.indexOf('function kindOf');
if (start < 0 || end < 0) throw new Error('提取失败');
const body = src.slice(start, end);

/* LiteGraph widget / node 桩 */
function makeWidget(name, value) {
  return { name, value, computeSize() { return [200, 22]; }, callback: undefined, hidden: false };
}
function makeNode(sourceValue) {
  const node = { widgets: [], setDirtyCanvas() {}, graph: { setDirtyCanvas() {} } };
  node.widgets.push(makeWidget('prompt_source', sourceValue));
  for (const n of ['ai_text', 'ai_language', 'ai_mode', 'ai_enrich', 'ai_soundscape', 'ai_music',
    'ai_auto_timestamps', 'ai_fixed_camera', 'ai_visual_stability', 'ai_no_subtitles',
    'ai_anti_pop', 'ai_strict_validation', 'ai_provider', 'ai_api_key',
    'ai_endpoint', 'ai_model', 'ai_timeout']) {
    node.widgets.push(makeWidget(n, ''));
  }
  return node;
}
const widget = (node, name) => node.widgets.find(w => w.name === name) || null;
const make = (tag, css = {}, text = '') =>
  ({ tag, css, textContent: text, listeners: {}, addEventListener(ev, fn) { this.listeners[ev] = fn; } });

/* 在桩可见的闭包中编译提取的函数体（document 桩给原版 make 用） */
const fakeEl = () => ({ style: {}, textContent: '', listeners: {}, addEventListener(ev, fn) { this.listeners[ev] = fn; } });
const factory = new Function('widget', 'make', 'document',
  body + '\nreturn installAiGroupFold;');
const install = factory(widget, make, { createElement() { return fakeEl(); } });

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('  [PASS] ' + name); }
  else { fail++; console.log('  [FAIL] ' + name + (detail ? ' —— ' + JSON.stringify(detail).slice(0, 160) : '')); }
}
const aiNames = ['ai_text', 'ai_language', 'ai_mode', 'ai_provider', 'ai_api_key', 'ai_timeout'];
const heights = node => {
  const ws = node.widgets.filter(w => aiNames.includes(w.name));
  return ws.map(w => Math.round(w.computeSize()[1]));
};
const headerOf = node => node.widgets.find(w => typeof w.textContent === 'string' && w.textContent.includes('AI 生成设置'));

console.log('== 1. panel 默认：折叠（-4）+ 标题指示已收起 ==');
const n1 = makeNode('panel');
install(n1);
check('标题行已插入', !!headerOf(n1));
check('标题文本含"已收起"', headerOf(n1).textContent.includes('已收起'), headerOf(n1).textContent);
check('全部 ai widget 高度压扁', heights(n1).every(h => h === -4), heights(n1));

console.log('== 2. 切到 ai：callback 触发展开（恢复原始高度 22）==');
const n2 = makeNode('ai');
install(n2);
const src2 = n2.widgets.find(w => w.name === 'prompt_source');
src2.value = 'ai';
src2.callback && src2.callback();
check('标题文本含"来源：ai"', headerOf(n2).textContent.includes('来源：ai'), headerOf(n2).textContent);
check('全部 ai widget 恢复原始高度', heights(n2).every(h => h === 22), heights(n2));

console.log('== 3. 手动点击收起/展开，再跨来源切换：不丢原始尺寸 ==');
const n3 = makeNode('ai');
install(n3);
headerOf(n3).listeners.click();
check('手动收起生效', heights(n3).every(h => h === -4));
headerOf(n3).listeners.click();
check('再展开生效', heights(n3).every(h => h === 22));
const s3 = n3.widgets.find(w => w.name === 'prompt_source');
s3.value = 'panel'; s3.callback && s3.callback();
check('回 panel 折叠', heights(n3).every(h => h === -4));
s3.value = 'offline'; s3.callback && s3.callback();
check('再切 offline 展开（无尺寸污染）', heights(n3).every(h => h === 22), heights(n3));

console.log('== 4. 序列化安全：折叠不改变 widget.value ==');
const n4 = makeNode('panel');
install(n4);
n4.widgets.find(w => w.name === 'ai_api_key').value = 'sk-test-123';
check('折叠后 value 仍在', n4.widgets.find(w => w.name === 'ai_api_key').value === 'sk-test-123');
check('ai_* 未被永久 hidden（隐藏只走 computeSize）',
  n4.widgets.filter(w => aiNames.includes(w.name)).every(w => !w.hidden));

console.log('== 5. prompt_source 缺失（旧图）时不崩，成员仍兜底折叠 ==');
const n5 = makeNode('panel');
n5.widgets = n5.widgets.filter(w => w.name !== 'prompt_source');
let ok5 = true;
try { install(n5); } catch (e) { ok5 = false; console.log('  ' + e.message); }
check('无 prompt_source 时不抛错', ok5);
check('成员仍被折叠兜底', heights(n5).every(h => h === -4));

console.log('\n结果: PASS=' + pass + ' FAIL=' + fail);
process.exit(fail ? 1 : 0);
