import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const OPTIMIZER_DONE_SOUND_URL = new URL("../audio/done.mp3", import.meta.url).href;

const NODE = "MiniMaxH3IntegrationRH";
const RH_NODE_IDS = [
    NODE,
];
const OPTIMIZER_ROUTE = "/rh/minimax-h3/prompt-optimizer";
const WIDTH = 500;
const PANEL_WIDTH = 476;
const INITIAL_NODE_HEIGHT = 700; // 面板按完全展开内容高度设计，节点高度自适应内容
const MAX_RESTORED_NODE_HEIGHT = INITIAL_NODE_HEIGHT * 4;
const MIN_NODE_HEIGHT = 0;
const ASPECTS = {
    adaptive: null,
    "16:9": 16 / 9, "9:16": 9 / 16, "3:2": 3 / 2, "2:3": 2 / 3, "4:3": 4 / 3,
    "3:4": 3 / 4, "1:1": 1, "21:9": 21 / 9,
};
const imageSlots = ["first_frame", "last_frame", ...Array.from({ length: 9 }, (_, i) => `ref_image_${i + 1}`)];
const videoSlots = Array.from({ length: 3 }, (_, i) => `ref_video_${i + 1}`);
const audioSlots = ["hybrid_audio", ...Array.from({ length: 3 }, (_, i) => `ref_audio_${i + 1}`)];
const mediaSlots = [...imageSlots, ...videoSlots, ...audioSlots];
const typeOrder = { video: 0, image: 1, audio: 2 };
const autoOptimizerHandlers = new Set();
const workflowStateHandlers = new Set();
let autoOptimizerQueueInstalled = false;
let workflowStateMonitorInstalled = false;
let workflowRunning = false;

function installWorkflowStateMonitor() {
    if (workflowStateMonitorInstalled) return;
    const update = running => {
        workflowRunning = running;
        for (const handler of [...workflowStateHandlers]) {
            try { handler(running); } catch {}
        }
    };
    api.addEventListener("execution_start", () => update(true));
    api.addEventListener("executing", event => { if (event?.detail == null) update(false); });
    for (const name of ["execution_success", "execution_error", "execution_interrupted"]) {
        api.addEventListener(name, () => update(false));
    }
    workflowStateMonitorInstalled = true;
}

function installAutoOptimizerQueueHook() {
    if (autoOptimizerQueueInstalled || typeof app.queuePrompt !== "function") return;
    const originalQueuePrompt = app.queuePrompt;
    app.queuePrompt = async function(...args) {
        for (const handler of [...autoOptimizerHandlers]) {
            try { await handler(); } catch (error) { console.warn("MiniMax H3 automatic prompt optimization skipped:", error); }
        }
        return originalQueuePrompt.apply(this, args);
    };
    autoOptimizerQueueInstalled = true;
}

const livePromptEditors = new Set();
let promptKeyShieldInstalled = false;

function promptEditorFromEvent(event) {
    const path = event?.composedPath?.() || [];
    for (const item of path) {
        if (item?.classList?.contains?.("mxv-prompt") && livePromptEditors.has(item)) return item;
    }
    const active = document.activeElement;
    if (active && livePromptEditors.has(active)) return active;
    const closest = active?.closest?.(".mxv-prompt");
    if (closest && livePromptEditors.has(closest)) return closest;
    // If focus intentionally moved to another form control (for example by
    // Tab), do not pull it back into the prompt. The RH workaround is only
    // needed when the canvas/body stole focus from an active prompt editor.
    if (active && active !== document.body && active !== document.documentElement) {
        const editable = active.matches?.("input,textarea,select,button,[contenteditable='true']")
            || active.closest?.("input,textarea,select,button,[contenteditable='true']");
        if (editable) return null;
    }
    for (const editor of livePromptEditors) {
        if (editor.dataset.mxvEditing === "1") return editor;
    }
    return null;
}

function markPromptEditor(editor, editing) {
    if (!editor) return;
    if (editing) editor.dataset.mxvEditing = "1";
    else delete editor.dataset.mxvEditing;
}

function applyStolenPromptKey(editor, event) {
    if (!editor || editor.contentEditable === "false" || event.isComposing) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (typeof editor.setRangeText !== "function") return;
    const start = editor.selectionStart ?? 0;
    const end = editor.selectionEnd ?? start;
    if (event.key === "Backspace") {
        editor.setRangeText("", start === end ? Math.max(0, start - 1) : start, end, "end");
    } else if (event.key === "Delete") {
        editor.setRangeText("", start, start === end ? end + 1 : end, "end");
    } else if (event.key === "Enter") {
        editor.setRangeText("\n", start, end, "end");
    } else if (event.key.length === 1) {
        editor.setRangeText(event.key, start, end, "end");
    } else {
        return;
    }
    editor.dispatchEvent(new Event("input", { bubbles: true }));
}

function patchLiteGraphPromptProcessKey() {
    const proto = globalThis.LGraphCanvas?.prototype;
    if (!proto || proto.__mxvPromptKeyPatched || typeof proto.processKey !== "function") return;
    proto.__mxvPromptKeyPatched = true;
    const original = proto.processKey;
    proto.processKey = function processKeyMxvShield(event) {
        if (promptEditorFromEvent(event)) return;
        return original.apply(this, arguments);
    };
}

function installPromptKeyShield() {
    patchLiteGraphPromptProcessKey();
    if (promptKeyShieldInstalled || typeof window === "undefined") return;
    promptKeyShieldInstalled = true;
    const swallowStolenFocusKeys = event => {
        const editor = promptEditorFromEvent(event);
        if (!editor) return;
        const onEditor = event.target === editor || editor.contains(event.target);
        if (onEditor) return;
        // RH binds Backspace to ClearWorkflow and only ignores INPUT/TEXTAREA,
        // not contenteditable. If the canvas stole focus after a rebuild, keep
        // those shortcuts off and put the keystroke back into the prompt.
        if (editor.contentEditable !== "false") editor.focus({ preventScroll: true });
        const key = String(event.key || "").toLowerCase();
        if ((event.ctrlKey || event.metaKey) && !event.altKey && (key === "z" || key === "y")) return;
        event.stopImmediatePropagation();
        event.preventDefault();
        if (event.type === "keydown") applyStolenPromptKey(editor, event);
    };
    window.addEventListener("keydown", swallowStolenFocusKeys, true);
    window.addEventListener("keyup", swallowStolenFocusKeys, true);
    document.addEventListener("pointerdown", event => {
        const path = event.composedPath?.() || [];
        for (const editor of livePromptEditors) {
            markPromptEditor(editor, path.includes(editor) || editor.contains(event.target));
        }
    }, true);
}

function registerPromptEditor(editor) {
    if (!editor) return;
    livePromptEditors.add(editor);
    installPromptKeyShield();
}

function unregisterPromptEditor(editor) {
    if (!editor) return;
    livePromptEditors.delete(editor);
    delete editor.dataset.mxvEditing;
}

const DOM_TRANSLATIONS = {
    "AI generation": "AI 生成",
    "AI timeout (seconds)": "AI 总超时（秒）",
    "Auto": "自动",
    "Refresh RunningHub models": "刷新 RunningHub 模型",
    "Steps": "采样步数",
    "Video shift": "视频偏移",
    "Audio shift": "音频偏移",
    "Sampler": "采样器",
    "Scheduler": "调度器",
    "Seed": "种子",
    "Unable to load prompt optimizer settings": "无法加载提示词优化器设置",
    "Source": "来源",
    "panel": "提示词编辑器（默认）",
    "ai": "AI 在线生成",
    "offline": "离线原文包装",
    "format": "整理完整提示词",
    "Idea / prompt to rewrite": "创意 / 待整理提示词",
    "API key (cleared when sharing the workflow)": "API Key（分享工作流前请清空）",
    "H3 mode": "H3 模式",
    "Endpoint override": "覆盖端点（可选）",
    "Model override": "覆盖模型名（可选）",
    "endpoint override": "覆盖端点（可选）",
    "model override": "覆盖模型名（可选）",
    "Bounded enrichment": "AI 扩写（受限）",
    "Auto timestamps": "自动时间戳",
    "Fixed camera": "固定镜头",
    "Visual stability": "画面稳定约束",
    "Anti-pop inline dialogue": "抗残音内联对白",
    "Strict validation": "严格校验",
    "zh": "中文",
    "mixed": "English 正文 + 对白原文（官方）",
    "en": "English",
    "auto": "自动（随主模式）",
    "t2va": "文生视频",
    "i2va": "首帧",
    "fl2va": "首尾帧",
    "l2va": "尾帧",
    "ref2va": "全参考",
    "deepseek": "DeepSeek",
    "glm": "GLM 智谱",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "dashscope": "通义千问（DashScope）",
    "siliconflow": "硅基流动（SiliconFlow）",
    "custom": "自定义（手填端点与模型）",
    "Audio": "音频",
    "First and last frames": "首尾帧",
    "Reference audio": "参考音频",
    "+ Add media": "＋ 添加素材",
    "T2VA · Text-to-video": "T2VA · 文生视频",
    "Text-to-video": "文生视频",
    "First/last frames / Text-to-video": "首尾帧 / 文生视频",
    "All-purpose reference": "全能参考",
    "Prompt:\nClick an uploaded asset to insert its tag, e.g. <picture 1>, <video 1>, or <audio 2>;\nDouble-click a video to insert its audio tag; mute a video at the top-right to exclude its audio from references": "提示词：\n单击已上传的素材自动添加标签，如：<picture 1>、<video 1>、<audio 2>；\n双击视频自动添加视频中的音频标签；视频右上角静音后音频将不传入参考",
    "Advanced options": "高级选项",
    "No-subtitle constraint": "无字幕约束",
    "Soundscape": "音景",
    "Music": "配乐",
    "Injects a positive no-subtitle constraint after [Shot 1]: speech exists only as audible voice with lip sync and never appears as on-screen text. Disable to strip it from the prompt": "在提示词的 [Shot 1] 后注入正向无字幕约束：语音仅以音频呈现并与口型同步，台词绝不以文字出现在画面中；关闭后自动从提示词移除该约束",
    "On: rewrite an N/A overall_soundscape with a default ambient description; the rest of the prompt stays untouched. Off: force N/A for a fully silent bed": "开启：将提示词中的 overall_soundscape: N/A 自愈为默认环境声描述，其余内容不改写；关闭：强制 N/A（全片无环境声）",
    "On: rewrite an N/A non_diegetic_music with a default score description; the rest of the prompt stays untouched. Off: force N/A (no score)": "开启：将提示词中的 non_diegetic_music: N/A 自愈为默认配乐描述，其余内容不改写；关闭：强制 N/A（无配乐）",
    "Audio mode": "音频模式",
    "Audio denoise strength": "音频去噪强度",
    "Source audio as reference": "源音频作为参考",
    "Strict prompt tags": "严格提示词标签",
    "Reference image size": "参考素材尺寸",
    "Reference video policy": "参考视频策略",
    "Force FPS": "强制帧率",
    "match": "匹配",
    "1.2x": "1.2倍",
    "1.5x": "1.5倍",
    "2x": "2倍",
    "max": "最大值",
    "First frame": "首帧",
    "Last frame": "尾帧",
    "First frame empty means text-to-video": "首帧留空为文生视频",
    "Last frame empty means text-to-video": "尾帧留空为文生视频",
    "First and last frames empty means text-to-video": "首尾帧留空为文生视频",
    "Reference audio": "参考音频",
    "Optional": "可选",
    "Add media": "添加素材",
    "Reference media": "参考素材",
    "Images x9 · Videos x3 (mp4/mov) · Audios x3 (mp3/wav/flac...)": "支持图像x9 · 视频x3(mp4/mov) · 音频x3 (mp3/wav/flac...)",
    "Up to 9 images": "最多9张图像",
    "Up to 3 videos": "最多3个视频",
    "Up to 3 audios": "最多3个音频",
    "Optimize": "优化", "LLM Prompt Optimization Configuration": "LLM提示词优化配置",
    "Provider": "平台", "API key": "API Key", "Read visual references": "读取视觉素材",
    "Save": "保存", "Cancel": "取消", "Custom": "自定义",
    "API URL": "API 地址", "Model": "模型", "Protocol": "协议",
    "Prompt is connected to an upstream node; the internal prompt is disabled!": "提示词已连接上游节点，内部提示词已禁用！",
    "Optimize prompt": "优化提示词", "Optimizing click to cancel": "优化中 点击取消",
    "Optimizing": "优化中", "Restore before optimization": "恢复优化前", "Configure API": "配置API",
    "Prompt optimizer API is not configured. Open settings now?": "尚未配置提示词优化 API，是否立即打开设置？",
    "Confirm": "确定",
    "Output language": "输出语言",
    "Maximum output tokens": "最大输出 Tokens",
    "Optimization mode": "优化方式", "Online API": "在线 API", "Local vision model": "本地视觉模型",
    "Refresh local models": "刷新本地模型", "Local model": "本地模型", "Local device": "本地设备",
    "Search local models": "搜索本地模型", "Search models": "搜索模型", "No matching models": "没有匹配的模型",
    "Automatic optimization before run": "运行前自动优化提示词", "No compatible local vision models found": "未找到可用的本地视觉模型",
    "Missing local model dependencies": "缺少本地模型依赖",
    "Choose vision projector": "选择视觉投影模型", "Vision model (mmproj)": "视觉模型（mmproj）",
    "No mmproj models found": "未找到mmproj视觉模型",
    "Multiple matching mmproj files were found. Choose one:": "检测到多个匹配的mmproj文件，请选择一个：",
    "GGUF dependency unavailable": "GGUF运行依赖不可用",
    "Download matching dependency": "下载匹配依赖",
    "Restart ComfyUI after installation": "安装后请重启ComfyUI",
    "Trim audio": "裁剪音频",
    "Audio trim": "音频截取",
    "Start": "开始",
    "End": "结束",
    "Selected duration": "选取时长",
    "Play selection": "播放选区",
    "Pause preview": "暂停预览",
    "Sync target duration": "同步目标时长",
    "Saving": "保存中",
    "Audio decoding failed": "音频解码失败",
    "Audio trim failed": "音频裁剪失败",
};

Object.assign(DOM_TRANSLATIONS, {
    "Audio redraw strength": "\u97f3\u9891\u91cd\u7ed8\u5f3a\u5ea6",
    "native": "\u81ea\u52a8\u751f\u6210",
    "reference_only": "\u4ec5\u53c2\u8003\u97f3\u9891",
    "lock_source": "\u539f\u58f0\u8f93\u51fa",
    "remix_source": "\u91cd\u6df7\u6e90\u97f3\u9891",
    "Mute video": "\u4e0d\u53c2\u8003\u97f3\u9891",
    "Unmute video": "\u53c2\u8003\u97f3\u9891",
    "Drive audio ordinal": "\u9a71\u52a8\u97f3\u9891\u5e8f\u53f7",
    "Drive audio": "\u9a71\u52a8\u97f3\u9891",
    "None": "\u65e0",
});

function currentLocale() {
    const candidates = [
        app?.ui?.settings?.getSettingValue?.("Comfy.Locale"),
        app?.ui?.settings?.getSettingValue?.("Comfy.Language"),
        document.documentElement?.lang,
        document.body?.dataset?.locale,
        document.body?.dataset?.language,
        localStorage.getItem("Comfy.Settings.Language"),
        localStorage.getItem("Comfy.Settings.language"),
        localStorage.getItem("language"),
        localStorage.getItem("locale"),
        navigator.language,
    ];
    return candidates.find(value => typeof value === "string" && value.trim())?.toLowerCase() || "en";
}

function isChineseLocale() { return /^(zh|cn|中文)/i.test(currentLocale()); }
function t(text) { return isChineseLocale() ? (DOM_TRANSLATIONS[text] || text) : text; }
function aspectInternalValue(value) {
    const aliases = {
        "自适应": "adaptive",
        "16:9 (Widescreen)": "16:9",
        "9:16 (Portrait)": "9:16",
        "21:9 (Ultrawide)": "21:9",
    };
    return aliases[value] || value;
}
function aspectDisplayValue(value) {
    const internal = aspectInternalValue(value);
    if (isChineseLocale() && internal === "adaptive") return "自适应";
    return internal;
}

const ASPECT_INTERNAL_VALUES = [
    "adaptive", "16:9", "9:16", "3:2", "2:3", "4:3", "3:4", "1:1", "21:9",
];
const ASPECT_DISPLAY_VALUES = {
    adaptive: "自适应",
    "16:9": "16:9",
    "9:16": "9:16",
    "3:2": "3:2",
    "2:3": "2:3",
    "4:3": "4:3",
    "3:4": "3:4",
    "1:1": "1:1",
    "21:9": "21:9",
};
function aspectOptionLabel(value) {
    const internal = aspectInternalValue(value);
    return isChineseLocale() ? (ASPECT_DISPLAY_VALUES[internal] || internal) : internal;
}
function normalizeAspectWidget(node) {
    const control = widget(node, "aspect");
    if (!control) return;
    const normalized = aspectInternalValue(control.value);
    if (!ASPECT_INTERNAL_VALUES.includes(normalized)) return;
    if (control.value !== normalized) control.value = normalized;
    control._mxvAspectInternal = normalized;
}
function installAspectDisplay(node) {
    const control = widget(node, "aspect");
    if (!control || control._mxvAspectWrapped || typeof control.draw !== "function") return;
    const originalDraw = control.draw;
    control.draw = function(ctx, nodeRef, width, y, height) {
        const savedValue = this.value;
        const savedValues = this.options?.values;
        const internalValues = ASPECT_INTERNAL_VALUES.slice();
        try {
            if (this.options && Array.isArray(savedValues)) {
                this.options.values = internalValues.map(aspectOptionLabel);
                this.value = aspectOptionLabel(savedValue);
            }
            return originalDraw.call(this, ctx, nodeRef, width, y, height);
        } finally {
            this.value = savedValue;
            if (this.options && Array.isArray(savedValues)) this.options.values = savedValues;
        }
    };
    control._mxvAspectWrapped = true;
}
function drawAspectDisplayOverlay(node, ctx) {
    if (!isChineseLocale()) return;
    const control = widget(node, "aspect");
    if (!control || control.value !== "adaptive" || control === node.__mxvActiveWidget) return;
    const y = Number(control.y);
    const height = Number(control.height) || 30;
    const width = Number(node.size?.[0]) || 0;
    if (!Number.isFinite(y) || width <= 80) return;
    const left = 34;
    const right = width - 34;
    ctx.save();
    ctx.fillStyle = node.bgcolor || "#1b1e23";
    ctx.fillRect(left, y + 2, Math.max(0, right - left), Math.max(0, height - 4));
    ctx.fillStyle = "#e8e6e1";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = `${Math.max(12, Math.round(height * 0.55))}px Arial,sans-serif`;
    ctx.fillText("自适应", (left + right) / 2, y + height / 2);
    ctx.restore();
}

function round32(v) { return Math.max(32, Math.floor(v / 32 + 0.5) * 32); }
function canvas(aspect, mp, adaptiveRatio = 16 / 9) {
    const area = Math.max(0.2, Number(mp) || 0.5) * 1024 * 1024;
    const ratio = ASPECTS[aspectInternalValue(aspect)] ?? adaptiveRatio;
    const width = round32(Math.sqrt(area * ratio));
    const height = ratio === 1 ? width : round32(Math.sqrt(area / ratio));
    return `${width}x${height}`;
}
function widget(node, name) { return node.widgets?.find(w => w.name === name); }
function setWidget(node, name, value) {
    const w = widget(node, name); if (!w) return;
    // Zero is a valid value for integer/float controls such as the optional
    // primary-audio ordinal. Only actual empty values should become (none).
    const next = value === null || value === undefined || value === "" ? "(none)" : value;
    if (w.value === next) return;
    w.value = next;
    w.callback?.call(w, w.value);
}
function setPromptWidget(node, value) {
    const w = widget(node, "prompt"); if (!w) return;
    const next = value ?? "";
    if (w.value === next) return;
    w.value = next;
    w.callback?.call(w, w.value);
}
function cleanPrompt(value) { return value && value !== "(none)" ? value : ""; }
function hasOwn(object, key) { return Object.prototype.hasOwnProperty.call(object, key); }
function setMediaWidget(node, name, value) {
    const w = widget(node, name); if (!w) return;
    // Media slots are optional STRING inputs. Keep unused slots truly empty;
    // the old Combo sentinel "(none)" is not a valid uploaded filename.
    const next = value === null || value === undefined || value === "" || value === "(none)" ? "" : value;
    if (w.value === next) return;
    w.value = next;
    w.callback?.call(w, w.value);
}
function normalizeIntWidget(node, name, fallback, minimum, maximum) {
    const w = widget(node, name); if (!w) return fallback;
    const parsed = Number(w.value);
    const value = Number.isFinite(parsed)
        ? Math.max(minimum, Math.min(maximum, Math.trunc(parsed)))
        : fallback;
    if (w.value !== value) {
        w.value = value;
        w.callback?.call(w, value);
    }
    return value;
}
function sanitizeHiddenInputs(node) {
    normalizeIntWidget(node, "drive_audio_ordinal", 1, 0, 6);
    const task = widget(node, "task_type");
    if (task && !["auto", "t2va", "i2va", "l2va", "fl2va", "ref2va", "hybrid"].includes(task.value)) {
        task.value = "auto";
    }
}
function hideWidget(w) {
    if (!w) return;
    w.hidden = true; w.options = w.options || {}; w.options.hidden = true;
    w.computeSize = () => [0, -4]; w.serialize = true;
}
/* AI 生成参数组（prompt_source + ai_*）：不渲染为节点体原生 widget（新版前端的
   widget 渲染不走 computeSize，折叠不可靠），全部隐藏并由 DOM 面板内的
   「AI 生成」区块接管 UI；控件值仍由这些 widget 承载并随工作流序列化。 */
/* 采样器/调度器选项：默认项与 ComfyUI 常用项（基线与 sampling.py 的 SAMPLER_OPTIONS 一致，
   完整清单仍可通过 API 直接传入节点输入）。 */
const SAMPLER_OPTIONS = ["dual_clock_euler", "euler", "euler_ancestral", "heun", "dpm_2", "dpmpp_2m", "dpmpp_2m_sde", "ddim", "uni_pc"];
const SCHEDULER_OPTIONS = ["native_flow", "normal", "karras", "sgm_uniform", "simple", "beta"];
const AI_GROUP_WIDGETS = [
    "prompt_source",
    "ai_text", "ai_language", "ai_mode", "ai_enrich", "ai_soundscape", "ai_music",
    "ai_auto_timestamps", "ai_fixed_camera", "ai_visual_stability", "ai_no_subtitles",
    "ai_anti_pop", "ai_strict_validation", "ai_provider", "ai_api_key",
    "ai_endpoint", "ai_model", "ai_timeout",
];
function hideAiGroupWidgets(node) {
    for (const name of AI_GROUP_WIDGETS) hideWidget(widget(node, name));
}
function make(tag, css = {}, text = "") {
    const el = document.createElement(tag); Object.assign(el.style, css);
    if (text) el.textContent = text; return el;
}
function kindOf(file) {
    if (file.type?.startsWith("image/") || /\.(png|jpe?g|webp|bmp|gif)$/i.test(file.name)) return "image";
    if (file.type?.startsWith("video/") || /\.(mp4|mov|webm|mkv|avi)$/i.test(file.name)) return "video";
    if (file.type?.startsWith("audio/") || /\.(mp3|wav|flac|m4a|aac|ogg|oga|opus|wma|aif|aiff|alac|amr|caf|ac3|mp2)$/i.test(file.name)) return "audio";
    return null;
}
function fileUrl(name) {
    if (!name || name === "(none)") return "";
    const parts = String(name).replaceAll("\\", "/").split("/").filter(Boolean);
    const filename = parts.pop() || "";
    const params = new URLSearchParams({ filename, type: "input", subfolder: parts.join("/") });
    const path = `/view?${params.toString()}`;
    return typeof api.apiURL === "function" ? api.apiURL(path) : path;
}
async function uploadFile(file) {
    const body = new FormData(); body.append("image", file, file.name); body.append("type", "input");
    const response = await api.fetchApi("/upload/image", { method: "POST", body });
    if (!response.ok) throw new Error(`Upload failed: ${response.status}`);
    const result = await response.json();
    return [result.subfolder, result.name].filter(Boolean).join("/");
}

