"""Ask plain-English questions about the recall history ("when did I last play osu?").

The searchable corpus is **only the AI output** in ``summaries_path`` -- the
per-moment vision descriptions (``<session>_vision.json``), the hourly schedule
blocks (``<session>_schedule.json``) and the day narratives (``<session>*.md``).
The capture database is deliberately not searched: ``openrecall/prune.py``
deletes screenshot files and their rows once a moment falls outside the
retention window, so the AI summaries are the only durable record of what
happened. Nothing is lost by searching them, because the vision text is
exactly where the app names, window titles and on-screen (OCR) text the model
read off the screen were written down.

Retrieval is a hybrid of BM25 over the corpus and MiniLM sentence embeddings,
which makes keyword-ish questions ("osu") reliable; an optional pass through the
local Ollama model then picks the best candidate(s) and writes the one-sentence
answer, which is what separates *playing* osu! from merely having the osu!
beatmap store or a Steam library entry on screen.

Results are grouped into "sittings" (runs of consecutive matching moments) and
each hit carries a deep link back into the timeline viewer at that timestamp.
"""

import datetime
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

import numpy as np

from openrecall.config import summaries_path
from openrecall.nlp import get_embedding, get_embeddings

# Consecutive matching moments closer than this are one sitting. The summary
# pipeline samples roughly every 20 minutes, so 45 min bridges a sample or two
# of a continuous session without merging two genuinely separate ones.
GROUP_GAP_MINUTES = 45.0

# Hybrid ranking weights. Lexical matching is trusted more than embeddings:
# app names ("osu") are exactly the kind of token embeddings blur, and a
# question mentioning an app is nearly always a keyword question.
LEX_WEIGHT = 3.0
SEM_WEIGHT = 1.0
# A hit must reach this share of the best hit's score to be considered a real
# match for the question. Without it "last time" could surface an unrelated
# recent moment that merely shares common words.
RELEVANCE_FLOOR = 0.35
# Extra credit when a quoted phrase from the question appears verbatim.
PHRASE_BONUS = 1.0

MAX_HITS = 12
LLM_CANDIDATES = 18

OLLAMA_URL = os.environ.get(
    "OPENRECALL_OLLAMA_URL", "http://localhost:11434/api/chat"
)
# Same writer model the summary pipeline uses, so a 9b is usually already warm.
ASK_MODEL = os.environ.get("OPENRECALL_ASK_MODEL", "huihui_ai/qwen3.5-abliterated:9b")
# Generous, because the first question of a session may have to load the model.
ASK_TIMEOUT = float(os.environ.get("OPENRECALL_ASK_TIMEOUT", "120"))

BM25_K1 = 1.5
BM25_B = 0.75

# --------------------------------------------------------------------------
# Tokenising
# --------------------------------------------------------------------------

# Trailing punctuation is dropped rather than kept, so "osu!" and "osu" are the
# same token -- the model writes the app name both ways.
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#&._-]*")
_STRIP_EDGES = "._-"


def _tokenize(text):
    out = []
    for raw in _WORD_RE.findall((text or "").lower()):
        tok = raw.strip(_STRIP_EDGES)
        if tok:
            out.append(tok)
    return out


_STOPWORDS = set(
    """
    a about all also am an and any are as at be been being but by can did do does
    doing done for from get got had has have how i if in into is it its just last
    latest let like made make me my of on once only or other our out over own
    please really should show so some tell than that the their them then there
    these they thing things this those time to too up us use used using very was
    way we were what when where which who why will with would you your
    """.split()
)

# Question words that never carry the subject of the question.
_QUESTION_WORDS = set(
    """
    whats what when where which who whom whose why how did do does is was were
    """.split()
)

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# Hour windows for "what did I do in the evening".
_DAYPARTS = {
    "morning": (5, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "night": (21, 29),  # wraps past midnight
}

# Generic verbs that describe the action rather than the subject of a question.
# They are dropped as retrieval anchors: "when did I play osu" is a question
# about osu, and matching documents on "played" alone returns every game, video
# and stream that was ever played.
_GENERIC_VERBS = set(
    """
    play plays played playing use uses used using watch watches watched watching
    read reads reading write writes writing code codes coding work works working
    browse browses browsing browsed open opens opened launch launches launched
    start starts started run runs running do does did doing make makes made
    install installs installed download downloads downloaded
    """.split()
)

_LAST_RE = re.compile(
    r"\b(?:last|latest|most\s+recent|recently|newest|just\s+now|currently)\b", re.I
)
_FIRST_RE = re.compile(
    r"\b(?:first|earliest|oldest|very\s+first|begin\w*)\b", re.I
)

# Session key of a summary file stem: "2026-09-06" or "2026-09-06_2". Writer
# variants ("2026-09-06_2_14b") are reduced to their session key.
_KEY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}(?:_\d+)?)")

