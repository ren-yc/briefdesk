# 更新日志

本文件记录 briefdesk 的版本变更，自 0.2.0 起维护。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

本节收录尚未发布的行为变化，发布时归入对应版本号并注明日期。

### 新增

- 出站展示名替换开关 `AI_PSEUDONYMIZE_NAMES`（`auto` / `on` / `off`，默认 `auto`，留空等同 `auto`）：`auto` 只在生效端点为远端时替换群名与发送者名，本地端点不改写输入；非法值启动期报错。回归：`test_remote_auto_replaces_names_and_keeps_pii_out`、`test_evidence_names_kept_for_local_endpoint`。
- 安装级别名注册表 `briefdesk/pseudonym.py`：盐与「完整哈希 → 别名」同文件原子提交，文件里只有盐与哈希；注册表损坏、缺盐、加锁超时或写盘失败一律拒绝出站，不自动换盐也不退回真名；还原只替换完整 token，失败保留原值并按字段计数。回归：`test_corrupt_salt_never_auto_rotates`、`test_replace_failure_keeps_old_map_and_prevents_send`、`test_restore_requires_whole_token`。
- 出境与排队计数 `briefdesk/ai_egress.py`：按端口与角色分桶记录远端/本地发送尝试、排队超时与取消、SDK 内部重试（读 `x-stainless-retry-count`）。回归：`test_queue_failures_never_count_remote_attempt`、`test_sdk_retry_separate_from_logical_attempt`、`test_disabled_telemetry_writes_no_zero_keys`。
- rag 派生索引新增 `rag_chunks.sender_id` 并从 `raw_messages` 回填：迁移先查表结构、缺列才 `ALTER`，回填只填空串，缺行或缺 ID 保持空串。回归：`test_sender_id_migration_is_idempotent`、`test_missing_raw_id_stays_empty`。

### 变更

- 远端端点的分类与问答请求默认替换展示名，模型输出里的别名在落库前还原为真名；本地端点输入保持不变。回归：`test_same_sender_id_uses_same_alias_in_sysb_and_sysc`、`test_quote_and_key_alias_restored_at_parse`。
- RAG 证据行的群名与发送者名改按「实际派发端点」判定是否替换：供应商没有专用问答通道、override 被丢弃并回退主端点时，按主端点判定。回归：`test_rag_fallback_pseudonymizes_for_actual_main_endpoint`。
- 分类批的展示名准备改为请求级一次并发准备，sysb / sysc / 摘要三次请求共用同一份请求内逆映射；准备失败整批进 failed 回填，不发明文。回归：`test_prepare_failure_fails_batch_without_sending`。
- `quote` 在字面漂移守卫之前还原：守卫按原文（真名）比对，带别名的引文不再被判成索引漂移。回归：`test_quote_and_key_alias_restored_at_parse`、`tests/test_classify_alignment_guard.py`。

### 破坏性变更

- `build_answer_prompt` 的 `names` 改为必填：不再回退 chunk 上的显示名，调用方必须为每条证据准备展示名，长度与命中条数不符直接报错。回归：`test_evidence_pseudonymized_for_remote_endpoint`、`test_evidence_names_kept_for_local_endpoint`。
- rag 表新增 `rag_chunks.sender_id`：回滚必须保留字段投影读取器与新增列，直接退到旧代码会因 `SELECT c.*` 多出一列而 `TypeError`，关键词检索腿在读取时失败；向量腿不经过该读取器、不受影响。回归：`test_rollback_reader_ignores_unknown_columns`。

### 修复

