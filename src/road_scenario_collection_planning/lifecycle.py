"""计划生命周期：接受、确认、对账撤销、回执登记与协调看板。

规则要点：
- 计划被接受后，其任务占用对应车辆和时段；重复接受或冲突接受会被拒绝。
- 路线窗口取消/封闭、许可过期、传感器配置变更时，只撤销仍可调整
  （proposed/accepted）的任务；已完成（done）任务及其已采集片段保持原样。
- 回执登记时记录片段当时的版本号，后续片段更新不影响历史回执。
"""
from __future__ import annotations

from datetime import datetime

from .contract import ADJUSTABLE_TASK_STATUSES, SCENARIO_ZH
from .coverage import (
    collect_evidence,
    exposure_count,
    planned_cells,
    task_cells,
    task_scenarios,
)
from .planning import occupancy, overlaps, permit_problem, servable_scenarios


class LifecycleError(ValueError):
    """生命周期操作被拒绝（冲突、状态不允许等）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _snapshot_task(task: dict, window: dict | None) -> dict:
    """接受/确认任务时把车辆、地区、时段、传感器快照写进任务记录。"""
    if window:
        task.setdefault("vehicle_id", window["vehicle_id"])
        task.setdefault("region", window["region"])
        task.setdefault("sensor_config", window["sensor_config"])
        task.setdefault("available_from", window["available_from"])
        task.setdefault("available_to", window["available_to"])
    return task


def _conflicts_for(store, candidate: dict, ignore_task_ids=()) -> list[str]:
    occupied = occupancy(store)
    hits = []
    for other in occupied:
        if other["task_id"] in ignore_task_ids:
            continue
        if other["vehicle_id"] != candidate["vehicle_id"]:
            continue
        if overlaps(candidate["available_from"], candidate["available_to"],
                    other["from"], other["to"]):
            hits.append(other["task_id"])
    return hits


def accept_plan(store, plan_id: str, today: str) -> dict:
    """接受一份草稿计划：生成已接受任务并占用车辆时段。"""
    plan = store.plans.get(plan_id)
    if plan is None:
        raise LifecycleError(f"计划 {plan_id} 不存在")
    if plan["status"] != "draft":
        raise LifecycleError(f"计划 {plan_id} 当前状态为 {plan['status']}，不能重复接受")

    conflicts = {}
    for task in plan["tasks"]:
        hits = _conflicts_for(store, task)
        if hits:
            conflicts[task["task_id"]] = hits
    if conflicts:
        return {"ok": False, "plan_id": plan_id, "conflicts": conflicts}

    accepted_at = _now()
    for planned in plan["tasks"]:
        record = {
            "task_id": planned["task_id"],
            "window_id": planned["window_id"],
            "scenario": planned["scenarios"][0],
            "scenarios": list(planned["scenarios"]),
            "priority": "",
            "status": "accepted",
            "accepted_at": accepted_at,
            "receipt_id": "",
            "vehicle_id": planned["vehicle_id"],
            "region": planned["region"],
            "sensor_config": planned["sensor_config"],
            "available_from": planned["available_from"],
            "available_to": planned["available_to"],
            "cells": [list(c) for c in planned["cells"]],
            "source": plan_id,
        }
        store.upsert("tasks", record["task_id"], record)
    plan["status"] = "accepted"
    plan["accepted_at"] = accepted_at
    return {"ok": True, "plan_id": plan_id, "accepted": [t["task_id"] for t in plan["tasks"]]}


def confirm_tasks(store, task_ids: list[str], today: str) -> dict:
    """确认（接受）导入的待确认任务；逐个做占用冲突检查。"""
    confirmed, rejected = [], {}
    for task_id in task_ids:
        task = store.get("tasks", task_id)
        if task is None:
            rejected[task_id] = "任务不存在"
            continue
        if task["status"] != "proposed":
            rejected[task_id] = f"当前状态为 {task['status']}，仅待确认任务可确认"
            continue
        window = store.get("windows", task["window_id"])
        candidate = _snapshot_task(dict(task), window)
        if not candidate.get("available_from"):
            rejected[task_id] = "窗口记录缺失，无法判断时段"
            continue
        hits = _conflicts_for(store, candidate, ignore_task_ids={task_id})
        if hits:
            rejected[task_id] = f"与已占用时段冲突：{'、'.join(hits)}"
            continue
        updated = _snapshot_task(dict(task), window)
        updated["status"] = "accepted"
        updated["accepted_at"] = _now()
        store.upsert("tasks", task_id, updated)
        confirmed.append(task_id)
    return {"confirmed": confirmed, "rejected": rejected}


def reconcile(store, today: str) -> dict:
    """对账：撤销因窗口/许可/传感器变化而失效的可调整任务，并解释暴露的缺口。"""
    config = store.config
    revoked, kept_done = [], []
    for task in sorted(store.all("tasks").values(), key=lambda t: t["task_id"]):
        if task["status"] == "done":
            # 已完成任务不撤销；仅提示其窗口事实已变化，已采集片段保留当时版本
            window = store.get("windows", task["window_id"])
            if window is not None and _task_broken(store, task, window):
                kept_done.append({"task_id": task["task_id"],
                                  "note": "已执行，保留当时记录"})
            continue
        if task["status"] not in ADJUSTABLE_TASK_STATUSES:
            continue
        window = store.get("windows", task["window_id"])
        reason = "窗口已取消" if window is None else _task_broken(store, task, window)
        if not reason:
            continue
        updated = dict(task)
        updated["status"] = "revoked"
        updated["revoke_reason"] = reason
        updated["revoked_at"] = today
        store.upsert("tasks", task["task_id"], updated)
        revoked.append({
            "task_id": task["task_id"],
            "window_id": task["window_id"],
            "was": task["status"],
            "reason": reason,
        })

    # 因撤销暴露的缺口：仅"已接受"任务原本承诺的覆盖才构成计划缺口
    exposures, _ = collect_evidence(store)
    still_planned = planned_cells(store, statuses=frozenset({"accepted"}))
    required = int(config["min_exposures_per_cell"])
    exposed = {}
    for item in revoked:
        if item["was"] != "accepted":
            continue
        task = store.get("tasks", item["task_id"])
        for cell in task_cells(store, task):
            available = exposure_count(exposures, cell) + len(still_planned.get(cell, []))
            if available < required:
                exposed.setdefault(cell, {"cell": cell, "available": available,
                                          "required": required,
                                          "from_tasks": []})["from_tasks"].append(item["task_id"])
    return {
        "revoked": revoked,
        "kept_done": kept_done,
        "exposed_gaps": [
            {**v, "cell": list(k)} for k, v in sorted(exposed.items())
        ],
    }


def _task_broken(store, task: dict, window: dict) -> str:
    """任务在当前窗口事实下的失效原因；空串表示仍有效。"""
    if window["route_state"] == "closed":
        return "道路封闭"
    problem = permit_problem(store, window)
    if problem:
        return problem
    servable = servable_scenarios(store.config, window["sensor_config"])
    missing = [s for s in task_scenarios(task) if s not in servable]
    if missing:
        names = "、".join(SCENARIO_ZH.get(s, s) for s in missing)
        return f"传感器配置{window['sensor_config']}不再满足场景：{names}"
    return ""


def register_receipt(store, task_id: str, segment_ids: list[str],
                     collected_at: str, receipt_id: str | None = None) -> dict:
    """登记采集回执：任务转为已完成，记录片段当时版本，并回报实际采集范围。"""
    task = store.get("tasks", task_id)
    if task is None:
        raise LifecycleError(f"任务 {task_id} 不存在")
    receipt_id = receipt_id or f"RC-{task_id}"
    if receipt_id in store.receipts:
        return {"receipt_id": receipt_id, "task_id": task_id, "already": True}
    if task["status"] == "revoked":
        raise LifecycleError(f"任务 {task_id} 已撤销，不能登记回执")
    if task["status"] == "done":
        raise LifecycleError(f"任务 {task_id} 已完成（回执 {task.get('receipt_id')}）")
    missing = [s for s in segment_ids if store.get("segments", s) is None]
    if missing:
        raise LifecycleError(f"片段尚未导入：{'、'.join(missing)}")

    versions = {s: store.get("segments", s)["version"] for s in segment_ids}
    store.receipts[receipt_id] = {
        "receipt_id": receipt_id,
        "task_id": task_id,
        "segment_ids": list(segment_ids),
        "collected_at": collected_at,
        "segment_versions": versions,
        "registered_at": _now(),
    }
    updated = dict(task)
    updated["status"] = "done"
    updated["receipt_id"] = receipt_id
    store.upsert("tasks", task_id, updated)

    # 实际采集范围：回执片段上有效标注命中的单元格 vs 任务计划覆盖
    planned = set(task_cells(store, task))
    actual, pending_labels = set(), 0
    valid_status = {"confirmed", "revised"}
    for segment_id in segment_ids:
        segment = store.get("segments", segment_id)
        for label in store.all("labels").values():
            if label["segment_id"] != segment_id:
                continue
            cell = (label["scenario"], segment["region"], segment["visibility"])
            if label["review_status"] in valid_status:
                actual.add(cell)
            else:
                pending_labels += 1
    return {
        "receipt_id": receipt_id,
        "task_id": task_id,
        "already": False,
        "segments": list(segment_ids),
        "segment_versions": versions,
        "planned_covered": sorted(list(c) for c in planned & actual),
        "extra_covered": sorted(list(c) for c in actual - planned),
        "planned_missing": sorted(list(c) for c in planned - actual),
        "pending_labels": pending_labels,
    }


def board(store, today: str) -> dict:
    """车队协调员看板：冲突、待确认、执行中、已完成、已撤销。"""
    tasks = store.all("tasks")
    occupied = occupancy(store)
    conflicts = []
    by_vehicle: dict[str, list[dict]] = {}
    for item in occupied:
        by_vehicle.setdefault(item["vehicle_id"], []).append(item)
    for vehicle, items in sorted(by_vehicle.items()):
        items.sort(key=lambda i: i["from"])
        for prev, curr in zip(items, items[1:]):
            if overlaps(prev["from"], prev["to"], curr["from"], curr["to"]):
                conflicts.append({
                    "vehicle_id": vehicle,
                    "tasks": [prev["task_id"], curr["task_id"]],
                    "period": [curr["from"], curr["to"]],
                })

    def view(task):
        window = store.get("windows", task["window_id"])
        return {
            "task_id": task["task_id"],
            "window_id": task["window_id"],
            "scenarios": task_scenarios(task),
            "vehicle_id": task.get("vehicle_id") or (window["vehicle_id"] if window else "?"),
            "period": [task.get("available_from") or (window["available_from"] if window else "?"),
                       task.get("available_to") or (window["available_to"] if window else "?")],
            "receipt_id": task.get("receipt_id", ""),
            "revoke_reason": task.get("revoke_reason", ""),
        }

    proposed = [view(t) for t in tasks.values() if t["status"] == "proposed"]
    accepted = []
    for task in tasks.values():
        if task["status"] != "accepted":
            continue
        entry = view(task)
        entry["overdue"] = bool(entry["period"][1] != "?" and entry["period"][1] <= today)
        accepted.append(entry)
    done = []
    for task in tasks.values():
        if task["status"] != "done":
            continue
        entry = view(task)
        receipt = store.receipts.get(task.get("receipt_id", ""))
        entry["segments"] = len(receipt["segment_ids"]) if receipt else 0
        done.append(entry)
    revoked = [view(t) for t in tasks.values() if t["status"] == "revoked"]
    return {
        "conflicts": conflicts,
        "proposed": sorted(proposed, key=lambda e: e["task_id"]),
        "accepted": sorted(accepted, key=lambda e: e["task_id"]),
        "done": sorted(done, key=lambda e: e["task_id"]),
        "revoked": sorted(revoked, key=lambda e: e["task_id"]),
    }
