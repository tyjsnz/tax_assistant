"""扣缴端工资模板导出测试（v1.46）。"""

from __future__ import annotations

from filing import payroll_template
from ledger import employees, payroll


def test_export_template_with_ids(tmp_path):
    employee = employees.add_employee({"name": "模板测试", "employee_no": "E001", "hire_date": "2026-01-01"})
    payroll.build_payroll("2026-10", employee_ids=[employee["id"]])
    row = payroll.list_payroll("2026-10")[0]
    payroll.update_payroll_row(row["id"], {
        "gross_salary": 8000, "social_insurance": 600, "pension_insurance": 400,
        "medical_insurance": 150, "unemployment_insurance": 50, "housing_fund": 500,
    })
    target = tmp_path / "正常工资薪金所得.xls"
    result = payroll_template.export_xls("2026-10", id_cards={employee["id"]: "530101199001011234"}, path=target)
    assert target.exists() and result["count"] == 1
    assert not result["warnings"]
    import xlrd
    workbook = xlrd.open_workbook(str(target))
    sheet = workbook.sheet_by_name("正常工资薪金收入")
    headers = [sheet.cell_value(0, col) for col in range(sheet.ncols)]
    assert headers[1] == "*姓名"
    assert sheet.cell_value(1, headers.index("*姓名")) == "模板测试"
    assert sheet.cell_value(1, headers.index("*证件号码")) == "530101199001011234"
    assert sheet.cell_value(1, headers.index("本期收入")) == 8000
    assert sheet.cell_value(1, headers.index("基本养老保险费")) == 400
    assert len(headers) == 30
    assert sheet.cell_value(1, headers.index("累计子女教育")) == 0


def test_save_copy_to_desktop(tmp_path, monkeypatch):
    source = tmp_path / "正常工资薪金所得_2026-10.xls"
    source.write_bytes(b"test")
    monkeypatch.setenv("HOME", str(tmp_path))
    target = payroll_template.save_copy_to_desktop(source)
    assert target.exists() and target.parent.name == "Desktop"
    assert target.read_bytes() == b"test"


def test_export_uses_employee_full_id(tmp_path):
    employee = employees.add_employee({
        "name": "本地身份证", "employee_no": "E002", "hire_date": "2026-01-01",
        "id_card_full": "530101199505055678",
    })
    payroll.build_payroll("2026-10", employee_ids=[employee["id"]])
    row = payroll.list_payroll("2026-10")[0]
    payroll.update_payroll_row(row["id"], {"gross_salary": 5000})
    target = tmp_path / "id.xls"
    result = payroll_template.export_xls("2026-10", path=target)
    assert not result["missing_ids"]
    import xlrd
    sheet = xlrd.open_workbook(str(target)).sheet_by_name("正常工资薪金收入")
    headers = [sheet.cell_value(0, c) for c in range(sheet.ncols)]
    assert sheet.cell_value(1, headers.index("*证件号码")) == "530101199505055678"
