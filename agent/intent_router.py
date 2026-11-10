"""意图识别与实体抽取（规则优先，LLM 仅作兜底）。

设计原则
--------
* **规则优先**：用正则 + 关键词覆盖《报税Agent开发规范.md》2.1 节的 8 类意图，
  以及算税细分（增值税 / 企业所得税 / 研发加计 / 印花税）与记账细分（收入 / 费用）；
* 金额支持「万 / 千 / 元 / 块」等中文单位，以及「两万」「一万五千」「二十万」等中文数字；
* 日期支持「今天 / 昨天 / 前天 / 明天」，期间支持「本月 / 这个季度 / 上季度 / 2026Q1 /
  2026年第一季度」；
* 规则无法判断（或置信度低）且已配置 DeepSeek 时，才调用 LLM 兜底，失败自动回退规则结果。

对外接口见 ``docs/CONTRACTS.md`` 4.4：:func:`detect_intent`、:func:`extract_entities`、:func:`route`。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import config
import engine
from engine.money import round2

# =============================================================================
# 意图定义
# =============================================================================
INTENTS: dict[str, str] = {
    "calc_vat": "增值税及附加计算",
    "calc_corp_tax": "企业所得税年度测算",
    "calc_cit_prepay": "企业所得税季度预缴",
    "calc_rd": "研发费用归集与加计扣除",
    "calc_stamp_tax": "印花税计算",
    "iit": "个人所得税扣缴（工资薪金累计预扣）",
    "income": "收入记账",
    "expense": "费用记账",
    "journal": "会计分录生成",
    "policy": "政策咨询",
    "filing_check": "申报表核对",
    "calendar": "申报日历 / 截止日提醒",
    "red_bill": "红利账单（减免明细）解读",
    "chat": "闲聊或其他",
}

#: 规则表：``(意图, 正则列表, 关键词列表)``；列表顺序即同分时的优先级。
_RULES: list[tuple[str, list[str], list[str]]] = [
    (
        "filing_check",
        [r"申报表", r"填对", r"填错", r"核对", r"比对", r"复核", r"检查.{0,4}(?:表|申报)", r"截图", r"OCR", r"扫一下"],
        ["申报表", "填对", "填错", "核对", "比对", "截图", "申报数据", "附表"],
    ),
    (
        "red_bill",
        [r"红利账单", r"减免(?:明细|税额|额).{0,6}(?:怎么|如何)", r"(?:怎么|如何).{0,4}(?:减|省)(?:了|下来)", r"减免.{0,8}怎么(?:来|算|得)"],
        ["红利账单", "红利", "减免明细", "减免额", "怎么来的", "省了多少"],
    ),
    (
        "calendar",
        [r"申报日历", r"什么时候(?:报|申报|交)", r"要报(?:什么|哪些)", r"报什么", r"有什么(?:要|需要)?报",
         r"截止(?:日|日期|时间)", r"(?:下|这个|本|下个)月.{0,6}报", r"逾期", r"申报期"],
        ["申报日历", "截止", "申报期", "提醒", "要报", "报税"],
    ),
    (
        "journal",
        [r"分录", r"凭证", r"借贷", r"借方", r"贷方", r"怎么做账", r"做(?:一?笔)?账", r"记账凭证"],
        ["分录", "凭证", "借贷", "借方", "贷方", "做账"],
    ),
    (
        "calc_rd",
        [r"研发.{0,8}(?:费用|工资|人员|材料|归集|加计|投入|支出)",
         r"加计扣除", r"研发辅助账", r"RD.{0,4}(?:费用|归集)"],
        ["研发", "加计扣除", "研发费用", "研发人员", "归集"],
    ),
    (
        "calc_stamp_tax",
        [r"印花税", r"合同.{0,6}(?:印花|税)"],
        ["印花税", "合同印花"],
    ),
    (
        "policy",
        [r"税率\s*(?:是|为)?\s*多少", r"征收率", r"政策", r"优惠", r"依据", r"文号", r"公告",
         r"免征(?:吗|么|范围)?", r"可以(?:享受|适用)", r"能不能(?:享受|免)", r"最新.{0,4}(?:政策|规定)",
         r"规定.{0,4}(?:是|怎么)"],
        ["政策", "优惠", "免征", "免税", "依据", "文号", "公告", "小规模纳税人", "最新政策",
         "税率", "征收率", "额度", "比例"],
    ),
    (
        "iit",
        [r"工资.{0,8}(?:个税|所得税|预扣|怎么算|多少税|扣多少)",
         r"(?:个税|个人所得税|工资薪金|薪金).{0,8}(?:怎么算|如何算|多少|计算|预扣|申报)",
         r"个税怎么算", r"专项附加扣除", r"减除费用", r"五险一金",
         r"预扣(?:预缴|率)?", r"累计预扣", r"发(?:放)?工资", r"工资(?:表|单|发放)"],
        ["个税", "个人所得税", "工资", "薪金", "工资薪金", "专项附加扣除", "专项附加",
         "减除费用", "预扣", "预扣率", "累计预扣", "五险一金", "个税怎么算"],
    ),
    (
        "calc_vat",
        [r"怎么算", r"如何算", r"计算.{0,4}税", r"要交多少(?:税)?", r"应缴.{0,4}税", r"算一下(?:税)?",
         r"普票", r"专票", r"开票.{0,6}税", r"增值税.{0,6}(?:怎么|如何|要交|应缴|计算|缴多少)"],
        ["增值税", "普票", "专票", "怎么算", "要交多少", "应缴", "算税", "交多少税"],
    ),
    (
        "calc_corp_tax",
        [r"企业所得税", r"汇算清缴", r"年度.{0,4}(?:所得税|纳税)", r"小型微利"],
        ["企业所得税", "汇算清缴", "所得税", "年度汇算", "小型微利"],
    ),
    (
        "calc_cit_prepay",
        [r"(?:季度|本期).{0,4}预缴", r"企业所得税.{0,6}预缴", r"预缴.{0,4}所得税"],
        ["预缴", "季度预缴"],
    ),
    (
        "income",
        [r"收到", r"收款", r"到账", r"入账", r"进账", r"开了.{0,8}(?:普票|专票|发票)", r"销售(?:收入|额)"],
        ["收到", "收入", "收款", "到账", "入账", "开票", "普票", "专票", "客户", "公司", "销售额"],
    ),
    (
        "expense",
        [r"花了", r"支出", r"报销", r"购买", r"采购", r"付了", r"交了", r"付款", r"工资", r"房租", r"差旅"],
        ["花了", "支出", "费用", "报销", "购买", "采购", "付了", "交了", "工资", "房租", "差旅", "社保", "办公"],
    ),
]


# =============================================================================
# 中文数字与金额
# =============================================================================
_CN_DIGITS: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2, "三": 3, "叁": 3,
    "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7,
    "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNITS: dict[str, int] = {
    "十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000, "万": 10000, "亿": 100000000,
}
_CN_CHARS = "".join(_CN_DIGITS) + "".join(_CN_UNITS)

#: 完整宽字符转半角
_FULLWIDTH = str.maketrans("０１２３４５６７８９．，", "0123456789.,")

_NUM = r"\d[\d,]*(?:\.\d+)?"
_DIGIT_AMOUNT_RE = re.compile(
    rf"(?P<num>{_NUM})\s*(?P<unit>亿|万|千)?\s*(?P<tail>元|块钱|块|人民币|圆)?"
)
_CN_AMOUNT_RE = re.compile(
    rf"(?P<num>[{_CN_CHARS}]{{1,12}})\s*(?P<unit>亿|万|千)?\s*(?P<tail>元|块钱|块|人民币|圆)?"
)
_TAIL_UNITS = {"亿": 100_000_000, "万": 10_000, "千": 1_000}
_NOISE_PREFIXES = (
    "今天", "今日", "昨天", "昨日", "前天", "明天", "后天", "本月", "上月", "下月",
    "本季度", "上季度", "上午", "下午", "刚刚", "收到", "收款", "付款", "来自", "客户是",
    "客户", "供应商是", "供应商", "对方是", "对方", "向", "付给", "支付给", "支付", "给", "了", "的",
)
_ORG_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,24}"
    r"(?:有限责任公司|股份有限公司|有限公司|公司|科技|集团|工作室|事务所|中心|商行|经营部))"
)


def cn_to_number(text: str) -> float | None:
    """中文数字转阿拉伯数字（支持「两万」「一万五千」「二十万」「三百」）。

    无法解析时返回 ``None``。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    total = 0
    section = 0
    number = 0
    for char in raw:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if unit >= 10000:
                section = (section + number) * unit
                total += section
                section = 0
                number = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
        else:
            return None
    value = total + section + number
    return float(value) if value > 0 else None


