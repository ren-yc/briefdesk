# briefdesk 插件开发指南

面向第三方开发者的插件编写手册：从最小可运行示例到生命周期契约、配置与密钥、测试建议。
架构背景请先阅读 [architecture.md](architecture.md) 的「插件框架」与「核心模块」两节；本文只讲「怎么写」。

## 目录

1. [最小 WebPlugin 示例](#1-最小-webplugin-示例)
2. [StagePlugin 槽位契约](#2-stageplugin-槽位契约)
3. [生命周期与失败补偿](#3-生命周期与失败补偿)
4. [发现方式与配置：entry points / PLUGINS / PLUGIN_PATH](#4-发现方式与配置)
5. [事件订阅与端口获取](#5-事件订阅与端口获取)
6. [配置与密钥](#6-配置与密钥)
7. [测试建议](#7-测试建议)

---

## 1. 最小 WebPlugin 示例

WebPlugin 在核心 REST 之外挂自己的路由与前端资源（日历、提醒、rag 问答均如此实现）。
下面是一个可直接复制的最小插件（假设放在你的包 `myplug` 中）：

```python
# myplug/plugin.py
from pathlib import Path

from fastapi import APIRouter
from starlette.responses import JSONResponse

from briefdesk.plugin.base import Plugin, PluginContext, WebPlugin


class MyPlugin(Plugin, WebPlugin):
    name = "myplug"          # 全局唯一；/plugin-assets/<name>/ 与 /api/plugins 元数据用它
    version = "1.0.0"
    dependencies: tuple[str, ...] = ()   # 依赖其它插件则写名字，如 ("ai_provider",)
    conflicts: tuple[str, ...] = ()      # 互斥声明：同时入选时按 PLUGINS 先列者保留
    core = False             # False = 可选插件，受 PLUGINS 过滤

    def router(self) -> APIRouter:
        r = APIRouter()

        @r.get("/api/myplug/hello")
        async def hello() -> JSONResponse:
            return JSONResponse({"hello": "world"})

        return r

    def asset_dir(self) -> Path | None:
        # 可选：返回你的前端资源目录，核心以 /plugin-assets/myplug/ 动态服务
        return Path(__file__).parent / "ui"

    async def setup(self, ctx: PluginContext) -> None:
        ctx.register_router(self.router())
        asset_dir = self.asset_dir()
        if asset_dir is not None:
            ctx.register_plugin_assets(self.name, str(asset_dir))

    async def activate(self, ctx: PluginContext) -> None: ...

    async def teardown(self) -> None: ...


plugin = MyPlugin()  # 模块底部必须暴露名为 plugin 的实例（entry point 指向它）
```

前端约定：`asset_dir` 目录下的文件经 `/plugin-assets/myplug/<path>` 服务（浏览器直连，可引用
`/ui/app.js` 导出的助手）；`app.js` 提供的公共助手清单见 `tests/test_web_plugins.py` 的对账表
（`esc`/`escAttr`/`showToast`/`registerPluginView` 等）。

## 2. StagePlugin 槽位契约

StagePlugin 参与消息处理管道。管道骨架 `briefdesk/pipeline.py` 按槽位顺序调度，
同槽位内按 `priority` 升序（数字小者先跑）：

| 槽位 | 调用时机 | 锁纪律 | 失败/早退语义 |
|------|----------|--------|----------------|
| `enrich` | 入口过滤后、分类前（OCR 等输入增强）；`before_run` 锁外、`run` 锁内 | `before_run` 锁外（网络调用只允许在这里）；`run` 锁内 | 可选插件缺失/禁用时 enrich 槽为空：纯占位符图片消息被入口过滤，混合消息降级纯文本；置位 `vision_without_ocr` 公告 |
| `classify` | enrich 之后；对每批消息调 AI 并把 `ClassifyOutcome` 写入 `batch.outcomes` | `run` 锁内（严禁网络调用——预计算放 `before_run`） | 引擎抛错 → 整批进 `failed` 保留待回填；`outcomes` 缺失按契约违约整批保留 |
| `dedup` | classify 之后；判重/入库/缓存 | `run` 锁内；`after_run` 向量落库持 `storage_lock`（内部有写锁重试退避，见 T09 取舍） | 判重失败的消息保留待回填；`processed` 标记只给确定终态的行 |
| `post_insert` | 入库后的派生处理（合并、rag 索引） | `run` 锁内 | 单阶段异常只记日志，不回滚已入库卡片 |

通用规则：

- **锁纪律**：`run` 在全局 `storage_lock`（`briefdesk.db`）内执行——单连接 + 隐式事务下，
  锁外 commit 会把管道未完成的多步写一并提交。**任何远程/网络调用必须在 `before_run`
  （锁外）完成**，`run` 内只有本地 SQLite。
- **早退语义**：`process_all_batches` 在「无启用类别 / classify 或 dedup 阶段缺失 / 分类全失败」
  时返回 False——整批不标记 processed，回填窗口内自动重试；阶段插件不得自行标记
  processed 之外的行。
- **可选依赖**：rapidocr/onnxruntime 这类可选运行时依赖应在 `setup` 里延迟导入，
  缺失时抛 `PluginDisabledError`（禁用本插件，不影响其余功能）。

## 3. 生命周期与失败补偿

装配顺序（见 `briefdesk/plugin/manager.py`）：

```
发现 → 过滤（PLUGINS）→ 互斥仲裁 → 依赖拓扑排序 → setup_all → （HTTP 启动）→ activate_all → … → teardown_all（逆序）
```

- **setup**：构造资源、校验配置、注册端口。可抛 `PluginDisabledError` 自禁用（非致命，
  `/api/plugins` 显示原因）；抛其它异常 → 框架 best-effort 调用你的 `teardown` 后标
  `failed`；`PLUGINS_REQUIRED` 名单内的失败会致命中止启动。
- **activate**：启动副作用（拉起后台任务、注册监听）。服务器就绪后才调用——不要在这里
  做配置校验（太晚）。
- **teardown**：**幂等**地回收你在 setup/activate 里注册/启动的一切；会被框架调用多次。
- **失败补偿契约（规范性）**：**资源获取先于注册；任何注册行为（`ctx.ai`、`ctx.dedup`、
  `register_stage`、`subscribe_event`、`register_router` 等）必须可被你自己的 teardown
  幂等回收**。把 setup 内所有可失败步骤放在注册之前——注册之后 setup 不再有可失败
  步骤，失败窗口即不残留半装配端口。内置 `ai_provider`（teardown 清 `ai_ports` 与
  `ctx.ai`）与 `dedup`（teardown 清 `ctx.dedup`）是参照实现。
- **后台任务**：activate 里 `asyncio.create_task` 的任务要保存引用并在 teardown 中
  `cancel()` + `await`（参考 rag 插件的 `_backfill_task`/`_gc_task` 处理）。

## 4. 发现方式与配置

**发现**两条路：

1. **entry points**（推荐）：在你的包 `pyproject.toml` 声明

   ```toml
   [project.entry-points."briefdesk.plugins"]
   myplug = "myplug.plugin:plugin"
   ```

   等号右侧指向**模块级插件实例**（不是类）。
2. **PLUGIN_PATH**：环境变量 `PLUGIN_PATH` 指向一个含插件模块的目录（本地开发用），
   框架按同样约定发现 `plugin` 实例。

**启用**：`PLUGINS` 环境变量为可选插件的显式白名单（逗号分隔，无通配语义）——

- 不在列表中的可选插件被禁用（disabled，原因「未启用」）；未知名打 WARNING。
- `core = True` 的核心插件**恒装配**，不受 PLUGINS 过滤。
- `dependencies` 声明依赖（拓扑排序保证被依赖者先 setup；未知依赖/依赖环 → 自禁用）。
- `conflicts` 声明互斥（如 weflow 与 weflow-legacy 二选一）：互斥对同时入选时按
  PLUGINS 列表位置先列者保留，落选者 disabled 并注明原因；**与核心插件互斥时可选侧
  让位**（核心恒胜）。
- `PLUGINS_REQUIRED` 名单内的插件装配失败会抛 `PluginError` 致命中止启动（默认为空，
  全部失败隔离降级）。

## 5. 事件订阅与端口获取

**事件订阅**：核心事件总线 `briefdesk.events`（模块级单例 `event_bus`，main 注入
PluginContext）。插件在 setup 里经 `ctx.subscribe_event(event, handler)` 订阅；
handler 为**同步**函数（发布方在持锁路径上同步调用，处理器异常只记日志不传播）。

```python
from briefdesk.events import EVENT_ITEMS_DELETED

async def setup(self, ctx: PluginContext) -> None:
    ctx.subscribe_event(EVENT_ITEMS_DELETED, self._on_items_deleted)

def _on_items_deleted(self, item_ids: list[str]) -> None:
    ...  # 卡片被删除：清理你的派生数据（缓存/索引/向量）
```

核心已发布的事件：`EVENT_ITEMS_DELETED`（"items_deleted"，删除卡片时同步清理派生数据的
钩子——rag 的孤儿对账、dedup 的缓存清理都靠它）。自行 `event_bus.publish` 的新事件
（如 rag 的）与核心事件共用同一总线和 `ctx.subscribe_event` 订阅路径。

**端口获取**（仅核心提供的两个服务端口）：

- `ctx.dedup`：`DedupService` 端口（dedup 插件 setup 注册）——同进程内共享去重缓存，
  merge 等后置阶段经此查询。
- `ctx.ai`：`AIProvider` 端口（ai_provider 插件 setup 注册）——`chat`/`embed_texts` 等。
  **引擎代码不要直接 import 供应商插件**：`briefdesk.ai_ports` 提供函数式端口
  （`ai_ports.chat(...)` 等），插件未装配时结构化报错。

依赖 `ai_provider` 的插件应声明 `dependencies=("ai_provider",)`，setup 里 `ctx.ai is None`
即视为依赖未就绪（自禁用并提示）。

## 6. 配置与密钥

- **配置类**：继承 `briefdesk.settings_base.KeyringSettingsBase`（pydantic-settings），
  用 `model_config` 设 `env_prefix`；字段注释写清对应环境变量与默认值。
- **密钥字段**：`SecretStr` 类型 + 类级 `KEYRING_FIELDS` 映射（字段名 → 环境变量名）。
  解析链：**系统密钥环（keyring）→ 环境变量 → .env → 默认值**；`repr`/序列化自动掩码。
- **密钥写入**：`briefdesk secrets set <NAME>`（CLI）或 UI「设置 → 密钥」密钥区；
  **禁止把真实密钥写进 .env 之外的任何仓库文件、日志或示例**。
- **设置面板**：在插件 setup 里把 schema 注册给设置页（参考 rag 插件的
  `build_settings_schema(RagSettings, plugin=...)`），字段类型/默认值/约束/密钥状态
  由框架统一生成，密钥不回传明文。
- **必填校验**：`briefdesk.plugin.config_helpers.validate_required_config(settings, {...})`
  统一检查必填项（缺失时一次性列出全部环境变量名），在 setup 抛 `PluginDisabledError`。

## 7. 测试建议

- **测试位置**：放你自己的包里或随 `tests/` 提交均可；跑法与仓库一致（`python -m pytest tests/`）。
- **unittest 惯例（pytest-asyncio 迁移完成前，见计划 T26）**：异步用例继承
  `unittest.IsolatedAsyncioTestCase`；配置隔离用 `patch.object(config, ...)`；
  内存库参考 `tests/test_db.py::_InMemoryDbTest` 的 `:memory:` + `init_schema` 样板。
  事件循环与模块级单例（`db.get_db`、`stages`、`status`）在用例间需要显式复位——
  各测试基类的 `asyncSetUp/asyncTearDown` 是现成模板。
- **pytest 风格（迁移完成后）**：直接写 `async def test_x()`；共享夹具从
  `tests/conftest.py` 导入（`memory_db`/`temp_db`/`fake_embed_provider`）——它们分别
  提供内存库、临时文件库与可配 enabled 的假嵌入 Provider。
- **不触碰真实环境**：不连真实上游、不写真实密钥、不发真实 AI 请求——用
  `unittest.mock.AsyncMock` / `httpx.MockTransport` 打桩（参考
  `tests/test_source_robustness.py` 的 SSE 真身测试）。
- **静态守卫**：涉及前端资源或 app.js 接线时，参考 `tests/test_announcements.py::UiWiringTest`
  的「读源码断言关键写法」风格补守卫。