- 源侧透传的异常时间戳不再进入前端属性插值：`InternalMessage` 构造即把非 int（含 bool）时间戳归 0，堵住字符串时间戳经 SQLite 宽类型落库、再被分组模板插进 `data-msgtime` 属性的路径；三处分组模板的时间戳属性与单卡口径统一走 `escAttr`。回归：`InternalMessageTimestampCoercionTest`。
- 别名还原不再把一个人换成另一个人：本请求已写回过的显示名整体受保护，落在其中的片段不再被当成别名二次替换。回归：`test_produced_display_name_is_never_reinterpreted`、`test_protected_display_name_survives_inside_sentence`。
- 注册表读取失败不再被当成「文件不存在」而静默重建：损坏、缺盐与版本不支持一律拒绝出站，避免一次重盐让历史化名永久失效。回归：`test_read_registry_rejects_non_alias_values`、`test_version_error_does_not_echo_value`。
- 本地端点判定的绕过路径收口：带凭据段或空 userinfo、控制字符、非法端口与近似域名一律按远端处理，畸形 URL 不再被判成本地。回归：`test_local_endpoint_matrix_fail_closed`、`test_remote_endpoint_matrix_fail_closed`。
- rag 里已有的 `sender_id` 不再被空串覆盖：回填与 upsert 都只保留旧值，新写入的非空值仍可更新。回归：`test_upsert_preserves_known_sender_id`。
- RAG 证据名的身份键、出站映射与还原目标统一走 pseudonym 模块的身份键单源：缺 ID 的发送者不再被错并成同一个人（同会话异名者各得化名、还原各还原到本人），跨会话缺 ID 的发送者不再因映射查不到而把真名送进远端证据行。回归：`test_idless_senders_in_same_session_keep_distinct_aliases`、`test_idless_senders_in_different_sessions_stay_separate`、`test_group_without_session_id_uses_name_key`。
- 证据行与分类批的「最后非空显示名」口径内建于收集阶段：先空后真名、真名后空名、群名先空后真都不再出现空名覆盖有效别名或锁定首个空串。回归：`test_empty_name_after_real_name_never_wipes_alias`、`test_group_empty_then_real_name_resolves_to_real`、`test_trailing_empty_name_does_not_wipe_earlier_real_name`、`test_multiple_real_names_take_the_last_one`。
- RAG 证据名的全部身份（人与群）共享一次并发准备：分两轮 gather 或逐条 await 会让每个合并落盘窗口各等一次，证据越多准备越慢。回归：`test_new_identities_share_exactly_one_commit_increment`。
- 启用假名化时证据名的出站映射查不到即按准备失败处理，不再静默回退真名。回归：`test_prepare_failure_skips_answer_request`。
- `rag_chunks.sender_id` 迁移的回填失败或被取消时整体回滚：不再留下「列已加、值全空、下次启动不再回填」的死状态。回归：`test_sender_id_backfill_failure_rolls_back_whole_migration`。
- SDK 重试观测状态独立计数：`sdk_retry_observed` 只由「请求头里真的读到了重试序号」推出（新指标 `ai.egress.retry_observed.*` / `ai.local.retry_observed.*`），不再把遥测开关当成观测可用——钩子未挂或头不可读时报告不再把「零次重试」伪装成可信读数。回归：`test_retry_observed_true_with_zero_retries`、`test_egress_summary_uses_header_observation_not_telemetry_switch`。

## [0.2.0] - 2026-11-14

本版让遥测可关可留、口径自洽，并把判定质量与隐私边界收口：合并预筛门与判重阈值换轨、安全审查修复落地、文档按结论先行重写。

### 新增

- 本地运行指标开关 `TELEMETRY_ENABLED`（默认开、重启生效）：关闭后不计数也不落盘，已生成的文件保留；落盘快照保留 14 天、余弦-真值明细 180 天，随同步顺带清理。回归：`test_disabled_switch_skips_recording_and_disk`、`test_flush_prunes_only_expired_dated_files`。
- 遥测口径补齐：RAG 问答增加 `rag.retrieve_error`、`rag.answer_error`、`rag.cancelled` 三个终态桶与 `rag.chat_fallback`、`rag.citation_fallback` 两种回退桶，恒等式 `ask = refused + answered + retrieve_error + answer_error + cancelled` 在所有出口成立；判官与分类调用按消息源分维度，基准流量恒记 `bench`。回归：`test_telemetry_identity_across_terminal_buckets`、`TestTelemetrySourceIsBenchOnly`。
- 合并判官余弦预筛门 `MERGE_PREFILTER_THRESHOLD`（默认 `-1` 关闭）：候选对余弦低于门限直接判不合并、不经 AI；门限须由余弦-真值数据标定产生，未标定前保持关闭。回归：`test_prefilter_skips_low_cosine_candidate`、`test_prefilter_closed_by_default_calls_judge`。
- 去重判官负向缓存：完全相同的判官输入命中后直接复用 DIFFERENT 结论，省掉一次 AI 调用；缓存实例随引擎生命周期，基准每用例新建即天然隔离。回归：`tests/test_negcache.py`、`test_remove_items_invalidates_negcache`。
- 时间解析器骨架与影子采集（`TEMPORAL_SHADOW=on`）：只记录解析器产出与时间提取的逐字段差异，不影响判定，为切换规则包积累对照数据。回归：`test_summarize_shadow_file`、`test_record_diff_sysc_failed_marked`。
- 分类数据采集、筛选计数表与类别分配器（影子打分，不接入判定主链路）。回归：`tests/test_classify_observations.py`。
- benchmark 工具链：用例数据集裁剪脚本、用例来源标注与合并导出、双口径分母工具。回归：`test_summarize_groups_by_feature_origin_fingerprint`、`tests/test_benchmark.py`。
- 设置页 schema 下发 `applyWhen` 采用时点；插件面板增加「清除本机插件启用与必选」的恢复按钮。回归：`test_schema_carries_apply_when_per_field`、`test_app_js_references_plugin_toggle_flow`。