def _digit_value(match: re.Match[str]) -> float | None:
    """把数字 + 单位匹配转换为元。"""
    raw = match.group("num").replace(",", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    unit = match.group("unit")
    if unit:
        value *= _TAIL_UNITS[unit]
    elif match.group("tail") is None and raw.isdigit() and len(raw) >= 4:
        # 无单位的大额纯数字（如「5000」「20600」），排除年份、栏次、日期等
        start, end = match.span()
        following = match.string[end : end + 2]
        preceding = match.string[max(0, start - 1) : start]
        if following.startswith(("年", "月", "日", "季", "Q", "q", "栏", "行", "号", "项", "%")):
            return None
        if preceding in ("第", "栏", "行"):
            return None
        if 1900 <= value <= 2100:
            return None
    elif match.group("tail") is None and raw.isdigit():
        return None
    if value <= 0 or value > 1_000_000_000:
        return None
    return round(value, 2)


def _cn_value(match: re.Match[str]) -> float | None:
    """把中文数字匹配转换为元；无单位且不像金额的（如「统一」）忽略。"""
    raw = match.group("num")
    tail = match.group("tail")
    unit = match.group("unit")
    if not tail and not unit and not any(ch in raw for ch in ("十", "拾", "百", "佰", "千", "仟", "万", "亿")):
        return None
    if not tail and not unit and len(raw) < 2:
        return None
    value = cn_to_number(raw)
    if value is None:
        return None
    if unit:
        value *= _TAIL_UNITS[unit]
    if value <= 0 or value > 1_000_000_000:
        return None
    return round(value, 2)


def amount_spans(text: str) -> list[dict[str, Any]]:
    """抽取句中金额及其位置：``[{"value", "start", "end", "raw"}]``（按出现顺序）。"""
    normalized = (text or "").translate(_FULLWIDTH)
    spans: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []

    for match in _DIGIT_AMOUNT_RE.finditer(normalized):
        value = _digit_value(match)
        if value is None:
            continue
        start, end = match.span()
        spans.append({"value": value, "start": start, "end": end, "raw": match.group(0)})
        occupied.append((start, end))

    for match in _CN_AMOUNT_RE.finditer(normalized):
        value = _cn_value(match)
        if value is None:
            continue
        start, end = match.span()
        if any(start < occ_end and end > occ_start for occ_start, occ_end in occupied):
            continue
        spans.append({"value": value, "start": start, "end": end, "raw": match.group(0)})

    spans.sort(key=lambda item: item["start"])
    # 相邻重复金额去重（同一位置区间只保留一个）
    deduped: list[dict[str, Any]] = []
    for span in spans:
        if deduped and span["start"] == deduped[-1]["start"] and span["value"] == deduped[-1]["value"]:
            continue
        deduped.append(span)
    return deduped


def extract_amounts(text: str) -> list[float]:
    """抽取句中所有金额（元），按出现顺序去重。"""
    values: list[float] = []
    for span in amount_spans(text):
        if span["value"] not in values:
            values.append(span["value"])
    return values


# =============================================================================
# 期间 / 日期
# =============================================================================
_CN_QUARTER = {"一": 1, "二": 2, "三": 3, "四": 4}
_QUARTER_RE = re.compile(r"(\d{4})\s*[-年]?\s*(?:第)?\s*([1-4一二三四])\s*(?:季度|季|Q|q)")
_YEAR_QUARTER_RE = re.compile(r"(\d{4})\s*[-]?\s*[Qq]\s*([1-4])")
_YEAR_MONTH_RE = re.compile(r"(\d{4})\s*[-年/]\s*(\d{1,2})\s*月?")


def _today() -> date:
    try:
        return config.today()
    except Exception:  # pragma: no cover
        return date.today()


def _shift_period(period: str, months: int = 0) -> str:
    """季度/月份期间平移（months 为月数，季度按 3 个月）。"""
    try:
        start, _ = engine.period_range(period)
    except ValueError:
        return ""
    year = start.year
    month = start.month + months
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    if re.match(r"^\d{4}Q[1-4]$", period.upper()):
        return f"{year}Q{(month - 1) // 3 + 1}"
    return f"{year}-{month:02d}"


def extract_period(text: str) -> str:
    """抽取所属期间（``2026Q1`` / ``2026-01`` / ``2026``），无则返回空串。"""
    raw = (text or "").translate(_FULLWIDTH)
    match = _QUARTER_RE.search(raw)
    if match:
        quarter = _CN_QUARTER.get(match.group(2)) or int(match.group(2))
        return f"{match.group(1)}Q{quarter}"
    match = _YEAR_QUARTER_RE.search(raw)
    if match:
        return f"{match.group(1)}Q{match.group(2)}"
    if re.search(r"(?:本|这个|当前|当|该)\s*季度", raw):
        return engine.current_quarter(_today())
    if re.search(r"(?:上|前)\s*(?:个|一)?\s*季度", raw):
        return engine.prev_quarter(engine.current_quarter(_today()))
    if re.search(r"(?:下)\s*(?:个|一)?\s*季度", raw):
        current = engine.current_quarter(_today())
        return engine.quarter_of(engine.quarter_end(current) + timedelta(days=1))

    match = _YEAR_MONTH_RE.search(raw)
    if match:
        month = int(match.group(2))
        following = raw[match.end() : match.end() + 1]
        # 避免把完整日期（2026-03-01）误当月份期间
        if 1 <= month <= 12 and following not in ("-", "/", ".", "日"):
            return f"{match.group(1)}-{month:02d}"
    current_quarter = engine.current_quarter(_today())
    if re.search(r"(?:本|这个|当前|当|该)\s*月(?!\s*[底末])", raw):
        return engine.current_month(_today())
    if re.search(r"上\s*(?:个|一)?\s*月", raw):
        return _shift_period(engine.current_month(_today()), -1)
    if re.search(r"下\s*(?:个|一)?\s*月", raw):
        return _shift_period(engine.current_month(_today()), 1)

    # 无年份的月份表述（如「11月工资」「3月的个税」）；「3个月」因中间有「个」不会误判
    match = re.search(r"(?<!\d)(1[0-2]|[1-9])\s*月", raw)
    if match:
        return f"{_today().year}-{int(match.group(1)):02d}"

    match = re.search(r"(\d{4})\s*年\s*(?:度|全年|汇算)", raw)
    if match:
        return match.group(1)
    return ""


_DATE_RE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
_MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def extract_date(text: str) -> str:
    """抽取日期（``YYYY-MM-DD``）；未提及具体日期时返回空串。"""
    raw = (text or "").translate(_FULLWIDTH)
    today = _today()
    match = _DATE_RE.search(raw)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            pass
    match = _MONTH_DAY_RE.search(raw)
    if match:
        try:
            return date(today.year, int(match.group(1)), int(match.group(2))).isoformat()
        except ValueError:
            pass
    if re.search(r"今天|今日|当天", raw):
        return today.isoformat()
    if re.search(r"昨天|昨日", raw):
        return (today - timedelta(days=1)).isoformat()
    if re.search(r"前天", raw):
        return (today - timedelta(days=2)).isoformat()
    if re.search(r"明天", raw):
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"后天", raw):
        return (today + timedelta(days=2)).isoformat()
    return ""


