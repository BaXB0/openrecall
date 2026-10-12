import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import openrecall.config as cfg  # noqa: E402
import openrecall.database as db  # noqa: E402
from openrecall.prune import prune_if_due, prune_old  # noqa: E402


class TestPrune(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "appdata": cfg.appdata_folder,
            "screens": cfg.screenshots_path,
            "summaries": cfg.summaries_path,
            "db": db.db_path,
        }
        self.tmp = tempfile.mkdtemp()
        self.screens = os.path.join(self.tmp, "screenshots")
        self.sums = os.path.join(self.tmp, "summaries")
        os.makedirs(self.screens)
        os.makedirs(self.sums)
        self.db_file = os.path.join(self.tmp, "recall.db")
        cfg.appdata_folder = self.tmp
        cfg.screenshots_path = self.screens
        cfg.summaries_path = self.sums
        db.db_path = self.db_file
        db.create_db()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        cfg.appdata_folder = self._orig["appdata"]
        cfg.screenshots_path = self._orig["screens"]
        cfg.summaries_path = self._orig["summaries"]
        db.db_path = self._orig["db"]

    def _add(self, day, hour=12, monitor=0, filename=None):
        ts = int(datetime.datetime(day.year, day.month, day.day, hour).timestamp())
        fn = filename or f"{ts}_{monitor}.webp"
        with open(os.path.join(self.screens, fn), "wb") as f:
            f.write(b"\x00")
        db.insert_entry(
            "txt", ts, np.array([0.1] * 5, dtype=np.float32),
            "App", "Title", filename=fn, monitor=monitor,
        )
        return ts

    def _vision(self, day, described_ts):
        path = os.path.join(self.sums, f"{day}_vision.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {str(t): {"when": "12:00", "description": "scene"} for t in described_ts},
                f,
            )

    def _summary(self, day):
        with open(os.path.join(self.sums, f"{day}.md"), "w", encoding="utf-8") as f:
            f.write("# summary\n")

    def test_described_kept_undescribed_removed(self):
        day = "2026-01-01"
        keep_ts = self._add(datetime.datetime(2026, 1, 1), hour=10)
        drop_ts = self._add(datetime.datetime(2026, 1, 1), hour=11)
        self._vision(day, {keep_ts})
        self._summary(day)

        result = prune_old(older_than_days=7)

        self.assertEqual(result["days_pruned"], 1)
        self.assertEqual(result["moments_removed"], 1)
        self.assertEqual(result["files_removed"], 1)
        # Described moment fully kept.
        self.assertTrue(os.path.exists(os.path.join(self.screens, f"{keep_ts}_0.webp")))
        remaining = db.get_entries_for_day(day)
        self.assertEqual([e.timestamp for e in remaining], [keep_ts])
        # Undescribed moment removed (file + row).
        self.assertFalse(os.path.exists(os.path.join(self.screens, f"{drop_ts}_0.webp")))

    def test_day_not_run_through_ai_is_kept(self):
        day = "2026-01-02"
        ts = self._add(datetime.datetime(2026, 1, 2), hour=10)
        # No vision.json and no .md -> AI never ran for this day.

        result = prune_old(older_than_days=7)

        self.assertEqual(result["skipped_unsummarized"], 1)
        self.assertEqual(result["moments_removed"], 0)
        self.assertTrue(os.path.exists(os.path.join(self.screens, f"{ts}_0.webp")))
        self.assertEqual(len(db.get_entries_for_day(day)), 1)

    def test_vision_json_alone_counts_as_ai_run(self):
        day = "2026-01-06"
        drop_ts = self._add(datetime.datetime(2026, 1, 6), hour=10)
        self._vision(day, set())  # vision ran, writer never finished -> still pruned
        self.assertFalse(os.path.exists(os.path.join(self.sums, f"{day}.md")))

        result = prune_old(older_than_days=7)

        self.assertEqual(result["moments_removed"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.screens, f"{drop_ts}_0.webp")))

    def test_recent_days_untouched(self):
        today = datetime.date.today()
        ts = self._add(datetime.datetime(today.year, today.month, today.day), hour=12)

        result = prune_old(older_than_days=7)

        self.assertEqual(result["moments_removed"], 0)
        self.assertTrue(os.path.exists(os.path.join(self.screens, f"{ts}_0.webp")))

    def test_dry_run_removes_nothing(self):
        day = "2026-01-03"
        ts = self._add(datetime.datetime(2026, 1, 3), hour=10)
        self._vision(day, set())

        result = prune_old(older_than_days=7, dry_run=True)

        self.assertEqual(result["moments_removed"], 0)
        self.assertEqual(result["files_removed"], 0)
        self.assertTrue(os.path.exists(os.path.join(self.screens, f"{ts}_0.webp")))
        self.assertEqual(len(db.get_entries_for_day(day)), 1)

    def test_prune_if_due_stamps_once_per_day(self):
        day = "2026-01-04"
        self._add(datetime.datetime(2026, 1, 4), hour=10)
        self._vision(day, set())

        self.assertTrue(prune_if_due(older_than_days=7, force=True))
        # Same day again -> stamp prevents a second prune.
        self.assertFalse(prune_if_due(older_than_days=7))

    def test_shared_file_backing_described_moment_is_kept(self):
        # The capture loop reuses one file for an unchanged screen. Here monitor
        # 0's screen is first captured at 09:00 (undescribed moment), then stays
        # unchanged through 10:00 (described) and 11:00 (undescribed) -- all
        # pointing at the SAME file. Pruning 09:00/11:00 must NOT delete a file
        # that the described 10:00 moment still references.
        day = "2026-01-05"
        self._add(datetime.datetime(2026, 1, 5), hour=9, monitor=0, filename="first_0.webp")
        keep_ts = self._add(datetime.datetime(2026, 1, 5), hour=10, monitor=0,
                            filename="first_0.webp")
        self._add(datetime.datetime(2026, 1, 5), hour=10, monitor=1, filename="keep_10_1.webp")
        self._add(datetime.datetime(2026, 1, 5), hour=11, monitor=0, filename="first_0.webp")
        self._add(datetime.datetime(2026, 1, 5), hour=11, monitor=1, filename="drop_11_1.webp")
        self._vision(day, {keep_ts})
        self._summary(day)

        result = prune_old(older_than_days=7)

        # 2 undescribed moments removed; the shared file and the described
        # moment's own monitor-1 image are kept, only drop_11_1.webp goes.
        self.assertEqual(result["moments_removed"], 2)
        self.assertEqual(result["files_removed"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.screens, "first_0.webp")))
        self.assertTrue(os.path.exists(os.path.join(self.screens, "keep_10_1.webp")))
        self.assertFalse(os.path.exists(os.path.join(self.screens, "drop_11_1.webp")))
        remaining = db.get_entries_for_day(day)
        self.assertEqual({e.timestamp for e in remaining}, {keep_ts})


if __name__ == "__main__":
    unittest.main()