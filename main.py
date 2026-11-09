"""命令行入口（可选）：算税、生成申报包、加载示例数据、命令行对话。

Web 界面是主要使用方式（``.venv/bin/streamlit run ui/streamlit_app.py``）；
本文件便于快速验证与脚本化，例如：

    python main.py calc-vat --total 103000 --special 0 --period 2026Q1
    python main.py file --period 2026Q1 --out exports/
    python main.py calendar --days 45
    python main.py demo
    python main.py chat
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def _print_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_calc_vat(args: argparse.Namespace) -> int:
    from engine import calc_vat

    result = calc_vat(args.total, args.special, args.period)
    if args.json:
        _print_json(result)
    else:
        print(f"\n📊 增值税计算 · {result['period']}")
        print("-" * 56)
        for row in result["breakdown"]:
            print(f"  {row['label']:<18}¥{row['value']:>14,.2f}")
        print("-" * 56)
        print(f"  {result['note']}")
        policy_source = result.get("vat_policy_source")
        if policy_source:
            print(f"  📌 政策依据：{policy_source}")
        for warning in result["warnings"]:
            print(f"  ⚠️ {warning}")
        print(f"  {result['disclaimer']}")
    return 0


def cmd_calc_corp(args: argparse.Namespace) -> int:
    from engine import calc_corp_tax

    result = calc_corp_tax(
        annual_profit=args.profit,
        rd_cost=args.rd,
        employees=args.employees,
        total_assets=args.assets,
        period=args.period,
    )
    if args.json:
        _print_json(result)
    else:
        print("\n📊 企业所得税年度测算")
        print("-" * 56)
        print(f"  会计利润        ¥{result['annual_profit']:>14,.2f}")
        print(f"  研发加计扣除    ¥{result['rd_deduction']:>14,.2f}")
        print(f"  应纳税所得额    ¥{result['taxable_income']:>14,.2f}")
        print(f"  实际税负        {result['effective_rate'] * 100:>14.2f}%")
        print(f"  应缴企业所得税  ¥{result['corp_tax']:>14,.2f}")
        print("-" * 56)
        print(f"  {result['note']}")
        print(f"  {result['disclaimer']}")
    return 0


def cmd_file(args: argparse.Namespace) -> int:
    from filing import export, progress, registry

    bundles = registry.build_all(args.period)
    out_dir = Path(args.out) if args.out else Path(config.EXPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = export.export_package_excel(args.period, bundles, out_dir / f"申报包_{args.period}.xlsx")
    for bundle in bundles:
        progress.save_filing(bundle, status="草稿")
    print(f"\n📋 {args.period} 申报表已生成：{len(bundles)} 张")
    for bundle in bundles:
        print(f"  - {bundle['form_name']}（{bundle['form_id']}）栏目 {sum(len(s['lines']) for s in bundle['sections'])} 项")
    print(f"\n📦 申报包已导出：{path}")
    print(f"  {config.DISCLAIMER}")
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    from engine import reminder_text, upcoming_deadlines

    items = upcoming_deadlines(lookahead_days=args.days)
    print(f"\n🗓️  未来 {args.days} 天申报事项")
    print("-" * 72)
    for item in items:
        print(f"  {item['due_date']}  {item['label']:<10} {item['name']}（{item.get('tax_period_label') or '—'}）")
    print("-" * 72)
    print(reminder_text())
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from ledger import models, seed

    result = seed.seed_demo_data(reset=args.reset)
    print("\n✨ 示例数据已生成：")
    table_names = set(models.TABLE_NAMES)
    for table, count in result.items():
        if table in table_names:
            label = models.TABLE_DEFS[table].get("label", table)
            print(f"  {label:<12}{count:>4} 条")
    extras = {key: value for key, value in result.items() if key not in table_names}
    if extras:
        print("  " + "；".join(f"{key} = {value:,.2f}" for key, value in extras.items() if isinstance(value, (int, float))))
    print(f"\n提示：启动界面后可在「首页/台账」查看示例数据。{config.DISCLAIMER}")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from agent import orchestrator

    if not config.is_ai_configured():
        print("⚠️  未检测到 DeepSeek API Key，将使用离线模式（可回答算税/日历等常见问题）。")
        print("   配置方式：界面「⚙️ 设置」填写，或 export DEEPSEEK_API_KEY=sk-xxx\n")
    history: list[dict] = []
    print(f"税小智 · 命令行对话（输入 exit 退出）\n{config.DISCLAIMER}\n")
    while True:
        try:
            text = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not text:
            continue
        if text.lower() in {"exit", "quit", "q"}:
            print("再见。")
            return 0
        result = orchestrator.run_turn(text, history)
        print(f"\n税小智：{result.get('reply', '').strip()}\n")
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": result.get("reply", "")})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="税小智 · 小规模纳税人自主税务填报系统（命令行）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_vat = sub.add_parser("calc-vat", help="计算增值税及附加税费")
    p_vat.add_argument("--total", type=float, required=True, help="季度含税总销售额")
    p_vat.add_argument("--special", type=float, default=0.0, help="其中专票含税金额")
    p_vat.add_argument("--period", default="", help="所属季度，如 2026Q1")
    p_vat.add_argument("--json", action="store_true", help="输出 JSON")
    p_vat.set_defaults(func=cmd_calc_vat)

    p_corp = sub.add_parser("calc-corp", help="测算企业所得税")
    p_corp.add_argument("--profit", type=float, required=True, help="年度会计利润")
    p_corp.add_argument("--rd", type=float, default=0.0, help="研发费用")
    p_corp.add_argument("--employees", type=int, default=None, help="从业人数")
    p_corp.add_argument("--assets", type=float, default=None, help="资产总额")
    p_corp.add_argument("--period", default="", help="所属年度，如 2026")
    p_corp.add_argument("--json", action="store_true", help="输出 JSON")
    p_corp.set_defaults(func=cmd_calc_corp)

    p_file = sub.add_parser("file", help="生成某季度申报表并导出 Excel 申报包")
    p_file.add_argument("--period", required=True, help="所属季度，如 2026Q1")
    p_file.add_argument("--out", default=None, help="导出目录（默认 exports/）")
    p_file.set_defaults(func=cmd_file)

    p_cal = sub.add_parser("calendar", help="查看申报日历")
    p_cal.add_argument("--days", type=int, default=60, help="查看未来天数")
    p_cal.set_defaults(func=cmd_calendar)

    p_demo = sub.add_parser("demo", help="生成示例数据")
    p_demo.add_argument("--reset", action="store_true", help="先清空已有台账")
    p_demo.set_defaults(func=cmd_demo)

    p_chat = sub.add_parser("chat", help="命令行 AI 对话")
    p_chat.set_defaults(func=cmd_chat)
    return parser


def main(argv: list[str] | None = None) -> int:
    config.ensure_dirs()
    try:
        from ledger import init_storage

        init_storage()
    except Exception:  # noqa: BLE001 - 首次运行时数据层异常不应阻止算税命令
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - 命令行统一友好报错
        print(f"❌ 执行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
