"""共享文本净化 — PII 脱敏 + 显示名清洗 + 主体名归一化。

- mask_content：手机号 / 身份证 / 邮箱 / 银行卡替换为占位符
  （含分隔符写法 138-0013-8000 与全角数字；详见 _SEP_RUN_RE 注释）
- clean_display_name：去除显示名中的 C0 控制字符与首尾空白
- normalize_subject：主体名 NFKC + 空白折叠/首尾 + 小写归一（供时间线跨写法聚合）
- PLACEHOLDER_ONLY_RE：纯附件占位符判定（pipeline 入口过滤与 dedup 原文短路共用）

模块化设计：纯函数、只依赖标准库 re/unicodedata，被 types.py（构造即
脱敏/净化）、pipeline.py（OCR 合并/入库）与 db.py（主体时间线查询）调用。
"""

import re
import unicodedata

EMAIL_PLACEHOLDER = "[EMAIL]"
ID_PLACEHOLDER = "[ID]"
BANKCARD_PLACEHOLDER = "[BANKCARD]"
PHONE_PLACEHOLDER = "[PHONE]"

# 单次扫描、命名组区分类型。顺序重要：
#  - email 优先：邮箱内 11 位数字不会被当作手机号单独脱敏
#  - ID 先于银行卡：18 位纯数字按身份证处理（规格歧义的确定性选择）；
#    15 位一代身份证紧随其后（<16 位，不与银行卡区间重叠）
#  - 数字类同时覆盖全角数字（０-９）：全角手机号/证件号/银行卡同样脱敏，
#    邻接断言把全角数字视同数字，全角长串不会被部分命中
#
# 用 (?<![0-9]) / (?![0-9]) 数字邻接断言而非 \b：Python re 的 \w 含中文，
# "电话13800138000联系" 中"话"与数字之间没有词边界，\b\d{11}\b 会漏匹配。
# 邻接断言只关心数字上下文：中文/字母/标点相邻正常匹配，
# 且 19 位数字串中的 11 位子串因前后仍是数字而不会被手机号规则部分命中。
_MASK_RE = re.compile(
    r"(?P<email>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
    r"|(?P<id>(?<![0-9０-９])[0-9０-９]{17}[0-9０-９Xx](?![0-9０-９]))"
    r"|(?P<id15>(?<![0-9０-９])[0-9０-９]{15}(?![0-9０-９]))"
    r"|(?P<bankcard>(?<![0-9０-９])[0-9０-９]{16,19}(?![0-9０-９]))"
    r"|(?P<phone>(?<![0-9０-９])[0-9０-９]{11}(?![0-9０-９]))"
)

_PLACEHOLDER_BY_GROUP = {
    "email": EMAIL_PLACEHOLDER,
    "id": ID_PLACEHOLDER,
    "id15": ID_PLACEHOLDER,
    "bankcard": BANKCARD_PLACEHOLDER,
    "phone": PHONE_PLACEHOLDER,
}


def _replace(match: re.Match[str]) -> str:
    assert match.lastgroup is not None  # 正则保证至少一个命名组命中
    return _PLACEHOLDER_BY_GROUP[match.lastgroup]


# 分隔符容错二次扫描：主正则要求连续数字，命中不了 "138-0013-8000"、
# "6222 0202 0000 0000 000" 这类带分隔符写法。此处捕获「数字+分隔符」候选串
# （分隔符仅半/全角连字符与空格；不含点号/冒号，避免误伤 IP、时间、版本号），
# 去掉分隔符后按长度分类；不构成任何已知 PII 形态则原样保留
# （日期 2024-01-15、房间号 301-302 等去分隔符后不足 11 位，不受影响）。
_SEP_RUN_RE = re.compile(
    r"(?<![0-9０-９])[0-9０-９][0-9０-９\- －　]*[0-9０-９](?![0-9０-９])"
)


def _sep_run_repl(match: re.Match[str]) -> str:
    run = match.group()
    digits: list[str] = []
    seps: set[str] = set()
    for ch in run:
        if ch.isdigit():
            digits.append(ch)
        else:
            seps.add(ch)
    if len(seps) > 1:
        # 混合分隔符（如日期区间被串成长串）按普通文本保留，防误伤
        return run
    count = len(digits)
    if count == 11:
        return PHONE_PLACEHOLDER
    if count == 15:
        return ID_PLACEHOLDER
    if 16 <= count <= 19:
        return BANKCARD_PLACEHOLDER
    return run


def mask_content(text: str | None) -> str:
    """将文本中的手机号/身份证/邮箱/银行卡替换为占位符。

    幂等：占位符不含数字与 @，对已脱敏文本再次调用无副作用（二次扫描亦然——
    占位符内无数字，不会再生成候选串）。
    None 归一只为防御上游 JSON 显式 null（如 REST 图片消息的 content）。
    """
    if not text:
        return ""
    return _SEP_RUN_RE.sub(_sep_run_repl, _MASK_RE.sub(_replace, text))


# 显示名清洗：上游档案昵称可能携带 C0 控制字符前缀（\x01\x01…）或纯空白，
# 净化后为空返回 ""（调用方回退到 uid / 会话 id / "未知" 等兜底值）
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def clean_display_name(raw: str | None) -> str:
    """净化显示名：去控制字符 + 首尾空白；净化后为空返回 ""（调用方回退）。

    幂等：对已净化文本再次调用无副作用（构造时统一净化 + 源内显式净化并存）。
    """
    if not raw:
        return ""
    return _CONTROL_RE.sub("", raw).strip()


# 主体名归一化：供主体时间线跨写法聚合。
#  - NFKC：全角字母/数字 → 半角（ＡＣＭ→ACM）；全角空格 → 半角空格
#    （摄　影社→摄 影社，单个空格保留）
#  - 连续空白折叠为单个空格并去首尾
#  - 拉丁字母小写（ACM→acm）——中文无影响
# 不做后缀剥离（"摄影社招新" 不归并到 "摄影社"）。
_SPACE_RE = re.compile(r"\s+")


def normalize_subject(name: str | None) -> str:
    """NFKC + 空白折叠/trip + 小写的主体名归一化；空输入返回 ""。

    写入（pipeline 入库）与查询（db.get_items_by_subject/get_subject_count）
    共用同一规则，保证双向一致。
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name)
    s = _SPACE_RE.sub(" ", s).strip()
    return s.lower()


# ── 纯附件占位符判定（单源）──
# 整条内容仅由方括号片段构成：[图片]/[image]/[语音]/[视频]… 及多片段拼接
# "[图片][图片]"（重复形），与源侧 normalize 的占位符判定语义一致。
# 消费方：pipeline 入口过滤（OCR 未启用时屏蔽纯占位符带图消息，不标
# processed、可经回填窗口恢复）；dedup 原文哈希短路的排除项（占位符原文
# 可对应不同图，防 qqflow 同文异图误判 SAME）。
PLACEHOLDER_ONLY_RE = re.compile(r"^(?:\s*\[[^\]]+\])+\s*$")
