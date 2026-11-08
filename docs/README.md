# 文档地图

按「我想…」选文件：门面看 README，日常使用看 USAGE，改代码前看架构，写插件看插件指南。

| 我想… | 读这个 | 类型 |
|---|---|---|
| 5 分钟装好并跑起来（这是什么、怎么启动） | [README.md](../README.md) | 门面 |
| 安装、配置、首次使用向导与常见排错 | [USAGE.md](../USAGE.md) | 操作入口 |
| 改代码前理解模块职责、数据流、数据库与设计陷阱 | [architecture.md](architecture.md) | 架构事实源 |
| 接入新消息源或管道阶段槽位（第三方插件契约） | [plugin-dev.md](plugin-dev.md) | 插件契约 |
| 查 benchmark 插件的局部说明 | [benchmark README](../briefdesk/plugins/benchmark/README.md) | 局部说明 |
| 查图标资源的维护规则 | [icons README](../briefdesk/ui/icons/README.md) | 局部说明 |
| 查 vendor SDK 镜像的同步规则 | [vendor README](../vendor/README.md) | 局部说明 |
| 查 WeFlow 上游 HTTP API 契约 | [WeFlow HTTP API / Push 文档][upstream-api] | 上游参考 |
| 报告安全问题 | [SECURITY.md](../.github/SECURITY.md) | 安全 |
| 提交 PR | [PR 模板](../.github/PULL_REQUEST_TEMPLATE.md) | 协作 |

[upstream-api]: https://github.com/hicccc77/WeFlow/blob/main/docs/HTTP-API.md

上游 HTTP API 以上游现行文档为准；仓库内不保留上游正文副本。

## 阅读路径

第一次接触、要改代码、要写插件三种场景各有一条最短路径。

- **第一次接触**：先 [README.md](../README.md) 跑通，再按上表挑任务；日常操作细节在 [USAGE.md](../USAGE.md)。
- **要改代码**：[architecture.md](architecture.md) 是架构细节的唯一汇总处 → 根目录 [AGENTS.md](../AGENTS.md)（命令、质量门禁、提交规范）。模块职责、插件框架、DB schema、server 路由、配置项或跨模块陷阱变化都须回写 architecture.md。
- **要写插件**：[plugin-dev.md](plugin-dev.md)（最小示例、槽位契约、生命周期与失败补偿、配置与密钥、测试建议）→ [architecture.md](architecture.md) 的「插件框架」与「核心模块」两节。