#: 「本年已实际申报月份数」常见表述（个税累计预扣法关键入参）
_MONTHS_DECLARED_PATTERNS = (
    r"累计\s*(?:申报|预扣|发放)?\s*(\d{1,2})\s*个?\s*月",
    r"(?:已|已经)\s*(?:申报|预扣|发放|发)\s*(?:了)?\s*(\d{1,2})\s*个?\s*月",
    r"第\s*(\d{1,2})\s*个?\s*月",
    r"前\s*(\d{1,2})\s*个?\s*月",
    r"共\s*(\d{1,2})\s*个?\s*月",
    r"1\s*[-~至到]\s*(\d{1,2})\s*月",
    r"(\d{1,2})\s*个\s*月(?:的)?\s*(?:工资|收入|申报|预扣|累计|数据)",
)


def extract_months_declared(text: str) -> int | None:
    """抽取「本年已实际申报月份数」（0—12，个税累计预扣法用）。

    2026 年扣缴端升级后，5,000 元/月减除费用按**实际申报月份**累计，
    因此「已申报 11 个月」「累计 3 个月」「1—3 月」等表述需要识别为月份数。
    """
    raw = (text or "").translate(_FULLWIDTH)
    for pattern in _MONTHS_DECLARED_PATTERNS:
        match = re.search(pattern, raw)
        if match:
            value = int(match.group(1))
            if 0 <= value <= 12:
                return value
    # 兜底：单独的「N 个月」（如「干了 3 个月」）
    match = re.search(r"(?<!\d)(\d{1,2})\s*个\s*月", raw)
    if match:
        value = int(match.group(1))
        if 0 <= value <= 12:
            return value
    return None


