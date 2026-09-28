// Run: node tests/language-globalization.cjs. No browser, credentials or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const os = require('node:os');
// 参考生成器 HTML 维护在上游主项目 MiniMaxH3（本仓库同级目录），本仓库不再随附该文件。
// 解析顺序：环境变量 MINIMAX_H3_HTML → 同级主项目 → 兼容旧仓库根路径；找不到则跳过本测试。
const htmlPath = [process.env.MINIMAX_H3_HTML,
  path.join(__dirname, '..', '..', 'MiniMaxH3', 'MiniMax-H3-提示词生成器.html'),
  path.join(__dirname, '..', 'MiniMax-H3-提示词生成器.html')].filter(Boolean).find(p => fs.existsSync(p));
if (!htmlPath) {
  console.log('SKIP: 未找到参考生成器 HTML（设置 MINIMAX_H3_HTML，或确认同级 MiniMaxH3 主项目存在）');
  process.exit(0);
}
const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const main = scripts.find(s => s.includes('function i2vaAlignLine'));
const temp = path.join(os.tmpdir(), `h3-syntax-${process.pid}.js`);
try {
  fs.writeFileSync(temp, scripts.join('\n;\n'));
  require('node:child_process').execFileSync(process.execPath, ['--check', temp]);
} finally { fs.unlinkSync(temp); }
// v4.1: the English constraint must stay byte-identical to experiment/subtitle_experiment.py (S2 uses the same sentence).
const pyConst = /NO_SUBTITLES_CONSTRAINT = \(\s*"(.+?)"\s*\)/s.exec(
  fs.readFileSync(path.join(__dirname, '../experiment/subtitle_experiment.py'), 'utf8'))[1];
assert.equal(pyConst, (html.match(/const NO_SUBTITLES_CONSTRAINT = '(.+?)';/) || [])[1]);
const els = {};
for (const m of html.matchAll(/<(input|select|textarea|div|span|button|output)[^>]*\bid="([^"]+)"[^>]*>/g)) {
  els[m[2]] = { value: (m[0].match(/value="([^"]*)"/) || [])[1] || '', checked: /\bchecked\b/.test(m[0]),
    field: /^(?:f_|i2_|fl_|l2_|r_)/.test(m[2]), style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {}, querySelectorAll() { return []; } };
}
const document = { getElementById: id => els[id] || null,
  querySelectorAll: q => q.startsWith('.mode-fields ') ? Object.values(els).filter(e => e.field) : [],
  documentElement: { setAttribute() {}, dataset: {} }, body: { classList: { add() {}, remove() {} } } };
const ctx = { document, window: { matchMedia: () => ({ matches: false, addEventListener() {} }) },
  localStorage: { getItem: () => null, setItem() {} }, navigator: {},
  console: { warn() {}, log() {} }, setTimeout: () => 1, clearTimeout() {}, AbortController,
  fetch: () => { throw Error('Network forbidden in regression tests'); } };
