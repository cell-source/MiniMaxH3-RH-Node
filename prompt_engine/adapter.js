// A fresh document state and QuickJS context are created for every execution.
const elements = {};
const noop = () => {};
function element(initial = {}) {
    return Object.assign({value: '', checked: false, style: {}, dataset: {},
        classList: {add: noop, remove: noop, toggle: noop},
        addEventListener: noop, appendChild: noop, setAttribute: noop,
        querySelectorAll: () => []}, initial);
}
for (const [id, state] of Object.entries(JSON.parse(__defaults))) elements[id] = element(state);
const document = {
    getElementById: id => elements[id] || null,
    querySelectorAll: () => [], createElement: () => element(),
    documentElement: element(), body: element()
};
const window = {matchMedia: () => ({matches: false, addEventListener: noop})};
const localStorage = {getItem: () => null, setItem: noop};
const navigator = {};
const console = {log: noop, warn: noop};
const setTimeout = () => 1;
const clearTimeout = noop;
class AbortController {
    constructor() { this.signal = {aborted: false}; }
    abort() { this.signal.aborted = true; }
}
