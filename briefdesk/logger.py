"""日志配置 — 使用标准 logging 模块，格式对齐 uvicorn/FastAPI（彩色、10 字符级别）。

支持 LOG_LEVEL 配置（.env 或环境变量，DEBUG / INFO / WARNING / ERROR，默认
INFO）：DEBUG 开启逐条细节（每条事件、每次请求、每条过滤决策），INFO 只保留
阶段与汇总行。级别经 briefdesk.config.Settings 读取（与全项目配置同源），
直接读 os.environ 看不到 .env 内容。
"""

import http
import logging
import sys
from typing import Any, cast

from briefdesk.config import config

# uvicorn 相关 logger：默认被 uvicorn 的 LOGGING_CONFIG 挂上自己的 formatter
# （无时间戳）且 propagate=False，输出到 stderr。统一改为传播到根 logger，
# 由本模块的 _BriefFormatter 输出（时间戳 + 模块名 + 彩色级别）。
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")

# ANSI 颜色码
_RESET = "\033[0m"
_COLORS = {
    "TRACE": "\033[36m",  # 青色（uvicorn 自定义级别 5）
    "DEBUG": "\033[36m",  # 青色
    "INFO": "\033[32m",  # 绿色
    "WARNING": "\033[33m",  # 黄色
    "ERROR": "\033[31m",  # 红色
    "CRITICAL": "\033[1;31m",  # 粗体红色
}

# 级别前缀：name + ":" 补齐到 14 字符（含 ANSI 重置码 4 字符，
# 可见宽度约 10 字符，各级别一致对齐）
_LEVEL_PREFIX = {
    name: f"{name}{_RESET}:".ljust(14)
    for name in ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
}

# 访问日志状态码着色（按百位分组，对齐 uvicorn AccessFormatter）
_STATUS_CODE_COLORS = {
    1: "\033[1;37m",  # 亮白（1xx）
    2: "\033[32m",  # 绿（2xx）
    3: "\033[33m",  # 黄（3xx）
    4: "\033[31m",  # 红（4xx）
    5: "\033[1;31m",  # 亮红（5xx）
}


def _status_text(status_code: int) -> str:
    """状态码 + HTTP 状态短语（如 "200 OK"），按百位分组着色。"""
    try:
        phrase = http.HTTPStatus(status_code).phrase
    except ValueError:
        phrase = ""
    text = f"{status_code} {phrase}".rstrip()
    color = _STATUS_CODE_COLORS.get(status_code // 100)
    return f"{color}{text}{_RESET}" if color else text


class _BriefFormatter(logging.Formatter):
    """uvicorn 兼容的格式化器：彩色 LEVEL:       message（级别名补齐 10 字符）。

    对 uvicorn.access 记录额外还原 HTTP 状态短语（uvicorn AccessFormatter 的
    行为，如 "200 OK"），其余格式（时间戳/级别/模块名）与普通日志一致。
    """

    def format(self, record: logging.LogRecord) -> str:
        lvl = record.levelname
        color = _COLORS.get(lvl, "")
        record.levelprefix = f"{color}{_LEVEL_PREFIX.get(lvl, lvl + _RESET + ':')}"
        return super().format(record)

    def formatMessage(self, record: logging.LogRecord) -> str:
        # uvicorn.access 的日志调用形如
        #   info('%s - "%s %s HTTP/%s" %d', client_addr, method, path, version, status)
        # 还原 uvicorn AccessFormatter 的访问行格式（含状态短语），
        # 其余记录走默认 %(message)s 插值。
        if record.name == "uvicorn.access" and record.args:
            try:
                client_addr, method, full_path, http_version, status_code = record.args
            except (TypeError, ValueError):
                pass
            else:
                record.message = (
                    f'{client_addr} - "{method} {full_path} HTTP/{http_version}" '
                    f"{_status_text(int(cast(Any, status_code)))}"
                )
                record.args = ()
        return super().formatMessage(record)


class _MessageFilter(logging.Filter):
    """按子串过滤指定 logger 的日志消息（命中任一子串即丢弃该条）。"""

    def __init__(self, *substrings: str):
        super().__init__()
        self._substrings = substrings

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(s in msg for s in self._substrings)


def setup_logging(level: int | None = None) -> None:
    """配置根 logger，所有子 logger 自动继承此格式。

    level 为 None 时读取 config.log_level（.env 的 LOG_LEVEL，默认 INFO）。
    """
    root = logging.getLogger()
    if level is None:
        raw = config.log_level.upper()
        level = getattr(logging, raw, logging.INFO)
    assert level is not None
    root.setLevel(level)

    # 避免重复添加 handler
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            _BriefFormatter(
                "%(asctime)s %(levelprefix)s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)

    # uvicorn/FastAPI 日志统一走根 handler（时间戳 + 模块名 + 彩色级别）。
    # 配合 uvicorn.Config(log_config=None) 使用：启动阶段不再 dictConfig 覆盖。
    # 级别门由 uvicorn 按 log_level 设置（见 uvicorn_log_level），根级别兜底。
    for name in _UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    # 降低第三方库的日志噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    # PIL（Pillow 图片解码）：DEBUG 输出 STREAM/IHDR 等字节流噪音，压到
    # WARNING——噪音消失但真实告警保留。PIL.* 子 logger 无独立级别，
    # 有效级别沿父链解析到 "PIL"，一并覆盖。
    logging.getLogger("PIL").setLevel(logging.WARNING)

    # RapidOCR 图片无文字时抛 RapidOCRError 并记 WARNING，属正常现象
    # （ocr.py 已把该异常视为"未识别到文字"返回空串，不向调用方抛错；
    # 此处仅压制 rapidocr 自身 logger 的 WARNING 噪音）。其 logger 自带
    # handler 且 propagate=False，根 logger 级别压不住，须在 logger 上挂 filter。
    # 注意：logger 实例须在 RapidOCR 首次构造前就拿到（logging 按名注册表
    # 返回同一实例，filter 在 handler 添加后依然生效）。
    logging.getLogger("RapidOCR").addFilter(
        _MessageFilter("The text detection result is empty")
    )


def fmt_dur(seconds: float) -> str:
    """统一耗时格式：<1s 显示毫秒，其余显示秒（1 位小数）。"""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def uvicorn_log_level() -> str:
    """把 config.log_level 映射为 uvicorn 的 log_level（uvicorn 自身 logger 的级别门）。

    与 setup_logging 保持同一来源；非法值回退 "info"。
    """
    level = config.log_level.lower()
    if level not in ("critical", "error", "warning", "info", "debug", "trace"):
        return "info"
    return level
