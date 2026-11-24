"""全流程测试：持续导入、覆盖口径、联合规划、生命周期与端到端演示。

运行方式：
    python3 -m unittest discover -s tests -v
测试只使用临时目录与仓库自带的合成数据，不改动 data/。
"""
from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from road_scenario_collection_planning import cli, coverage, lifecycle, planning  # noqa: E402
from road_scenario_collection_planning.contract import DEFAULT_CONFIG  # noqa: E402
from road_scenario_collection_planning.ingest import ingest_dir  # noqa: E402
from road_scenario_collection_planning.store import Store  # noqa: E402
from road_scenario_collection_planning.synthetic import write_drop  # noqa: E402

DATA = ROOT / "data"
TODAY = "2026-11-24"


def write_csv(drop: Path, name: str, header: list[str], rows: list[list[str]]) -> None:
    drop.mkdir(parents=True, exist_ok=True)
    with (drop / name).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def fresh_store(tmp: str) -> Store:
    return Store.load(Path(tmp) / "state")


class IngestTests(unittest.TestCase):
    def test_base_ingest_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = fresh_store(tmp)
            report = ingest_dir(store, DATA)
            self.assertEqual(len(store.all("segments")), 35)
            self.assertEqual(len(store.all("labels")), 30)
            self.assertEqual(len(store.all("windows")), 24)
            self.assertEqual(len(store.all("tasks")), 19)
            self.assertEqual(report["batch_id"], "B-0001")
            self.assertFalse(report["errors"])

    def test_reingest_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = fresh_store(tmp)
            ingest_dir(store, DATA)
            store.save()
            report = ingest_dir(store, DATA)
            self.assertEqual(sorted(report["skipped_files"]),
                             ["collection_tasks.csv", "fleet_windows.csv",
                              "labels.csv", "segments.csv"])
            self.assertIsNone(report["batch_id"])
            self.assertEqual(len(store.data["batches"]), 1)

    def test_segment_update_creates_version_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = fresh_store(tmp)
            ingest_dir(store, DATA)
            drop = Path(tmp) / "drop2"
            changed = ["S-001", "T-000", "rural", "R-N", "dusk", "CFG-B", "0.4", "0.7"]
            write_csv(drop, "segments.csv",
                      ["segment_id", "trip_id", "road_class", "region", "visibility",
                       "sensor_config", "start_km", "end_km"], [changed])
            report = ingest_dir(store, drop)
            self.assertEqual(report["files"][0]["updated"], 1)
            current = store.get("segments", "S-001")
            self.assertEqual(current["version"], 2)
            self.assertEqual(current["region"], "R-N")
            history = store.history("segments", "S-001")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["region"], "R-S")  # 旧版本条件保留

    def test_bad_rows_are_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = fresh_store(tmp)
            drop = Path(tmp) / "drop"
            write_csv(drop, "labels.csv",
                      ["label_id", "segment_id", "scenario", "review_status", "annotator_batch"],
                      [["L-1", "S-1", "animal", "confirmed", "AB-1"],
                       ["L-2", "S-2", "not-a-scenario", "confirmed", "AB-1"]])
            report = ingest_dir(store, drop)
            self.assertEqual(report["files"][0]["new"], 1)
            self.assertEqual(report["files"][0]["errors"], 1)
            self.assertEqual(len(report["errors"]), 1)

    def test_local_terminal_task_status_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = fresh_store(tmp)
            ingest_dir(store, DATA)
            lifecycle.reconcile(store, TODAY)  # CT-004 等被撤销
            self.assertEqual(store.get("tasks", "CT-004")["status"], "revoked")
            # 旧批次重新导出（内容变化，含 accepted 旧状态）不应覆盖本地撤销
            drop = Path(tmp) / "restale"
            write_csv(drop, "collection_tasks.csv",
                      ["task_id", "window_id", "scenario", "priority", "status",
                       "accepted_at", "receipt_id"],
                      [["CT-004", "W-004", "pothole", "2", "accepted",
                        "2026-11-14T08:00:00Z", "RC-0"]])
            report = ingest_dir(store, drop)
            self.assertEqual(store.get("tasks", "CT-004")["status"], "revoked")
            self.assertTrue(any("保留本地状态" in w for w in report["warnings"]))


