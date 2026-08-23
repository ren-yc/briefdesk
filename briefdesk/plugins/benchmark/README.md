# LLM 功能基准测试插件（benchmark）

对四个 LLM 功能（**分类 classify / 去重 dedup / 合并判定 merge / 标题重拟 title**）
做**输出质量**评估：与生产管道**同引擎、同 AI 供应商**（真实 API），逐用例判定 +
聚合指标 + 可选 HTML 图表报告。

> ⚠️ 运行会**真实调用 AI**（读 `.env` 的 `AI_API_KEY` / `AI_API_BASE` / `AI_MODEL`），
> 按用例数产生 API 费用。运行期间临时库经补丁隔离，**不影响应用数据库**，
> 但请勿同时触发同步（补丁是进程级的）。测试数据请使用虚构/脱敏内容
> （AGENTS.md 隐私规范）。

## 用法一：Web（插件）

插件启用即随应用加载（`PLUGINS` 默认 `["*"]`），设置弹窗 → **「关于」面板 →
「基准测试」区块**：

1. **导出当前列表为基准用例**：把当前筛选条件下的卡片导出为**四类**基准用例
   （期望 = 卡片当前状态；无类别卡片跳过；与「导出卡片 CSV」同口径），
   逐功能**覆盖写入**插件目录 `cases/<feature>.fromweb.json`（文件存储，
   不写数据库；某功能无可导出用例时保留其原文件）。四类期望推导：
   - **classify**：期望 = 卡片当前分类/主体/时间字段（按批上限拆分多例）；
     **已忽略卡片（is_verified=-1）作为噪声样本进入 messages、不写期望**——
     它们是 AI 误分类上来的闲聊（人工标记应丢弃），模型不应输出分类结果
     （输出即计为误报）；噪声样本恒被纳入，与当前视图的"未核实"筛选无关；
   - **title**：每卡一例，期望 = 卡片关键词包含（`key_info` 拆分；无 key_info
     回退期望标题 = 卡片当前标题）；已忽略卡片排除（噪声不作标题期望）；
   - **dedup**：same=true = 同 `content_hash` 却共存的卡片对（同一信息）；
     same=false = 时间相邻且类别不同的共存卡片对（不同信息）；已忽略卡片排除；
   - **merge**：同会话同类别、时间窗（`MERGE_WINDOW_MINUTES`）内的相邻卡片对，
     同主体同发送者 → merge=true（同话题片段），主体均非空且不同 → merge=false；
     已忽略卡片排除。
   注意：title/dedup/merge 用例数与列表规模成正比（每卡/每对一例），请先用
   筛选控制规模再导出。
2. **记录处理过程**（可选，默认关闭）：benchmark 同时注册为**阶段插件**
   （slot=post_insert），开启后在**管道真实处理时点**采集 dedup/merge 的判定
   观察记录并累积内存。它与「导出当前列表」互补——按卡片最终状态导出只能看到
   "去重漏判/未合并"的共存对（命中证据已被删库），而处理记录能捕获：
   - **dedup**：判重命中的 (query, 命中候选) → same=true；候选被判定为不同的
     (query, 最高分候选) → same=false（真实 AI 判定依据，非相邻类别近似）；
   - **merge**：合并判定命中/未命中的 (head, tail) 对 → merge=true/false；
   - **title**：合并后重拟标题事件（old_title + 合并后 key_info/quote →
     期望 = key_info 关键词包含，与网页导出同口径）。
   **导出处理记录**把累积记录逐功能覆盖写入 `cases/<feature>.fromweb.json`
   （无记录的功能保留原文件），导出后清空累积器；**丢弃记录**不清文件。
   记录含真实聊天内容，导出文件已 gitignore，请勿长时间开启后忘记导出。
3. **运行基准测试**：后台真实调用 AI（耗时数分钟），状态实时轮询；
   完成后显示各功能核心指标，可**打开图表报告**（自包含 HTML，可另存分享）。
4. **清空基准用例**：删除全部 `cases/*.fromweb.json`（不影响卡片数据）。

