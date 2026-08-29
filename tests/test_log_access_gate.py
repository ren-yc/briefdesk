"""访问日志闸门守卫：uvicorn/FastAPI 的请求日志仅在 LOG_LEVEL=DEBUG 时输出。

访问日志是 uvicorn 硬编码的 INFO 级记录，而本项目是单用户本机服务（Host 白
名单只放 localhost/127.0.0.1），逐请求刷屏会把同步/去重/分类等业务行冲散。
故默认静默、DEBUG 放行。

这里钉住三件容易在重构中被破坏的事：
  1. 判据同源——`_AccessLogGate` 与 main.py 传给 uvicorn.Config 的 access_log
     都取 `access_log_enabled()`，不各自读 config；
  2. 闸门只挡 uvicorn.access，uvicorn.error / 业务 logger 的 INFO 不受影响；
  3. 格式化逻辑（状态短语还原、查询参数掩码）在放行路径上仍然生效——闸门是
     级别门，不是格式改动（掩码行为本身另见 test_log_redaction.py）。
"""

import logging
import unittest
from collections.abc import Callable
from unittest.mock import patch

from briefdesk.config import config
from briefdesk.logger import (
    _ACCESS_LOGGER,
    _TRACE_LEVEL,
    _AccessLogGate,
    access_log_enabled,
    configured_level,
    setup_logging,
)


def _access_record(status: int = 200) -> logging.LogRecord:
    """构造一条与 uvicorn 实际调用形状一致的 access 记录。"""
    return logging.LogRecord(
        name=_ACCESS_LOGGER,
        level=logging.INFO,  # uvicorn 硬编码 access_logger.info(...)
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", "/api/items?limit=20", "1.1", status),
        exc_info=None,
    )


class ConfiguredLevelTest(unittest.TestCase):
    def test_maps_standard_names(self) -> None:
        for raw, expected in [
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ]:
            with patch.object(config, "log_level", raw):
                self.assertEqual(configured_level(), expected, msg=raw)

    def test_accepts_lowercase(self) -> None:
        with patch.object(config, "log_level", "debug"):
            self.assertEqual(configured_level(), logging.DEBUG)

    def test_maps_uvicorn_trace(self) -> None:
        # uvicorn_log_level() 接受 "trace"，标准 logging 无此级别；两处必须同解，
        # 否则 LOG_LEVEL=TRACE 会回退 INFO——比 DEBUG 更细的意图反而丢了
        with patch.object(config, "log_level", "TRACE"):
            self.assertEqual(configured_level(), _TRACE_LEVEL)
            self.assertLess(_TRACE_LEVEL, logging.DEBUG)

    def test_illegal_value_falls_back_to_info(self) -> None:
        for raw in ("", "verbose", "42", "-"):
            with patch.object(config, "log_level", raw):
                self.assertEqual(configured_level(), logging.INFO, msg=repr(raw))

    def test_warn_alias_resolves_to_warning(self) -> None:
        # logging.WARN 是 WARNING 的真别名（=30），故 LOG_LEVEL=WARN 生效而非回退。
        # 注意 uvicorn_log_level() 不收 "warn"（回退 "info"）——两处对这个别名
        # 口径不同，但都比 DEBUG 粗，不影响访问日志静默这一结论
        with patch.object(config, "log_level", "WARN"):
            self.assertEqual(configured_level(), logging.WARNING)
            self.assertFalse(access_log_enabled())

    def test_non_level_attribute_name_falls_back(self) -> None:
        # getattr(logging, raw) 可能命中非级别属性（logging.Filter 等）；
        # 必须回退 INFO 而不是把类对象当级别用
        for raw in ("FILTER", "HANDLER", "RAISEEXCEPTIONS"):
            with patch.object(config, "log_level", raw):
                self.assertEqual(configured_level(), logging.INFO, msg=raw)


