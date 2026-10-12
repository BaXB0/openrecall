"""Session-based day segmentation for OpenRecall.

A "session" is a run of captured moments with no gap between consecutive
timestamps larger than ``SESSION_GAP_HOURS`` (default 2h). Each session is
treated as one self-contained "day" in the web UI, summaries and pruning, and
it belongs to the LOCAL calendar date it started on: a session that runs
18:00 -> 06:00 is e.g. keyed ``2026-09-13`` and its ``.md`` summary is stored
under that start date. If more than one session starts on the same calendar
date they get ``2026-09-13``, ``2026-09-13_2``, ... in start order.

Session splitting is independent of pruning: pruning keeps its own age cutoff
and simply iterates sessions instead of calendar days.
"""
import datetime
from collections import namedtuple
from typing import Iterable, List, Optional, Tuple

from openrecall.config import SESSION_GAP_HOURS, SESSION_MIN_LEN_MINUTES

Session = namedtuple("Session", ["key", "start", "end", "timestamps"])
"""Session key (start-date based), first/last timestamp, all timestamps."""


def _gap_seconds(gap_hours: Optional[float]) -> int:
    if gap_hours is None:
        gap_hours = SESSION_GAP_HOURS
    return int(float(gap_hours) * 3600)


def split_sessions(
    timestamps: Iterable[int],
    gap_hours: Optional[float] = None,
    min_len_minutes: Optional[float] = None,
) -> List[Session]:
    """Splits a timestamp collection into sessions at gaps > ``gap_hours``.

    Timestamps that form a run shorter than ``min_len_minutes`` are dropped as
    noise (a stray notification, a 3-second check of the screen). Sessions are
    keyed by the local start date, suffixed ``_2``/``_3`` when several start on
    the same calendar date.
    """
    if min_len_minutes is None:
        min_len_minutes = SESSION_MIN_LEN_MINUTES
    gap = _gap_seconds(gap_hours)
    min_len = int(float(min_len_minutes) * 60)

    ts = sorted({int(t) for t in timestamps})
    runs: List[List[int]] = []
    cur: List[int] = []
    for t in ts:
        if cur and t - cur[-1] > gap:
            runs.append(cur)
            cur = []
        cur.append(t)
    if cur:
        runs.append(cur)

    # Drop micro-runs that never actually formed a working block.
    runs = [r for r in runs if r[-1] - r[0] >= min_len]

    counters: dict = {}
    sessions: List[Session] = []
    for r in runs:
        start_date = datetime.datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d")
        n = counters.get(start_date, 0) + 1
        counters[start_date] = n
        key = start_date if n == 1 else f"{start_date}_{n}"
        sessions.append(Session(key=key, start=r[0], end=r[-1], timestamps=tuple(r)))
    return sessions


def session_by_key(sessions: List[Session], key: str) -> Optional[Session]:
    """Returns the session with the given key, or None."""
    for s in sessions:
        if s.key == key:
            return s
    return None


def covering_session(sessions: List[Session], day_iso: str) -> str:
    """The session key a given local calendar day cell should open.

    A session whose key equals the day's date owns that cell (its start day).
    Otherwise the cell may contain only the tail of a session that started on a
    previous day (e.g. 00:00-06:00 of a session started 18:00 the day before);
    map it to that covering session, then to the nearest one.
    """
    if not sessions:
        return day_iso
    for s in sessions:
        if s.key == day_iso:
            return s.key
    midnight = int(datetime.datetime.strptime(day_iso, "%Y-%m-%d").timestamp())
    for s in sessions:
        if s.start <= midnight < s.end:
            return s.key
    near = min(sessions, key=lambda s: abs(s.start - midnight))
    return near.key