> `*.fromweb.json` 含真实聊天内容，已 gitignore，**禁止提交**（AGENTS.md 隐私规范）。

API：`GET/DELETE /api/benchmark/cases`（列出/删除导出用例）、
`POST /api/benchmark/import-current`（导出当前列表）、
`GET/POST /api/benchmark/record`（记录状态/开关）、
`POST /api/benchmark/export-recorded`（导出处理记录）、
`DELETE /api/benchmark/record`（丢弃记录）、
`POST/GET /api/benchmark/run`（运行/状态）、`GET /api/benchmark/report(.json)`（结果）。

## 用法二：CLI

```bash
python -m briefdesk.plugins.benchmark.cli --dry-run          # 校验文件数据集（不调用 AI）
python -m briefdesk.plugins.benchmark.cli --feature classify  # 跑单功能（文件数据集）
python -m briefdesk.plugins.benchmark.cli --charts            # 额外生成 HTML 图表报告
python -m briefdesk.plugins.benchmark.cli --model deepseek-v4-flash --concurrency 4
```

- 文件数据集在 `cases/`（`<feature>.json`，示例为 `<feature>.example.json`，
  复制后手动编辑；结构与 schema 校验见下）。查找顺序：`<feature>.json` →
  网页导出的 `<feature>.fromweb.json` → `<feature>.example.json`；
- 运行中每完成 5 条用例输出一次进度（如 `  classify 5/20 · 42.3s`，失败用例
  带 `（失败 N）`；最后一条恒输出），`--dry-run` 不评估、无进度；
- 结果导出到 `reports/`（gitignore）：`run-<时间戳>.json`（指标 + 逐用例明细）与
  `run-<时间戳>.html`（`--charts` 时，自包含 SVG 图表报告）。

## 四个功能与评估口径

| 功能 | 引擎入口（与生产同路径） | 测试集输入 | 期望标注 | 主要指标 |
|---|---|---|---|---|
| classify | `classify_batch(messages)` | 一批 InternalMessage | 每条应分类消息的 `index`/`category`，可选 `start`/`end`/`times` | 类别准确率/精确率/召回率/F1、时间点完全一致率、失败率 |
| dedup | `DedupEngine.check_dedup` | `items`（历史卡片）+ `query`（新消息） | `same: true/false` | 判重准确率/精确率/召回率/F1、预筛跳过数 |
| merge | `judge_merge` | `head` + `tail` 两张卡 | `merge: true/false` | 判定准确率/精确率/召回率/F1 |
| title | `summarize_title` | 一张卡 + `old_title`/`key_info` | `title` 精确 与/或 `keywords` 包含 | 精确匹配率、关键词命中率、平均长度、回退数 |

## 测试集格式（手动添加 / 网页导出共用）

手写文件数据集与网页导出的 `*.fromweb.json` 共用同一 schema。顶层结构：

```json
{
  "feature": "classify",
  "description": "可选说明",
  "categories": [],          // 可选，仅 classify：覆盖默认五类的 [{name, prompt?, color?}]
  "cases": [ ... ]
}
```

### 消息输入（InternalMessage 形状，四个功能通用）

字段与 `briefdesk.types.InternalMessage` 对齐：`msg_id`、`content`、`sender_name`、
`sender_id`、`session_id`、`group_name`、`timestamp`、`source`、`is_self`、
`image_urls`、`article_url`。必填仅 `msg_id` + `content`，其余可省略。

- `timestamp` 除整数 epoch 秒外，**推荐直接写本地时间字符串**：
  `"2026-04-05 14:30"`（或 `"2026-04-05"`），自动换算；
  分类的时间提取以该时刻为相对时间锚点，请写真实意图对应的时间。
- 两个基准扩展字段（模拟"已分类卡片"）：
  - `title`：可选，卡片的标题（缺省取 content 前 50 字）；
  - `key_info`：可选，卡片关键词（title 判官使用）。

### classify