function createPanel(node) {
    if (typeof node.addDOMWidget !== "function") return false;
    // The node itself has a fixed 500px width. Do not cap the panel with
    // max-width:100%: while a workflow/tab is being restored, ComfyUI can
    // briefly report a narrower DOM-widget wrapper. That transient width was
    // retained after the node returned to 500px, leaving the panel compressed
    // until a browser refresh or node rebuild.
    const root = make("div", { position: "relative", width: `${PANEL_WIDTH}px`, minWidth: `${PANEL_WIDTH}px`, boxSizing: "border-box", color: "#e8e6e1", fontFamily: "Arial,sans-serif", fontSize: "12px", userSelect: "none", padding: "3px 0 2px", overflow: "visible" });
    root.className = "mxv-root";
    Object.assign(root.style, {
        display: "flex",
        flexFlow: "column",
        alignItems: "stretch",
        gap: "6px",
        minHeight: "0",
        overflow: "hidden",
    });
    const style = make("style");
    style.textContent = `
      .mxv-modes{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:3px;border:1px solid #383e46;border-radius:16px;margin:3px 0 6px;background:#14171b}
      .mxv-mode{height:26px;border:0;border-radius:12px;background:transparent;color:#b3b1ac;font-size:13px;cursor:pointer;transition:background .12s,color .12s}.mxv-mode.active{background:#e8a33d;color:#221a0c;font-weight:700;box-shadow:0 1px 6px rgba(232,163,61,.35)}
      .mxv-box{border:1px solid #383e46;border-radius:6px;padding:7px;margin:0 0 6px;background:#1b1e23;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}.mxv-title{font-size:12px;color:#efede8;margin-bottom:3px}.mxv-hint{font-size:10px;color:#8c8983;line-height:1.3}
      .mxv-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:5px}.mxv-drop{aspect-ratio:1;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#b3b1ac;cursor:pointer;border:1px dashed #383e46;border-radius:6px;background:#1b1e23;padding:4px;box-sizing:border-box}.mxv-drop:hover{border-style:solid;border-color:#e8a33d;background:rgba(232,163,61,.08);color:#e8e6e1}.mxv-reference-empty{grid-column:1/-1;width:100%;aspect-ratio:5.2/1;align-items:flex-start;justify-content:center;text-align:left;padding:18px 24px}.mxv-reference-empty .mxv-drop-icon{font-size:14px;margin:0 7px 0 0}.mxv-reference-empty .mxv-drop-title{font-size:12px}.mxv-reference-empty .mxv-drop-subtitle{font-size:9px;margin-top:8px}.mxv-drop-icon{font-size:14px;line-height:1.2;margin:0;color:#e8a33d;font-family:Arial,sans-serif}.mxv-drop-title{font-size:10px;line-height:1.2;color:#e2e0db}.mxv-drop-title-row{display:flex;align-items:center;justify-content:center;gap:5px;line-height:1.2}.mxv-optional{color:#8a6d33;font-size:.82em;line-height:1.2;position:relative;top:-1px}.mxv-audio-drop .mxv-optional{top:-2px}.mxv-drop-subtitle{font-size:8px;line-height:1.25;color:#8c8983;margin-top:3px}.mxv-limit{grid-column:1/-1;color:#d47d8b;font-size:9px;padding:2px 3px 0;text-align:left}
      .mxv-keygrid{display:grid;grid-template-columns:1fr 1fr;gap:5px}.mxv-keygrid .mxv-drop{aspect-ratio:16/9}.mxv-keygrid .mxv-drop:not(.mxv-audio-drop) .mxv-drop-subtitle{font-size:7px;color:#7d7a74}.mxv-keygrid .mxv-audio-card,.mxv-keygrid .mxv-audio-drop{grid-column:1/-1;width:100%;height:34px;aspect-ratio:auto;margin-top:3px}
      .mxv-card{min-width:0;aspect-ratio:1;border:1px solid #383e46;border-radius:6px;background:#1b1e23;overflow:hidden;position:relative;cursor:pointer;touch-action:none}.mxv-card.mxv-reorder-source{opacity:.68;cursor:grabbing}.mxv-card.mxv-reorder-target{border-color:#e8a33d;box-shadow:0 0 0 2px rgba(232,163,61,.45) inset}.mxv-reorder-indicator{display:none;position:absolute;left:50%;top:50%;z-index:8;transform:translate(-50%,-50%);width:24px;height:24px;border-radius:50%;align-items:center;justify-content:center;background:rgba(20,22,26,.78);color:#f4e8d0;font:18px/24px Arial,sans-serif;pointer-events:none;box-shadow:0 0 0 1px rgba(232,163,61,.6)}.mxv-card.mxv-reorder-source .mxv-reorder-indicator{display:flex}.mxv-card img,.mxv-card video{display:block;width:100%;height:100%;object-fit:cover;background:#14171b}.mxv-card:hover img,.mxv-card:hover video{object-fit:contain}.mxv-card-name{position:absolute;left:0;right:0;bottom:0;padding:2px 15px 2px 3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#fff;background:rgba(16,17,20,.68);font-size:7px;line-height:1.15}.mxv-remove{position:absolute;right:1px;bottom:0;border:0;background:transparent;color:#d5d2cc;cursor:pointer;font-size:11px;z-index:3}.mxv-media-controls{position:absolute;left:3px;right:3px;bottom:12px;z-index:4;height:14px;display:flex;align-items:center;color:rgba(255,255,255,.6);font:8px/1 Arial,sans-serif;pointer-events:none}.mxv-media-toggle{width:14px;height:14px;padding:0;border:0;background:rgba(46,50,56,.6)!important;border-radius:50%;cursor:pointer;opacity:1;display:flex;align-items:center;justify-content:center;pointer-events:auto}.mxv-media-toggle svg{display:block;width:10px;height:10px;overflow:visible}.mxv-media-time{margin-left:auto}.mxv-audio-drop{grid-column:1/-1;width:100%;height:34px;min-height:34px;aspect-ratio:auto;margin-top:0;font-size:9px}.mxv-audio-card{grid-column:1/-1;width:100%;height:34px;aspect-ratio:auto;margin-top:0}.mxv-prompt{display:block;width:100%;height:100%;min-height:0;resize:none;overflow:auto;box-sizing:border-box;border:0;border-radius:6px;background:#1b1e23;color:#e8e6e1;padding:7px 7px calc(7px + 14 * 1.4em);font:12px/1.4 Arial,sans-serif;outline:none;user-select:text;scrollbar-width:thin;scrollbar-color:#2e3238 transparent}.mxv-prompt::placeholder{color:#4a5058;opacity:1}.mxv-prompt::-webkit-scrollbar{width:5px}.mxv-prompt::-webkit-scrollbar-track{background:transparent}.mxv-prompt::-webkit-scrollbar-thumb{background:#2e3238;border-radius:3px}.mxv-prompt::-webkit-scrollbar-thumb:hover{background:#383e46}.mxv-advanced{position:relative;margin:0 0 6px;box-sizing:border-box;user-select:none;background:#1b1e23;border:1px solid #383e46;border-radius:8px}.mxv-advanced>summary{background:transparent!important;padding-left:10px;padding-right:10px;border-radius:8px;list-style:none;min-height:28px;color:#b3b1ac;font-size:12px}.mxv-advanced>summary::-webkit-details-marker{display:none}.mxv-advanced>summary:before{content:"▸ ";color:#8a6d33}.mxv-advanced[open]>summary:before{content:"▾ "}.mxv-advanced .mxv-advanced-body{background:transparent!important;padding-left:10px;padding-right:10px;border-radius:0 0 8px 8px}.mxv-advanced[open]{background:#1b1e23;border:1px solid #383e46;border-radius:8px}.mxv-size{color:#d9992f;font-size:12px;padding:2px 0 4px}
      .mxv-size{display:flex;justify-content:space-between;align-items:center;color:#d9992f;font-size:12px;padding:2px 3px 5px}.mxv-task{white-space:nowrap}.mxv-dimensions{white-space:nowrap;text-align:right}.mxv-advanced-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(140px,220px);align-items:center;gap:8px;min-height:30px}.mxv-advanced-row>label{text-align:left;color:#b3b1ac}.mxv-control{width:220px;justify-self:end;box-sizing:border-box;background:#1b1e23;color:#e8e6e1;border:1px solid #383e46;border-radius:4px;padding:5px}.mxv-number{width:220px;height:30px;display:grid;grid-template-columns:26px minmax(0,1fr) 26px;align-items:stretch;justify-self:end}.mxv-number button{border:1px solid #383e46;background:#1b1e23;color:#e8e6e1;font-size:10px;padding:0;cursor:pointer}.mxv-number button:first-child{border-radius:4px 0 0 4px}.mxv-number button:last-child{border-radius:0 4px 4px 0}.mxv-number input{width:100%;min-width:0;border:1px solid #383e46;border-left:0;border-right:0;border-radius:0;background:#1b1e23;color:#e8e6e1;padding:5px;box-sizing:border-box}.mxv-number input::-webkit-inner-spin-button,.mxv-number input::-webkit-outer-spin-button{appearance:none;margin:0}.mxv-toggle{position:relative;display:inline-flex;width:38px;height:22px;justify-self:end;cursor:pointer}.mxv-toggle input{opacity:0;width:0;height:0}.mxv-toggle span{position:absolute;inset:0;border-radius:12px;background:#2e3238;border:1px solid #4a5058;transition:.15s}.mxv-toggle span:before{content:"";position:absolute;width:16px;height:16px;left:2px;top:2px;border-radius:50%;background:#d5d2cc;transition:.15s}.mxv-toggle input:checked+span{background:#e8a33d;border-color:#e8a33d}.mxv-toggle input:checked+span:before{transform:translateX(16px);background:#fff}
      .mxv-drop-title-row .mxv-drop-icon{display:inline-flex;align-items:center;justify-content:center;height:1.2em;font-size:10px;line-height:1;margin:0}
      .mxv-audio-drop .mxv-drop-icon{font-size:14px;line-height:1;height:1.2em}
      .mxv-drop-title-row .mxv-optional{position:static;display:inline-flex;align-items:center;height:1.2em;line-height:1.2}
      .mxv-keygrid .mxv-drop:not(.mxv-audio-drop) .mxv-drop-icon,.mxv-keygrid .mxv-drop:not(.mxv-audio-drop) .mxv-optional{transform:translateY(-1px)}
       .mxv-prompt-wrap{position:relative;display:grid;grid-template-rows:22px minmax(160px,auto);width:100%;min-height:190px;background:#1b1e23;border-radius:6px;overflow:hidden;border-left:3px solid #e8a33d}.mxv-prompt-wrap .mxv-prompt{grid-row:2;border-radius:0 0 6px 6px}.mxv-prompt-wrap.external .mxv-prompt{opacity:.42;cursor:not-allowed}.mxv-prompt-tools{grid-row:1;display:flex;align-items:center;justify-content:flex-end;gap:3px;padding:2px 5px;box-sizing:border-box;background:#1b1e23;z-index:4}.mxv-prompt-elapsed{display:none;margin-right:auto;color:#75726c;font:9px/17px Arial,sans-serif}.mxv-prompt-elapsed.visible{display:inline-block}.mxv-optimizer-model{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right;color:rgba(140,137,131,.6);font:8px/17px Arial,sans-serif;margin-left:auto;margin-right:17px}.mxv-prompt-tool{height:17px;min-width:17px;padding:0 3px;border:0;border-radius:3px;background:#1b1e23;color:#7d7a74;font:11px/17px Arial,sans-serif;cursor:pointer;opacity:.72;flex:0 0 auto}.mxv-optimize-tool{font-size:13px}.mxv-prompt-tool:hover{color:#b3b1ac;background:#2e3238}.mxv-prompt-tool:disabled{opacity:.25;cursor:not-allowed}.mxv-prompt-reset{display:none;font-size:12px;line-height:15px}.mxv-prompt-reset.visible{display:inline-block}.mxv-prompt-loading{color:#e8a33d!important;opacity:1!important;animation:mxv-spin 1.6s linear infinite}@keyframes mxv-spin{to{transform:rotate(360deg)}}.mxv-tool-tip{position:fixed;z-index:10100;padding:4px 7px;border-radius:4px;background:#1b1e23;color:#e8e6e1;border:1px solid #383e46;font:10px/1.2 Arial,sans-serif;pointer-events:none;white-space:nowrap}.mxv-opt-check{justify-self:end;width:auto!important}.mxv-opt-language{display:grid;grid-template-columns:1fr 1fr;width:100%;align-items:center}.mxv-opt-language label{display:flex;align-items:center;gap:4px;white-space:nowrap}.mxv-opt-language label:first-child{justify-self:start}.mxv-opt-language label:last-child{justify-self:end}.mxv-opt-language input{width:auto}
       .mxv-prompt-wrap{grid-template-columns:minmax(0,1fr)}.mxv-prompt-wrap .mxv-prompt-rich{position:relative;grid-row:2;left:0;right:0;top:0;bottom:auto;width:100%;height:auto;min-height:168px;max-height:none;z-index:2;overflow-x:hidden;overflow-y:auto;background:#1b1e23;color:#e8e6e1;-webkit-text-fill-color:currentColor;caret-color:#e8e6e1;line-height:2.35;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;padding:7px 7px calc(7px + 14 * 1.4em)}.mxv-prompt-rich.mxv-prompt-empty:before{content:attr(data-placeholder);position:absolute;left:7px;right:7px;top:7px;color:#4a5058;white-space:pre-wrap;pointer-events:none}.mxv-prompt-highlight{display:none}.mxv-prompt-rich mark{padding:0;color:#e8a33d;-webkit-text-fill-color:#e8a33d;background:transparent;font:inherit}.mxv-prompt-section{padding:0;color:#9a7734;-webkit-text-fill-color:#9a7734;background:transparent;font:inherit}.mxv-prompt-tag{display:inline-block;box-sizing:border-box;padding:1px 5px;margin:0 2px;border-radius:5px;background:#3a3d42;color:#cdcdcd;-webkit-text-fill-color:#cdcdcd;font-size:.86em;line-height:1.35;vertical-align:middle;white-space:nowrap}.mxv-prompt-tag-video{background:#443c4a}.mxv-prompt-tag-picture{background:#3d3a2e}.mxv-prompt-media-token{display:inline-flex;align-items:center;vertical-align:middle;white-space:nowrap}.mxv-prompt-media-preview{position:static;flex:0 0 26px;width:26px;height:26px;margin-left:.5em;margin-right:4px;box-sizing:border-box;border:1px solid rgba(148,120,62,.7);border-radius:5px;background:#22262c;object-fit:cover;color:#c2a05c;display:inline-flex;align-items:center;justify-content:center;font:14px/26px Arial,sans-serif;overflow:hidden;vertical-align:middle;user-select:none}.mxv-prompt-media-preview.mxv-prompt-audio-preview{border-radius:50%}.mxv-prompt-media-preview.mxv-prompt-audio-preview:before{content:"♫";font-size:13px}.mxv-prompt-wrap.external .mxv-prompt-rich{opacity:.42}
        .mxv-opt-overlay{position:fixed;inset:0;z-index:10050;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.58);font:12px Arial,sans-serif}.mxv-opt-dialog{width:min(470px,calc(100vw - 30px));background:#1b1e23;color:#e8e6e1;border:1px solid #383e46;border-radius:8px;box-shadow:0 18px 50px rgba(0,0,0,.5);padding:14px}.mxv-opt-title{font-size:12px;margin-bottom:12px;white-space:nowrap}.mxv-opt-row{display:grid;grid-template-columns:140px minmax(0,1fr);align-items:center;gap:8px;min-height:38px;margin:0}.mxv-opt-row>span{white-space:nowrap}.mxv-opt-row input,.mxv-opt-row select{width:100%;box-sizing:border-box;background:#1b1e23;color:#e8e6e1;border:1px solid #383e46;border-radius:4px;padding:6px;font:inherit}.mxv-opt-hidden{display:none!important}.mxv-opt-model-row{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:7px;min-width:0}.mxv-opt-model-native{display:none}.mxv-opt-model-picker{width:100%;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left;background:#1b1e23;color:#e8e6e1;border:1px solid #383e46;border-radius:4px;padding:6px 24px 6px 7px;cursor:pointer;position:relative;font:inherit}.mxv-opt-model-picker:after{content:"⌄";position:absolute;right:7px}.mxv-opt-model-menu{display:none;position:absolute;left:0;top:calc(100% + 4px);z-index:10070;box-sizing:border-box;width:max-content;min-width:100%;max-width:calc(100vw - 30px);padding:5px;background:#1b1e23;border:1px solid #383e46;border-radius:5px;box-shadow:0 8px 22px rgba(0,0,0,.45)}.mxv-opt-model-menu.open{display:block}.mxv-opt-model-search{display:block;width:100%;min-width:100%;margin-bottom:5px;font:inherit}.mxv-opt-model-results{max-height:285px;overflow:auto}.mxv-opt-model-option{display:block;width:max-content;min-width:100%;border:0;background:transparent;color:#e8e6e1;text-align:left;padding:6px;border-radius:3px;white-space:nowrap;cursor:pointer;font:inherit}.mxv-opt-model-option:hover,.mxv-opt-model-option.selected{background:#2e3238}.mxv-opt-model-empty{padding:7px;color:#b3b1ac;font-size:12px}.mxv-opt-refresh{display:flex;align-items:center;justify-content:center;width:30px;height:30px;margin:0;background:#1b1e23;color:#e8e6e1;border:1px solid #383e46;border-radius:4px;padding:0;cursor:pointer;font-size:16px;line-height:1;white-space:nowrap;transition:background .12s,color .12s,border-color .12s}.mxv-opt-refresh:hover{background:#2e3238;border-color:#e8a33d;color:#e8e6e1}.mxv-opt-refresh:active{background:#e8a33d;border-color:#e8a33d;color:#221a0c}.mxv-opt-refresh.loading{background:#e8a33d;border-color:#e8a33d;color:#221a0c;cursor:wait}.mxv-opt-refresh.loading svg{animation:mxv-spin 1.2s linear infinite}.mxv-opt-refresh:disabled{opacity:.85}.mxv-opt-dependencies{margin:6px 0;color:#d98d97;font-size:12px;line-height:1.35}.mxv-opt-checks{margin:0}.mxv-opt-actions{display:flex;justify-content:flex-end;gap:7px;margin-top:28px}.mxv-opt-actions button{border:1px solid #383e46;border-radius:4px;background:#1b1e23;color:#e8e6e1;padding:5px 12px;cursor:pointer}.mxv-opt-actions button:last-child{background:#e8a33d;border-color:#e8a33d;color:#221a0c}.mxv-opt-custom{display:none}.mxv-opt-dialog.custom .mxv-opt-custom{display:grid}
        .mxv-audio-trim-button,.mxv-sound{position:absolute;z-index:6;width:14px;height:14px;padding:0;border:0;border-radius:50%;background:rgba(46,50,56,.6)!important;color:#fff;font:9px/14px Arial,sans-serif;cursor:pointer;opacity:0;pointer-events:none;transition:opacity .12s ease}.mxv-audio-trim-button{right:3px;top:3px}.mxv-audio-card .mxv-audio-trim-button{right:40px;top:4px}.mxv-sound{right:2px;top:2px}.mxv-card:hover .mxv-audio-trim-button,.mxv-card:hover .mxv-sound,.mxv-audio-trim-button:focus-visible,.mxv-sound:focus-visible{opacity:1;pointer-events:auto}.mxv-audio-trim-button:hover,.mxv-sound:hover{color:#fff;background:rgba(74,80,88,.72)!important}
        .mxv-trim-overlay{position:fixed;inset:0;z-index:10200;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.58);font:12px Arial,sans-serif}.mxv-trim-dialog{width:min(720px,calc(100vw - 30px));box-sizing:border-box;padding:14px;background:#1b1e23;color:#e8e6e1;border:1px solid #383e46;border-radius:8px;box-shadow:0 18px 50px rgba(0,0,0,.5)}.mxv-trim-title{font-size:13px;margin-bottom:12px}.mxv-trim-wave-wrap{position:relative;width:100%;height:190px;overflow:hidden;border:1px solid #383e46;border-radius:6px;background:#14171b;cursor:crosshair;touch-action:none}.mxv-trim-wave-wrap.mxv-trim-panning{cursor:grabbing}.mxv-trim-wave{display:block;width:100%;height:100%}.mxv-trim-loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#b3b1ac;background:#14171b}.mxv-trim-times{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:10px}.mxv-trim-time{position:relative;padding:7px 9px;border-radius:5px;background:#1b1e23;color:#b3b1ac}.mxv-trim-time strong{display:block;margin-top:3px;color:#e8e6e1;font-size:13px;font-weight:500}.mxv-trim-sync{position:absolute;right:6px;top:5px;border:1px solid #383e46;border-radius:4px;background:#2e3238;color:#e8e6e1;padding:2px 7px;font:12px/16px Arial,sans-serif;cursor:pointer;box-shadow:inset 0 1px rgba(255,255,255,.04)}.mxv-trim-sync:hover{background:#2e3238;border-color:#e8a33d;color:#e8e6e1}.mxv-trim-controls{display:flex;align-items:center;gap:8px;margin-top:12px}.mxv-trim-preview{display:flex;align-items:center;justify-content:center;width:30px;height:28px;border:1px solid #383e46;border-radius:4px;background:#1b1e23;color:#e8e6e1;padding:0;cursor:pointer}.mxv-trim-preview svg{width:12px;height:12px}.mxv-trim-hint{color:#b3b1ac;font-size:10px}.mxv-trim-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}.mxv-trim-actions button{border:1px solid #383e46;border-radius:4px;background:#1b1e23;color:#e8e6e1;padding:6px 14px;cursor:pointer}.mxv-trim-actions button:last-child{background:#e8a33d;border-color:#e8a33d;color:#221a0c}.mxv-trim-actions button:disabled,.mxv-trim-preview:disabled{opacity:.45;cursor:not-allowed}
      .mxv-details{border:1px solid #383e46;border-radius:6px;background:#1b1e23;overflow:hidden;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}
      .mxv-details>summary{list-style:none;background:#22262c;border-bottom:1px solid transparent;transition:background .12s}
      .mxv-details>summary::-webkit-details-marker{display:none}
      .mxv-details>summary:hover{background:#2a2f36}
      .mxv-details[open]>summary{border-bottom-color:#383e46}
      .mxv-root>.mxv-size,.mxv-root>.mxv-modes,.mxv-root>.mxv-box,.mxv-root>.mxv-prompt-wrap,.mxv-advanced,.mxv-ai{flex-shrink:0}
      .mxv-advanced,.mxv-ai{border:1px solid #383e46;border-radius:6px;background:#1b1e23;overflow:hidden;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}
      .mxv-advanced>summary,.mxv-ai>summary{list-style:none;background:#22262c}
      .mxv-advanced>summary::-webkit-details-marker,.mxv-ai>summary::-webkit-details-marker{display:none}
      .mxv-advanced>summary:hover,.mxv-ai>summary:hover{background:#2a2f36}
      .mxv-advanced[open]>summary,.mxv-ai[open]>summary{border-bottom:1px solid #383e46}
      .mxv-advanced>summary:before,.mxv-ai>summary:before{content:"";position:absolute;left:11px;top:50%;width:0;height:0;border-left:5px solid #e8a33d;border-top:4px solid transparent;border-bottom:4px solid transparent;transform:translateY(-50%);transition:transform .12s}
      .mxv-advanced[open]>summary:before,.mxv-ai[open]>summary:before{transform:translateY(-50%) rotate(90deg)}
    `;
    // Shared control states also apply to dialogs mounted outside the panel.
    style.textContent += `
      .mxv-control,.mxv-number input,.mxv-opt-dialog,.mxv-trim-dialog{color-scheme:dark;font-family:Arial,sans-serif;font-size:12px}
      .mxv-opt-dialog{box-sizing:border-box}
      .mxv-opt-row>span{color:#b3b1ac}
      .mxv-opt-title,.mxv-trim-title{font-size:12px;color:#e8e6e1}
      .mxv-opt-actions,.mxv-trim-actions{gap:8px;margin-top:14px}
      .mxv-opt-actions button,.mxv-trim-actions button{min-height:30px;padding:5px 12px;font:12px Arial,sans-serif}
      .mxv-opt-actions button:last-child,.mxv-trim-actions button:last-child{font-weight:600}
      .mxv-opt-row input[type=checkbox],.mxv-opt-row input[type=radio]{accent-color:#e8a33d}
      .mxv-control:focus-visible,.mxv-number input:focus-visible,.mxv-opt-dialog input:focus-visible,.mxv-opt-dialog select:focus-visible,.mxv-opt-dialog button:focus-visible,.mxv-trim-dialog button:focus-visible{outline:2px solid #e8a33d;outline-offset:2px}
      .mxv-toggle input:focus-visible+span{outline:2px solid #e8a33d;outline-offset:2px}
      .mxv-opt-actions button:not(:last-child):hover:not(:disabled),.mxv-trim-actions button:not(:last-child):hover:not(:disabled),.mxv-trim-preview:hover:not(:disabled){background:#2e3238;border-color:#e8a33d}
      .mxv-opt-model-results{scrollbar-width:thin;scrollbar-color:#2e3238 transparent}
    `;
    root.appendChild(style);
    const size = make("div"); size.className = "mxv-size";
    const taskStatus = make("span", {}, t("T2VA · Text-to-video")); taskStatus.className = "mxv-task";
    const dimensions = make("span", {}, canvas(widget(node, "aspect")?.value, widget(node, "megapixels")?.value)); dimensions.className = "mxv-dimensions";
    size.append(taskStatus, dimensions);
    const modes = make("div"); modes.className = "mxv-modes";
    const modeText = make("button", {}, t("First/last frames / Text-to-video")); modeText.className = "mxv-mode";
    const modeRef = make("button", {}, t("All-purpose reference")); modeRef.className = "mxv-mode"; modes.append(modeText, modeRef);
    root.append(size, modes);

    let layoutLock = false;
    let userHeight = INITIAL_NODE_HEIGHT;
    let restoringState = true;
    let restoreEpoch = 0;
    const stateKey = "mxv_state";
    const savedState = (() => {
        try { return JSON.parse(node.properties?.[stateKey] || "{}"); } catch { return {}; }
    })();
    node.properties = node.properties || {};
    const stateWidget = widget(node, "gh_state_json");
    if (stateWidget?.value) {
        try { Object.assign(savedState, JSON.parse(stateWidget.value)); } catch {}
    }
    let optimizerSettings = savedState.optimizer || null;
    let optimizerCache = savedState.optimizerCache || null;
    let optimizerBefore = savedState.optimizerBefore ?? null;
    const optimizerBeforeByMode = savedState.optimizerBeforeByMode && typeof savedState.optimizerBeforeByMode === "object"
        ? { text_keyframes: savedState.optimizerBeforeByMode.text_keyframes ?? null, all_reference: savedState.optimizerBeforeByMode.all_reference ?? null }
        : {
            text_keyframes: (savedState.mode || "text_keyframes") === "text_keyframes" ? optimizerBefore : null,
            all_reference: savedState.mode === "all_reference" ? optimizerBefore : null,
        };
    const serializedState = () => {
        promptByMode[state.mode] = prompt.value;
        return JSON.stringify({
            mode: state.mode,
            media: [...media.entries()],
            vacantMediaSlots: [...vacantMediaSlots.entries()],
            prompt: prompt.value,
            prompts: { ...promptByMode },
            height: userHeight,
            advanced: !!advanced?.open,
            audioModeByMode: { ...audioModeByMode },
            audioModeAutoByMode: { ...audioModeAutoByMode },
            optimizer: optimizerSettings,
            optimizerCache,
            optimizerBefore,
            optimizerBeforeByMode: { ...optimizerBeforeByMode },
        });
    };
    const persistState = () => {
        // During workflow restoration ComfyUI briefly exposes default widget
        // values before onConfigure has restored this panel's per-mode state.
        // Never let that transient state overwrite the serialized workflow.
        if (restoringState) return;
        const value = serializedState();
        if (node.properties[stateKey] === value && (!stateWidget || stateWidget.value === value)) return;
        node.properties[stateKey] = value;
        if (stateWidget) stateWidget.value = value;
    };
    const domWidget = node.addDOMWidget("mxv_panel", "mxv_panel", root, { serialize: false, hideOnZoom: false });
    domWidget.options = domWidget.options || {}; domWidget.options.serialize = false;
    domWidget.options.getMinHeight = () => MIN_NODE_HEIGHT;
    domWidget.options.getHeight = () => "100%";
    domWidget.options.minNodeSize = [WIDTH, 0];
    const baseComputeSize = node.computeSize.bind(node);
    node.computeSize = function(out) {
        const measured = baseComputeSize(out);
        measured[0] = WIDTH;
        return measured;
    };
    const promptWidget = widget(node, "prompt"); hideWidget(widget(node, "main_mode")); hideWidget(promptWidget);
    for (const name of [
        ...mediaSlots, "task_type", "audio_mode", "audio_denoise_strength",
        "drive_audio_ordinal",
        "ref_image_size", "strict_prompt_tags", "gh_state_json",
        "no_subtitle", "soundscape", "music",
        ...AI_GROUP_WIDGETS,
        // 采样参数组（单节点整合新增）：在「高级选项」卡片内提供等价控件，原生 widget 一并隐藏
        "steps", "shift_video", "shift_audio", "sampler_name", "scheduler", "noise_seed",
    ]) hideWidget(widget(node, name));
    // Old graphs may contain the literal string "(none)" in this hidden Int
    // input. Clean it before ComfyUI serializes/submits the prompt.
    sanitizeHiddenInputs(node);
    const prompt = make("div"); prompt.className = "mxv-prompt mxv-prompt-rich"; prompt.contentEditable = "true"; prompt.spellcheck = false;
    prompt.tabIndex = 0;
    prompt.setAttribute("role", "textbox");
    prompt.setAttribute("aria-multiline", "true");
    prompt.style.userSelect = "text";
    prompt.style.pointerEvents = "auto";
    registerPromptEditor(prompt);
    let promptPlainText = cleanPrompt(promptWidget?.value);
    let promptReadOnly = false;
    let promptComposing = false;
    let pendingPromptSnapshot = null;
    const promptHistory = {
        text_keyframes: { undo: [], redo: [] },
        all_reference: { undo: [], redo: [] },
    };
    const ignoredPromptNode = node => node?.classList?.contains("mxv-prompt-media-preview") || node?.classList?.contains("mxv-prompt-caret-sentinel");
    const domText = node => {
        if (!node) return "";
        if (node.nodeType === Node.TEXT_NODE) return node.data;
        if (node.nodeType !== Node.ELEMENT_NODE) return "";
        if (ignoredPromptNode(node)) return "";
        if (node.tagName === "BR") return "\n";
        return [...node.childNodes].map(domText).join("");
    };
    const editorText = () => {
        const value = domText(prompt).replace(/\r/g, "");
        // Chromium may retain a lone BR after deleting all content. It is an
        // editing caret container, not an intentional prompt character.
        return /^\n*$/.test(value) ? "" : value;
    };
    const textOffsetTo = (targetNode, targetOffset) => {
        let result = "";
        let found = false;
        const visit = node => {
            if (found || !node) return;
            if (node === targetNode) {
                if (node.nodeType === Node.TEXT_NODE) result += node.data.slice(0, targetOffset);
                else if (node.nodeType === Node.ELEMENT_NODE) {
                    for (let index = 0; index < Math.min(targetOffset, node.childNodes.length); index++) result += domText(node.childNodes[index]);
                }
                found = true;
                return;
            }
            if (node.nodeType === Node.TEXT_NODE) { result += node.data; return; }
            if (node.nodeType !== Node.ELEMENT_NODE || ignoredPromptNode(node)) return;
            if (node.tagName === "BR") { result += "\n"; return; }
            for (const child of node.childNodes) { visit(child); if (found) break; }
        };
        visit(prompt);
        return result.length;
    };
    const selectionOffsets = () => {
        const selection = window.getSelection();
        if (!selection?.rangeCount || !prompt.contains(selection.anchorNode)) return [promptPlainText.length, promptPlainText.length];
        const anchor = textOffsetTo(selection.anchorNode, selection.anchorOffset);
        const focus = textOffsetTo(selection.focusNode, selection.focusOffset);
        return [Math.min(anchor, focus), Math.max(anchor, focus)];
    };
    const promptSnapshot = () => {
        const [start, end] = selectionOffsets();
        return { text: editorText(), start, end };
    };
    const pushPromptUndo = snapshot => {
        if (!snapshot) return;
        const history = promptHistory[state.mode];
        const last = history.undo[history.undo.length - 1];
        if (!last || last.text !== snapshot.text || last.start !== snapshot.start || last.end !== snapshot.end) {
            history.undo.push(snapshot);
            if (history.undo.length > 300) history.undo.shift();
        }
        history.redo.length = 0;
    };
    const restorePromptSnapshot = snapshot => {
        if (!snapshot) return;
        promptPlainText = snapshot.text;
        renderPromptHighlights();
        prompt.focus();
        setEditorSelection(snapshot.start, snapshot.end);
        promptByMode[state.mode] = prompt.value;
        setPromptWidget(node, prompt.value);
        persistState();
    };
    const stepPromptHistory = redo => {
        const history = promptHistory[state.mode];
        const source = redo ? history.redo : history.undo;
        const target = source.pop();
        if (!target) return;
        const current = promptSnapshot();
        (redo ? history.undo : history.redo).push(current);
        restorePromptSnapshot(target);
    };
    const setEditorSelection = (start, end = start) => {
        const selection = window.getSelection(); if (!selection) return;
        const locate = target => {
            let remaining = Math.max(0, target);
            let location = null;
            const visit = node => {
                if (location || ignoredPromptNode(node)) return;
                if (node.nodeType === Node.TEXT_NODE) {
                    if (remaining <= node.data.length) location = [node, remaining];
                    else remaining -= node.data.length;
                    return;
                }
                if (node.nodeType !== Node.ELEMENT_NODE) return;
                if (node.tagName === "BR") {
                    const parent = node.parentNode, index = [...parent.childNodes].indexOf(node);
                    if (remaining === 0) location = [parent, index];
                    else if (remaining === 1) location = [parent, index + 1];
                    else remaining -= 1;
                    return;
                }
                for (const child of node.childNodes) { visit(child); if (location) return; }
            };
            visit(prompt);
            if (location) return location;
            return [prompt, prompt.childNodes.length];
        };
        const [startNode, startOffset] = locate(start), [endNode, endOffset] = locate(end);
        const range = document.createRange(); range.setStart(startNode, startOffset); range.setEnd(endNode, endOffset);
        selection.removeAllRanges(); selection.addRange(range);
    };
    Object.defineProperties(prompt, {
        value: {
            configurable: true,
            get: () => promptPlainText,
            set: value => { promptPlainText = String(value ?? ""); },
        },
        selectionStart: { configurable: true, get: () => selectionOffsets()[0] },
        selectionEnd: { configurable: true, get: () => selectionOffsets()[1] },
        readOnly: {
            configurable: true,
            get: () => promptReadOnly,
            set: value => { promptReadOnly = !!value; prompt.contentEditable = promptReadOnly ? "false" : "true"; },
        },
        placeholder: {
            configurable: true,
            get: () => prompt.dataset.placeholder || "",
            set: value => { prompt.dataset.placeholder = String(value ?? ""); },
        },
    });
    prompt.setRangeText = (replacement, start, end, selectionMode = "end") => {
        const next = promptPlainText.slice(0, start) + replacement + promptPlainText.slice(end);
        promptPlainText = next;
        renderPromptHighlights();
        const caret = selectionMode === "select" ? [start, start + replacement.length] : [start + replacement.length, start + replacement.length];
        prompt.focus(); setEditorSelection(caret[0], caret[1]);
    };
    prompt.value = promptPlainText;
    prompt.placeholder = t("Prompt:\nClick an uploaded asset to insert its tag, e.g. <picture 1>, <video 1>, or <audio 2>;\nDouble-click a video to insert its audio tag; mute a video at the top-right to exclude its audio from references");
    const legacyPrompt = cleanPrompt(savedState.prompt) || prompt.value;
    const savedPrompts = savedState.prompts && typeof savedState.prompts === "object" ? savedState.prompts : null;
    const savedMode = savedState.mode || widget(node, "main_mode")?.value || "text_keyframes";
    const promptByMode = {
        text_keyframes: savedPrompts && hasOwn(savedPrompts, "text_keyframes")
            ? cleanPrompt(savedPrompts.text_keyframes)
            : (savedMode === "text_keyframes" ? legacyPrompt : ""),
        all_reference: savedPrompts && hasOwn(savedPrompts, "all_reference")
            ? cleanPrompt(savedPrompts.all_reference)
            : (savedMode === "all_reference" ? legacyPrompt : ""),
    };
    let optimizing = false;
    let optimizerRequestId = null;
    let optimizerAbort = null;
    let optimizerTimer = null;
    let optimizerCompleteAudio = null;
    const promptWrap = make("div"); promptWrap.className = "mxv-prompt-wrap";
    const promptHighlight = make("div"); promptHighlight.className = "mxv-prompt-highlight";
    const promptHighlightContent = make("div"); promptHighlightContent.className = "mxv-prompt-highlight-content"; promptHighlight.append(promptHighlightContent);
    const promptVideoThumbnailCache = new Map();
    let resolvePromptMedia = () => null;
    const promptTools = make("div"); promptTools.className = "mxv-prompt-tools";
    const resetPrompt = make("button", {}, "↻"); resetPrompt.className = "mxv-prompt-tool mxv-prompt-reset";
    const elapsedPrompt = make("span"); elapsedPrompt.className = "mxv-prompt-elapsed";
    const optimizerModelName = make("span"); optimizerModelName.className = "mxv-optimizer-model";
    const optimizePrompt = make("button", {}, "✦"); optimizePrompt.className = "mxv-prompt-tool mxv-optimize-tool";
    const optimizerGear = make("button", {}, "⚙"); optimizerGear.className = "mxv-prompt-tool";
    promptTools.append(elapsedPrompt, optimizerModelName, resetPrompt, optimizePrompt, optimizerGear); promptWrap.append(promptTools, promptHighlight, prompt);
    const syncPromptHighlightGeometry = () => {
        promptHighlightContent.style.width = `${prompt.clientWidth}px`;
    };
    const requestPromptVideoThumbnail = name => {
        if (!name) return null;
        const cached = promptVideoThumbnailCache.get(name);
        if (typeof cached === "string") return cached;
        if (cached) return null;
        const pending = new Promise(resolve => {
            const video = document.createElement("video");
            video.muted = true; video.playsInline = true; video.preload = "auto"; video.crossOrigin = "anonymous";
            const finish = value => { video.removeAttribute("src"); video.load(); resolve(value); };
            const capture = () => {
                try {
                    const canvas = document.createElement("canvas"); canvas.width = 64; canvas.height = 64;
                    const context = canvas.getContext("2d");
                    const sourceRatio = video.videoWidth / Math.max(1, video.videoHeight);
                    let sx = 0, sy = 0, sw = video.videoWidth, sh = video.videoHeight;
                    if (sourceRatio > 1) { sw = video.videoHeight; sx = (video.videoWidth - sw) / 2; }
                    else { sh = video.videoWidth; sy = (video.videoHeight - sh) / 2; }
                    context.drawImage(video, sx, sy, sw, sh, 0, 0, 64, 64);
                    finish(canvas.toDataURL("image/jpeg", .68));
                } catch { finish(""); }
            };
            video.onloadeddata = () => {
                if (video.duration > .04 && video.currentTime < .01) {
                    video.onseeked = capture;
                    try { video.currentTime = Math.min(.05, video.duration / 2); } catch { capture(); }
                } else capture();
            };
            video.onerror = () => finish("");
            video.src = fileUrl(name);
        });
        promptVideoThumbnailCache.set(name, pending);
        pending.then(value => {
            promptVideoThumbnailCache.set(name, value || "");
            renderPromptHighlights();
        });
        return null;
    };
    const promptMediaMatches = source => {
        const pattern = /<\s*(Picture|Image|Video|Audio|图片|图像|图|视频|音频)\s*#?\s*(\d+)\s*>|(?<![\w<])(Picture|Image|Video|Audio)\s*#?\s*(\d+)\b(?!\s*>)|(图片|图像|图|视频|音频)\s*#?\s*(\d+)/gi;
        const matches = [];
        let item;
        while ((item = pattern.exec(source))) {
            const label = item[1] || item[3] || item[5] || "";
            const rawType = label.toLowerCase();
            const type = ["picture", "image", "图片", "图像", "图"].includes(rawType) ? "picture"
                : ["video", "视频"].includes(rawType) ? "video"
                : "audio";
            const ordinal = Number(item[2] || item[4] || item[6]);
            // H3 media ordinals start at 1. Excluding zero also prevents time
            // phrases such as “视频0.00秒” from decorating “视频0” as a tag.
            if (ordinal >= 1) matches.push({ index: item.index, raw: item[0], type, ordinal });
            if (!item[0].length) pattern.lastIndex++;
        }
        return matches;
    };
    const promptTagMatches = source => {
        const mediaMatches = promptMediaMatches(source);
        const overlapsMedia = (start, end) => mediaMatches.some(item =>
            start < item.index + item.raw.length && end > item.index
        );
        const matches = [];
        const collect = pattern => {
            let item;
            while ((item = pattern.exec(source))) {
                const start = item.index, end = start + item[0].length;
                if (!overlapsMedia(start, end)) matches.push({ index: start, end, raw: item[0], kind: "tag" });
                if (!item[0].length) pattern.lastIndex++;
            }
        };
        // Non-media H3 tags must retain their explicit angle/square brackets.
        // This covers subjects, shots, scenes, dialogue/language markers and
        // official compound task tags without styling ordinary prose.
        collect(/<\/?[A-Za-z\u4e00-\u9fff][^<>\r\n]{0,80}>/g);
        collect(/\[[A-Za-z\u4e00-\u9fff][^\[\]\r\n]{0,80}\]/g);
        collect(/(?<![A-Za-z0-9_])\(S(?:[1-9]|1\d|20)\)(?![A-Za-z0-9_])/gi);
        return matches.sort((a, b) => a.index - b.index || b.end - a.end);
    };
    const promptDecorations = source => {
        const decorations = promptMediaMatches(source).map(item => ({
            ...item, end: item.index + item.raw.length, kind: "media",
        }));
        decorations.push(...promptTagMatches(source));
        const sectionPattern = /^(?:subject_definitions|integrated_multimodal_description|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music)\s*[:：]/gim;
        let section;
        while ((section = sectionPattern.exec(source))) {
            decorations.push({ index: section.index, end: section.index + section[0].length, raw: section[0], kind: "section" });
            if (!section[0].length) sectionPattern.lastIndex++;
        }
        const dialoguePattern = /<d>\s*\[[^\]\r\n]+\]([\s\S]*?)<\/d>/gi;
        let item;
        while ((item = dialoguePattern.exec(source))) {
            const body = item[1] || "";
            const index = item.index + item[0].indexOf(body);
            if (body) decorations.push({ index, end: index + body.length, raw: body, kind: "dialogue" });
            if (!item[0].length) dialoguePattern.lastIndex++;
        }
        const priority = { media: 4, tag: 3, section: 2, dialogue: 1 };
        return decorations.sort((a, b) => a.index - b.index || priority[b.kind] - priority[a.kind] || b.end - a.end);
    };
    const renderPromptHighlights = () => {
        const source = String(prompt.value || "");
        const keepFocus = document.activeElement === prompt || prompt.dataset.mxvEditing === "1";
        const restoreSelection = keepFocus ? selectionOffsets() : null;
        prompt.replaceChildren();
        const appendPlain = text => {
            const parts = text.split("\n");
            parts.forEach((part, index) => {
                if (part) prompt.append(document.createTextNode(part));
                if (index < parts.length - 1) prompt.append(document.createElement("br"));
            });
        };
        let cursor = 0;
        for (const decoration of promptDecorations(source)) {
            if (decoration.index < cursor) continue;
            if (decoration.index > cursor) appendPlain(source.slice(cursor, decoration.index));
            if (decoration.kind === "dialogue") {
                const mark = make("mark"); mark.textContent = decoration.raw; prompt.append(mark);
                cursor = decoration.end;
                continue;
            }
            if (decoration.kind === "section") {
                const section = make("span"); section.className = "mxv-prompt-section"; section.textContent = decoration.raw;
                prompt.append(section);
                cursor = decoration.end;
                continue;
            }
            if (decoration.kind === "tag") {
                const tag = make("span"); tag.className = "mxv-prompt-tag"; tag.textContent = decoration.raw;
                prompt.append(tag);
                cursor = decoration.end;
                continue;
            }
            const { type, ordinal } = decoration;
            const token = make("span"); token.className = "mxv-prompt-media-token";
            const mediaEntry = resolvePromptMedia(type, ordinal);
            token.dataset.mxvMediaSignature = `${type}:${ordinal}:${mediaEntry?.name || ""}`;
            if (mediaEntry) {
                let preview;
                if (type === "audio") {
                    preview = make("span"); preview.className = "mxv-prompt-media-preview mxv-prompt-audio-preview";
                } else {
                    preview = make("img"); preview.className = "mxv-prompt-media-preview";
                    if (type === "video") {
                        const thumbnail = requestPromptVideoThumbnail(mediaEntry?.name);
                        if (thumbnail) preview.src = thumbnail;
                    } else if (mediaEntry?.name) preview.src = fileUrl(mediaEntry.name);
                }
                preview.contentEditable = "false";
                token.appendChild(preview);
            }
            const tag = make("span");
            tag.className = `mxv-prompt-tag mxv-prompt-tag-${type}`;
            tag.textContent = decoration.raw;
            token.appendChild(tag);
            prompt.appendChild(token);
            cursor = decoration.end;
        }
        if (cursor < source.length) appendPlain(source.slice(cursor));
        // A contenteditable ending in one BR has inconsistent caret behaviour
        // across Chromium builds: some place the caret visually before it and
        // swallow the first Enter.  A non-data trailing BR gives the native
        // caret a stable empty line without becoming part of the prompt text.
        if (source.endsWith("\n")) {
            const sentinel = document.createElement("br");
            sentinel.className = "mxv-prompt-caret-sentinel";
            sentinel.setAttribute("aria-hidden", "true");
            prompt.appendChild(sentinel);
        }
        prompt.classList.toggle("mxv-prompt-empty", !source);
        if (restoreSelection) {
            if (document.activeElement !== prompt) prompt.focus({ preventScroll: true });
            setEditorSelection(restoreSelection[0], restoreSelection[1]);
        }
    };
    const promptDecorationsOutOfSync = source => {
        const expectedMedia = promptMediaMatches(source).map(item => {
            const entry = resolvePromptMedia(item.type, item.ordinal);
            return { raw: item.raw, signature: `${item.type}:${item.ordinal}:${entry?.name || ""}` };
        });
        const renderedMedia = [...prompt.querySelectorAll(".mxv-prompt-media-token")].map(item => ({
            raw: domText(item),
            signature: item.dataset.mxvMediaSignature || "",
        }));
        if (expectedMedia.length !== renderedMedia.length || expectedMedia.some((item, index) =>
            item.raw !== renderedMedia[index]?.raw || item.signature !== renderedMedia[index]?.signature
        )) return true;
        const expectedTags = promptTagMatches(source).map(item => item.raw);
        const renderedTags = [...prompt.querySelectorAll(".mxv-prompt-tag")]
            .filter(item => !item.closest(".mxv-prompt-media-token"))
            .map(item => domText(item));
        if (expectedTags.length !== renderedTags.length || expectedTags.some((item, index) => item !== renderedTags[index])) return true;
        const expectedSections = [...source.matchAll(/^(?:subject_definitions|integrated_multimodal_description|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music)\s*[:：]/gim)].map(item => item[0]);
        const renderedSections = [...prompt.querySelectorAll(".mxv-prompt-section")].map(item => domText(item));
        if (expectedSections.length !== renderedSections.length || expectedSections.some((item, index) => item !== renderedSections[index])) return true;
        const expectedDialogue = [...source.matchAll(/<d>\s*\[[^\]\r\n]+\]([\s\S]*?)<\/d>/gi)].map(item => item[1] || "");
        const renderedDialogue = [...prompt.querySelectorAll("mark")].map(item => domText(item));
        return expectedDialogue.length !== renderedDialogue.length || expectedDialogue.some((item, index) => item !== renderedDialogue[index]);
    };
    const promptHighlightResizeObserver = new ResizeObserver(() => {
        syncPromptHighlightGeometry();
    });
    promptHighlightResizeObserver.observe(prompt);
    renderPromptHighlights();
    if (optimizerBefore != null) resetPrompt.classList.add("visible");
    const configuredOptimizerName = () => {
        if (!optimizerSettings) return "";
        if (optimizerSettings.mode === "local") {
            const name = String(optimizerSettings.local_model || "").split(/[\\/]/).pop();
            return name ? `本地：${name}` : "";
        }
        const rawName = String(optimizerSettings.model || "").trim().split("/").pop();
        const name = rawName
            .replace(/^gpt(?=[-_.\d])/i, "GPT")
            .replace(/^grok(?=[-_.\d])/i, "Grok")
            .replace(/^gemini(?=[-_.\d])/i, "Gemini")
            .replace(/^qwen(?=[-_.\d])/i, "Qwen")
            .replace(/^claude(?=[-_.\d])/i, "Claude")
            .replace(/^deepseek(?=[-_.\d])/i, "DeepSeek")
            .replace(/^doubao(?=[-_.\d])/i, "Doubao")
            .replace(/^minimax(?=[-_.\d])/i, "MiniMax");
        const providerName = ({
            runninghub: "RunningHub 国内版", runninghub_overseas: "RunningHub 海外版", openai: "OpenAI", gemini: "Google Gemini",
            openrouter: "OpenRouter", dashscope: "阿里云百炼", siliconflow: "SiliconFlow",
            custom: "API",
        })[optimizerSettings.provider] || "API";
        return name ? `${providerName}: ${name}` : "";
    };
    const refreshOptimizerName = () => {
        const text = configuredOptimizerName();
        optimizerModelName.textContent = text;
        optimizerModelName.removeAttribute("title");
    };
    refreshOptimizerName();
    let wheelBoundaryDirection = 0;
    let wheelBoundaryUntil = 0;
    let wheelBoundaryReleased = false;
    const advanced = make("details"); advanced.className = "mxv-advanced";
    const advancedSummary = make("summary", { cursor: "pointer", color: "#b3b1ac", padding: "7px 26px 8px 30px", minHeight: "20px", lineHeight: "20px", boxSizing: "border-box", position: "relative" }, t("Advanced options"));
    advanced.appendChild(advancedSummary);
    const advancedBody = make("div", { display: "grid", gap: "5px", padding: "5px 16px 8px" }); advancedBody.className = "mxv-advanced-body";
    advanced.appendChild(advancedBody);
