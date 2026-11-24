"""资料契约：CSV 文件结构、枚举取值、默认配置与行级校验。

本模块只描述"输入资料长什么样"以及默认的业务参数，不包含分析逻辑。
所有记录均为本地合成数据，字段含义见 docs/资料契约.md。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

# ---- 场景与枚举 ------------------------------------------------------------

SCENARIOS = ["sign", "animal", "debris", "chain", "pothole", "water"]
SCENARIO_ZH = {
    "sign": "交通标志",
    "animal": "小动物",
    "debris": "散落物",
    "chain": "链条",
    "pothole": "坑洼",
    "water": "深积水",
}

VISIBILITIES = ["day", "dusk", "night"]
VISIBILITY_ZH = {"day": "白天", "dusk": "黄昏", "night": "夜间"}

ROUTE_STATES = ["open", "urban-only", "closed"]
ROUTE_STATE_ZH = {"open": "开放", "urban-only": "仅市区", "closed": "封闭"}

REVIEW_STATUSES = ["pending", "confirmed", "revised"]
REVIEW_STATUS_ZH = {"pending": "待复核", "confirmed": "已确认", "revised": "已修订"}
#: 复核后视为有效证据的标注状态；pending 只作为"待复核"提示，不计入覆盖。
EVIDENCE_REVIEW_STATUSES = {"confirmed", "revised"}

TASK_STATUSES = ["proposed", "accepted", "done", "revoked"]
TASK_STATUS_ZH = {
    "proposed": "待确认",
    "accepted": "已接受",
    "done": "已完成",
    "revoked": "已撤销",
}
#: 仍可调整（可被撤销）的任务状态
ADJUSTABLE_TASK_STATUSES = {"proposed", "accepted"}
#: 会占用车辆与时段的任务状态
OCCUPYING_TASK_STATUSES = {"accepted", "done"}

# ---- 文件结构 --------------------------------------------------------------

FILE_SCHEMAS = {
    "segments.csv": [
        "segment_id", "trip_id", "road_class", "region", "visibility",
        "sensor_config", "start_km", "end_km",
    ],
    "labels.csv": ["label_id", "segment_id", "scenario", "review_status", "annotator_batch"],
    "fleet_windows.csv": [
        "window_id", "vehicle_id", "sensor_config", "region", "route_state",
        "available_from", "available_to", "permit_id",
    ],
    "collection_tasks.csv": [
        "task_id", "window_id", "scenario", "priority", "status",
        "accepted_at", "receipt_id",
    ],
    # 可选文件：许可有效期登记（缺省时仅按 permit_id 字面判断）
    "permits.csv": ["permit_id", "valid_from", "valid_to"],
    # 可选文件：采集回执（一行一个片段，同一 receipt_id 可多行）
    "receipts.csv": ["receipt_id", "task_id", "segment_id", "collected_at"],
}

#: 导入处理顺序：被引用的记录先导入
INGEST_ORDER = [
    "segments.csv", "labels.csv", "permits.csv",
    "fleet_windows.csv", "collection_tasks.csv", "receipts.csv",
]

#: 文件 → (实体类别, 主键字段)；receipts.csv 单独处理
FILE_ENTITY = {
    "segments.csv": ("segments", "segment_id"),
    "labels.csv": ("labels", "label_id"),
    "fleet_windows.csv": ("windows", "window_id"),
    "collection_tasks.csv": ("tasks", "task_id"),
    "permits.csv": ("permits", "permit_id"),
}

# ---- 默认配置 --------------------------------------------------------------

DEFAULT_CONFIG = {
    # 每个 场景×地区×时段 单元格视为"已覆盖"所需的独立暴露次数
    "min_exposures_per_cell": 2,
    # 传感器配置 → 传感器清单
    "sensor_configs": {
        "CFG-A": ["camera", "lidar", "thermal", "sonar"],
        "CFG-B": ["camera", "lidar"],
    },
    # 场景 → 所需传感器
    "scenario_requirements": {
        "sign": ["camera"],
        "animal": ["camera", "thermal"],
        "debris": ["camera", "lidar"],
        "chain": ["camera", "lidar"],
        "pothole": ["camera", "lidar"],
        "water": ["camera", "sonar"],
    },
    # 需要有效许可的路线状态（closed 一律不可执行）
    "permit_required_route_states": ["open", "urban-only"],
    # 视为"未办理许可"的 permit_id
    "no_permit_ids": ["", "P-0"],
    # 视为"已过期"的 permit_id（permits.csv 登记的有效期优先于该名单）
    "expired_permit_ids": ["P-EXPIRED"],
    # 优先级方案：场景 → 权重；未列出的场景权重为 0（不参与该方案）
    "priority_profiles": {
        "均衡": {s: 1.0 for s in SCENARIOS},
        "安全优先": {
            "water": 3.0, "pothole": 2.0, "debris": 2.0,
            "chain": 1.0, "animal": 1.0, "sign": 1.0,
        },
        "设施优先": {
            "sign": 3.0, "chain": 2.0, "debris": 2.0,
            "pothole": 1.0, "water": 1.0, "animal": 1.0,
        },
    },
}


class ContractError(ValueError):
    """行内容不符合资料契约。"""


# ---- 行级校验 --------------------------------------------------------------


def _need(row: dict, field: str, where: str) -> str:
    value = (row.get(field) or "").strip()
    if not value:
        raise ContractError(f"{where}: 字段 {field} 为空")
    return value


def parse_date(text: str, where: str) -> str:
    try:
        return date.fromisoformat(text.strip()).isoformat()
    except ValueError as exc:
        raise ContractError(f"{where}: 日期格式无效 {text!r}") from exc


def parse_datetime_text(text: str, where: str) -> str:
    raw = text.strip()
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{where}: 时间格式无效 {text!r}") from exc
    return raw


def _parse_km(row: dict, field: str, where: str) -> float:
    raw = _need(row, field, where)
    try:
        return float(raw)
    except ValueError as exc:
        raise ContractError(f"{where}: 里程不是数值 {raw!r}") from exc


def validate_row(filename: str, row: dict, lineno: int) -> dict:
    """把一行 CSV 记录校验并归一化；不合法时抛出 ContractError。"""
    where = f"第{lineno}行"
    for field in FILE_SCHEMAS[filename]:
        if field not in row or row[field] is None:
            raise ContractError(f"{where}: 缺少字段 {field}")
    row = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}

    if filename == "segments.csv":
        out = {
            "segment_id": _need(row, "segment_id", where),
            "trip_id": _need(row, "trip_id", where),
            "road_class": _need(row, "road_class", where),
            "region": _need(row, "region", where),
            "visibility": _need(row, "visibility", where),
            "sensor_config": _need(row, "sensor_config", where),
            "start_km": _parse_km(row, "start_km", where),
            "end_km": _parse_km(row, "end_km", where),
        }
        if out["visibility"] not in VISIBILITIES:
            raise ContractError(f"{where}: 未知时段 {out['visibility']!r}")
        if out["end_km"] <= out["start_km"]:
            raise ContractError(f"{where}: end_km 必须大于 start_km")
        return out

    if filename == "labels.csv":
        out = {
            "label_id": _need(row, "label_id", where),
            "segment_id": _need(row, "segment_id", where),
            "scenario": _need(row, "scenario", where),
            "review_status": _need(row, "review_status", where),
            "annotator_batch": _need(row, "annotator_batch", where),
        }
        if out["scenario"] not in SCENARIOS:
            raise ContractError(f"{where}: 未知场景 {out['scenario']!r}")
        if out["review_status"] not in REVIEW_STATUSES:
            raise ContractError(f"{where}: 未知复核状态 {out['review_status']!r}")
        return out

    if filename == "fleet_windows.csv":
        out = {
            "window_id": _need(row, "window_id", where),
            "vehicle_id": _need(row, "vehicle_id", where),
            "sensor_config": _need(row, "sensor_config", where),
            "region": _need(row, "region", where),
            "route_state": _need(row, "route_state", where),
            "available_from": parse_date(_need(row, "available_from", where), where),
            "available_to": parse_date(_need(row, "available_to", where), where),
            "permit_id": _need(row, "permit_id", where),
        }
        if out["route_state"] not in ROUTE_STATES:
            raise ContractError(f"{where}: 未知路线状态 {out['route_state']!r}")
        if not out["available_from"] < out["available_to"]:
            raise ContractError(f"{where}: available_to 必须晚于 available_from（时段为左闭右开）")
        return out

    if filename == "collection_tasks.csv":
        out = {
            "task_id": _need(row, "task_id", where),
            "window_id": _need(row, "window_id", where),
            "scenario": _need(row, "scenario", where),
            "priority": 0,
            "status": _need(row, "status", where),
            "accepted_at": parse_datetime_text(_need(row, "accepted_at", where), where),
            "receipt_id": _need(row, "receipt_id", where),
        }
        if out["scenario"] not in SCENARIOS:
            raise ContractError(f"{where}: 未知场景 {out['scenario']!r}")
        if out["status"] not in TASK_STATUSES:
            raise ContractError(f"{where}: 未知任务状态 {out['status']!r}")
        try:
            out["priority"] = int(row["priority"])
        except ValueError as exc:
            raise ContractError(f"{where}: priority 不是整数 {row['priority']!r}") from exc
        return out

    if filename == "permits.csv":
        out = {
            "permit_id": _need(row, "permit_id", where),
            "valid_from": parse_date(_need(row, "valid_from", where), where),
            "valid_to": parse_date(_need(row, "valid_to", where), where),
        }
        if not out["valid_from"] < out["valid_to"]:
            raise ContractError(f"{where}: valid_to 必须晚于 valid_from")
        return out

    if filename == "receipts.csv":
        return {
            "receipt_id": _need(row, "receipt_id", where),
            "task_id": _need(row, "task_id", where),
            "segment_id": _need(row, "segment_id", where),
            "collected_at": parse_date(_need(row, "collected_at", where), where),
        }

    raise ContractError(f"未知文件 {filename}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