_FOCUS_RE = re.compile(r"^FOCUS\s*:\s*(.*)$", re.I | re.M)
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------


class _Bm25:
    """Plain BM25 over the summary corpus.

    Small and self-contained on purpose: the corpus is a few thousand short
    documents, so an inverted index with IDF weighting is both fast enough and
    far more predictable than a pure-vector search for app-name questions.
    """

    def __init__(self, token_lists):
        self.n = len(token_lists)
        self.lengths = [len(d) for d in token_lists]
        self.avg_len = (sum(self.lengths) / self.n) if self.n else 1.0
        df = Counter()
        for toks in token_lists:
            df.update(set(toks))
        self.idf = {
            t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }
        self.postings = defaultdict(list)
        for i, toks in enumerate(token_lists):
            for t, f in Counter(toks).items():
                self.postings[t].append((i, f))

    def score(self, terms):
        """Returns {doc_index: score} for the given query terms."""
        out = defaultdict(float)
        for t in terms:
            idf = self.idf.get(t)
            if idf is None:
                continue  # term is not in the corpus at all
            for i, f in self.postings[t]:
                norm = 1 - BM25_B + BM25_B * (self.lengths[i] / (self.avg_len or 1))
                out[i] += idf * (f * (BM25_K1 + 1)) / (f + BM25_K1 * norm)
        return out


def _fingerprint(names):
    """Cheap change detector for the summaries directory."""
    parts = []
    for name in names:
        try:
            st = os.stat(os.path.join(summaries_path, name))
        except OSError:
            continue
        parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
    return "|".join(sorted(parts))


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _head_of(description):
    """One good line to show as a snippet / to give the model as context.

    The FOCUS line is the model's own one-line summary of the moment, so it
    beats the first monitor block when both are available.
    """
    m = _FOCUS_RE.search(description or "")
    if m and m.group(1).strip():
        return m.group(1).strip()
    for line in (description or "").splitlines():
        line = re.sub(r"^\[?\d{1,2}:\d{2}\]?\s*", "", line.strip())
        if line[:1] == "M" and ":" in line:
            return line.split(":", 1)[1].strip()[:240]
    # Fall back to the first real line: a day narrative opens with headings and
    # an italic "generated by" line, neither of which says anything.
    for line in (description or "").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "_", "|")):
            return line[:240]
    return " ".join((description or "").split())[:240]


def _strip_schedule_section(md):
    """Drops the '## SCHEDULE' block from a day narrative.

    The schedule is already indexed as its own hourly documents, so leaving it
    in would let one day-level document outrank the moments it duplicates.
    """
    m = re.search(r"^##\s+SCHEDULE\b", md or "", re.I | re.M)
    if not m:
        return (md or "").strip()
    rest = md[m.end():]
    nxt = re.search(r"^##\s+", rest, re.M)
    return (md[: m.start()] + (rest[nxt.start():] if nxt else "")).strip()


def _resolve_hhmm(session_start_ts, label):
    """Turns an 'HH:MM' schedule label into a real timestamp.

    A session is keyed by the date it STARTED on and may run past midnight, so a
    '01:30' block in the 2026-09-21 session belongs to the following morning.
    """
    m = _HHMM_RE.match((label or "").strip())
    if not m:
        return session_start_ts
    midnight = datetime.datetime.fromtimestamp(session_start_ts).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cand = midnight + datetime.timedelta(
        hours=int(m.group(1)), minutes=int(m.group(2))
    )
    while cand < datetime.datetime.fromtimestamp(session_start_ts):
        cand += datetime.timedelta(days=1)
    return int(cand.timestamp())


