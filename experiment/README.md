# MiniMax H3 片头爆破音 A/B 实验

## 准备（一次性）

1. 把 MiniMax API Key 写入本目录的 `apikey.txt`（纯文本一行，**不要提交到 git**）。
2. 把原始 A/B 测试使用的那张参考图复制到本目录，命名为 `ref.png`。
3. 安装依赖：

```powershell
pip install numpy imageio-ffmpeg matplotlib
```

## 运行

```powershell
# 一键最小成本实验（默认推荐）：V0 vs V2 各 5 条，共 10 条，然后自动分析
.\go.ps1

# 最小成本实验（等价手动命令）
python run_experiment.py 5 V0,V2

# 完整实验（6 变体, 各 3 条）
python run_experiment.py 3 V0,V1,V2,V3,V4,A

# 分析音频 + 输出波形图 + 统计爆破率
python analyze.py
```

## 默认参数（本次实验约定）

- 规模：最小成本 10 条（V0 vs V2，各 5 条）——先验证最强假设；若需归因再补 V1/V3/V4/A。
- 时长：5 秒；分辨率：768P；ratio：adaptive（跟随参考图）。
- 端点：国内 `https://api.minimaxi.com`（环境变量 `MINIMAX_API_BASE` 可切全球 `https://api.minimax.io`）。

## 说明

- 端点默认 `https://api.minimaxi.com`（国内），可用环境变量 `MINIMAX_API_BASE` 切换全球端点。
- 分辨率固定 768P、时长固定 5s、ratio=adaptive（跟随参考图），控制成本。
- MiniMax API 无 seed 参数，靠重复生成 + 统计比例对抗种子依赖。
- `run_experiment.py` 幂等：已下载成功的 `(variant, trial)` 会跳过，可断点续跑。
- 产物：`out/<variant>_<trial>.mp4`、`out/results.json`、`out/report.json`、`out/wave_<variant>.png`。

## 变体设计

| 变体 | 与 B 的差异 |
|---|---|
| V0 | B 原样（`<d>` + 音景 N/A + 主体语音定义 + 开场缓冲句） |
| V1 | 对白去 `<d>` 内联 |
| V2 | 仅音景换成连续底噪 |
| V3 | 仅主体视觉锚定 `<Picture 1>` + summary 视觉优先 + `<Subject 1> (S1)` |
| V4 | 全量仿 A |
| A | 豆包原版（阳性对照） |

## 字幕 A/B 实验（默认烧字幕问题，2026-09-20 新增）

背景：H3 对「带对白的画面」有很强的烧字幕先验；"不要字幕 / No subtitles" 否定声明无效，其 token 本身可能反而是触发词。已核查 v2 `video_generation` 已知负载（model/content/resolution/duration/ratio）与官方 skill 引用的开关参数（如 `generate_audio`），均无字幕相关字段——提示词侧是唯一控制点。

与爆破音实验共用 `apikey.txt` 与 `ref.png`（同一场景同一参考图，唯一变量是提示词策略，可与 V 系结果横向对照）。

```powershell
# 推荐最小组合：S0/S2/S3 各 5 条（15 条），然后自动分析
python subtitle_experiment.py 5 S0,S2,S3
python subtitle_analyze.py

# 全部 5 个变体，各 3 条
python subtitle_experiment.py 3
```

| 变体 | 策略 | 与 S1 的差异 |
|---|---|---|
| S0 | 基线-否定声明 | 官方 `<d>` 格式 + detailed_description 末尾追加 "No subtitles..." 否定句（模拟用户现状） |
| S1 | 删否定声明 | 官方格式，完全不含字幕字眼 |
| S2 | 正向约束句 | S1 + 工具「🔤 无字幕约束」注入的同款正向句（放在 [Shot 1] 之后） |
| S3 | 电影感锚定 | S2 + 真人院线电影风格锚定句 |
| S4 | 无对白对照 | S1 的画面但无对白（测无语音时的字幕基线，隔离"对白触发"假设） |

- 判定：`subtitle_analyze.py` 优先用 OCR（`pip install rapidocr-onnxruntime`）自动检测画面下部字幕带（高度 70%~97%、宽度 12%~88%）文字；无 OCR 时导出 `out_subtitle/frames/` 裁剪图人工核对。"底部/中部边缘密度比"（suspect）仅作粗筛定位，不作结论。
- 命中口径：字幕带出现与台词对应的文字才算；场景内实体文字（标牌、霓虹灯）不算。
- 成本：每条 5s 768P；5 变体 × 5 条 = 25 条；幂等可断点续跑。
- 结论回灌：胜出句式改写根 HTML 的 `NO_SUBTITLES_CONSTRAINT` 常量，并**同步** `subtitle_experiment.py` 中 S2/S3 使用的同款句子。
- 产物：`out_subtitle/<variant>_<trial>.mp4`、`out_subtitle/results.json`、`out_subtitle/report.json`（均不入库）。

## 爆破音判定

`analyze.py` 的自动判定口径：开头 0~0.3s 内出现 40~250ms 的孤立高能脉冲、其后与正文声音之间有 ≥80ms 低能间隙。判定结果仅作参考，最终以 `wave_<variant>.png` 波形图人工复核为准。
