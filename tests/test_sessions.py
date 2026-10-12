import unittest
import os
import datetime

# Temporarily adjust path to import from openrecall
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from openrecall.sessions import split_sessions, session_by_key, covering_session


def ts(y, mo, d, h, mi=0):
    """Local timestamp for a calendar moment."""
    return int(datetime.datetime(y, mo, d, h, mi).timestamp())


class TestSessions(unittest.TestCase):

    def test_single_continuous_run(self):
        run = [ts(2026, 9, 13, 10), ts(2026, 9, 13, 11), ts(2026, 9, 13, 12)]
        s = split_sessions(run)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0].key, "2026-09-13")
        self.assertEqual(s[0].start, run[0])
        self.assertEqual(s[0].end, run[-1])
        self.assertEqual(tuple(s[0].timestamps), tuple(run))

    def test_gap_over_2h_splits(self):
        # 13:00/13:30 then 16:00/16:30 (2.5h gap) -> two sessions same day.
        s = split_sessions([ts(2026, 9, 13, 13, 0), ts(2026, 9, 13, 13, 30),
                            ts(2026, 9, 13, 16, 0), ts(2026, 9, 13, 16, 30)])
        self.assertEqual([x.key for x in s], ["2026-09-13", "2026-09-13_2"])

    def test_gap_under_2h_keeps_session(self):
        # 13:00 then 14:30 (1.5h) -> one session.
        s = split_sessions([ts(2026, 9, 13, 13, 0), ts(2026, 9, 13, 14, 30)])
        self.assertEqual(len(s), 1)

    def test_midnight_spanning_session_belongs_to_start_day(self):
        # 18:00 on the 13th through 06:00 on the 14th, sub-2h snapshots:
        # a single "day" keyed by the START date.
        run = [ts(2026, 9, 13, 18, 0), ts(2026, 9, 13, 19, 30),
               ts(2026, 9, 13, 21, 0), ts(2026, 9, 13, 22, 30),
               ts(2026, 9, 14, 0, 30), ts(2026, 9, 14, 2, 0),
               ts(2026, 9, 14, 4, 0), ts(2026, 9, 14, 6, 0)]
        s = split_sessions(run)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0].key, "2026-09-13")
        self.assertEqual(s[0].start, run[0])
        self.assertEqual(s[0].end, run[-1])

    def test_multi_session_same_calendar_day(self):
        # Morning check (10:00-11:00), a 3h+ gap, then an hourly afternoon+night
        # run that starts on the same date and crosses midnight.
        run2 = [ts(2026, 9, 14, h) for h in range(14, 24)]
        run2 += [ts(2026, 9, 15, 0), ts(2026, 9, 15, 1)]
        run = [ts(2026, 9, 14, 10, 0), ts(2026, 9, 14, 10, 30),
               ts(2026, 9, 14, 11, 0)] + run2
        s = split_sessions(run)
        self.assertEqual([x.key for x in s], ["2026-09-14", "2026-09-14_2"])
        self.assertEqual(s[0].end, ts(2026, 9, 14, 11, 0))
        self.assertEqual(s[1].start, ts(2026, 9, 14, 14, 0))
        self.assertEqual(s[1].end, ts(2026, 9, 15, 1, 0))

    def test_micro_run_dropped(self):
        # A lone timestamp at 23:58 is not a session; the next real block the
        # following morning IS (8h+ gap) keyed to the new start date.
        run = [ts(2026, 9, 13, 23, 58), ts(2026, 9, 14, 8, 0), ts(2026, 9, 14, 9, 0)]
        s = split_sessions(run)
        self.assertEqual([x.key for x in s], ["2026-09-14"])

    def test_min_len_respected(self):
        run = [ts(2026, 9, 13, 12, 0), ts(2026, 9, 13, 12, 5)]
        self.assertEqual(split_sessions(run), [])  # 5 min < 10 min default
        self.assertEqual(len(split_sessions(run, min_len_minutes=3)), 1)

    def test_gap_hours_override(self):
        # 2.5h gap: split with the 2h default, single session with a 3h bar.
        run = [ts(2026, 9, 13, 13, 0), ts(2026, 9, 13, 13, 30),
               ts(2026, 9, 13, 16, 0), ts(2026, 9, 13, 16, 30)]
        self.assertEqual(len(split_sessions(run)), 2)                     # default 2h
        self.assertEqual(len(split_sessions(run, gap_hours=3.0)), 1)      # 2.5h < 3h

    def test_session_by_key(self):
        s = split_sessions([ts(2026, 9, 13, 10, 0), ts(2026, 9, 13, 11, 0),
                            ts(2026, 9, 13, 15, 0), ts(2026, 9, 13, 16, 0)])
        self.assertIsNotNone(session_by_key(s, "2026-09-13"))
        self.assertIsNotNone(session_by_key(s, "2026-09-13_2"))
        self.assertIsNone(session_by_key(s, "2026-09-13_3"))

    def test_covering_session(self):
        # One session spans 18:00 on the 13th -> 05:00 on the 14th, and a second
        # session starts on the 14th at 13:00.
        tail = [ts(2026, 9, 13, 18, 0), ts(2026, 9, 13, 19, 30),
                ts(2026, 9, 13, 21, 0), ts(2026, 9, 13, 22, 30),
                ts(2026, 9, 14, 0, 30), ts(2026, 9, 14, 2, 0),
                ts(2026, 9, 14, 4, 0), ts(2026, 9, 14, 5, 0)]
        own = [ts(2026, 9, 14, 13, 0), ts(2026, 9, 14, 14, 0), ts(2026, 9, 14, 15, 0)]
        s = split_sessions(tail + own)
        # A cell that a session STARTED on opens that session.
        self.assertEqual(covering_session(s, "2026-09-13"), "2026-09-13")
        self.assertEqual(covering_session(s, "2026-09-14"), "2026-09-14")
        # A pure tail day (no session of its own) maps to the covering session.
        s2 = split_sessions(tail)
        self.assertEqual(covering_session(s2, "2026-09-14"), "2026-09-13")
        self.assertEqual(covering_session([], "2026-09-14"), "2026-09-14")


if __name__ == "__main__":
    unittest.main()