def _build_index():
    """Scans the summaries directory into a flat list of searchable documents."""
    docs = []
    days = {}
    try:
        names = sorted(os.listdir(summaries_path))
    except OSError:
        names = []

    # Day narratives first so a session's real timestamps are known before the
    # schedule labels need resolving.
    md_by_key = {}
    for name in names:
        if not name.endswith(".md") or name.startswith("user_profile"):
            continue
        km = _KEY_RE.match(name[:-3])
        if km:
            md_by_key.setdefault(km.group(1), []).append(name)

    for name in names:
        if not name.endswith("_vision.json"):
            continue
        key = name[: -len("_vision.json")]
        scenes = _read_json(os.path.join(summaries_path, name))
        if not isinstance(scenes, dict):
            continue
        stamps = []
        for raw_ts, rec in scenes.items():
            try:
                ts = int(raw_ts)
            except (TypeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            text = rec.get("description") or ""
            if not text.strip():
                continue
            stamps.append(ts)
            docs.append(
                {
                    "id": f"m:{ts}",
                    "kind": "moment",
                    "day": key,
                    "ts": ts,
                    "end_ts": ts,
                    "when": rec.get("when") or datetime.datetime.fromtimestamp(
                        ts
                    ).strftime("%H:%M"),
                    "text": text,
                    "head": _head_of(text),
                }
            )
        if stamps:
            days[key] = {
                "key": key,
                "start": min(stamps),
                "end": max(stamps),
                "moments": len(stamps),
            }

    for name in names:
        if not name.endswith("_schedule.json"):
            continue
        key = name[: -len("_schedule.json")]
        sched = _read_json(os.path.join(summaries_path, name))
        if not isinstance(sched, dict):
            continue
        day = days.get(key)
        if not day:
            continue
        for hour, blocks in (sched.get("hours") or {}).items():
            for b in blocks or []:
                text = (b.get("text") or "").strip()
                if not text:
                    continue
                start_label = b.get("start") or f"{hour}:00"
                end_label = b.get("end") or start_label
                start_ts = _resolve_hhmm(day["start"], start_label)
                end_ts = _resolve_hhmm(day["start"], end_label)
                if end_ts < start_ts:
                    end_ts = start_ts
                docs.append(
                    {
                        "id": f"b:{key}:{start_label}-{end_label}",
                        "kind": "block",
                        "day": key,
                        "ts": start_ts,
                        "end_ts": end_ts,
                        "when": start_label,
                        "text": text,
                        "head": text[:240],
                    }
                )

    for key, files in md_by_key.items():
        # Prefer writer variants ("..._2_14b.md") over the plain base file, the
        # same choice the day view makes.
        files.sort(key=lambda n: (0 if "_" in n[:-3] else 1, n))
        try:
            with open(os.path.join(summaries_path, files[0]), encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        body = _strip_schedule_section(raw)
        if not body:
            continue
        day = days.get(key)
        docs.append(
            {
                "id": f"d:{key}",
                "kind": "day",
                "day": key,
                "ts": day["start"] if day else int(time.time()),
                "end_ts": day["end"] if day else int(time.time()),
                "when": "",
                "text": body,
                "head": _head_of(body),
            }
        )
        if day:
            day["has_md"] = True

    for d in docs:
        d["score"] = 0.0
    return {
        "sig": _fingerprint(names),
        "docs": docs,
        "days": days,
        "bm25": _Bm25([_tokenize(d["text"]) for d in docs]) if docs else None,
    }


_INDEX = None
_INDEX_SIG = None
_INDEX_LOCK = threading.Lock()


def get_index():
    """The summary index, rebuilt only when the summaries directory changes."""
    global _INDEX, _INDEX_SIG
    try:
        sig = _fingerprint(sorted(os.listdir(summaries_path)))
    except OSError:
        sig = ""
    if _INDEX is not None and _INDEX_SIG == sig:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is None or _INDEX_SIG != sig:
            _INDEX = _build_index()
            _INDEX_SIG = sig
    return _INDEX


# --------------------------------------------------------------------------
# Embeddings (built in the background; retrieval works without them)
# --------------------------------------------------------------------------

_VEC = None
_VEC_SIG = None
_VEC_LOCK = threading.Lock()
_VEC_THREAD = None


def _build_vectors(sig):
    global _VEC, _VEC_SIG
    try:
        idx = get_index()
        docs = idx["docs"]
        if not docs or idx["sig"] != sig:
            return
        # Trimmed: the leading lines carry the app names and the focus, which
        # is all the embedding needs, and it keeps the batch fast.
        texts = [" ".join(d["text"].split())[:1200] for d in docs]
        vecs = get_embeddings(texts)
        if vecs is not None and len(vecs) == len(docs):
            _VEC = np.asarray(vecs, dtype=np.float32)
            _VEC_SIG = sig
    except Exception:
        _VEC = None
        _VEC_SIG = None


def ensure_vectors():
    """Kicks off (once) a background embedding of the corpus.

    Returns the matrix if it is ready, else None -- lexical retrieval answers
    the question immediately either way.
    """
    global _VEC_THREAD
    idx = get_index()
    if not idx["docs"]:
        return None
    if _VEC is not None and _VEC_SIG == idx["sig"]:
        return _VEC
    with _VEC_LOCK:
        if _VEC_THREAD is None or not _VEC_THREAD.is_alive():
            _VEC_THREAD = threading.Thread(
                target=_build_vectors, args=(idx["sig"],), daemon=True
            )
            _VEC_THREAD.start()
    return _VEC if _VEC_SIG == idx["sig"] else None


# --------------------------------------------------------------------------
# Question parsing
# --------------------------------------------------------------------------


def _midnight(now):
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _consume(text, pattern):
    """Removes every match of ``pattern`` from ``text`` and returns the strings."""
    found = [m.group(0) for m in pattern.finditer(text)]
    return found, pattern.sub(" ", text)


def _parse_window(text, now):
    """Detects a time window ("yesterday", "last week", "on friday", "in may").

    Returns ``(label, start_ts, end_ts, cleaned_text)``; the label is None when
    the question names no window.
    """
    label = None
    start = end = None

    def day_window(lo_dt, hi_dt, name):
        nonlocal label, start, end
        label, start, end = name, int(lo_dt.timestamp()), int(hi_dt.timestamp())

    found, text = _consume(
        text,
        re.compile(
            r"\b(?:today|tonight|this\s+morning|this\s+afternoon|this\s+evening)\b",
            re.I,
        ),
    )
    if found:
        day_window(_midnight(now), _midnight(now) + datetime.timedelta(days=1), found[0].lower())

    found, text = _consume(text, re.compile(r"\byesterday\b", re.I))
    if found and label is None:
        d = _midnight(now) - datetime.timedelta(days=1)
        day_window(d, d + datetime.timedelta(days=1), "yesterday")

    found, text = _consume(
        text, re.compile(r"\b(?:this|last|past)\s+week\b", re.I)
    )
    if found and label is None:
        this_monday = _midnight(now) - datetime.timedelta(days=now.weekday())
        lo = this_monday if found[0].lower().startswith("this") else this_monday - datetime.timedelta(days=7)
        day_window(lo, lo + datetime.timedelta(days=7), found[0].lower())

    m = re.search(r"\b(\d{1,3})\s+(day|week|month)s?\s+ago\b", text, re.I)
    if m and label is None:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"day": 1, "week": 7, "month": 30}[unit] * n
        lo = _midnight(now) - datetime.timedelta(days=delta)
        day_window(lo, _midnight(now) + datetime.timedelta(days=1), f"{n} {unit}s ago")
        text = text[: m.start()] + " " + text[m.end():]

    m = re.search(r"\b(?:last|past|previous)\s+(\d{1,3})\s+(day|week|month)s?\b", text, re.I)
    if m and label is None:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"day": 1, "week": 7, "month": 30}[unit] * n
        lo = _midnight(now) - datetime.timedelta(days=delta)
        day_window(lo, _midnight(now) + datetime.timedelta(days=1), f"last {n} {unit}s")
        text = text[: m.start()] + " " + text[m.end():]

    m = re.search(r"\bin\s+([a-z]+)\b", text, re.I)
    if m and label is None and m.group(1).lower() in _MONTHS:
        month = _MONTHS[m.group(1).lower()]
        year = now.year if month <= now.month else now.year - 1
        lo = datetime.datetime(year, month, 1)
        hi = datetime.datetime(year + (month == 12), (month % 12) + 1, 1)
        day_window(lo, hi, m.group(1).lower())
        text = text[: m.start()] + " " + text[m.end():]

    m = re.search(r"\b(?:on\s+)?([a-z]+day)\b", text, re.I)
    if m and label is None and m.group(1).lower() in _WEEKDAYS:
        target = _WEEKDAYS[m.group(1).lower()]
        delta = (now.weekday() - target) % 7
        lo = _midnight(now) - datetime.timedelta(days=delta)
        day_window(lo, lo + datetime.timedelta(days=1), m.group(1).lower())
        text = text[: m.start()] + " " + text[m.end():]

    return label, start, end, text


