"""合成演示批次生成器。

`data/` 是第一批（基线导入）；本模块生成 `round2` 投递目录，模拟持续运营中
到达的新事实：标注复核结果、新采集片段与回执、窗口取消、传感器配置变更、
许可有效期登记和新窗口。全部内容固定、可复现，仅供本地流程演示与测试。

用法：
    python -m road_scenario_collection_planning.synthetic --out drops
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .contract import FILE_SCHEMAS

# ---- round2：复核结论（同一 label_id 的新状态，导入后覆盖数会变化）----------

ROUND2_LABELS = [
    # 复核通过：小动物/坑洼 从"待复核"变为有效证据
    ["L-001", "S-001", "animal", "confirmed", "AB-5"],
    ["L-004", "S-004", "pothole", "confirmed", "AB-5"],
    ["L-007", "S-007", "animal", "confirmed", "AB-5"],
    ["L-010", "S-010", "pothole", "revised", "AB-5"],
    ["L-013", "S-013", "animal", "confirmed", "AB-5"],
    ["L-016", "S-016", "pothole", "confirmed", "AB-5"],
    ["L-019", "S-019", "animal", "revised", "AB-5"],
    ["L-022", "S-022", "pothole", "revised", "AB-5"],
    ["L-025", "S-025", "animal", "confirmed", "AB-5"],
    ["L-028", "S-028", "pothole", "confirmed", "AB-5"],
    # 复核打回与改标：原有覆盖随之减少/迁移
    ["L-006", "S-006", "sign", "pending", "AB-5"],
    ["L-009", "S-009", "debris", "revised", "AB-5"],
    # 新采集片段的标注
    ["L-101", "S-101", "animal", "confirmed", "AB-9"],
    ["L-102", "S-102", "animal", "confirmed", "AB-9"],
    ["L-103", "S-102", "pothole", "pending", "AB-9"],
    ["L-104", "S-103", "animal", "confirmed", "AB-9"],
    ["L-105", "S-104", "pothole", "confirmed", "AB-9"],
    ["L-106", "S-104", "water", "confirmed", "AB-9"],
    ["L-107", "S-105", "animal", "confirmed", "AB-9"],
    ["L-108", "S-106", "animal", "confirmed", "AB-9"],
]

# ---- round2：新采集片段（由已接受任务采回） ---------------------------------

ROUND2_SEGMENTS = [
    ["S-101", "T-100", "urban", "R-S", "day", "CFG-B", "20.0", "20.4"],
    ["S-102", "T-100", "rural", "R-S", "dusk", "CFG-B", "20.5", "20.9"],
    ["S-103", "T-101", "rural", "R-S", "night", "CFG-B", "21.0", "21.4"],
    ["S-104", "T-102", "urban", "R-S", "day", "CFG-A", "21.5", "21.9"],
    ["S-105", "T-103", "rural", "R-S", "dusk", "CFG-B", "22.0", "22.4"],
    ["S-106", "T-104", "urban", "R-S", "day", "CFG-B", "22.5", "22.9"],
]

# ---- round2：窗口变化（取消、传感器降级）与新增窗口 --------------------------

ROUND2_WINDOWS = [
    ["W-021", "V-00", "CFG-B", "R-N", "closed", "2026-12-02", "2026-12-03", "P-1"],
    ["W-022", "V-01", "CFG-B", "R-S", "urban-only", "2026-12-03", "2026-12-04", "P-2"],
    ["W-101", "V-02", "CFG-A", "R-N", "open", "2026-12-22", "2026-12-23", "P-2"],
    ["W-102", "V-03", "CFG-A", "R-W", "urban-only", "2026-12-23", "2026-12-24", "P-2"],
    ["W-103", "V-04", "CFG-B", "R-N", "open", "2026-12-24", "2026-12-25", "P-3"],
]

# ---- round2：许可有效期登记（P-1 提前到期） ----------------------------------

ROUND2_PERMITS = [
    ["P-1", "2026-12-01", "2026-12-10"],
    ["P-2", "2026-12-01", "2026-12-31"],
    ["P-3", "2026-12-01", "2026-12-31"],
]

# ---- round2：采集回执（一行一个片段） ----------------------------------------

ROUND2_RECEIPTS = [
    ["RC-101", "CT-001", "S-101", "2026-12-03"],
    ["RC-101", "CT-001", "S-102", "2026-12-03"],
    ["RC-102", "CT-007", "S-103", "2026-12-09"],
    ["RC-103", "CT-010", "S-104", "2026-12-12"],
    ["RC-104", "CT-013", "S-105", "2026-12-15"],
    ["RC-105", "CT-019", "S-106", "2026-12-21"],
]

ROUND2_FILES = {
    "segments.csv": ROUND2_SEGMENTS,
    "labels.csv": ROUND2_LABELS,
    "fleet_windows.csv": ROUND2_WINDOWS,
    "permits.csv": ROUND2_PERMITS,
    "receipts.csv": ROUND2_RECEIPTS,
}


def write_drop(out_dir: str | Path) -> Path:
    """把 round2 批次写入 <out_dir>/round2 并返回该目录。"""
    drop = Path(out_dir) / "round2"
    drop.mkdir(parents=True, exist_ok=True)
    for filename, rows in ROUND2_FILES.items():
        path = drop / filename
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(FILE_SCHEMAS[filename])
            writer.writerows(rows)
    return drop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成合成演示批次（round2）")
    parser.add_argument("--out", default="drops", help="输出目录（默认 ./drops）")
    args = parser.parse_args(argv)
    drop = write_drop(args.out)
    print(f"已生成合成批次：{drop}")
    for filename, rows in ROUND2_FILES.items():
        print(f"  {filename}: {len(rows)} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
