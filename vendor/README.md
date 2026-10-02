# vendor/

本目录是**上游 SDK 的 git subtree 镜像**（`--squash`），随本仓分发：

| 目录 | 上游 | 供应分支 |
|---|---|---|
| `weflow_sdk` | weflow-server `clients/python/src/weflow_sdk` | `sdk-dist` |
| `qqflow_sdk` | qqflow-server `clients/python/src/qqflow_sdk` | `sdk-dist` |

## 纪律：vendor 内禁止任何本地修改

本目录内容是上游的纯镜像。**补丁一律先落在上游仓，再经 `scripts/sync_vendor.ps1` 同步进来**；
直接改这里的文件，下次 subtree pull 会以 modify/delete 冲突响亮失败——这是有意的：
静默分叉比构建失败贵得多。

## 同步流程

```powershell
# 上游侧（weflow-server / qqflow-server）：SDK 变更提交推送后，重建供应分支
git subtree split -P clients/python/src/weflow_sdk -b sdk-dist
git push origin sdk-dist --force-with-lease
# qqflow 同构

# briefdesk 侧：拉取同步
powershell -File scripts/sync_vendor.ps1
```

同步脚本会：pull 两个 subtree → 解析 squash 提交里的 `changes from X..Y` 标记
（解析不到即响亮报错退出，不静默跳过）→ 提示更新本文件的同步记录段 → import 冒烟。

## 同步记录

- 初始挂载：weflow `sdk-dist`（55cb066，源自 weflow-server master `d8d191b`）、
  qqflow `sdk-dist`（fdc3f0c，源自 qqflow-server master `a4f0d0e`）。
