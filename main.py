"""命令行入口（可选）：算税、生成申报包、加载示例数据、命令行对话。

Web 界面是主要使用方式（``.venv/bin/streamlit run ui/streamlit_app.py``）；
本文件便于快速验证与脚本化，例如：

    python main.py calc-vat --total 103000 --special 0 --period 2026Q1
    python main.py file --period 2026Q1 --out exports/
    python main.py calendar --days 45
    python main.py demo
    python main.py ingest --invoices examples/invoices/*.xml --bank examples/bank/*.csv --confirm-score 90
    python main.py pool
    python main.py periods
    python main.py close --period 2026-01
    python main.py archive --period 2026Q1
    python main.py backup --tag before_close
    python main.py restore --file data/backups/backup_xxx.zip --yes
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


def cmd_periods(args: argparse.Namespace) -> int:
    """查看会计期间结账状态与金额概览。"""
    from closing import periods as period_store

    rows = period_store.list_periods(limit=args.limit)
    if args.json:
        _print_json({"periods": rows})
        return 0
    print("\n🗓️ 会计期间状态")
    print("-" * 78)
    print(f"  {'期间':<10}{'类型':<6}{'状态':<8}{'收入(不含税)':>16}{'费用':>14}{'利润':>14}")
    for row in rows:
        print(
            f"  {str(row.get('period')):<10}{str(row.get('period_type')):<6}{str(row.get('status')):<8}"
            f"{float(row.get('income_exclusive') or 0):>16,.2f}"
            f"{float(row.get('expense') or 0):>14,.2f}"
            f"{float(row.get('profit') or 0):>14,.2f}"
        )
    print("-" * 78)
    print(f"  {config.DISCLAIMER}")
    return 0


def _confirm(prompt: str) -> bool:
    """交互确认（非交互环境返回 False）。"""
    try:
        if not sys.stdin.isatty():
            return False
        answer = input(f"{prompt} [y/N] ").strip().lower()
        return answer in {"y", "yes", "是"}
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_close(args: argparse.Namespace) -> int:
    """执行期间结账：先做结账前检查，再生成损益结转凭证并锁定期间。"""
    from closing import checks, close

    report = checks.pre_close_checks(args.period)
    print(f"\n🗓️ {report.get('period_label') or args.period} 结账前检查：{report.get('level')}")
    print("-" * 78)
    for check in report.get("checks") or []:
        icon = {"PASS": "✅", "WARNING": "⚠️", "ERROR": "❌"}.get(str(check.get("status")), "•")
        print(f"  {icon} {check.get('name')}：{check.get('message')}")
    print("-" * 78)
    if not report.get("ok"):
        print("  ❌ 存在阻断项，无法结账，请先处理上述 ERROR。")
        return 1

    warnings = report.get("warnings") or []
    if warnings and not args.force:
        if not _confirm(f"存在 {len(warnings)} 项提示，仍要继续结账吗？"):
            print("  已取消结账（确认无异议后可加 --force 跳过确认）。")
            return 1

    result = close.close_period(args.period, operator="cli", force=args.force, note=args.note)
    print(f"\n✅ {result.get('period')} 已结账，快照哈希 {str(result.get('snapshot_hash'))[:16]}…")
    for entry in result.get("entries") or []:
        print(f"  凭证 {entry.get('entry_no')}：", end="")
        print("；".join(
            f"{line.get('direction')} {line.get('account')} {float(line.get('amount') or 0):,.2f}"
            for line in entry.get("lines") or []
        ))
    print(f"  {config.DISCLAIMER}")
    return 0


def cmd_reopen(args: argparse.Namespace) -> int:
    """反结账（仅最近一个已结账期间，需二次确认）。"""
    from closing import close

    if not args.yes and not _confirm(f"确认反结账 {args.period}？将删除自动生成的损益结转凭证"):
        print("  已取消（非交互环境请加 --yes）。")
        return 1
    result = close.reopen_period(args.period, operator="cli", reason=args.reason or "")
    print(f"\n↩️ {result.get('period')} 已反结账，已删除结转凭证："
          f"{'、'.join(result.get('removed_entries') or []) or '无'}")
    print(f"  {config.DISCLAIMER}")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    """生成并归档本期申报包（Excel + PDF + SHA-256 清单）。"""
    from archiving import filing_archive

    result = filing_archive.archive_filing_package(args.period, include_pdf=not args.no_pdf, note=args.note)
    print(f"\n📦 {args.period} 申报包已归档：{result.get('dir')}")
    for item in result.get("files") or []:
        print(f"  - {item.get('name')}  {item.get('size')} B  sha256:{str(item.get('sha256'))[:16]}…")
    print(f"  清单：{result.get('manifest_path')}")
    for warning in result.get("warnings") or []:
        print(f"  ⚠️ {warning}")
    if args.json:
        _print_json(result)
    print(f"  {config.DISCLAIMER}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """创建数据备份（zip，不含 API Key）。"""
    from archiving import backup

    result = backup.create_backup(tag=args.tag or "", note=args.note or "")
    print(f"\n💾 备份已创建：{result.get('path')}")
    print(f"  大小 {result.get('size_bytes')} B  sha256:{str(result.get('file_hash'))[:16]}…")
    print(f"  台账 {len(result.get('tables') or {})} 张，文件 {len(result.get('files') or [])} 个")
    print(f"  {config.DISCLAIMER}")
    return 0


def cmd_backups(args: argparse.Namespace) -> int:
    """列出数据备份。"""
    from archiving import backup

    rows = backup.list_backups()
    if args.json:
        _print_json({"backups": rows})
        return 0
    print("\n💾 数据备份")
    print("-" * 78)
    for row in rows:
        print(f"  {str(row.get('created_at') or '')}  {str(row.get('name')):<34}"
              f"{int(row.get('size_bytes') or 0):>10} B  {'✓' if row.get('exists') else '缺失'}")
    if not rows:
        print("  （暂无备份，使用 main.py backup 创建）")
    print("-" * 78)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """恢复数据备份：默认只预览，--yes 才真正覆盖当前台账。"""
    from archiving import restore

    preview = restore.inspect_backup(args.file)
    print(f"\n📂 备份预览：{preview.get('path')}")
    print(f"  创建时间：{preview.get('created_at') or '—'}")
    tables = preview.get("tables") or {}
    print("  表与条数：" + "，".join(f"{name} {count} 条" for name, count in tables.items()))
    verify = preview.get("verify") or {}
    print(f"  完整性校验：{'✅ 通过' if verify.get('ok') else '❌ 未通过'}")
    if not args.yes:
        print("\n  仅预览，未写入数据。确认后加 --yes 执行恢复（恢复前会自动安全备份）。")
        return 0
    result = restore.restore_backup(args.file, dry_run=False, safety_backup=not args.no_safety, operator="cli")
    print(f"\n✅ 已恢复：")
    for item in result.get("restored") or []:
        print(f"  - {item.get('table')} {item.get('rows')} 条")
    if result.get("safety_backup"):
        print(f"  恢复前安全备份：{result.get('safety_backup')}")
    print(f"  {config.DISCLAIMER}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """导入发票 / 银行流水，可选自动匹配并按分数确认入账。"""
    from ingest import importer, matcher

    files: list[tuple[str, bytes]] = []
    for pattern in list(args.invoices or []) + list(args.bank or []):
        for path in sorted(Path().glob(pattern)) if any(ch in pattern for ch in "*?[") else [Path(pattern)]:
            if not path.is_file():
                print(f"⚠️ 文件不存在，已跳过：{path}")
                continue
            files.append((path.name, path.read_bytes()))
    if not files:
        print("请用 --invoices / --bank 指定要导入的文件（支持通配符，如 examples/invoices/*.xml）。")
        return 1

    result = importer.import_files(files, auto_match=not args.no_match)
    print(f"\n📥 导入完成：文件 {result.get('files', len(files))} 个，"
          f"发票 {result.get('invoices', 0)} 张，流水 {result.get('transactions', 0)} 笔")
    for error in result.get("errors") or []:
        print(f"  ❌ {error.get('file')}: {error.get('error')}")
    for skipped in result.get("skipped") or []:
        print(f"  - 跳过重复：{skipped.get('reason')}")

    if args.confirm_score is not None:
        suggestions = [match for match in matcher.list_matches() if match.get("status") == "建议"]
        confirmed = 0
        for match in suggestions:
            if float(match.get("score") or 0) >= float(args.confirm_score):
                try:
                    matcher.confirm_match(match["id"])
                    confirmed += 1
                except ValueError as exc:
                    print(f"  ⚠️ 确认失败：{exc}")
        print(f"\n✅ 已按分数 ≥ {args.confirm_score} 自动确认入账 {confirmed} 笔匹配")
    else:
        summary = matcher.match_summary()
        print(f"\n🔎 匹配建议：{summary.get('suggestions', 0)} 条，"
              f"待匹配发票 {summary.get('invoices_pending', 0)} 张、流水 {summary.get('transactions_pending', 0)} 笔")
        print("   在「📥 票据与流水」页面勾选确认，或加 --confirm-score 90 自动确认。")

    if args.json:
        _print_json(result)
    print(f"  {config.DISCLAIMER}")
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    """查看票据池 / 流水池 / 匹配概览。"""
    from ingest import importer, matcher

    data = {"pool": importer.pool_summary(), "match": matcher.match_summary()}
    if args.json:
        _print_json(data)
        return 0
    pool = data["pool"]
    invoices = pool.get("invoices") or {}
    transactions = pool.get("transactions") or {}
    matches = pool.get("matches") or {}
    print("\n📊 票据与流水池")
    print("-" * 52)
    print(f"  发票池：{invoices.get('count', 0)} 张  状态 {invoices.get('by_status', {})}")
    print(f"  流水池：{transactions.get('count', 0)} 笔  状态 {transactions.get('by_status', {})}")
    print(f"  匹配：建议 {matches.get('by_status', {}).get('建议', 0)} 条，"
          f"已确认 {matches.get('by_status', {}).get('已确认', 0)} 条")
    match = data["match"]
    income_amount = match.get("matched_income_amount", match.get("matched_income", 0)) or 0
    expense_amount = match.get("matched_expense_amount", match.get("matched_expense", 0)) or 0
    print(f"  已入账收入 ¥{float(income_amount):,.2f}（{match.get('matched_income', 0)} 笔），"
          f"费用 ¥{float(expense_amount):,.2f}（{match.get('matched_expense', 0)} 笔）")
    print("-" * 52)
    print(f"  {config.DISCLAIMER}")
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

    p_ingest = sub.add_parser("ingest", help="导入发票与银行流水（可选自动匹配入账）")
    p_ingest.add_argument("--invoices", nargs="*", default=[], help="发票文件（xml/ofd/pdf，支持通配符）")
    p_ingest.add_argument("--bank", nargs="*", default=[], help="银行流水文件（csv/xlsx，支持通配符）")
    p_ingest.add_argument("--no-match", action="store_true", help="仅导入，不自动生成匹配建议")
    p_ingest.add_argument("--confirm-score", type=float, default=None, metavar="N",
                          help="自动确认匹配分 ≥ N 的建议并入账（如 90）")
    p_ingest.add_argument("--json", action="store_true", help="输出 JSON")
    p_ingest.set_defaults(func=cmd_ingest)

    p_pool = sub.add_parser("pool", help="查看票据池 / 流水池 / 匹配概览")
    p_pool.add_argument("--json", action="store_true", help="输出 JSON")
    p_pool.set_defaults(func=cmd_pool)

    p_periods = sub.add_parser("periods", help="查看会计期间结账状态")
    p_periods.add_argument("--limit", type=int, default=12)
    p_periods.add_argument("--json", action="store_true")
    p_periods.set_defaults(func=cmd_periods)

    p_close = sub.add_parser("close", help="期间结账（生成损益结转凭证并锁定）")
    p_close.add_argument("--period", required=True, help="期间，如 2026-01 或 2026Q1")
    p_close.add_argument("--force", action="store_true", help="跳过 WARNING 确认（ERROR 仍会阻断）")
    p_close.add_argument("--note", default="")
    p_close.set_defaults(func=cmd_close)

    p_reopen = sub.add_parser("reopen", help="反结账（仅最近一期）")
    p_reopen.add_argument("--period", required=True)
    p_reopen.add_argument("--reason", default="")
    p_reopen.add_argument("--yes", action="store_true", help="跳过交互确认")
    p_reopen.set_defaults(func=cmd_reopen)

    p_archive = sub.add_parser("archive", help="归档申报包（Excel + PDF + SHA-256 清单）")
    p_archive.add_argument("--period", required=True, help="申报所属期，如 2026Q1")
    p_archive.add_argument("--no-pdf", action="store_true", help="不生成 PDF")
    p_archive.add_argument("--note", default="")
    p_archive.add_argument("--json", action="store_true")
    p_archive.set_defaults(func=cmd_archive)

    p_backup = sub.add_parser("backup", help="创建数据备份（zip，不含 API Key）")
    p_backup.add_argument("--tag", default="")
    p_backup.add_argument("--note", default="")
    p_backup.set_defaults(func=cmd_backup)

    p_backups = sub.add_parser("backups", help="列出数据备份")
    p_backups.add_argument("--json", action="store_true")
    p_backups.set_defaults(func=cmd_backups)

    p_restore = sub.add_parser("restore", help="恢复数据备份（默认仅预览）")
    p_restore.add_argument("--file", required=True, help="备份 zip 路径或备份编号")
    p_restore.add_argument("--yes", action="store_true", help="确认执行恢复（覆盖当前台账）")
    p_restore.add_argument("--no-safety", action="store_true", help="恢复前不做安全备份（不推荐）")
    p_restore.set_defaults(func=cmd_restore)

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
