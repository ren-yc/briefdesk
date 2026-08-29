"""入口 — 运行时生命周期管理（启动/优雅关闭）。

轮询周期业务编排见 briefdesk/poll_cycle.py；插件装配见
briefdesk/plugin/manager.py —— 消息源为内置插件（weflow-legacy/qqflow），
启用/禁用走 PLUGINS / PLUGINS_DISABLED 配置。
"""

import asyncio
import logging
import signal
import sys
import time as time_module
from collections.abc import Callable

import uvicorn
from fastapi import APIRouter

from briefdesk import stages
from briefdesk.config import config
from briefdesk.db import (
    apply_pending_restore,
    close_db,
    get_db,
    purge_expired_ignored,
    upsert_session,
)
from briefdesk.events import event_bus
from briefdesk.logger import (
    access_log_enabled,
    fmt_dur,
    setup_logging,
    uvicorn_log_level,
)
from briefdesk.pipeline import process_all_batches
from briefdesk.plugin.base import PluginContext
from briefdesk.plugin.manager import PluginManager
from briefdesk.poll_cycle import run_poll_cycle
from briefdesk.realtime import signal_shutdown
from briefdesk.server import (
    app,
    include_plugin_router,
    register_plugin_assets,
    set_plugins_info_callback,
    set_refresh_sessions_callback,
    set_settings_schema_callback,
)
from briefdesk.sources_base import SourceRuntime
from briefdesk.status import register_source_client, set_listener
from briefdesk.sync import set_sync_callback, trigger_sync
from briefdesk.types import InternalMessage

logger = logging.getLogger(__name__)

# ── Source ──


async def _poll_all(sources: list[SourceRuntime]) -> None:
    """串行轮询全部消息源（单次同步内依次回填）。"""
    for s in sources:
        await run_poll_cycle(s)


async def _refresh_all(sources: list[SourceRuntime]) -> None:
    """并行刷新全部消息源会话；结果统一落库（源不写库）。

    单个源刷新失败不影响其它源的会话结果落库。
    """
    results = await asyncio.gather(
        *(s.refresh_sessions() for s in sources), return_exceptions=True
    )
    for source, sessions in zip(sources, results):
        if isinstance(sessions, BaseException):
            logger.error("[%s] 会话刷新失败: %s", source.name, sessions)
            continue
        for s in sessions:
            await upsert_session(
                s.source,
                s.session_id,
                s.name,
                s.is_group,
                s.is_official,
                last_active_at=s.last_active_at or None,
            )


def _start_listener(s: SourceRuntime) -> None:
    """启动单个源的实时监听；批次处理由 pipeline 承接。

    用函数参数而非闭包捕获循环变量，避免所有监听器拿到最后一个源。
    """
    async def _on_batch(batch: list[InternalMessage]) -> None:
        # 实时路径不消费返回值（早退标志仅供 backfill 轮询推进水位用）
        await process_all_batches(
            batch,
            s.client,
            batch_size=config.realtime_batch_max_count,
            origin="realtime",
        )

    s.start(_on_batch)


# ── Startup ──


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, on_shutdown: Callable[[], None]
) -> None:
    """注册优雅关闭信号处理器。

    Windows 上 loop.add_signal_handler 不可用，回退到 signal.signal +
    call_soon_threadsafe 把关闭调度回事件循环线程。
    """
    try:
        loop.add_signal_handler(signal.SIGINT, on_shutdown)
        loop.add_signal_handler(signal.SIGTERM, on_shutdown)
    except NotImplementedError:
        signal.signal(
            signal.SIGINT, lambda _s, _f: loop.call_soon_threadsafe(on_shutdown)
        )
        signal.signal(
            signal.SIGTERM, lambda _s, _f: loop.call_soon_threadsafe(on_shutdown)
        )


async def _cancel_pending_tasks() -> None:
    """取消并等待所有未完成任务，避免 loop.close() 报 "Task was destroyed"。

    取消的生成器还会有一轮子任务清理（见 server.py api_stream 的 finally），
    因此循环清理直到没有 pending 任务。
    """
    current = asyncio.current_task()
    for _ in range(5):
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if not pending:
            return
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    remaining = [t for t in asyncio.all_tasks() if not t.done()]
    logger.warning("5 轮后仍有未完成任务: %s", [t.get_name() for t in remaining])


