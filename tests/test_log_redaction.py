"""日志脱敏守卫：uvicorn access log 的查询参数掩码不得输出令牌明文。

weflow SSE 长连接按上游文档以 ?access_token= 携带令牌（与 Bearer 头同时
携带），该参数会出现在 uvicorn access log 的请求行中；本模块验证
redact_query_string 与 _BriefFormatter 的联动掩码行为。
"""

import logging
import unittest

from briefdesk.logger import _BriefFormatter, redact_query_string


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


if __name__ == "__main__":
    unittest.main()