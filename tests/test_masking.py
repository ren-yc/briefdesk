"""mask_content / clean_display_name / normalize_subject 单元测试。"""

import unittest

from briefdesk.masking import (
    BANKCARD_PLACEHOLDER,
    EMAIL_PLACEHOLDER,
    ID_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    clean_display_name,
    mask_content,
    normalize_subject,
)


class MaskContentTest(unittest.TestCase):
    def test_phone(self):
        self.assertIn(PHONE_PLACEHOLDER, mask_content("电话13800138000联系"))
        self.assertNotIn("13800138000", mask_content("电话13800138000联系"))

    def test_email(self):
        self.assertIn(EMAIL_PLACEHOLDER, mask_content("邮箱 abc@example.com 联系"))
        self.assertNotIn("abc@example.com", mask_content("邮箱 abc@example.com 联系"))

    def test_id_card(self):
        self.assertIn(ID_PLACEHOLDER, mask_content("身份证11010519491231002X有效"))
        self.assertNotIn(
            "11010519491231002X", mask_content("身份证11010519491231002X有效")
        )

    def test_bankcard(self):
        self.assertIn(BANKCARD_PLACEHOLDER, mask_content("卡号6222021234567890123"))
        self.assertNotIn("6222021234567890123", mask_content("卡号6222021234567890123"))

    def test_none_and_empty(self):
        self.assertEqual(mask_content(None), "")
        self.assertEqual(mask_content(""), "")

    def test_idempotent(self):
        once = mask_content("电话13800138000 邮箱 a@b.com")
        self.assertEqual(mask_content(once), once)


class CleanDisplayNameTest(unittest.TestCase):
    def test_control_chars_removed(self):
        self.assertEqual(clean_display_name("\x01\x01Alice\x7f"), "Alice")

    def test_blank_fallback(self):
        self.assertEqual(clean_display_name("  \t "), "")

    def test_none(self):
        self.assertEqual(clean_display_name(None), "")


class NormalizeSubjectTest(unittest.TestCase):
    def test_nfkc_fullwidth_to_halfwidth(self):
        self.assertEqual(normalize_subject("ＡＣＭ"), "acm")
        self.assertEqual(normalize_subject("ＡＣＭ社"), "acm社")

    def test_whitespace_collapsed_and_trimmed(self):
        # 首尾空白（含全角空格转半角后）被去首尾
        self.assertEqual(normalize_subject("　摄影社　"), "摄影社")
        # 连续多个空格折叠为单个
        self.assertEqual(normalize_subject("摄  影社"), "摄 影社")
        # 单个全角空格 → NFKC 转半角空格：仍保留单个空格（不删除）
        self.assertEqual(normalize_subject("摄　影社"), "摄 影社")

    def test_lowercase(self):
        self.assertEqual(normalize_subject("ACM 社"), "acm 社")
        self.assertEqual(normalize_subject("ACM社"), "acm社")

    def test_no_suffix_stripping(self):
        self.assertEqual(normalize_subject("摄影社招新"), "摄影社招新")

    def test_empty_and_none(self):
        self.assertEqual(normalize_subject(""), "")
        self.assertEqual(normalize_subject(None), "")

    def test_idempotent(self):
        once = normalize_subject(" ＡＣＭ 社  ")
        self.assertEqual(normalize_subject(once), once)


if __name__ == "__main__":
    unittest.main()
