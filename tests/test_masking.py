"""mask_content / clean_display_name / normalize_subject 单元测试。"""

import unittest

from briefdesk.masking import (
    BANKCARD_PLACEHOLDER,
    EMAIL_PLACEHOLDER,
    ID_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    TOKEN_PLACEHOLDER,
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

    def test_api_key_token(self):
        """【复核 P3】sk- 前缀密钥脱敏（群聊贴 key 的常见形态）。

        样例为虚构值且含下划线（`[A-Za-z0-9_\\-]` 字符类允许），避免命中
        pre-commit 密钥扫描器的纯字母数字连串形态。"""
        text = "key 是 sk-abc123_456789012345678x 不要泄露"
        self.assertIn(TOKEN_PLACEHOLDER, mask_content(text))
        self.assertNotIn("sk-abc", mask_content(text))

    def test_jwt_token(self):
        """【复核 P3】三段式 JWT 整体脱敏：段内可能含 16-19 位数字（时间戳
        形态 payload），须先于数字类规则整体命中。"""
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"
        self.assertIn(TOKEN_PLACEHOLDER, mask_content(f"token: {jwt}"))
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", mask_content(f"token: {jwt}"))

    def test_unix_timestamp_and_short_id_not_masked(self):
        """10 位 Unix 时间戳 / 9-10 位 QQ 号不脱敏：形态完全冲突，误伤面
        大于收益（设计决策，防回归）。"""
        self.assertIn("1700000000", mask_content("时间戳 1700000000 保留"))
        self.assertIn("1234567890", mask_content("QQ 1234567890 找我"))

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

    def test_fullwidth_mixed_with_space_segment(self):
        # 全角连字符 + 全角空格混排：空白切段后各段独立判定
        out = mask_content("１３８－００１３－８０００　找我")
        self.assertIn("[PHONE]", out)


class SeparatorAggregationGuardTest(unittest.TestCase):
    """段数守卫（审查回归）：数字列表/日期范围按总位数撞型的聚合误伤。
    旧实现按「去分隔符总位数」分类，8 段 16 位被判银行卡、12 段 15 位被判
    身份证且永久入库（types.py 构造即脱敏、原文不落盘，损坏不可逆）；
    现要求切段数 ≤ 5（真实 PII 分组至多 5 段）。"""

    def test_digit_list_not_bankcard(self):
        raw = "12 13 14 15 16 17 18 19"
        self.assertEqual(mask_content(raw), raw)

    def test_long_digit_list_not_id(self):
        raw = "1 2 3 4 5 6 7 8 9 10 11 12"
        self.assertEqual(mask_content(raw), raw)

    def test_room_number_chain_not_bankcard(self):
        raw = "会议室 301-302-303-304-305-306"
        self.assertEqual(mask_content(raw), raw)

    def test_date_range_not_bankcard(self):
        raw = "2024-01-15 - 2024-01-20"
        self.assertEqual(mask_content(raw), raw)

    def test_legit_grouped_forms_still_masked(self):
        self.assertEqual(mask_content("138 0013 8000 找我"), "[PHONE] 找我")
        self.assertEqual(
            mask_content("卡号 6222 0202 0000 0000 000 收款"),
            "卡号 [BANKCARD] 收款",
        )


class MixedSeparatorTest(unittest.TestCase):
    """混合分隔符路径（审查回归）：旧实现 join 丢空白分隔符篡改文本且非幂等，
    「138 0013-8000」首遍输出「1380013-8000」（手机号数字仍可见）、二遍才变占位符；
    现空白分隔符原样回填、整段「86+11 位」国家码形态可整体判定。"""

    def test_mixed_phone_masked_whole(self):
        self.assertEqual(mask_content("138 0013-8000"), "[PHONE]")

    def test_date_and_mixed_phone_separators_preserved(self):
        out = mask_content("2024-01-15 138-0013-8000")
        self.assertEqual(out, "2024-01-15 [PHONE]")

    def test_mixed_idempotent(self):
        for raw in ("138 0013-8000", "2024-01-15 138-0013-8000", "1-2－3"):
            once = mask_content(raw)
            self.assertEqual(mask_content(once), once)


class CountryCodePrefixTest(unittest.TestCase):
    """+86/86 国家码前缀（审查回归）：旧实现 13 位串任何起点都因邻接数字
    断言失败而原样保留（PII 漏报）。"""

    def test_contiguous_plus86(self):
        self.assertEqual(mask_content("+8613800138000"), "[PHONE]")

    def test_separated_plus86(self):
        self.assertEqual(mask_content("电话+86 138 0013 8000"), "电话[PHONE]")

    def test_bare_86_prefix(self):
        self.assertEqual(mask_content("8613800138000"), "[PHONE]")

    def test_arithmetic_untouched(self):
        for raw in ("价格 + 23 分", "100 + 86 = 186", "12+34"):
            self.assertEqual(mask_content(raw), raw)

    def test_non_phone_long_numbers_untouched(self):
        # 86 打头但位数不构成任何 PII 形态：不脱敏
        self.assertEqual(mask_content("861380013800"), "861380013800")


class FullwidthEmailTest(unittest.TestCase):
    def test_fullwidth_at_masked(self):
        self.assertEqual(mask_content("邮箱 foo＠qq.com 收件"), "邮箱 [EMAIL] 收件")


if __name__ == "__main__":
    unittest.main()
