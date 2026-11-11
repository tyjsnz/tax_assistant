"""申报截止日导出为 iCalendar (.ics)，可导入手机/电脑日历做系统级提醒。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

PRODID = "-//zhixiaoshui//tax calendar//CN"


def _escape(text: Any) -> str:
    value = str(text or "")
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def build_ics(deadlines: list[dict[str, Any]], *, calendar_name: str = "智小税 申报日历") -> str:
    """把申报截止事项转成 ICS 文本（全天事件，提前 3 天提醒）。"""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:" + PRODID,
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + _escape(calendar_name),
    ]
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for item in deadlines:
        due_text = str(item.get("due_date") or "")
        try:
            due = date.fromisoformat(due_text[:10])
        except ValueError:
            continue
        uid = f"{item.get('key') or item.get('name')}-{due.isoformat()}@zhixiaoshui"
        summary = f"申报：{item.get('name') or ''}"
        description = str(item.get("description") or item.get("name") or "")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{_escape(uid)}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{due.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(due + timedelta(days=1)).strftime('%Y%m%d')}",
                f"SUMMARY:{_escape(summary)}",
                f"DESCRIPTION:{_escape(description)}",
                "BEGIN:VALARM",
                "TRIGGER:-P3D",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape(summary)}",
                "END:VALARM",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


__all__ = ["build_ics", "PRODID"]
