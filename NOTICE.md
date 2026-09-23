# 来源与修改说明

本包的视频集成代码基于用户提供的 `MiniMaxH3-Integration-GHX` 工作树（2026-09-23）。
该参考项目是 goohai/Goohai-MiniMax-H3_Integration 的定制版本，包含 T8 风格的
H3 条件构建、双时钟采样及音视频解码实现。原说明完整保留于 UPSTREAM-GHX-README.md，
原版权声明和 GPL-3.0-or-later 许可证保留于 LICENSE。

本次修改：新增服务端提示词生成器；节点 ID 改用 RH 后缀；前端扩展名和优化器路由隔离；
独立 MiniMaxH3RH 连接类型；上游提示词接入时保留生成器的声音与画面约束。
提示词规则来自本项目现有 HTML 工具，通过 scripts/build_prompt_engine.py 提取，
仅移除内置凭据并加入服务端适配；源 HTML 的 SHA-256 记录于 prompt_engine/source.sha256。

分发包不包含模型权重，模型许可由各模型发布方规定。