class AccessLogEnabledTest(unittest.TestCase):
    def test_enabled_only_at_debug_and_finer(self) -> None:
        for raw, expected in [
            ("TRACE", True),
            ("DEBUG", True),
            ("INFO", False),
            ("WARNING", False),
            ("ERROR", False),
            ("CRITICAL", False),
        ]:
            with patch.object(config, "log_level", raw):
                self.assertEqual(access_log_enabled(), expected, msg=raw)

    def test_default_config_keeps_access_log_silent(self) -> None:
        # 默认 LOG_LEVEL=INFO，即「装好就静默」；若默认值哪天改成 DEBUG，
        # 这条会失败并提醒重新评估刷屏问题
        self.assertEqual(type(config).model_fields["log_level"].default, "INFO")
        with patch.object(config, "log_level", "INFO"):
            self.assertFalse(access_log_enabled())


class AccessLogGateTest(unittest.TestCase):
    def test_gate_drops_at_info(self) -> None:
        gate = _AccessLogGate()
        with patch.object(config, "log_level", "INFO"):
            self.assertFalse(gate.filter(_access_record()))

    def test_gate_passes_at_debug(self) -> None:
        gate = _AccessLogGate()
        with patch.object(config, "log_level", "DEBUG"):
            self.assertTrue(gate.filter(_access_record()))

    def test_gate_reads_level_per_record(self) -> None:
        # 级别现取而非构造时固化：同一个 gate 实例在配置切换后必须改变判定，
        # 否则 setup_logging / uvicorn.Config 的调用先后会决定行为
        gate = _AccessLogGate()
        with patch.object(config, "log_level", "INFO"):
            self.assertFalse(gate.filter(_access_record()))
        with patch.object(config, "log_level", "DEBUG"):
            self.assertTrue(gate.filter(_access_record()))

    def test_gate_ignores_status_code(self) -> None:
        # 闸门是级别门，不按状态码放行：4xx/5xx 的排障出口是 uvicorn.error 与
        # 各业务 logger，不是 access log（避免"部分放行"造成半套日志的错觉）
        gate = _AccessLogGate()
        with patch.object(config, "log_level", "INFO"):
            for status in (200, 304, 400, 403, 500):
                self.assertFalse(gate.filter(_access_record(status)), msg=str(status))


