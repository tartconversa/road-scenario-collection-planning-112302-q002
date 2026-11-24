"""命令行入口：持续导入、缺口分析、采集计划与计划维护。

用法概览见 README.md；所有命令只读写本地状态目录，不访问网络。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import coverage, lifecycle, planning, report
from .contract import DEFAULT_CONFIG, SCENARIO_ZH, SCENARIOS, ContractError
from .ingest import ingest_dir
from .store import Store


class CliError(Exception):
    """面向用户的操作错误（退出码 2）。"""


def _today(text: str | None) -> str:
    if text is None:
        return date.today().isoformat()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise CliError(f"--today 日期格式无效：{text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="road-scenario",
        description="道路场景缺口分析与车队采集排程（本地合成数据）",
    )
    parser.add_argument("--state", default="state", help="状态目录（默认 ./state）")
    parser.add_argument("--today", default=None,
                        help="业务日期 YYYY-MM-DD（默认系统当天），用于窗口/许可判断")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化状态目录并写入默认配置")

    p_ingest = sub.add_parser("ingest", help="导入一个投递目录中的 CSV 批次（幂等）")
    p_ingest.add_argument("drop", help="投递目录路径")

    sub.add_parser("gaps", help="输出代表性缺口分析报告")

    p_plan = sub.add_parser("plan", help="按优先级方案生成联合采集计划（草稿）")
    p_plan.add_argument("--profile", default="均衡", help="优先级方案名（默认：均衡）")

    p_compare = sub.add_parser("compare", help="比较不同优先级方案下的覆盖改善")
    p_compare.add_argument("--profiles", default=None,
                           help="逗号分隔的方案名；缺省比较配置中的全部方案")

    p_accept = sub.add_parser("accept", help="接受一份草稿计划，任务开始占用车辆时段")
    p_accept.add_argument("plan_id", help="计划编号（plan 命令输出）")

    p_confirm = sub.add_parser("confirm", help="确认导入的待确认任务")
    p_confirm.add_argument("task_ids", nargs="+", help="任务编号列表")

    sub.add_parser("reconcile", help="对账：撤销失效的可调整任务并解释暴露的缺口")

    p_receipt = sub.add_parser("receipt", help="登记采集回执并回报实际采集范围")
    p_receipt.add_argument("--task", required=True, help="任务编号")
    p_receipt.add_argument("--segments", nargs="+", required=True, help="实际采集的片段编号")
    p_receipt.add_argument("--at", default=None, help="采集日期 YYYY-MM-DD（默认业务日期）")
    p_receipt.add_argument("--receipt-id", default=None, help="回执编号（默认自动生成）")

    sub.add_parser("board", help="车队协调看板：冲突、待确认、执行中、已完成、已撤销")

    sub.add_parser("scenarios", help="列出场景、传感器需求与优先级方案")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        today = _today(args.today)
        store = Store.load(args.state)
        message = _dispatch(store, args, today)
        store.save()
    except (CliError, ContractError, planning.PlanError, lifecycle.LifecycleError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if message:
        print(message)
    return 0


def _dispatch(store: Store, args, today: str) -> str:
    if args.command == "init":
        return f"状态目录已就绪：{store.root}（配置见 {store.path.name} 中 config 节）"

    if args.command == "ingest":
        return report.format_ingest(ingest_dir(store, args.drop))

    if args.command == "gaps":
        rows = coverage.gap_cells(store)
        summary = coverage.scenario_summary(rows)
        planned = coverage.planned_cells(store)
        return report.format_gaps(store, rows, summary, planned)

    if args.command == "plan":
        plan = planning.build_plan(store, args.profile, today)
        return report.format_plan(plan) + \
            f"\n\n接受该计划请执行：accept {plan['plan_id']}"

    if args.command == "compare":
        names = ([n.strip() for n in args.profiles.split(",") if n.strip()]
                 if args.profiles else list(store.config["priority_profiles"]))
        if not names:
            raise CliError("没有可比较的优先级方案")
        return report.format_compare(planning.compare_profiles(store, names, today))

    if args.command == "accept":
        return report.format_accept(lifecycle.accept_plan(store, args.plan_id, today))

    if args.command == "confirm":
        return report.format_confirm(lifecycle.confirm_tasks(store, args.task_ids, today))

    if args.command == "reconcile":
        return report.format_reconcile(lifecycle.reconcile(store, today))

    if args.command == "receipt":
        result = lifecycle.register_receipt(
            store, args.task, args.segments,
            collected_at=args.at or today, receipt_id=args.receipt_id)
        return report.format_receipt(result)

    if args.command == "board":
        return report.format_board(lifecycle.board(store, today), today)

    if args.command == "scenarios":
        return _format_scenarios(store)

    raise CliError(f"未知命令 {args.command}")


def _format_scenarios(store: Store) -> str:
    config = store.config
    lines = ["== 场景与采集要求 =="]
    rows = []
    for scenario in SCENARIOS:
        sensors = "、".join(config["scenario_requirements"].get(scenario, []))
        rows.append([SCENARIO_ZH[scenario], scenario, sensors])
    lines.append(report.table(["场景", "标识", "所需传感器"], rows))
    lines.append("")
    lines.append("传感器配置：")
    for name, sensors in config["sensor_configs"].items():
        lines.append(f"  {name}: {'、'.join(sensors)}")
    lines.append("优先级方案：")
    for name, weights in config["priority_profiles"].items():
        weight_text = "、".join(f"{SCENARIO_ZH.get(s, s)}={w}" for s, w in weights.items())
        lines.append(f"  {name}: {weight_text}")
    lines.append(f"每单元格达标暴露数：{config['min_exposures_per_cell']}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