def parse_question(question, now=None):
    """Splits a question into subject terms, temporal intent and a time window.

    ``intent`` is ``last``, ``first`` or ``all`` and decides how the final hits
    are ordered; ``window`` is an optional ``(start_ts, end_ts)`` filter.
    """
    now = now or datetime.datetime.now()
    raw = question or ""
    phrases = re.findall(r"[\"']([^\"']{2,60})[\"']", raw)
    text = " ".join(p for p in phrases) + " " + re.sub(r"[\"']", " ", raw)

    window_label, win_start, win_end, text = _parse_window(text, now)

    last_hits, text = _consume(text, _LAST_RE)
    first_hits, text = _consume(text, _FIRST_RE)
    if first_hits and not last_hits:
        intent = "first"
    elif last_hits:
        intent = "last"
    else:
        intent = "all"

    # Time-of-day narrows the hours but not the dates, so it is applied after
    # the day window (if any) has been decided.
    daypart = None
    for name, (lo_h, hi_h) in _DAYPARTS.items():
        found, text = _consume(text, re.compile(rf"\b{name}\b", re.I))
        if found and daypart is None:
            daypart = (name, lo_h, hi_h)

    toks = _tokenize(text)
    drop = _STOPWORDS | _QUESTION_WORDS | set(_WEEKDAYS) | set(_MONTHS)
    drop |= {w.lower() for w in re.findall(r"[A-Za-z]+", " ".join(last_hits + first_hits))}
    drop |= set(_DAYPARTS)
    if window_label:
        drop |= set(_tokenize(window_label))
    terms = []
    for t in toks:
        if t in drop or len(t) < 2:
            continue
        if t not in terms:
            terms.append(t)

    return {
        "terms": terms,
        "phrases": [p.strip() for p in phrases if p.strip()],
        "intent": intent,
        "window": (win_start, win_end) if win_start is not None else None,
        "window_label": window_label,
        "daypart": daypart,
    }