class CoverageTests(unittest.TestCase):
    def test_same_trip_counts_as_one_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = fresh_store(tmp)
            for seg_id in ("S-1", "S-2"):
                store.upsert("segments", seg_id, {
                    "segment_id": seg_id, "trip_id": "T-1", "road_class": "urban",
                    "region": "R-N", "visibility": "night", "sensor_config": "CFG-A",
                    "start_km": 0.0, "end_km": 0.3})
            for label_id, seg_id in (("L-1", "S-1"), ("L-2", "S-2")):
                store.upsert("labels", label_id, {
                    "label_id": label_id, "segment_id": seg_id, "scenario": "water",
                    "review_status": "confirmed", "annotator_batch": "AB-1"})
            exposures, _ = coverage.collect_evidence(store)
            # 同一行程的相邻片段不构成独立暴露
            self.assertEqual(coverage.exposure_count(exposures, ("water", "R-N", "night")), 1)

    def test_distinct_trips_count_separately_and_pending_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = fresh_store(tmp)
            for seg_id, trip in (("S-1", "T-1"), ("S-2", "T-2"), ("S-3", "T-3")):
                store.upsert("segments", seg_id, {
                    "segment_id": seg_id, "trip_id": trip, "road_class": "urban",
                    "region": "R-N", "visibility": "night", "sensor_config": "CFG-A",
                    "start_km": 0.0, "end_km": 0.3})
            rows = [("L-1", "S-1", "confirmed"), ("L-2", "S-2", "revised"),
                    ("L-3", "S-3", "pending")]
            for label_id, seg_id, status in rows:
                store.upsert("labels", label_id, {
                    "label_id": label_id, "segment_id": seg_id, "scenario": "water",
                    "review_status": status, "annotator_batch": "AB-1"})
            exposures, pending = coverage.collect_evidence(store)
            cell = ("water", "R-N", "night")
            self.assertEqual(coverage.exposure_count(exposures, cell), 2)
            self.assertEqual(pending[cell], 1)

    def test_review_changes_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = fresh_store(tmp)
            ingest_dir(store, DATA)
            before = {tuple(r["cell"]): r for r in coverage.gap_cells(store)}
            cell = ("animal", "R-S", "dusk")
            self.assertEqual(before[cell]["exposures"], 0)
            self.assertEqual(before[cell]["pending"], 5)
            drop = Path(tmp) / "review"
            write_csv(drop, "labels.csv",
                      ["label_id", "segment_id", "scenario", "review_status", "annotator_batch"],
                      [["L-001", "S-001", "animal", "confirmed", "AB-5"]])
            ingest_dir(store, drop)
            after = {tuple(r["cell"]): r for r in coverage.gap_cells(store)}
            self.assertEqual(after[cell]["exposures"], 1)
            self.assertEqual(after[cell]["pending"], 4)


