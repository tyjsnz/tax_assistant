"""电脑损坏/资料丢失后的找回清单（v1.37）。

按公司保存进度；每一步提供电子税务局/个税扣缴端/银行等找回路径，
以及回到本系统后如何补录和继续申报。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config

TOTAL_STEPS = 5

STEPS: list[dict[str, Any]] = [
    {
        "title": "先确认丢了什么、别重复申报",
        "summary": "先别急着重新报税；确认本地数据、备份、交接包和电子税务局记录哪些还在。",
        "bullets": [
            "旧电脑是否还能开机/拆硬盘？能开机先导出 data/ 或做备份；",
            "找找旧电脑的备份文件、交接包、U 盘、网盘、邮件附件；",
            "登录电子税务局查看最近所属期的申报状态，避免重复申报；",
            "确认税种认定和申报期限（按月/按季），再决定怎么补。",
        ],
        "links": [("电子税务局（国家税务总局入口）", "https://www.chinatax.gov.cn")],
        "actions": [],
    },
    {
        "title": "从电子税务局找回申报与完税记录",
        "summary": "历史申报表、申报状态、完税凭证是重建账套最可靠的依据。",
        "bullets": [
            "我要办税 → 税费申报及缴纳 → 申报查询：按年度/税种下载申报表；",
            "税费缴纳/完税证明：下载缴款记录、完税证明；",
            "财务报表报送：下载历史资产负债表、利润表；",
            "企业所得税：下载年度汇算申报表、弥补亏损明细；",
            "把下载文件按“年度+税种”命名，存到新电脑专用文件夹。",
        ],
        "links": [("电子税务局", "https://etax.chinatax.gov.cn"), ("12366", "https://12366.chinatax.gov.cn")],
        "actions": [],
    },
    {
        "title": "找回发票、个税、社保和银行流水",
        "summary": "发票、工资个税、社保和银行流水是补账的四类关键资料。",
        "bullets": [
            "电子税务局 → 发票业务 → 全量发票查询/税务数字账户：导出销项、进项发票（PDF/OFD/XML/Excel）；",
            "个税扣缴端：重新安装后查看/导出申报记录；人员信息可重新采集；",
            "社保费：电子税务局/社保客户端导出缴费记录与参保人数；",
            "银行：网银导出历史流水（收入/支出/税单）；",
            "合同、工资表、报销单：从邮箱、微信、网盘、供应商处找回。",
        ],
        "links": [("自然人电子税务局", "https://etax.chinatax.gov.cn")],
        "actions": [("📬 去发票归集", "recovery_goto_collector")],
    },
    {
        "title": "在新系统重建账套",
        "summary": "先建公司，再导入发票/流水，按历史申报表补录收入、费用、工资和申报记录。",
        "bullets": [
            "设置 → 企业信息：新建公司，录入名称、信用代码、地区、税种；",
            "发票归集/票据与流水：导入电子税务局下载的发票与银行流水；",
            "台账管理：按历史申报表补录各期收入、费用（可先按季度汇总）；",
            "员工与工资：补齐历史工资、个税、社保数据，保证累计预扣正确；",
            "申报填报：从最近一个未申报期间开始，历史期间以电子税务局记录为准。",
        ],
        "links": [],
        "actions": [("⚙️ 去设置建公司", "recovery_goto_settings"), ("📥 去导发票/流水", "recovery_goto_ingest")],
    },
    {
        "title": "继续申报并防止再次丢失",
        "summary": "核对累计数、亏损弥补和申报状态，之后每月备份。",
        "bullets": [
            "个税：核对累计收入、累计减除费用、已缴税额，避免累计预扣错误；",
            "企业所得税：核对以前年度亏损、已预缴税额、优惠资格；",
            "增值税：核对未开票收入、专票/普票、附加税费；",
            "提交前在电子税务局查看是否已有申报，避免重复；",
            "以后每月做数据备份/交接包，存到移动硬盘或私有云。",
        ],
        "links": [("设置 → 数据管理（备份/交接包）", "https://tax.flacn.net")],
        "actions": [("🗓️ 去报税日历", "recovery_goto_calendar")],
    },
]

_STATE_FILE = lambda: Path(config.DATA_DIR) / "recovery_progress.json"


def load_progress(company_id: str = "") -> dict[str, Any]:
    key = company_id or "current"
    try:
        path = _STATE_FILE()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        state = data.get(key) if isinstance(data, dict) else None
        steps = state.get("steps") if isinstance(state, dict) else []
        steps = [bool(x) for x in (steps or [])]
        return {"steps": steps + [False] * (TOTAL_STEPS - len(steps)), "updated_at": (state or {}).get("updated_at", "")}
    except Exception:  # noqa: BLE001
        return {"steps": [False] * TOTAL_STEPS, "updated_at": ""}


def save_progress(steps: list[bool], company_id: str = "") -> None:
    key = company_id or "current"
    try:
        path = _STATE_FILE()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
        data[key] = {"steps": [bool(x) for x in steps[:TOTAL_STEPS]], "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


__all__ = ["TOTAL_STEPS", "STEPS", "load_progress", "save_progress"]
