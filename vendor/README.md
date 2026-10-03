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
