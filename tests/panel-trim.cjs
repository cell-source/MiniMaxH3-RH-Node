'use strict';
/* 音频裁剪对话框的纯计算基线（去相似化重写时抽出的 4 个 helper）。
   这些函数不依赖 DOM，直接从面板源码里切出来在 vm 里跑，锁住数值行为。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const js = fs.readFileSync(path.join(__dirname, '../web/js/h3kit_panel.js'), 'utf8');

function section(start, end) {
    const a = js.indexOf(start);
    const b = js.indexOf(end, a);
    assert(a >= 0 && b > a, `source anchors exist: ${start}`);
    return js.slice(a, b);
}

const helpers = [
    section('const TICK_STEPS =', 'function canvas('),
].join('\n');
const ctx = vm.createContext({});
vm.runInContext(helpers, ctx);
const run = (code) => vm.runInContext(code, ctx);

/* 刻度步长：目标约 7 格，取候选表里第一个够大的。 */
assert.equal(run('chooseTickStep(0.7)'), 0.1);
assert.equal(run('chooseTickStep(7)'), 1);
assert.equal(run('chooseTickStep(14)'), 2);
assert.equal(run('chooseTickStep(70)'), 10);
assert.equal(run('chooseTickStep(700)'), 120);
assert.equal(run('chooseTickStep(25200)'), 3600);
// 超出候选表（>7 小时）时按整小时向上取：36000/7 ≈ 5143 → 7200。
assert.equal(run('chooseTickStep(36000)'), 7200);
assert.equal(run('chooseTickStep(70000)'), 10800);

/* 刻度文本：不到 1 秒带小数，分钟内带 s，超过一分钟用 m:ss / Nm。 */
assert.equal(run('formatTickLabel(0.25, 0.01)'), '0.25s');
assert.equal(run('formatTickLabel(0.25, 0.5)'), '0.3s');
assert.equal(run('formatTickLabel(5, 1)'), '5s');
assert.equal(run('formatTickLabel(59, 1)'), '59s');
assert.equal(run('formatTickLabel(60, 30)'), '1m');
assert.equal(run('formatTickLabel(90, 30)'), '1:30');
assert.equal(run('formatTickLabel(125, 15)'), '2:05');

/* 拖拽目标判定：把手命中优先，其次选区内部，最后就近一侧。 */
assert.equal(run('pickTrimDrag(1.0, 0.05, 1.0, 5.0)'), 'start');
assert.equal(run('pickTrimDrag(5.0, 0.05, 1.0, 5.0)'), 'end');
assert.equal(run('pickTrimDrag(3.0, 0.05, 1.0, 5.0)'), 'selection');
assert.equal(run('pickTrimDrag(0.2, 0.05, 1.0, 5.0)'), 'start');
assert.equal(run('pickTrimDrag(9.0, 0.05, 1.0, 5.0)'), 'end');
// 两端等距时归到 start（与实现一致）。
assert.equal(run('pickTrimDrag(3.0, 0.0, 0.0, 6.0)'), 'selection');

/* 对齐目标时长：音频不够长就整段，否则从当前起点截取并向前夹紧。
   返回的是另一个 vm realm 里的数组，deepStrictEqual 会因原型不同而失败，故比 JSON。 */
const pair = (code) => JSON.stringify(run(code));
assert.equal(pair('fitSelectionToTarget(2, 3, 5)'), '[0,3]');
assert.equal(pair('fitSelectionToTarget(2, 20, 5)'), '[2,7]');
assert.equal(pair('fitSelectionToTarget(18, 20, 5)'), '[15,20]');
assert.equal(pair('fitSelectionToTarget(0, 5, 5)'), '[0,5]');

/* 面板其余套件也依赖这些源码锚点：抽出的助手必须留在 canvas() 之前。 */
assert(js.indexOf('const TICK_STEPS =') < js.indexOf('function canvas('),
    'trim helpers stay above canvas()');

console.log('PASS: 25 assertions (audio trim helpers)');
