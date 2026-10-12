from threading import Thread
import datetime
import json
import os
import re

import numpy as np
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory

from openrecall.config import (
    SESSION_GAP_HOURS,
    appdata_folder,
    screenshots_path,
    summaries_path,
)
from openrecall.database import (
    create_db,
    get_all_entries,
    get_entries_for_day,
    get_entries_for_session,
    get_sessions,
    get_timestamps,
)
from openrecall.sessions import covering_session
from openrecall.nlp import cosine_similarity, get_embedding
from openrecall.screenshot import record_screenshots_thread
from openrecall.utils import human_readable_time, timestamp_to_human_readable
from openrecall import ai_status
from openrecall import ask as ask_qa

app = Flask(__name__)

app.jinja_env.filters["human_readable_time"] = human_readable_time
app.jinja_env.filters["timestamp_to_human_readable"] = timestamp_to_human_readable


def _load_vision(day: str) -> dict:
    """Load the day's per-moment AI descriptions ({ts: {when, description}})."""
    path = os.path.join(summaries_path, f"{day}_vision.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}


_SCENE_MARKERS = ("SCENE", "PRIMARY", "SECONDARY", "TAGS", "WINDOWS", "TABS")

_V2_MONITOR_RE = re.compile(
    r"^M(?:onitor)?\s*(\d+)\s*(?:\((.*)\)\s*)?:\s*(.*)$", re.IGNORECASE
)

_V2_PREAMBLE_RE = re.compile(r"^(?:SCENE|SCENARIO|SEQUENCE|OVERVIEW)\s*[:.]?\s*", re.IGNORECASE)


def _parse_description(text):
    """Splits a vision description into the pieces the UI shows.

    Format v2 (current): one block per monitor ``M0 (PRIMARY (main)): ...``
    plus a ``FOCUS:`` line and ``TAGS:``. Returns {'format': 'v2',
    per_monitor: {idx: text}, focus, tags}.

    Legacy (older cache): ``[HH:MM] SCENE: ... PRIMARY: ... SECONDARY: ...
    TAGS: ...``. Returns {'format': 'legacy', scene, primary, secondary, tags,
    ...} so previously summarized days still render. Keys with no content are
    omitted.
    """
    if not text:
        return None
    lines = []
    for raw in text.splitlines():
        # Drop the leading "[HH:MM]" prefix the model puts on the message.
        raw = re.sub(r"^\[?\d{1,2}:\d{2}\]?\s*", "", raw.strip())
        if raw:
            lines.append(raw)

    first_mon = next(
        (ln for ln in lines if _V2_MONITOR_RE.match(_V2_PREAMBLE_RE.sub("", ln))),
        None,
    )
    if first_mon:
        per_monitor = {}
        cur = None
        focus, tags = "", ""
        for raw in lines:
            ln = _V2_PREAMBLE_RE.sub("", raw)  # tolerate a leading SCENE: noise line
            m = _V2_MONITOR_RE.match(ln)
            if m:
                per_monitor.setdefault(m.group(1), [m.group(3).strip()])
                cur = m.group(1)
                continue
            fm = re.match(r"^FOCUS[:.]?\s*(.*)$", ln, re.IGNORECASE)
            if fm:
                focus = fm.group(1).strip()
                continue
            tm = re.match(r"^TAGS[:.]?\s*(.*)$", ln, re.IGNORECASE)
            if tm:
                tags = tm.group(1).strip()
                continue
            if cur is not None:
                per_monitor[cur].append(ln)
        res = {"format": "v2"}
        res["per_monitor"] = {
            k: "\n".join(v).strip()
            for k, v in per_monitor.items()
            if "\n".join(v).strip()
        }
        if focus:
            res["focus"] = focus
        if tags:
            res["tags"] = tags
        return res

    sections = {}
    current = "scene"
    for ln in lines:
        marker = next((m for m in _SCENE_MARKERS if ln.upper().startswith(m + ":")), None)
        if marker:
            current = marker.lower()
            sections.setdefault(current, [ln[len(marker) + 1:].strip()])
        else:
            sections.setdefault(current, []).append(ln)
    scene = "\n".join(sections.get("scene", [])).strip()
    scene = re.sub(r"^SCENE[:]?\s*", "", scene, flags=re.IGNORECASE).strip()
    res = {"format": "legacy"}
    if scene:
        res["scene"] = scene
    if "\n".join(sections.get("primary", [])).strip():
        res["primary"] = "\n".join(sections.get("primary", [])).strip()
    if "\n".join(sections.get("secondary", [])).strip():
        res["secondary"] = "\n".join(sections.get("secondary", [])).strip()
    if ",".join(sections.get("tags", [])).strip(" ,"):
        res["tags"] = ",".join(sections.get("tags", [])).strip(" ,")
    if "\n".join(sections.get("windows", [])).strip():
        res["windows_desc"] = "\n".join(sections.get("windows", [])).strip()
    if "\n".join(sections.get("tabs", [])).strip():
        res["tabs_desc"] = "\n".join(sections.get("tabs", [])).strip()
    return res if res.get("scene") else None


def _load_schedule(day: str):
    """Day's structured schedule (per-hour blocks + real no-data spans)."""
    path = os.path.join(summaries_path, f"{day}_schedule.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _summary_info(day: str) -> dict:
    """Chosen summary file + raw markdown for a day, or None."""
    if not os.path.isdir(summaries_path):
        return None
    candidates = []
    try:
        names = os.listdir(summaries_path)
    except OSError:
        return None
    for name in names:
        if name.startswith("user_profile"):
            continue
        if name == f"{day}_vision.json":
            continue
        if not name.endswith(".md"):
            continue
        stem = name[:-3]
        if stem == day or stem.startswith(day + "_"):
            candidates.append(name)
    if not candidates:
        return None
    # Prefer writer variants (e.g. 2026-09-01_14b.md) over the plain base file.
    candidates.sort(key=lambda n: (0 if "_" in n[:-3] else 1, n))
    chosen = candidates[0]
    try:
        with open(os.path.join(summaries_path, chosen), "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    return {"chosen": chosen, "files": candidates, "raw": raw}


def _day_counts() -> dict:
    """Map {YYYY-MM-DD: number of distinct captured moments}."""
    days = {}
    for ts in get_timestamps():
        key = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        days.setdefault(key, set()).add(ts)
    return {k: len(v) for k, v in days.items()}


def _activity_blocks(day: str, timestamps) -> list:
    """Contiguous activity ranges ``[start_min, end_min]`` for a calendar day.

    Hours are in the calendar day's local time (0..1440 minutes). Timestamps
    closer than 15 minutes apart are merged so AFK 5-min heartbeats render as
    one continuous block; real sleep/away gaps stay separate. Used to colour
    each calendar cell "across the whole day" without hardcoding any schedule.
    """
    day_start = int(datetime.datetime.strptime(day, "%Y-%m-%d").timestamp())
    day_end = day_start + 86400
    mins = sorted(
        {
            (ts - day_start) // 60
            for ts in timestamps
            if day_start <= ts < day_end
        }
    )
    if not mins:
        return []
    blocks = []
    lo = hi = mins[0]
    for m in mins[1:]:
        if m - hi <= 15:
            hi = m
        else:
            blocks.append([lo, hi + 1])
            lo = hi = m
    blocks.append([lo, hi + 1])
    return blocks


@app.route("/")
def timeline():
    return render_template("main.html")


@app.route("/ai")
def ai_page():
    return render_template("ai.html")


@app.route("/ask")
def ask_page():
    return render_template("ask.html")


@app.route("/api/ask", methods=["GET", "POST"])
def api_ask():
    """Answers a plain-English question from the AI summaries.

    ``ai=0`` skips the local model pass and answers from keyword ranking alone,
    which is much faster and still returns timestamped links.
    """
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        q = payload.get("q", "")
    else:
        q = request.args.get("q", "")
    q = (q or "").strip()
    if not q:
        return jsonify({"question": "", "answer": None, "hits": [], "used_ai": False})
    use_ai = str(request.args.get("ai", "1")).lower() not in ("0", "false", "no")
    return jsonify(ask_qa.search(q, use_ai=use_ai))


@app.route("/api/ai-status")
def api_ai_status():
    st = ai_status.read_status()
    try:
        n = int(request.args.get("lines", 200))
    except (TypeError, ValueError):
        n = 200
    st["log_tail"] = ai_status.tail(
        ai_status.progress_log_path(), n=max(1, min(n, 2000))
    )
    st["stage_label"] = ai_status.stage_label(st)
    return jsonify(st)


@app.route("/api/timeline")
def api_timeline():
    sessions = get_sessions()
    keys = {s.key for s in sessions}
    has_summary, has_vision = set(), set()
    if os.path.isdir(summaries_path):
        try:
            for name in os.listdir(summaries_path):
                if name.startswith("user_profile"):
                    continue
                if name.endswith("_vision.json"):
                    key = name[: -len("_vision.json")]
                    if key in keys:
                        has_vision.add(key)
                elif name.endswith(".md"):
                    stem = name[:-3]
                    if stem in keys:
                        has_summary.add(stem)
                    else:
                        for k in keys:
                            if stem.startswith(k + "_") or k.startswith(stem + "_"):
                                has_summary.add(k)
                                break
        except OSError:
            pass

    counts = _day_counts()
    ts_all = get_timestamps()
    days = [
        {
            "day": d,
            "count": counts.get(d, 0),
            "session": covering_session(sessions, d),
            "has_summary": d in has_summary,
            "has_vision": d in has_vision,
            "blocks": _activity_blocks(d, ts_all),
        }
        for d in sorted(counts)
    ]
    session_list = [
        {
            "key": s.key,
            "start_ts": s.start,
            "end_ts": s.end,
            "count": len(s.timestamps),
            "start_local": datetime.datetime.fromtimestamp(s.start).strftime("%Y-%m-%d %H:%M"),
            "end_local": datetime.datetime.fromtimestamp(s.end).strftime("%Y-%m-%d %H:%M"),
            "has_summary": s.key in has_summary,
            "has_vision": s.key in has_vision,
        }
        for s in sessions
    ]
    return jsonify(
        {
            "today": datetime.date.today().isoformat(),
            "gap_hours": SESSION_GAP_HOURS,
            "sessions": session_list,
            "days": days,
        }
    )


@app.route("/api/day/<day>")
def api_day(day):
    entries = get_entries_for_session(day)
    groups = {}
    for e in entries:
        groups.setdefault(e.timestamp, []).append(e)
    vision = _load_vision(day)
    trios = []
    for ts in sorted(groups):
        mons = sorted(
            groups[ts],
            key=lambda e: (
                e.screen_x if e.screen_x is not None else 0,
                e.monitor if e.monitor is not None else 0,
            ),
        )
        monitors = []
        parsed = None
        per_monitor = None
        fmt = None
        focus = tags = None
        desc = vision.get(ts)
        if desc:
            parsed = _parse_description(desc.get("description"))
            fmt = (parsed or {}).get("format") if parsed else None
            per_monitor = (parsed or {}).get("per_monitor") if parsed else None
            focus = (parsed or {}).get("focus")
            tags = (parsed or {}).get("tags")
        for pos, m in enumerate(mons, start=1):
            exists = bool(m.filename) and os.path.exists(
                os.path.join(screenshots_path, m.filename)
            )
            label = (
                f"Screen {pos}"
                + (f" (m{m.monitor})" if m.monitor is not None else "")
                + f" | {m.app or 'Unknown'} | {m.title or ''}"
            )
            monitors.append(
                {
                    "file": m.filename,
                    "label": label,
                    "url": m.browser_url,
                    "tabs": (m.browser_tabs if m.browser_tabs else None) or [],
                    "windows": (m.windows if m.windows else None) or [],
                    "monitor": m.monitor,
                    "ai": (per_monitor or {}).get(str(m.monitor)) if per_monitor else None,
                    "exists": exists,
                }
            )
        when = (
            desc.get("when")
            if desc
            else datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
        )
        # Desktop-wide union of tabs/windows across the moment's screens.
        all_tabs, all_wins = [], []
        seen_t, seen_w = set(), set()
        for m in mons:
            for t in m.browser_tabs or []:
                k = (str(t.get("title") or ""), str(t.get("url") or ""))
                if k[0] or k[1]:
                    if k not in seen_t:
                        seen_t.add(k)
                        all_tabs.append(t)
            for w in m.windows or []:
                k = (str(w.get("app") or ""), str(w.get("title") or ""))
                if k[0] or k[1]:
                    if k not in seen_w:
                        seen_w.add(k)
                        all_wins.append(w)
        trios.append(
            {
                "ts": ts,
                "local_time": datetime.datetime.fromtimestamp(ts).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "when": when,
                "described": bool(fmt),
                "format": fmt,
                "per_monitor": per_monitor,
                "focus": focus,
                "tags": tags,
                "description": (parsed or {}).get("scene") if parsed else None,
                "primary": (parsed or {}).get("primary") or None,
                "secondary": (parsed or {}).get("secondary") or None,
                "tabs": all_tabs,
                "windows": all_wins,
                "tabs_desc": (parsed or {}).get("tabs_desc") or None,
                "windows_desc": (parsed or {}).get("windows_desc") or None,
                "monitors": monitors,
            }
        )
    session_info = None
    for s in get_sessions():
        if s.key == day:
            session_info = {
                "key": s.key,
                "start_ts": s.start,
                "end_ts": s.end,
                "start_local": datetime.datetime.fromtimestamp(s.start).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "end_local": datetime.datetime.fromtimestamp(s.end).strftime(
                    "%Y-%m-%d %H:%M"
                ),
            }
            break
    return jsonify(
        {
            "day": day,
            "session": session_info,
            "trio_count": len(trios),
            "trios": trios,
            "summary": _summary_info(day),
            "schedule": _load_schedule(day),
        }
    )


@app.route("/day/<date_str>")
def day(date_str):
    return redirect(f"/?day={date_str}")


@app.route("/days")
def days():
    return redirect("/")


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    entries = []
    results = []
    if q:
        entries = get_all_entries()
        if entries:
            embeddings = [
                np.frombuffer(e.embedding, dtype=np.float32) for e in entries
            ]
            query_embedding = get_embedding(q)
            similarities = [
                cosine_similarity(query_embedding, emb) for emb in embeddings
            ]
            indices = np.argsort(similarities)[::-1]
            results = [entries[i] for i in indices]
    return render_template(
        "search.html",
        q=q,
        results=results,
    )


@app.route("/screenshots/<filename>")
def serve_screenshot(filename):
    return send_from_directory(screenshots_path, filename)


if __name__ == "__main__":
    create_db()

    print(f"Appdata folder: {appdata_folder}")

    # Start the embedded capture thread UNLESS capture is run as its own service.
    # Set OPENRECALL_DISABLE_CAPTURE=1 when a separate openrecall-capture.service
    # owns capturing, so only one process writes moments to the database.
    if os.environ.get("OPENRECALL_DISABLE_CAPTURE", "").strip().lower() not in ("1", "true", "yes", "on"):
        t = Thread(target=record_screenshots_thread, daemon=True)
        t.start()
    else:
        print("Embedded capture disabled (OPENRECALL_DISABLE_CAPTURE set); "
              "expecting a separate capture service.")

    app.run(port=8082)