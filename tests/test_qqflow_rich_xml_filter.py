"""qqflow 富媒体 XML 残片入口过滤测试。

覆盖 REST 回填与 SSE 实时两条预过滤链对含 m_fileName/m_resid 属性对的
QQ 图片/文件卡片 XML 残片的过滤，以及图片消息/正常文本不受影响的反向用例。
"""

import unittest

from briefdesk.plugins.qqflow.normalize import pre_filter_rest, pre_filter_sse

# 实测形态：上游解析失败后以原始 XML 尾部进入 content
# （`]" m_fileName="<uuid>" m_resid="<base64>" tSum="n" flag="3"><item ...>`），
# 值已换用虚构数据
_RICH_XML_FRAGMENT = (
    ']" m_fileName="11111111-2222-3333-4444-555555555555" '
    'm_resid="AAAAbbbbCCCCddddEEEEffff" tSum="3" flag="3">'
    '<item layout="1"> <title color="#000000" size="34">'
)


class RestRichXmlFilterTest(unittest.TestCase):
    def _msg(self, content: str, local_type: int = 0) -> dict:
        return {
            "localId": 1,
            "localType": local_type,
            "createTime": 123,
            "senderUsername": "u_a",
            "content": content,
        }

    def test_filters_rich_xml_fragment(self):
        self.assertFalse(pre_filter_rest(self._msg(_RICH_XML_FRAGMENT)))

    def test_filters_fragment_with_other_local_type(self):
        # localType=1（"其他"）的文本规则同样拦截残片
        self.assertFalse(
            pre_filter_rest(self._msg(_RICH_XML_FRAGMENT, local_type=1))
        )

    def test_keeps_image_message_with_media_id(self):
        msg = self._msg("[image]", local_type=3)
        msg["mediaId"] = "abc123"
        self.assertTrue(pre_filter_rest(msg))

    def test_keeps_text_mentioning_file_name_only(self):
        # 仅含 m_fileName 字样、缺 m_resid 属性对 → 不命中签名，放行
        self.assertTrue(pre_filter_rest(self._msg('m_fileName="abc" 是文件名')))

    def test_keeps_plain_text(self):
        self.assertTrue(pre_filter_rest(self._msg("hello world")))


class SseRichXmlFilterTest(unittest.TestCase):
    def _event(self, content: str) -> dict:
        return {
            "event": "message.new",
            "rawid": "1",
            "sessionId": "10001",
            "sessionType": "group",
            "sourceName": "张三",
            "content": content,
            "timestamp": 123,
        }

    def test_filters_rich_xml_fragment(self):
        self.assertFalse(pre_filter_sse(self._event(_RICH_XML_FRAGMENT)))

    def test_keeps_image_placeholder_with_local_path(self):
        event = self._event("[image]")
        event["media"] = {"localPath": "C:\\tmp\\a.png", "md5": "abc123"}
        self.assertTrue(pre_filter_sse(event))

    def test_keeps_plain_text(self):
        self.assertTrue(pre_filter_sse(self._event("hello world")))


if __name__ == "__main__":
    unittest.main()