class SetupLoggingInstallsGateTest(unittest.TestCase):
    """setup_logging 的全局副作用测试：逐个用例保存并还原 logging 状态。"""

    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.access = logging.getLogger(_ACCESS_LOGGER)
        self._root_level = self.root.level
        self._root_handlers = list(self.root.handlers)
        self._access_level = self.access.level
        self._access_handlers = list(self.access.handlers)
        self._access_filters = list(self.access.filters)
        self._access_propagate = self.access.propagate

    def tearDown(self) -> None:
        self.root.setLevel(self._root_level)
        self.root.handlers[:] = self._root_handlers
        self.access.setLevel(self._access_level)
        self.access.handlers[:] = self._access_handlers
        self.access.filters[:] = self._access_filters
        self.access.propagate = self._access_propagate

    def test_installs_gate_on_access_logger(self) -> None:
        self.access.filters.clear()
        setup_logging(logging.INFO)
        self.assertTrue(
            any(isinstance(f, _AccessLogGate) for f in self.access.filters),
            "setup_logging 必须给 uvicorn.access 挂上闸门",
        )

    def test_repeated_setup_does_not_stack_gates(self) -> None:
        self.access.filters.clear()
        for _ in range(3):
            setup_logging(logging.INFO)
        gates = [f for f in self.access.filters if isinstance(f, _AccessLogGate)]
        self.assertEqual(len(gates), 1, "重复 setup_logging 不应叠加 filter")

    def test_gate_survives_handler_clear(self) -> None:
        # setup_logging 无条件 handlers.clear() + propagate=True，会复活 uvicorn
        # 用 access_log=False 关掉的访问日志；filter 挂在 logger 上而非 handler
        # 上，正是为了在这种重配后依然生效
        setup_logging(logging.INFO)
        self.access.handlers.clear()
        self.access.propagate = True
        self.assertTrue(any(isinstance(f, _AccessLogGate) for f in self.access.filters))

    def _records_reaching_root(self, emit: Callable[[], None]) -> list[logging.LogRecord]:
        """收集 emit() 期间真正落到根 handler 的记录。

        不用 assertLogs：它会自己往目标 logger 挂 handler 并临时改级别，测不到
        「记录有没有穿过闸门抵达输出端」这件事。
        """
        captured: list[logging.LogRecord] = []
        probe = logging.Handler()
        probe.emit = captured.append  # type: ignore[method-assign,assignment]
        self.root.addHandler(probe)
        try:
            emit()
        finally:
            self.root.removeHandler(probe)
        return captured

    def test_access_silent_at_info_but_error_logger_speaks(self) -> None:
        # 端到端：INFO 下 access 静默，uvicorn.error 与业务 logger 正常输出。
        # 后两者是对照——若闸门误伤它们，「静默」就不是精准降噪而是丢日志
        setup_logging(logging.INFO)
        with patch.object(config, "log_level", "INFO"):
            self.assertEqual(
                self._records_reaching_root(
                    lambda: self.access.handle(_access_record())
                ),
                [],
            )
            for name, msg in [
                ("uvicorn.error", "Application startup complete."),
                ("briefdesk.pipeline", "入库 3 条"),
            ]:
                got = self._records_reaching_root(
                    lambda n=name, m=msg: logging.getLogger(n).info(m)  # type: ignore[misc]
                )
                self.assertEqual([r.getMessage() for r in got], [msg], msg=name)

    def _access_records_at(self, log_level: str) -> list[logging.LogRecord]:
        """在给定 LOG_LEVEL 下，投一条 access 记录并返回抵达根 handler 的记录。

        logger 自身级别显式放开到 DEBUG（不依赖别处遗留的级别），使唯一的变量
        是 LOG_LEVEL——挡下记录的只能是闸门。
        """
        setup_logging(logging.DEBUG)
        self.access.setLevel(logging.DEBUG)
        with patch.object(config, "log_level", log_level):
            return self._records_reaching_root(
                lambda: self.access.handle(_access_record())
            )

    def test_access_emitted_at_debug(self) -> None:
        got = self._access_records_at("DEBUG")
        self.assertEqual(len(got), 1, "DEBUG 下访问日志必须抵达输出端")

    def test_access_dropped_at_info(self) -> None:
        # 与上一条成对：同一条记录、同一条 logger 管线，仅 LOG_LEVEL 不同
        self.assertEqual(
            self._access_records_at("INFO"), [], "INFO 下访问日志不得落到任何 handler"
        )


class UvicornContractTest(unittest.TestCase):
    """钉住所依赖的 uvicorn 外部契约与 main.py 的接线。"""

    def test_config_access_log_false_disables_logger(self) -> None:
        # 依赖点：access_log=False 时 uvicorn 清 handler 并断 propagate，协议层
        # 据 hasHandlers() 连 LogRecord 都不构造。uvicorn 若改掉这个实现，
        # main.py 的"零开销"前提失效——此处提前暴露
        import uvicorn

        access = logging.getLogger(_ACCESS_LOGGER)
        saved = (list(access.handlers), access.propagate, access.level)
        try:
            uvicorn.Config("briefdesk.server:app", access_log=False, log_config=None)
            self.assertEqual(access.handlers, [])
            self.assertFalse(access.propagate)
            self.assertFalse(access.hasHandlers())
        finally:
            access.handlers[:] = saved[0]
            access.propagate = saved[1]
            access.setLevel(saved[2])

    def test_main_passes_access_log_flag_to_uvicorn(self) -> None:
        # 防回归：接线被删掉时闸门仍能挡住输出，但每请求白建 LogRecord；
        # 源码级断言比行为断言更早发现这种"静默退化"
        import inspect

        from briefdesk import main as main_mod

        src = inspect.getsource(main_mod._run)
        self.assertIn("access_log=access_log_enabled()", src)


if __name__ == "__main__":
    unittest.main()
