"""覆盖与代表性缺口分析。

口径约定：
- 覆盖单元格 = 场景 × 地区 × 时段（day/dusk/night）。
- 独立暴露：同一单元格内、来自同一行程(trip)的若干片段只计为一次独立暴露——
  相邻片段往往同车同路同天气，不能当作独立样本；不同行程才计为新的暴露。
- 只有复核通过的标注（confirmed/revised）计入覆盖；pending 单独提示为"待复核"。
- 一个片段可命中多个场景（多条标注），分别计入各自单元格。
"""
from __future__ import annotations

from .contract import (
    EVIDENCE_REVIEW_STATUSES,
    OCCUPYING_TASK_STATUSES,
    SCENARIOS,
    VISIBILITIES,
)

Cell = tuple  # (scenario, region, visibility)


def task_scenarios(task: dict) -> list[str]:
    """任务针对的场景列表（兼容只有单场景字段的导入任务）。"""
    scenarios = task.get("scenarios")
    if scenarios:
        return list(scenarios)
    return [task["scenario"]]


def task_cells(store, task: dict) -> list[Cell]:
    """任务计划覆盖的单元格：场景 × 窗口地区 × 全部时段。

    优先取任务接受时的地区快照；缺失时回退到窗口当前记录。
    """
    region = task.get("region")
    if not region:
        window = store.get("windows", task["window_id"])
        region = window["region"] if window else "?"
    return [(s, region, v) for s in task_scenarios(task) for v in VISIBILITIES]


def collect_evidence(store) -> tuple[dict, dict]:
    """返回 (有效暴露, 待复核计数)。

    有效暴露: cell -> {trip_id: [segment_id, ...]}
    待复核计数: cell -> 条数
    """
    exposures: dict[Cell, dict[str, list[str]]] = {}
    pending: dict[Cell, int] = {}
    for label in store.all("labels").values():
        segment = store.get("segments", label["segment_id"])
        if segment is None:
            continue
        cell = (label["scenario"], segment["region"], segment["visibility"])
        if label["review_status"] in EVIDENCE_REVIEW_STATUSES:
            trips = exposures.setdefault(cell, {})
            trips.setdefault(segment["trip_id"], []).append(segment["segment_id"])
        else:
            pending[cell] = pending.get(cell, 0) + 1
    return exposures, pending


def exposure_count(exposures: dict, cell: Cell) -> int:
    return len(exposures.get(cell, {}))


def known_regions(store) -> list[str]:
    regions = {s["region"] for s in store.all("segments").values()}
    regions |= {w["region"] for w in store.all("windows").values()}
    return sorted(regions)


def gap_cells(store) -> list[dict]:
    """逐单元格的覆盖情况，按 场景→地区→时段 排列。"""
    exposures, pending = collect_evidence(store)
    required = int(store.config["min_exposures_per_cell"])
    rows = []
    for scenario in SCENARIOS:
        for region in known_regions(store):
            for visibility in VISIBILITIES:
                cell = (scenario, region, visibility)
                count = exposure_count(exposures, cell)
                deficit = max(0, required - count)
                status = "covered" if count >= required else ("partial" if count else "blank")
                rows.append({
                    "cell": cell,
                    "exposures": count,
                    "pending": pending.get(cell, 0),
                    "required": required,
                    "deficit": deficit,
                    "status": status,
                })
    return rows


def planned_cells(store, statuses=frozenset({"accepted"})) -> dict:
    """已接受（默认）任务承诺覆盖的单元格：cell -> [task_id, ...]。"""
    cells: dict[Cell, list[str]] = {}
    for task in store.all("tasks").values():
        if task["status"] not in statuses:
            continue
        for cell in task_cells(store, task):
            cells.setdefault(cell, []).append(task["task_id"])
    return cells


def scenario_summary(rows: list[dict]) -> list[dict]:
    """按场景汇总：有效暴露、待复核、已覆盖/总单元格。"""
    summary = []
    for scenario in SCENARIOS:
        cells = [r for r in rows if r["cell"][0] == scenario]
        summary.append({
            "scenario": scenario,
            "exposures": sum(r["exposures"] for r in cells),
            "pending": sum(r["pending"] for r in cells),
            "covered": sum(1 for r in cells if r["status"] == "covered"),
            "partial": sum(1 for r in cells if r["status"] == "partial"),
            "blank": sum(1 for r in cells if r["status"] == "blank"),
            "total": len(cells),
        })
    return summary
