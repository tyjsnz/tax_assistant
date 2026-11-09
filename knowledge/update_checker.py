"""政策知识库更新检查：按 ``last_verified`` 计算过期、手动标记核验、统计概况。

背景：税务政策具有时效性（《报税Agent开发规范.md》5.3）。本项目采用「本地标记核验」策略：
每篇政策的 frontmatter 记录 ``last_verified``（最后人工 / 自动核验日期），默认超过 90 天视为
过期，提示用户到财政部、国家税务总局官网核对最新口径。检查与标记动作写入
``logs/policy_check_log.csv``（审计日志），便于追溯。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import config
from knowledge.retriever import (
    load_policies,
    parse_frontmatter,
    policy_dir,
)

#: 默认过期天数
DEFAULT_STALE_DAYS = 90


# =============================================================================
# 内部工具
# =============================================================================
def _today(value: date | str | None = None) -> date:
    if value is None:
        return config.today()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return config.today()


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _write_log(row: dict[str, Any]) -> None:
    """写政策检查审计日志；优先 ledger.audit，失败降级到 store.append_log，均失败不影响主流程。"""
    try:
        from ledger import audit

        if hasattr(audit, "log_policy_check"):
            audit.log_policy_check(
                row.get("checked_total"),
                row.get("stale_count"),
                row.get("latest_verified"),
                row.get("detail", ""),
            )
            return
    except Exception:  # noqa: BLE001 - 日志失败不得影响检查
        pass
    try:
        from ledger.store import append_log

        append_log("policy_check_log", row)
    except Exception:  # noqa: BLE001
        pass


def _stale_entry(policy: dict[str, Any], today: date) -> dict[str, Any] | None:
    """返回过期条目；未过期返回 None。"""
    verified = _parse_date(policy.get("last_verified"))
    if verified is None:
        return {
            "id": policy.get("id"),
            "title": policy.get("title"),
            "category": policy.get("category"),
            "last_verified": policy.get("last_verified") or "",
            "days": None,
            "reason": "缺少有效的 last_verified 日期",
        }
    days = (today - verified).days
    return {
        "id": policy.get("id"),
        "title": policy.get("title"),
        "category": policy.get("category"),
        "last_verified": verified.isoformat(),
        "days": days,
        "reason": f"距今 {days} 天未核验" if days >= 0 else "last_verified 晚于今天，请检查日期",
    }


# =============================================================================
# 对外接口
# =============================================================================
def stale_policies(days: int = DEFAULT_STALE_DAYS, today: date | str | None = None) -> list[dict[str, Any]]:
    """返回超过 ``days`` 天未核验（或缺少核验日期）的政策列表。"""
    try:
        threshold = int(days)
    except (TypeError, ValueError):
        threshold = DEFAULT_STALE_DAYS
    now = _today(today)
    stale: list[dict[str, Any]] = []
    for policy in load_policies():
        entry = _stale_entry(policy, now)
        if entry is None:
            continue
        if entry["days"] is None or entry["days"] > threshold:
            stale.append(entry)
    stale.sort(key=lambda item: (item["days"] is not None, item["days"] or 0), reverse=True)
    return stale


def check_policies(days: int = DEFAULT_STALE_DAYS) -> dict[str, Any]:
    """检查全部政策的核验时效，写审计日志并给出更新建议。

    返回
    ----
    ``{"checked_total", "stale": [...], "latest_verified", "advice": [str], "checked_at"}``
    """
    try:
        threshold = int(days)
    except (TypeError, ValueError):
        threshold = DEFAULT_STALE_DAYS
    now = config.today()
    policies = load_policies()
    stale: list[dict[str, Any]] = []
    verified_dates: list[date] = []
    for policy in policies:
        parsed = _parse_date(policy.get("last_verified"))
        if parsed is not None:
            verified_dates.append(parsed)
        entry = _stale_entry(policy, now)
        if entry is None:
            continue
        if entry["days"] is None or entry["days"] > threshold:
            stale.append(entry)
    stale.sort(key=lambda item: (item["days"] is not None, item["days"] or 0), reverse=True)
    latest_verified = max(verified_dates).isoformat() if verified_dates else ""

    advice: list[str] = []
    if stale:
        advice.append(
            f"共 {len(stale)} 篇政策超过 {threshold} 天未核验，建议到财政部、国家税务总局官网"
            "或电子税务局「政策法规」栏目核对最新口径后，在知识库中标记核验日期。"
        )
        titles = "、".join(str(item["title"]) for item in stale[:5])
        advice.append(f"优先核对：{titles}{' 等' if len(stale) > 5 else ''}。")
        advice.append("重点确认文号、执行期限（如 2027-12-31 到期政策）与地方执行口径是否变化。")
    else:
        advice.append(f"全部 {len(policies)} 篇政策均在 {threshold} 天内核验过，暂无过期项。")

    row = {
        "checked_total": len(policies),
        "stale_count": len(stale),
        "latest_verified": latest_verified,
        "detail": "；".join(str(item["id"]) for item in stale[:20]),
    }
    _write_log(row)

    return {
        "checked_total": len(policies),
        "stale": stale,
        "latest_verified": latest_verified,
        "advice": advice,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days": threshold,
    }


def _replace_last_verified(text: str, date_text: str, fallback_title: str = "") -> str:
    """在 Markdown 文本中更新（或插入）frontmatter 的 ``last_verified`` 字段。"""
    lines = text.lstrip("\ufeff").splitlines()
    if lines and lines[0].strip() == "---":
        end = None
        for index in range(1, len(lines)):
            if lines[index].strip() in ("---", "..."):
                end = index
                break
        if end is not None:
            for index in range(1, end):
                if re.match(r"^\s*last_verified\s*:", lines[index]):
                    lines[index] = f"last_verified: {date_text}"
                    break
            else:
                lines.insert(end, f"last_verified: {date_text}")
            return "\n".join(lines) + "\n"
    # 没有 frontmatter：补一个最小可用的
    header = ["---", f"title: {fallback_title or '未命名政策'}", f"last_verified: {date_text}", "---", ""]
    return "\n".join(header) + "\n" + text


def mark_verified(policy_id: str, verified_date: date | str | None = None) -> dict[str, Any]:
    """把某篇政策的 ``last_verified`` 更新为指定日期（默认今天），并写审计日志。

    返回更新后的政策字典（含 ``last_verified``）；政策不存在时抛 ``KeyError``。
    """
    from knowledge.retriever import get_policy

    policy = get_policy(policy_id)
    if not policy:
        raise KeyError(f"知识库中不存在政策：{policy_id}")
    path = Path(policy["path"])
    if not path.exists():
        # 支持传入知识库内的相对 id（例如测试中替换了 POLICY_DIR）
        candidate = policy_dir() / f"{policy['id']}.md"
        path = candidate if candidate.exists() else path
    if not path.exists():
        raise FileNotFoundError(f"政策文件不存在：{path}")

    date_text = _today(verified_date).isoformat()
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OSError(f"读取政策文件失败：{exc}") from exc
    updated = _replace_last_verified(original, date_text, fallback_title=str(policy.get("title") or ""))
    if updated != original:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(updated, encoding="utf-8")
        tmp.replace(path)
    load_policies(force=True)
    refreshed = get_policy(str(policy.get("id"))) or dict(policy)
    refreshed["last_verified"] = date_text

    _write_log(
        {
            "checked_total": len(load_policies()),
            "stale_count": 0,
            "latest_verified": date_text,
            "detail": f"手动标记核验：{policy.get('id')} → {date_text}",
        }
    )
    return refreshed


def policy_stats() -> dict[str, Any]:
    """知识库统计：总数、分类分布、最早 / 最新核验日期。"""
    policies = load_policies()
    categories: dict[str, int] = {}
    dated: list[tuple[date, dict[str, Any]]] = []
    for policy in policies:
        category = str(policy.get("category") or "未分类")
        categories[category] = categories.get(category, 0) + 1
        parsed = _parse_date(policy.get("last_verified"))
        if parsed is not None:
            dated.append((parsed, policy))
    oldest = None
    latest = None
    if dated:
        dated.sort(key=lambda pair: pair[0])
        oldest_date, oldest_policy = dated[0]
        latest_date = dated[-1][0]
        oldest = {
            "id": oldest_policy.get("id"),
            "title": oldest_policy.get("title"),
            "last_verified": oldest_date.isoformat(),
            "days": (config.today() - oldest_date).days,
        }
        latest = latest_date.isoformat()
    else:
        latest = ""
    return {
        "total": len(policies),
        "categories": categories,
        "oldest": oldest,
        "latest_verified": latest,
        "stale_days": DEFAULT_STALE_DAYS,
    }
