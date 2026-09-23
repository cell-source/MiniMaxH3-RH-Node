// Network I/O belongs to Python, never to the embedded browser implementation.
let resultNotice = {};
setAiResultNotice = (status, reason) => { resultNotice = {status, reason: reason || ''}; };
showToast = () => {};
renderPromptHistory = () => {};
callLLM = async (system, user, key, onDelta) => {
    const result = JSON.parse(__llm(JSON.stringify({system, user})));
    if (result.error) throw new Error(result.error);
    onDelta(result.content);
    return result.content;
};
async function runNode(options) {
    for (const [id, value] of Object.entries(options.controls)) {
        if (!elements[id]) throw new Error('Unknown control: ' + id);
        elements[id][typeof value === 'boolean' ? 'checked' : 'value'] = value;
    }
    for (const ids of Object.values(STYLE_FIELDS)) elements[ids[0]].value = '';
    if (options.operation === 'ai') {
        await runAiEnrich(options.text);
    } else if (options.operation === 'format') {
        elements.output.value = normalizePrompt(getMode(), options.text);
    } else {
        runTemplateEnrich(options.text);
    }
    const prompt = elements.output.value.trim();
    const error = resultNotice.reason || finalPromptError(prompt, getMode()) || '';
    return {prompt, valid: !error && !['invalid', 'partial', 'error'].includes(resultNotice.status),
        report: error || '通过当前 H3 结构与语言校验'};
}
