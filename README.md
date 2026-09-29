# MiniMax H3 · RunningHub / ComfyUI 节点

把本项目提示词生成器与 MiniMax H3 视频生成链路整合为**一个**原生 ComfyUI V3 节点：
`MiniMaxH3IntegrationRH`（All-in-One）内置提示词生成（AI / 离线包装 / 整理已有）、条件构建、
双时钟采样与音视频解码。支持 T2VA、I2VA、FL2VA、L2VA、Ref2VA；中文、英文、英文正文加原文对白；
受限扩写、自动时间轴、固定镜头、画面稳定、无字幕、音景、配乐和抗残音开关。
分块采样器与潜空间放大器的实现保留在包内（暂未注册为节点，供后续扩展），细节见包内 `tiled_sampler.py` 与 `latent_upscaler.py`。
工具使用手册与维护文档见 `docs/使用说明.md`、`docs/技术文档.md`；发布流程见 `docs/RELEASE-TO-RUNNINGHUB.md`。

## 安装

要求 Python 3.10+、支持 `comfy_api.latest` 及 MiniMax H3 模型的 ComfyUI。
把发布包解压到 `ComfyUI/custom_nodes/`，使用 **ComfyUI 自己的 Python** 安装：

```sh
python -m pip install -r ComfyUI/custom_nodes/MiniMaxH3-RH-Node/requirements.txt
```

重启后在 `RH/MiniMax H3 Integration` 分类查找节点（仅一个：`MiniMaxH3IntegrationRH`）。
QuickJS 在 Python 进程内执行现有提示词规则，无需 Node.js 或浏览器。
RunningHub 需要平台侧安装本包及依赖；本仓库提供审核/部署用 ZIP，不自动上传或发布。
平台部署和模型可用情况须在目标环境确认。

## 工作流

`examples/` 内置全部五种模式的示例，每个模式一组：`<mode>.workflow.json` 是画布格式（直接导入），
`<mode>.api.json` 是同一图的 API prompt 格式（用于 ComfyUI `/prompt` 的 `prompt` 字段；不是 RunningHub
工作流调用接口的请求体）。

画布示例（`.workflow.json`）已补齐 4 个节点的参数：主节点的提示词直接写进**编辑器**（面板恒以编辑器
内容为准，`prompt_source` 写作 `panel`，文本由引擎整理并校验通过），素材与扩散模型为 `SELECT_YOUR_*`
占位；API 示例（`.api.json`）保留 `offline`/`format` 来源（API 路径不经面板，后端仍支持这三种来源）。

| 示例 | API 示例来源 / 模式 | 节点上需要上传的素材 |
| --- | --- | --- |
| `t2va.*` | offline / t2va | 无 |
| `i2va.*` | offline / i2va | 首帧（first_frame） |
| `fl2va.*` | offline / fl2va | 首帧 + 尾帧 |
| `l2va.*` | offline / l2va | 尾帧（last_frame） |
| `ref2va.*` | format / ref2va | 参考图、参考视频、参考音频（按需） |

`ref2va` 的 API 示例用 `format` 来源整理一份完整的六 section 提示词（画布示例的编辑器里就是同一份文本）；
离线包装不生成六 section，Ref2VA 建议用 AI 生成或对完整六 section 输入选择 format。示例中的
`SELECT_YOUR_*` 是占位文件名（扩散模型、首尾帧、参考素材），运行前替换为实际上传。每张图仅 4 个节点：

1. 在 UNETLoader 选择实际 H3 扩散模型（占位 `SELECT_YOUR_H3_DIFFUSION_MODEL.safetensors`），接入节点 `model`。
2. 在节点上选择已安装的 H3 文本编码器、视频 VAE、音频 VAE；按模式上传素材。
3. 提示词直接写在**编辑器**里（面板固定以编辑器内容为准，`prompt_source` 写入 `panel`）；画布示例已填好
   各模式的完整提示词，可直接改用。`ai` / `offline` / `format` 三种来源仅对 API 调用与直接编辑工作流
   JSON 的用户有效（见 `examples/*.api.json`）。
4. 节点内部完成条件构建 → 双时钟采样 → 音视频解码，输出 `frames` / `audio` → CreateVideo → SaveVideo。
5. 视频以 24fps 合成。`audio` 输出口按音频模式自动选择：锁定原声输出源音轨，否则输出生成音轨。

节点只读取文字与上传素材；它不会自动读取未上传的图片或视频，请在提示词中写明参考内容和标签。
面板保留素材预览与手动优化器；提示词引擎在工作流执行时运行，支持无浏览器的 API 调度。

编辑器自带的无字幕/音景/配乐开关（以及高级选项）用于给手写/生成的提示词追加约束；API 调用使用
`ai/offline/format` 来源时，对应的 `ai_*` 参数组接替这些开关。

## 提示词生成（节点内置）

提示词生成已内置于 All-in-One 节点：API 调用把 `prompt_source` 设为 `ai` / `offline` / `format` 时生效；
面板界面不再提供来源选择（恒为 `panel`，即编辑器内容）。