def _in_daypart(ts, daypart):
    if not daypart:
        return True
    _, lo_h, hi_h = daypart
    hour = datetime.datetime.fromtimestamp(ts).hour
    if hi_h <= 24:
        return lo_h <= hour < hi_h
    return hour >= lo_h or hour < hi_h - 24


# --------------------------------------------------------------------------
# Grouping + hit formatting
# --------------------------------------------------------------------------


def group_moments(moments, gap_minutes=GROUP_GAP_MINUTES):
    """Merges consecutive matching moments into sittings.

    A sitting is the natural answer to "when did I play X" -- the summary
    pipeline only samples every ~20 minutes, so a two-hour session is several
    near-identical moments, not one timestamp.
    """
    gap = gap_minutes * 60
    groups = []
    for doc in sorted(moments, key=lambda d: d["ts"]):
        if (
            groups
            and groups[-1]["day"] == doc["day"]
            and doc["ts"] - groups[-1]["end_ts"] <= gap
        ):
            g = groups[-1]
            g["end_ts"] = doc["ts"]
            g["docs"].append(doc)
            g["score"] = max(g["score"], doc["score"])
        else:
            groups.append(
                {
                    "kind": "moment",
                    "day": doc["day"],
                    "ts": doc["ts"],
                    "end_ts": doc["ts"],
                    "docs": [doc],
                    "score": doc["score"],
                }
            )
    for g in groups:
        # Sustained activity is better evidence than a single sampled moment.
        g["moments"] = len(g["docs"])
        g["score"] *= 1 + min(0.5, 0.08 * (g["moments"] - 1))
        g["head"] = g["docs"][0]["head"]
        g["id"] = f"m:{g['ts']}"
    return groups


