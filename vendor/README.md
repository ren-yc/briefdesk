# vendor/

本目录是**上游 SDK 的 git subtree 镜像**（`--squash`），随本仓分发：

| 目录 | 上游 | 供应分支 |
|---|---|---|
| `weflow_sdk` | weflow-server `clients/python/src/weflow_sdk` | `sdk-dist` |
| `qqflow_sdk` | qqflow-server `clients/python/src/qqflow_sdk` | `sdk-dist` |

## 纪律：vendor 内禁止任何本地修改

本目录内容是上游的纯镜像。**补丁一律先落在上游仓，再经 `scripts/sync_vendor.ps1` 同步进来**；
直接改这里的文件会在下次同步时产生冲突、需要人工解决（不是自动丢弃，也不是静默接受）——
这是有意的：静默分叉比构建失败贵得多。

## 同步流程

```powershell
# 上游侧（weflow-server / qqflow-server）：SDK 变更提交推送后，重建供应分支。
# 本地分支必须一起更新：下游按路径 fetch 的是分支本身，只推 origin 会让下游继续拿到旧镜像。
git subtree split -P clients/python/src/weflow_sdk -b sdk-dist-new
git branch -f sdk-dist sdk-dist-new
git branch -D sdk-dist-new
git push origin sdk-dist --force-with-lease
# qqflow 同构

# briefdesk 侧：拉取同步
powershell -File scripts/sync_vendor.ps1
```

同步脚本会：pull 两个 subtree → 从 merge 提交的**第二父**解析 squash 标记
（`changes from X..Y` / `content from <sha>`；merge 提交自己的标题只有 `Merge commit … as …`，
从 HEAD 正文找不到。解析不到即打印两方正文并响亮报错，不静默跳过）→ 打印可直接粘贴的
同步记录行 → import 冒烟（解释器取 `$env:PYTHON`，未设时用 `python`）。

## 同步记录

- 初始挂载：weflow `sdk-dist`（55cb066，源自 weflow-server master `d8d191b`）、
  qqflow `sdk-dist`（fdc3f0c，源自 qqflow-server master `a4f0d0e`）。
- 生成层去空白同步：weflow `sdk-dist`（`04bcbb7`，源自 weflow-server master `a192767`）、
  qqflow `sdk-dist`（`b6f1f06`，源自 qqflow-server master `c840db5`）。上游生成管线新增确定性
  规范化（逐行去行尾空白、折叠结尾空行、保证单个结尾换行），镜像随之更新；此次同步后
  vendor 内不再有条目级空白问题，范围级空白门禁对镜像部分归零。
- `list_all_sessions` 页大小参数同步：weflow `sdk-dist`（`5345d6d`，源自 weflow-server master
  `12d68f2`）、qqflow `sdk-dist`（`819690b`，源自 qqflow-server master `cdf7418`）。上游改动：
  该方法新增 `page_size`（服务端上限 10000 条/页）与 `keyword`，供**每轮都重读列表**的轮询
  消费者一次取满——否则 4000 个会话要发 40 个请求。传 `None` 保持服务端默认页，默认值未变。
- SDK 0.8.0 公共面扩展同步：weflow `sdk-dist`（`682fee7`，源自 weflow-server master `9d26033`）、
  qqflow `sdk-dist`（`4ca7666`，源自 qqflow-server master `dbd090f`）。上游改动：**新增七项公共面**
  （`health` / `accounts` / `register`（非阻塞，原始 `state`/`status`）/ `wait_ready` /
  `list_messages`（原生消息面，含 `offset` 翻页与 `media=1`）/ `contacts` / `media_bytes_by_id`）、
  **删除 `search`**（`list_messages` 是其严格超集）、`ensure_ready` 对 200 拒绝态**立即失败**
  （Rust 侧新增 `ClientError::Refused`，Python 侧维持 `StatusError`）、时间界校验放宽为
  「`YYYYMMDD` 或 unix 秒」。同步经 `scripts/sync_vendor.ps1` 完成，本行两个 commit 取自
  merge 提交的第二父。
- 行为层修复与包内许可证同步：weflow `sdk-dist`（`8456773`，源自 weflow-server master `c8c7392`）、
  qqflow `sdk-dist`（`d6d7550`，源自 qqflow-server master `58856b9`）。上游改动：`watch` 的退避只在
  干净结束时复位、1 MiB 上限同时约束单个完整帧与未成帧累计、一帧多条 `data:` 行按规范以 LF 拼接、
  撤销从未生效的 `poll_interval` 形参；包目录新增 `LICENSE`（本仓 `package-data` 同步声明，
  vendor 全量清单随之 109 → 111 项）。同步经 `scripts/sync_vendor.ps1` 完成，脚本打印的
  记录行即本节两行的来源（squash 标记取自 merge 提交的第二父）。
