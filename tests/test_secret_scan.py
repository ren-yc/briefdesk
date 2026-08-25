"""预提交密钥扫描器测试（scripts/secret_scan.py 的纯函数部分）。

- 只扫描新增行（+ 前缀）：删除行与上下文行不命中
- 密钥形态规则：sk-/AKIA/PEM 私钥块/本项目密钥环境变量非空赋值
- 防误报：空值环境变量（模板）、普通代码不命中
"""

import unittest

from scripts.secret_scan import scan_text


class SecretScanTest(unittest.TestCase):
    def test_sk_pattern_hits_added_line_only(self) -> None:
        # 运行期拼接避免字面量命中扫描规则（预提交钩子会扫描本文件自身的新增行）
        secret = "sk-" + "abcdef0123456789abcdef0123456789"
        diff = (
            "-" + secret + "\n"  # 删除行不算风险
            "+" + secret + "\n"  # 新增行必须命中
            "  context line sk-ok-not-scanned"
        )
        hits = scan_text(diff)
        self.assertEqual(len(hits), 1)
        line, label, snippet = hits[0]
        self.assertEqual(line, 2)
        self.assertIn("OpenAI", label)
        self.assertIn("sk-", snippet)

    def test_aws_and_pem_hits(self) -> None:
        # 拆分 token 字面量，避免预提交钩子扫描本文件自身的新增行时误报
        diff = (
            "+" + "AKIA" + "IOSFODNN7EXAMPLE" + "\n"
            "+" + "-----BEGIN RSA " + "PRIVATE KEY-----" + "\n"
        )
        labels = [label for _, label, _ in scan_text(diff)]
        self.assertIn("AWS 访问密钥", labels)
        self.assertIn("PEM 私钥块", labels)

    def test_secret_env_assignment_nonempty_hits(self) -> None:
        diff = "+" + "AI_API_KEY=" + "sk-real-looking-value" + "\n"
        hits = scan_text(diff)
        self.assertEqual(len(hits), 1)
        self.assertIn("环境变量", hits[0][1])

    def test_empty_env_assignment_is_template_not_hit(self) -> None:
        diff = "+AI_API_KEY=\n+WEFLOW_API_TOKEN=\n"
        self.assertEqual(scan_text(diff), [])

    def test_plain_code_not_hit(self) -> None:
        diff = (
            "+def test_foo() -> None:\n"
            '+    out.write_text("AI_API_KEY=dotenv-value\\n")  # 字符串内、非行首\n'
            "+    return 42"
        )
        self.assertEqual(scan_text(diff), [])

    def test_diff_header_lines_ignored(self) -> None:
        diff = "+++ b/.env.example\n+AI_API_KEY=\n+---\n"
        self.assertEqual(scan_text(diff), [])


if __name__ == "__main__":
    unittest.main()