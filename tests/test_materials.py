from __future__ import annotations

import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MaterialTests(unittest.TestCase):
    def test_files_have_declared_headers_and_rows(self):
        expected = EXPECTED
        for filename, (columns, minimum_rows) in expected.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertGreaterEqual(len(rows), minimum_rows)
                self.assertTrue(all(None not in row for row in rows))


EXPECTED = {'segments.csv': (['segment_id', 'trip_id', 'road_class', 'region', 'weather', 'sensor_config', 'start_km', 'end_km'], 35), 'labels.csv': (['label_id', 'segment_id', 'scenario', 'review_status', 'annotator_batch'], 30), 'fleet_windows.csv': (['window_id', 'vehicle_id', 'sensor_config', 'region', 'weather_window', 'available_from', 'available_to', 'permit_id'], 24), 'collection_tasks.csv': (['task_id', 'window_id', 'scenario', 'priority', 'status', 'accepted_at', 'receipt_id'], 19)}


if __name__ == "__main__":
    unittest.main()
