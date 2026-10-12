"""Shared AI-summary progress tracking for OpenRecall.

`bulk_summary.py` tees all of its output into a single authoritative log
(``<data root>/logs/bulk_summary.out.log``) no matter who launched it, and this
module parses that log plus the ``summaries/`` folder into one status dict.

Both the system tray (`tray.py`) and the web UI (`openrecall/app.py`, ``/ai``
and ``/api/ai-status``) render from here, so they can never disagree.
"""
import os
import re

from openrecall.config import appdata_folder, summaries_path


def progress_log_path() -> str:
    """The one log every bulk_summary run appends to."""
    return os.path.join(appdata_folder, "logs", "bulk_summary.out.log")


# Progress-log line shapes emitted by bulk_summary.py (sequential; later wins).
_RE_RUN_START = re.compile(r"^Vision model: ", re.MULTILINE)
_RE_DAY_START = re.compile(r"^\s+(\d{4}-\d{2}-\d{2}): \d+ snapshots, sampling \d+ moment")
_RE_VISION = re.compile(r"\[\s*(\d+)/(\d+)\]\s+Vision analyzing moment at (\d{2}:\d{2})")
_RE_HOUR = re.compile(r"Analyzing hour (\d{4}-\d{2}-\d{2}) (\d{2}):00 \(\d+ moment")
_RE_OVERVIEW = re.compile(r"Building comprehensive day schedule")
_RE_DONE = re.compile(r"^\s+(OK|FAIL)\s+(\d{4}-\d{2}-\d{2})")
_RE_SCENE = re.compile(r"^\s+SCENE:\s+(.*)$")


def tail(path: str, n: int = 250, budget: int = 262144) -> list:
    """Returns the last `n` lines of a file (reads only the trailing budget bytes)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - budget))
            data = f.read()
        return data.splitlines()[-n:]
    except OSError:
        return []


def summary_process_running() -> bool:
    """True while any bulk_summary process is alive (psutil, best-effort)."""
    try:
        import psutil

        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmd = proc.info["cmdline"] or []
            except Exception:
                continue
            if any("bulk_summary.py" in c for c in cmd):
                return True
    except Exception:
        pass
    return False


def read_status() -> dict:
    """Live AI-summary status: stage, day, progress, latest scene and log tail."""
    st = {
        "running": False,
        "stage": "idle",
        "day": None,
        "pending_day": None,
        "cur": 0,
        "total": 0,
        "minute": None,
        "hour_now": None,
        "scene": "",
        "days_done": [],
        "latest_done": None,
        "log": [],
    }

    # Robust facts from the summaries/ folder: which days are finished and which
    # day is being (or was left) worked on (vision cache exists, .md doesn't).
    try:
        if os.path.isdir(summaries_path):
            names = set(os.listdir(summaries_path))
            st["days_done"] = sorted(
                f[:10] for f in names if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f)
            )
            st["latest_done"] = st["days_done"][-1] if st["days_done"] else None
            vision_days = sorted(
                f[:10] for f in names
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}_vision\.json", f)
            )
            done = set(st["days_done"])
            st["pending_day"] = next(
                (d for d in reversed(vision_days) if d not in done), None
            )
    except OSError:
        pass

    lines = tail(progress_log_path())
    st["log"] = lines[-20:]

    # Only look at the CURRENT run: everything before the last "Vision model:"
    # header belongs to a previous run and must not leak into the status.
    starts = [i for i, ln in enumerate(lines) if _RE_RUN_START.match(ln)]
    run_lines = lines[starts[-1]:] if starts else lines

    for ln in run_lines:
        m = _RE_DAY_START.match(ln)
        if m:
            st["day"] = m.group(1)

        m = _RE_VISION.search(ln)
        if m:
            st["stage"] = "vision"
            st["cur"], st["total"], st["minute"] = (
                int(m.group(1)), int(m.group(2)), m.group(3),
            )

        m = _RE_HOUR.search(ln)
        if m:
            st["stage"] = "writer"
            st["day"] = m.group(1)
            st["hour_now"] = m.group(2)

        if _RE_OVERVIEW.search(ln):
            st["stage"] = "overview"

        m = _RE_DONE.match(ln)
        if m:
            st["stage"] = "done"
            st["day"] = m.group(2)

        m = _RE_SCENE.match(ln)
        if m and m.group(1).strip():
            st["scene"] = m.group(1).strip()

    st["running"] = summary_process_running()
    if st["running"]:
        if st["stage"] in ("idle", "done", "paused", "starting"):
            st["stage"] = "starting"
        if not st["day"]:
            st["day"] = st["pending_day"]
    else:
        # Nothing is producing progress right now: never present a stale current
        # day/scene/hour as if it were live. Keep the last run's outcome only.
        if st["stage"] in ("vision", "writer", "overview", "starting"):
            st["stage"] = "paused"
        st["day"] = None
        st["scene"] = ""
        st["cur"] = st["total"] = 0
        st["minute"] = None
        st["hour_now"] = None
    return st


def stage_label(st: dict) -> str:
    if st.get("running"):
        return {
            "vision": "Vision description",
            "writer": "Writer - hour analysis",
            "overview": "Writer - day overview",
            "starting": "Starting up",
        }.get(st.get("stage"), "Working")
    if st.get("stage") == "paused":
        return "Paused (process not running)"
    if st.get("stage") == "done":
        return "Idle (all days complete)"
    return "Not running"


def tray_title(st: dict) -> str:
    parts = ["OpenRecall"]
    if st.get("running"):
        bits = ["AI"]
        if st.get("day"):
            bits.append(st["day"])
        parts.append(" ".join(bits))
        parts.append(stage_label(st))
        if st.get("stage") == "vision" and st.get("total"):
            parts.append(f"{st['minute']} {st['cur']}/{st['total']}")
        elif st.get("stage") == "writer" and st.get("hour_now"):
            parts.append(f"{st['hour_now']}:00")
        if st.get("latest_done"):
            parts.append(f"last done {st['latest_done']}")
        return " \u00b7 ".join(parts)
    if st.get("pending_day"):
        return f"OpenRecall \u00b7 AI summary idle \u00b7 pending {st['pending_day']}"
    if st.get("latest_done"):
        return f"OpenRecall \u00b7 AI summary idle \u00b7 last done {st['latest_done']}"
    return "OpenRecall \u00b7 AI summary idle"