```json
{
  "id": "cls-001",
  "note": "可选说明",
  "messages": [ { ...InternalMessage... }, ... ],
  "expected": [
    { "index": 0, "category": "活动通知", "subject": "校园十佳歌手大赛",
      "start": "2026-04-20 19:00", "end": "2026-04-15",
      "times": [ {"type": "start", "time": "2026-05-01", "label": "决赛"} ] },
    { "index": 3, "category": "交易" }
  ]
}
```

- `expected` 列出**所有应被分类**的消息（按在 `messages` 中的下标）；不在列表中的消息视为闲聊/噪声——模型若输出其分类结果计为误报。
- `category` 必须是启用类别之一（默认五类：活动通知/社团招新/学术/交易/实习；可用顶层 `categories` 覆盖）。
- 时间字段为期望值：`start`/`end` 对应卡片主时间字段，`times` 对应 `extra_times`（同一条消息的其它时间点）。**期望应写全**：时间点按 `(type, time)` 精确集合比对（忽略 label 措辞）。相对时间按引擎规则换算：`"4月15日"` 取与发送时刻最近的一次（已过的截止日仍取当年）。
- 无明确时间的消息不写时间字段（写了会被严格比对扣分）。

### dedup / merge / title

```json
{ "id": "dd-001", "items": [ { ...InternalMessage..., "title": "摄影社招新面试" } ],
  "query": { ...InternalMessage..., "title": "摄影社周三下午三点在体育馆招新" },
  "expected": { "same": true } }

{ "id": "mg-001", "head": { ...InternalMessage... }, "tail": { ...InternalMessage... },
  "expected": { "merge": true } }

{ "id": "tt-001", "message": { ...InternalMessage... }, "old_title": "...", "key_info": "...",
  "expected": { "title": "精确期望（可选）", "keywords": ["关键词", "包含匹配（可选）"] } }
```

- **dedup 候选预筛注意**：未配置 `EMBED_API_BASE` 时用标题字符重叠预筛（阈值
  `DEDUP_SIMILARITY_THRESHOLD`=0.3），重叠不足的 pair **不会触发 AI**、直接判为不同。
  要评测 LLM 判重质量，请让 `query.title` 与某条 `items.title` 保持足够重叠
  （如相同标题、不同正文）；结果中会标注"预筛跳过"的用例数。
- title 期望的 `title` 精确匹配 与 `keywords` 包含匹配至少提供一种；
  标题是开放生成，**建议主要用 `keywords`**（精确率对多次运行有抖动，仅作参考）。

## 指标定义

- **类别准确率** = 期望集合中类别精确命中数 / 期望条数（漏检即扣分，与召回率同值）；
  **精确率** = 命中数 / 模型输出条数（误报扣分）；F1 为两者调和平均。
- **时间点完全一致率** = 时间点集合与期望精确相等（多提/漏提都扣）的消息占比；
  另有逐点召回率/精确率（只按 `(type, time)` 比对，忽略 label）。
- **失败率** = `outcome.failed` 条数 / 消息总数（解析失败、未知类别、网络错误等
  本轮无法入库的消息；生产中这些消息会在回填窗口重试）。
- **dedup/merge**：以 true 为正类，accuracy/precision/recall/F1 + TP/FP/TN/FN；
  dedup 额外统计"预筛跳过"用例数。
- **title**：精确匹配率（有 `title` 期望的用例）、关键词命中率（有 `keywords` 期望的用例）、
  平均输出长度、超长（>30 字，与 `TITLE_PROMPT` 约束一致）数与回退原标题数。

## 常见问题

- **`DatasetError: 用例校验失败`**：`--dry-run` 会列出全部非法用例与字段错误，按提示修正。
- **dedup 大量"预筛跳过"**：标题重叠不足，未走到 AI；参考上文候选预筛注意。
- **想测不同模型**：`--model` 覆盖模型名；同一测试集多跑几次可观察判定稳定性。
- **Web 导出的用例去哪了**：插件目录 `cases/*.fromweb.json`（classify/dedup/
  merge/title 四类，覆盖式文件，不写数据库；含真实聊天内容，已 gitignore，
  禁止提交）；CLI 未找到 `<feature>.json` 时会自动回退使用它们。