function nodeColorToCss(value) {
        if (typeof value === "string" && value.trim()) return value.trim();
        if (Array.isArray(value) && value.length >= 3) {
            const values = value.slice(0, 4).map(Number);
            if (values.slice(0, 3).every(Number.isFinite)) {
                const normalized = values.slice(0, 3).every(v => v >= 0 && v <= 1);
                const rgb = values.slice(0, 3).map(v => Math.max(0, Math.min(255, Math.round(normalized ? v * 255 : v))));
                const alpha = Number.isFinite(values[3]) ? Math.max(0, Math.min(1, values[3])) : 1;
                return `rgba(${rgb.join(",")},${alpha})`;
            }
        }
        if (Number.isFinite(value)) {
            const hex = Math.max(0, Math.min(0xffffff, Math.trunc(value))).toString(16).padStart(6, "0");
            return `#${hex}`;
        }
        return "";
    }
    function advancedBackgroundColor() {
        const color = nodeColorToCss(node.bgcolor)
            || nodeColorToCss(node.color)
            || nodeColorToCss(node.properties?.bgcolor)
            || nodeColorToCss(node.properties?.color)
            || nodeColorToCss(node._bgcolor)
            || nodeColorToCss(node._color)
            || "#22262c";
        return color;
    }
    function syncAdvancedBackground(force = false) {
        const color = advancedBackgroundColor();
        if (!force && color === root.dataset.mxvNodeBg) return;
        root.dataset.mxvNodeBg = color;
        root.style.setProperty("--mxv-node-bg", color);
    }
    syncAdvancedBackground(true);
    const advancedLabels = new Map();
    const advancedRows = new Map();
    const addAdvanced = (name, label, control) => { const row = make("div"); row.className = "mxv-advanced-row"; const labelEl = make("label", {}, t(label)); labelEl.dataset.mxvTranslation = label; row.appendChild(labelEl); row.appendChild(control); advancedBody.appendChild(row); advancedLabels.set(name, labelEl); advancedRows.set(name, row); };
    const localizedSelects = [];
    const select = (name, values) => {
        const s = document.createElement("select"); s.className = "mxv-control";
        const syncOptions = () => {
            const current = s.value || widget(node, name)?.value || values[0];
            s.replaceChildren(...values.map(v => new Option(t(v), v)));
            s.value = values.includes(current) ? current : values[0];
        };
        syncOptions();
        s.onchange = () => setWidget(node, name, s.value);
        s._mxvSyncOptions = syncOptions;
        localizedSelects.push(s);
        return s;
    };
    const number = (name, step, min, max) => { const wrap = make("div"); wrap.className = "mxv-number"; const left = make("button", {}, "◀"); const n = document.createElement("input"); n.type = "number"; n.step = step; n.min = min; n.max = max; const raw = Number(widget(node, name)?.value); n.value = Number.isFinite(raw) ? raw : min; const right = make("button", {}, "▶"); const update = value => { const numeric = Number(value); const next = Math.max(Number(min), Math.min(Number(max), Number.isFinite(numeric) ? numeric : Number(min))); n.value = Number.isInteger(next) ? next : next.toFixed(2).replace(/0+$/, "").replace(/\.$/, ""); setWidget(node, name, next); }; left.onclick = () => update(Number(n.value || min) - Number(step)); right.onclick = () => update(Number(n.value || min) + Number(step)); n.onchange = () => update(n.value); wrap.append(left, n, right); return wrap; };
    const check = (name) => { const label = make("label"); label.className = "mxv-toggle"; const c = document.createElement("input"); c.type = "checkbox"; c.checked = !!widget(node, name)?.value; const track = make("span"); c.onchange = () => setWidget(node, name, c.checked); label.append(c, track); return label; };
    addAdvanced("audio_mode", "Audio mode", select("audio_mode", ["native", "lock_source", "reference_only", "remix_source"]));
    const audioStrengthControl = number("audio_denoise_strength", "0.05", "0", "1");
    addAdvanced("audio_denoise_strength", "Audio redraw strength", audioStrengthControl);
    const driveAudioOrdinalControl = document.createElement("select");
    driveAudioOrdinalControl.className = "mxv-control";
    addAdvanced("drive_audio_ordinal", "Drive audio", driveAudioOrdinalControl);
    addAdvanced("strict_prompt_tags", "Strict prompt tags", check("strict_prompt_tags"));
    addAdvanced("ref_image_size", "Reference image size", select("ref_image_size", ["match", "1.2x", "1.5x", "2x", "max"]));
    const noSubtitleControl = check("no_subtitle");
    noSubtitleControl.title = t("Injects a positive no-subtitle constraint after [Shot 1]: speech exists only as audible voice with lip sync and never appears as on-screen text. Disable to strip it from the prompt");
    addAdvanced("no_subtitle", "No-subtitle constraint", noSubtitleControl);
    const soundscapeControl = check("soundscape");
    soundscapeControl.title = t("On: rewrite an N/A overall_soundscape with a default ambient description; the rest of the prompt stays untouched. Off: force N/A for a fully silent bed");
    addAdvanced("soundscape", "Soundscape", soundscapeControl);
    const musicControl = check("music");
    musicControl.title = t("On: rewrite an N/A non_diegetic_music with a default score description; the rest of the prompt stays untouched. Off: force N/A (no score)");
    addAdvanced("music", "Music", musicControl);
    /* ===== 采样参数组（单节点整合新增，原生 widget 已隐藏，此处提供等价控件）===== */
    const stepsControl = number("steps", "1", "1", "1000");
    addAdvanced("steps", "Steps", stepsControl);
    const shiftVideoControl = number("shift_video", "0.01", "0.01", "100");
    addAdvanced("shift_video", "Video shift", shiftVideoControl);
    const shiftAudioControl = number("shift_audio", "0.01", "0.01", "100");
    addAdvanced("shift_audio", "Audio shift", shiftAudioControl);
    addAdvanced("sampler_name", "Sampler", select("sampler_name", SAMPLER_OPTIONS));
    addAdvanced("scheduler", "Scheduler", select("scheduler", SCHEDULER_OPTIONS));
    const seedControl = number("noise_seed", "1", "0", "0xffffffff");
    addAdvanced("noise_seed", "Seed", seedControl);
    const audioModeWidget = widget(node, "audio_mode");
    const audioModeByMode = savedState.audioModeByMode && typeof savedState.audioModeByMode === "object"
        ? { ...savedState.audioModeByMode } : {};
    const audioModeAutoByMode = savedState.audioModeAutoByMode && typeof savedState.audioModeAutoByMode === "object"
        ? { ...savedState.audioModeAutoByMode } : {};
    for (const mode of ["text_keyframes", "all_reference"]) {
        if (audioModeByMode[mode] != null && audioModeAutoByMode[mode] == null) {
            audioModeAutoByMode[mode] = false;
        }
    }
    let audioStrengthManual = false;
    const driveAudioEntries = () => {
        const videos = [...media.entries()]
            .filter(([slot, entry]) => entry?.kind === "video" && !entry.muted)
            .sort((a, b) => a[0].localeCompare(b[0]));
        const audios = [...media.entries()]
            .filter(([slot, entry]) => entry?.kind === "audio" && slot !== "hybrid_audio")
            .sort((a, b) => a[0].localeCompare(b[0]));
        return [
            ...videos.map(([slot, entry], index) => ({ ordinal: index + 1, name: entry.name, slot, entry })),
            ...audios.map(([slot, entry], index) => ({ ordinal: videos.length + index + 1, name: entry.name, slot, entry })),
        ];
    };
    const captureDriveAudioSelection = () => {
        const ordinal = Number(widget(node, "drive_audio_ordinal")?.value || 0);
        const selected = ordinal > 0 ? driveAudioEntries().find(item => item.ordinal === ordinal) : null;
        return {
            ordinal,
            entry: selected?.entry || null,
            slot: selected?.slot || null,
        };
    };
    const restoreDriveAudioSelection = (selection, preserveReplacementSlot = false) => {
        const entries = driveAudioEntries();
        const selected = selection?.ordinal > 0
            ? entries.find(item => item.entry === selection.entry)
                || (preserveReplacementSlot ? entries.find(item => item.slot === selection.slot) : null)
            : null;
        const ordinal = selected?.ordinal || 0;
        setWidget(node, "drive_audio_ordinal", ordinal);
        driveAudioOrdinalControl.value = String(ordinal);
        return ordinal;
    };
    const allReferenceAudioCount = () => driveAudioEntries().length;
    const selectFirstDriveAudioForOriginalMode = () => {
        if (state.mode !== "all_reference") return 0;
        if ((widget(node, "audio_mode")?.value || "native") !== "lock_source") return 0;
        const current = Number(widget(node, "drive_audio_ordinal")?.value || 0);
        if (current > 0) return current;
        const first = driveAudioEntries()[0];
        if (!first) return 0;
        setWidget(node, "drive_audio_ordinal", first.ordinal);
        driveAudioOrdinalControl.value = String(first.ordinal);
        return first.ordinal;
    };
    const hasModeAudio = () => state.mode === "text_keyframes"
        ? media.has("hybrid_audio")
        : allReferenceAudioCount() > 0 && Number(widget(node, "drive_audio_ordinal")?.value || 0) > 0;
    const syncAudioModeDefault = () => {
        const mode = state.mode;
        if (audioModeAutoByMode[mode] !== false) {
            if (mode === "all_reference") {
                const count = allReferenceAudioCount();
                audioModeByMode[mode] = count === 0
                    ? "native"
                    : count === 1 ? "lock_source" : "reference_only";
                const driveOrdinal = count === 0 ? 0 : 1;
                setWidget(node, "drive_audio_ordinal", driveOrdinal);
                driveAudioOrdinalControl.value = String(driveOrdinal);
            } else {
                audioModeByMode[mode] = hasModeAudio() ? "lock_source" : "native";
            }
        }
        setWidget(node, "audio_mode", audioModeByMode[mode] || "native");
        const control = advancedRows.get("audio_mode")?.querySelector("select");
        if (control) control.value = widget(node, "audio_mode")?.value;
        // Original-audio output requires a drive track. If the mode was
        // entered manually or automatically while "None" was selected, use
        // Audio 1 as the initial choice without locking the selector; users
        // can still choose any other available drive audio afterwards.
        selectFirstDriveAudioForOriginalMode();
        if (!audioStrengthManual) {
            const modeValue = widget(node, "audio_mode")?.value || "native";
            const strength = modeValue === "lock_source" ? 0 : 1;
            setWidget(node, "audio_denoise_strength", strength);
            const input = audioStrengthControl.querySelector("input");
            if (input) input.value = strength;
        }
    };
    const syncAudioModeAfterMediaChange = (slot, previousAudioCount = null) => {
        const affectsAudio = state.mode === "text_keyframes"
            ? slot === "hybrid_audio"
            : slot?.startsWith("ref_audio_") || slot?.startsWith("ref_video_");
        if (!affectsAudio) return;
        if (
            state.mode === "all_reference"
            && previousAudioCount != null
            && previousAudioCount === allReferenceAudioCount()
        ) return;
        const before = Number(previousAudioCount ?? 0);
        const after = state.mode === "text_keyframes"
            ? (media.has("hybrid_audio") ? 1 : 0)
            : allReferenceAudioCount();
        if (after === 0) {
            audioModeAutoByMode[state.mode] = true;
            audioModeByMode[state.mode] = "native";
            syncAudioModeDefault();
            return;
        }
        // Only the first transition from no reference audio to audio present
        // gets the automatic original-audio selection. Subsequent uploads,
        // replacements, and workflow redraws preserve the user's choice.
        if (before === 0 && after > 0) {
            audioModeAutoByMode[state.mode] = true;
            syncAudioModeDefault();
        }
    };
    const updateAdvancedVisibility = (preserveAudioSettings = false) => {
        if (state.mode === "all_reference") {
            const count = allReferenceAudioCount();
            const current = Number(widget(node, "drive_audio_ordinal")?.value || 0);
            const entries = driveAudioEntries();
            const next = count === 0 || current === 0 ? 0 : entries.some(entry => entry.ordinal === current) ? current : 1;
            setWidget(node, "drive_audio_ordinal", next);
            driveAudioOrdinalControl.replaceChildren(
                new Option(t("None"), "0"),
                ...entries.slice(0, 6).map(entry => new Option(`<audio ${entry.ordinal}>${entry.name}`, String(entry.ordinal))),
            );
            driveAudioOrdinalControl.value = String(next);
        }
        const visible = state.mode === "text_keyframes"
            ? ["audio_mode", "audio_denoise_strength", "strict_prompt_tags", "no_subtitle", "soundscape", "music"]
            : ["audio_mode", "audio_denoise_strength", "ref_image_size", "strict_prompt_tags", "drive_audio_ordinal", "no_subtitle", "soundscape", "music"];
        for (const [name, row] of advancedRows) {
            const isVisible = visible.includes(name);
            row.hidden = !isVisible;
            row.style.display = isVisible ? "grid" : "none";
        }
        if (!preserveAudioSettings) syncAudioModeDefault();
    };
    advancedRows.get("audio_mode")?.querySelector("select")?.addEventListener("change", event => {
        audioModeByMode[state.mode] = event.target.value;
        audioModeAutoByMode[state.mode] = false;
        audioStrengthManual = false;
        syncAudioModeDefault();
    });
    audioStrengthControl.querySelector("input")?.addEventListener("input", () => { audioStrengthManual = true; });
    driveAudioOrdinalControl.addEventListener("change", () => {
        setWidget(node, "drive_audio_ordinal", Number(driveAudioOrdinalControl.value || 0));
        if (state.mode === "all_reference" && Number(driveAudioOrdinalControl.value || 0) === 0) {
            audioModeAutoByMode[state.mode] = true;
            audioModeByMode[state.mode] = "native";
            setWidget(node, "audio_mode", "native");
            const control = advancedRows.get("audio_mode")?.querySelector("select");
            if (control) control.value = "native";
            return;
        }
        if (audioModeAutoByMode[state.mode] !== false) syncAudioModeDefault();
    });
    function applyLocale() {
        taskStatus.textContent = `${resolvedTaskType()} · ${taskLabel(resolvedTaskType())}`;
        modeText.textContent = t("First/last frames / Text-to-video");
        modeRef.textContent = t("All-purpose reference");
        prompt.placeholder = t("Prompt:\nClick an uploaded asset to insert its tag, e.g. <picture 1>, <video 1>, or <audio 2>;\nDouble-click a video to insert its audio tag; mute a video at the top-right to exclude its audio from references");
        advancedSummary.textContent = t("Advanced options");
        for (const label of advancedLabels.values()) label.textContent = t(label.dataset.mxvTranslation);
        localizedSelects.forEach(control => control._mxvSyncOptions?.());
        for (const row of aiRows.values()) {
            const label = row.querySelector("label[data-mxv-translation]");
            if (label) label.textContent = t(label.dataset.mxvTranslation);
        }
        aiText.placeholder = t("Idea / prompt to rewrite");
        aiKey.placeholder = t("API key (cleared when sharing the workflow)");
        aiEndpoint.placeholder = t("endpoint override");
        aiModel.placeholder = t("model override");
        syncAiRows();
        updateAdvancedVisibility();
        render();
    }
    // Read locale at node initialization. A storage event can still refresh
    // the UI after a real settings change, but no periodic polling is needed.
    window.addEventListener("storage", applyLocale);
    advanced.addEventListener("toggle", () => { persistState(); syncLayout(false, true); });
    root.appendChild(promptWrap);
    /* ===== AI 生成区块：接管隐藏的 prompt_source/ai_* widget（与隐藏列表一一对应）===== */
    const aiDetails = make("details"); aiDetails.className = "mxv-advanced mxv-ai";
    const aiSummary = make("summary", { cursor: "pointer", color: "#b3b1ac", padding: "7px 26px 7px 30px", minHeight: "20px", lineHeight: "20px", boxSizing: "border-box", position: "relative" }, t("AI generation"));
    aiDetails.appendChild(aiSummary);
    const aiBody = make("div", { display: "grid", gap: "5px", padding: "5px 12px 8px" }); aiBody.className = "mxv-advanced-body";
    aiDetails.appendChild(aiBody);
    const aiRows = new Map();
    const addAi = (name, label, control) => {
        const row = make("div"); row.className = "mxv-advanced-row";
        const labelEl = make("label", {}, t(label)); labelEl.dataset.mxvTranslation = label;
        row.appendChild(labelEl); row.appendChild(control);
        aiBody.appendChild(row); aiRows.set(name, row);
    };
    const aiSelect = select;
    const aiCheck = (name) => {
        const label = make("label"); label.className = "mxv-toggle";
        const c = document.createElement("input"); c.type = "checkbox"; c.checked = !!widget(node, name)?.value;
        const track = make("span"); c.onchange = () => setWidget(node, name, c.checked);
        label.append(c, track); return label;
    };
    const aiText = document.createElement("textarea"); aiText.className = "mxv-control";
    aiText.rows = 3; aiText.placeholder = t("Idea / prompt to rewrite");
    aiText.style.width = "100%"; aiText.style.boxSizing = "border-box"; aiText.style.resize = "vertical";
    aiText.value = String(widget(node, "ai_text")?.value ?? "");
    aiText.oninput = () => setWidget(node, "ai_text", aiText.value);
    const aiKey = document.createElement("input"); aiKey.className = "mxv-control"; aiKey.type = "password";
    aiKey.style.width = "100%"; aiKey.style.boxSizing = "border-box";
    aiKey.placeholder = t("API key (cleared when sharing the workflow)");
    aiKey.value = String(widget(node, "ai_api_key")?.value ?? "");
    aiKey.oninput = () => setWidget(node, "ai_api_key", aiKey.value);
    const aiSource = aiSelect("prompt_source", ["panel", "ai", "offline", "format"]);
    const AI_PROVIDERS = ["custom", "dashscope", "deepseek", "glm", "openai", "openrouter", "siliconflow"]; // keep in sync with llm_client.PROVIDERS
    const aiProvider = aiSelect("ai_provider", AI_PROVIDERS);
    const aiLanguage = aiSelect("ai_language", ["zh", "mixed", "en"]);
    const aiMode = aiSelect("ai_mode", ["auto", "t2va", "i2va", "fl2va", "l2va", "ref2va"]);
    aiMode.onchange = () => {
        setWidget(node, "ai_mode", aiMode.value);
        switchMode(aiMode.value === "ref2va" ? "all_reference" : aiMode.value === "auto" ? state.mode : "text_keyframes", true);
    };
    const aiEndpoint = document.createElement("input"); aiEndpoint.className = "mxv-control"; aiEndpoint.type = "text";
    aiEndpoint.style.width = "100%"; aiEndpoint.style.boxSizing = "border-box";
    aiEndpoint.placeholder = t("endpoint override");
    aiEndpoint.value = String(widget(node, "ai_endpoint")?.value ?? "");
    aiEndpoint.oninput = () => setWidget(node, "ai_endpoint", aiEndpoint.value);
    const aiModel = document.createElement("input"); aiModel.className = "mxv-control"; aiModel.type = "text";
    aiModel.style.width = "100%"; aiModel.style.boxSizing = "border-box";
    aiModel.placeholder = t("model override");
    aiModel.value = String(widget(node, "ai_model")?.value ?? "");
    aiModel.oninput = () => setWidget(node, "ai_model", aiModel.value);
    addAi("prompt_source", "Source", aiSource);
    addAi("ai_text", "Idea / prompt to rewrite", aiText);
    addAi("ai_language", "Output language", aiLanguage);
    addAi("ai_mode", "H3 mode", aiMode);
    addAi("ai_provider", "Provider", aiProvider);
    addAi("ai_api_key", "API key", aiKey);
    addAi("ai_endpoint", "Endpoint override", aiEndpoint);
    addAi("ai_model", "Model override", aiModel);
    addAi("ai_enrich", "Bounded enrichment", aiCheck("ai_enrich"));
    addAi("ai_soundscape", "Soundscape", aiCheck("ai_soundscape"));
    addAi("ai_music", "Music", aiCheck("ai_music"));
    addAi("ai_auto_timestamps", "Auto timestamps", aiCheck("ai_auto_timestamps"));
    addAi("ai_fixed_camera", "Fixed camera", aiCheck("ai_fixed_camera"));
    addAi("ai_visual_stability", "Visual stability", aiCheck("ai_visual_stability"));
    addAi("ai_no_subtitles", "No-subtitle constraint", aiCheck("ai_no_subtitles"));
    addAi("ai_anti_pop", "Anti-pop inline dialogue", aiCheck("ai_anti_pop"));
    addAi("ai_strict_validation", "Strict validation", aiCheck("ai_strict_validation"));
    addAi("ai_timeout", "AI timeout (seconds)", number("ai_timeout", "1", "15", "600"));
    /* 来源联动：panel 时 ai_text 及全部生成参数仅保留来源可见（隐藏其余行）；
       custom 供应商才显示端点/模型覆盖；Key 行仅在 ai 来源显示。 */
    const aiPanelRows = ["ai_text", "ai_language", "ai_mode", "ai_provider", "ai_api_key",
        "ai_endpoint", "ai_model", "ai_timeout", "ai_enrich", "ai_soundscape", "ai_music",
        "ai_auto_timestamps", "ai_fixed_camera", "ai_visual_stability",
        "ai_no_subtitles", "ai_anti_pop", "ai_strict_validation"];
    const syncAiRows = () => {
        const source = String(aiSource.value || "panel");
        const custom = source === "ai" && aiProvider.value === "custom";
        for (const name of aiPanelRows) {
            const row = aiRows.get(name);
            if (!row) continue;
            let show;
            if (source === "panel") {
                show = false; // panel：使用提示词编辑器内容，AI 参数整组收起
            } else if (name === "ai_endpoint" || name === "ai_model") {
                show = custom; // 端点/模型覆盖仅自定义供应商需要
            } else if (source === "ai") {
                show = true;
            } else {
                // offline/format：不需要创意输入与生成开关，只保留语言/模式/校验等
                show = ["ai_language", "ai_mode", "ai_strict_validation"].includes(name);
            }
            row.style.display = show ? "" : "none";
        }
        aiSummary.textContent = `${t("AI generation")} — ${t("Source")}: ${t(source)}`;
        // syncLayout/measureContentHeight 定义在面板装配后期；此函数在装配早期
        // 就会被首次调用（TDZ 限制），故延迟到下一帧执行。
        if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => syncLayout(false, true));
    };
    aiSource.onchange = () => { setWidget(node, "prompt_source", aiSource.value); syncAiRows(); };
    aiProvider.onchange = () => { setWidget(node, "ai_provider", aiProvider.value); syncAiRows(); };
    syncAiRows();
    aiDetails.addEventListener("toggle", () => { syncLayout(false, true); });
    const syncAiControls = () => {
        for (const [name, row] of aiRows) {
            const control = row.querySelector("input, select, textarea");
            const value = widget(node, name)?.value;
            if (!control || value == null) continue;
            if (control.type === "checkbox") control.checked = !!value;
            else control.value = String(value);
        }
        syncAiRows();
    };
    root.appendChild(aiDetails);
    root.appendChild(advanced);
    const commitPromptEditorInput = () => {
        const keepFocus = document.activeElement === prompt || prompt.dataset.mxvEditing === "1";
        promptPlainText = editorText();
        // Keep native contenteditable editing intact for ordinary keystrokes.
        // Rebuild only when a media/dialogue decoration actually changed.
        if (promptDecorationsOutOfSync(promptPlainText)) renderPromptHighlights();
        if (keepFocus && document.activeElement !== prompt) prompt.focus({ preventScroll: true });
        prompt.classList.toggle("mxv-prompt-empty", !promptPlainText);
        if (pendingPromptSnapshot) {
            pushPromptUndo(pendingPromptSnapshot);
            pendingPromptSnapshot = null;
        }
        promptByMode[state.mode] = prompt.value; setPromptWidget(node, prompt.value); persistState();
    };
    prompt.addEventListener("compositionstart", () => { pendingPromptSnapshot ||= promptSnapshot(); promptComposing = true; });
    prompt.addEventListener("compositionend", () => { promptComposing = false; commitPromptEditorInput(); });
    prompt.addEventListener("input", () => { if (!promptComposing) commitPromptEditorInput(); });
    prompt.addEventListener("beforeinput", event => {
        if (prompt.readOnly) return;
        if (["historyUndo", "historyRedo"].includes(event.inputType)) {
            // Ctrl+Z/Y is handled by the editor's private text history. Block
            // the browser's delayed native history event so it cannot bubble
            // into LiteGraph/ComfyUI and refresh the whole canvas.
            event.preventDefault(); event.stopImmediatePropagation();
            return;
        }
        pendingPromptSnapshot ||= promptSnapshot();
        if (!["insertParagraph", "insertLineBreak"].includes(event.inputType)) return;
        event.preventDefault();
        // Chromium can report an explicit paragraph insertion just before its
        // delayed compositionend event.  Do not discard that first Enter;
        // first capture the already committed DOM text, then add the newline.
        const [start, end] = selectionOffsets();
        promptPlainText = editorText();
        prompt.setRangeText("\n", start, end, "end");
        pushPromptUndo(pendingPromptSnapshot); pendingPromptSnapshot = null;
        promptByMode[state.mode] = prompt.value; setPromptWidget(node, prompt.value); persistState();
    });
    const isPromptHistoryKey = event => {
        const editing = document.activeElement === prompt || prompt.dataset.mxvEditing === "1";
        if (!editing || prompt.readOnly || !(event.ctrlKey || event.metaKey) || event.altKey) return;
        const key = String(event.key || "").toLowerCase();
        return key === "z" || key === "y";
    };
    const capturePromptHistoryKeys = event => {
        if (!isPromptHistoryKey(event)) return;
        event.preventDefault(); event.stopImmediatePropagation();
        const key = String(event.key || "").toLowerCase();
        stepPromptHistory(key === "y" || (key === "z" && event.shiftKey));
    };
    const releasePromptHistoryKeys = event => {
        if (!isPromptHistoryKey(event)) return;
        event.preventDefault(); event.stopImmediatePropagation();
    };
    window.addEventListener("keydown", capturePromptHistoryKeys, true);
    window.addEventListener("keyup", releasePromptHistoryKeys, true);
    function upstreamConnected() {
        const input = node.inputs?.find(item => item.name === "prompt_override");
        return input?.link != null;
    }
    function refreshPromptConnection() {
        const external = upstreamConnected();
        const localBlocked = workflowRunning && optimizerSettings?.mode === "local";
        prompt.readOnly = external;
        promptWrap.classList.toggle("external", external);
        prompt.title = external ? t("Prompt is connected to an upstream node; the internal prompt is disabled!") : "";
        optimizePrompt.disabled = external || localBlocked;
        resetPrompt.disabled = external;
    }
    const updateWorkflowState = running => {
        refreshPromptConnection();
        if (running && optimizing && optimizerSettings?.mode === "local") cancelOptimization();
    };
    workflowStateHandlers.add(updateWorkflowState);
    installWorkflowStateMonitor();
    const oldConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function(...args) {
        const result = oldConnectionsChange?.apply(this, args);
        queueMicrotask(refreshPromptConnection);
        return result;
    };
    function delayedTooltip(button, getText) {
        let timer = null; let tip = null;
        const clear = () => { clearTimeout(timer); timer = null; tip?.remove(); tip = null; };
        button.addEventListener("mouseenter", () => {
            timer = setTimeout(() => {
                tip = make("div", {}, getText()); tip.className = "mxv-tool-tip"; document.body.append(tip);
                const rect = button.getBoundingClientRect();
                const centeredLeft = rect.left + rect.width / 2 - tip.offsetWidth / 2;
                tip.style.left = `${Math.max(4, Math.min(window.innerWidth - tip.offsetWidth - 4, centeredLeft))}px`;
                tip.style.top = `${Math.max(4, rect.top - tip.offsetHeight - 5)}px`;
            }, 1000);
        });
        button.addEventListener("mouseleave", clear); button.addEventListener("pointerdown", clear);
    }
    delayedTooltip(optimizePrompt, () => t(optimizing ? "Optimizing click to cancel" : "Optimize prompt"));
    delayedTooltip(resetPrompt, () => t("Restore before optimization"));
    delayedTooltip(optimizerGear, () => t("Configure API"));
    const stopPromptKeyBubble = event => event.stopPropagation();
    prompt.addEventListener("keydown", stopPromptKeyBubble);
    prompt.addEventListener("keyup", stopPromptKeyBubble);
    prompt.addEventListener("keypress", stopPromptKeyBubble);
    prompt.addEventListener("focus", () => markPromptEditor(prompt, true));
    prompt.addEventListener("pointerdown", e => {
        markPromptEditor(prompt, true);
        if (e.button === 1) return;
        e.stopPropagation();
        if (prompt.readOnly) return;
        queueMicrotask(() => {
            if (document.activeElement !== prompt) prompt.focus({ preventScroll: true });
            app.canvas?.canvas?.blur?.();
        });
    });
    const promptCanConsumeWheel = event => {
        const inPrompt = event.composedPath?.().includes(prompt) || event.target === prompt;
        if (!inPrompt || prompt.scrollHeight <= prompt.clientHeight) return false;
        const atTop = prompt.scrollTop <= 0;
        const atBottom = prompt.scrollTop + prompt.clientHeight >= prompt.scrollHeight - 1;
        return (event.deltaY < 0 && !atTop) || (event.deltaY > 0 && !atBottom);
    };
    const capturePromptWheel = event => {
        if (promptCanConsumeWheel(event)) {
            wheelBoundaryDirection = 0;
            wheelBoundaryUntil = 0;
            wheelBoundaryReleased = false;
            event.preventDefault();
            event.stopImmediatePropagation();
            prompt.scrollTop += event.deltaY;
            return;
        }
        const inPrompt = event.composedPath?.().includes(prompt) || event.target === prompt;
        if (!inPrompt || prompt.scrollHeight <= prompt.clientHeight || !event.deltaY) return;
        const direction = event.deltaY < 0 ? -1 : 1;
        const now = performance.now();
        if (wheelBoundaryDirection !== direction) {
            wheelBoundaryDirection = direction;
            wheelBoundaryUntil = now + 1000;
            wheelBoundaryReleased = false;
        }
        if (!wheelBoundaryReleased && now < wheelBoundaryUntil) {
            event.preventDefault();
            event.stopImmediatePropagation();
            return;
        }
        if (!wheelBoundaryReleased) wheelBoundaryReleased = true;
    };
    window.addEventListener("wheel", capturePromptWheel, { capture: true, passive: false });
    root.addEventListener("wheel", event => {
        // Forward the native delta unchanged when the prompt cannot consume
        // the event, including when it is at the top or bottom edge.
        if (promptCanConsumeWheel(event)) return;
        if (event.defaultPrevented) return;
        const canvas = app.canvas;
        if (canvas?.processMouseWheel) {
            event.preventDefault();
            canvas.processMouseWheel(event);
        }
    }, { capture: true, passive: false });
    let middlePan = null;
    const moveMiddlePan = event => {
        if (!middlePan) return;
        const canvas = app.canvas;
        const scale = Number(canvas?.ds?.scale) || 1;
        const dx = (event.clientX - middlePan.x) / scale;
        const dy = (event.clientY - middlePan.y) / scale;
        middlePan.x = event.clientX;
        middlePan.y = event.clientY;
        if (canvas?.ds?.offset) {
            canvas.ds.offset[0] += dx;
            canvas.ds.offset[1] += dy;
            canvas.setDirty?.(true, true);
            node.setDirtyCanvas(true, true);
        }
        event.preventDefault();
        event.stopImmediatePropagation();
    };
    const stopMiddlePan = event => {
        if (!middlePan || (event.type !== "blur" && event.button !== 1)) return;
        middlePan = null;
        document.body.style.removeProperty("cursor");
        window.removeEventListener("pointermove", moveMiddlePan, true);
        window.removeEventListener("pointerup", stopMiddlePan, true);
        window.removeEventListener("blur", stopMiddlePan, true);
        event.preventDefault?.();
        event.stopImmediatePropagation?.();
    };
    prompt.addEventListener("pointerdown", event => {
        if (event.button !== 1) return;
        middlePan = { x: event.clientX, y: event.clientY };
        document.body.style.cursor = "grabbing";
        window.addEventListener("pointermove", moveMiddlePan, true);
        window.addEventListener("pointerup", stopMiddlePan, true);
        window.addEventListener("blur", stopMiddlePan, true);
        event.preventDefault();
        event.stopImmediatePropagation();
    }, true);
    const measureContentHeight = () => {
        // 完全展开的自然内容高度。注意：ComfyUI 会在节点滚出视口时给 dom-widget
        // 容器设 display:none，此时所有测量值全为 0——必须保留上次已知高度，
        // 绝不能返回坍缩值，否则 node.setSize 会被锁死在最小高度上。
        const visible = root.offsetHeight > 0;
        if (!visible) return userHeight;
        // 直接采用渲染态 scrollHeight：它天然包含全部子元素、gap 与最新内容
        // （render/updateAdvancedVisibility 更新 DOM 后的值），避免了"测量时点
        // 早于内容更新"导致的 27px 级误差。提示词 flex:1 吸收富余时
        // scrollHeight==分配高度，读数等于当前值——不会自我增长（无正反馈）；
        // 代价是内容减少后节点不自动回落（保持宽松），可接受。
        const content = root.scrollHeight;
        if (content <= 0) return userHeight;
        return Math.max(MIN_NODE_HEIGHT + 120, Math.ceil(content) + 2);
    };
    function syncLayout(height = userHeight, writeNode = false) {
        if (layoutLock) return;
        layoutLock = true;
        try {
            // 面板按"完全展开"设计：节点高度 = 面板内容自然高度 + 面板顶部占位。
            // 顶部占位用 domWidget.y（LiteGraph 布局坐标：标题栏 + 面板上方的
            // 原生 widget 行），它不受画布 transform 缩放影响，比"节点高-容器高"
            // 的 DOM 读数差值稳定（后者会被 canvas scale 污染）。
            const panelTop = Number(domWidget?.y);
            const content = measureContentHeight();
            const nextHeight = Number.isFinite(panelTop) && panelTop > 0
                ? Math.ceil(panelTop + content)
                : content;
            userHeight = nextHeight;
            // 面板根的高度 = 容器将得到的高度（=内容高），提示词的 1fr 由此撑满。
            root.style.height = `${content}px`;
            if ((writeNode || node.size?.[1] !== nextHeight) && (node.size?.[0] !== WIDTH || node.size?.[1] !== nextHeight)) {
                node.setSize([WIDTH, nextHeight]);
            }
            // The rich editor is absolutely bounded by promptWrap. Keeping an
            // inline 100% height here would add the 22px toolbar once again and
            // lets Nodes 2.0 include text content in the widget's measured size.
            prompt.style.removeProperty("height");
            node.setDirtyCanvas(true, true);
        } finally {
            layoutLock = false;
        }
    }
    const previousOnResize = node.onResize;
    node.onResize = function(...args) {
        if (this.size?.[0] !== WIDTH) this.size[0] = WIDTH;
        previousOnResize?.apply(this, args);
        if (layoutLock) return;
        // 完全展开设计：忽略外部传入的高度，始终回弹到内容自然高度。
        syncLayout(false, true);
        persistState();
    };

    const mode = widget(node, "main_mode"), aspect = widget(node, "aspect"), mp = widget(node, "megapixels");
    const durationWidget = widget(node, "duration_seconds");
    if (durationWidget) {
        durationWidget.options = durationWidget.options || {};
        durationWidget.options.precision = 0;
        durationWidget.options.step = 1;
        try {
            Object.defineProperty(durationWidget.options, "step2", {
                value: 1, writable: true, configurable: true,
            });
        } catch {
            durationWidget.options.step2 = 1;
        }
        durationWidget.options.round = 1;
        const oldDurationCallback = durationWidget.callback;
        durationWidget.callback = function(value) {
            oldDurationCallback?.call(this, value);
            this.value = Math.max(2, Math.min(30, Math.round(Number(value) || 2)));
            node.setDirtyCanvas(true, true);
        };
    }
    const state = { mode: savedMode }; const media = new Map(savedState.media || []);
    // A removed upload leaves its prompt ordinal reserved. This keeps, for
    // example, <Picture 1> empty instead of making Picture 2 take its preview.
    // Vacancies are UI-only and are never written to the actual media widgets.
    const vacantMediaSlots = new Map(savedState.vacantMediaSlots || []);
    for (const slot of media.keys()) vacantMediaSlots.delete(slot);
    const vacantMediaEntries = new Map();
    const mediaEntryForOrder = slot => {
        const entry = media.get(slot);
        if (entry) return entry;
        const vacancy = vacantMediaSlots.get(slot);
        if (!vacancy) return null;
        if (!vacantMediaEntries.has(slot)) {
            vacantMediaEntries.set(slot, { ...vacancy, vacant: true, slot });
        }
        return vacantMediaEntries.get(slot);
    };
    let adaptiveRatio = 16 / 9;
    function firstVisualEntry() {
        const slots = state.mode === "text_keyframes"
            ? ["first_frame", "last_frame"]
            : [...videoSlots, ...imageSlots.slice(2)];
        return slots.map(slot => media.get(slot)).find(entry =>
            entry && (entry.kind === "image" || entry.kind === "video")
        ) || null;
    }
    function refreshAdaptiveRatio() {
        if (aspectInternalValue(aspect?.value) !== "adaptive") {
            adaptiveRatio = 16 / 9;
            refreshSize?.();
            return;
        }
        const entry = firstVisualEntry();
        if (!entry) {
            adaptiveRatio = 16 / 9;
            refreshSize?.();
            return;
        }
        const url = fileUrl(entry.name);
        if (entry.kind === "image") {
            const image = new Image();
            image.onload = () => { if (image.naturalWidth && image.naturalHeight) { adaptiveRatio = image.naturalWidth / image.naturalHeight; refreshSize?.(); } };
            image.crossOrigin = "anonymous";
            image.src = url;
        } else {
            const video = document.createElement("video");
            video.onloadedmetadata = () => { if (video.videoWidth && video.videoHeight) { adaptiveRatio = video.videoWidth / video.videoHeight; refreshSize?.(); } };
            video.src = url;
        }
    }
    prompt.value = cleanPrompt(promptByMode[state.mode]);
    renderPromptHighlights();
    setPromptWidget(node, prompt.value);
    let uploadNotice = "";
    function syncMediaWidgets() {
        for (const name of mediaSlots) {
            const w = widget(node, name);
            if (!w) continue;
            const next = media.get(name)?.name || "";
            if (w.value !== next) w.value = next;
        }
    }
    if (stateWidget) {
        stateWidget.serialize = true;
        stateWidget.options = stateWidget.options || {};
        stateWidget.options.serialize = true;
        stateWidget.serializeValue = () => {
            persistState();
            return stateWidget.value;
        };
    }
    for (const name of mediaSlots) {
        const value = widget(node, name)?.value;
        if (!media.has(name) && value && value !== "(none)") media.set(name, { name: value, kind: kindOf({ name: value, type: "" }) });
    }
    syncMediaWidgets();
    function nextSlot(kind) {
        const slots = state.mode === "text_keyframes"
            ? (kind === "image" ? (aiMode.value === "l2va" ? ["last_frame", "first_frame"] : ["first_frame", "last_frame"]) : kind === "audio" ? ["hybrid_audio"] : [])
            : kind === "image" ? imageSlots.slice(2) : kind === "video" ? videoSlots : audioSlots.slice(1);
        return slots.find(s => !media.has(s));
    }
    function resolvedTaskType() {
        const first = media.has("first_frame");
        const last = media.has("last_frame");
        const refs = [...media.entries()].some(([slot]) => !["first_frame", "last_frame", "hybrid_audio"].includes(slot));
        const hybridAudio = media.has("hybrid_audio");
        if (state.mode === "all_reference") return "Ref2VA";
        // Keyframe mode ignores all-reference slots. Old serialized graphs
        // may still contain ref_* entries after switching modes, so they must
        // not turn a plain I2VA/FL2VA/L2VA state into Hybrid in the UI.
        if (state.mode === "text_keyframes") {
            if (hybridAudio && (first || last)) return "Hybrid";
            if (first && last) return "FL2VA";
            if (first) return "I2VA";
            if (last) return "L2VA";
            return "T2VA";
        }
        if (refs) return (first || last) ? "Hybrid" : "Ref2VA";
        if (hybridAudio && (first || last)) return "Hybrid";
        if (first && last) return "FL2VA";
        if (first) return "I2VA";
        if (last) return "L2VA";
        return "T2VA";
    }
    const taskLabels = { I2VA: "First frame", FL2VA: "First and last frames", L2VA: "Last frame", T2VA: "Text-to-video", Ref2VA: "All-purpose reference" };
    function taskLabel(task) {
        if (task !== "Hybrid") return t(taskLabels[task] || task);
        if (state.mode === "text_keyframes") {
            const first = media.has("first_frame");
            const last = media.has("last_frame");
            if (first && last) return t("First and last frames") + " + " + t("Audio");
            if (first) return t("First frame") + " + " + t("Audio");
            if (last) return t("Last frame") + " + " + t("Audio");
        }
        return t("Reference media") + " + " + t("Audio");
    }
    function repairLegacyReferenceTags() {
        if (state.mode !== "all_reference" || !prompt.value) return;
        const activeCounts = { image: 0, video: 0, audio: 0 };
        for (const [slot, entry] of media) {
            if (["first_frame", "last_frame", "hybrid_audio"].includes(slot)) continue;
            if (entry?.kind in activeCounts) activeCounts[entry.kind] += 1;
        }
        let value = prompt.value;
        for (const [kind, label] of [["image", "Picture"], ["video", "Video"], ["audio", "Audio"]]) {
            // Deleted slots deliberately reserve their old prompt ordinal.
            // Legacy single-item repair must not collapse those vacancies.
            if ([...vacantMediaSlots.values()].some(entry => entry?.kind === kind)) continue;
            if (activeCounts[kind] !== 1) continue;
            const one = new RegExp(`<${label}\\s+1>`, "i");
            if (one.test(value)) continue;
            value = value.replace(new RegExp(`<${label}\\s+\\d+>`, "i"), `<${label} 1>`);
        }
        if (value !== prompt.value) {
            prompt.value = value;
            renderPromptHighlights();
            promptByMode[state.mode] = value;
            setPromptWidget(node, value);
            persistState();
        }
    }
    function refreshTaskType() {
        const task = resolvedTaskType();
        const label = taskLabel(task);
        taskStatus.textContent = `${task} · ${label}`;
        const automatic = aiMode.querySelector('option[value="auto"]');
        if (automatic) automatic.textContent = `${t("auto")} — ${label}`;
        dimensions.textContent = canvas(aspectInternalValue(aspect?.value), mp?.value, adaptiveRatio);
        setWidget(node, "task_type", "auto");
    }
    function normalizePromptTagFormat() {
        const task = resolvedTaskType();
        const bare = task === "FL2VA";
        const normalized = (prompt.value || "").replace(
            /<\s*(Picture|Image|Video|Audio)\s*#?\s*(\d+)\s*>|(?<![\w<])(Picture|Image|Video|Audio)\s*#?\s*(\d+)\b(?!\s*>)/gi,
            (_match, bracketType, bracketOrdinal, bareType, bareOrdinal) => {
                const type = (bracketType || bareType).toLowerCase();
                const ordinal = bracketOrdinal || bareOrdinal;
                const officialType = type === "image" ? "Picture" : type[0].toUpperCase() + type.slice(1);
                return bare ? `${officialType} ${ordinal}` : `<${officialType} ${ordinal}>`;
            },
        );
        if (normalized !== prompt.value) {
            prompt.value = normalized;
            renderPromptHighlights();
            promptByMode[state.mode] = normalized;
            setPromptWidget(node, normalized);
            persistState();
        }
    }
    function labelFor(slot, kind) {
        if (slot === "first_frame") return t("First frame");
        if (slot === "last_frame") return t("Last frame");
        if (kind === "audio" && slot !== "hybrid_audio") return `audio ${audioOrdinalFor(slot)}`;
        const prefix = kind === "video" ? "video" : kind === "image" ? "picture" : "audio";
        // Frame uploads are kept in state when switching modes, but they are
        // not reference media in Ref2VA. Exclude them from reference ordinals
        // so a single reference picture is always <picture 1>.
        const list = mediaSlots.map(s => [s, mediaEntryForOrder(s)]).filter(([s, e]) => {
            if (!e) return false;
            if (e.kind !== kind) return false;
            if (state.mode === "all_reference" && (s === "first_frame" || s === "last_frame")) return false;
            if (state.mode === "all_reference" && s === "hybrid_audio") return false;
            return true;
        }).sort((a,b) => a[0].localeCompare(b[0]));
        return `${prefix} ${list.findIndex(([s]) => s === slot) + 1}`;
    }
    function audioOrdinalFor(slot) {
        const orderedEntries = mediaSlots.map(s => [s, mediaEntryForOrder(s)]);
        const videos = orderedEntries.filter(([s, e]) => e?.kind === "video" && !e.muted).sort((a, b) => a[0].localeCompare(b[0]));
        const videoIndex = videos.findIndex(([s]) => s === slot);
        if (videoIndex >= 0) return videoIndex + 1;
        const audios = orderedEntries.filter(([s, e]) => e?.kind === "audio" && s !== "hybrid_audio").sort((a, b) => a[0].localeCompare(b[0]));
        const audioIndex = audios.findIndex(([s]) => s === slot);
        return audioIndex >= 0 ? videos.length + audioIndex + 1 : 0;
    }
    resolvePromptMedia = (kind, ordinal) => {
        if (!Number.isFinite(ordinal) || ordinal < 1) return null;
        if (kind === "picture" && state.mode === "text_keyframes") {
            const keyframes = ["first_frame", "last_frame"]
                .map(mediaEntryForOrder).filter(entry => entry?.kind === "image");
            const selected = keyframes[ordinal - 1];
            return selected?.vacant ? null : selected || null;
        }
        if (kind === "audio") {
            if (state.mode === "text_keyframes") {
                const selected = ordinal === 1 ? mediaEntryForOrder("hybrid_audio") : null;
                return selected?.vacant ? null : selected || null;
            }
            const orderedEntries = mediaSlots.map(slot => [slot, mediaEntryForOrder(slot)]);
            const videoAudio = orderedEntries
                .filter(([slot, entry]) => slot.startsWith("ref_video_") && entry?.kind === "video" && !entry.muted)
                .sort((a, b) => a[0].localeCompare(b[0]));
            const audioFiles = orderedEntries
                .filter(([slot, entry]) => slot.startsWith("ref_audio_") && entry?.kind === "audio")
                .sort((a, b) => a[0].localeCompare(b[0]));
            const selected = [...videoAudio, ...audioFiles][ordinal - 1]?.[1];
            return selected?.vacant ? null : selected || null;
        }
        const targetKind = kind === "picture" ? "image" : kind;
        const entries = mediaSlots.map(slot => [slot, mediaEntryForOrder(slot)]).filter(([slot, entry]) => {
            if (entry?.kind !== targetKind) return false;
            if (state.mode === "all_reference" && ["first_frame", "last_frame", "hybrid_audio"].includes(slot)) return false;
            return state.mode !== "text_keyframes" || ["first_frame", "last_frame", "hybrid_audio"].includes(slot);
        }).sort((a, b) => a[0].localeCompare(b[0]));
        const selected = entries[ordinal - 1]?.[1];
        return selected?.vacant ? null : selected || null;
    };
    const refreshPromptMediaPreviews = (...changedEntries) => {
        // Media changes are discrete user actions, so refresh the rich prompt
        // only here instead of observing or polling the media map. Drop stale
        // video frame data URLs when a clip is removed or replaced.
        for (const entry of changedEntries) {
            if (entry?.kind === "video" && entry.name) promptVideoThumbnailCache.delete(entry.name);
        }
        renderPromptHighlights();
    };
    function insertTag(kind, slot) {
        if (upstreamConnected()) return;
        const raw = labelFor(slot, kind);
        const task = resolvedTaskType();
        let tag;
        if (slot === "first_frame" || slot === "last_frame") {
            const hasBothKeyframes = media.has("first_frame") && media.has("last_frame");
            const ordinal = (task === "FL2VA" || (task === "Hybrid" && hasBothKeyframes))
                ? (slot === "first_frame" ? 1 : 2)
                : 1;
            tag = task === "FL2VA" ? `Picture ${ordinal}` : `<Picture ${ordinal}>`;
        } else {
            tag = raw.startsWith("picture ") ? `<Picture ${raw.slice(8)}>`
                : raw.startsWith("video ") ? `<Video ${raw.slice(6)}>`
                : raw.startsWith("audio ") ? `<Audio ${raw.slice(6)}>`
                : `<${raw}>`;
        }
        const a = document.activeElement === prompt ? prompt.selectionStart : prompt.value.length;
        const b = document.activeElement === prompt ? prompt.selectionEnd : a;
        prompt.setRangeText(tag, a, b, "end");
        renderPromptHighlights();
        promptByMode[state.mode] = prompt.value;
        setPromptWidget(node, prompt.value);
        persistState();
    }
    function insertVideoAudioTag(slot) {
        if (upstreamConnected()) return;
        const ordinal = audioOrdinalFor(slot);
        if (media.get(slot)?.muted || !ordinal) return;
        const a = document.activeElement === prompt ? prompt.selectionStart : prompt.value.length;
        const b = document.activeElement === prompt ? prompt.selectionEnd : a;
        prompt.setRangeText(`<Audio ${ordinal}>`, a, b, "end"); renderPromptHighlights(); promptByMode[state.mode] = prompt.value; setPromptWidget(node, prompt.value); persistState();
    }
    const optimizerProviders = {
        runninghub: { label: "RunningHub 国内版（推荐）", url: "https://www.runninghub.cn/openapi/v2", model: "openai/gpt-5.6-sol", protocol: "runninghub" },
        runninghub_overseas: { label: "RunningHub 海外版", url: "https://www.runninghub.ai/openapi/v2", model: "openai/gpt-5.6-sol", protocol: "runninghub" },
        openai: { label: "OpenAI", url: "https://api.openai.com/v1", model: "gpt-4.1-mini", protocol: "openai" },
        gemini: { label: "Google Gemini", url: "https://generativelanguage.googleapis.com/v1beta", model: "gemini-2.5-flash", protocol: "gemini" },
        openrouter: { label: "OpenRouter", url: "https://openrouter.ai/api/v1", model: "google/gemini-2.5-flash", protocol: "openai" },
        dashscope: { label: "阿里云百炼", url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-vl-max", protocol: "openai" },
        siliconflow: { label: "SiliconFlow", url: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen2.5-VL-72B-Instruct", protocol: "openai" },
        custom: { label: t("Custom"), url: "", model: "", protocol: "openai" },
    };
    const migrateProviderApiKeys = settings => {
        if (!settings) return settings;
        const provider = String(settings.provider || "runninghub").toLowerCase();
        const apiKeys = settings.api_keys && typeof settings.api_keys === "object"
            ? { ...settings.api_keys }
            : {};
        const legacyKey = String(settings.api_key || "");
        // Legacy workflows had one shared key. Assign it only to the provider
        // that was selected when the workflow was saved; never copy a CN key
        // into the overseas account or vice versa.
        if (legacyKey && !apiKeys[provider]) apiKeys[provider] = legacyKey;
        const activeKey = String(apiKeys[provider] || "");
        const providerModels = settings.provider_models && typeof settings.provider_models === "object"
            ? { ...settings.provider_models }
            : {};
        // Older workflows only stored the currently active model. Migrate it
        // to the selected provider without copying that choice to the other
        // RunningHub region.
        const legacyModel = String(settings.model || "");
        if (legacyModel && !providerModels[provider]) providerModels[provider] = legacyModel;
        const activeModel = String(providerModels[provider] || legacyModel || optimizerProviders[provider]?.model || "");
        return {
            ...settings,
            api_keys: apiKeys,
            api_key: activeKey,
            has_api_key: !!activeKey,
            provider_models: providerModels,
            model: activeModel,
        };
    };
    const migrateLegacyOptimizerDefault = settings => {
        settings = migrateProviderApiKeys(settings);
        if (!settings || settings.mode === "local") return settings;
        const provider = String(settings.provider || "").toLowerCase();
        const model = String(settings.model || "").toLowerCase();
        const apiUrl = String(settings.api_url || "").replace(/\/$/, "").toLowerCase();
        const hasKey = !!settings.has_api_key || !!settings.api_key;
        const legacyDefault = provider === "openai"
            && model === "gpt-4.1-mini"
            && (!apiUrl || apiUrl === "https://api.openai.com/v1")
            && !hasKey;
        if (!legacyDefault) return settings;
        return migrateProviderApiKeys({
            ...settings,
            mode: "api",
            provider: "runninghub",
            api_url: optimizerProviders.runninghub.url,
            model: optimizerProviders.runninghub.model,
            protocol: optimizerProviders.runninghub.protocol,
            api_key: "",
            has_api_key: false,
        });
    };
    async function fetchOptimizerJson(path, options = {}, retryEmpty = true) {
        const response = await api.fetchApi(path, options);
        const text = await response.text();
        if (!text.trim() && retryEmpty) {
            await new Promise(resolve => setTimeout(resolve, 120));
            const separator = path.includes("?") ? "&" : "?";
            return fetchOptimizerJson(`${path}${separator}_=${Date.now()}`, options, false);
        }
        let data;
        try { data = text ? JSON.parse(text) : {}; }
        catch { throw new Error(`模型列表接口返回了无效数据 (HTTP ${response.status})`); }
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        return data;
    }
    async function loadOptimizerSettings() {
        if (optimizerSettings) {
            if (!Array.isArray(optimizerSettings.runninghub_models) || !optimizerSettings.runninghub_models.length
                || !Array.isArray(optimizerSettings.runninghub_overseas_models) || !optimizerSettings.runninghub_overseas_models.length
                || !Array.isArray(optimizerSettings.mmproj_models) || !optimizerSettings.mmproj_models.length) {
                try {
                    const defaults = await fetchOptimizerJson(`${OPTIMIZER_ROUTE}/config`, { cache: "no-store" });
                    optimizerSettings = {
                        ...defaults,
                        ...optimizerSettings,
                        runninghub_models: defaults.runninghub_models || [],
                        runninghub_overseas_models: defaults.runninghub_overseas_models || [],
                        models: optimizerSettings.models || defaults.models || [],
                        mmproj_models: optimizerSettings.mmproj_models || defaults.mmproj_models || [],
                        missing_dependencies: optimizerSettings.missing_dependencies || defaults.missing_dependencies || [],
                    };
                } catch (error) {
                    console.warn("MiniMax H3 prompt optimizer settings refresh failed; using saved settings:", error);
                    optimizerSettings = {
                        ...optimizerSettings,
                        runninghub_models: optimizerSettings.runninghub_models || [],
                        runninghub_overseas_models: optimizerSettings.runninghub_overseas_models || [],
                        models: optimizerSettings.models || [],
                        mmproj_models: optimizerSettings.mmproj_models || [],
                        missing_dependencies: optimizerSettings.missing_dependencies || [],
                    };
                }
            }
            optimizerSettings = migrateLegacyOptimizerDefault(optimizerSettings);
            refreshOptimizerName();
            return optimizerSettings;
        }
        let defaults;
        try { defaults = await fetchOptimizerJson(`${OPTIMIZER_ROUTE}/config`, { cache: "no-store" }); }
        catch (error) { throw new Error(`${t("Unable to load prompt optimizer settings")}: ${error.message}`); }
        optimizerSettings = migrateLegacyOptimizerDefault(defaults);
        refreshOptimizerName(); refreshPromptConnection();
        return optimizerSettings;
    }
    function openOptimizerSettings() {
        loadOptimizerSettings().then(current => {
            const overlay = make("div"); overlay.className = "mxv-opt-overlay";
            const dialog = make("div"); dialog.className = "mxv-opt-dialog"; overlay.append(dialog);
            const title = make("div", {}, t("LLM Prompt Optimization Configuration")); title.className = "mxv-opt-title"; dialog.append(title);
            const row = (label, control, custom = false) => { const wrap = make("label"); wrap.className = `mxv-opt-row${custom ? " mxv-opt-custom" : ""}`; wrap.append(make("span", {}, t(label)), control); dialog.append(wrap); return control; };
            const mode = row("Optimization mode", make("select")); mode.append(new Option(t("Online API"), "api"), new Option(t("Local vision model"), "local")); mode.value = current.mode || "api";
            const provider = row("Provider", make("select"));
            for (const [value, preset] of Object.entries(optimizerProviders)) provider.append(new Option(preset.label, value));
            provider.value = current.provider || "runninghub";
            const providerApiKeys = { ...(current.api_keys || {}) };
            if (current.api_key && !providerApiKeys[current.provider || "runninghub"]) providerApiKeys[current.provider || "runninghub"] = current.api_key;
            const providerModels = { ...(current.provider_models || {}) };
            if (current.model && !providerModels[current.provider || "runninghub"]) providerModels[current.provider || "runninghub"] = current.model;
            const key = row("API key", make("input")); key.type = "password"; key.value = providerApiKeys[provider.value] || "";
            const readMedia = make("input"); readMedia.type = "checkbox"; readMedia.checked = current.read_media !== false; readMedia.className = "mxv-opt-check";
            const language = make("div"); language.className = "mxv-opt-language";
            for (const value of ["English", "中文"]) {
                const label = make("label"); const radio = make("input"); radio.type = "radio"; radio.name = `mxv-output-language-${node.id}`; radio.value = value; radio.checked = (current.output_language || "中文") === value; label.append(radio, make("span", {}, value)); language.append(label);
            }
            const url = row("API URL", make("input"), true);
            const model = row("Model", make("input"), true);
            const protocol = row("Protocol", make("select"), true); protocol.append(
                new Option("OpenAI Chat Completions", "openai"),
                new Option("OpenAI Responses", "responses"),
                new Option("Gemini GenerateContent", "gemini"),
            );
            const runninghubModelGroup = make("div"); runninghubModelGroup.className = "mxv-opt-model-row";
            const runninghubModel = make("select"); runninghubModel.className = "mxv-opt-model-native"; runninghubModelGroup.append(runninghubModel);
            const runninghubModelPicker = make("button"); runninghubModelPicker.type = "button"; runninghubModelPicker.className = "mxv-opt-model-picker"; runninghubModelGroup.append(runninghubModelPicker);
            const refreshIcon = '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7a5 5 0 0 1 4.9 4H20a8 8 0 0 0-2.35-4.65ZM12 17a5 5 0 0 1-4.9-4H4a8 8 0 0 0 8 7v3l5-5-5-5v4Z"/></svg>';
            const refreshRunninghubModels = make("button"); refreshRunninghubModels.type = "button"; refreshRunninghubModels.className = "mxv-opt-refresh"; refreshRunninghubModels.innerHTML = refreshIcon; refreshRunninghubModels.title = t("Refresh RunningHub models"); refreshRunninghubModels.setAttribute("aria-label", t("Refresh RunningHub models")); runninghubModelGroup.append(refreshRunninghubModels);
            const runninghubModelMenu = make("div"); runninghubModelMenu.className = "mxv-opt-model-menu";
            const runninghubModelSearch = make("input"); runninghubModelSearch.type = "search"; runninghubModelSearch.placeholder = t("Search models"); runninghubModelSearch.className = "mxv-opt-model-search";
            const runninghubModelResults = make("div"); runninghubModelResults.className = "mxv-opt-model-results";
            runninghubModelMenu.append(runninghubModelSearch, runninghubModelResults); runninghubModelGroup.append(runninghubModelMenu);
            row("Model", runninghubModelGroup);
            const runninghubModelsByProvider = {
                runninghub: [...(current.runninghub_models || [])],
                runninghub_overseas: [...(current.runninghub_overseas_models || current.runninghub_models || [])],
            };
            let runninghubModels = runninghubModelsByProvider[provider.value] || [];
            const fillRunninghubModels = (models = runninghubModels, preserveValue = runninghubModel.value || current.model || "openai/gpt-5.6-sol") => {
                runninghubModels = models || [];
                runninghubModel.replaceChildren(...runninghubModels.map(value => new Option(value, value)));
                if ([...runninghubModel.options].some(option => option.value === preserveValue)) runninghubModel.value = preserveValue;
                else if ([...runninghubModel.options].some(option => option.value === "openai/gpt-5.6-sol")) runninghubModel.value = "openai/gpt-5.6-sol";
                refreshRunninghubModelPicker(); renderRunninghubModels();
            };
            for (const value of runninghubModels) runninghubModel.append(new Option(value, value));
            runninghubModel.value = providerModels[provider.value] || "openai/gpt-5.6-sol";
            const refreshRunninghubModelPicker = () => { runninghubModelPicker.textContent = runninghubModel.value || ""; };
            const renderRunninghubModels = () => {
                const keyword = runninghubModelSearch.value.trim().toLowerCase();
                const filtered = keyword ? runninghubModels.filter(value => value.toLowerCase().includes(keyword)) : runninghubModels;
                runninghubModelResults.replaceChildren();
                if (!filtered.length) {
                    const empty = make("div", {}, t("No matching models")); empty.className = "mxv-opt-model-empty"; runninghubModelResults.append(empty); return;
                }
                for (const value of filtered) {
                    const option = make("button", {}, value); option.type = "button"; option.className = "mxv-opt-model-option";
                    option.classList.toggle("selected", value === runninghubModel.value);
                    option.onclick = () => { runninghubModel.value = value; refreshRunninghubModelPicker(); runninghubModelMenu.classList.remove("open"); };
                    runninghubModelResults.append(option);
                }
            };
            refreshRunninghubModelPicker(); renderRunninghubModels();
            runninghubModelPicker.onclick = () => {
                const opening = !runninghubModelMenu.classList.contains("open");
                runninghubModelMenu.classList.toggle("open", opening);
                if (opening) { runninghubModelSearch.value = ""; renderRunninghubModels(); requestAnimationFrame(() => runninghubModelSearch.focus()); }
            };
            runninghubModelSearch.addEventListener("input", renderRunninghubModels);
            const localModelGroup = make("div"); localModelGroup.className = "mxv-opt-model-row";
            const localModel = make("select"); localModel.className = "mxv-opt-model-native"; localModelGroup.append(localModel);
            const localModelPicker = make("button"); localModelPicker.type = "button"; localModelPicker.className = "mxv-opt-model-picker"; localModelGroup.append(localModelPicker);
            const localModelMenu = make("div"); localModelMenu.className = "mxv-opt-model-menu";
            const localModelSearch = make("input"); localModelSearch.type = "search"; localModelSearch.placeholder = t("Search local models"); localModelSearch.className = "mxv-opt-model-search";
            const localModelResults = make("div"); localModelResults.className = "mxv-opt-model-results";
            localModelMenu.append(localModelSearch, localModelResults); localModelGroup.append(localModelMenu);
            const refreshModels = make("button"); refreshModels.type = "button"; refreshModels.className = "mxv-opt-refresh"; refreshModels.innerHTML = refreshIcon; refreshModels.title = t("Refresh local models"); refreshModels.setAttribute("aria-label", t("Refresh local models")); localModelGroup.append(refreshModels);
            row("Local model", localModelGroup);
            const localMmproj = row("Vision model (mmproj)", make("select"));
            const localDevice = row("Local device", make("select")); localDevice.append(new Option(t("Auto"), "auto"), new Option("GPU", "cuda"), new Option("CPU", "cpu")); localDevice.value = current.local_device || "cuda";
            const dependencyStatus = make("div"); dependencyStatus.className = "mxv-opt-dependencies"; dialog.append(dependencyStatus);
            const maxTokens = row("Maximum output tokens", make("input"));
            maxTokens.type = "number"; maxTokens.min = "512"; maxTokens.max = "8192"; maxTokens.step = "512";
            maxTokens.value = String(Math.max(512, Math.min(8192, Number(current.max_tokens) || 4096)));
            const normalizeMaxTokens = () => {
                const value = Number.parseInt(maxTokens.value, 10);
                maxTokens.value = String(Math.max(512, Math.min(8192, Number.isFinite(value) ? value : 4096)));
            };
            maxTokens.addEventListener("change", normalizeMaxTokens);
            row("Output language", language);
            const checkboxRows = make("div"); checkboxRows.className = "mxv-opt-checks";
            const checkboxRow = (label, control) => { const wrap = make("label"); wrap.className = "mxv-opt-row"; wrap.append(make("span", {}, t(label)), control); checkboxRows.append(wrap); };
            checkboxRow("Read visual references", readMedia);
            const autoOptimize = make("input"); autoOptimize.type = "checkbox"; autoOptimize.checked = !!current.auto_optimize; autoOptimize.className = "mxv-opt-check"; checkboxRow("Automatic optimization before run", autoOptimize);
            dialog.append(checkboxRows);
            let localModels = current.models || [];
            let mmprojModels = current.mmproj_models || [];
            let mmprojManuallySelected = !!current.local_mmproj;
            const selectedLocalModel = () => localModels.find(item => item.relative_path === localModel.value);
            const fillMmprojModels = (models = mmprojModels, preserveValue = localMmproj.value || current.local_mmproj || "") => {
                mmprojModels = models || [];
                localMmproj.replaceChildren();
                localMmproj.append(new Option(t(mmprojModels.length ? "Choose vision projector" : "No mmproj models found"), ""));
                mmprojModels.forEach(item => localMmproj.append(new Option(item.name, item.relative_path)));
                if ([...localMmproj.options].some(option => option.value === preserveValue)) localMmproj.value = preserveValue;
            };
            const autoSelectMmproj = () => {
                const selected = selectedLocalModel();
                if (selected?.format !== "gguf") { localMmproj.value = ""; return; }
                const best = selected.mmproj_candidates?.find(value => mmprojModels.some(item => item.relative_path === value));
                localMmproj.value = best || "";
            };
            const refreshModelPicker = () => {
                const selected = selectedLocalModel();
                localModelPicker.textContent = selected?.name || t("No compatible local vision models found");
            };
            const renderModelResults = () => {
                const keyword = localModelSearch.value.trim().toLowerCase();
                const filtered = keyword
                    ? localModels.filter(item => `${item.name} ${item.relative_path}`.toLowerCase().includes(keyword))
                    : localModels;
                localModelResults.replaceChildren();
                if (!filtered.length) {
                    const empty = make("div", {}, t("No compatible local vision models found")); empty.className = "mxv-opt-model-empty"; localModelResults.append(empty); return;
                }
                for (const item of filtered) {
                    const option = make("button", {}, item.name); option.type = "button"; option.className = "mxv-opt-model-option";
                    option.classList.toggle("selected", item.relative_path === localModel.value);
                    option.onclick = () => { localModel.value = item.relative_path; mmprojManuallySelected = false; refreshModelPicker(); autoSelectMmproj(); sync(); localModelMenu.classList.remove("open"); };
                    localModelResults.append(option);
                }
            };
            const fillModels = (models = localModels, preserveValue = localModel.value || current.local_model || "") => {
                localModels = models || [];
                localModel.replaceChildren();
                localModels.forEach(item => localModel.append(new Option(item.name, item.relative_path)));
                if ([...localModel.options].some(option => option.value === preserveValue)) localModel.value = preserveValue;
                if (!localModel.options.length) localModel.append(new Option(t("No compatible local vision models found"), ""));
                refreshModelPicker(); renderModelResults();
            };
            const showDependencies = missing => { dependencyStatus.textContent = missing?.length ? `${t("Missing local model dependencies")}: ${missing.join(", ")}` : ""; };
            fillMmprojModels(current.mmproj_models);
            fillModels(current.models);
            if (!localMmproj.value) autoSelectMmproj();
            showDependencies(current.missing_dependencies);
            localModelPicker.onclick = () => {
                const opening = !localModelMenu.classList.contains("open");
                localModelMenu.classList.toggle("open", opening);
                if (opening) { localModelSearch.value = ""; renderModelResults(); requestAnimationFrame(() => localModelSearch.focus()); }
            };
            localModelSearch.addEventListener("input", renderModelResults);
            localMmproj.addEventListener("change", () => { mmprojManuallySelected = !!localMmproj.value; });
            dialog.addEventListener("pointerdown", event => {
                if (!localModelGroup.contains(event.target)) localModelMenu.classList.remove("open");
                if (!runninghubModelGroup.contains(event.target)) runninghubModelMenu.classList.remove("open");
            });
            const withRefreshState = async (button, action) => {
                if (button.disabled) return;
                button.disabled = true; button.classList.add("loading");
                try { await action(); }
                finally { button.classList.remove("loading"); button.disabled = false; }
            };
            refreshRunninghubModels.onclick = () => withRefreshState(refreshRunninghubModels, async () => {
                const selectedProvider = provider.value === "runninghub_overseas" ? "runninghub_overseas" : "runninghub";
                const data = await fetchOptimizerJson(`${OPTIMIZER_ROUTE}/runninghub-models?provider=${encodeURIComponent(selectedProvider)}`, { cache: "no-store" });
                const refreshed = selectedProvider === "runninghub_overseas" ? data.runninghub_overseas_models : data.runninghub_models;
                runninghubModelsByProvider[selectedProvider] = [...(refreshed || [])];
                fillRunninghubModels(runninghubModelsByProvider[selectedProvider]);
                optimizerSettings = {
                    ...(optimizerSettings || current),
                    runninghub_models: [...runninghubModelsByProvider.runninghub],
                    runninghub_overseas_models: [...runninghubModelsByProvider.runninghub_overseas],
                };
            }).catch(error => alert(error.message));
            refreshModels.onclick = () => withRefreshState(refreshModels, async () => {
                const data = await fetchOptimizerJson(`${OPTIMIZER_ROUTE}/models`, { cache: "no-store" });
                const previousModel = localModel.value;
                const previousMmproj = localMmproj.value;
                fillMmprojModels(data.mmproj_models, previousMmproj);
                fillModels(data.models, previousModel);
                const manualStillExists = mmprojManuallySelected && mmprojModels.some(item => item.relative_path === previousMmproj);
                if (!manualStillExists) { mmprojManuallySelected = false; autoSelectMmproj(); }
                optimizerSettings = {
                    ...(optimizerSettings || current),
                    models: [...localModels],
                    mmproj_models: [...mmprojModels],
                    missing_dependencies: data.missing_dependencies || [],
                };
                sync(); showDependencies(data.missing_dependencies);
            }).catch(error => alert(error.message));
            if (!current.models) refreshModels.click();
            const sync = () => {
                const custom = provider.value === "custom";
                const runninghub = provider.value === "runninghub" || provider.value === "runninghub_overseas";
                dialog.classList.toggle("custom", custom);
                const local = mode.value === "local";
                dialog.classList.toggle("local", local);
                [provider, key, url, model, protocol].forEach(control => control.closest("label")?.classList.toggle("mxv-opt-hidden", local));
                runninghubModel.closest("label")?.classList.toggle("mxv-opt-hidden", local || !runninghub);
                model.closest("label")?.classList.toggle("mxv-opt-hidden", local || !custom);
                url.closest("label")?.classList.toggle("mxv-opt-hidden", local || !custom);
                protocol.closest("label")?.classList.toggle("mxv-opt-hidden", local || !custom);
                [localModel, localDevice, refreshModels].forEach(control => (control.closest("label") || control).classList.toggle("mxv-opt-hidden", !local));
                const gguf = selectedLocalModel()?.format === "gguf";
                localMmproj.closest("label")?.classList.toggle("mxv-opt-hidden", !local || !gguf);
                dependencyStatus.classList.toggle("mxv-opt-hidden", !local || !dependencyStatus.textContent);
                const preset = optimizerProviders[provider.value];
                if (!custom) { url.value = preset.url; model.value = preset.model; protocol.value = preset.protocol; }
            };
            url.value = current.api_url || ""; model.value = current.model || ""; protocol.value = current.protocol || "openai";
            provider.addEventListener("change", () => {
                const previousProvider = provider.dataset.previousValue || current.provider || "runninghub";
                providerApiKeys[previousProvider] = key.value;
                if (previousProvider === "runninghub" || previousProvider === "runninghub_overseas") {
                    providerModels[previousProvider] = runninghubModel.value;
                }
                key.value = providerApiKeys[provider.value] || "";
                provider.dataset.previousValue = provider.value;
                if (provider.value === "runninghub" || provider.value === "runninghub_overseas") {
                    const preserve = providerModels[provider.value] || optimizerProviders[provider.value].model;
                    fillRunninghubModels(runninghubModelsByProvider[provider.value] || [], preserve);
                }
                sync();
            }); mode.addEventListener("change", sync); sync();
            provider.dataset.previousValue = provider.value;
            const actions = make("div"); actions.className = "mxv-opt-actions";
            const cancel = make("button", {}, t("Cancel")); const save = make("button", {}, t("Save")); actions.append(cancel, save); dialog.append(actions);
            const close = () => overlay.remove(); cancel.onclick = close; overlay.addEventListener("pointerdown", event => { if (event.target === overlay) close(); });
            save.onclick = async () => {
                const outputLanguage = language.querySelector("input:checked")?.value || "中文";
                const preset = optimizerProviders[provider.value];
                const selectedModel = (provider.value === "runninghub" || provider.value === "runninghub_overseas") ? runninghubModel.value : model.value;
                providerApiKeys[provider.value] = key.value;
                providerModels[provider.value] = selectedModel;
                normalizeMaxTokens();
                const body = { mode: mode.value, provider: provider.value, api_url: provider.value === "custom" ? url.value : preset?.url || url.value, model: selectedModel, protocol: provider.value === "custom" ? protocol.value : preset?.protocol || protocol.value, read_media: readMedia.checked, output_language: outputLanguage, local_model: localModel.value, local_mmproj: localMmproj.value, local_device: localDevice.value, max_tokens: Number(maxTokens.value), auto_optimize: autoOptimize.checked, api_keys: { ...providerApiKeys }, provider_models: { ...providerModels } };
                body.api_key = key.value;
                body.has_api_key = !!key.value;
                optimizerSettings = body; refreshOptimizerName(); refreshPromptConnection(); persistState(); close();
            };
            document.body.append(overlay);
        }).catch(error => alert(error.message));
    }
    function showOptimizerConfigPrompt() {
        const overlay = make("div"); overlay.className = "mxv-opt-overlay";
        const dialog = make("div"); dialog.className = "mxv-opt-dialog"; overlay.append(dialog);
        dialog.append(make("div", {}, t("Prompt optimizer API is not configured. Open settings now?")));
        const actions = make("div"); actions.className = "mxv-opt-actions";
        const cancel = make("button", {}, t("Cancel")); const confirm = make("button", {}, t("Confirm")); actions.append(cancel, confirm); dialog.append(actions);
        const close = () => overlay.remove(); cancel.onclick = close; confirm.onclick = () => { close(); openOptimizerSettings(); };
        document.body.append(overlay);
    }
    function lowImageData(url) {
        return new Promise((resolve, reject) => {
            const image = new Image(); image.crossOrigin = "anonymous";
            image.onload = () => {
                const scale = Math.min(1, 512 / Math.max(image.naturalWidth, image.naturalHeight));
                const canvasEl = document.createElement("canvas"); canvasEl.width = Math.max(1, Math.round(image.naturalWidth * scale)); canvasEl.height = Math.max(1, Math.round(image.naturalHeight * scale));
                canvasEl.getContext("2d", { alpha: false }).drawImage(image, 0, 0, canvasEl.width, canvasEl.height);
                resolve(canvasEl.toDataURL("image/jpeg", .42));
            };
            image.onerror = () => reject(new Error("Unable to read reference image")); image.src = url;
        });
    }
    function lowVideoFrames(url) {
        return new Promise((resolve, reject) => {
            const video = document.createElement("video"); video.crossOrigin = "anonymous"; video.muted = true; video.preload = "metadata";
            video.onloadedmetadata = async () => {
                try {
                    const duration = Number.isFinite(video.duration) ? video.duration : 0;
                    const times = duration > 0 ? [0, duration / 2, Math.max(0, duration - .04)] : [0];
                    const frames = [];
                    for (const time of times) {
                        const target = Math.min(time, Math.max(0, duration - .001));
                        if (video.readyState < 2 || Math.abs(video.currentTime - target) > .002) {
                            await new Promise((done, fail) => {
                                const timeout = setTimeout(() => fail(new Error("Video frame seek timed out")), 10000);
                                video.onseeked = () => { clearTimeout(timeout); done(); };
                                video.currentTime = target;
                            });
                        }
                        const scale = Math.min(1, 512 / Math.max(video.videoWidth, video.videoHeight));
                        const canvasEl = document.createElement("canvas"); canvasEl.width = Math.max(1, Math.round(video.videoWidth * scale)); canvasEl.height = Math.max(1, Math.round(video.videoHeight * scale));
                        canvasEl.getContext("2d", { alpha: false }).drawImage(video, 0, 0, canvasEl.width, canvasEl.height);
                        frames.push(canvasEl.toDataURL("image/jpeg", .42));
                    }
                    resolve(frames);
                } catch (error) { reject(error); }
            };
            video.onerror = () => reject(new Error("Unable to read reference video")); video.src = url;
        });
    }
    function optimizerMediaSpecs() {
        const specs = [];
        if (state.mode === "text_keyframes") {
            const task = resolvedTaskType();
            const frames = ["first_frame", "last_frame"].filter(slot => media.has(slot));
            frames.forEach((slot, index) => specs.push({ slot, kind: "image", label: task === "FL2VA" ? `picture ${index + 1}` : `<picture ${index + 1}>` }));
            if (media.has("hybrid_audio")) specs.push({ slot: "hybrid_audio", kind: "audio", label: "<audio 1>" });
            return specs;
        }
        const images = [...media.entries()].filter(([slot, entry]) => slot.startsWith("ref_image_") && entry?.kind === "image").sort((a,b) => a[0].localeCompare(b[0]));
        const videos = [...media.entries()].filter(([slot, entry]) => slot.startsWith("ref_video_") && entry?.kind === "video").sort((a,b) => a[0].localeCompare(b[0]));
        const audios = [...media.entries()].filter(([slot, entry]) => slot.startsWith("ref_audio_") && entry?.kind === "audio").sort((a,b) => a[0].localeCompare(b[0]));
        images.forEach(([slot], index) => specs.push({ slot, kind: "image", label: `<picture ${index + 1}>` }));
        videos.forEach(([slot], index) => specs.push({ slot, kind: "video", label: `<video ${index + 1}>` }));
        let audioOrdinal = 1;
        videos.forEach(([slot, entry]) => { if (!entry.muted) specs.push({ slot, kind: "audio", label: `<audio ${audioOrdinal++}>`, labelOnly: true }); });
        audios.forEach(([slot]) => specs.push({ slot, kind: "audio", label: `<audio ${audioOrdinal++}>` }));
        return specs;
    }
    async function optimizerMediaPayload(specs) {
        const payload = [];
        const runninghub = optimizerSettings?.mode !== "local" && ["runninghub", "runninghub_overseas"].includes(optimizerSettings?.provider);
        let runninghubImages = 0;
        let runninghubVideo = false;
        for (const spec of specs) {
            const entry = media.get(spec.slot);
            if (!entry) continue;
            const item = { kind: spec.kind, label: spec.label, labelOnly: !!spec.labelOnly };
            if (optimizerSettings?.read_media !== false && spec.kind === "image") {
                if (!runninghub || runninghubImages < 8) item.images = [await lowImageData(fileUrl(entry.name))];
                runninghubImages++;
            }
            else if (optimizerSettings?.read_media !== false && spec.kind === "video" && !spec.labelOnly) {
                if (runninghub) {
                    if (!runninghubVideo) item.source_name = entry.name;
                    runninghubVideo = true;
                } else item.images = await lowVideoFrames(fileUrl(entry.name));
            }
            payload.push(item);
        }
        return payload;
    }
    function optimizerContextSignature(specs, mode = state.mode, task = resolvedTaskType(), duration = Number(durationWidget?.value || 5), context = optimizerTaskContext(specs, mode)) {
        return JSON.stringify({ task, duration, mode, context, media: specs.map(spec => [spec.slot, spec.kind, spec.label, media.get(spec.slot)?.name || "", !!media.get(spec.slot)?.muted]), settings: optimizerSettings && [optimizerSettings.mode, optimizerSettings.provider, optimizerSettings.api_url, optimizerSettings.model, optimizerSettings.protocol, optimizerSettings.local_model, optimizerSettings.local_mmproj, optimizerSettings.local_device, optimizerSettings.read_media, optimizerSettings.output_language] });
    }
    function optimizerTaskContext(specs, mode = state.mode, audioMode = widget(node, "audio_mode")?.value || "native") {
        const keyframes = specs
            .filter(spec => spec.slot === "first_frame" || spec.slot === "last_frame")
            .map(spec => ({
                label: spec.label,
                role: spec.slot === "first_frame" ? "exact_first_frame" : "exact_last_frame",
            }));
        return {
            main_mode: mode,
            keyframes,
            audio_mode: audioMode,
        };
    }
    function applyOptimizedPrompt(value, before, targetMode = state.mode) {
        promptByMode[targetMode] = value;
        optimizerBeforeByMode[targetMode] = before;
        if (state.mode === targetMode) {
            optimizerBefore = before;
            prompt.value = value; renderPromptHighlights(); setPromptWidget(node, value);
            resetPrompt.classList.add("visible");
        }
        persistState();
    }
    function playOptimizerCompleteSound() {
        try {
            optimizerCompleteAudio?.pause();
            optimizerCompleteAudio = new Audio(OPTIMIZER_DONE_SOUND_URL);
            optimizerCompleteAudio.volume = 0.6;
            optimizerCompleteAudio.play().catch(() => {});
        } catch {}
    }
    optimizerGear.onclick = openOptimizerSettings;
    resetPrompt.onclick = () => {
        if (optimizerBefore == null || upstreamConnected()) return;
        if (optimizerCache?.originalPrompt === optimizerBefore) optimizerCache.result = prompt.value;
        prompt.value = optimizerBefore; renderPromptHighlights(); promptByMode[state.mode] = prompt.value; setPromptWidget(node, prompt.value);
        optimizerBeforeByMode[state.mode] = null;
        optimizerBefore = null; resetPrompt.classList.remove("visible"); persistState();
    };
    async function cancelOptimization() {
        if (!optimizing) return;
        const requestId = optimizerRequestId;
        optimizerAbort?.abort();
        if (requestId) {
            api.fetchApi(`${OPTIMIZER_ROUTE}/cancel`, { method: "POST", body: new Blob([JSON.stringify({ request_id: requestId })], { type: "application/json" }) }).catch(() => {});
        }
    }
    function resemblesOfficialPrompt(value, task = resolvedTaskType()) {
        const text = String(value || "").toLowerCase();
        const fields = task === "Ref2VA" || task === "Hybrid"
            ? ["subject_definitions:", "summary:", "retention_analysis:", "detailed_description:", "overall_soundscape:", "non_diegetic_music:"]
            : ["integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:"];
        return fields.every(field => text.includes(field.toLowerCase())) && /\[shot\s*1\]/i.test(text);
    }
    async function waitWithAbort(milliseconds, signal) {
        await new Promise((resolve, reject) => {
            let timer;
            const aborted = () => { clearTimeout(timer); reject(new DOMException("Aborted", "AbortError")); };
            if (signal?.aborted) return aborted();
            timer = setTimeout(() => { signal?.removeEventListener("abort", aborted); resolve(); }, milliseconds);
            signal?.addEventListener("abort", aborted, { once: true });
        });
    }
    async function runRunningHubOptimization(requestBody, signal) {
        const started = await fetchOptimizerJson(`${OPTIMIZER_ROUTE}/start`, {
            method: "POST", signal,
            body: new Blob([JSON.stringify(requestBody)], { type: "application/json" }),
        }, false);
        if (started.status !== "running") throw new Error(started.error || "Prompt optimization failed");
        while (true) {
            await waitWithAbort(1500, signal);
            const status = await fetchOptimizerJson(
                `${OPTIMIZER_ROUTE}/status?request_id=${encodeURIComponent(requestBody.request_id)}`,
                { cache: "no-store", signal },
                false,
            );
            if (status.status === "success") return status;
        }
    }
    async function runPromptOptimization({ automatic = false } = {}) {
        if (optimizing || upstreamConnected() || node.graph !== app.graph || (node.mode != null && node.mode !== 0)) return false;
        const optimizationMode = state.mode;
        const before = prompt.value;
        const task = resolvedTaskType();
        const duration = Number(durationWidget?.value || 5);
        const audioMode = widget(node, "audio_mode")?.value || "native";
        if (automatic && resemblesOfficialPrompt(before, task)) return false;
        const specs = optimizerMediaSpecs();
        const taskContext = optimizerTaskContext(specs, optimizationMode, audioMode);
        try {
            await loadOptimizerSettings();
            if (automatic && !optimizerSettings?.auto_optimize) return false;
            const local = optimizerSettings?.mode === "local";
            if (local && workflowRunning) return false;
            if ((!local && !optimizerSettings?.has_api_key) || (local && !optimizerSettings?.local_model)) {
                if (!automatic) showOptimizerConfigPrompt();
                return false;
            }
            const contextSignature = optimizerContextSignature(specs, optimizationMode, task, duration, taskContext);
            if (optimizerCache?.contextSignature === contextSignature && optimizerCache?.result && (before === optimizerCache.originalPrompt || before === optimizerCache.result)) { applyOptimizedPrompt(optimizerCache.result, optimizerCache.originalPrompt, optimizationMode); return true; }
            optimizing = true; optimizerRequestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`; optimizerAbort = new AbortController();
            optimizePrompt.classList.add("mxv-prompt-loading"); elapsedPrompt.classList.add("visible");
            const started = performance.now();
            const refreshElapsed = () => { elapsedPrompt.textContent = `${t("Optimizing")}：${Math.floor((performance.now() - started) / 1000)} s`; };
            refreshElapsed(); optimizerTimer = setInterval(refreshElapsed, 1000);
            const mediaPayload = await optimizerMediaPayload(specs);
            const timeout = setTimeout(() => {
                const requestId = optimizerRequestId;
                optimizerAbort?.abort();
                if (requestId) api.fetchApi(`${OPTIMIZER_ROUTE}/cancel`, { method: "POST", body: new Blob([JSON.stringify({ request_id: requestId })], { type: "application/json" }) }).catch(() => {});
            }, 200000);
            let response;
            let data;
            try {
                const requestBody = { request_id: optimizerRequestId, prompt: before, task, duration, media: mediaPayload, context: taskContext, config: optimizerSettings };
                // RunningHub must use the single-request endpoint. Its hosted
                // reverse proxy may route separate /start and /status calls to
                // different workers, while the in-process async job registry
                // only exists in the worker that received /start. The direct
                // endpoint keeps the RH polling loop on one request and matches
                // the stable pre-async behavior.
                response = await api.fetchApi(`${OPTIMIZER_ROUTE}/optimize`, { method: "POST", signal: optimizerAbort.signal, body: new Blob([JSON.stringify(requestBody)], { type: "application/json" }) });
                const text = await response.text();
                try { data = JSON.parse(text); }
                catch { throw new Error(/^\s*(?:<!doctype\s+html|<html)/i.test(text) ? "云端网关返回了网页而不是节点数据" : "提示词优化接口返回了无效数据"); }
                if (!response.ok) throw new Error(data.error || "Prompt optimization failed");
            } finally { clearTimeout(timeout); }
            optimizerCache = { contextSignature, originalPrompt: before, result: data.prompt }; applyOptimizedPrompt(data.prompt, before, optimizationMode); playOptimizerCompleteSound();
            return true;
        } catch (error) {
            if (!automatic && error.name !== "AbortError") alert(error.message);
            return false;
        }
        finally {
            clearInterval(optimizerTimer); optimizerTimer = null; optimizerAbort = null; optimizerRequestId = null; optimizing = false;
            elapsedPrompt.classList.remove("visible"); elapsedPrompt.textContent = ""; optimizePrompt.classList.remove("mxv-prompt-loading"); refreshPromptConnection();
        }
    }
    optimizePrompt.onclick = async () => {
        if (optimizing) { await cancelOptimization(); return; }
        await runPromptOptimization();
    };
    const autoOptimizeBeforeQueue = () => runPromptOptimization({ automatic: true });
    autoOptimizerHandlers.add(autoOptimizeBeforeQueue);
    installAutoOptimizerQueueHook();
    // undefined = pointer is outside a paste target; null = the generic
    // "add media" target, where accept() chooses the next compatible slot.
    let hoverPasteSlot;
    let activeMedia = null;
    let closeActiveTrimEditor = null;
    const decodedAudioCache = new Map();
    const formatMediaTime = value => String(Math.max(0, Math.floor(Number(value) || 0))).padStart(2, "0");
    const formatAudioMediaTime = value => String(Math.max(0, Math.round(Number(value) || 0))).padStart(2, "0");
    const stopActiveMedia = () => { if (!activeMedia) return; activeMedia.media.pause(); activeMedia.media.currentTime = activeMedia.resetTime ?? 0; activeMedia.setPlaying(false); activeMedia.onStop?.(); activeMedia = null; };
    const mediaIcon = playing => playing ? '<svg viewBox="0 0 12 12"><path d="M3 2v8M9 2v8" fill="none" stroke="rgba(255,255,255,.6)" stroke-width="1.5"/></svg>' : '<svg viewBox="0 0 12 12"><path d="M2.5 1.5 10 6l-7.5 4.5Z" fill="rgba(255,255,255,.6)" stroke="rgba(255,255,255,.6)" stroke-width="1"/></svg>';
    const formatTrimTime = value => {
        const seconds = Math.max(0, Number(value) || 0);
        const minutes = Math.floor(seconds / 60);
        return `${String(minutes).padStart(2, "0")}:${(seconds - minutes * 60).toFixed(3).padStart(6, "0")}`;
    };
    async function decodeAudioFile(name) {
        if (decodedAudioCache.has(name)) return decodedAudioCache.get(name);
        const pending = (async () => {
            const response = await fetch(fileUrl(name));
            if (!response.ok) throw new Error(`${t("Audio decoding failed")}: ${response.status}`);
            const bytes = await response.arrayBuffer();
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) throw new Error(t("Audio decoding failed"));
            const context = new AudioContextClass();
            try { return await context.decodeAudioData(bytes.slice(0)); }
            finally { await context.close().catch(() => {}); }
        })();
        decodedAudioCache.set(name, pending);
        try { return await pending; }
        catch (error) { decodedAudioCache.delete(name); throw error; }
    }
    async function openAudioTrimEditor(slot, entry) {
        closeActiveTrimEditor?.();
        stopActiveMedia();
        const sourceName = entry.originalName || entry.name;
        const overlay = make("div"); overlay.className = "mxv-trim-overlay";
        const dialog = make("div"); dialog.className = "mxv-trim-dialog";
        dialog.appendChild(make("div", {}, t("Audio trim"))).className = "mxv-trim-title";
        const waveWrap = make("div"); waveWrap.className = "mxv-trim-wave-wrap";
        const wave = make("canvas"); wave.className = "mxv-trim-wave";
        const loading = make("div", {}, "…"); loading.className = "mxv-trim-loading";
        waveWrap.append(wave, loading); dialog.appendChild(waveWrap);
        const times = make("div"); times.className = "mxv-trim-times";
        const timeBox = label => { const box = make("div", {}, t(label)); box.className = "mxv-trim-time"; const value = make("strong", {}, "--"); box.appendChild(value); times.appendChild(box); return { box, value }; };
        const startTime = timeBox("Start"), endTime = timeBox("End"), durationTime = timeBox("Selected duration");
        const startValue = startTime.value, endValue = endTime.value, durationValue = durationTime.value;
        const syncDuration = make("button", {}, t("Sync target duration")); syncDuration.className = "mxv-trim-sync"; durationTime.box.appendChild(syncDuration);
        dialog.appendChild(times);
        const controls = make("div"); controls.className = "mxv-trim-controls";
        const preview = make("button"); preview.className = "mxv-trim-preview"; preview.disabled = true;
        controls.append(preview, make("span", {}, isChineseLocale() ? "滚轮缩放；中键拖动视图；点击选区播放/停止，选区内按住左键拖动选区位置" : "Wheel to zoom; middle-drag to pan; click selection to play/stop; hold left mouse inside selection to move it"));
        controls.lastChild.className = "mxv-trim-hint"; dialog.appendChild(controls);
        const actions = make("div"); actions.className = "mxv-trim-actions";
        const cancel = make("button", {}, t("Cancel"));
        const save = make("button", {}, t("Save")); save.disabled = true;
        actions.append(cancel, save); dialog.appendChild(actions); overlay.appendChild(dialog); document.body.appendChild(overlay);

        let buffer = null;
        let start = 0;
        let end = 0;
        let dragging = null;
        let dragOriginX = 0;
        let dragStart = 0;
        let dragEnd = 0;
        let pointerDownX = 0;
        let selectionMoved = false;
        let panOriginX = 0;
        let panViewStart = 0;
        let viewStart = 0;
        let viewEnd = 0;
        let previewAudio = null;
        let previewPlaying = false;
        let playheadFrame = 0;
        const minSelection = 0.05;
        const previewIcon = playing => playing
            ? '<svg viewBox="0 0 12 12"><rect x="2.5" y="2.5" width="7" height="7" rx=".6" fill="#e2e0db"/></svg>'
            : '<svg viewBox="0 0 12 12"><path d="M2.5 1.5 10 6l-7.5 4.5Z" fill="#e2e0db"/></svg>';
        preview.innerHTML = previewIcon(false);
        const stopPreview = (reset = true) => {
            if (!previewAudio) return;
            previewAudio.pause();
            if (reset) previewAudio.currentTime = start;
            previewPlaying = false;
            cancelAnimationFrame(playheadFrame); playheadFrame = 0;
            preview.innerHTML = previewIcon(false);
            drawWaveform();
        };
        const close = () => {
            stopPreview(false);
            previewAudio?.removeAttribute("src");
            overlay.remove();
            if (closeActiveTrimEditor === close) closeActiveTrimEditor = null;
        };
        closeActiveTrimEditor = close;
        cancel.onclick = close;
        overlay.onclick = event => { if (event.target === overlay) close(); };
        dialog.onclick = event => event.stopPropagation();

        const resizeCanvas = () => {
            const rect = wave.getBoundingClientRect();
            const ratio = Math.max(1, window.devicePixelRatio || 1);
            const width = Math.max(1, Math.round(rect.width * ratio));
            const height = Math.max(1, Math.round(rect.height * ratio));
            if (wave.width !== width || wave.height !== height) { wave.width = width; wave.height = height; }
            return { width, height, ratio };
        };
        const drawWaveform = () => {
            if (!buffer) return;
            const { width, height, ratio } = resizeCanvas();
            const context = wave.getContext("2d");
            context.clearRect(0, 0, width, height);
            context.fillStyle = "#14171b"; context.fillRect(0, 0, width, height);
            const scaleHeight = 24 * ratio;
            const waveformHeight = Math.max(1, height - scaleHeight);
            const center = waveformHeight / 2;
            const channels = Array.from({ length: buffer.numberOfChannels }, (_, channel) => buffer.getChannelData(channel));
            const visibleDuration = Math.max(.001, viewEnd - viewStart);
            const firstSample = Math.max(0, Math.floor(viewStart * buffer.sampleRate));
            const lastSample = Math.min(buffer.length, Math.ceil(viewEnd * buffer.sampleRate));
            const visibleSamples = Math.max(1, lastSample - firstSample);
            const samplesPerPixel = Math.max(1, Math.floor(visibleSamples / width));
            context.strokeStyle = "#93793f"; context.lineWidth = Math.max(1, ratio);
            context.beginPath();
            for (let x = 0; x < width; x++) {
                const from = firstSample + x * visibleSamples / width;
                const to = Math.min(lastSample, firstSample + (x + 1) * visibleSamples / width);
                let peak = 0;
                const stride = Math.max(1, Math.floor((to - from) / 32));
                for (let sample = Math.floor(from); sample < to; sample += stride) {
                    for (const data of channels) peak = Math.max(peak, Math.abs(data[sample] || 0));
                }
                const amplitude = Math.max(1, peak * (waveformHeight * .42));
                context.moveTo(x + .5, center - amplitude); context.lineTo(x + .5, center + amplitude);
            }
            context.stroke();
            const timeToX = seconds => (seconds - viewStart) / visibleDuration * width;
            const startX = timeToX(start), endX = timeToX(end);
            const clippedStartX = Math.max(0, Math.min(width, startX));
            const clippedEndX = Math.max(0, Math.min(width, endX));
            context.fillStyle = "rgba(4,10,15,.62)"; context.fillRect(0, 0, clippedStartX, waveformHeight); context.fillRect(clippedEndX, 0, width - clippedEndX, waveformHeight);
            context.fillStyle = "rgba(10,164,214,.16)"; context.fillRect(clippedStartX, 0, Math.max(0, clippedEndX - clippedStartX), waveformHeight);
            context.strokeStyle = "#e8a33d"; context.lineWidth = Math.max(2, 2 * ratio);
            for (const x of [startX, endX]) { if (x < 0 || x > width) continue; context.beginPath(); context.moveTo(x, 0); context.lineTo(x, waveformHeight); context.stroke(); }
            context.fillStyle = "#e8a33d";
            const handleWidth = 5 * ratio, handleHeight = 24 * ratio;
            if (startX >= 0 && startX <= width) context.fillRect(startX - handleWidth / 2, center - handleHeight / 2, handleWidth, handleHeight);
            if (endX >= 0 && endX <= width) context.fillRect(endX - handleWidth / 2, center - handleHeight / 2, handleWidth, handleHeight);
            const targetTickSeconds = visibleDuration / 7;
            const tickCandidates = [.01, .02, .05, .1, .2, .5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600];
            const tickStep = tickCandidates.find(value => value >= targetTickSeconds) || Math.ceil(targetTickSeconds / 3600) * 3600;
            const firstTick = Math.ceil(viewStart / tickStep) * tickStep;
            const formatTick = seconds => {
                if (tickStep < 1) return `${seconds.toFixed(tickStep < .1 ? 2 : 1)}s`;
                if (seconds < 60) return `${Math.round(seconds)}s`;
                const minutes = Math.floor(seconds / 60), remaining = Math.round(seconds % 60);
                return remaining ? `${minutes}:${String(remaining).padStart(2, "0")}` : `${minutes}m`;
            };
            context.fillStyle = "#14171b"; context.fillRect(0, waveformHeight, width, scaleHeight);
            context.strokeStyle = "#4a5058"; context.lineWidth = Math.max(1, ratio);
            context.beginPath(); context.moveTo(0, waveformHeight + .5 * ratio); context.lineTo(width, waveformHeight + .5 * ratio); context.stroke();
            context.fillStyle = "#8c8983"; context.font = `${9 * ratio}px Arial,sans-serif`; context.textBaseline = "top";
            for (let seconds = firstTick; seconds <= viewEnd + tickStep * .001; seconds += tickStep) {
                const x = timeToX(seconds);
                if (x < 0 || x > width) continue;
                context.strokeStyle = "#75726c"; context.beginPath(); context.moveTo(x, waveformHeight); context.lineTo(x, waveformHeight + 5 * ratio); context.stroke();
                const label = formatTick(seconds);
                const labelWidth = context.measureText(label).width;
                const labelX = Math.max(2 * ratio, Math.min(width - labelWidth - 2 * ratio, x - labelWidth / 2));
                context.fillText(label, labelX, waveformHeight + 7 * ratio);
            }
            if (previewPlaying && previewAudio) {
                const playheadX = timeToX(previewAudio.currentTime);
                if (playheadX >= 0 && playheadX <= width) {
                    context.strokeStyle = "#fff"; context.lineWidth = Math.max(1, ratio);
                    context.beginPath(); context.moveTo(playheadX, 0); context.lineTo(playheadX, waveformHeight); context.stroke();
                }
            }
        };
        const updateTimes = () => {
            startValue.textContent = formatTrimTime(start);
            endValue.textContent = formatTrimTime(end);
            durationValue.textContent = `${(end - start).toFixed(3)} s`;
            drawWaveform();
        };
        const pointerSeconds = event => {
            const rect = waveWrap.getBoundingClientRect();
            return Math.max(0, Math.min(buffer.duration, viewStart + (event.clientX - rect.left) / rect.width * (viewEnd - viewStart)));
        };
        const startPreview = async () => {
            if (!previewAudio || end <= start) return;
            previewAudio.currentTime = start;
            try {
                await previewAudio.play();
                previewPlaying = true; preview.innerHTML = previewIcon(true);
                const animatePlayhead = () => {
                    if (!previewPlaying || !previewAudio) return;
                    if (previewAudio.currentTime >= end) { stopPreview(); return; }
                    const span = viewEnd - viewStart;
                    if (previewAudio.currentTime < viewStart || previewAudio.currentTime > viewEnd) {
                        viewStart = Math.max(0, Math.min(buffer.duration - span, previewAudio.currentTime - span * .08));
                        viewEnd = Math.min(buffer.duration, viewStart + span);
                    }
                    drawWaveform();
                    playheadFrame = requestAnimationFrame(animatePlayhead);
                };
                animatePlayhead();
            } catch {}
        };
        const togglePreview = () => previewPlaying ? stopPreview() : startPreview();
        waveWrap.onwheel = event => {
            if (!buffer) return;
            event.preventDefault(); event.stopPropagation();
            const rect = waveWrap.getBoundingClientRect();
            const anchorRatio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
            const anchorTime = viewStart + anchorRatio * (viewEnd - viewStart);
            const currentSpan = viewEnd - viewStart;
            const minimumSpan = Math.min(buffer.duration, Math.max(.25, (end - start) / 20));
            const nextSpan = Math.max(minimumSpan, Math.min(buffer.duration, currentSpan * (event.deltaY < 0 ? .8 : 1.25)));
            viewStart = Math.max(0, Math.min(buffer.duration - nextSpan, anchorTime - nextSpan * anchorRatio));
            viewEnd = viewStart + nextSpan;
            drawWaveform();
        };
        waveWrap.onpointerdown = event => {
            if (!buffer) return;
            event.preventDefault();
            if (event.button === 1) {
                dragging = "view";
                panOriginX = event.clientX;
                panViewStart = viewStart;
                waveWrap.classList.add("mxv-trim-panning");
                waveWrap.setPointerCapture(event.pointerId);
                return;
            }
            if (event.button !== 0) return;
            const wasPlaying = previewPlaying;
            const seconds = pointerSeconds(event);
            const hitSeconds = Math.max((viewEnd - viewStart) * 10 / Math.max(1, waveWrap.clientWidth), .03);
            if (Math.abs(seconds - start) <= hitSeconds) dragging = "start";
            else if (Math.abs(seconds - end) <= hitSeconds) dragging = "end";
            else if (seconds > start && seconds < end) dragging = "selection";
            else dragging = Math.abs(seconds - start) < Math.abs(seconds - end) ? "start" : "end";
            dragOriginX = seconds; dragStart = start; dragEnd = end;
            pointerDownX = event.clientX; selectionMoved = false;
            waveWrap.dataset.wasPlaying = wasPlaying ? "1" : "0";
            waveWrap.setPointerCapture(event.pointerId);
        };
        waveWrap.onpointermove = event => {
            if (!buffer || !dragging) return;
            if (dragging === "view") {
                const span = viewEnd - viewStart;
                const deltaSeconds = -(event.clientX - panOriginX) / Math.max(1, waveWrap.clientWidth) * span;
                viewStart = Math.max(0, Math.min(buffer.duration - span, panViewStart + deltaSeconds));
                viewEnd = viewStart + span;
                drawWaveform();
                return;
            }
            const seconds = pointerSeconds(event);
            if (dragging === "selection" && !selectionMoved) {
                if (Math.abs(event.clientX - pointerDownX) < 4) return;
                selectionMoved = true;
            }
            if (dragging === "start") start = Math.max(0, Math.min(end - minSelection, seconds));
            else if (dragging === "end") end = Math.min(buffer.duration, Math.max(start + minSelection, seconds));
            else {
                const length = dragEnd - dragStart;
                const nextStart = Math.max(0, Math.min(buffer.duration - length, dragStart + seconds - dragOriginX));
                start = nextStart; end = nextStart + length;
            }
            updateTimes();
        };
        const finishDrag = event => {
            if (dragging === "view") {
                dragging = null;
                waveWrap.classList.remove("mxv-trim-panning");
                try { waveWrap.releasePointerCapture(event.pointerId); } catch {}
                return;
            }
            const clickedSelection = dragging === "selection" && !selectionMoved;
            const wasPlaying = waveWrap.dataset.wasPlaying === "1";
            delete waveWrap.dataset.wasPlaying;
            dragging = null; try { waveWrap.releasePointerCapture(event.pointerId); } catch {}
            if (clickedSelection) wasPlaying ? stopPreview() : startPreview();
        };
        waveWrap.onpointerup = finishDrag; waveWrap.onpointercancel = finishDrag;
        preview.onclick = () => togglePreview();
        syncDuration.onclick = () => {
            if (!buffer) return;
            stopPreview();
            const target = Math.max(0, Number(durationWidget?.value) || 5);
            if (buffer.duration <= target) { start = 0; end = buffer.duration; }
            else {
                start = Math.max(0, Math.min(start, buffer.duration - target));
                end = Math.min(buffer.duration, start + target);
            }
            updateTimes();
        };
        save.onclick = async () => {
            if (!buffer) return;
            save.disabled = true; cancel.disabled = true; preview.disabled = true; save.textContent = `${t("Saving")}…`;
            stopPreview();
            try {
                entry.trimStart = start;
                entry.trimEnd = end;
                entry.originalDuration = buffer.duration;
                media.set(slot, entry);
                persistState();
                close();
                render();
            } catch (error) {
                console.error("[MiniMax H3 Integration]", error);
                alert(`${t("Audio trim failed")}: ${error.message || error}`);
                save.disabled = false; cancel.disabled = false; preview.disabled = false; save.textContent = t("Save");
            }
        };
        try {
            buffer = await decodeAudioFile(sourceName);
            if (!document.body.contains(overlay)) return;
            const targetDuration = Math.max(0, Number(durationWidget?.value) || 5);
            start = Math.max(0, Math.min(buffer.duration, Number.isFinite(Number(entry.trimStart)) ? Number(entry.trimStart) : 0));
            const savedEnd = Number(entry.trimEnd);
            end = Number.isFinite(savedEnd) && savedEnd > start ? Math.min(buffer.duration, savedEnd) : Math.min(buffer.duration, targetDuration);
            if (end - start < minSelection) { start = 0; end = Math.min(buffer.duration, Math.max(minSelection, targetDuration)); }
            previewAudio = new Audio(fileUrl(sourceName));
            previewAudio.preload = "auto";
            previewAudio.ontimeupdate = () => { if (previewPlaying && previewAudio.currentTime >= end) stopPreview(); };
            previewAudio.onended = () => stopPreview();
            viewStart = 0; viewEnd = buffer.duration;
            loading.remove(); preview.disabled = false; save.disabled = false; updateTimes();
        } catch (error) {
            loading.textContent = `${t("Audio decoding failed")}: ${error.message || error}`;
            console.error("[MiniMax H3 Integration]", error);
        }
    }
    let suppressMediaClickUntil = 0;
    function canReorderMedia(sourceSlot, targetSlot) {
        if (!sourceSlot || !targetSlot || sourceSlot === targetSlot) return false;
        const source = media.get(sourceSlot);
        const target = media.get(targetSlot);
        if (!source || !target) return false;
        if (state.mode === "text_keyframes") {
            return [sourceSlot, targetSlot].every(item => item === "first_frame" || item === "last_frame")
                && source.kind === "image" && target.kind === "image";
        }
        const reserved = new Set(["first_frame", "last_frame", "hybrid_audio"]);
        return !reserved.has(sourceSlot) && !reserved.has(targetSlot) && source.kind === target.kind;
    }
    function promptMediaIdentityOrder() {
        if (state.mode === "text_keyframes") {
            return {
                picture: ["first_frame", "last_frame"].map(mediaEntryForOrder).filter(entry => entry?.kind === "image"),
                video: [],
                audio: mediaEntryForOrder("hybrid_audio") ? [mediaEntryForOrder("hybrid_audio")] : [],
            };
        }
        const ordered = kind => mediaSlots.map(slot => [slot, mediaEntryForOrder(slot)])
            .filter(([slot, entry]) => !["first_frame", "last_frame", "hybrid_audio"].includes(slot) && entry?.kind === kind)
            .sort((a, b) => a[0].localeCompare(b[0]))
            .map(([, entry]) => entry);
        const orderedAudio = [
            ...videoSlots.map(slot => mediaEntryForOrder(slot)).filter(entry => entry?.kind === "video" && !entry.muted),
            ...audioSlots.slice(1).map(slot => mediaEntryForOrder(slot)).filter(entry => entry?.kind === "audio"),
        ];
        return {
            picture: ordered("image"),
            video: ordered("video"),
            audio: orderedAudio,
        };
    }
    function applyPromptMediaMappings(mappings) {
        if (!mappings.size) return;
        let value = String(prompt.value || "");
        const matches = promptMediaMatches(value);
        for (let index = matches.length - 1; index >= 0; index--) {
            const match = matches[index];
            const key = `${match.type}:${match.ordinal}`;
            if (!mappings.has(key)) continue;
            const replacementOrdinal = mappings.get(key);
            const replacement = replacementOrdinal == null ? "" : match.raw.replace(/\d+/, String(replacementOrdinal));
            value = value.slice(0, match.index) + replacement + value.slice(match.index + match.raw.length);
        }
        if (value === prompt.value) return;
        prompt.value = value;
        promptByMode[state.mode] = value;
        setPromptWidget(node, value);
    }
    function remapPromptAfterMediaChange(before, removeMissing = false) {
        const after = promptMediaIdentityOrder();
        const mappings = new Map();
        for (const type of ["picture", "video", "audio"]) {
            before[type].forEach((entry, index) => {
                const nextIndex = after[type].indexOf(entry);
                if (nextIndex >= 0 && nextIndex !== index) mappings.set(`${type}:${index + 1}`, nextIndex + 1);
                else if (nextIndex < 0 && removeMissing) mappings.set(`${type}:${index + 1}`, null);
            });
        }
        applyPromptMediaMappings(mappings);
    }
    function mediaEntryIsReferenced(beforeOrder, entry) {
        if (!entry || !prompt.value) return false;
        const type = entry.kind === "image" ? "picture" : entry.kind;
        const ordinal = beforeOrder[type]?.indexOf(entry);
        if (ordinal == null || ordinal < 0) return false;
        return promptMediaMatches(prompt.value).some(match => match.type === type && match.ordinal === ordinal + 1);
    }
    function remapPromptForMediaOrder(assignments, kind) {
        const entryAt = slot => assignments.has(slot) ? assignments.get(slot) : media.get(slot);
        const orderedKindSlots = targetKind => {
            if (targetKind === "image" && state.mode === "text_keyframes") {
                return ["first_frame", "last_frame"];
            }
            if (state.mode === "all_reference") {
                return targetKind === "image" ? imageSlots.slice(2)
                    : targetKind === "video" ? videoSlots : [];
            }
            return [...media.keys()]
                .filter(slot => !["first_frame", "last_frame", "hybrid_audio"].includes(slot))
                .filter(slot => mediaEntryForOrder(slot)?.kind === targetKind)
                .sort((a, b) => a.localeCompare(b));
        };
        const orderedAudioSlots = getter => {
            const videos = (state.mode === "all_reference" ? videoSlots : [...media.keys()])
                .filter(slot => getter(slot)?.kind === "video" && !getter(slot).muted)
                .sort((a, b) => a.localeCompare(b));
            const audios = (state.mode === "all_reference" ? audioSlots.slice(1) : [...media.keys()])
                .filter(slot => getter(slot)?.kind === "audio" && slot !== "hybrid_audio")
                .sort((a, b) => a.localeCompare(b));
            return [...videos, ...audios];
        };
        const mappings = new Map();
        const mapIdentityOrdinals = (type, beforeSlots, afterSlots, beforeGetter, afterGetter) => {
            beforeSlots.forEach((slot, index) => {
                const entry = beforeGetter(slot);
                if (!entry || entry.vacant) return;
                const nextIndex = afterSlots.findIndex(candidate => afterGetter(candidate) === entry);
                if (nextIndex >= 0 && nextIndex !== index) mappings.set(`${type}:${index + 1}`, nextIndex + 1);
            });
        };
        if (kind === "image" || kind === "video") {
            const slots = orderedKindSlots(kind);
            mapIdentityOrdinals(
                kind === "image" ? "picture" : "video",
                slots, slots, mediaEntryForOrder, entryAt,
            );
        }
        if (kind === "video" || kind === "audio") {
            const slots = orderedAudioSlots(mediaEntryForOrder);
            mapIdentityOrdinals(
                "audio",
                slots, slots, mediaEntryForOrder, entryAt,
            );
        }
        applyPromptMediaMappings(mappings);
    }
    function reorderMediaSlots(sourceSlot, targetSlot) {
        if (!canReorderMedia(sourceSlot, targetSlot)) return false;
        const source = media.get(sourceSlot);
        const driveSelection = captureDriveAudioSelection();
        const assignments = new Map();
        if (state.mode === "text_keyframes") {
            assignments.set(sourceSlot, media.get(targetSlot));
            assignments.set(targetSlot, source);
        } else {
            const slots = [...media.entries()]
                .filter(([slot, entry]) => !["first_frame", "last_frame", "hybrid_audio"].includes(slot) && entry?.kind === source.kind)
                .map(([slot]) => slot)
                .sort((a, b) => a.localeCompare(b));
            const sourceIndex = slots.indexOf(sourceSlot);
            const targetIndex = slots.indexOf(targetSlot);
            if (sourceIndex < 0 || targetIndex < 0) return false;
            const entries = slots.map(slot => media.get(slot));
            const [moved] = entries.splice(sourceIndex, 1);
            entries.splice(targetIndex, 0, moved);
            slots.forEach((slot, index) => assignments.set(slot, entries[index]));
        }
        remapPromptForMediaOrder(assignments, source.kind);
        for (const [slot, entry] of assignments) {
            media.set(slot, entry);
            setMediaWidget(node, slot, entry.name);
        }
        restoreDriveAudioSelection(driveSelection);
        refreshPromptMediaPreviews(...assignments.values());
        persistState();
        render(true);
        return true;
    }
    function installMediaLongPressReorder(item, slot) {
        item.onpointerdown = event => {
            if (event.target.closest("button") || event.button !== 0 || !event.isPrimary) return;
            event.preventDefault();
            event.stopPropagation();
            const originX = event.clientX;
            const originY = event.clientY;
            let active = false;
            let target = null;
            let timer = window.setTimeout(() => {
                if (!item.isConnected) return;
                active = true;
                suppressMediaClickUntil = Date.now() + 600;
                item.classList.add("mxv-reorder-source");
                try { item.setPointerCapture(event.pointerId); } catch {}
            }, 350);
            const setTarget = next => {
                if (target === next) return;
                target?.classList.remove("mxv-reorder-target");
                target = next;
                target?.classList.add("mxv-reorder-target");
            };
            const move = moveEvent => {
                if (!active) {
                    if (Math.hypot(moveEvent.clientX - originX, moveEvent.clientY - originY) > 8) {
                        clearTimeout(timer);
                        timer = 0;
                    }
                    return;
                }
                moveEvent.preventDefault();
                moveEvent.stopPropagation();
                const candidate = document.elementFromPoint(moveEvent.clientX, moveEvent.clientY)?.closest?.(".mxv-card");
                const candidateSlot = candidate?.dataset?.mxvDropSlot;
                setTarget(candidate && root.contains(candidate) && canReorderMedia(slot, candidateSlot) ? candidate : null);
            };
            const finish = finishEvent => {
                if (timer) clearTimeout(timer);
                window.removeEventListener("pointermove", move, true);
                window.removeEventListener("pointerup", finish, true);
                window.removeEventListener("pointercancel", finish, true);
                item.classList.remove("mxv-reorder-source");
                target?.classList.remove("mxv-reorder-target");
                try { item.releasePointerCapture(event.pointerId); } catch {}
                if (!active) return;
                finishEvent.preventDefault();
                finishEvent.stopPropagation();
                suppressMediaClickUntil = Date.now() + 600;
                const targetSlot = target?.dataset?.mxvDropSlot;
                if (targetSlot) reorderMediaSlots(slot, targetSlot);
            };
            window.addEventListener("pointermove", move, true);
            window.addEventListener("pointerup", finish, true);
            window.addEventListener("pointercancel", finish, true);
        };
    }
    function card(slot, entry, square = true) {
        const item = make("div"); item.className = "mxv-card"; if (!square) item.style.aspectRatio = "16/9"; if (slot === "hybrid_audio") { item.classList.add("mxv-audio-card"); item.style.aspectRatio = "auto"; }
        item.dataset.mxvDropSlot = slot;
        const reorderIndicator = make("div", {}, "✥"); reorderIndicator.className = "mxv-reorder-indicator"; item.appendChild(reorderIndicator);
        if (entry.kind === "image") { const img = make("img"); img.src = fileUrl(entry.name); img.draggable = false; item.appendChild(img); }
        else if (entry.kind === "video") { const video = make("video"); video.src = fileUrl(entry.name); video.muted = !!entry.muted; video.preload = "metadata"; video.draggable = false; item.appendChild(video); const play = make("button", {}, "▶"); play.className = "mxv-play"; play.onclick = e => { e.stopPropagation(); video.muted = !!entry.muted; video.paused ? video.play() : video.pause(); }; item.appendChild(play); }
        else {
            item.appendChild(make("div", { height: "100%", display: "flex", alignItems: "center", justifyContent: "center", color: "#a09d97", fontSize: "22px" }, "♫"));
            const audio = new Audio(fileUrl(entry.name));
            const controls = make("div"); controls.className = "mxv-media-controls";
            const toggle = make("button"); toggle.className = "mxv-media-toggle";
            const time = make("span", {}, "00 s"); time.className = "mxv-media-time";
            const selectionStart = Math.max(0, Number(entry.trimStart) || 0);
            const savedEnd = Number(entry.trimEnd);
            const selectionEnd = () => Number.isFinite(savedEnd) && savedEnd > selectionStart ? Math.min(audio.duration || savedEnd, savedEnd) : audio.duration;
            const selectionDuration = () => Math.max(0, selectionEnd() - selectionStart);
            const setPlaying = playing => { toggle.innerHTML = mediaIcon(playing); };
            const refresh = () => { const remaining = audio.paused ? selectionDuration() : Math.max(0, selectionEnd() - audio.currentTime); time.textContent = `${formatAudioMediaTime(remaining)} s`; };
            const stopSelection = () => {
                audio.pause(); audio.currentTime = selectionStart; setPlaying(false); refresh();
                if (activeMedia?.media === audio) activeMedia = null;
            };
            const start = () => {
                if (activeMedia && activeMedia.media !== audio) stopActiveMedia();
                audio.currentTime = selectionStart;
                activeMedia = { media: audio, setPlaying, resetTime: selectionStart, onStop: refresh };
                audio.play().then(() => setPlaying(true)).catch(() => { setPlaying(false); activeMedia = null; });
            };
            setPlaying(false);
            toggle.onclick = e => { e.stopPropagation(); audio.paused ? start() : stopSelection(); };
            audio.addEventListener("loadedmetadata", refresh);
            audio.addEventListener("timeupdate", () => { if (!audio.paused && audio.currentTime >= selectionEnd()) stopSelection(); else refresh(); });
            audio.addEventListener("ended", stopSelection);
            controls.append(toggle, time); item.appendChild(controls);
            const trim = make("button", {}, "✂"); trim.className = "mxv-audio-trim-button"; trim.title = t("Trim audio");
            trim.onclick = event => { event.stopPropagation(); openAudioTrimEditor(slot, entry); };
            item.appendChild(trim);
        }
        if (entry.kind === "video") {
            const sound = make("button", {}, entry.muted ? String.fromCodePoint(0x1F507) : String.fromCodePoint(0x1F50A));
            sound.className = "mxv-sound";
            sound.title = t(entry.muted ? "Unmute video" : "Mute video");
            sound.onclick = e => {
                e.stopPropagation();
                const previousAudioCount = state.mode === "text_keyframes"
                    ? (media.has("hybrid_audio") ? 1 : 0) : allReferenceAudioCount();
                entry.muted = !entry.muted;
                syncAudioModeAfterMediaChange(slot, previousAudioCount);
                refreshPromptMediaPreviews(); persistState(); render();
            };
            item.appendChild(sound);
            const video = item.querySelector("video");
            const oldPlay = item.querySelector(".mxv-play");
            if (oldPlay) oldPlay.style.display = "none";
            const controls = make("div"); controls.className = "mxv-media-controls";
            const toggle = make("button"); toggle.className = "mxv-media-toggle";
            const time = make("span", {}, "00 s"); time.className = "mxv-media-time";
            const setPlaying = playing => { toggle.innerHTML = mediaIcon(playing); };
            const refresh = () => { const remaining = video.paused ? video.duration : Math.max(0, video.duration - video.currentTime); time.textContent = `${formatMediaTime(remaining)} s`; };
            const start = () => { if (activeMedia && activeMedia.media !== video) stopActiveMedia(); activeMedia = { media: video, setPlaying }; video.muted = !!entry.muted; video.play().catch(() => {}); setPlaying(true); };
            setPlaying(false);
            toggle.onclick = e => { e.stopPropagation(); video.paused ? start() : stopActiveMedia(); };
            video.addEventListener("loadedmetadata", refresh); video.addEventListener("timeupdate", refresh); video.addEventListener("ended", () => { refresh(); setPlaying(false); if (activeMedia?.media === video) activeMedia = null; });
            controls.append(toggle, time); item.appendChild(controls);
        }
        item.appendChild(make("div", {}, `${labelFor(slot, entry.kind)}: ${entry.name}`)).className = "mxv-card-name";
        const remove = make("button", {}, "×"); remove.className = "mxv-remove"; remove.onclick = e => {
            e.stopPropagation();
            const driveSelection = captureDriveAudioSelection();
            const previousAudioCount = state.mode === "text_keyframes"
                ? (media.has("hybrid_audio") ? 1 : 0) : allReferenceAudioCount();
            const beforeOrder = promptMediaIdentityOrder();
            const removed = media.get(slot);
            const hasPromptMediaTags = promptMediaMatches(prompt.value).length > 0;
            const preserveOrdinal = hasPromptMediaTags && mediaEntryIsReferenced(beforeOrder, removed);
            if (removed && preserveOrdinal) {
                vacantMediaSlots.set(slot, { kind: removed.kind, muted: !!removed.muted });
                vacantMediaEntries.delete(slot);
            } else if (!hasPromptMediaTags) {
                // With no media tags in the prompt, there is no user-visible
                // ordinal to preserve. Restore the original compact numbering.
                vacantMediaSlots.clear();
                vacantMediaEntries.clear();
            } else {
                vacantMediaSlots.delete(slot);
                vacantMediaEntries.delete(slot);
            }
            media.delete(slot);
            setMediaWidget(node, slot, "");
            if (!preserveOrdinal) remapPromptAfterMediaChange(beforeOrder, false);
            restoreDriveAudioSelection(driveSelection);
            syncAudioModeAfterMediaChange(slot, previousAudioCount);
            refreshPromptMediaPreviews(removed);
            persistState();
            render(false, true);
        }; item.appendChild(remove);
        let clickTimer;
        installMediaLongPressReorder(item, slot);
        item.onclick = e => { if (e.target.closest("button") || Date.now() < suppressMediaClickUntil) return; e.stopPropagation(); clearTimeout(clickTimer); clickTimer = setTimeout(() => insertTag(entry.kind, slot), 220); };
        item.ondblclick = e => { if (e.target.closest("button") || Date.now() < suppressMediaClickUntil) return; e.stopPropagation(); clearTimeout(clickTimer); if (entry.kind === "video") insertVideoAudioTag(slot); };
        item.onpointerenter = () => { hoverPasteSlot = slot; };
        item.onpointerleave = () => { if (hoverPasteSlot === slot) hoverPasteSlot = undefined; };
        item.ondragover = e => { e.preventDefault(); e.stopPropagation(); };
        item.ondrop = e => { e.preventDefault(); e.stopPropagation(); accept(e.dataTransfer.files, slot); };
        return item;
    }
    function choose(slot) { const input = document.createElement("input"); input.type = "file"; input.multiple = !slot; input.accept = slot === "first_frame" || slot === "last_frame" ? "image/*" : "image/*,video/*,audio/*"; input.onchange = () => accept(input.files, slot); input.click(); }
    function addDrop(slot = null, referenceEmpty = false) {
        let title = t("+ Add media");
        let subtitle = "";
        let icon = "📤︎";
        if (slot === "first_frame") { title = t("First frame"); subtitle = t("First and last frames empty means text-to-video"); }
        else if (slot === "last_frame") { title = t("Last frame"); subtitle = t("First and last frames empty means text-to-video"); }
        else if (slot === "hybrid_audio") { title = t("Reference audio"); subtitle = t("Optional"); icon = "♫"; }
        else if (referenceEmpty) { title = t("Reference media"); subtitle = t("Images x9 · Videos x3 (mp4/mov) · Audios x3 (mp3/wav/flac...)"); }
        const d = make("div"); d.className = "mxv-drop"; if (slot === "hybrid_audio") d.classList.add("mxv-audio-drop"); if (referenceEmpty) d.classList.add("mxv-reference-empty");
        d.dataset.mxvDropSlot = slot || "";
        if (slot === "first_frame" || slot === "last_frame") {
            const row = make("div"); row.className = "mxv-drop-title-row";
            row.appendChild(make("span", {}, icon)).className = "mxv-drop-icon";
            row.appendChild(make("span", {}, title)).className = "mxv-drop-title";
            row.appendChild(make("span", {}, `· ${t("Optional")}`)).className = "mxv-optional";
            d.appendChild(row);
            d.appendChild(make("div", {}, subtitle)).className = "mxv-drop-subtitle";
        } else if (slot === "hybrid_audio") {
            const row = make("div"); row.className = "mxv-drop-title-row";
            row.appendChild(make("span", {}, icon)).className = "mxv-drop-icon";
            row.appendChild(make("span", {}, title)).className = "mxv-drop-title";
            row.appendChild(make("span", {}, `· ${t("Optional")}`)).className = "mxv-optional";
            d.appendChild(row);
        } else {
            const row = make("div"); row.className = "mxv-drop-title-row";
            row.appendChild(make("span", {}, icon)).className = "mxv-drop-icon";
            row.appendChild(make("span", {}, title)).className = "mxv-drop-title";
            d.appendChild(row);
            if (subtitle) d.appendChild(make("div", {}, subtitle)).className = "mxv-drop-subtitle";
        }
        d.onclick = () => choose(slot); d.onpointerenter = () => { hoverPasteSlot = slot; }; d.onpointerleave = () => { if (hoverPasteSlot === slot) hoverPasteSlot = undefined; }; d.ondragover = e => { e.preventDefault(); e.stopPropagation(); }; d.ondrop = e => { e.preventDefault(); e.stopPropagation(); accept(e.dataTransfer.files, slot); }; return d;
    }
    let renderedMediaSignature = null;
    function render(preserveAdvancedSettings = false, preservePromptTags = false) {
        const signature = JSON.stringify([...media].map(([slot, entry]) => [slot, entry.name]));
        if (renderedMediaSignature !== null && signature !== renderedMediaSignature) {
            aiMode.value = "auto"; setWidget(node, "ai_mode", "auto");
        }
        renderedMediaSignature = signature;
        if (!preservePromptTags) normalizePromptTagFormat();
        updateAdvancedVisibility(preserveAdvancedSettings);
        root.querySelector(".mxv-dynamic")?.remove(); const box = make("div"); box.className = "mxv-box mxv-dynamic";
        if (state.mode === "text_keyframes") {
            const grid = make("div"); grid.className = "mxv-keygrid";
            const selectedMode = aiMode.value;
            const slots = selectedMode === "t2va" ? [] : selectedMode === "i2va" ? ["first_frame"] : selectedMode === "l2va" ? ["last_frame"] : ["first_frame", "last_frame"];
            for (const s of ["first_frame", "last_frame"]) {
                if (slots.includes(s) || media.has(s)) grid.appendChild(media.has(s) ? card(s, media.get(s), false) : addDrop(s));
            }
            grid.appendChild(media.has("hybrid_audio") ? card("hybrid_audio", media.get("hybrid_audio"), false) : addDrop("hybrid_audio"));
            box.appendChild(grid);
        } else {
            const grid = make("div"); grid.className = "mxv-grid"; const entries = [...media.entries()].filter(([slot]) => !["first_frame", "last_frame", "hybrid_audio"].includes(slot)).sort((a,b) => typeOrder[a[1].kind] - typeOrder[b[1].kind] || a[0].localeCompare(b[0]));
            if (!entries.length) grid.appendChild(addDrop(null, true));
            else { entries.forEach(([s,e]) => grid.appendChild(card(s,e))); grid.appendChild(addDrop()); }
            if (uploadNotice) grid.appendChild(make("div", {}, uploadNotice)).className = "mxv-limit";
            box.appendChild(grid);
        }
    root.insertBefore(box, promptWrap);
    if (!preservePromptTags) repairLegacyReferenceTags();
    refreshTaskType(); refreshAdaptiveRatio(); syncLayout();
    }
    function limitText(kind) {
        return kind === "image" ? t("Up to 9 images") : kind === "video" ? t("Up to 3 videos") : t("Up to 3 audios");
    }
    function filesFromPasteEvent(event) {
        const files = [];
        const items = event.clipboardData?.items || [];
        for (const item of items) {
            if (item.kind !== "file") continue;
            const file = item.getAsFile();
            if (!file) continue;
            const kind = kindOf(file);
            if (kind === "image" || kind === "video" || kind === "audio") files.push(file);
        }
        return files;
    }
    const onPaste = event => {
        const promptActive = document.activeElement === prompt || event.composedPath?.().includes(prompt);
        if (promptActive && !prompt.readOnly) {
            event.preventDefault(); event.stopImmediatePropagation();
            const text = event.clipboardData?.getData("text/plain") || "";
            const before = promptSnapshot();
            const [start, end] = selectionOffsets();
            prompt.setRangeText(text, start, end, "end");
            pushPromptUndo(before);
            promptByMode[state.mode] = prompt.value; setPromptWidget(node, prompt.value); persistState();
            return;
        }
        if (hoverPasteSlot === undefined) return;
        event.preventDefault(); event.stopImmediatePropagation();
        const files = filesFromPasteEvent(event);
        if (files.length) accept(files, hoverPasteSlot);
    };
    window.addEventListener("paste", onPaste, true);
    async function accept(files, preferredSlot) {
        uploadNotice = "";
        const beforeOrder = promptMediaIdentityOrder();
        const driveSelection = captureDriveAudioSelection();
        const changedEntries = [];
        for (const file of files || []) {
            const kind = kindOf(file); if (!kind) continue;
            const slot = preferredSlot || nextSlot(kind);
            if (!slot) { uploadNotice = limitText(kind); continue; }
            const expectedKind = slot === "hybrid_audio" || slot.startsWith("ref_audio_") ? "audio" : slot.startsWith("ref_video_") ? "video" : "image";
            if (kind !== expectedKind) { preferredSlot = null; continue; }
            const existing = media.get(slot);
            if (existing && existing.kind !== kind) continue;
            try {
                const previousAudioCount = state.mode === "text_keyframes"
                    ? (media.has("hybrid_audio") ? 1 : 0) : allReferenceAudioCount();
                const name = await uploadFile(file);
                const entry = kind === "audio" ? { name, kind, trimStart: 0, trimEnd: null } : { name, kind };
                if (existing) changedEntries.push(existing);
                vacantMediaSlots.delete(slot);
                vacantMediaEntries.delete(slot);
                media.set(slot, entry); setMediaWidget(node, slot, name);
                syncAudioModeAfterMediaChange(slot, previousAudioCount);
                changedEntries.push(entry);
            } catch (e) { console.error("[MiniMax H3 Integration]", e); }
            preferredSlot = null;
        }
        if (changedEntries.length) {
            remapPromptAfterMediaChange(beforeOrder, false);
            restoreDriveAudioSelection(driveSelection, true);
        }
        if (changedEntries.length) refreshPromptMediaPreviews(...changedEntries);
        persistState();
        render();
    }
    const captureMaterialDrop = event => {
        const target = event.target instanceof Element ? event.target : null;
        const materialArea = target?.closest?.(".mxv-card, .mxv-drop, .mxv-dynamic");
        if (!materialArea || !root.contains(materialArea)) return;
        if (!event.dataTransfer?.types?.includes?.("Files")) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (event.type !== "drop") return;
        const slotElement = target.closest("[data-mxv-drop-slot]");
        const preferredSlot = slotElement?.dataset?.mxvDropSlot || null;
        accept(event.dataTransfer.files, preferredSlot);
    };
    window.addEventListener("dragenter", captureMaterialDrop, true);
    window.addEventListener("dragover", captureMaterialDrop, true);
    window.addEventListener("drop", captureMaterialDrop, true);
    function switchMode(nextMode, fromAiMode = false) {
        if (!fromAiMode) { aiMode.value = "auto"; setWidget(node, "ai_mode", "auto"); }
        promptByMode[state.mode] = prompt.value;
        state.mode = nextMode;
        optimizerBefore = optimizerBeforeByMode[state.mode] ?? null;
        prompt.value = cleanPrompt(promptByMode[state.mode]);
        renderPromptHighlights();
        setPromptWidget(node, prompt.value);
        resetPrompt.classList.toggle("visible", optimizerBefore != null);
        setWidget(node, "main_mode", state.mode);
        persistState();
        modeText.classList.toggle("active", state.mode === "text_keyframes");
        modeRef.classList.toggle("active", state.mode === "all_reference");
        updateAdvancedVisibility();
        render();
    }
    modeText.onclick = () => switchMode("text_keyframes");
    modeRef.onclick = () => switchMode("all_reference");
    function refreshSize() {
        dimensions.textContent = canvas(aspectInternalValue(aspect?.value), mp?.value, adaptiveRatio);
        node.setDirtyCanvas(true, true);
    }
    normalizeAspectWidget(node);
    installAspectDisplay(node);
    aspect && (aspect.callback = ((old) => function(v) {
        const internalValue = ASPECT_INTERNAL_VALUES.includes(v) ? v : aspectInternalValue(v);
        old?.call(this, internalValue);
        this.value = internalValue;
        this._mxvAspectInternal = internalValue;
        refreshSize();
        refreshAdaptiveRatio();
    })(aspect.callback));
    mp && (mp.callback = ((old) => function(v) { old?.call(this,v); refreshSize(); })(mp.callback));
    setWidget(node, "main_mode", state.mode);
    modeText.classList.toggle("active", state.mode === "text_keyframes");
    modeRef.classList.toggle("active", state.mode === "all_reference");
    updateAdvancedVisibility();
    render();
    advanced.open = true;
    aiDetails.open = true;
    for (const hook of ["onAdded", "onConfigure", "onGraphConfigured"]) {
        const old = node[hook];
        node[hook] = function(...args) {
            const configuredState = hook === "onConfigure" ? args[0]?.properties?.[stateKey] : null;
            const result = old?.apply(this, args);
            sanitizeHiddenInputs(this);
            for (const n of [
                "main_mode", "prompt", "task_type", ...mediaSlots,
                "audio_mode", "audio_denoise_strength",
                "drive_audio_ordinal", "ref_image_size", "strict_prompt_tags",
                "gh_state_json", "no_subtitle", "soundscape", "music",
            ]) hideWidget(widget(node,n));
            normalizeIntWidget(node, "drive_audio_ordinal", 1, 0, 6);
            if (hook !== "onAdded") {
                restoringState = true;
                const currentRestoreEpoch = ++restoreEpoch;
                requestAnimationFrame(() => {
                let restored = {};
                try { restored = JSON.parse(configuredState || widget(node, "gh_state_json")?.value || node.properties?.[stateKey] || "{}"); } catch {}
                const restoredMode = restored.mode || widget(node, "main_mode")?.value || "text_keyframes";
                state.mode = restoredMode;
                const restoredLegacyPrompt = cleanPrompt(restored.prompt) || cleanPrompt(widget(node, "prompt")?.value);
                const restoredPrompts = restored.prompts && typeof restored.prompts === "object" ? restored.prompts : null;
                promptByMode.text_keyframes = restoredPrompts && hasOwn(restoredPrompts, "text_keyframes")
                    ? cleanPrompt(restoredPrompts.text_keyframes)
                    : (restoredMode === "text_keyframes" ? restoredLegacyPrompt : "");
                promptByMode.all_reference = restoredPrompts && hasOwn(restoredPrompts, "all_reference")
                    ? cleanPrompt(restoredPrompts.all_reference)
                    : (restoredMode === "all_reference" ? restoredLegacyPrompt : "");
                prompt.value = cleanPrompt(promptByMode[state.mode]);
                renderPromptHighlights();
                setPromptWidget(node, prompt.value);
                optimizerSettings = restored.optimizer || optimizerSettings;
                refreshOptimizerName(); refreshPromptConnection();
                optimizerCache = restored.optimizerCache || null;
                if (restored.audioModeByMode && typeof restored.audioModeByMode === "object") {
                    for (const mode of ["text_keyframes", "all_reference"]) {
                        delete audioModeByMode[mode];
                        delete audioModeAutoByMode[mode];
                        if (restored.audioModeByMode[mode] != null) {
                            audioModeByMode[mode] = restored.audioModeByMode[mode];
                            audioModeAutoByMode[mode] = restored.audioModeAutoByMode?.[mode] !== false ? true : false;
                        }
                    }
                }
                const restoredBeforeByMode = restored.optimizerBeforeByMode && typeof restored.optimizerBeforeByMode === "object"
                    ? restored.optimizerBeforeByMode
                    : { [restoredMode]: restored.optimizerBefore ?? null };
                optimizerBeforeByMode.text_keyframes = restoredBeforeByMode.text_keyframes ?? null;
                optimizerBeforeByMode.all_reference = restoredBeforeByMode.all_reference ?? null;
                optimizerBefore = optimizerBeforeByMode[state.mode] ?? null;
                resetPrompt.classList.toggle("visible", optimizerBefore != null);
                setWidget(node, "main_mode", state.mode);
                media.clear();
                for (const [slot, entry] of restored.media || []) media.set(slot, entry);
                for (const name of mediaSlots) {
                    const value = widget(node, name)?.value;
                    if (!media.has(name) && value && value !== "(none)") media.set(name, { name: value, kind: kindOf({ name: value, type: "" }) });
                    if (value === "(none)") setMediaWidget(node, name, "");
                }
                syncMediaWidgets();
                refreshPromptMediaPreviews();
                const restoredHasContent = Boolean(
                    restored.prompt
                    || restored.prompts?.text_keyframes
                    || restored.prompts?.all_reference
                    || (Array.isArray(restored.media) && restored.media.length),
                );
                if (restoredHasContent && Number.isFinite(Number(restored.height)) && Number(restored.height) <= MAX_RESTORED_NODE_HEIGHT) {
                    userHeight = Number(restored.height);
                }
                modeText.classList.toggle("active", state.mode === "text_keyframes");
                modeRef.classList.toggle("active", state.mode === "all_reference");
                // 完全展开设计：手风琴始终展开，不恢复历史收起状态。
                advanced.open = true;
                aiDetails.open = true;
                // Hidden widgets are restored by LiteGraph after the panel was
                // built; resync toggle visuals so they never contradict the
                // values that actually get submitted.
                syncAiControls();
                for (const name of ["strict_prompt_tags", "no_subtitle", "soundscape", "music"]) {
                    const box = advancedRows.get(name)?.querySelector("input[type=checkbox]");
                    if (box) box.checked = !!widget(node, name)?.value;
                }
                updateAdvancedVisibility();
                render();
                syncLayout(userHeight, true);
                if (currentRestoreEpoch === restoreEpoch) restoringState = false;
                });
            }
            return result;
        };
    }
    const oldDrawForeground = node.onDrawForeground;
    node.onDrawForeground = function(...args) {
        syncAdvancedBackground();
        const result = oldDrawForeground?.apply(this, args);
        drawAspectDisplayOverlay(this, args[0]);
        return result;
    };
    const oldPropertyChanged = node.onPropertyChanged;
    node.onPropertyChanged = function(...args) {
        const result = oldPropertyChanged?.apply(this, args);
        syncAdvancedBackground(true);
        return result;
    };
    // Background synchronization is driven by node draw/property changes.
    // A per-frame style write can cause Nodes 2.0 to remeasure this DOM widget
    // continuously, so it must not participate in the animation loop.
    const oldSerialize = node.onSerialize;
    node.onSerialize = function(...args) {
        sanitizeHiddenInputs(this);
        persistState();
        const result = oldSerialize?.apply(this, args);
        const serializedNode = args[0];
        if (serializedNode) {
            serializedNode.properties = serializedNode.properties || {};
            serializedNode.properties[stateKey] = node.properties[stateKey];
        }
        return result;
    };
    const oldRemoved = node.onRemoved;
    node.onRemoved = function(...args) {
        autoOptimizerHandlers.delete(autoOptimizeBeforeQueue);
        workflowStateHandlers.delete(updateWorkflowState);
        clearInterval(optimizerTimer);
        optimizerAbort?.abort();
        optimizerCompleteAudio?.pause(); optimizerCompleteAudio = null;
        if (optimizerRequestId) api.fetchApi(`${OPTIMIZER_ROUTE}/cancel`, { method: "POST", body: new Blob([JSON.stringify({ request_id: optimizerRequestId })], { type: "application/json" }) }).catch(() => {});
        stopActiveMedia();
        closeActiveTrimEditor?.();
        decodedAudioCache.clear();
        promptHighlightResizeObserver.disconnect();
        window.removeEventListener("dragenter", captureMaterialDrop, true);
        window.removeEventListener("dragover", captureMaterialDrop, true);
        window.removeEventListener("drop", captureMaterialDrop, true);
        window.removeEventListener("wheel", capturePromptWheel, true);
        if (middlePan) stopMiddlePan({ type: "blur", preventDefault() {}, stopImmediatePropagation() {} });
        window.removeEventListener("paste", onPaste, true);
        window.removeEventListener("keydown", capturePromptHistoryKeys, true);
        window.removeEventListener("keyup", releasePromptHistoryKeys, true);
        window.removeEventListener("storage", applyLocale);
        unregisterPromptEditor(prompt);
        return oldRemoved?.apply(this, args);
    };
    const initialRestoreEpoch = restoreEpoch;
    requestAnimationFrame(() => {
        const savedHasContent = Boolean(
            savedState.prompt
            || savedState.prompts?.text_keyframes
            || savedState.prompts?.all_reference
            || (Array.isArray(savedState.media) && savedState.media.length),
        );
        if (savedHasContent && Number.isFinite(Number(savedState.height)) && Number(savedState.height) <= MAX_RESTORED_NODE_HEIGHT) {
            userHeight = Math.max(0, Number(savedState.height));
        }
        advanced.open = true;
        aiDetails.open = true;
        if (node.size?.[0] !== WIDTH || node.size?.[1] !== userHeight) node.setSize([WIDTH, userHeight]);
        syncLayout(userHeight, true);
        refreshPromptConnection();
        if (initialRestoreEpoch === restoreEpoch) {
            restoringState = false;
            persistState();
        }
    });
    return true;
}

app.registerExtension({
    name: "minimax_h3_integration_rh",
    rh: {
        type: "nodes",
        nodes: RH_NODE_IDS,
    },
    async setup() {
        installPromptKeyShield();
        for (const delay of [0, 100, 500, 1200]) setTimeout(() => patchLiteGraphPromptProcessKey(), delay);
        if (typeof app.ensureNodesRegistered !== "function") return;
        await app.ensureNodesRegistered(new Set(RH_NODE_IDS));
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE) return;
    const previous = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function() {
        const result = previous?.apply(this, arguments);
        if (!this._mxvPanelReady && createPanel(this)) this._mxvPanelReady = true;
        return result;
    };
    },
});