# =============================================================================
# 其他实体
# =============================================================================
_INVOICE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("未开票", r"未开票|没开票|没开发票|未开发票|不开票|无票"),
    ("专票", r"专票|专用发票"),
    ("普票", r"普票|普通发票"),
)

_INCOME_CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("软件开发", r"软件开发|开发软件|小程序|APP|app|系统开发|平台开发|软件定制"),
    ("运维服务", r"运维|维护|技术支持|系统维护"),
    ("技术服务", r"技术服务|技术咨询|技术开发|技术转让|咨询服务|服务费|技术费"),
)

_EXPENSE_CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("工资", r"工资|薪资|薪酬|人员工资"),
    ("社保", r"社保|五险一金|公积金"),
    ("房租", r"房租|租金|租赁费|办公室租"),
    ("差旅", r"差旅|出差|住宿|车费|机票|高铁"),
    ("软件订阅", r"软件订阅|订阅|云服务|服务器|SaaS|saas|软件服务费"),
    ("研发材料", r"研发材料|材料费|元器件|耗材"),
    ("折旧", r"折旧|固定资产折旧"),
    ("办公", r"办公用品|办公费|文具|打印|耗材"),
)

_ALL_CATEGORY_KEYWORDS = _INCOME_CATEGORY_KEYWORDS + _EXPENSE_CATEGORY_KEYWORDS


