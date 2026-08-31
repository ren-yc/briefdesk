"""日志配置 — 使用标准 logging 模块，格式对齐 uvicorn/FastAPI（彩色、10 字符级别）。

支持 LOG_LEVEL 配置（.env 或环境变量，DEBUG / INFO / WARNING / ERROR /
CRITICAL，另接受 uvicorn 的 TRACE，默认 INFO）：DEBUG 开启逐条细节（每条事件、
每条过滤决策，以及
uvicorn/FastAPI 的每次 HTTP 请求——见 _AccessLogGate），INFO 只保留阶段与汇总行。
级别经 briefdesk.config.Settings 读取（与全项目配置同源），直接读 os.environ
看不到 .env 内容。

行格式为 `时间戳 LEVEL: 来源 message`，其中「来源」是定宽的 logger 短名
（见 short_logger_name）。来源既然已占一列，消息体内不应再重复写源名——
唯一例外是 logger 名本身不含源的通用模块（poll_cycle/pipeline 代某个源
干活），那里以 `[源名] ` 前缀标注。
"""

import http
import logging
import re
import sys
from typing import Any, cast

from briefdesk.config import config

# uvicorn 相关 logger：默认被 uvicorn 的 LOGGING_CONFIG 挂上自己的 formatter
# （无时间戳）且 propagate=False，输出到 stderr。统一改为传播到根 logger，
# 由本模块的 _BriefFormatter 输出（时间戳 + 模块名 + 彩色级别）。
_ACCESS_LOGGER = "uvicorn.access"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", _ACCESS_LOGGER, "uvicorn.asgi")

# uvicorn 自定义的 TRACE 级别值（uvicorn.config.TRACE_LOG_LEVEL）。标准
# logging 没有 TRACE，getattr(logging, "TRACE") 取不到——但 uvicorn_log_level()
# 接受 "trace"，故本模块的级别解析必须认得它，否则 LOG_LEVEL=TRACE 下
# 根级别会回退 INFO，比 DEBUG 更细的意图反而丢了。
_TRACE_LEVEL = 5

# ── 显示名（短名）──
#
# logger 名一律由 logging.getLogger(__name__) 产生，故实际值形如
# briefdesk.plugins.weflow_legacy.normalize（41 字符）。其中
# "briefdesk." 与 "plugins." 共 18 字符在每一行重复且零信息量，且各模块
# 名长不齐（12~41）会让消息起始位置逐行漂移。这里只改**显示**：剥掉这两段
# 公共前缀、定宽补齐，令首列成为稳定可扫的「来源」列。
#
# 关键：不改真实 logger 名（record.name）。测试以 assertLogs(
# "briefdesk.plugins.qqflow.client") 匹配真实名，且 briefdesk.* 层级要能
# 整树设级别——两者都依赖真实名不变。
_NAME_STRIP_PREFIXES = ("briefdesk.", "plugins.")

# 短名首段的别名：模块名用下划线（Python 标识符限制），而插件名/DB
# sessions.source 列/poll_cycle 的 "[%s]" 用连字符。不归一的话同一个源在
# 日志里有两种拼写（weflow_legacy.client 与 [weflow-legacy]），按源名 grep
# 会漏掉一半。
_NAME_ALIASES = {"weflow_legacy": "weflow-legacy"}

# 短名列宽：取全项目最长短名（weflow-legacy.normalize = 23）。超宽不截断
# （截断会毁掉 grep），只是那一行的消息起点右移。
_NAME_WIDTH = 23

# 行格式：来源列用 %(shortname)s（本模块在 format() 里注入）而非 %(name)s。
# 提为模块常量供测试钉住，防止重构时退回不定宽的全限定名。
_LOG_FORMAT = "%(asctime)s %(levelprefix)s %(shortname)s %(message)s"

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


# 访问日志需要掩码的疑似密钥查询参数：令牌/密钥经 URL 查询参数传递时
# （如 weflow-legacy SSE 的 ?access_token=）会出现在 uvicorn access log 请求行中。
# 按参数键名判定：键名按 [_-] 分段，任一分段命中敏感词即掩码——覆盖
# auth_token / session_token / secret_key / access_key / api_secret /
# refresh_token / signature 等常见变体（审查回归），同时不误伤
# keyword / tokenizer 等整词含敏感词子串的普通参数。
_QUERY_SECRET_PARAM_RE = re.compile(r"([?&])([a-z0-9_-]+)=([^&\s]*)", re.IGNORECASE)
_SECRET_KEY_SEGMENTS = frozenset(
    {"token", "key", "secret", "auth", "sig", "sign", "signature",
     "password", "passwd", "pwd", "credential"}
)
_SECRET_KEY_NAMES = frozenset({"apikey", "apikeytoken", "authorization"})


def _is_secret_param(name: str) -> bool:
    lowered = name.lower()
    if lowered in _SECRET_KEY_NAMES:
        return True
    # CamelCase 连写无分隔符可分段（AccessToken/SecretKey），按去分隔符后的
    # 整名后缀判定；tokenizer（以 ize 结尾）/keyword（以 word 结尾）不命中
    compact = re.sub(r"[_-]+", "", lowered)
    if any(compact.endswith(seg) for seg in _SECRET_KEY_SEGMENTS):
        return True
    segments = set(re.split(r"[_-]+", lowered))
    return bool(segments & _SECRET_KEY_SEGMENTS)


def redact_query_string(path: str) -> str:
    """掩码 URL 查询字符串中的疑似密钥参数值（?access_token=xxx → ?access_token=***）。

    按参数键名判定而非按值匹配：无需知道密钥具体内容，命中即掩码，
    因此对任意长度/形态的令牌都生效，且不会误伤正常查询参数。
    """
    return _QUERY_SECRET_PARAM_RE.sub(
        lambda m: (
            f"{m.group(1)}{m.group(2)}=***"
            if _is_secret_param(m.group(2))
            else m.group(0)
        ),
        path,
    )


