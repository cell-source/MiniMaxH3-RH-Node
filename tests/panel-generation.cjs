'use strict';
// Execute the production closures with a controlled host and delayed HTTP replies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const js = fs.readFileSync(path.join(__dirname, '../web/js/minimax_h3_integration.js'), 'utf8');
function section(start, end) {
    const a = js.indexOf(start), b = js.indexOf(end, a);
    assert(a >= 0 && b > a, `source anchors exist: ${start}`);
    return js.slice(a, b);
}
const noop = () => {};
function host(overrides = {}) {
    const values = { prompt_source: 'ai', ai_text: 'old idea', ai_provider: 'deepseek', ai_api_key: '', ai_endpoint: '', ai_model: '',
        ai_mode: 'auto', ai_language: 'zh', ai_timeout: 180, ai_strict_validation: true,
        ai_enrich: false, ai_soundscape: true, ai_music: true, ai_fixed_camera: true,
        ai_visual_stability: true, ai_no_subtitles: false, ai_anti_pop: true, ai_auto_timestamps: true,
        soundscape: false, music: false, no_subtitle: true, aspect: '16:9', prompt: 'original', ...overrides };
    const graph = {};
    const ctx = {
        console, AbortController, Blob, DOMException, performance, setTimeout, clearTimeout, setInterval, clearInterval,
        node: { graph, widgets: Object.entries(values).map(([name, value]) => ({ name, value })) }, app: { graph },
        state: { mode: 'text_keyframes' }, media: new Map(), promptPlainText: 'original',
        promptRevision: 0, panelRemoved: false, settingsEpoch: 0, generationOptionsVersion: 0,
        activePromptOperation: null, aiGenerating: false, optimizing: false, optimizerRequestId: null, optimizerAbort: null, optimizerTimer: null,
        refreshDialogActions: noop, refreshPromptConnection: noop, upstreamConnected: () => false,
        persistState: noop, t: s => s, close: noop, playOptimizerCompleteSound: noop,
        promptHistory: { text_keyframes: { undo: [], redo: [] }, all_reference: { undo: [], redo: [] } },
        selectionOffsets: () => [0, 0], setEditorSelection: noop, renderPromptHighlights: noop,
        promptByMode: { text_keyframes: 'original', all_reference: 'reference' }, optimizerBeforeByMode: {}, optimizerBefore: null,
        resetPrompt: { classList: { add: noop, remove: noop } },
        polishPrompt: {}, aiGeneratePrompt: {}, aiFormatPrompt: {}, // toolbar buttons; onclick rows are not extracted
        durationWidget: { value: 5 },
        genProvider: { value: 'deepseek' },
        providerApiKeys: {}, getSharedKey: p => p === 'deepseek' ? 'host-key' : '',
        OPTIMIZER_ROUTE: '/rh/minimax-h3/prompt-optimizer', alerts: [], requests: [],
        optimizerSettings: { has_api_key: true, auto_optimize: true, read_media: false, api_keys: { deepseek: 'host-key' }, api_key: 'host-key' }, optimizerCache: null,
        resolvedTaskType: () => 'T2VA', resemblesOfficialPrompt: () => false, workflowRunning: false,
        optimizerMediaSpecs: () => [], optimizerTaskContext: () => ({}), optimizerContextSignature: () => 'signature',
        optimizerMediaPayload: async () => [], showOptimizerConfigPrompt: () => { ctx.configPromptShown++; },
        elapsedPrompt: { classList: { add: noop, remove: noop } },
    };
    ctx.prompt = { focus: noop, get value() { return ctx.promptPlainText; }, set value(v) { ctx.promptPlainText = v; } };
    ctx.editorText = () => ctx.prompt.value;
    ctx.alert = s => ctx.alerts.push(s);
    ctx.loadOptimizerSettings = async () => ctx.optimizerSettings;
    ctx.configPromptShown = 0;
    ctx.api = { fetchApi: (url, options) => {
        if (url.endsWith('/cancel')) { ctx.cancelRequest = options; return Promise.resolve({}); }
        return new Promise((resolve, reject) => ctx.requests.push({ url, options, resolve: data => resolve({ ok: true, text: async () => JSON.stringify(data) }), reject }));
    } };
    vm.createContext(ctx);
    vm.runInContext([
        section('function widget(node, name)', 'function hasOwn('),
        section('const sharedKeychain =', 'const sharedGenerationOptions ='),
        section('const sharedGenerationOptions =', 'const commitPromptEditorInput ='),
        section('const promptSnapshot =', 'const setEditorSelection ='),
        // 润色配置真源（2026-09-26 审查）：runPromptOptimization 依赖这几个 helper。
        section('const polishProviderValue =', 'const configuredOptimizerName ='),
        section('const generationMode = () => {', 'optimizerGear.onclick = openOptimizerSettings;'),
        section('function applyOptimizedPrompt(', 'function playOptimizerCompleteSound('),
        section('async function cancelOptimization()', 'function resemblesOfficialPrompt('),
        section('async function runPromptOptimization(', 'const autoOptimizeBeforeQueue ='),
    ].join('\n'), ctx);
    return ctx;
}
const run = (ctx, code) => vm.runInContext(code, ctx);
const value = (ctx, name) => ctx.node.widgets.find(w => w.name === name).value;
const flush = () => new Promise(resolve => setImmediate(resolve));

