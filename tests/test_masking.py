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

    # ── 分隔符形态手机号 / 一代身份证 / 全角数字（P2 脱敏绕过修复）──

    def test_phone_with_dashes(self):
        self.assertEqual(mask_content("联系138-0013-8000谢谢"), "联系[PHONE]谢谢")

    def test_phone_with_spaces(self):
        self.assertEqual(mask_content("138 0013 8000 找我"), "[PHONE] 找我")

    def test_fullwidth_digits_phone(self):
        out = mask_content("电话１３８００１３８０００")
        self.assertIn(PHONE_PLACEHOLDER, out)
        self.assertNotIn("１３８００１３８０００", out)

    def test_legacy_15_digit_id_card(self):
        out = mask_content("一代证110105491231002号")
        self.assertIn(ID_PLACEHOLDER, out)
        self.assertNotIn("110105491231002", out)

    def test_bankcard_with_spaces(self):
        out = mask_content("卡号 6222 0202 0000 0000 000 收款")
        self.assertIn(BANKCARD_PLACEHOLDER, out)
        self.assertNotIn("6222", out)

    def test_dates_and_room_numbers_untouched(self):
        # 分隔符容错不得误伤短数字串：日期、房间号等去分隔符后长度不足 11 位
        text = "2024-01-15 前到 301-302 室"
        self.assertEqual(mask_content(text), text)

    def test_separator_form_idempotent(self):
        once = mask_content("138-0013-8000")
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




class SeparatorRunEdgeTest(unittest.TestCase):
    """二次扫描边界（审查 A1）：日期与分隔符手机号同段时空格/连字符混排，
    旧实现整段放弃导致段内真手机号漏脱敏；现按空白切分逐段独立分类。"""

    def test_date_and_separated_phone_same_run(self):
        raw = "截止2024-01-15报名138-0013-8000"
        out = mask_content(raw)
        self.assertIn("[PHONE]", out)
        self.assertNotIn("138-0013-8000", out)
        self.assertNotIn("13800138000", out.replace("-", "").replace("[PHONE]", ""))
        self.assertIn("2024-01-15", out)  # 日期段不受牵连

    def test_fullwidth_digits_with_separator(self):
        self.assertEqual(mask_content("联系１３８－００１３－８０００"), "联系[PHONE]")

    def test_non_pii_segments_untouched(self):
        raw = "房间301-302，会期2024-01-15到01-20"
        self.assertEqual(mask_content(raw), raw)


if __name__ == "__main__":
    unittest.main()
