"""状态仓库：把持续导入的资料保存为带版本的本地 JSON 存储。

每次导入对实体做"按键 upsert"：内容有变化时旧记录进入 history 并升版本，
因此已采集片段当时的版本与条件始终可查。整个仓库只有一个 store.json，
写盘采用临时文件 + 原子替换。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .contract import DEFAULT_CONFIG

STORE_FILE = "store.json"

ENTITY_KINDS = ["segments", "labels", "windows", "tasks", "permits"]


def _empty_state() -> dict:
    return {
        "schema": 1,
        "config": copy.deepcopy(DEFAULT_CONFIG),
        "entities": {kind: {} for kind in ENTITY_KINDS},
        "receipts": {},
        "plans": {},
        "batches": [],
        "file_hashes": {},
    }


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / STORE_FILE
        self.data = _empty_state()

    @classmethod
    def load(cls, root: str | Path) -> "Store":
        store = cls(root)
        if store.path.exists():
            with store.path.open(encoding="utf-8") as stream:
                store.data = json.load(stream)
        return store

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    # ---- 配置 ----

    @property
    def config(self) -> dict:
        return self.data["config"]

    # ---- 实体（带版本） ----

    def get(self, kind: str, key: str) -> dict | None:
        entry = self.data["entities"][kind].get(key)
        return entry["current"] if entry else None

    def all(self, kind: str) -> dict[str, dict]:
        return {k: e["current"] for k, e in self.data["entities"][kind].items()}

    def history(self, kind: str, key: str) -> list[dict]:
        entry = self.data["entities"][kind].get(key)
        return list(entry["history"]) if entry else []

    def upsert(self, kind: str, key: str, record: dict) -> str:
        """按键写入；返回 new / updated / same。内容变化时旧版本进入 history。"""
        entries = self.data["entities"][kind]
        entry = entries.get(key)
        if entry is None:
            entries[key] = {"current": {**record, "version": 1}, "history": []}
            return "new"
        current = entry["current"]
        if {k: v for k, v in current.items() if k != "version"} == record:
            return "same"
        entry["history"].append(current)
        entry["current"] = {**record, "version": current["version"] + 1}
        return "updated"

    # ---- 回执 / 计划 / 批次 ----

    @property
    def receipts(self) -> dict:
        return self.data["receipts"]

    @property
    def plans(self) -> dict:
        return self.data["plans"]

    def seen_hash(self, digest: str) -> bool:
        return digest in self.data["file_hashes"]

    def add_batch(self, batch: dict) -> None:
        self.data["batches"].append(batch)
        for digest in batch["file_hashes"]:
            self.data["file_hashes"][digest] = batch["batch_id"]
