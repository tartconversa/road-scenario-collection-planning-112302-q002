"""采集计划：在车辆、路线许可与传感器约束下做联合贪心规划。

要点：
- 一个窗口生成一个候选任务，可同时服务多个场景×时段单元格；
  相同车辆和时段只能被一个任务占用，不能把各场景各自可行的计划简单相加。
- 贪心选择：每轮在未冲突候选中选"加权新增覆盖"最大者，直到没有正增益。
- 输出区分：新增覆盖任务 / 重复采样 / 冲突 / 不可执行（含原因）/ 仍有缺口。
"""
from __future__ import annotations

from datetime import datetime

from .contract import (
    OCCUPYING_TASK_STATUSES,
    SCENARIOS,
    VISIBILITIES,
)
from .coverage import exposure_count, collect_evidence, gap_cells, task_cells


class PlanError(ValueError):
    """计划请求无法完成（如未知优先级方案）。"""


def servable_scenarios(config: dict, sensor_config: str) -> list[str]:
    """该传感器配置在窗口期间可服务的场景。"""
    sensors = set(config["sensor_configs"].get(sensor_config, []))
    requirements = config["scenario_requirements"]
    return [s for s in SCENARIOS if set(requirements.get(s, [])) <= sensors]


def permit_problem(store, window: dict) -> str | None:
    """窗口的许可问题；无问题返回 None。"""
    config = store.config
    if window["route_state"] not in config["permit_required_route_states"]:
        return None
    permit_id = window["permit_id"]
    if permit_id in config["no_permit_ids"]:
        return "缺少许可"
    permit = store.get("permits", permit_id)
    if permit is not None:
        if not (permit["valid_from"] <= window["available_from"]
                and window["available_to"] <= permit["valid_to"]):
            return f"许可有效期({permit['valid_from']}~{permit['valid_to']})不覆盖窗口"
        return None
    if permit_id in config["expired_permit_ids"]:
        return "许可已过期"
    return None


def overlaps(a_from: str, a_to: str, b_from: str, b_to: str) -> bool:
    """左闭右开时段是否相交（ISO 日期字符串按字典序比较即可）。"""
    return a_from < b_to and b_from < a_to


def occupancy(store) -> list[dict]:
    """已被 accepted/done 任务占用的 车辆×时段（优先取任务接受时的快照）。"""
    occupied = []
    for task in store.all("tasks").values():
        if task["status"] not in OCCUPYING_TASK_STATUSES:
            continue
        window = store.get("windows", task["window_id"])
        occupied.append({
            "task_id": task["task_id"],
            "vehicle_id": task.get("vehicle_id") or (window["vehicle_id"] if window else "?"),
            "from": task.get("available_from") or (window["available_from"] if window else ""),
            "to": task.get("available_to") or (window["available_to"] if window else ""),
        })
    return occupied


def evaluate_windows(store, today: str) -> dict:
    """把全部窗口分类为：可行候选 / 不可执行(含原因) / 已有任务 / 与占用冲突。"""
    config = store.config
    tasks_by_window: dict[str, list[dict]] = {}
    for task in store.all("tasks").values():
        tasks_by_window.setdefault(task["window_id"], []).append(task)
    occupied = occupancy(store)

    result = {"feasible": [], "infeasible": [], "busy": [], "conflicts": []}
    for window in sorted(store.all("windows").values(), key=lambda w: w["window_id"]):
        reasons = []
        if window["available_to"] <= today:
            reasons.append("窗口已过期")
        if window["route_state"] == "closed":
            reasons.append("道路封闭")
        problem = permit_problem(store, window)
        if problem:
            reasons.append(problem)
        servable = servable_scenarios(config, window["sensor_config"])
        if not servable:
            reasons.append(f"传感器配置{window['sensor_config']}不满足任何场景要求")
        if reasons:
            result["infeasible"].append({"window": window, "reasons": reasons})
            continue
        active = [t for t in tasks_by_window.get(window["window_id"], [])
                  if t["status"] in ("proposed", "accepted", "done")]
        if active:
            result["busy"].append({"window": window, "tasks": active})
            continue
        conflicts_with = [
            o for o in occupied
            if o["vehicle_id"] == window["vehicle_id"]
            and overlaps(window["available_from"], window["available_to"], o["from"], o["to"])
        ]
        if conflicts_with:
            result["conflicts"].append({"window": window, "conflicts_with": conflicts_with})
            continue
        result["feasible"].append({"window": window, "servable": servable})
    return result