class PlanningTests(unittest.TestCase):
    def _store_with_windows(self, tmp, windows, segments=(), labels=(), tasks=()):
        store = fresh_store(tmp)
        for row in windows:
            store.upsert("windows", row[0], dict(zip(
                ["window_id", "vehicle_id", "sensor_config", "region", "route_state",
                 "available_from", "available_to", "permit_id"], row)))
        for row in segments:
            store.upsert("segments", row[0], dict(zip(
                ["segment_id", "trip_id", "road_class", "region", "visibility",
                 "sensor_config", "start_km", "end_km"], row)))
        for row in labels:
            store.upsert("labels", row[0], dict(zip(
                ["label_id", "segment_id", "scenario", "review_status",
                 "annotator_batch"], row)))
        for row in tasks:
            store.upsert("tasks", row[0], dict(zip(
                ["task_id", "window_id", "scenario", "priority", "status",
                 "accepted_at", "receipt_id"], row)))
        return store

    def test_infeasible_reasons_closed_permit_sensor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_windows(tmp, [
                ["W-A", "V-1", "CFG-A", "R-N", "closed", "2026-12-01", "2026-12-02", "P-1"],
                ["W-B", "V-1", "CFG-A", "R-N", "urban-only", "2026-12-02", "2026-12-03", "P-0"],
                ["W-C", "V-1", "CFG-A", "R-N", "open", "2026-12-03", "2026-12-04", "P-EXPIRED"],
            ])
            evaluation = planning.evaluate_windows(store, TODAY)
            reasons = {i["window"]["window_id"]: i["reasons"]
                       for i in evaluation["infeasible"]}
            self.assertIn("道路封闭", reasons["W-A"])
            self.assertIn("缺少许可", reasons["W-B"])
            self.assertIn("许可已过期", reasons["W-C"])
            self.assertFalse(evaluation["feasible"])

    def test_sensor_constraint_blocks_water_on_basic_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_windows(tmp, [
                ["W-A", "V-1", "CFG-B", "R-Z", "open", "2026-12-01", "2026-12-02", "P-1"],
            ])
            plan = planning.build_plan(store, "安全优先", TODAY)
            water_gaps = [u for u in plan["uncovered"] if u["cell"][0] == "water"
                          and u["cell"][1] == "R-Z"]
            self.assertTrue(water_gaps)
            self.assertTrue(any("传感器" in r for r in water_gaps[0]["reasons"]))

    def test_same_vehicle_cannot_be_double_booked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_windows(tmp, [
                ["W-A", "V-1", "CFG-A", "R-N", "open", "2026-12-01", "2026-12-03", "P-1"],
                ["W-B", "V-1", "CFG-A", "R-S", "open", "2026-12-02", "2026-12-04", "P-1"],
            ])
            plan = planning.build_plan(store, "均衡", TODAY)
            self.assertEqual(len(plan["tasks"]), 1)  # 两个窗口同一车辆且时段重叠
            skipped = {s["window_id"]: s["reason"] for s in plan["skipped"]}
            other = "W-B" if plan["tasks"][0]["window_id"] == "W-A" else "W-A"
            self.assertIn("冲突", skipped[other])

    def test_one_task_hits_multiple_scenarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_windows(tmp, [
                ["W-A", "V-1", "CFG-A", "R-N", "open", "2026-12-01", "2026-12-02", "P-1"],
            ])
            plan = planning.build_plan(store, "均衡", TODAY)
            self.assertEqual(len(plan["tasks"]), 1)
            self.assertEqual(set(plan["tasks"][0]["scenarios"]),
                             {"sign", "animal", "debris", "chain", "pothole", "water"})
            self.assertEqual(len(plan["tasks"][0]["cells"]), 18)

    def test_duplicate_sampling_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 历史数据已覆盖 (sign, R-N, *) 之外全部空白，但 W-B 所在地区 R-S 的
            # 所有单元格已由历史覆盖 → W-B 属重复采样
            segments = [["S-1", "T-1", "urban", "R-S", "day", "CFG-A", "0.0", "0.3"],
                        ["S-2", "T-2", "urban", "R-S", "day", "CFG-A", "0.3", "0.6"],
                        ["S-3", "T-3", "urban", "R-S", "dusk", "CFG-A", "0.6", "0.9"],
                        ["S-4", "T-4", "urban", "R-S", "dusk", "CFG-A", "0.9", "1.2"],
                        ["S-5", "T-5", "urban", "R-S", "night", "CFG-A", "1.2", "1.5"],
                        ["S-6", "T-6", "urban", "R-S", "night", "CFG-A", "1.5", "1.8"]]
            labels = []
            scenarios = ["sign", "animal", "debris", "chain", "pothole", "water"]
            for i, seg in enumerate(segments):
                for j, scenario in enumerate(scenarios):
                    labels.append([f"L-{i}-{j}", seg[0], scenario, "confirmed", "AB-1"])
            store = self._store_with_windows(
                tmp,
                [["W-B", "V-1", "CFG-A", "R-S", "open", "2026-12-01", "2026-12-02", "P-1"]],
                segments=segments, labels=labels)
            plan = planning.build_plan(store, "均衡", TODAY)
            self.assertFalse(plan["tasks"])
            self.assertEqual(plan["skipped"][0]["reason"], "重复采样：历史数据已覆盖")

    def test_profiles_change_selection_under_contention(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 同一车辆同一时段只能二选一：CFG-A 窗口服务深积水，CFG-B 窗口服务交通标志
            store = self._store_with_windows(tmp, [
                ["W-WATER", "V-1", "CFG-A", "R-N", "open", "2026-12-01", "2026-12-02", "P-1"],
                ["W-SIGN", "V-1", "CFG-B", "R-S", "open", "2026-12-01", "2026-12-02", "P-1"],
            ])
            # 只留两个缺口单元格：深积水×R-N×白天、交通标志×R-S×白天
            segments, labels = [], []
            cells_to_fill = [(s, r, v) for s in DEFAULT_CONFIG["scenario_requirements"]
                             for r in ("R-N", "R-S") for v in ("day", "dusk", "night")]
            cells_to_fill.remove(("water", "R-N", "day"))
            cells_to_fill.remove(("sign", "R-S", "day"))
            for i, (scenario, region, vis) in enumerate(sorted(cells_to_fill)):
                for trip in (1, 2):
                    seg_id = f"S-{i}-{trip}"
                    segments.append([seg_id, f"T-{i}-{trip}", "urban", region, vis,
                                     "CFG-A", 0.0, 0.3])
                    labels.append([f"L-{i}-{trip}", seg_id, scenario, "confirmed", "AB-1"])
            windows = [
                ["W-WATER", "V-1", "CFG-A", "R-N", "open", "2026-12-01", "2026-12-02", "P-1"],
                ["W-SIGN", "V-1", "CFG-B", "R-S", "open", "2026-12-01", "2026-12-02", "P-1"],
            ]
            store = self._store_with_windows(tmp, windows, segments=segments, labels=labels)
            safety = planning.build_plan(store, "安全优先", TODAY, register=False)
            sign_first = planning.build_plan(store, "设施优先", TODAY, register=False)
            self.assertEqual(safety["tasks"][0]["window_id"], "W-WATER")
            self.assertEqual(sign_first["tasks"][0]["window_id"], "W-SIGN")
            results = planning.compare_profiles(store, ["安全优先", "设施优先"], TODAY)
            self.assertEqual(results[0]["per_scenario"]["water"], 1)
            self.assertEqual(results[1]["per_scenario"]["sign"], 1)


class LifecycleTests(unittest.TestCase):
    def _base_store(self, tmp):
        store = fresh_store(tmp)
        ingest_dir(store, DATA)
        return store

    def test_accept_occupies_and_rejects_second_accept(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._base_store(tmp)
            plan = planning.build_plan(store, "均衡", TODAY)
            result = lifecycle.accept_plan(store, plan["plan_id"], TODAY)
            self.assertTrue(result["ok"])
            for task_id in result["accepted"]:
                self.assertEqual(store.get("tasks", task_id)["status"], "accepted")
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.accept_plan(store, plan["plan_id"], TODAY)

    def test_accept_conflict_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._base_store(tmp)
            # 先接受一个计划占用 W-021/W-022
            plan = planning.build_plan(store, "均衡", TODAY)
            lifecycle.accept_plan(store, plan["plan_id"], TODAY)
            # 构造与已接受任务同时段同车辆的草稿任务并强行接受
            store.upsert("windows", "W-900", {
                "window_id": "W-900", "vehicle_id": "V-01", "sensor_config": "CFG-A",
                "region": "R-N", "route_state": "open",
                "available_from": "2026-12-03", "available_to": "2026-12-05",
                "permit_id": "P-2"})
            store.plans["PLAN-X"] = {
                "plan_id": "PLAN-X", "status": "draft", "profile": "均衡",
                "tasks": [{"task_id": "PLAN-X-T01", "window_id": "W-900",
                           "vehicle_id": "V-01", "region": "R-N",
                           "sensor_config": "CFG-A",
                           "available_from": "2026-12-03", "available_to": "2026-12-05",
                           "scenarios": ["sign"], "cells": [["sign", "R-N", "day"]],
                           "gain": 1.0, "kind": "新增覆盖"}],
            }
            result = lifecycle.accept_plan(store, "PLAN-X", TODAY)
            self.assertFalse(result["ok"])
            self.assertIn("PLAN-X-T01", result["conflicts"])

    def test_reconcile_revokes_only_adjustable_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._base_store(tmp)
            report = lifecycle.reconcile(store, TODAY)
            revoked = {r["task_id"]: r["reason"] for r in report["revoked"]}
            self.assertEqual(revoked["CT-004"], "缺少许可")
            self.assertEqual(revoked["CT-016"], "缺少许可")
            self.assertEqual(revoked["CT-009"], "许可已过期")
            self.assertEqual(revoked["CT-012"], "缺少许可")
            self.assertEqual(revoked["CT-018"], "许可已过期")
            # 小动物任务挂在 CFG-B（无红外）窗口上，传感器不满足场景要求
            for task_id in ("CT-001", "CT-007", "CT-013", "CT-019"):
                self.assertIn("传感器配置CFG-B", revoked[task_id])
            # 已完成任务不受影响
            for task_id in ("CT-002", "CT-005", "CT-008", "CT-011", "CT-014", "CT-017"):
                self.assertEqual(store.get("tasks", task_id)["status"], "done")
            self.assertTrue(any(k["task_id"] == "CT-002" for k in report["kept_done"]))
            # 合规任务保持原状（链条可用 CFG-B，窗口开放且许可有效）
            self.assertEqual(store.get("tasks", "CT-003")["status"], "proposed")
            self.assertEqual(store.get("tasks", "CT-006")["status"], "proposed")
            self.assertEqual(store.get("tasks", "CT-015")["status"], "proposed")

    def test_reconcile_explains_exposed_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._base_store(tmp)
            report = lifecycle.reconcile(store, TODAY)
            cells = {tuple(g["cell"]) for g in report["exposed_gaps"]}
            # CT-004/CT-016 承诺的 坑洼×R-S 覆盖因撤销变成缺口
            self.assertIn(("pothole", "R-S", "day"), cells)
            self.assertIn(("pothole", "R-S", "night"), cells)

    def test_receipt_marks_done_and_records_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._base_store(tmp)
            drop = write_drop(Path(tmp) / "drops")
            ingest_dir(store, drop)
            task = store.get("tasks", "CT-001")
            self.assertEqual(task["status"], "done")
            self.assertEqual(task["receipt_id"], "RC-101")
            receipt = store.receipts["RC-101"]
            self.assertEqual(receipt["segment_ids"], ["S-101", "S-102"])
            self.assertEqual(receipt["segment_versions"], {"S-101": 1, "S-102": 1})
            # 片段后续更新不影响回执记录的当时版本
            store.upsert("segments", "S-101", {
                **store.get("segments", "S-101"), "region": "R-N"})
            self.assertEqual(store.receipts["RC-101"]["segment_versions"]["S-101"], 1)

    def test_receipt_reports_actual_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._base_store(tmp)
            ingest_dir(store, write_drop(Path(tmp) / "drops"))
            # CT-010 目标坑洼，S-104 同时命中坑洼（计划内）与深积水（计划外）
            receipt = store.receipts["RC-103"]
            self.assertEqual(receipt["task_id"], "CT-010")
            result = lifecycle.register_receipt(store, "CT-010", ["S-104"], "2026-12-12",
                                                receipt_id="RC-103")
            self.assertTrue(result["already"])

    def test_board_lists_conflicts_after_window_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._base_store(tmp)
            # 把 W-007 的时段改到与 V-00 另一已接受窗口重叠
            store.upsert("windows", "W-021", {
                "window_id": "W-021", "vehicle_id": "V-00", "sensor_config": "CFG-B",
                "region": "R-N", "route_state": "open",
                "available_from": "2026-12-08", "available_to": "2026-12-10",
                "permit_id": "P-1"})
            store.upsert("tasks", "CT-900", {
                "task_id": "CT-900", "window_id": "W-021", "scenario": "sign",
                "priority": 1, "status": "accepted",
                "accepted_at": "2026-11-20T08:00:00Z", "receipt_id": ""})
            board = lifecycle.board(store, TODAY)
            pairs = [set(c["tasks"]) for c in board["conflicts"]]
            self.assertIn({"CT-007", "CT-900"}, pairs)


class EndToEndTests(unittest.TestCase):
    def test_full_walkthrough_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "state")

            def run(*argv):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(["--state", state, "--today", TODAY, *argv])
                self.assertEqual(code, 0, out.getvalue())
                return out.getvalue()

            run("ingest", str(DATA))
            gaps_before = run("gaps")
            self.assertIn("小动物", gaps_before)
            plan_text = run("plan", "--profile", "均衡")
            plan_id = plan_text.split("采集计划 ")[1].split("（")[0]
            run("accept", plan_id)
            drop = write_drop(Path(tmp) / "drops")
            run("ingest", str(drop))
            reconcile_text = run("reconcile")
            self.assertIn("道路封闭", reconcile_text)
            self.assertIn("传感器配置CFG-B不再满足场景", reconcile_text)
            board_text = run("board")
            self.assertIn("已撤销：7 个", board_text)
            gaps_after = run("gaps")
            # 复核与新片段使小动物覆盖改善：有效暴露 0 → 10
            self.assertIn("10", gaps_after.split("小动物")[1].split("\n")[0])
            compare_text = run("compare")
            self.assertIn("加权得分", compare_text)
            # 重复导入不改变状态
            again = run("ingest", str(drop))
            self.assertIn("内容未变化", again)


if __name__ == "__main__":
    unittest.main()
