"""申报日历与提醒（确定性代码）。

支持：季度/月度期间换算、各项申报截止日计算（周末顺延）、即将到期事项提醒、
季度末结转与零申报提示。法定节假日顺延以税务机关公告为准，本模块仅做周末顺延。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

import config

DISCLAIMER = config.DISCLAIMER

DATE_FMT = "%Y-%m-%d"


# =============================================================================
# 期间换算
# =============================================================================
def quarter_of(value: str | date | None = None) -> str:
    """返回某日期所属季度，如 ``2026Q1``。"""
    day = _as_date(value)
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def quarter_start(period: str) -> date:
    """季度起始日。"""
    match = re.match(r"^(\d{4})Q([1-4])$", str(period).strip().upper())
    if not match:
        raise ValueError(f"季度格式不正确：{period}，应形如 2026Q1")
    year, quarter = int(match.group(1)), int(match.group(2))
    return date(year, (quarter - 1) * 3 + 1, 1)


def quarter_end(period: str) -> date:
    """季度结束日。"""
    start = quarter_start(period)
    month = start.month + 2
    return date(start.year, month, calendar.monthrange(start.year, month)[1])


def quarter_range(period: str) -> tuple[date, date]:
    return quarter_start(period), quarter_end(period)


def period_range(period: str) -> tuple[date, date]:
    """通用期间范围：支持 ``2026Q1``、``2026-03``、``2026``。"""
    text = str(period).strip().upper()
    if re.match(r"^\d{4}Q[1-4]$", text):
        return quarter_range(text)
    match = re.match(r"^(\d{4})-(\d{1,2})$", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    match = re.match(r"^(\d{4})$", text)
    if match:
        year = int(match.group(1))
        return date(year, 1, 1), date(year, 12, 31)
    raise ValueError(f"期间格式不正确：{period}")


def current_quarter(today: date | str | None = None) -> str:
    return quarter_of(today)


def current_month(today: date | str | None = None) -> str:
    day = _as_date(today)
    return f"{day.year}-{day.month:02d}"


def prev_quarter(period: str) -> str:
    start = quarter_start(period)
    return quarter_of(start - timedelta(days=1))


def next_quarter(period: str) -> str:
    end = quarter_end(period)
    return quarter_of(end + timedelta(days=1))


def quarter_options(today: date | str | None = None, count: int = 6, include_current: bool = True) -> list[str]:
    """返回最近若干季度选项（新的在前）。"""
    period = current_quarter(today)
    if not include_current:
        period = prev_quarter(period)
    options = []
    for _ in range(count):
        options.append(period)
        period = prev_quarter(period)
    return options


def month_options(today: date | str | None = None, count: int = 6) -> list[str]:
    """返回最近若干月份选项（新的在前），如 ``2026-10``。"""
    day = _as_date(today)
    year, month = day.year, day.month
    options = []
    for _ in range(count):
        options.append(f"{year}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return options


def period_label(period: str) -> str:
    """期间的中文展示名。"""
    text = str(period).strip().upper()
    match = re.match(r"^(\d{4})Q([1-4])$", text)
    if match:
        return f"{match.group(1)}年第{'一二三四'[int(match.group(2)) - 1]}季度"
    match = re.match(r"^(\d{4})-(\d{1,2})$", text)
    if match:
        return f"{match.group(1)}年{int(match.group(2))}月"
    return text


def _as_date(value: date | str | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return date.today()


def format_date_cn(value: date | str | None) -> str:
    """``2026-04-15`` → ``2026年4月15日``。"""
    day = _as_date(value)
    return f"{day.year}年{day.month}月{day.day}日"


def shift_to_workday(day: date) -> date:
    """把截止日顺延到最近的工作日（周末顺延，法定节假日请以税局公告为准）。"""
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


# =============================================================================
# 申报事项
# =============================================================================
def _tax_period_for(item_key: str, due: date) -> str:
    """根据申报事项与截止日推算「税款所属期」。"""
    if item_key in ("vat_quarterly", "corp_tax_quarterly", "financial_report_quarterly", "stamp_tax_quarterly"):
        return prev_quarter(quarter_of(due))
    if item_key == "personal_tax_monthly":
        first = date(due.year, due.month, 1)
        prev_month = first - timedelta(days=1)
        return f"{prev_month.year}-{prev_month.month:02d}"
    if item_key in ("annual_settlement", "annual_report"):
        return f"{due.year - 1}"
    return ""


def _deadline_description(item_key: str, item: dict[str, Any]) -> str:
    return f"法定截止日：{item['months_cn']}{item['day']}日（遇周末顺延，节假日以税务机关公告为准）"


def filing_deadlines(year: int) -> list[dict[str, Any]]:
    """生成某一自然年度内的全部申报截止事项。"""
    items: list[dict[str, Any]] = []
    for key, spec in config.FILING_CALENDAR.items():
        for month in spec["months"]:
            day = spec["day"]
            try:
                nominal = date(year, month, day)
            except ValueError:
                nominal = date(year, month, calendar.monthrange(year, month)[1])
            due = shift_to_workday(nominal)
            tax_period = _tax_period_for(key, nominal)
            months_cn = "、".join(f"{m}月" for m in spec["months"][:4]) if key.endswith("quarterly") else f"{month}月"
            items.append(
                {
                    "key": key,
                    "name": spec["name"],
                    "category": spec["category"],
                    "due_date": due.isoformat(),
                    "nominal_due_date": nominal.isoformat(),
                    "tax_period": tax_period,
                    "tax_period_label": period_label(tax_period) if tax_period else "",
                    "remind_days": spec.get("remind_days", [3]),
                    "months_cn": months_cn,
                    "description": f"{spec['name']}（税款所属期：{period_label(tax_period) if tax_period else '—'}）",
                    "disclaimer": DISCLAIMER,
                }
            )
    items.sort(key=lambda item: item["due_date"])
    return items


def deadline_status(due_date: str | date, today: date | str | None = None) -> dict[str, Any]:
    """返回截止日状态：剩余天数与紧急程度。"""
    due = _as_date(due_date)
    now = _as_date(today)
    days_left = (due - now).days
    if days_left < 0:
        level, label = "overdue", f"已逾期 {abs(days_left)} 天"
    elif days_left == 0:
        level, label = "urgent", "今天截止"
    elif days_left <= 3:
        level, label = "urgent", f"还剩 {days_left} 天"
    elif days_left <= 7:
        level, label = "soon", f"还剩 {days_left} 天"
    else:
        level, label = "normal", f"还剩 {days_left} 天"
    return {
        "days_left": days_left,
        "level": level,
        "label": label,
        "due_date": due.isoformat(),
        "due_date_cn": format_date_cn(due),
        "is_overdue": days_left < 0,
    }


def upcoming_deadlines(
    today: date | str | None = None,
    lookahead_days: int = 60,
    include_overdue: bool = False,
) -> list[dict[str, Any]]:
    """未来一段时间内即将到期的申报事项（按截止日升序）。"""
    now = _as_date(today)
    horizon = now + timedelta(days=lookahead_days)
    candidates = filing_deadlines(now.year) + filing_deadlines(now.year + 1)
    result: list[dict[str, Any]] = []
    for item in candidates:
        due = date.fromisoformat(item["due_date"])
        if due > horizon:
            continue
        if due < now and not include_overdue:
            continue
        status = deadline_status(due, now)
        item = dict(item)
        item.update(status)
        result.append(item)
    result.sort(key=lambda entry: entry["due_date"])
    return result


def next_deadline(today: date | str | None = None) -> dict[str, Any] | None:
    """最近一个未到期的申报事项。"""
    items = upcoming_deadlines(today, lookahead_days=120)
    return items[0] if items else None


def reminders(
    today: date | str | None = None,
    lookahead_days: int = 30,
    alert_window_days: int = 15,
) -> list[dict[str, Any]]:
    """命中提醒窗口的申报事项（用于首页 / 提醒推送）。

    命中规则：剩余天数在配置的 ``remind_days`` 内，或进入 ``alert_window_days`` 天的
    提醒窗口（默认 15 天，覆盖月度申报的常见提醒节奏）。
    """
    now = _as_date(today)
    hits: list[dict[str, Any]] = []
    for item in upcoming_deadlines(now, lookahead_days=lookahead_days):
        remind_days = set(int(day) for day in item.get("remind_days", []))
        if item["days_left"] in remind_days or item["days_left"] <= alert_window_days:
            hits.append(item)
    return hits


def reminder_text(today: date | str | None = None) -> str:
    """生成一段可直接展示的申报提醒文本。"""
    now = _as_date(today)
    items = reminders(now, lookahead_days=45)
    if not items:
        return "📅 近期没有需要办理的申报事项，保持关注即可。"

    lines = ["📅 申报提醒", ""]
    for item in items:
        lines.append(f"【{item['name']}】{item['label']}")
        lines.append(f"截止日期：{item['due_date_cn']}（税款所属期：{item.get('tax_period_label') or '—'}）")
        lines.append("")
    lines.append("⚠️ 请登录电子税务局完成申报，零申报也需按期提交。")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def completion_checklist(period: str) -> list[dict[str, Any]]:
    """季度申报完成清单（供填报页做进度跟踪）。"""
    label = period_label(period)
    return [
        {"item": "增值税及附加税费申报", "form_id": "VAT_QUARTERLY", "required": True, "note": f"税款所属期：{label}"},
        {"item": "企业所得税季度预缴申报", "form_id": "CIT_PREPAY", "required": True, "note": "按本年累计利润额预缴"},
        {"item": "财务报表报送（资产负债表、利润表）", "form_id": "FIN_REPORT", "required": True, "note": "与申报数据保持一致"},
        {"item": "印花税申报", "form_id": "STAMP_QUARTERLY", "required": False, "note": "有应税合同或减免类型时申报"},
        {"item": "个人所得税扣缴申报", "form_id": "IIT_MONTHLY", "required": False, "external": True,
         "note": "在自然人电子税务局（扣缴端）按月申报，本系统提供工资总额数据参考，不代生成申报表"},
    ]