def _clean_client_name(name: str) -> str:
    """去掉客户名前的动词 / 时间噪声，如「今天收到云图科技」→「云图科技」。"""
    cleaned = (name or "").strip(" ，,。.：:;；")
    changed = True
    while changed and len(cleaned) > 2:
        changed = False
        for prefix in _NOISE_PREFIXES:
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix) + 1:
                cleaned = cleaned[len(prefix) :]
                changed = True
                break
    return cleaned.strip()


def extract_client(text: str) -> str:
    """抽取客户 / 对方单位名称，识别不到返回空串。"""
    for match in _ORG_RE.finditer((text or "").translate(_FULLWIDTH)):
        name = _clean_client_name(match.group(1))
        if 2 <= len(name) <= 30:
            return name
    return ""


def extract_category(text: str) -> str:
    """抽取业务 / 费用类别（优先收入类别，其次费用类别）。"""
    raw = text or ""
    for category, pattern in _ALL_CATEGORY_KEYWORDS:
        if re.search(pattern, raw):
            return category
    return ""


def _invoice_type(text: str) -> str:
    raw = text or ""
    for name, pattern in _INVOICE_PATTERNS:
        if re.search(pattern, raw):
            return name
    return ""


def _keywords(text: str) -> list[str]:
    lexicon = (
        "增值税", "企业所得税", "个人所得税", "印花税", "六税两费", "附加税费", "研发费用",
        "加计扣除", "免税", "免征", "减半", "小规模纳税人", "小型微利企业", "汇算清缴", "预缴",
        "申报", "红利账单", "专票", "普票", "未开票", "分录", "凭证", "台账", "截止日",
        "技术合同", "技术转让", "工资", "社保", "房租", "差旅", "办公", "折旧",
        "个税", "个人所得税", "工资薪金", "专项附加扣除", "减除费用", "预扣", "累计预扣", "五险一金",
    )
    raw = text or ""
    return [word for word in lexicon if word in raw]


def _is_rd_related(text: str) -> bool:
    raw = text or ""
    if "加计扣除" in raw or "研发辅助账" in raw:
        return True
    return "研发" in raw and any(word in raw for word in ("费用", "工资", "人员", "材料", "投入", "支出", "项目", "归集"))


def extract_entities(text: str) -> dict[str, Any]:
    """从用户输入中抽取结构化实体。

    返回
    ----
    ``{"amounts": [float], "amount": float|None, "date": "YYYY-MM-DD", "period": "2026Q1",
       "months_declared": int|None, "client": str, "invoice_type": str, "category": str,
       "is_rd_related": bool, "keywords": [str]}``
    """
    raw = text or ""
    amounts = extract_amounts(raw)
    return {
        "amounts": amounts,
        "amount": amounts[0] if amounts else None,
        "date": extract_date(raw),
        "period": extract_period(raw),
        "months_declared": extract_months_declared(raw),
        "client": extract_client(raw),
        "invoice_type": _invoice_type(raw),
        "category": extract_category(raw),
        "is_rd_related": _is_rd_related(raw),
        "keywords": _keywords(raw),
    }


# =============================================================================
# 意图评分
# =============================================================================
#: 咨询型表述（问政策、问税率、问额度，而不是要求算税）
_CONSULT_RE = re.compile(
    r"税率|征收率|政策|优惠|文件|公告|文号|依据|规定|多少|比例|额度|标准|期限|范围|条件|能不能|可不可以|是否|吗|呢|？|\?"
)
#: 明确的计算意图（要求算出税额）
_CALC_INTENT_RE = re.compile(r"怎么算|如何算|计算|要交多少|交多少|应缴|算一下|缴多少|多少税|算算|帮我算")


def _score_rules(text: str) -> list[tuple[int, str, list[str]]]:
    """返回按分数降序、同分按规则顺序的 ``[(score, intent, hits)]``。

    额外规则（避免把「税率是多少」这类**咨询**误判成算税）：
    当用户只是在问政策 / 税率 / 额度（咨询关键词），且**既没有明确金额**（≥100 元）
    **也没有计算意图**（「怎么算 / 要交多少」等）时，对算税类意图降权，
    让 ``policy`` 优先。
    """
    raw = text or ""
    amounts = extract_amounts(raw)
    has_amount = any(value >= 100 for value in amounts)
    has_calc_intent = bool(_CALC_INTENT_RE.search(raw))
    is_consult = bool(_CONSULT_RE.search(raw)) and not has_calc_intent
    calc_intents = {"calc_vat", "calc_corp_tax", "calc_cit_prepay", "calc_rd", "calc_stamp_tax"}

    scored: list[tuple[int, str, list[str]]] = []
    for intent, patterns, keywords in _RULES:
        hits: list[str] = []
        score = 0
        for pattern in patterns:
            if re.search(pattern, raw):
                score += 2
                hits.append(pattern)
        for keyword in keywords:
            if keyword in raw:
                score += 1
                hits.append(keyword)
        if is_consult and not has_amount and intent in calc_intents:
            score = max(0, score - 3)
        scored.append((score, intent, hits))
    scored.sort(key=lambda item: -item[0])
    return scored