module.exports = { host, run };
if (require.main === module) (async () => {
    const migration = host({ prompt_source: 'panel', soundscape: true, music: false, no_subtitle: false });
    run(migration, 'migrateGenerationOptions()');
    assert.equal(value(migration, 'ai_soundscape'), true);
    assert.equal(value(migration, 'ai_music'), false);
    assert.equal(value(migration, 'ai_no_subtitles'), false);
    run(migration, 'setGenerationOption("ai_music", true)');
    assert.equal(value(migration, 'music'), true);
    run(migration, 'setGenerationOption("ai_no_subtitles", true)');
    assert.equal(value(migration, 'no_subtitle'), true);
    run(migration, 'migrateGenerationOptions()');
    assert.equal(value(migration, 'ai_music'), true); // versioned migration is idempotent
    const legacy = host();
    run(legacy, 'migrateGenerationOptions()');
    assert.equal(value(legacy, 'soundscape'), true); // old runtime source keeps AI options
    assert.equal(value(legacy, 'prompt_source'), 'panel'); // v2 migration normalizes the source
    run(legacy, 'syncEditorSource()');
    assert.equal(value(legacy, 'ai_text'), 'original'); // editor content mirrors into the runtime input
    const body = run(legacy, 'generationBody()');
    assert.equal(body.controls.visual_stability, true);
    for (const mode of ['t2va', 'i2va', 'fl2va', 'l2va', 'ref2va']) {
        run(legacy, `setWidget(node, "ai_mode", "${mode}")`);
        assert.equal(run(legacy, 'generationBody().controls.mode'), mode);
    }
    legacy.durationWidget.value = 30;
    assert.throws(() => run(legacy, 'generationBody()'), /2–15/);
    run(legacy, 'setWidget(node, "ai_model", ""); setWidget(node, "ai_api_key", "")');
    assert.equal(value(legacy, 'ai_model'), '');
    assert.equal(value(legacy, 'ai_api_key'), '');
    const keys = host();
    keys.optimizerSettings = { provider: 'deepseek', api_keys: { deepseek: 'old', openai: 'other' }, api_key: 'old', has_api_key: true };
    keys.providerApiKeys = { deepseek: 'old', openai: 'other' };
    keys.key = { value: 'replacement' }; keys.genKey = { value: 'gen-key' };
    keys.genProvider = { value: 'deepseek' }; keys.provider = { value: 'deepseek' };
    // 分区 Key 行：各自 onchange 即时写回共享钥匙串与生成 widget（setSharedKey 已由 host 注入）。
    run(keys, `
        providerApiKeys[provider.value] = key.value.trim(); setSharedKey(provider.value, key.value.trim());
        providerApiKeys[genProvider.value] = genKey.value.trim(); setSharedKey(genProvider.value, genKey.value.trim());
        setWidget(node, "ai_api_key", genKey.value.trim());
    `);
    assert.equal(keys.providerApiKeys.deepseek, 'gen-key');
    assert.equal(keys.optimizerSettings.api_keys.deepseek, 'gen-key');
    assert.equal(value(keys, 'ai_api_key'), 'gen-key');
    assert.equal(keys.optimizerSettings.api_keys.openai, 'other'); // other platform untouched
    keys.key.value = '';
    run(keys, `providerApiKeys[provider.value] = key.value.trim(); setSharedKey(provider.value, key.value.trim());`);
    // 润色行清空同一平台槽位：即时写回，deepseek 槽随之清空（两行都指向 deepseek 时以最后操作为准）。
    assert.equal(keys.optimizerSettings.api_keys.deepseek, '');
    keys.genKey.value = '';
    run(keys, `
        providerApiKeys[genProvider.value] = genKey.value.trim(); setSharedKey(genProvider.value, genKey.value.trim());
        setWidget(node, "ai_api_key", genKey.value.trim());
    `);
    assert.equal(value(keys, 'ai_api_key'), '');
    assert.equal(keys.optimizerSettings.has_api_key, false);

    const strict = host();
    let pending = run(strict, 'runGeneration("ai")');
    assert.equal(await run(strict, 'runPromptOptimization()'), false);
    assert.equal(strict.requests.length, 1);
    strict.requests[0].resolve({ prompt: 'invalid', valid: false, report: 'invalid structure' });
    await pending;
    assert.equal(strict.prompt.value, 'original');
    assert.equal(strict.promptHistory.text_keyframes.undo.length, 0);
    assert.deepEqual(strict.alerts, ['invalid structure']);
    assert.equal(strict.activePromptOperation, null);

    const success = host({ ai_strict_validation: false });
    run(success, 'migrateGenerationOptions()');
    pending = run(success, 'runGeneration("format")');
    const sent = JSON.parse(await success.requests[0].options.body.text());
    assert.equal(sent.controls.enrich_soundscape, true);
    assert.equal(sent.controls.visual_stability, true);
    assert(sent.request_id);
    success.requests[0].resolve({ prompt: 'generated', valid: false, report: 'edit this' });
    await pending;
    assert.equal(success.prompt.value, 'generated');
    assert.equal(value(success, 'prompt_source'), 'panel');
    assert.equal(value(success, 'ai_text'), 'generated'); // filled result mirrors into the runtime input
    assert.equal(value(success, 'prompt'), 'generated');
    run(success, 'stepPromptHistory(false)');
    assert.equal(success.prompt.value, 'original');
    assert.equal(value(success, 'prompt'), 'original');
    assert.equal(success.promptByMode.text_keyframes, 'original');
    run(success, 'stepPromptHistory(true)');
    assert.equal(success.prompt.value, 'generated');

    const polishing = host();
    let releaseSettings;
    polishing.loadOptimizerSettings = () => new Promise(resolve => { releaseSettings = resolve; });
    const polish = run(polishing, 'runPromptOptimization()');
    await run(polishing, 'runGeneration("ai")');
    assert.equal(polishing.requests.length, 0); // lock taken before the first await
    assert.equal(polishing.activePromptOperation.kind, 'optimize');
    releaseSettings(polishing.optimizerSettings);
    await flush();
    assert.equal(polishing.requests.length, 1);
    await run(polishing, 'cancelOptimization()');
    assert(polishing.requests[0].options.signal.aborted);
    assert(polishing.cancelRequest);
    polishing.requests[0].resolve({ prompt: 'late polish' });
    await polish;
    assert.equal(polishing.prompt.value, 'original');

    for (const change of ['cancelPromptOperation()', 'panelRemoved = true; cancelPromptOperation()',
        'prompt.value = "new edit"; syncEditorSource()', 'app.graph = {}', 'state.mode = "all_reference"']) {
        const stale = host();
        pending = run(stale, 'runGeneration("ai")');
        run(stale, change);
        stale.requests[0].resolve({ prompt: 'late generation', valid: true });
        await pending;
        assert.notEqual(stale.prompt.value, 'late generation', change);
        assert.equal(stale.activePromptOperation, null);
    }
    const timeout = host();
    let deadline;
    timeout.setTimeout = callback => { deadline = callback; return 1; };
    timeout.clearTimeout = noop;
    pending = run(timeout, 'runGeneration("ai")');
    deadline();
    assert(timeout.requests[0].options.signal.aborted);
    timeout.requests[0].reject(new DOMException('Aborted', 'AbortError'));
    await pending;
    assert.deepEqual(timeout.alerts, ['Generation timed out']);
    assert.equal(timeout.prompt.value, 'original');
    const removed = host();
    Object.assign(removed, {
        autoOptimizerHandlers: new Set(), autoOptimizeBeforeQueue: noop, workflowStateHandlers: new Set(), updateWorkflowState: noop,
        optimizerCompleteAudio: null, stopActiveMedia: noop, closeActiveTrimEditor: noop, decodedAudioCache: new Map(),
        promptHighlightResizeObserver: { disconnect: noop }, panelResizeObserver: { disconnect: noop }, layoutFrame: null,
        window: { removeEventListener: noop }, captureMaterialDrop: noop, capturePromptWheel: noop, middlePan: null,
        onPaste: noop, capturePromptHistoryKeys: noop, releasePromptHistoryKeys: noop, applyLocale: noop, unregisterPromptEditor: noop,
        closeOptimizerSettings: () => { removed.modalClosed = true; },
    });
    run(removed, section('const oldRemoved = node.onRemoved;', 'const initialRestoreEpoch ='));
    pending = run(removed, 'runGeneration("ai")');
    removed.node.onRemoved();
    assert(removed.modalClosed && removed.requests[0].options.signal.aborted);
    removed.requests[0].resolve({ prompt: 'removed node result', valid: true });
    await pending;
    assert.equal(removed.prompt.value, 'original');
    console.log('PASS: generation migration, request controls, strict validation, undo/redo, mutual exclusion, cancellation, stale replies and deadline');
})().catch(error => { console.error(error); process.exitCode = 1; });
