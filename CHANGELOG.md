# 更新日志

本文件记录 briefdesk 的版本变更，自 0.2.0 起维护。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

暂无未发布变更。

## [0.2.0] - 2026-11-14

本版让遥测可关可留、口径自洽，并把判定质量与隐私边界收口：合并预筛门与判重阈值换轨、安全审查修复落地、文档按结论先行重写。

### 新增

- 本地运行指标开关 `TELEMETRY_ENABLED`（默认开、重启生效）：关闭后不计数也不落盘，已生成的文件保留；落盘快照保留 14 天、余弦-真值明细 180 天，随同步顺带清理。回归：`test_disabled_switch_skips_recording_and_disk`、`test_flush_prunes_only_expired_dated_files`。
- 遥测口径补齐：RAG 问答增加 `rag.retrieve_error`、`rag.answer_error`、`rag.cancelled` 三个终态桶与 `rag.chat_fallback`、`rag.citation_fallback` 两种回退桶，恒等式 `ask = refused + answered + retrieve_error + answer_error + cancelled` 在所有出口成立；判官与分类调用按消息源分维度，基准流量恒记 `bench`。回归：`test_telemetry_identity_across_terminal_buckets`、`TestTelemetrySourceIsBenchOnly`。
- 合并判官余弦预筛门 `MERGE_PREFILTER_THRESHOLD`（默认 `-1` 关闭）：候选对余弦低于门限直接判不合并、不经 AI；门限须由余弦-真值数据标定产生，未标定前保持关闭。
- 去重判官负向缓存：完全相同的判官输入命中后直接复用 DIFFERENT 结论，省掉一次 AI 调用；缓存实例随引擎生命周期，基准每用例新建即天然隔离。
- 时间解析器骨架与影子采集（`TEMPORAL_SHADOW=on`）：只记录解析器产出与时间提取的逐字段差异，不影响判定，为切换规则包积累对照数据。
- 分类数据采集、筛选计数表与类别分配器（影子打分，不接入判定主链路）。
- benchmark 工具链：用例数据集裁剪脚本、用例来源标注与合并导出、双口径分母工具。
- 设置页 schema 下发 `applyWhen` 采用时点；插件面板增加「清除本机插件启用与必选」的恢复按钮。

### 变更

- 去重标题重叠度量换轨为字符 bigram Jaccard，`DEDUP_SIMILARITY_THRESHOLD` 默认值按新口径重标定为 `0.07`。
- 轮询周期的遥测汇总行由 INFO 降为 DEBUG：默认 INFO 下每轮一行会把同步与分类的业务行冲散，验收读数以落盘的 `latest.json` 为准。
- 两个消息源插件改为运行期消费随包分发的 `weflow_sdk` / `qqflow_sdk`（git subtree 挂载）。
- 运行时依赖抬到 `Pillow>=13.0` 与 `certifi>=2026.10.19`，脱离已撤销信任根与已知安全公告。
- 文档重写：README / USAGE / 架构文档按结论先行重排，补齐配置项逐字清单、隐私边界与数据位置。

### 破坏性变更

- `DEDUP_SIMILARITY_THRESHOLD` 默认值由 `0.3` 改为 `0.07`：显式写过 `0.3` 的部署建议删掉该行改用默认值，或按 `scripts/recalibrate_similarity.py` 重标定；沿用旧值会让无嵌入部署的召回明显下降。
- 空遥测轮次不再覆盖 `latest.json`：依赖「每轮都有新快照」的读数脚本需按快照里的 `ts` 判断新鲜度。回归：`test_empty_snapshot_keeps_previous_latest`。
- 暂存配置层为字面量层：值里写 `${VAR}` 不再被插值，保存时直接拒绝并提示改写到项目 `.env` 或环境变量层。回归：`test_staged_layer_literal_and_project_layer_interpolated`。

### 修复

- 昵称与群名按正文同一套 PII 规则构造即脱敏：此前可绕过掩码进入 AI 请求、落库并随导出携带。
- 密钥「已配置」改为按当前解析链活判定，删除后不再谎报已配置。
- 暂存配置提交的目录创建与残留清理失败纳入结构化错误，不再以异常栈形式冒泡。
- 消息源连接级重试穿透 SDK 的错误包装；两个插件客户端接入共享就绪门控。
- benchmark：图表与历史报告把缺失指标显示为占位符而不是零；导出批次指纹改内容派生，同路径重导互斥可咬住；判重基准镜像生产取向量并隔离用例。
- 界面：批量取消全选后的绿框残留、问一问输入框清空、日历备忘空态提示。