def _confidence(score: int) -> float:
    if score <= 0:
        return 0.2
    return round(min(0.98, 0.52 + 0.1 * score), 2)


def _llm_detect(text: str, client: Any) -> dict[str, Any] | None:
    """调用 LLM 兜底识别意图；失败返回 None（不抛异常）。"""
    try:
        from agent.llm_client import DeepSeekError
        from agent.prompt_templates import INTENT_PROMPT
        from agent.llm_client import parse_json

        response = client.chat(
            [
                {"role": "system", "content": "你是税务意图识别器，只输出 JSON。"},
                {"role": "user", "content": INTENT_PROMPT.format(text=text)},
            ],
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        data = parse_json(response.get("content") or "")
        if not data:
            return None
        intent = str(data.get("intent") or "").strip()
        if intent not in INTENTS:
            return None
        try:
            confidence = float(data.get("confidence") or 0.7)
        except (TypeError, ValueError):
            confidence = 0.7
        entities = data.get("entities") if isinstance(data.get("entities"), dict) else {}
        return {
            "intent": intent,
            "confidence": max(0.0, min(0.99, confidence)),
            "entities": entities,
        }
    except Exception:  # noqa: BLE001 - LLM 兜底失败必须回退规则，不影响主流程
        return None


def detect_intent(text: str, *, client: Any | None = None) -> dict[str, Any]:
    """识别用户意图。

    返回
    ----
    ``{"intent", "confidence", "entities", "method": "rule"|"llm", "raw"}``
    """
    raw = (text or "").strip()
    entities = extract_entities(raw)
    scored = _score_rules(raw)
    best_score, best_intent, _ = scored[0] if scored else (0, "chat", [])
    intent = best_intent if best_score > 0 else "chat"
    method = "rule"
    confidence = _confidence(best_score)

    should_use_llm = client is not None and getattr(client, "configured", False) and (
        best_score == 0 or (best_score <= 1 and len(raw) > 16)
    )
    if should_use_llm:
        result = _llm_detect(raw, client)
        if result:
            intent = result["intent"]
            confidence = result["confidence"]
            method = "llm"
            llm_entities = result.get("entities") or {}
            for key, value in llm_entities.items():
                if value in (None, "", [], {}):
                    continue
                if key == "amounts":
                    cleaned = []
                    for item in value if isinstance(value, list) else [value]:
                        try:
                            cleaned.append(round(float(str(item).replace(",", "")), 2))
                        except (TypeError, ValueError):
                            continue
                    if cleaned:
                        entities["amounts"] = cleaned
                        entities["amount"] = cleaned[0]
                elif not entities.get(key):
                    entities[key] = value

    return {
        "intent": intent,
        "confidence": confidence,
        "entities": entities,
        "method": method,
        "raw": raw,
    }


# =============================================================================
# 路由
# =============================================================================
def _special_amount(text: str, spans: list[dict[str, Any]]) -> float:
    """找出与「专票 / 专用发票」**最近**的金额，作为专票含税金额。"""
    raw = text or ""
    positions = [match.start() for match in re.finditer(r"专票|专用发票", raw)]
    if not positions or not spans:
        return 0.0

    def distance(span: dict[str, Any], position: int) -> int:
        if position < span["start"]:
            return span["start"] - position
        if position > span["end"]:
            return position - span["end"]
        return 0

    chosen: list[float] = []
    for position in positions:
        nearest = min(spans, key=lambda span: distance(span, position))
        if distance(nearest, position) <= 6 and nearest["value"] not in chosen:
            chosen.append(nearest["value"])
    return round(sum(chosen), 2)


def _total_amount(text: str, spans: list[dict[str, Any]]) -> float:
    """推断「季度含税总销售额」。"""
    if not spans:
        return 0.0
    if len(spans) == 1:
        return round(spans[0]["value"], 2)
    raw = text or ""
    if re.search(r"其中|里面有|包含|含.{0,2}专票|专票.{0,4}(?:占|为)", raw):
        return round(max(span["value"] for span in spans), 2)
    return round(sum(span["value"] for span in spans), 2)


def _calendar_days(text: str) -> int:
    raw = text or ""
    match = re.search(r"(\d{1,3})\s*(?:天|日)", raw)
    if match:
        try:
            return max(1, min(365, int(match.group(1))))
        except ValueError:
            pass
    if "下个月" in raw or "下月" in raw:
        return 45
    if "本月" in raw or "这个月" in raw:
        return 30
    if "季度" in raw:
        return 90
    return 60


#: 金额 + 可选单位（万/千/亿）
_AMOUNT_UNIT = r"(\d+(?:\.\d+)?)\s*(亿|万|千)?"


def _amount_near_keyword(text: str, keyword_pattern: str) -> float | None:
    """在关键词前 / 后 8 个字符内抽取一个金额（元），找不到返回 None。"""
    raw = (text or "").translate(_FULLWIDTH)
    for pattern in (
        rf"(?:{keyword_pattern})[^\d]{{0,8}}{_AMOUNT_UNIT}\s*(?:元|块)?",
        rf"{_AMOUNT_UNIT}\s*(?:元|块)?[^\d]{{0,6}}(?:{keyword_pattern})",
    ):
        match = re.search(pattern, raw)
        if not match:
            continue
        try:
            value = float(match.group(1).replace(",", ""))
        except (TypeError, ValueError):
            continue
        unit = match.group(2)
        if unit == "亿":
            value *= 100_000_000
        elif unit == "万":
            value *= 10_000
        elif unit == "千":
            value *= 1_000
        if value > 0:
            return round(value, 2)
    return None


def _iit_arguments(text: str, entities: dict[str, Any]) -> dict[str, Any]:
    """把个税相关表述转换为 ``calc_iit`` 工具参数（累计预扣法口径）。

    规则要点：
    * 收入：优先取「月薪 / 工资 / 收入 / 应发」附近的金额；若为「月薪 × 已申报月份」则换算为累计收入，
      明确写「累计收入」时直接作为累计收入；
    * 月份：``months_declared`` 未提及默认 1（单月），提及「已申报 N 个月 / 累计 N 个月 / 1—N 月」时取 N；
    * 专项扣除 / 专项附加 / 已预扣：关键词附近金额；「每月」表述 × 月份数换算为累计；
    * 一次性 60,000 元扣除：识别「一次性扣除 6 万」等表述。
    """
    raw = text or ""
    amounts: list[float] = entities.get("amounts") or []
    months = entities.get("months_declared")
    args: dict[str, Any] = {"months_declared": int(months) if months is not None else 1}
    declared_months = args["months_declared"]

    cumulative_flag = bool(re.search(r"累计(?:收入|工资|薪金|应发|发放)|全年累计|累计数|收入合计", raw))
    monthly_flag = bool(re.search(r"月薪|每月|月工资|月收入|单月|一个月", raw))

    income = _amount_near_keyword(raw, r"月薪|工资|收入|薪金|应发|工资薪金")
    if income is None and amounts:
        income = amounts[0]
    if income is not None:
        if monthly_flag and not cumulative_flag and declared_months > 1:
            args["cumulative_income"] = round2(income * declared_months)
        else:
            args["cumulative_income"] = round2(income)

    # 扣除项若按「每月」表述，换算为累计金额
    multiplier = declared_months if re.search(r"每月|每个月|月均", raw) else 1
    social = _amount_near_keyword(raw, r"社保|三险|社会保险|五险一金")
    if social:
        args["cumulative_social_insurance"] = round2(social * multiplier)
    fund = _amount_near_keyword(raw, r"公积金|住房公积金")
    if fund:
        args["cumulative_housing_fund"] = round2(fund * multiplier)
    special = _amount_near_keyword(raw, r"专项附加扣除|专项附加")
    if special:
        args["cumulative_special_additional"] = round2(special * multiplier)
    other = _amount_near_keyword(raw, r"其他扣除")
    if other:
        args["cumulative_other_deduction"] = round2(other * multiplier)
    withheld = _amount_near_keyword(raw, r"已预扣|已预缴|已扣缴|已缴个税|已缴税")
    if withheld:
        args["cumulative_tax_withheld"] = round2(withheld)

    if re.search(r"一次性\s*(?:扣除)?\s*(?:6\s*万|60,?000|六万)|一次性减除", raw):
        args["one_time_basic_deduction"] = True

    period = entities.get("period") or ""
    if period:
        args["period"] = period
    return args


def route(text: str) -> dict[str, Any]:
    """把用户输入路由到具体工具，返回 ``{"intent", "tool", "arguments", "reply_hint"}``。

    仅使用规则识别（不调用 LLM），便于无 API Key 时本地确定性回复。
    """
    result = detect_intent(text, client=None)
    intent = result["intent"]
    entities = result["entities"]
    spans = amount_spans(text or "")
    amounts: list[float] = entities.get("amounts") or []
    period = entities.get("period") or ""
    today = _today().isoformat()

    tool: str | None = None
    arguments: dict[str, Any] = {}
    reply_hint = ""

    if intent == "calc_vat":
        tool = "calc_vat"
        if amounts:
            arguments["quarterly_total_inclusive"] = _total_amount(text, spans)
            arguments["special_invoice_inclusive"] = _special_amount(text, spans)
        if period:
            arguments["period"] = period
        elif re.search(r"季度|季", text or ""):
            arguments["period"] = engine.current_quarter(_today())
        reply_hint = "调用 calc_vat 计算增值税及附加税费，解释免税额度、专票部分与适用政策依据。"

    elif intent == "calc_corp_tax":
        tool = "calc_corp_tax"
        if amounts:
            arguments["annual_profit"] = amounts[0]
        if period:
            arguments["period"] = period
        reply_hint = "调用 calc_corp_tax 测算年度企业所得税，说明小型微利企业 5% 实际税负与研发加计扣除。"

    elif intent == "calc_cit_prepay":
        tool = "calc_cit_prepay"
        if amounts:
            arguments["quarter_profit"] = amounts[0]
            arguments["ytd_profit"] = amounts[1] if len(amounts) > 1 else amounts[0]
        if period:
            arguments["period"] = period
        reply_hint = "调用 calc_cit_prepay 计算季度企业所得税预缴额（本年累计口径）。"

    elif intent == "calc_rd":
        tool = "calc_rd_deduction"
        if period:
            arguments["period"] = period
        elif re.search(r"季度|季", text or ""):
            arguments["period"] = engine.current_quarter(_today())
        arguments["formed_intangible"] = "无形资产" in (text or "")
        reply_hint = "调用 calc_rd_deduction 归集研发费用并计算 100% 加计扣除额。"

    elif intent == "calc_stamp_tax":
        tool = "calc_stamp_tax"
        if period:
            arguments["period"] = period
        elif re.search(r"季度|季", text or ""):
            arguments["period"] = engine.current_quarter(_today())
        reply_hint = "调用 calc_stamp_tax 计算合同印花税，说明六税两费减半后的应缴金额。"

    elif intent == "iit":
        tool = "calc_iit"
        arguments.update(_iit_arguments(text or "", entities))
        reply_hint = (
            "调用 calc_iit 按累计预扣法计算本期应预扣个税，解释累计口径、专项附加扣除，"
            "并提醒 2026 年扣缴端按实际申报月份累计、零工资月份也要零申报。"
        )

    elif intent == "income":
        tool = "add_income"
        if amounts:
            arguments["inclusive_amount"] = amounts[0]
        client_name = entities.get("client") or ""
        if client_name:
            arguments["client"] = client_name
        invoice_type = entities.get("invoice_type") or ""
        if invoice_type:
            arguments["invoice_type"] = invoice_type
        category = entities.get("category") or ""
        if category in ("技术服务", "软件开发", "运维服务"):
            arguments["category"] = category
        arguments["date"] = entities.get("date") or today
        reply_hint = "调用 add_income 记一笔收入，说明不含税金额、增值税额与自动生成的分录。"

    elif intent == "expense":
        tool = "add_expense"
        if amounts:
            arguments["amount"] = amounts[0]
        arguments["description"] = (text or "").strip()[:60]
        category = entities.get("category") or ""
        if category:
            arguments["category"] = category
        arguments["is_rd_related"] = bool(entities.get("is_rd_related"))
        arguments["date"] = entities.get("date") or today
        reply_hint = "调用 add_expense 记一笔费用；小规模纳税人进项税不可抵扣，金额即成本费用。"

    elif intent == "policy":
        tool = "search_policy"
        arguments["query"] = (text or "").strip()
        reply_hint = "调用 search_policy 检索政策知识库，回答必须标注文号、生效日期与来源。"

    elif intent == "filing_check":
        tool = "check_filing"
        if period:
            arguments["period"] = period
        elif re.search(r"季度|季", text or ""):
            arguments["period"] = engine.current_quarter(_today())
        reply_hint = "调用 check_filing 核对申报表栏次与台账数据；如为截图请先本地 OCR 再人工确认。"

    elif intent == "calendar":
        tool = "get_filing_calendar"
        arguments["days"] = _calendar_days(text)
        reply_hint = "调用 get_filing_calendar 列出临近申报截止事项，并提醒零申报也需提交。"

    elif intent == "red_bill":
        reply_hint = "解读红利账单（减免明细）：拆分为增值税免税/1%减征、六税两费减半、企业所得税小微优惠等。"

    elif intent == "journal":
        reply_hint = "生成会计分录：借银行存款/应收账款，贷主营业务收入 + 应交税费—应交增值税。"

    else:
        reply_hint = "介绍税小智能力范围，引导用户描述具体税务问题。"

    return {
        "intent": intent,
        "intent_label": INTENTS.get(intent, intent),
        "tool": tool,
        "arguments": arguments,
        "reply_hint": reply_hint,
    }
