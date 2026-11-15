"""综合所得申报表导入测试（v1.42）。"""

from __future__ import annotations

import io

import pandas as pd

from ingest import iit_import
from ledger import employees


def _xlsx_bytes() -> bytes:
    frame = pd.DataFrame([
        {"姓名": "张三", "身份证件号码": "530101199001011234", "本期收入": "8,000.00",
         "基本养老保险费": "480", "基本医疗保险费": "120", "失业保险费": "30",
         "住房公积金": "600", "专项附加扣除": "1000", "本期应预扣预缴税额": "45.00"},
        {"姓名": "李四", "身份证件号码": "530101199202022345", "本期收入": "9000",
         "基本养老保险费": "540", "基本医疗保险费": "135", "失业保险费": "34",
         "住房公积金": "700", "专项附加扣除": "0", "本期应预扣预缴税额": "90"},
    ])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="综合所得申报")
    return buffer.getvalue()


def test_parse_and_import_employees():
    result = iit_import.parse_file(_xlsx_bytes(), "综合所得申报表.xlsx")
    assert len(result["rows"]) == 2
    row = result["rows"][0]
    assert row["name"] == "张三"
    assert row["social_insurance"] == 630
    assert row["id_card_masked"].startswith("530101")
    imported = iit_import.import_employees(result["rows"], period="2026-03")
    assert len(imported["created"]) == 2
    saved = employees.list_employees(status=None, include_left=True)
    assert any(item["name"] == "张三" and "****" in item["id_card_masked"] for item in saved)
    # 再导一次应按身份证/姓名跳过
    again = iit_import.import_employees(result["rows"], period="2026-03")
    assert len(again["created"]) == 0
