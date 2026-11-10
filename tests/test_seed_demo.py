"""演示账套脚本测试（P0-4 开源发布准备的示例数据）。"""

from __future__ import annotations

import workspace


def test_seed_demo_script_creates_company():
    import scripts.seed_demo as seed_demo

    assert seed_demo.main(["--reset", "--quiet"]) == 0
    demo = workspace.get_company("demo")
    assert demo is not None
    assert demo["name"] == seed_demo.DEMO_NAME
    assert demo["id"] == "demo"
    # 演示公司被激活，且已生成台账/工资/结账/汇算数据
    assert workspace.active_company_id() == "demo"
    from ledger import store

    assert store.table("income").count() >= 10
    assert store.table("expense").count() >= 20
    assert store.table("employees").count() >= 3
    assert store.table("payroll").count() >= 6
    assert store.table("periods").count() >= 1
    assert store.table("annual_settlements").count() == 1
    # 再次运行幂等（复用公司）
    assert seed_demo.main(["--quiet"]) == 0
    assert workspace.get_company("demo") is not None