def _candidate_cells(candidate: dict) -> list[tuple]:
    region = candidate["window"]["region"]
    return [(s, region, v) for s in candidate["servable"] for v in VISIBILITIES]


def build_plan(store, profile_name: str, today: str, register: bool = True) -> dict:
    """按优先级方案生成联合采集计划；register=True 时把计划存入仓库。"""
    config = store.config
    profiles = config["priority_profiles"]
    if profile_name not in profiles:
        raise PlanError(f"未知优先级方案 {profile_name!r}，可选：{'、'.join(profiles)}")
    profile = profiles[profile_name]

    rows = gap_cells(store)
    original_deficit = {tuple(r["cell"]): r["deficit"] for r in rows}
    remaining = dict(original_deficit)

    evaluation = evaluate_windows(store, today)
    candidates = []
    for item in evaluation["feasible"]:
        candidates.append({
            "window": item["window"],
            "servable": item["servable"],
            "cells": _candidate_cells(item),
        })

    # 联合贪心：每轮选加权新增覆盖最大且与已选任务不冲突的候选
    selected: list[dict] = []
    while True:
        best = None
        best_gain = 0.0
        best_new_cells: list[tuple] = []
        for candidate in candidates:
            if candidate in selected:
                continue
            window = candidate["window"]
            if any(
                other["window"]["vehicle_id"] == window["vehicle_id"]
                and overlaps(window["available_from"], window["available_to"],
                             other["window"]["available_from"], other["window"]["available_to"])
                for other in selected
            ):
                continue
            new_cells = [c for c in candidate["cells"]
                         if remaining.get(c, 0) > 0 and float(profile.get(c[0], 0)) > 0]
            gain = sum(float(profile[c[0]]) for c in new_cells)
            if gain > best_gain + 1e-9:
                best, best_gain, best_new_cells = candidate, gain, new_cells
        if best is None:
            break
        selected.append(best)
        for cell in best_new_cells:
            remaining[cell] -= 1
        best["new_cells"] = best_new_cells
        best["gain"] = best_gain

    # 未选中候选的去向：重复采样 / 与已选冲突 / 场景不在方案内
    skipped = []
    for candidate in candidates:
        if candidate in selected:
            continue
        window = candidate["window"]
        had_deficit = [c for c in candidate["cells"] if original_deficit.get(c, 0) > 0]
        if not had_deficit:
            reason = "重复采样：历史数据已覆盖"
        elif all(remaining.get(c, 0) <= 0 for c in had_deficit):
            reason = "重复采样：已被选中任务覆盖"
        elif not any(float(profile.get(c[0], 0)) > 0 for c in had_deficit):
            reason = "优先级方案未覆盖其可服务场景"
        else:
            reason = "与已选任务的车辆时段冲突"
        skipped.append({"window_id": window["window_id"], "reason": reason})

    # 仍有缺口的单元格及原因
    uncovered = _explain_uncovered(store, evaluation, selected, original_deficit, remaining)

    seq = len(store.plans) + 1
    plan = {
        "plan_id": f"PLAN-{today.replace('-', '')}-{seq:02d}",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "today": today,
        "profile": profile_name,
        "status": "draft",
        "tasks": [],
        "skipped": skipped,
        "infeasible": [
            {"window_id": i["window"]["window_id"], "reasons": i["reasons"]}
            for i in evaluation["infeasible"]
        ],
        "busy": [
            {"window_id": b["window"]["window_id"],
             "tasks": [{"task_id": t["task_id"], "status": t["status"]} for t in b["tasks"]]}
            for b in evaluation["busy"]
        ],
        "conflicts": [
            {"window_id": c["window"]["window_id"],
             "conflicts_with": [o["task_id"] for o in c["conflicts_with"]]}
            for c in evaluation["conflicts"]
        ],
        "uncovered": uncovered,
    }
    for index, candidate in enumerate(selected, start=1):
        window = candidate["window"]
        plan["tasks"].append({
            "task_id": f"{plan['plan_id']}-T{index:02d}",
            "window_id": window["window_id"],
            "vehicle_id": window["vehicle_id"],
            "region": window["region"],
            "sensor_config": window["sensor_config"],
            "available_from": window["available_from"],
            "available_to": window["available_to"],
            "scenarios": [s for s in candidate["servable"]
                          if any(c[0] == s for c in candidate["new_cells"])],
            "cells": [list(c) for c in candidate["new_cells"]],
            "gain": round(candidate["gain"], 3),
            "kind": "新增覆盖",
        })
    if register:
        store.plans[plan["plan_id"]] = plan
    return plan