def _ago(ts, now=None):
    delta = (now or time.time()) - ts
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    days = int(delta // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _fmt_span(ts, end_ts):
    a = datetime.datetime.fromtimestamp(ts)
    b = datetime.datetime.fromtimestamp(end_ts)
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    head = f"{weekdays[a.weekday()]} {a.day} {a.strftime('%b %Y')}, {a.strftime('%H:%M')}"
    if b.date() == a.date():
        return f"{head} - {b.strftime('%H:%M')}"
    return f"{head} - {b.strftime('%a %d %b, %H:%M')}"


def _fmt_date(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _hit(group, now):
    return {
        "id": group["id"],
        "kind": group["kind"],
        "day": group["day"],
        "ts": group["ts"],
        "end_ts": group["end_ts"],
        "when": group.get("when", ""),
        "local": _fmt_date(group["ts"]),
        "span": _fmt_span(group["ts"], group["end_ts"]),
        "age": _ago(group["ts"], now),
        "score": round(group["score"], 4),
        "snippet": group.get("head", ""),
        "moments": group.get("moments", 1),
        # Deep link into the timeline viewer; the viewer snaps to the nearest
        # captured moment, so schedule-block timestamps land correctly too.
        "link": f"/?day={group['day']}&ts={group['ts']}",
    }


# --------------------------------------------------------------------------
# Optional Ollama rerank
# --------------------------------------------------------------------------


def _ollama_chat(model, messages, timeout=ASK_TIMEOUT, num_predict=300, temperature=0.0):
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {
                "temperature": temperature,
                "num_predict": num_predict,
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError(str(body["error"]))
    return (body.get("message") or {}).get("content", "")


_RERANK_SYS = (
    "You answer questions about a person's screen-recording history. You are "
    "given numbered candidate moments, each a short description of what was on "
    "screen at that time. Pick the candidate that best answers the question.\n"
    "Rules:\n"
    "- Only use the candidates given. Never invent a time.\n"
    "- A candidate that merely MENTIONS the subject is not the subject being "
    "used. Browsing a game's store page, a library entry, a wallpaper, a chat "
    "message about it, or a settings screen is not playing/using/doing it.\n"
    "- If the question asks for the last/most recent time, pick the latest "
    "candidate that genuinely matches. If it asks for the first, pick the "
    "earliest.\n"
    "- If nothing genuinely matches, answer NONE.\n"
    "Reply with exactly three lines and nothing else:\n"
    "ANSWER: <one short sentence naming the date and time, e.g. 'Last seen on "
    "Fri 11 Sep 2026 at 00:52.'>\n"
    "BEST: <candidate number>\n"
    "OTHERS: <up to two more candidate numbers, or none>"
)


def _rerank(question, hits, parsed, model=ASK_MODEL):
    """Asks the local model to choose among the retrieved candidates.

    Returns ``{"answer", "best", "others", "note"}`` or None when the model is
    unreachable -- the caller then answers from the retrieval order alone.
    """
    pool = hits[:LLM_CANDIDATES]
    if not pool:
        return None
    lines = []
    for i, h in enumerate(pool, start=1):
        snippet = " ".join((h["snippet"] or "").split())[:400]
        lines.append(f"[{i}] {h['span']}\n{snippet}")
    intent_note = {
        "last": "The question asks for the LAST time.",
        "first": "The question asks for the FIRST time.",
        "all": "The question asks for every occurrence; pick the most representative.",
    }[parsed["intent"]]
    window_note = (
        f"Restricted to: {parsed.get('window_label')}."
        if parsed.get("window_label")
        else ""
    )
    user = (
        f"Question: {question.strip()}\n"
        f"{intent_note} {window_note}\n\n"
        f"Candidates:\n" + "\n\n".join(lines)
    )
    try:
        reply = _ollama_chat(
            model,
            [
                {"role": "system", "content": _RERANK_SYS},
                {"role": "user", "content": user},
            ],
        )
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
        return {"note": f"local model unavailable ({type(e).__name__})"}

    if not reply.strip():
        return {"note": "local model returned nothing"}
    answer_m = re.search(r"ANSWER\s*:\s*(.+)", reply)
    best_m = re.search(r"BEST\s*:\s*\[?\s*(\d+)", reply)
    others_m = re.search(r"OTHERS\s*:\s*(.+)", reply)
    if not best_m:
        return {"note": "could not parse the model's choice", "raw": reply[:400]}
    best = int(best_m.group(1))
    if not 1 <= best <= len(pool):
        return {"note": "model chose a candidate outside the list", "raw": reply[:400]}
    others = []
    if others_m:
        for n in re.findall(r"\d+", others_m.group(1)):
            n = int(n)
            if 1 <= n <= len(pool) and n != best:
                others.append(n)
    return {
        "answer": answer_m.group(1).strip() if answer_m else None,
        "best": best,
        "others": others[:2],
    }


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def _plain_answer(question, parsed, hits, now):
    """Answer built from the retrieval order alone (no model available)."""
    if not hits:
        return "I could not find anything about that in the AI summaries."
    h = hits[0]
    when = h["span"]
    if parsed["intent"] == "first":
        lead = "Earliest match"
    elif parsed["intent"] == "last":
        lead = "Most recent match"
    else:
        lead = "Best match"
    extra = f" ({h['moments']} moments)" if h.get("moments", 1) > 1 else ""
    return f"{lead}: {when}{extra} \u2014 {h['snippet']}"


def _digest(idx, docs, parsed, now_ts, base):
    """Answers a question that names no subject ("what did I do yesterday?").

    There is nothing to rank against, so the window is simply laid out
    chronologically -- the useful answer is the list of moments, not one
    arbitrary "best match" out of a meaningless similarity score.
    """
    win = parsed["window"]
    if win:
        lo, hi = win
        label = parsed["window_label"]
    else:
        latest = max(idx["days"].values(), key=lambda d: d["end"]) if idx["days"] else None
        lo, hi = (latest["start"], latest["end"]) if latest else (0, now_ts)
        label = "the most recent session"
    pool = [
        dict(d, score=1.0)
        for d in docs
        if d["kind"] == "moment"
        and lo <= d["ts"] <= hi
        and _in_daypart(d["ts"], parsed["daypart"])
    ]
    if not pool:
        pool = [
            dict(d, score=1.0)
            for d in docs
            if d["kind"] == "moment" and _in_daypart(d["ts"], parsed["daypart"])
        ]
        if pool:
            label = "all time"
    base["window_label"] = label
    # Deliberately NOT grouped into sittings here: a digest question wants the
    # moments themselves, and a day of 20-minute samples is one continuous run
    # that would collapse into a single useless hit. Repeats of the same
    # one-line focus (the pipeline re-describes an unchanged screen) are
    # collapsed instead.
    hits = []
    seen = set()
    for d in sorted(pool, key=lambda d: -d["ts"]):
        snippet = " ".join(d["head"].split())[:120]
        if snippet and snippet in seen:
            continue
        seen.add(snippet)
        hits.append(
            _hit(
                {
                    "kind": "moment",
                    "day": d["day"],
                    "ts": d["ts"],
                    "end_ts": d["ts"],
                    "when": d.get("when", ""),
                    "head": d["head"],
                    "id": d["id"],
                    "score": 1.0,
                    "moments": 1,
                },
                now_ts,
            )
        )
        if len(hits) >= MAX_HITS:
            break
    base["hits"] = hits
    base["digest"] = True
    base["total_moments"] = len(pool)
    if not hits:
        base["answer"] = f"No AI-described moments found for {label}."
        return base
    top = hits[0]
    shown = f"showing {len(hits)} of {len(pool)}" if len(pool) > len(hits) else f"{len(pool)}"
    base["answer"] = (
        f"{shown} AI-described moment(s) for {label}. "
        f"Most recent: {top['span']} \u2014 {top['snippet']}"
    )
    return base


def search(question, limit=MAX_HITS, use_ai=True, now=None):
    """Answers a natural-language question from the AI summaries.

    Returns a JSON-ready dict with the question's interpretation, a one-sentence
    answer and the ranked hits, each carrying a deep link to the timeline.
    """
    now_ts = now.timestamp() if isinstance(now, datetime.datetime) else (now or time.time())
    idx = get_index()
    docs = idx["docs"]
    parsed = parse_question(question, now if isinstance(now, datetime.datetime) else None)
    base = {
        "question": question,
        "parsed": parsed,
        "hits": [],
        "used_ai": False,
        "answer": None,
        "note": None,
        "indexed": len(docs),
        "days": len(idx["days"]),
        "semantic": False,
        "window_fallback": False,
    }
    if not docs or not question.strip():
        base["answer"] = "Nothing to search yet \u2014 no AI summaries have been generated."
        return base

    terms = parsed["terms"]
    if not terms:
        return _digest(idx, docs, parsed, now_ts, base)

    bm25 = idx["bm25"]
    lex = bm25.score(terms) if bm25 else {}
    max_lex = max(lex.values()) if lex else 0.0

    # The subject of a question is what is left after the action verbs are
    # removed: "when did I first play osu" is about osu, and a document that
    # only matched "play" (some game, some video, some stream) is not an answer.
    # Note this cannot be an IDF rule: the schedule blocks conventionally open
    # with "App: Played ...", which makes the verb rarer than the app name.
    anchor_docs = None
    if bm25:
        content = [t for t in terms if t not in _GENERIC_VERBS]
        if content:
            anchor_docs = set()
            for t in content:
                anchor_docs.update(i for i, _ in bm25.postings[t])

    vecs = ensure_vectors()
    cos = None
    if vecs is not None and len(vecs) == len(docs):
        try:
            qv = get_embedding(" ".join(terms) if terms else question)
            norms = np.linalg.norm(vecs, axis=1)
            qn = float(np.linalg.norm(qv))
            if qn > 0 and np.any(norms > 0):
                cos = (vecs @ qv) / (norms * qn)
        except Exception:
            cos = None
    base["semantic"] = cos is not None

    win = parsed["window"]
    # Scores are kept request-local (the cached documents are shared between
    # requests, and Flask serves them from more than one thread).
    scores = np.zeros(len(docs), dtype=np.float64)
    for i, doc in enumerate(docs):
        l = (lex.get(i, 0.0) / max_lex) if max_lex else 0.0
        s = float(cos[i]) if cos is not None else 0.0
        score = LEX_WEIGHT * l + SEM_WEIGHT * max(0.0, s)
        if parsed["phrases"]:
            low = doc["text"].lower()
            if any(p.lower() in low for p in parsed["phrases"]):
                score += PHRASE_BONUS
        if not _in_daypart(doc["ts"], parsed["daypart"]):
            score = 0.0
        scores[i] = score

    scored = [
        (i, dict(d, score=float(scores[i])))
        for i, d in enumerate(docs)
        if scores[i] > 0.0
    ]
    if anchor_docs is not None:
        anchored = [pair for pair in scored if pair[0] in anchor_docs]
        if anchored:
            scored = anchored
    scored = [pair[1] for pair in scored]
    if win:
        inside = [d for d in scored if win[0] <= d["ts"] <= win[1]]
        if inside:
            scored = inside
        elif scored:
            # Nothing inside the window: answer anyway, but say so.
            base["window_fallback"] = True

    if not scored:
        base["answer"] = (
            "I could not find anything about that in the AI summaries."
            + (f" (searched {parsed['window_label']})" if parsed["window_label"] else "")
        )
        return base

    # Moment docs become sittings; schedule blocks that fall inside a sitting
    # are folded into it (they usually cover the same stretch at finer
    # granularity), and day narratives stay as their own day-level hit.
    groups = group_moments([d for d in scored if d["kind"] == "moment"])
    for d in scored:
        if d["kind"] == "moment":
            continue
        target = None
        if d["kind"] == "block":
            for g in groups:
                if (
                    g["day"] == d["day"]
                    and d["ts"] - 300 <= g["end_ts"]
                    and d["end_ts"] + 300 >= g["ts"]
                ):
                    target = g
                    break
        if target is not None:
            target["docs"].append(d)
            target["end_ts"] = max(target["end_ts"], d["end_ts"])
            target["score"] = max(target["score"], d["score"])
            continue
        groups.append(
            {
                "kind": d["kind"],
                "day": d["day"],
                "ts": d["ts"],
                "end_ts": d["end_ts"],
                "when": d.get("when", ""),
                "docs": [d],
                "score": d["score"],
                "head": d["head"],
                "id": d["id"],
                "moments": 1,
            }
        )

    if anchor_docs is not None:
        # The anchor filter already guaranteed every candidate mentions the
        # subject, so it IS the relevance criterion. A score floor on top of it
        # would only throw away genuine but shorter matches (e.g. a one-moment
        # sitting ranked below a verbose day narrative).
        relevant = groups
    else:
        # No anchor (every query term was a generic verb), so the only defence
        # against a long tail of weak semantic matches is a relative floor.
        floor = max(g["score"] for g in groups) * RELEVANCE_FLOOR
        relevant = [g for g in groups if g["score"] >= floor] or groups[:1]

    if parsed["intent"] == "last":
        # "When did I last ..." asks for the most recent genuine match, so
        # recency decides once relevance has been established.
        relevant.sort(key=lambda g: -g["ts"])
    elif parsed["intent"] == "first":
        relevant.sort(key=lambda g: g["ts"])
    else:
        relevant.sort(key=lambda g: (-g["score"], -g["ts"]))

    # More candidates go to the model than are finally shown, so it can pick a
    # lower-ranked-but-correct one and mention it.
    hits = [_hit(g, now_ts) for g in relevant[:LLM_CANDIDATES]]

    if use_ai and hits:
        verdict = _rerank(question, hits, parsed)
        if verdict:
            if verdict.get("note"):
                base["note"] = (
                    f"{verdict['note']} \u2014 ranked by keywords only."
                )
            else:
                base["used_ai"] = True
                chosen = [hits[verdict["best"] - 1]]
                chosen += [hits[n - 1] for n in verdict["others"]]
                rest = [h for h in hits if h not in chosen]
                hits = chosen + rest
                base["answer"] = verdict.get("answer")

    if not base["answer"]:
        base["answer"] = _plain_answer(question, parsed, hits, now_ts)
    if base["window_fallback"] and base["answer"]:
        base["answer"] = (
            f"Nothing was found for {parsed['window_label']}, so the closest match "
            f"from all time is shown. " + base["answer"]
        )
    base["hits"] = hits[:limit]
    return base
