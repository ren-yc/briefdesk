"""日志格式守卫：查询参数掩码，以及「来源」列的短名归一。

前者——weflow-legacy SSE 长连接按上游文档以 ?access_token= 携带令牌（与 Bearer
头同时携带），该参数会出现在 uvicorn access log 的请求行中；验证
redact_query_string 与 _BriefFormatter 的联动掩码行为。

后者——日志行首的「来源」列由 short_logger_name 从真实 logger 名派生（剥公共
前缀、归一别名、定宽补齐）。这里钉住三件容易在重构中被破坏的事：真实 logger
名不参与改写（大量 assertLogs 依赖它）、weflow_legacy 显示为连字符形态、
_NAME_WIDTH 足够容纳全项目最长短名。
"""

import logging
import pathlib
import unittest

import briefdesk
from briefdesk import logger as logger_mod
from briefdesk.logger import (
    _NAME_WIDTH,
    _BriefFormatter,
    redact_query_string,
    short_logger_name,
)


class RedactQueryStringTest(unittest.TestCase):
    def test_masks_secret_params(self) -> None:
        out = redact_query_string("/api/v1/push/messages?access_token=sk-live-secret&x=1")
        self.assertNotIn("sk-live-secret", out)
        self.assertIn("access_token=***", out)

    def test_masks_common_secret_key_names(self) -> None:
        path = "/api/x?a=1"
        for name in ("token", "api_key", "apikey", "key", "secret", "auth"):
            out = redact_query_string(f"{path}&{name}=super-secret-value")
            self.assertNotIn("super-secret-value", out, msg=f"param {name}")
            self.assertIn(f"{name}=***", out)

    def test_keeps_innocent_params(self) -> None:
        path = "/api/items?talker=wxid_123&format=chatlab&limit=20&offset=0"
        self.assertEqual(redact_query_string(path), path)


class AccessLogFormatterTest(unittest.TestCase):
    def _format_access(self, path: str) -> str:
        formatter = _BriefFormatter("%(message)s")
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1", "GET", path, "1.1", 200),
            exc_info=None,
        )
        return formatter.format(record)

    def test_access_log_masks_query_token(self) -> None:
        out = self._format_access("/api/v1/push/messages?access_token=sk-live-secret")
        self.assertNotIn("sk-live-secret", out)
        self.assertIn("access_token=***", out)
        self.assertIn('"GET /api/v1/push/messages?access_token=*** HTTP/1.1"', out)

    def test_access_log_keeps_normal_path_intact(self) -> None:
        out = self._format_access("/api/items?talker=wxid_123&limit=20")
        self.assertIn("/api/items?talker=wxid_123&limit=20", out)


    def test_masks_secret_params_case_insensitive(self) -> None:
        # 大小写变体键名同样必须掩码（审计 #11：原实现仅匹配小写）
        out = redact_query_string("/api/x?Token=tok-1&APIKey=key-2&AccessToken=acc-3")
        for secret in ("tok-1", "key-2", "acc-3"):
            self.assertNotIn(secret, out)


class ShortLoggerNameTest(unittest.TestCase):
    def test_strips_common_prefixes(self) -> None:
        self.assertEqual(
            short_logger_name("briefdesk.plugins.weflow.client"), "weflow.client"
        )
        self.assertEqual(short_logger_name("briefdesk.poll_cycle"), "poll_cycle")

    def test_aliases_underscore_source_to_hyphen(self) -> None:
        # 模块名用下划线、源名用连字符；显示统一到源名形态，否则按源名
        # grep 日志会漏掉一半（见 logger._NAME_ALIASES 处注释）
        self.assertEqual(
            short_logger_name("briefdesk.plugins.weflow_legacy.normalize"),
            "weflow-legacy.normalize",
        )

    def test_third_party_names_untouched(self) -> None:
        # uvicorn/httpx 的名字本就是识别来源的依据，不剥不改
        for name in ("uvicorn.access", "httpx", "PIL.Image"):
            self.assertEqual(short_logger_name(name), name)

    def test_alias_only_applies_to_first_segment(self) -> None:
        # 别名归一的是「源」这一段；同名的下游段不应被连带改写
        self.assertEqual(
            short_logger_name("briefdesk.plugins.weflow.weflow_legacy"),
            "weflow.weflow_legacy",
        )

    def test_name_width_fits_longest_project_name(self) -> None:
        # 列宽小于最长短名不会截断（截断毁 grep），但会让该行消息起点右移、
        # 破坏对齐。这里从**真实包树**枚举而非写死清单——写死的话新增一个更长
        # 的模块（如 plugins/xxx/yyy_zzz.py）不会让任何测试失败，对齐就在无人
        # 察觉中坏掉。
        #
        # 只统计出现了 `getLogger(__name__)` 的模块：来源列的取值域正是这些
        # 模块名，没建 logger 的模块（如纯路由聚合）其名字永远不会进入该列，
        # 把它们算进来只会逼列宽为不存在的行加宽。第三方 logger 名不以
        # `briefdesk.` 开头，short_logger_name 原样返回，不受列宽约束。
        pkg_root = pathlib.Path(briefdesk.__file__).parent
        logger_names = [
            "briefdesk." + ".".join(py.relative_to(pkg_root).with_suffix("").parts)
            for py in pkg_root.rglob("*.py")
            if "getLogger(__name__)" in py.read_text(encoding="utf-8")
        ]
        self.assertGreater(len(logger_names), 30, "包树枚举失败，断言会失去意义")
        over = {
            n: short_logger_name(n)
            for n in logger_names
            if len(short_logger_name(n)) > _NAME_WIDTH
        }
        self.assertEqual(over, {}, f"短名超出 _NAME_WIDTH={_NAME_WIDTH}，需调宽列宽")

    def test_formatter_emits_padded_short_name(self) -> None:
        record = logging.LogRecord(
            name="briefdesk.plugins.weflow.client",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="就绪",
            args=(),
            exc_info=None,
        )
        out = _BriefFormatter("%(shortname)s|%(message)s").format(record)
        self.assertEqual(out, "weflow.client".ljust(_NAME_WIDTH) + "|就绪")
        # 真实 logger 名不被改写：assertLogs 与 briefdesk.* 层级设级别都依赖它
        self.assertEqual(record.name, "briefdesk.plugins.weflow.client")

    def test_configured_format_uses_short_name(self) -> None:
        # 防回归：格式串若退回 %(name)s，来源列会重新变成不定宽的全限定名
        self.assertIn("%(shortname)s", logger_mod._LOG_FORMAT)
        self.assertNotIn("%(name)s", logger_mod._LOG_FORMAT)


if __name__ == "__main__":
    unittest.main()