def _explain_uncovered(store, evaluation, selected, original_deficit, remaining) -> list[dict]:
    """对每个仍有缺口的单元格给出原因：封闭/许可/传感器/冲突/已有任务/不足。"""
    required = int(store.config["min_exposures_per_cell"])
    by_window = {}
    for item in evaluation["infeasible"]:
        by_window[item["window"]["window_id"]] = ("infeasible", item)
    for item in evaluation["busy"]:
        by_window[item["window"]["window_id"]] = ("busy", item)
    for item in evaluation["conflicts"]:
        by_window[item["window"]["window_id"]] = ("conflict", item)

    uncovered = []
    for cell, left in sorted(remaining.items()):
        if left <= 0:
            continue
        scenario, region, _ = cell
        reasons = []
        planned = original_deficit[cell] - left
        if planned > 0:
            reasons.append(f"已计划{planned}次暴露，仍缺{left}次")
        matched = False
        for window in store.all("windows").values():
            if window["region"] != region:
                continue
            if scenario not in servable_scenarios(store.config, window["sensor_config"]):
                matched = True
                reasons.append(f"{window['window_id']}: 传感器配置{window['sensor_config']}不满足该场景")
                continue
            matched = True
            entry = by_window.get(window["window_id"])
            if entry is None:
                continue  # 可行候选但未被选中（增益不足），无需解释
            kind, item = entry
            if kind == "infeasible":
                reasons.append(f"{window['window_id']}: {'、'.join(item['reasons'])}")
            elif kind == "busy":
                tasks = "、".join(t["task_id"] for t in item["tasks"])
                reasons.append(f"{window['window_id']}: 已有任务 {tasks}")
            else:
                others = "、".join(o["task_id"] for o in item["conflicts_with"])
                reasons.append(f"{window['window_id']}: 与已占用时段冲突({others})")
        if not matched:
            reasons.append("没有任何窗口覆盖该地区")
        uncovered.append({
            "cell": list(cell),
            "deficit": left,
            "required": required,
            "reasons": reasons,
        })
    return uncovered


def compare_profiles(store, profile_names: list[str], today: str) -> list[dict]:
    """同一状态下按不同优先级方案分别规划，供数据负责人比较覆盖改善。"""
    results = []
    for name in profile_names:
        plan = build_plan(store, name, today, register=False)
        per_scenario = {s: 0 for s in SCENARIOS}
        for task in plan["tasks"]:
            for cell in task["cells"]:
                per_scenario[cell[0]] += 1
        results.append({
            "profile": name,
            "tasks": len(plan["tasks"]),
            "new_cells": sum(len(t["cells"]) for t in plan["tasks"]),
            "total_gain": round(sum(t["gain"] for t in plan["tasks"]), 3),
            "per_scenario": per_scenario,
            "uncovered_cells": len(plan["uncovered"]),
        })
    return results
