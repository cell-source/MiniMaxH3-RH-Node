'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/js/minimax_h3_integration.js'), 'utf8');
const layout = source.slice(source.indexOf('    let measuredContentHeight = null;'), source.indexOf('    // Text edits, fonts'));
const child = (height, display = 'block') => ({
    tagName: 'DIV', offsetHeight: height,
    computedStyle: { display, marginTop: '0', marginBottom: '0' },
});
const root = {
    offsetHeight: 0, children: [child(100), child(0, 'none'), child(200)],
    computedStyle: { rowGap: '6px', paddingTop: '3px', paddingBottom: '2px' },
    style: {},
};
let writes = 0;
const node = {
    size: [500, 700],
    setSize(size) { this.size = size; writes++; },
    setDirtyCanvas() {},
};
const context = vm.createContext({
    root, node, domWidget: { y: 254 },
    prompt: { style: { removeProperty() {} } },
    getComputedStyle: element => element.computedStyle,
    WIDTH: 500, MIN_NODE_HEIGHT: 0, BOTTOM_DECOR_HEIGHT: 20,
    userHeight: 700, layoutLock: false,
});
vm.runInContext(layout, context);
const sync = () => vm.runInContext('syncLayout(false, true)', context);
sync();
assert.equal(writes, 0, 'unmounted content must not change the node height');
root.offsetHeight = 311;
sync();
assert.equal(root.style.height, '311px', 'count visible gaps and both root paddings');
assert.equal(node.size[1], 585);
root.children[0].offsetHeight = 500;
sync();
assert.equal(node.size[1], 985, 'growing content expands the node');
root.offsetHeight = 0;
for (let i = 0; i < 4; i++) sync();
assert.equal(node.size[1], 985, 'offscreen calls must not repeatedly add the widget offset');
root.offsetHeight = 711;
root.children[0].offsetHeight = 100;
sync();
assert.equal(node.size[1], 585, 'shorter content releases unused height');
console.log('Panel layout: 7 assertions passed');