vm.createContext(ctx);
vm.runInContext(main.split('/* ==================== 初始化')[0], ctx);
const run = code => vm.runInContext(code, ctx);
let assertions = 0;
const check = (code, expected, label = code) => { assert.deepEqual(JSON.parse(JSON.stringify(run(code))), expected, label); assertions++; };
const base = scene => `integrated_multimodal_description: [Shot 1] ${scene}\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A`;
const reset = (lang = 'zh') => {
  els.lang.value = lang; els.mode.value = 't2va'; els.duration.value = '5';
  for (const id of ['fixed_camera','visual_stability','no_subtitles','auto_timestamps','anti_pop','enrich_soundscape','enrich_music','enrich_do_enrich']) els[id].checked = false;
};
reset(); ctx.base = base('A woman stands still.'); ctx.zh = base('女子站着。');
for (const language of ['zh', 'mixed', 'en']) {
  reset(language);
  check("[i2vaAlignOk(i2vaAlignLine()+'\\n\\nintegrated_multimodal_description:'),fl2vaAlignOk(fl2vaAlignLine(2,5),2),l2vaAlignOk(l2vaAlignLine(2,5),2)]", [true,true,true]);
  for (const mode of ['t2va','i2va','fl2va','l2va']) {
    ctx.mode = mode; ctx.fixture = language === 'zh' ? ctx.zh : ctx.base;
    check("finalPromptError(normalizePrompt(mode,fixture),mode)", '');
  }
}
reset();
check("normalizeKeyframeAlignment('fl2va','UNIQUE USER FACT\\n'+base).includes('UNIQUE USER FACT')", true);
run("globalThis.englishAlign = ''"); els.lang.value = 'en'; run('englishAlign = i2vaAlignLine()'); els.lang.value = 'zh';
check("normalizeKeyframeAlignment('i2va',englishAlign+'\\n\\n'+zh).startsWith(i2vaAlignLine())", true);
for (const id of ['fixed_camera','visual_stability','no_subtitles']) els[id].checked = true;
check('rebuildVisualConstraints(rebuildVisualConstraints(zh))===rebuildVisualConstraints(zh)', true);
check("stripRoundTripGlobalConstraints([FIXED_CAMERA_CONSTRAINT,FIXED_CAMERA_CONSTRAINT_ZH,VISUAL_STABILITY_CONSTRAINT,VISUAL_STABILITY_CONSTRAINT_ZH,NO_SUBTITLES_CONSTRAINT,NO_SUBTITLES_CONSTRAINT_ZH].join(''))", '');
els.lang.value = 'en'; run('globalThis.old = rebuildVisualConstraints(base)'); els.lang.value = 'zh';
check('[FIXED_CAMERA_CONSTRAINT,VISUAL_STABILITY_CONSTRAINT,NO_SUBTITLES_CONSTRAINT].every(x=>!rebuildVisualConstraints(old).includes(x))', true);
reset(); els.no_subtitles.checked = true;
check("!!bodyLangError(normalizePrompt('t2va',base))", true);
check("!!finalPromptError(normalizePrompt('t2va',base),'t2va')", true);
els.enrich_do_enrich.checked = true;
check("enrichLengthError(normalizePrompt('t2va',zh),'女子站着。','t2va')", '');
check("validateEnrichmentAmount(parseAiOutput(normalizePrompt('t2va',zh)).scene,'女子站着。','t2va')", true);
check("hasDialogueCue('女子劝止：别走。')", true);
check("validateDialogueMarkup('女子（S1）劝止：别走。',0)", false);
check("validateDialogueMarkup('女子（S1）劝止：<d>[Chinese] 别走。</d>',1)", true);
reset('mixed');
for (const source of ['The woman (S1) says: <d>[English] 你好。</d>', 'The woman (S1) says: <d>[中文] 你好。</d>']) {
  ctx.source = source; check('validateDialogueMarkup(source,1)', false); check('validateFinalDialogueMarkup(source)', false);
}
check("validateDialogueMarkup('The woman (S1) says: <d>[Chinese] 你好。</d>',1)", true);
els.anti_pop.checked = true;
check("validateFinalDialogueMarkup('The woman (S1) says: [English] 你好。')", false);
check("validateFinalDialogueMarkup('The woman (S1) says: [English] Hello.')", true);
reset('en');
check("aiFormatError('i2va',base,'',0)", 'I2VA 首帧对齐指令不正确');
run('setControlsDisabled(true)'); assert.equal(els.lang.disabled, true); assert.equal(els.no_subtitles.disabled, true);
const restore = main.match(/\(function restoreLang\(\) \{[\s\S]*?\}\)\(\);/)[0];
for (const value of [null, 'BAD', 'zh', 'en', 'mixed']) {
  els.lang.value = 'zh'; ctx.localStorage.getItem = () => value; run(restore);
  assert.equal(els.lang.value, ['zh','en','mixed'].includes(value) ? value : 'zh'); assertions++;
}
ctx.localStorage.getItem = () => { throw Error('storage disabled'); }; els.lang.value='zh'; run(restore); assert.equal(els.lang.value,'zh');
ctx.localStorage.getItem = () => null;
ctx.ref = 'subject_definitions:\n<Subject 1>：<Picture 1> 中的女子。\n\nsummary:\n[reference generation] 女子站着。\n\nretention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - 保留人物身份。\n\ndetailed_description:\n[Shot 1] <Subject 1> 站着。\n\noverall_soundscape:\nN/A\n\nnon_diegetic_music:\nN/A';
reset(); check("finalPromptError(ref,'ref2va')", '');
check("!!bodyLangError(ref.replace('女子站着。','The woman stands still.'))", true);
check("!!bodyLangError(zh.replace('overall_soundscape: N/A','overall_soundscape: Wind blows softly.'))", true);
// Regression: strip complete dialogue before classifying multiline Chinese fields.
reset('zh');
ctx.multiline = '女子（S1）说：<d>[Chinese] 你好\nOpenAI\n再见。</d>';
check('bodyLangOk(multiline,multiline)', true);
check("validateChineseDescription('女子站着。\\nA woman waves.')", false);
reset('en');
check("validateDialogueMarkup('The woman (S1) looks at a clock showing 12:30.',0)", true);
check("validateDialogueMarkup('The woman (S1) says: Hello.',0)", false);
els.anti_pop.checked = true;
check("validateFinalDialogueMarkup('The woman (S1) says: [English] \\nShe leaves.')", false);
check("validateDialogueMarkup('The woman (S1) says: [English] \\nShe leaves.',1)", false);
ctx.tail = base('The woman (S1) says: <d>[English] Hello.</d> 女子离开。');
check("!!bodyLangError(normalizePrompt('t2va',tail))", true);
ctx.safeTail = base('The woman (S1) says: <d>[English] Hello.</d> She leaves.');
check("finalPromptError(normalizePrompt('t2va',safeTail),'t2va')", '');
check("normalizeInlineDialogue(normalizeInlineDialogue(safeTail))===normalizeInlineDialogue(safeTail)", true);
for (const language of ['en','zh']) {
  reset(language);ctx.fixture = language === 'zh' ? ctx.zh : ctx.base;
  for (const mode of ['fl2va','l2va']) {
    ctx.mode=mode;
    run("globalThis.extraPrefix = (mode==='fl2va'?fl2vaAlignLine(1,5):l2vaAlignLine(1,5))+'\\nUNIQUE EXTRA FACT\\n\\n'+fixture");
    check('!!finalPromptError(extraPrefix,mode)', true);
    check("normalizePrompt(mode,extraPrefix).includes('UNIQUE EXTRA FACT')", true);
  }
}
reset('zh');els.anti_pop.checked=true;
ctx.multilinePrompt=base(ctx.multiline);
check("finalPromptError(normalizePrompt('t2va',multilinePrompt),'t2va')", '');
check("normalizeOfficialDialogue(normalizeInlineDialogue(safeTail)).includes('</d> She leaves.')", false);
check("normalizeOfficialDialogue(normalizeInlineDialogue(safeTail)).includes('She leaves.')", true);
reset('en');
ctx.spaced=base('The woman (S1) says: <d>  [English] Hello.</d>');
check("finalPromptError(normalizePrompt('t2va',spaced),'t2va')", '');
ctx.spacedWrong=base('The woman (S1) says: <d>  [Chinese] 你好。</d>');
check('!!dialogueLanguageError(spacedWrong)', true);
// v4 no-written-dialogue block: bilingual migration, exact-once insertion and option removal.
for (const language of ['zh','mixed','en']) {
  reset(language);els.no_subtitles.checked=true;ctx.fixture=language==='zh'?ctx.zh:ctx.base;
  run("globalThis.legacyPrompt = fixture.replace('[Shot 1]', '[Shot 1] '+LEGACY_NO_SUBTITLES_CONSTRAINTS.join(' '))");
  check('LEGACY_NO_SUBTITLES_CONSTRAINTS.every(x=>!rebuildVisualConstraints(legacyPrompt).includes(x))', true);
  check('rebuildVisualConstraints(legacyPrompt).split(noSubtitlesConstraint()).length-1', 1);
  check('rebuildVisualConstraints(rebuildVisualConstraints(legacyPrompt))===rebuildVisualConstraints(legacyPrompt)', true);
  check('LEGACY_NO_SUBTITLES_CONSTRAINTS.every(x=>!stripRoundTripGlobalConstraints(legacyPrompt).includes(x))', true);
  check("finalPromptError(rebuildVisualConstraints(legacyPrompt),'t2va')", '');
  check('/subtitle|caption/i.test(noSubtitlesConstraint())', false);
  els.no_subtitles.checked=false;
  check("!!customOptionError(legacyPrompt,'t2va',false)", true);
  check('LEGACY_NO_SUBTITLES_CONSTRAINTS.concat([NO_SUBTITLES_CONSTRAINT,NO_SUBTITLES_CONSTRAINT_ZH]).every(x=>!rebuildVisualConstraints(legacyPrompt).includes(x))', true);
}
// v6 condensed constraint (2026-09-28): two-round condensation — three shorter sentences; scene-text protection kept; v4/v4.1/v5 sentences migrate via the legacy list.
reset('zh'); els.no_subtitles.checked = true;
check('/招牌|海报|屏幕|印花/.test(noSubtitlesConstraint())', true);
check('/不被擦除/.test(noSubtitlesConstraint())', true);
check('noSubtitlesConstraint().length < 200', true);
els.lang.value = 'en';
check('/signs, posters/.test(noSubtitlesConstraint())', true);
check('/never erased/.test(noSubtitlesConstraint())', true);
check('noSubtitlesConstraint().length < 600', true);
run("globalThis.oldPair = LEGACY_NO_SUBTITLES_CONSTRAINTS.slice(-2).join(' ')");
run("globalThis.oldPairPrompt = 'integrated_multimodal_description: [Shot 1] '+oldPair+'\\n\\noverall_soundscape: N/A\\n\\nnon_diegetic_music: N/A'");
check("rebuildVisualConstraints(oldPairPrompt).split(noSubtitlesConstraint()).length-1", 1);
check("rebuildVisualConstraints(rebuildVisualConstraints(oldPairPrompt))===rebuildVisualConstraints(oldPairPrompt)", true);
run("currentKey=()=> 'test-only'; currentModel=()=>({label:'mock',endpoint:'mock',model:'mock'}); showToast=()=>{}; setAiResultNotice=()=>{}; renderPromptHistory=()=>{}; fillAiFields=()=>{};");
const mock = (responses, failAt = -1, stopAt = -1) => {
  ctx.calls=[]; ctx.timeoutStop=false; ctx.responses=responses; ctx.failAt=failAt; ctx.stopAt=stopAt;
  run(`callLLM=async(sys,user,key,onDelta)=>{
    calls.push({sys,user});const n=calls.length-1;
    if(n===failAt)throw Error('mock failure');
    if(n===stopAt){if(timeoutStop)aiTimedOut=true;else aiStopped=true;onDelta('partial');return 'partial';}
    const result=responses[Math.min(n,responses.length-1)];onDelta(result);return result;
  }`);
};
(async()=>{
  reset('mixed'); mock(['DIALOGUE_COUNT: 0\n'+ctx.zh]);
  await run("runAiEnrich('UNIQUE ORIGINAL INPUT')");
  assert.equal(ctx.calls.length,3); assert.ok(ctx.calls.every(c=>c.user.includes('UNIQUE ORIGINAL INPUT')));
  assert.ok(!ctx.calls[0].user.includes('Timed ranges use the Chinese format'));
  for (const scenario of ['error','empty','stop','timeout']) {
    reset('mixed'); mock(['DIALOGUE_COUNT: 0\n'+ctx.zh, ''],scenario==='error'?1:-1,['stop','timeout'].includes(scenario)?1:-1);
    ctx.timeoutStop=scenario==='timeout';
    if(['stop','timeout'].includes(scenario))await run("runAiEnrich('input')");
    else await assert.rejects(run("runAiEnrich('input')"));
    assert.ok(els.output.value.includes('女子站着。'), scenario+' retains previous result');
    assert.equal(els.lang.disabled,false);assert.equal(els.no_subtitles.disabled,false);
  }
  reset('zh');els.no_subtitles.checked=true;mock(['DIALOGUE_COUNT: 0\n'+ctx.base,'DIALOGUE_COUNT: 0\n'+ctx.zh]);
  assert.equal(await run("runAiEnrich('女子站着。')"),'valid'); assert.equal(ctx.calls.length,2);
  reset('en');mock(['DIALOGUE_COUNT: 1\n'+base('The woman (S1) says: <d>[Chinese] 你好。</d>'),'DIALOGUE_COUNT: 1\n'+base('The woman (S1) says: <d>[English] Hello.</d>')]);
  assert.equal(await run("runAiEnrich('女子说：你好。')"),'valid');assert.equal(ctx.calls.length,2);
  reset('zh');els.mode.value='fl2va';els.auto_timestamps.checked=true;
  mock(['DIALOGUE_COUNT: 0\n'+ctx.zh,'DIALOGUE_COUNT: 0\n'+base('0.0-2.5 秒：女子站着。2.5-5.0 秒：女子保持站姿。')]);
  assert.equal(await run("runAiEnrich('女子站着。')"),'valid');assert.equal(ctx.calls.length,2);
  assert.ok(ctx.calls[1].user.includes('using labels such as 0.0-1.5 秒：'));
  reset('en');els.anti_pop.checked=true;
  mock(['DIALOGUE_COUNT: 1\n'+ctx.tail,'DIALOGUE_COUNT: 1\n'+ctx.safeTail]);
  assert.equal(await run("runAiEnrich('A woman greets someone and leaves.')"),'valid');assert.equal(ctx.calls.length,2);
  reset('en');mock(['DIALOGUE_COUNT: 1\n'+ctx.spacedWrong,'DIALOGUE_COUNT: 1\n'+ctx.spaced]);
  assert.equal(await run("runAiEnrich('女子说：你好。')"),'valid');assert.equal(ctx.calls.length,2);
  // Persistent FL2VA timeline and language failures exhaust both budgets, then stop.
  reset('en');els.mode.value='fl2va';els.auto_timestamps.checked=true;
  mock(['DIALOGUE_COUNT: 0\n'+ctx.zh]);
  await run("runAiEnrich('女子站着。')");assert.equal(ctx.calls.length,5);
  reset('en');els.output.value='UNIQUE OUTPUT EDIT';els.f_scene.value='STALE FORM';run('onLangChange()');assert.ok(els.output.value.includes('UNIQUE OUTPUT EDIT'));
  assert.equal(html.includes('[中文]'),false);
  console.log(`PASS: syntax; ${assertions} helper assertions; mocked generation, language/timeline repair, bounded retries, failure/empty/stop recovery, and edit preservation.`);
})().catch(error=>{console.error(error);process.exitCode=1;});
