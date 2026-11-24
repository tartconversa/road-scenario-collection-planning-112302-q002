"""持续导入：把一个投递目录中的 CSV 批次并入状态仓库。

- 幂等：按文件内容哈希去重，同一文件重复导入不会产生任何变化。
- 版本化：同一主键的记录内容变化时，旧记录进入历史并升版本。
- 容错：单行不合法只跳过该行并记录错误，不中断整个批次。
- 任务保护：本地已 done/revoked 的任务不会被导入的旧状态覆盖。
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .contract import (
    FILE_ENTITY,
    FILE_SCHEMAS,
    INGEST_ORDER,
    ContractError,
    file_sha256,
    validate_row,
)
from .lifecycle import register_receipt, LifecycleError


def ingest_dir(store, drop_dir: str | Path) -> dict:
    """导入一个投递目录，返回中文导入报告（字典）。"""
    drop = Path(drop_dir)
    if not drop.is_dir():
        raise ContractError(f"投递目录不存在：{drop}")
    report = {
        "drop": str(drop),
        "files": [],
        "errors": [],
        "warnings": [],
        "skipped_files": [],
    }
    batch = {
        "batch_id": f"B-{len(store.data['batches']) + 1:04d}",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "drop": str(drop),
        "file_hashes": [],
        "files": {},
    }

    for filename in INGEST_ORDER:
        path = drop / filename
        if not path.exists():
            continue
        digest = file_sha256(path)
        if store.seen_hash(digest):
            report["skipped_files"].append(filename)
            continue
        counts = {"new": 0, "updated": 0, "same": 0, "errors": 0}
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != FILE_SCHEMAS[filename]:
                report["errors"].append(
                    f"{filename}: 表头与契约不一致（实际 {reader.fieldnames}）")
                continue
            rows = list(reader)

        if filename == "receipts.csv":
            _ingest_receipts(store, rows, filename, report, counts)
        else:
            kind, key_field = FILE_ENTITY[filename]
            for index, row in enumerate(rows, start=2):
                try:
                    record = validate_row(filename, row, index)
                except ContractError as exc:
                    counts["errors"] += 1
                    report["errors"].append(f"{filename} {exc}")
                    continue
                key = record[key_field]
                if kind == "tasks" and _protected_task(store, key, record):
                    counts["same"] += 1
                    report["warnings"].append(
                        f"{filename} 第{index}行: 任务 {key} 本地已完成/已撤销，保留本地状态")
                    continue
                outcome = store.upsert(kind, key, record)
                counts[outcome] += 1
                _check_reference(store, kind, record, filename, index, report)

        batch["file_hashes"].append(digest)
        batch["files"][filename] = counts
        report["files"].append({"name": filename, **counts})

    if batch["file_hashes"]:
        store.add_batch(batch)
        report["batch_id"] = batch["batch_id"]
    else:
        report["batch_id"] = None
    return report


def _protected_task(store, task_id: str, incoming: dict) -> bool:
    """本地已完成/已撤销的任务不被导入状态覆盖。"""
    current = store.get("tasks", task_id)
    return (
        current is not None
        and current["status"] in ("done", "revoked")
        and incoming["status"] != current["status"]
    )


def _check_reference(store, kind: str, record: dict, filename: str,
                     lineno: int, report: dict) -> None:
    """引用完整性提示（不阻断导入，引用对象可能在后续批次到达）。"""
    missing = None
    if kind == "labels" and store.get("segments", record["segment_id"]) is None:
        missing = f"片段 {record['segment_id']}"
    elif kind == "tasks" and store.get("windows", record["window_id"]) is None:
        missing = f"窗口 {record['window_id']}"
    if missing:
        report["warnings"].append(
            f"{filename} 第{lineno}行: 引用的{missing}尚未导入")


def _ingest_receipts(store, rows: list[dict], filename: str,
                     report: dict, counts: dict) -> None:
    """按 receipt_id 聚合回执行并逐条登记。"""
    grouped: dict[str, dict] = {}
    for index, row in enumerate(rows, start=2):
        try:
            record = validate_row(filename, row, index)
        except ContractError as exc:
            counts["errors"] += 1
            report["errors"].append(f"{filename} {exc}")
            continue
        group = grouped.setdefault(record["receipt_id"], {
            "task_id": record["task_id"],
            "collected_at": record["collected_at"],
            "segment_ids": [],
        })
        if group["task_id"] != record["task_id"]:
            counts["errors"] += 1
            report["errors"].append(
                f"{filename} 第{index}行: 回执 {record['receipt_id']} 对应多个任务")
            continue
        group["segment_ids"].append(record["segment_id"])
        group["collected_at"] = max(group["collected_at"], record["collected_at"])

    for receipt_id, group in sorted(grouped.items()):
        try:
            result = register_receipt(
                store, group["task_id"], group["segment_ids"],
                group["collected_at"], receipt_id=receipt_id)
        except LifecycleError as exc:
            counts["errors"] += 1
            report["errors"].append(f"{filename}: 回执 {receipt_id} 登记失败：{exc}")
            continue
        if result.get("already"):
            counts["same"] += 1
        else:
            counts["new"] += 1