async def _reap_task(task: asyncio.Task[None] | None, timeout: float = 5.0) -> None:
    """取消并等待单个任务终结（关闭期收尾用，幂等）。

    已完成（含已取消）的任务原样返回；未完成任务先 cancel 再限时等待。超时或
    任务自身异常都只记日志、不向上传播——关闭是 best-effort，等待超时的残留
    任务交由 _cancel_pending_tasks 兜底再清理。
    """
    if task is None or task.done():
        return
    task.cancel()
    try:
        # shield：wait_for 超时只取消包装层，任务保持 pending 留给兜底清理，
        # 避免二次 cancel 打断其内部清理流程
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        logger.warning(
            "关闭等待超时：任务 %s 未在 %.0fs 内终结（留待兜底清理）",
            task.get_name(),
            timeout,
        )
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("关闭期间任务 %s 异常", task.get_name())


async def _run() -> None:
    """启动顺序：数据库 → 插件装配 → HTTP 服务器 → 实时监听 → 回填。

    全程处于唯一清理 finally 的保护内：setup/activate/listener 启动等任何一步
    失败（如 PLUGINS_REQUIRED 插件装配失败抛 PluginError）都会执行 teardown_all
    / close_db / _cancel_pending_tasks——否则 aiosqlite 的非 daemon worker 线程
    不关闭，解释器退出 join 挂死，只能强杀进程。
    """
    manager = PluginManager()
    runtimes: list[SourceRuntime] = []
    server_task: asyncio.Task[None] | None = None
    initial_sync_task: asyncio.Task[None] | None = None

    try:
        # 1. 应用待恢复备份（上传恢复后重启生效：先替换正式库再开库）
        if await apply_pending_restore():
            logger.info("数据库已从恢复备份重建")

        # 1.5 初始化数据库
        phase_start = time_module.perf_counter()
        await get_db()
        logger.info(
            "数据库初始化完成 (%s)", fmt_dur(time_module.perf_counter() - phase_start)
        )

        # 1.6 清理过期的已忽略条目（IGNORED_EXPIRY_HOURS = 0 表示禁用）
        if config.ignored_expiry_hours > 0:
            phase_start = time_module.perf_counter()
            deleted = await purge_expired_ignored(config.ignored_expiry_hours)
            logger.info(
                "已清理过期已忽略条目: %d 条 (%s)",
                deleted,
                fmt_dur(time_module.perf_counter() - phase_start),
            )

        # 2. 发现并装配插件（消息源经 ctx.register_source 注册，暂不启动监听；
        #    去重缓存预热由 dedup 插件在 setup 阶段完成——HTTP 服务启动前、
        #    源监听启动前，避免首个批次在 _storage_lock 内触发全量嵌入阻塞）
        phase_start = time_module.perf_counter()
        routers: list[APIRouter] = []
        plugin_assets: dict[str, str] = {}
        ctx = PluginContext(
            config=config,
            publish_event=event_bus.publish,
            subscribe_event=event_bus.subscribe,
            register_source=runtimes.append,
            register_stage=stages.register_stage,
            register_router=routers.append,
            register_plugin_assets=plugin_assets.__setitem__,
        )
        stages.set_context(ctx)  # 管道骨架（pipeline）经此读取阶段与服务端口
        await manager.setup_all(ctx)
        # 2.1 Web 插件挂载（HTTP 服务启动前）：路由 + 静态资源 + 插件元数据
        for r in routers:
            include_plugin_router(r)
        for name, directory in plugin_assets.items():
            register_plugin_assets(name, directory)
        set_plugins_info_callback(manager.infos)
        set_settings_schema_callback(manager.settings_schema)
        if not runtimes:
            raise ValueError(
                "没有可用的消息源插件（检查 PLUGINS / PLUGINS_DISABLED 配置与上方插件日志）"
            )
        for s in runtimes:
            register_source_client(s.name, s.client)
        set_sync_callback(lambda: _poll_all(runtimes))
        set_refresh_sessions_callback(lambda: _refresh_all(runtimes))
        logger.info(
            "消息源已安装: %s (%s)",
            [s.name for s in runtimes],
            fmt_dur(time_module.perf_counter() - phase_start),
        )

        # 3. 先启动 HTTP 服务器
        # 注：不传 reload —— uvicorn 0.34+ 的 reload 只存在于 uvicorn.run() 的
        # supervisor 路径，直接 Server(config).serve() 会静默忽略该参数。
        # log_config=None：禁用 uvicorn 默认 LOGGING_CONFIG（其 formatter 无时间戳
        # 且 propagate=False），uvicorn/FastAPI 日志统一由 setup_logging 的根
        # handler 输出（时间戳 + 模块名）；log_level 跟随 LOG_LEVEL 环境变量。
        # access_log：请求日志仅 DEBUG 输出（判据见 logger.access_log_enabled）。
        # 传 False 时 uvicorn 清空 uvicorn.access 的 handler 并断 propagate，协议层
        # 据 hasHandlers() 连 LogRecord 都不构造——比事后 filter 丢弃更省；
        # logger.setup_logging 里的 _AccessLogGate 是绕过本路径时的第二道防线。
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="localhost",
                port=config.server_port,
                log_level=uvicorn_log_level(),
                log_config=None,
                access_log=access_log_enabled(),
                timeout_graceful_shutdown=5,
            )
        )
        server_task = asyncio.create_task(server.serve())

        # 4. 插件激活（当前各内置插件 activate 均无副作用；消息源实时监听须在
        #    uvicorn serve 任务创建后启动，避免事件循环被 SSE 长连接独占阻塞）→
        #    逐个启动实时监听
        phase_start = time_module.perf_counter()
        await manager.activate_all(ctx)
        for s in runtimes:
            _start_listener(s)
        for s in runtimes:
            if s.listener is not None:
                set_listener(s.name, s.listener)
        logger.info(
            "实时监听已启动: %s (%s)",
            [s.name for s in runtimes if s.listener is not None],
            fmt_dur(time_module.perf_counter() - phase_start),
        )

        # 5. 后台执行首轮回填（与 /api/sync 共用 trigger_sync）
        initial_sync_task = trigger_sync(reason="startup")
        if initial_sync_task is None:
            logger.warning("首轮回填跳过：同步已在进行或未注册回调")

        # 6. 注册优雅关闭信号
        # Ctrl+C 触发优雅关闭（SSE 流结束 → should_exit），uvicorn 正常收尾后
        # await server_task 返回。若信号处理未生效（如异常路径），
        # KeyboardInterrupt 由顶层兜底。

        def _shutdown() -> None:
            """Ctrl+C 优雅关闭：让 SSE 流主动结束，再让 uvicorn 走正式退出通道。

            这里只做 should_exit 前必须做的事 —— 不先 signal_shutdown 的话，
            uvicorn 优雅退出会一直等 /api/stream 这些常驻 ASGI 任务。
            其余清理统一在 finally（插件 teardown + 资源关闭）。
            """
            logger.info("关闭中...")
            signal_shutdown()
            server.should_exit = True

        # uvicorn 的 serve() 启动时会用自己的 handle_exit 覆盖信号 handler
        # （capture_signals），Ctrl+C 只设 should_exit、不会 signal_shutdown，
        # SSE 流会干等 timeout_graceful_shutdown 后被强杀。等 uvicorn 启动
        # 完成（started，handle_exit 已注册）后再注册，覆盖回我们的 handler。
        for _ in range(200):  # 最多等 10s（启动失败则跳过注册）
            if server.started:
                break
            await asyncio.sleep(0.05)
        _install_signal_handlers(asyncio.get_running_loop(), _shutdown)

        # 7. 等待服务器退出；随后统一清理（finally 是唯一清理点，
        # 正常退出与异常路径都必达，顺序见 finally 内注释）。
        await server_task
    finally:
        shutdown_start = time_module.perf_counter()
        # 清理顺序即下方语句序：server → initial_sync → 插件逆序 → DB → 残留兜底。
        # cancel 不同步等待、aiosqlite 非 daemon 线程等陷阱见 docs/architecture.md
        # 「运行时与优雅关闭」小节。
        await _reap_task(server_task)
        await _reap_task(initial_sync_task)
        await manager.teardown_all()
        logger.info(
            "插件已关闭 (%s)", fmt_dur(time_module.perf_counter() - shutdown_start)
        )
        # 关闭数据库连接，停掉 aiosqlite 的非 daemon worker 线程，
        # 否则解释器退出 join 该线程时进程挂死
        await close_db()
        logger.info("数据库连接已关闭")
        await _cancel_pending_tasks()
        logger.info(
            "清理完成，退出 (%s)", fmt_dur(time_module.perf_counter() - shutdown_start)
        )


def main() -> None:
    """入口：`briefdesk secrets` 子命令或启动本地服务。"""
    if len(sys.argv) >= 2 and sys.argv[1] == "secrets":
        from briefdesk.secrets_cli import secrets_cli_main

        raise SystemExit(secrets_cli_main(sys.argv[2:]))

    setup_logging()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