def short_logger_name(name: str) -> str:
    """logger 真实名 → 显示用短名（不含补齐）。

    仅处理本项目的 logger（`briefdesk.` 开头）：剥掉 `briefdesk.`/`plugins.`
    公共前缀并按 `_NAME_ALIASES` 归一首段。第三方 logger（uvicorn/httpx/PIL
    等）原样返回——它们的名字本就是用户识别来源的依据。
    """
    if not name.startswith(_NAME_STRIP_PREFIXES[0]):
        return name
    for prefix in _NAME_STRIP_PREFIXES:
        name = name.removeprefix(prefix)
    head, sep, rest = name.partition(".")
    return _NAME_ALIASES.get(head, head) + sep + rest


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

    额外提供 `%(shortname)s`：定宽的显示用 logger 名（见 short_logger_name）。
    """

    def format(self, record: logging.LogRecord) -> str:
        lvl = record.levelname
        color = _COLORS.get(lvl, "")
        record.levelprefix = f"{color}{_LEVEL_PREFIX.get(lvl, lvl + _RESET + ':')}"
        record.shortname = short_logger_name(record.name).ljust(_NAME_WIDTH)
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
                # 请求行重建前先掩码查询参数中的疑似密钥，防止令牌落入访问日志
                full_path = redact_query_string(str(full_path))
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


class _AccessLogGate(logging.Filter):
    """仅在 LOG_LEVEL 为 DEBUG/TRACE 时放行 uvicorn.access 记录。

    访问日志本身是 INFO 级记录（uvicorn 硬编码 `access_logger.info(...)`），
    而本项目是单用户本机服务：Host 白名单只放 localhost/127.0.0.1，请求行里
    client_addr 恒为本机、状态码绝大多数是 200，逐请求刷屏会把同步/去重/分类
    等真正的业务行冲散。故默认静默，DEBUG 才放出——排查「前端到底发了什么请求」
    时按需开启。

    级别现取（而非构造时固化）：filter 每条记录都问一次 `access_log_enabled()`，
    因此与 setup_logging / uvicorn.Config 的调用先后无关。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return access_log_enabled()


def setup_logging(level: int | None = None) -> None:
    """配置根 logger，所有子 logger 自动继承此格式。

    level 为 None 时读取 config.log_level（.env 的 LOG_LEVEL，默认 INFO）。
    """
    root = logging.getLogger()
    if level is None:
        level = configured_level()
    root.setLevel(level)

    # 避免重复添加 handler
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            _BriefFormatter(_LOG_FORMAT, datefmt="%H:%M:%S")
        )
        root.addHandler(handler)

    # uvicorn/FastAPI 日志统一走根 handler（时间戳 + 模块名 + 彩色级别）。
    # 配合 uvicorn.Config(log_config=None) 使用：启动阶段不再 dictConfig 覆盖。
    # 级别门由 uvicorn 按 log_level 设置（见 uvicorn_log_level），根级别兜底。
    for name in _UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    # 访问日志闸门（DEBUG 才放行，见 _AccessLogGate）。
    #
    # 与 main.py 传给 uvicorn.Config 的 access_log=access_log_enabled() 是两道
    # 独立防线，都要留：
    #   * Config 那道更省——access_log=False 时 uvicorn 清空 uvicorn.access 的
    #     handler 并置 propagate=False，协议层 `hasHandlers()` 取假，连
    #     LogRecord 都不构造，逐请求零开销；
    #   * 但上面这个循环无条件把 propagate 重置为 True，任何在 Config 之后再次
    #     调用 setup_logging 的路径（测试、未来的重配）都会把 uvicorn 关掉的
    #     访问日志复活；直接 `uvicorn` CLI 起服务时也绕过 Config 那道。
    #     filter 挂在 logger 上（不随 handlers.clear() 掉），兜住这些路径。
    access_logger = logging.getLogger(_ACCESS_LOGGER)
    if not any(isinstance(f, _AccessLogGate) for f in access_logger.filters):
        access_logger.addFilter(_AccessLogGate())

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
    # （briefdesk/plugins/ocr/engine.py 已把该异常视为"未识别到文字"返回
    # 空串，不向调用方抛错；此处仅压制 rapidocr 自身 logger 的 WARNING 噪音）。
    # 其 logger 自带
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


def configured_level() -> int:
    """config.log_level（LOG_LEVEL）→ 数值级别；非法值回退 INFO。

    额外认得 uvicorn 的 TRACE（=5）——标准 logging 无此级别，而
    `uvicorn_log_level()` 会把它透给 uvicorn，两处必须同解。
    """
    raw = config.log_level.upper()
    if raw == "TRACE":
        return _TRACE_LEVEL
    level = getattr(logging, raw, logging.INFO)
    return level if isinstance(level, int) else logging.INFO


def access_log_enabled() -> bool:
    """uvicorn/FastAPI 的请求（access）日志是否输出：仅 DEBUG 及更细级别。

    同时供 `_AccessLogGate` 与 main.py 的 `uvicorn.Config(access_log=...)` 取用，
    确保两道防线判据同源。
    """
    return configured_level() <= logging.DEBUG


def uvicorn_log_level() -> str:
    """把 config.log_level 映射为 uvicorn 的 log_level（uvicorn 自身 logger 的级别门）。

    与 setup_logging 保持同一来源；非法值回退 "info"。
    """
    level = config.log_level.lower()
    if level not in ("critical", "error", "warning", "info", "debug", "trace"):
        return "info"
    return level