### 变更

- 去重标题重叠度量换轨为字符 bigram Jaccard，`DEDUP_SIMILARITY_THRESHOLD` 默认值按新口径重标定为 `0.07`。回归：`tests/test_benchmark_runner_contract.py`（标题对按 bigram 口径断言）。
- 轮询周期的遥测汇总行由 INFO 降为 DEBUG：默认 INFO 下每轮一行会把同步与分类的业务行冲散，验收读数以落盘的 `latest.json` 为准。回归：`test_summary_logs_at_debug_only`。
- 两个消息源插件改为运行期消费随包分发的 `weflow_sdk` / `qqflow_sdk`（git subtree 挂载）。回归：`scripts/wheel_smoke.py`（vendor 消费者断言）。
- 运行时依赖抬到 `Pillow>=13.0` 与 `certifi>=2026.10.19`，脱离已撤销信任根与已知安全公告。回归：`scripts/sdist_check.py`（产物成员与元数据断言）。
- 文档重写：README / USAGE / 架构文档按结论先行重排，补齐配置项逐字清单、隐私边界与数据位置。回归：`tests/test_doc_style.py`。

### 破坏性变更

- `DEDUP_SIMILARITY_THRESHOLD` 默认值由 `0.3` 改为 `0.07`：显式写过 `0.3` 的部署建议删掉该行改用默认值，或按 `scripts/recalibrate_similarity.py` 重标定；沿用旧值会让无嵌入部署的召回明显下降。回归：`tests/test_benchmark_runner_contract.py`。
- 空遥测轮次不再覆盖 `latest.json`：依赖「每轮都有新快照」的读数脚本需按快照里的 `ts` 判断新鲜度。回归：`test_empty_snapshot_keeps_previous_latest`。
- 暂存配置层为字面量层：值里写 `${VAR}` 不再被插值，保存时直接拒绝并提示改写到项目 `.env` 或环境变量层。回归：`test_staged_layer_literal_and_project_layer_interpolated`。

### 修复

- 昵称与群名按正文同一套 PII 规则构造即脱敏：此前可绕过掩码进入 AI 请求、落库并随导出携带。回归：`test_email_in_sender_and_group_masked`、`test_phone_in_sender_masked`。
- 密钥「已配置」改为按当前解析链活判定，删除后不再谎报已配置。回归：`test_keyring_empty_string_not_configured`、`test_env_empty_string_is_not_configured`。
- 暂存配置提交的目录创建与残留清理失败纳入结构化错误，不再以异常栈形式冒泡。回归：`tests/test_settings_env.py`。
- 消息源连接级重试穿透 SDK 的错误包装；两个插件客户端接入共享就绪门控。回归：`tests/test_client_retry.py`。
- benchmark：图表与历史报告把缺失指标显示为占位符而不是零；导出批次指纹改内容派生，同路径重导互斥可咬住；判重基准镜像生产取向量并隔离用例。回归：`test_html_chart_skips_missing_metric_instead_of_zero_bar`、`tests/test_benchmark.py`。
- 界面：批量取消全选后的绿框残留、问一问输入框清空、日历备忘空态提示。回归：`tests/test_ui_onboarding_select_all.py`。
