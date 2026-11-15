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


def _merged_header_xlsx() -> bytes:
    """模拟扣缴端导出的多行表头 + 脱敏身份证（同尾号多人）。"""
    import openpyxl
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "个人所得税扣缴申报表"
    sheet.append(["个人所得税扣缴申报表"])
    sheet.append(["税款所属期：2026年10月01日至2026年10月31日"])
    sheet.append([])
    sheet.append(["序号", "姓名", "身份证件类型", "身份证件号码", "", "", "", "本月（次）情况", "", "", "", "", "", "", ""])
    sheet.append(["", "", "", "", "", "", "", "收入额计算", "", "", "专项扣除", "", "", "", ""])
    sheet.append(["", "", "", "", "", "", "", "收入", "费用", "免税收入", "基本养老保险费", "基本医疗保险费", "失业保险费", "住房公积金", "子女教育"])
    sheet.append([str(i) for i in range(1, 16)])
    for index, (name, id_card, income, pension) in enumerate(
        [("甲", "5****************2", 4300, 0), ("乙", "2****************5", 4850, 100), ("丙", "2****************5", 4700, 120)], start=1
    ):
        sheet.append([index, name, "居民身份证", id_card, id_card, "否", "正常工资薪金", income, 0, 0, pension, 0, 0, 0, 0])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_merged_header_and_masked_id_dedupe():
    result = iit_import.parse_file(_merged_header_xlsx(), "申报表.xlsx")
    assert [row["name"] for row in result["rows"]] == ["甲", "乙", "丙"]
    assert result["rows"][0]["gross_salary"] == 4300
    assert result["rows"][1]["social_insurance"] == 100


def test_create_payroll_drafts_after_import():
    result = iit_import.parse_file(_xlsx_bytes(), "申报表.xlsx")
    converted = [{"name": "王五", "id_card": "530101199303033456", "gross_salary": 7000, "social_insurance": 420, "housing_fund": 500, "special_deduction": 0, "tax_withheld": 21}]
    iit_import.import_employees(converted, period="2026-04")
    drafts = iit_import.create_payroll_drafts(converted, period="2026-04")
    assert len(drafts["created"]) == 1
    assert drafts["created"][0]["gross_salary"] == 7000
    assert drafts["created"][0]["status"] == "草稿"