| 参数 | 用途 |
| --- | --- |
| prompt_source | 面板恒写入 `panel`（用编辑器内容）；API 调用可选 `ai` 在线生成 / `offline` 原文包装 / `format` 整理已有完整 H3 提示词 |
| ai_text | 创意输入或待整理提示词 |
| ai_language | `zh` 正文和对白中文；`en` 全英文；`mixed` 英文正文、对白保留原文 |
| ai_mode | `auto` 随主模式；或固定 t2va/i2va/fl2va/l2va/ref2va |
| ai_strict_validation | 默认开启；不合规结果中止执行。关闭后继续输出文本，报告写明原因 |
| ai_anti_pop | 开启时使用实验性的内联对白；关闭时使用官方 `<d>` 包装 |
| ai_provider | 大模型服务预设：DeepSeek / GLM 智谱 / OpenAI / OpenRouter / 通义千问（DashScope）/ 硅基流动（SiliconFlow）/ 自定义；选定后自动使用预设端点与默认模型 |
| ai_api_key | **由用户自行填写**的大模型 API Key；留空时读取对应服务商的环境变量 |
| ai_endpoint / ai_model | 可覆盖预设端点与模型；选「自定义」时两者必填 |

AI 生成保留原工具的语言和 FL2VA 时间轴纠正，每类最多两次。所有调用共享 ai_timeout 总预算；
等待网络时中断响应受单次读超时限制（最长约 30 秒）。请求失败不回退为离线结果。
提示词引擎按 duration_seconds 生成时间轴，仅支持 2–15 秒；更长时长请直接在编辑器撰写（panel）。
离线模式不翻译，不补全 Ref2VA 主体/保留关系；Ref2VA 建议用 AI 或输入完整六 section 后选择 format。

接入方式：**大模型信息由用户自行填写**——选一个服务商预设、填入自己的
API Key 即可；预设只提供端点与默认模型，可随时用 ai_endpoint / ai_model 覆盖为任意
OpenAI 兼容服务。Key 按服务商对应的环境变量名为兑底：

| ai_provider | 兑底环境变量 | 预设默认模型 |
| --- | --- | --- |
| deepseek | `DEEPSEEK_API_KEY` | deepseek-flash |
| glm | `GLM_API_KEY` | glm-5.3-flash |
| openai | `OPENAI_API_KEY` | gpt-4.1-mini |
| openrouter | `OPENROUTER_API_KEY` | google/gemini-2.5-flash |
| dashscope | `DASHSCOPE_API_KEY` | qwen-plus |
| siliconflow | `SILICONFLOW_API_KEY` | Qwen/Qwen2.5-72B-Instruct |
| custom | `H3_LLM_API_KEY` | （必填 ai_endpoint 与 ai_model） |

也可填写节点 ai_api_key，但该字段会保存到工作流中，分享前应清空。
节点不使用生成器 HTML 里的内置 Key，分发 ZIP 也不包含任何 HTML 文件。

## 模型与能力边界

扩散模型放在 `models/diffusion_models`，文本编码器放在 `models/text_encoders`，
视频和音频 VAE 放在 `models/vae`。默认模型文件名为 MiniMax H3 的常见命名，实际选择取决于安装环境。
包内还包含分块采样器和潜空间放大器的实现（暂未注册为节点，供后续扩展）；细节见包内 `tiled_sampler.py` 与 `latent_upscaler.py`。
本包不会自动下载大模型。真实生成需要兼容模型、足够显存和平台节点安装权限。

## 开发与打包

```sh
# 引擎规则以 prompt_engine/rules.js 为维护真源（直接改该文件）。
# 参考生成器 HTML 维护在上游主项目 MiniMaxH3（本仓库同级目录）：同步时把改动
# 移植进 rules.js（勿整文件重建，会回退仓库维护的引擎修复），必要时更新
# prompt_engine/source.sha256（HTML 以 read_text 归一化换行后 UTF-8 编码的 SHA-256）。
python -m pytest tests -o addopts='' -q        # Python 全量回归（65 用例 + 78 子测试）
node tests/panel-ai-section.cjs                # 面板静态一致性（274 断言）
node tests/panel-generation.cjs                # 面板生成行为（vm 沙箱）
node tests/panel-layout.cjs                    # 面板布局（7 断言）
node tests/language-globalization.cjs          # 引擎规则回归（HTML 取自同级主项目，缺失时 SKIP）
node --check web/js/minimax_h3_integration.js
python scripts/check_comfy_registration.py /path/to/ComfyUI
python scripts/test_allinone_execute.py /path/to/ComfyUI   # execute 行为实测（无需模型权重）
python scripts/build_example_workflow.py
python scripts/package_runninghub.py
```

输出 `dist/MiniMaxH3-RH-Node.zip` 和 SHA-256 文件。打包使用白名单，排除原 HTML、
实验、部署配置及本地凭据。提示词规则以 `prompt_engine/rules.js` 为维护真源，打包时逐字节校验发布包与仓库一致。

授权信息见 LICENSE、NOTICE.md；文档索引见 `docs/`。
