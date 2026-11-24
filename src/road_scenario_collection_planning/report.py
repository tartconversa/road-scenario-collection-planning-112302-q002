"""中文报告格式化：把分析结果渲染为终端可读文本。"""
from __future__ import annotations

import unicodedata

from .contract import (
    ROUTE_STATE_ZH,
    SCENARIO_ZH,
    TASK_STATUS_ZH,
    VISIBILITY_ZH,
)

DISCLAIMER = "说明：场景清单仅来自公开宣传口径，作为采集需求来源；本报告不对产品功能状态作任何结论。"


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def table(headers: list[str], rows: list[list[str]]) -> str:
    cols = len(headers)
    widths = [_width(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], _width(str(row[i])))
    lines = ["  ".join(pad(str(h), widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(cols)))
    for row in rows:
        lines.append("  ".join(pad(str(row[i]), widths[i]) for i in range(cols)))
    return "\n".join(lines)


def cell_zh(cell) -> str:
    scenario, region, visibility = cell
    return f"{SCENARIO_ZH.get(scenario, scenario)}×{region}×{VISIBILITY_ZH.get(visibility, visibility)}"


def scenarios_zh(scenarios) -> str:
    return "、".join(SCENARIO_ZH.get(s, s) for s in scenarios)


# ---- 导入报告 ----

def format_ingest(report: dict) -> str:
    lines = [f"导入目录：{report['drop']}"]
    if report["batch_id"]:
        lines.append(f"批次号：{report['batch_id']}")
    for item in report["files"]:
        lines.append(
            f"  {item['name']}: 新增 {item['new']}，更新 {item['updated']}，"
            f"未变化 {item['same']}，错误 {item['errors']}")
    for name in report["skipped_files"]:
        lines.append(f"  {name}: 内容未变化，跳过")
    for warning in report["warnings"]:
        lines.append(f"  警告：{warning}")
    for error in report["errors"]:
        lines.append(f"  错误：{error}")
    if not report["files"] and not report["skipped_files"]:
        lines.append("  目录中没有可导入的契约文件")
    return "\n".join(lines)


# ---- 缺口报告 ----

def format_gaps(store, rows: list[dict], summary: list[dict], planned: dict) -> str:
    lines = ["== 场景覆盖总览（有效暴露 = 复核通过标注的独立行程数）=="]
    lines.append(table(
        ["场景", "有效暴露", "待复核", "已覆盖格", "部分覆盖", "空白格", "总格数"],
        [[SCENARIO_ZH[s["scenario"]], s["exposures"], s["pending"],
          s["covered"], s["partial"], s["blank"], s["total"]] for s in summary],
    ))
    gaps = [r for r in rows if r["deficit"] > 0]
    lines.append("")
    lines.append(f"== 代表性缺口（{len(gaps)} 个单元格未达标，每格需 {rows[0]['required'] if rows else '-'} 次独立暴露）==")
    if gaps:
        lines.append(table(
            ["单元格", "现有暴露", "待复核", "缺口", "已接受计划"],
            [[cell_zh(r["cell"]), r["exposures"], r["pending"], r["deficit"],
              len(planned.get(tuple(r["cell"]), []))] for r in gaps],
        ))
    else:
        lines.append("  全部单元格均已达标。")
    pending_total = sum(r["pending"] for r in rows)
    if pending_total:
        lines.append(f"\n待复核标注共 {pending_total} 条，复核结果可能改变上述覆盖数与缺口范围。")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


# ---- 计划报告 ----

def format_plan(plan: dict) -> str:
    lines = [f"== 采集计划 {plan['plan_id']}（优先级方案：{plan['profile']}，状态：{plan['status']}）=="]
    if plan["tasks"]:
        lines.append("新增覆盖任务：")
        lines.append(table(
            ["任务", "窗口", "车辆", "地区", "时段", "目标场景", "加权增益"],
            [[t["task_id"], t["window_id"], t["vehicle_id"], t["region"],
              f"{t['available_from']}~{t['available_to']}",
              scenarios_zh(t["scenarios"]), t["gain"]] for t in plan["tasks"]],
        ))
    else:
        lines.append("  没有可新增覆盖的任务。")
    if plan["skipped"]:
        lines.append("未选中窗口：")
        for item in plan["skipped"]:
            lines.append(f"  {item['window_id']}: {item['reason']}")
    if plan["busy"]:
        lines.append("已有任务的窗口（不参与本次规划）：")
        for item in plan["busy"]:
            tasks = "、".join(f"{t['task_id']}({TASK_STATUS_ZH.get(t['status'], t['status'])})"
                              for t in item["tasks"])
            lines.append(f"  {item['window_id']}: {tasks}")
    if plan["conflicts"]:
        lines.append("与已占用车辆时段冲突的窗口：")
        for item in plan["conflicts"]:
            lines.append(f"  {item['window_id']}: 冲突任务 {'、'.join(item['conflicts_with'])}")
    if plan["infeasible"]:
        lines.append("不可执行的窗口：")
        for item in plan["infeasible"]:
            lines.append(f"  {item['window_id']}: {'、'.join(item['reasons'])}")
    if plan["uncovered"]:
        lines.append(f"计划执行后仍有缺口的单元格（{len(plan['uncovered'])} 个）：")
        for item in plan["uncovered"]:
            lines.append(f"  {cell_zh(item['cell'])}: 仍缺 {item['deficit']} 次；"
                         f"{'；'.join(item['reasons'])}")
    return "\n".join(lines)


def format_compare(results: list[dict]) -> str:
    lines = ["== 不同优先级方案的覆盖改善比较（同一现状、同一批窗口）=="]
    header = ["方案", "任务数", "新增覆盖格", "加权得分", "仍缺口格"] + \
             [SCENARIO_ZH[s] for s in results[0]["per_scenario"]] if results else []
    rows = []
    for r in results:
        rows.append([r["profile"], r["tasks"], r["new_cells"], r["total_gain"],
                     r["uncovered_cells"]] + list(r["per_scenario"].values()))
    lines.append(table(header, rows))
    lines.append("（右侧各列为该方案计划新增的各场景暴露单元格数）")
    return "\n".join(lines)


# ---- 生命周期报告 ----

def format_accept(result: dict) -> str:
    if not result.get("ok"):
        lines = [f"计划 {result['plan_id']} 接受失败，存在车辆时段冲突："]
        for task_id, hits in result["conflicts"].items():
            lines.append(f"  {task_id}: 与 {'、'.join(hits)} 冲突")
        return "\n".join(lines)
    return (f"计划 {result['plan_id']} 已接受，以下任务开始占用对应车辆和时段：\n  "
            + "、".join(result["accepted"]))


def format_confirm(result: dict) -> str:
    lines = []
    if result["confirmed"]:
        lines.append(f"已确认任务：{'、'.join(result['confirmed'])}")
    for task_id, reason in result["rejected"].items():
        lines.append(f"无法确认 {task_id}：{reason}")
    return "\n".join(lines) if lines else "没有需要确认的任务。"


def format_reconcile(report: dict) -> str:
    lines = ["== 计划对账结果 =="]
    if report["revoked"]:
        lines.append(f"已撤销 {len(report['revoked'])} 个仍可调整的任务：")
        for item in report["revoked"]:
            was = TASK_STATUS_ZH.get(item["was"], item["was"])
            lines.append(f"  {item['task_id']}（窗口 {item['window_id']}，原状态 {was}）：{item['reason']}")
    else:
        lines.append("没有需要撤销的任务。")
    if report["kept_done"]:
        lines.append("以下任务已完成，尽管窗口事实已变化，仍保留当时记录：")
        for item in report["kept_done"]:
            lines.append(f"  {item['task_id']}: {item['note']}")
    if report["exposed_gaps"]:
        lines.append("因撤销而暴露的覆盖缺口：")
        for gap in report["exposed_gaps"]:
            lines.append(f"  {cell_zh(gap['cell'])}: 现有 {gap['available']} 次 / "
                         f"需 {gap['required']} 次（原由 {'、'.join(gap['from_tasks'])} 承诺）")
    return "\n".join(lines)


def format_receipt(result: dict) -> str:
    if result.get("already"):
        return f"回执 {result['receipt_id']} 已登记过，未重复处理。"
    lines = [f"回执 {result['receipt_id']} 已登记，任务 {result['task_id']} 转为已完成。"]
    lines.append(f"  实际采集片段 {len(result['segments'])} 个（版本 "
                 + "、".join(f"{s}@v{v}" for s, v in result["segment_versions"].items()) + "）")
    if result["planned_covered"]:
        lines.append("  计划内覆盖：" + "；".join(cell_zh(c) for c in result["planned_covered"]))
    if result["extra_covered"]:
        lines.append("  计划外命中：" + "；".join(cell_zh(c) for c in result["extra_covered"]))
    if result["planned_missing"]:
        lines.append("  计划目标未命中（可能无有效标注）："
                     + "；".join(cell_zh(c) for c in result["planned_missing"]))
    if result["pending_labels"]:
        lines.append(f"  其中 {result['pending_labels']} 条标注仍待复核，复核后覆盖数可能变化。")
    return "\n".join(lines)


def format_board(board: dict, today: str) -> str:
    lines = [f"== 车队协调看板（业务日期 {today}）=="]
    lines.append(f"车辆时段冲突：{len(board['conflicts'])} 起")
    for item in board["conflicts"]:
        lines.append(f"  {item['vehicle_id']}: 任务 {'、'.join(item['tasks'])} "
                     f"在 {item['period'][0]}~{item['period'][1]} 重叠")
    lines.append(f"待确认任务：{len(board['proposed'])} 个")
    for item in board["proposed"]:
        lines.append(f"  {item['task_id']}（窗口 {item['window_id']}，{item['vehicle_id']}，"
                     f"{item['period'][0]}~{item['period'][1]}）：{scenarios_zh(item['scenarios'])}")
    lines.append(f"已接受待执行：{len(board['accepted'])} 个")
    for item in board["accepted"]:
        flag = "【逾期未回执】" if item.get("overdue") else ""
        lines.append(f"  {item['task_id']}（{item['vehicle_id']}，"
                     f"{item['period'][0]}~{item['period'][1]}）{flag}：{scenarios_zh(item['scenarios'])}")
    lines.append(f"已完成：{len(board['done'])} 个")
    for item in board["done"]:
        lines.append(f"  {item['task_id']}（回执 {item['receipt_id'] or '未登记'}，"
                     f"片段 {item['segments']} 个）：{scenarios_zh(item['scenarios'])}")
    lines.append(f"已撤销：{len(board['revoked'])} 个")
    for item in board["revoked"]:
        lines.append(f"  {item['task_id']}：{item['revoke_reason']}")
    return "\n".join(lines)
