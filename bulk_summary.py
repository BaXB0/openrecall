"""
OpenRecall day-summary pipeline.

Runs locally (no cloud), driven by Ollama. The pipeline is shaped by what the
capture loop ALREADY records per moment (which it does, this fork): the active
app + window title, the ACTIVE browser tab (title + URL) of every visible browser
window, every visible window (app + title + screen), and the OCR text -- all per
screen, on disk in recall.db. That OS-verified context is exact, so the vision
model no longer has to transcribe it from pixels.

Pipeline (three stages):
  1. Sampling: every ~20 minutes one "moment" is selected -- the full set of
     monitor images at that instant (all screens), with its timestamp. (Vision is
     cheap now, so this stays dense enough to track app switches.)
  2. Vision stage (~4B qwen3-vl abliterated): each sampled moment's screens are described as
     ONE cohesive scene -- all monitors active simultaneously -- but ONLY as a
     thin synthesizer: what is happening, what each app/screen is doing, named
     from the images. Short prompt, short reply. No window/tab transcription.
   3. Analysis stage (9B abliterated writer): the moment-descriptions are grouped
      by hour, and BULKED with the full per-moment OS context for that hour -- a
      minute-level activity timeline of every app/tab switch plus rich snapshots
      (active tabs, open windows, OCR) for every sampled moment. The writer
      synthesizes each hour densely. If it needs on-screen visual detail between
samples it can REQUEST_MORE extra moments (gap-fill vision reuses the
       still-resident vision model, no reload); only hours that actually gained
       a usable extra scene are re-analyzed. Each hour's digest is ALSO fed the
       previous hour's digest so the writer can note continuity or a clear task
       switch across hour boundaries. Then the writer composes the whole day
       into a coherent narrative.

A full day of captures typically finishes in 15-25 minutes (vision ~5-8 min,
hourly writer ~0.5-1 min each, one day-overview pass) when both models fit in
VRAM simultaneously (no swap overhead).

Run it daily/weekly and it catches up on everything new.

Usage:
    python bulk_summary.py                    # summarize every day not done yet
    python bulk_summary.py --all              # regenerate all days
    python bulk_summary.py --since 7          # last 7 days only
    python bulk_summary.py --day 2026-09-01   # one day
    python bulk_summary.py --sample-min 20    # sample every N minutes (default 20)
    python bulk_summary.py --vision huihui_ai/qwen3-vl-abliterated:latest   # use larger vision model
    python bulk_summary.py --writer huihui_ai/qwen3-abliterated:14b-v2    # use larger writer model
    python bulk_summary.py --day 2026-09-01 --outdir D:\\ab\\4b   # isolate all outputs
    python bulk_summary.py --shutdown                     # shut down the PC when done
    python bulk_summary.py --shutdown --shutdown-grace 60 # ... with a 1 minute grace

Summaries are written to <appdata>/openrecall/summaries/YYYY-MM-DD.md
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Both defaults are the current-generation abliterated Qwen3.5 family (vision +
# + text in one architecture): the 9b model is used for BOTH vision and writer.
# The 9b describes scenes correctly (the 4b hallucinated the user's Minecraft
# wallpaper as real gameplay) and runs at the same warm speed as the 4b; using
# one model for the whole pipeline keeps a single model resident for the entire
# day -- nothing is ever unloaded mid-run. Measured on this machine: gen ~96-99
# tok/s, prefill ~1500-1800 tok/s, warm vision ~3-5 s/moment.
DEFAULT_VISION = "huihui_ai/qwen3.5-abliterated:9b"
DEFAULT_WRITER = "huihui_ai/qwen3.5-abliterated:9b"
OLLAMA_URL = "http://localhost:11434/api/chat"

# Vision output cap. The abliterated VL repacks ignore `think: false` and will
# otherwise keep generating (internal reasoning + preamble) until they hit the
# budget -- a scene that only needs ~800 chars can burn the full 4096-token
# budget (~40 s). 512 tokens (~0.7k chars) covers the M-block scene plus FOCUS/
# TAGS for the 640px input (measured ~250-300 tok replies); qwen3.5 replies in
# one pass, so a tight cap just bounds worst-case generation without clipping.
_vision_predict = 512

# Keep loaded models resident between calls (4 min, Ollama's keep-alive lease).
# We free both models explicitly at the end of a day.
KEEP_ALIVE_S = 240

# The user's desktop wallpaper (visible on any monitor that a window isn't
# covering). A screen that matches the wallpaper reference image (see
# WALLPAPER_REF_PATH) is treated as an empty/blank screen: it is NEVER sent to
# the vision model (no more money/VRAM/context wasted describing wallpaper) and
# its block is written locally as "Screen contains no meaningful content.".
# What the art depicts is irrelevant to the day log, so it is never described.
WALLPAPER_OUT = "Screen contains no meaningful content."

# Wallpaper-reference filter. A monitor's screenshot counts as wallpaper when it
# is at least WALLPAPER_SIM_THRESHOLD similar to the reference image, using the
# SAME metric the capture loop applies (MSSIM from openrecall.similarity).
WALLPAPER_SIM_THRESHOLD = 0.90

# Synthetic scene for a moment where EVERY screen is just the empty desktop.
# Scanning blank wallpaper is a waste of a vision pass and tempts the model to
# invent activity from the background art, so `describe_batch` writes this scene
# locally instead of calling the model.
IDLE_FOCUS_LINE = "FOCUS: no meaningful on-screen content (all screens match the wallpaper reference)"
IDLE_TAGS = "IDLE"

# Gap-fill budget: the writer may REQUEST at most this many extra vision scenes
# per hour and across the whole day, so a full re-analysis stays under one hour.
MAX_EXTRA_PER_HOUR = 2
MAX_EXTRA_PER_DAY = 12

# Vision cache format version. Bump when the description format changes so old
# cached entries are treated as missing and re-described on the next full scan.
VISION_FMT = 4

# openrecall/config.py parses sys.argv at import; strip our flags first.
_ap = argparse.ArgumentParser(description="OpenRecall day summary", add_help=False)
_ap.add_argument("--day", default=None)
_ap.add_argument("--since", type=int, default=None)
_ap.add_argument("--all", action="store_true")
_ap.add_argument("--no-today", action="store_true",
                 help="never include the current day when scanning dates "
                      "(skips half-finished / premature days by default)")
_ap.add_argument("--sample-min", type=int, default=20)
_ap.add_argument("--vision", default=DEFAULT_VISION)
_ap.add_argument("--writer", default=DEFAULT_WRITER)
_ap.add_argument("--ctx", type=int, default=16384,
                     help="writer context window; even the full-day overview only "
                          "runs ~8-10k tokens (measured)")
_ap.add_argument("--vision-ctx", type=int, default=4096,
                     help="vision context window; a 3-monitor 640px trio measures "
                          "~700-1100 tokens of image+prompt, so 4096 is ample "
                          "and keeps the KV cache tiny")
_ap.add_argument("--out", default=None)
_ap.add_argument("--outdir", default=None)
_ap.add_argument("--vision-only", action="store_true")
_ap.add_argument("--writer-only", action="store_true")
_ap.add_argument("--profile", default=None)
_ap.add_argument("--shutdown", action="store_true",
                 help="shut down the PC once every pending day has been "
                      "summarized (only shuts down if nothing failed)")
_ap.add_argument("--shutdown-grace", type=int, default=120,
                 help="seconds of grace before shutdown (default 120)")
_ap.add_argument("--help", action="store_true")

_ns, _ = _ap.parse_known_args()
if _ns.help:
    print(__doc__)
    sys.exit(0)

_known = ("--day", "--since", "--sample-min", "--vision", "--writer", "--ctx",
          "--vision-ctx", "--out", "--outdir", "--profile", "--shutdown-grace")
_flag_no_value = ("--all", "--vision-only", "--writer-only", "--shutdown", "--help")
_filtered, _skip = [], 0
for _tok in list(sys.argv[1:]):
    if _skip > 0:
        _skip -= 1
        continue
    if _tok in _flag_no_value:
        continue
    if _tok in _known:
        _skip = 1
        continue
    _filtered.append(_tok)
sys.argv = [sys.argv[0]] + _filtered

from openrecall.config import SESSION_GAP_HOURS, appdata_folder, screenshots_path  # noqa: E402
from openrecall.database import (  # noqa: E402
    create_db,
    get_entries_for_session,
    get_sessions,
)

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

try:
    import numpy as np
except ImportError:
    np = None

from openrecall.similarity import mean_structured_similarity_index  # noqa: E402

SUMMARIES_DIR = os.path.join(appdata_folder, "summaries")
if getattr(_ns, "outdir", None):
    # Isolate all outputs (vision cache, raw descriptions, .md) into a custom
    # folder so parallel/alternative AI runs don't collide on the same files.
    SUMMARIES_DIR = os.path.abspath(_ns.outdir)
DEFAULT_PROFILE_PATH = os.path.join(SUMMARIES_DIR, "user_profile.md")

# The reference wallpaper image used by the wallpaper filter (compare a screen's
# screenshot against this; a match >= WALLPAPER_SIM_THRESHOLD means "no
# meaningful content" and the image is never sent to the vision model). Lives in
# the data root so a temp-file cleanup can't accidentally delete it.
WALLPAPER_REF_PATH = os.path.join(appdata_folder, "wallpaper_reference.webp")


def _load_profile(profile_path: str | None) -> str:
    path = profile_path or (DEFAULT_PROFILE_PATH if os.path.exists(DEFAULT_PROFILE_PATH) else None)
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


_profile_path = _ns.profile
_writer_ctx = _ns.ctx
_vision_ctx = _ns.vision_ctx


def ensure_server(url=OLLAMA_URL, retries: int = 25) -> None:
    """Makes sure the Ollama HTTP server is up, starting 'ollama serve' if needed."""
    ping = url.replace("/api/chat", "/api/version")
    for _ in range(retries):
        try:
            urllib.request.urlopen(ping, timeout=3)
            return
        except Exception:
            pass
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
        time.sleep(2)
    try:
        urllib.request.urlopen(ping, timeout=5)
    except Exception as e:
        raise RuntimeError(f"Could not reach Ollama at {url}. Is it installed? ({e})")


def unload_model(model: str, url=OLLAMA_URL) -> None:
    """Frees a loaded model from memory (keep_alive=0) so the next model can load."""
    try:
        payload = {"model": model, "prompt": "", "keep_alive": 0}
        req = urllib.request.Request(
            url.replace("/api/chat", "/api/generate"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=60)
    except Exception:
        pass


def ollama(model: str, messages, images=None, retries: int = 3, num_ctx: int = 16384,
           num_predict: int = 4096) -> str:
    """Calls the Ollama chat API (optionally with base64 images). Returns reply text."""
    if images:
        messages = list(messages)
        messages = messages[:-1] + [{"role": "user", "content": messages[-1]["content"], "images": images}]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": KEEP_ALIVE_S,
        "options": {"temperature": 0.3, "num_ctx": num_ctx, "num_predict": num_predict},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if "error" in body:
                raise RuntimeError(f"Ollama error: {body['error']}")
            return body.get("message", {}).get("content", "").strip()
        except (urllib.error.URLError, RuntimeError, TimeoutError, socket.timeout) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Ollama call failed after {retries} tries: {last}")


_b64_cache: dict = {}


def image_b64(path: str, max_dim: int = 768, quality: int = 80) -> str:
    """Downscales and encodes an image to base64 to keep vision context small."""
    key = (path, max_dim, quality)
    if key in _b64_cache:
        return _b64_cache[key]
    if PILImage is not None:
        try:
            img = PILImage.open(path)
            img = img.convert("RGB")
            img.thumbnail((max_dim, max_dim))
            buf = __import__("io").BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            out = base64.b64encode(buf.getvalue()).decode("ascii")
            _b64_cache[key] = out
            return out
        except Exception:
            pass
    with open(path, "rb") as f:
        raw = f.read()
    out = base64.b64encode(raw).decode("ascii")
    _b64_cache[key] = out
    return out


def _clear_b64_cache() -> None:
    _b64_cache.clear()


def batches_for_day(entries, sample_min: int):
    """Returns (sampled_batches, by_ts) for a day.

    Each batch = {timestamp, monitors:[{idx,path,app,title,text}]}.
    by_ts maps every timestamp with a usable screenshot to its entry list, so the
    writer can build its minute-level activity rollup over the WHOLE hour (every
    timestamp, not just the sampled moments).

    Sampling aims for roughly one full multi-monitor moment every sample_min
    minutes, preferring the moment closest to each window's midpoint.
    """
    by_ts: dict = {}
    for e in entries:
        if not (e.filename and os.path.exists(os.path.join(screenshots_path, e.filename))):
            continue
        by_ts.setdefault(e.timestamp, []).append(e)

    if not by_ts:
        return [], {}

    ts_list = sorted(by_ts.keys())
    span = (ts_list[-1] - ts_list[0]) / 3600.0  # hours
    n_windows = max(1, int(span * 60 / max(1, sample_min)))
    if n_windows <= 1:
        return [_make_batch(by_ts, ts_list[-1])], by_ts

    picked = []
    for w in range(n_windows):
        lo = ts_list[0] + (w * span * 3600) / n_windows
        mid = lo + (span * 3600) / (2 * n_windows)
        chosen = min(ts_list, key=lambda t: abs(t - mid))
        if all(abs(c - chosen) > 1 for c in picked):
            picked.append(chosen)
    for extreme in (ts_list[0], ts_list[-1]):
        if all(abs(c - extreme) > 1 for c in picked):
            picked.append(extreme)

    picked = sorted(picked)
    return [_make_batch(by_ts, t) for t in picked], by_ts


_NOISE_APPS = {"razerappengine.exe"}
_NOISE_TITLES = {"rzmonitorforegroundwindow"}


def _is_noise_window(app="", title=""):
    """Background overlay windows (e.g. Razer's invisible monitor overlay) that
    must never reach the AI prompts -- they are not things the person did."""
    return ((app or "").strip().lower() in _NOISE_APPS
            or (title or "").strip().lower() in _NOISE_TITLES)


def _clean_windows(wins):
    """Drops overlay/noise windows (e.g. RazerAppEngine) from a windows list."""
    return [w for w in wins
            if not _is_noise_window(w.get("app"), w.get("title"))]


def _json_list(value) -> list:
    """Parses a stored JSON TEXT column (or already-parsed list) to a list."""
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return value if isinstance(value, list) else []


def _dedupe_records(items, keys):
    """Merges records that share the same key values (first one wins)."""
    seen, out = set(), []
    for r in items:
        key = tuple(str(r.get(k) or "").strip().lower() for k in keys)
        if key in seen or not any(key):
            continue
        seen.add(key)
        out.append(r)
    return out


def _make_batch(by_ts, ts):
    entries = by_ts[ts]
    monitors = []
    for e in sorted(entries, key=lambda x: x.monitor if x.monitor is not None else 0):
        path = os.path.join(screenshots_path, e.filename)
        app, title = e.app or "", e.title or ""
        if _is_noise_window(app, title):
            app, title = "", ""
        monitors.append(
            {
                "idx": e.monitor if e.monitor is not None else 0,
                "path": path,
                "app": app,
                "title": title,
                "text": (e.text or "").strip().replace("\n", " ")[:2000],
                "url": (e.browser_url or "").strip(),
                "screen_x": e.screen_x if e.screen_x is not None else 0,
                # Per-screen OS context for this exact monitor's row.
                "tabs": _json_list(e.browser_tabs),
                "windows": _clean_windows(_json_list(e.windows)),
            }
        )
    # Desktop-wide context (merged across the moment's rows; each record keeps
    # its per-screen ``monitor`` tag so the prompt can name the screen).
    tabs, wins = [], []
    for e in entries:
        tabs.extend(_json_list(e.browser_tabs))
        wins.extend(_clean_windows(_json_list(e.windows)))
    return {
        "timestamp": ts,
        "monitors": monitors,
        "tabs": _dedupe_records(tabs, ("title", "url")),
        "windows": _dedupe_records(wins, ("app", "title")),
    }


_BLOCK_HEAD_RE = re.compile(r"^M(\d+)\s*(?:\(((?:[^()]|\([^()]*\))*)\))?\s*:\s*(.*)$")
_FOCUS_RE = re.compile(r"^FOCUS\s*:\s*(.*)$", re.I)
_TAGS_RE = re.compile(r"^TAGS\s*:\s*(.*)$", re.I)


def _file_hash(path: str) -> str | None:
    """md5 of a screenshot file, or None if unreadable."""
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return None


def _split_blocks(description: str) -> dict:
    """Parses an 'M<idx> (role): ...' description into {idx: block_text}.

    Blocks are one-per-monitor lines; a block may run over extra lines until the
    next M/FOCUS/TAGS line. FOCUS/TAGS lines are dropped from the blocks.
    """
    blocks: dict = {}
    cur = None
    first_idx = None
    order = []
    for ln in description.splitlines():
        ln = ln.rstrip()
        if not ln.strip():
            continue
        bm = _BLOCK_HEAD_RE.match(ln.strip())
        sm = _FOCUS_RE.match(ln.strip())
        tm = _TAGS_RE.match(ln.strip())
        if bm:
            idx = int(bm.group(1))
            blocks.setdefault(idx, bm.group(3).strip())
            cur = idx
            if cur not in order:
                order.append(cur)
            first_idx = first_idx if first_idx is not None else idx
        elif sm or tm:
            cur = None
        elif cur is not None:
            blocks[cur] = blocks[cur] + " " + ln.strip()
    return blocks


def _reprefix(description: str, when: str) -> str:
    """Replaces the leading [HH:MM] marker of a stored description."""
    m = re.match(r"^\[(\d{2}):(\d{2})\]\s*", description)
    if m:
        return f"[{when}] " + description[m.end():]
    return f"[{when}] {description}"


def _is_wallpaper_screen(m: dict) -> bool:
    """Programmatic check that a monitor shows only the empty desktop.

    True when no window is attributed to the screen (app/title/url all blank).
    With the area-based window attribution in ``utils.get_top_window_per_monitor``
    + ``monitor_for_rect_area``, a screen whose app/title/url are empty has NO
    window actually covering it, so it necessarily shows only the user's
    wallpaper -- whatever the wallpaper art happens to look like. A full-screen
    game or video always carries an app/title, so it is never mislabelled.
    """
    return (not (m.get("app") or "").strip()
            and not (m.get("title") or "").strip()
            and not (m.get("url") or "").strip())


_reference_cached: dict = {}


def _reference_small(ref_path: str):
    """Lazily loads + normalizes the wallpaper reference (downscaled RGB numpy
    array). Returns None if unavailable."""
    if ref_path in _reference_cached:
        return _reference_cached.get(ref_path)
    _reference_cached[ref_path] = None
    if PILImage is None or np is None or not os.path.exists(ref_path):
        return None
    try:
        img = PILImage.open(ref_path).convert("RGB").resize((192, 108), PILImage.BILINEAR)
        _reference_cached[ref_path] = np.asarray(img, dtype=np.float32)
        return _reference_cached[ref_path]
    except Exception:
        return None


def wallpaper_similarity(path: str) -> float:
    """0..1 similarity of a screenshot to the wallpaper reference.

    Both images are downscaled to the same size and compared with the SAME
    metric OpenRecall's capture loop uses for change detection
    (mean_structured_similarity_index / MSSIM, from openrecall.similarity).
    Returns 0.0 when the image or reference is unavailable, so "unknown" never
    counts as wallpaper. This is the number the WALLPAPER_SIM_THRESHOLD is
    compared against; the calibration tool reuses it so tuning is one-to-one.
    """
    ref = _reference_small(WALLPAPER_REF_PATH)
    if ref is None or np is None or not os.path.exists(path):
        return 0.0
    try:
        img = PILImage.open(path).convert("RGB").resize((192, 108), PILImage.BILINEAR)
        img = np.asarray(img, dtype=np.float32)
    except Exception:
        return 0.0
    if img.shape != ref.shape:
        return 0.0
    return max(0.0, float(mean_structured_similarity_index(ref, img)))


def _matches_wallpaper_ref(path: str) -> bool:
    """Pixel-level check: is this screen essentially the user's wallpaper?

    A score at or above WALLPAPER_SIM_THRESHOLD means the screen shows no
    meaningful content beyond the wallpaper and must NEVER be sent to the vision
    model.
    """
    return wallpaper_similarity(path) >= WALLPAPER_SIM_THRESHOLD


# Per-run memory of what the vision model already described, keyed by the md5 of
# the exact screenshot bytes. The capture loop reuses the SAME screenshot file
# for a monitor that didn't change (MSSIM>=0.9), so a repeated moment is pixel-
# identical to an earlier one and cheap to reuse instead of re-describing.
#   _known_blocks: idx -> (file_hash, block_body)  (last body we wrote for that screen)
#   _scene_cache:  frozenset((idx, file_hash)...) -> full description body
_known_blocks: dict = {}
_scene_cache: dict = {}


def describe_batch(vision_model, batch, known_blocks=None) -> dict:
    """Stage 1: vision model synthesizes this full multi-monitor moment.

    Returns a dict with keys: timestamp, when, description, fmt.
    The vision model sees ALL monitors at once and describes EACH screen in its
    own block, one ``M<idx> (<role>):`` line per monitor, plus a one-line FOCUS
    and TAGS. The per-screen text is what the web UI shows next to each monitor
    image once the moment is described. The writer stage reuses the same scenes
    (whole description) when composing each hour's digest.

    Unchanged screens are skipped: when a monitor's screenshot file is
    byte-identical to one already described in this run, its block text is
    reused verbatim and only the CHANGED screens are shown to the model. When
    the whole multi-monitor fingerprint matches an earlier moment, the full
    description is recycled with no model call at all.

    Empty-desktop (wallpaper-only) screens are detected locally -- no window is
    attributed to them, or their screenshot matches the wallpaper reference
    (>= WALLPAPER_SIM_THRESHOLD) -- and excluded the same way: their block is
    written in code as "Screen contains no meaningful content.", their image is
    never sent to the model, and a moment where EVERY screen is empty skips the
    vision call completely (``IDLE_FOCUS``/``IDLE_TAGS`` scene). The wallpaper
    art is never named or described anywhere in the prompts or output.
    """
    when = datetime.datetime.fromtimestamp(batch["timestamp"]).strftime("%H:%M")
    known = known_blocks if known_blocks is not None else _known_blocks

    monitors = batch["monitors"]
    # Byte-level fingerprint of this moment's screens.
    hashes = {}
    for m in monitors:
        hashes[m["idx"]] = _file_hash(m["path"])
    fp = frozenset((m["idx"], hashes[m["idx"]]) for m in monitors if hashes[m["idx"]])
    if len(fp) == len(monitors):
        cached_body = _scene_cache.get(fp)
        if cached_body:
            return {"timestamp": batch["timestamp"], "when": when, "fmt": VISION_FMT,
                    "description": _reprefix(cached_body, when), "reused": True}

    # Determine the physical layout: monitors ordered left->right by their
    # screen_x coordinate. The Windows PRIMARY monitor is the one whose
    # horizontal span contains the virtual origin (0,0) -- equivalently the
    # monitor whose left edge is the last one <= 0 (monitors tile contiguously).
    # We CANNOT key that on the DB monitor index: the index order that mss
    # enumerates is not stable across restarts, so monitor 2 may be the center
    # on one boot and something else later. Physical position is the ground
    # truth. The primary gets the PRIMARY role; the others are named LEFT/
    # CENTER/RIGHT by where they sit.
    ordered = sorted(monitors, key=lambda m: (m.get("screen_x") if m.get("screen_x") is not None else 0, m["idx"]))
    lefts = [m.get("screen_x") if m.get("screen_x") is not None else 0 for m in ordered]
    prim_pos = 0
    if lefts:
        for i, x in enumerate(lefts):
            nxt = lefts[i + 1] if i + 1 < len(lefts) else float("inf")
            if x <= 0 < nxt:
                prim_pos = i
                break
        else:
            prim_pos = min(range(len(lefts)), key=lambda i: abs(lefts[i]))
    roles = {}
    for pos, m in enumerate(ordered):
        if pos == prim_pos:
            roles[m["idx"]] = "PRIMARY (main)"
        elif pos == 0:
            roles[m["idx"]] = f"LEFT monitor ({m['idx']})"
        elif pos == len(ordered) - 1:
            roles[m["idx"]] = f"RIGHT monitor ({m['idx']})"
        else:
            roles[m["idx"]] = f"CENTER monitor ({m['idx']})"

    layout_parts = []
    for m in ordered:
        role = roles[m["idx"]]
        layout_parts.append(f"[{m['idx']}] {role}")
    layout_desc = " | ".join(layout_parts)

    monitor_info = []
    # Screens that show only the empty desktop: detected TWO ways -- programmatic
    # (no window attributed to the screen) OR pixel-level (the screenshot matches
    # the wallpaper reference image). Their block is written locally (never by
    # the model), their image is not sent, and the model is told to just
    # reproduce the line.
    wallpaper = {m["idx"] for m in ordered
                 if _is_wallpaper_screen(m) or _matches_wallpaper_ref(m["path"])}
    excluded_lines = []
    # Screens that get a fresh image and a model description, left-to-right.
    # Empty-desktop screens and byte-identical reused screens are excluded: no
    # image is attached and the model is told to reproduce the given line, so a
    # wallpaper screen can no longer be hallucinated into active content.
    fresh = [m for m in ordered if m["idx"] not in wallpaper
             and not (known.get(m["idx"])
                      and known[m["idx"]][0] == hashes.get(m["idx"]))]
    if not fresh and len(wallpaper) != len(ordered):
        # Every non-wallpaper screen is already described this run and only the
        # fingerprint cache is cold; fall back to re-sending the NON-wallpaper
        # screens so the model can still refresh FOCUS/TAGS. Wallpaper screens
        # are handled entirely in code and NEVER sent to the model.
        fresh = [m for m in ordered if m["idx"] not in wallpaper]

    for m in ordered:
        role = roles.get(m["idx"], f"monitor {m['idx']}")
        url_part = f" url={m['url']}" if m.get("url") else ""
        monitor_info.append(
            f"M{m['idx']} ({role}) app={m['app']} title=\"{m['title']}\"{url_part}"
        )
        if m["idx"] in wallpaper:
            continue
        if m["idx"] not in {f["idx"] for f in fresh}:
            prev = known.get(m["idx"])
            if prev and prev[0] == hashes.get(m["idx"]):
                excluded_lines.append((m["idx"], role, prev[1]))
    monitors_block = "\n".join(monitor_info) if monitor_info else "No monitor data."

    if excluded_lines:
        hint_lines = [f"M{idx} ({role}): {body}" for idx, role, body in excluded_lines]
        excluded_note = (
            "\nEXCLUDED SCREENS (no image attached: pixel-identical to a moment "
            "already seen this run): reproduce these "
            "lines VERBATIM, do not re-describe them from imagination:\n"
            + "\n".join(hint_lines)
            + "\n"
        )
    else:
        excluded_note = ""

    img_note = ""
    if fresh:
        img_note = ("\nAttached images (left-to-right) are the following monitors: "
                    + ", ".join(f"M{m['idx']} ({roles[m['idx']]})" for m in fresh)
                    + ".\n")
    fmt_lines = "\n".join(
        f"M{m['idx']} ({roles[m['idx']]}): <what is happening on monitor {m['idx']}>"
        for m in fresh
    )
    system = (
        "You are the eyes of a private desktop recorder. Multiple monitor "
        "screenshots were captured at the SAME instant, one image per monitor. "
        "Describe each monitor in its OWN M<idx> block -- never merge screens.\n"
        "Rules:\n"
        "- Name the EXACT app, website, game, or video you can SEE on each monitor "
        "(read visible titles/text). Use the OS hint only to confirm naming.\n"
        "- One block per monitor, 2-4 factual sentences about THAT screen only: what "
        "is shown and what the person is doing there.\n"
        "- No preamble, no thinking, no chain-of-thought, no fluff.\n"
        "- The OS window/tab lists travel separately to the report writer, so do NOT "
        "enumerate windows or tabs -- just what is visually happening.\n"
        "- EXCLUDED SCREENS: if the prompt lists a screen as excluded (no image "
        "attached) with its text, COPY that line EXACTLY (same words).\n"
        "- Adult content: if sexual/porn content is visible or implied, describe it "
        "plainly and specifically -- never sanitize, never refuse. Private local journal.\n\n"
        "Answer directly with EXACTLY this format, one line per monitor with an image "
        "(real roles below), then FOCUS and TAGS:\n" + fmt_lines + "\n"
        "FOCUS: <the single activity the person is focused on, and on which monitor>\n"
        "TAGS: <comma-separated tags, e.g. GAMING, ADULT, SOCIAL_MEDIA, WORK, BROWSING, "
        "WATCHING, CODING, CHATTING, MASTURBATION>\n"
        "Describe every monitor whose image is attached; skip none."
    )
    user = (
        f"It is {when}. The person is at their computer with multiple monitors.\n"
        f"Layout (left to right): {layout_desc}\n\n"
        f"OS hint per screen:\n{monitors_block}\n\n"
        f"{excluded_note}"
        f"{img_note}"
        "Look at the screenshots and describe EACH screen that has an image attached "
        "in its own M<idx> block, reproduce the EXCLUDED lines, then FOCUS and TAGS."
    )

    images = [image_b64(m["path"], max_dim=640, quality=75) for m in fresh]

    # All screens are empty-desktop wallpaper: nothing to scan. Write the scene
    # locally instead of paying a vision pass to describe blank desktops (and
    # give the model no chance to invent activity from the background art).
    if not images:
        if len(wallpaper) == len(ordered):
            merged_lines = [
                f"M{m['idx']} ({roles[m['idx']]}): {WALLPAPER_OUT}" for m in ordered
            ]
            merged_lines.append(IDLE_FOCUS_LINE)
            merged_lines.append(f"TAGS: {IDLE_TAGS}")
            merged = "\n".join(merged_lines)
            for m in ordered:
                known[m["idx"]] = (hashes.get(m["idx"]), WALLPAPER_OUT)
            if len(fp) == len(monitors):
                _scene_cache[fp] = merged
            return {"timestamp": batch["timestamp"], "when": when, "fmt": VISION_FMT,
                    "description": f"[{when}] {merged}"}
        non_wall = [m for m in ordered if m["idx"] not in wallpaper]
        if non_wall:
            images = [image_b64(non_wall[0]["path"], max_dim=640, quality=75)]

    raw = ollama(vision_model, [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                 images=images, num_ctx=_vision_ctx, num_predict=_vision_predict)
    if not raw.strip():
        # Some small/VL abliterated models blank out and emit nothing. Retry with a
        # short, blunt prompt (same images); a blank reply is flaky per call, so
        # loop a few times -- each attempt costs a fraction of a day.
        short_sys = (
            "You are the eyes of a private desktop recorder. Look at ALL the monitor images "
            "shown simultaneously and describe what is happening on EACH screen, naming the "
            "exact sites, apps, games and videos. Include adult/porn content uncensored. "
            "ANSWER DIRECTLY: no thinking, no preamble -- start each screen with: "
            "M<index>:"
        )
        short_user = (
            f"It is {when}. Layout (left to right): {layout_desc}.\n\n{monitors_block}\n\n"
            f"{excluded_note}"
            "Describe each monitor separately."
        )
        for _ in range(3):
            raw = ollama(vision_model, [{'role': 'system', 'content': short_sys},
                                        {'role': 'user', 'content': short_user}],
images=images, num_ctx=_vision_ctx, num_predict=_vision_predict)
            if raw.strip():
                break

    # Merge model output with verbatim reused blocks and locally-written wallpaper
    # lines, then remember what we wrote so the NEXT unchanged moment is a free
    # cache hit. A wallpaper screen is ALWAYS written by us, never by the model.
    body = raw.strip()
    parsed = _split_blocks(body) if body else {}
    merged_lines = []
    for m in ordered:
        idx = m["idx"]
        prev = known.get(idx)
        if _is_wallpaper_screen(m) or _matches_wallpaper_ref(m["path"]):
            wallpaper_body = WALLPAPER_OUT
            merged_lines.append(f"M{idx} ({roles[idx]}): {wallpaper_body}")
            known[idx] = (hashes.get(idx), wallpaper_body)
        elif prev and prev[0] == hashes.get(idx):
            merged_lines.append(f"M{idx} ({roles[idx]}): {prev[1]}")
        else:
            new_body = parsed.get(idx, '').strip()
            merged_lines.append(f"M{idx} ({roles[idx]}): {new_body}")
            known[idx] = (hashes.get(idx), new_body)
    for ln in body.splitlines():
        s = ln.strip()
        if _FOCUS_RE.match(s) or _TAGS_RE.match(s):
            merged_lines.append(s)
    merged = "\n".join(l for l in merged_lines if not l.endswith(": "))
    if len(fp) == len(monitors):
        _scene_cache[fp] = merged
    return {"timestamp": batch["timestamp"], "when": when, "fmt": VISION_FMT,
            "description": f"[{when}] {merged}"}


def hour_key(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:00")


def group_by_hour(batches):
    """Group sampled moments into (hour_label, [batch...]) ordered by time.

    Each hour's digest must cover the WHOLE hour, but the last sampled moment of
    an hour usually lands ~15-20 min before the :00 boundary (e.g. 00:42), and
    the next hour's first sample is a couple of minutes IN to the next hour --
    so a strict per-hour bucket would silently skip the tail of the hour. The
    FIRST moment of the following hour is therefore appended to the current
    hour's group too: it is the closest sample describing this hour's last
    minutes (e.g. 00:42 -> 01:03), and it may legitimately appear in BOTH
    hours' digests. The last hour keeps only its own moments.
    """
    groups = {}
    for b in batches:
        groups.setdefault(hour_key(b["timestamp"]), []).append(b)
    ordered = sorted(groups.items(), key=lambda kv: kv[0])
    out = []
    for i, (label, group) in enumerate(ordered):
        own = list(group)
        if i + 1 < len(ordered) and ordered[i + 1][1]:
            own.append(ordered[i + 1][1][0])
        out.append((label, own))
    return out


def _ts_str(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def _when_key(desc):
    m = re.search(r"\[\s*(\d{2}):(\d{2})\]", desc)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 0


def parse_requests(reply: str) -> list:
    """Extracts the hour:minute list from a 'REQUEST_MORE: HH:MM, HH:MM' line."""
    if not reply:
        return []
    out = []
    for ln in reply.splitlines():
        if ln.strip().upper().startswith("REQUEST_MORE"):
            for tok in ln.split(":", 1)[-1].split(","):
                tok = tok.strip()
                if re.fullmatch(r"\d{2}:\d{2}", tok):
                    out.append(tok)
    return out


def resolve_request_time(day, hhmm, by_ts) -> int:
    """Find the closest timestamp within ±1 hour of the requested HH:MM."""
    dt = datetime.datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")
    target = dt.timestamp()
    candidates = [t for t in by_ts if target - 3600 <= t < target + 3600]
    if not candidates:
        raise ValueError(f"no moments near {day} {hhmm}")
    return min(candidates, key=lambda t: abs(t - target))


def midpoint_requests(hour_label, group, by_ts) -> list:
    """Times to fill when a first-pass digest came back blank: the midpoint of
    every >20-minute gap between sampled moments in the hour."""
    ts_list = sorted(b["timestamp"] for b in group)
    hour_start = datetime.datetime.strptime(hour_label, "%Y-%m-%d %H:%M").timestamp()
    hour_end = hour_start + 3600
    avail = sorted(t for t in by_ts if hour_start <= t < hour_end)
    picks = []
    for lo, hi in zip(ts_list, ts_list[1:]):
        if hi - lo > 60 * 20 and avail:
            span = [t for t in avail if lo < t < hi]
            if span:
                picks.append(min(span, key=lambda t: abs(t - (lo + hi) / 2)))
    if not picks and ts_list:
        # Single-moment hour: describe one extra moment elsewhere in the hour.
        span = [t for t in avail if t not in ts_list]
        if span:
            picks.append(span[len(span) // 2])
    return [datetime.datetime.fromtimestamp(t).strftime("%H:%M") for t in picks]


def _primary_entry(entries):
    """The row for the primary/focused screen (the one spanning the virtual
    origin x=0, i.e. physically the Windows PRIMARY monitor), not the row with
    the lowest DB index -- mss's index order is not stable across restarts."""
    ordered = sorted(entries, key=lambda e: (e.screen_x if e.screen_x is not None else 0,
                                             e.monitor if e.monitor is not None else 0))
    if not ordered:
        return None
    lefts = [e.screen_x if e.screen_x is not None else 0 for e in ordered]
    for i, x in enumerate(lefts):
        nxt = lefts[i + 1] if i + 1 < len(lefts) else float("inf")
        if x <= 0 < nxt:
            return ordered[i]
    return ordered[min(range(len(ordered)), key=lambda i: abs(lefts[i]))]


def _rollup(by_ts, hour_label):
    """Compact per-minute activity line for every timestamp within one hour, consecutive identical states collapsed."""
    hour_start = datetime.datetime.strptime(hour_label, "%Y-%m-%d %H:%M").timestamp()
    hour_end = hour_start + 3600
    lines, last_active = [], None
    for ts in sorted(ts for ts in by_ts if hour_start <= ts < hour_end):
        active = _primary_entry(by_ts[ts])
        if _is_noise_window(active.app or "", active.title or ""):
            continue
        cur = (active.app or "", active.title or "", (active.browser_url or "")[:64])
        if cur == last_active:
            continue
        label = f"{_ts_str(ts)} {(active.app or 'unknown').strip()}"
        if active.title:
            label += f" / \"{active.title.strip()}\""
        if active.browser_url:
            label += f" <- {active.browser_url.strip()[:64]}"
        lines.append(label)
        last_active = cur
    return lines or ["(no moments in this span)"]


def _batch_os_context(batch) -> str:
    """Compact per-moment OS context string (apps, active tabs, open windows, OCR)."""
    lines = [f"{_ts_str(batch['timestamp'])}"]
    for m in sorted(batch["monitors"], key=lambda x: x.get("idx") or 0):
        bits = [f"M{m['idx']}: {m['app'] or '?'}"]
        if m.get("title"):
            bits.append(f"\"{m['title'][:120]}\"")
        if m.get("url"):
            bits.append(f"url={m['url'][:120]}")
        text = (m.get("text") or "").replace("\n", " ").strip()
        if text:
            bits.append(f"OCR={text[:220]}")
        lines.append("  " + " | ".join(bits))
    for w in _dedupe_records(batch.get("windows", []) or [], ("app", "title")):
        if w.get("title"):
            lines.append(f"  WIN {w.get('app') or ''}: \"{w.get('title')[:120]}\"")
    for t in _dedupe_records(batch.get("tabs", []) or [], ("title", "url")):
        t_title = (t.get("title") or "").strip()
        url = (t.get("url") or "").strip()
        lines.append(f"  TAB \"{t_title[:120]}\" ({url[:120]})" if url else
                     f"  TAB \"{t_title[:120]}\"")
    return "\n".join(lines)


def write_hour_breakdown(writer_model, day, hour_label, scenes, hour_batches=None, by_ts=None,
                         prev_hour_text=None, prev_hour_label=None) -> str:
    """Stage 2: writer synthesizes ONE HOUR into a compact, specific digest.

    ``scenes`` is the ordered list of sampled-moment vision descriptions for the
    hour. ``hour_batches`` (optional) is the list of sampled batch dicts whose full
    OS context (apps, active tabs, open windows, OCR) enriches the writer.
    ``by_ts`` (optional) supplies the minute-level activity rollup -- the exact
    per-minute app/title/active-tab-URL for the whole hour (every timestamp, not
    just the sampled ones) -- giving the writer a reliable timing backbone.
    ``prev_hour_text``/``prev_hour_label`` (optional) is the PREVIOUS hour's
    digest, used ONLY to spot continuity or a clear task switch across the hour
    boundary -- never to repeat or invent anything.

    The writer works in a dense single pass; it may end its reply with a
    ``REQUEST_MORE: HH:MM, ...`` line if an on-screen detail between sampled
    moments deserves a vision scene. ``num_predict`` is capped so a busy
    all-monitor hour cannot run away and stays inside the day's time budget.
    """
    profile_text = _load_profile(_profile_path)
    personality = (
        "\n\nSTYLE: Write crisp, factual, plain English. No jokes, no sarcasm, no drama, no "
        "moralizing, no praise, no invented emotion. This is a neutral timeline, not an "
        "editorial. Never describe the user's mood, energy, intentions, attention, or "
        "thoughts -- only observable facts. A window being open is not proof of what the "
        "user felt or wanted.\n"
    )
    system = (
        "You are a personal digital-memory analyst writing the digest for ONE HOUR of a "
        "person's computer activity.\n\n"
        "You receive up to THREE kinds of input:\n"
        "1. A few sampled-moment SCENE descriptions (image-level, ~every 20 min) -- the "
        "PRIMARY source of what was actually happening on screen. Trust these most.\n"
        "2. A minute-level ACTIVITY timeline (every timestamp in the hour, folded into "
        "consecutive identical lines): time + active app + window title + active browser-tab "
        "URL, captured from the OS. Use it ONLY for exact timing and for the exact "
        "app/tab/window names -- not for what the person was doing beyond that name.\n"
        "3. Per-moment OS context (open windows, active browser tabs, OCR) at each sampled "
        "moment -- plain lists of what was open, not an interpretation of it.\n\n"
        "Your job: a COMPACT digest of the hour -- one short bullet per activity block, "
        "opened with the block's time range (e.g. '09:12-09:40') taken from the timeline, "
        "naming the app/site/game and stating what the SCENE text shows was happening. "
        "Ground every 'what' in a scene: never guess what was on screen from a window title "
        "alone.\n\n"
        "RULES:\n"
        "- KEEP the specificity. Never replace exact names with vague categories: 'Reddit "
        "(r/gaming)', 'Elden Ring', 'Site X: <category>, <acts>' -- the exact site and "
        "content, never a sanitized label.\n"
        "- NEVER attribute mood, energy, focus, tiredness, intention, or thoughts to the "
        "person. State only what scenes and OS data show.\n"
        "- Never invent activity, breaks, pauses, or transitions. The scenes show activity, "
        "the timeline shows timing; if a stretch has no moments, omit it.\n"
        "- If a PREVIOUS HOUR digest is supplied, use it ONLY for continuity ('still on the "
        "same task') or a clearly-shown switch; never repeat it and never invent a link "
        "between hours the data does not show.\n"
        "- Adult/porn content: describe it explicitly and uncensored (site, genre, "
        "acts/positions as shown) -- plainly, without metaphor, without moralizing, and "
        "without speculation about why or how it fit the day.\n"
        "Use a few short markdown bullets, no headings.\n"
    )
    if profile_text:
        system += (
            "\n\nUSER PROFILE (optional background only -- never use it to invent activity, "
            "mood, or intent that the data does not show):\n"
            f"{profile_text}"
        )
    system += (
        "\n\nYou may request an additional on-screen moment only when you need to SEE "
        "something the sampled scenes do not cover and the visual detail matters (e.g. a "
        "scene shift between samples that the samples missed). If you want the pipeline to "
        "describe an additional on-screen moment, end your reply with exactly one line: "
        "REQUEST_MORE: HH:MM, HH:MM, ... (times within this hour, formatted like the ACTIVITY "
        "lines). Use no other wording on that line. Omit it entirely if you have enough."
    )
    system += personality

    parts = [f"Day {day}, hour {hour_label}.\n"]
    if scenes:
        parts.append("SAMPLED MOMENT SCENES (image-level, the reliable record of what was on screen):")
        parts.append("\n\n".join(scenes))
        parts.append("")
    if by_ts:
        parts.append("MINUTE-LEVEL ACTIVITY (timing + exact app/tab names, from the OS):")
        parts.append("\n".join(_rollup(by_ts, hour_label)))
        parts.append("")
    if hour_batches:
        parts.append("OS CONTEXT AT SAMPLED MOMENTS (open windows, active tabs, OCR):")
        for b in hour_batches:
            parts.append(_batch_os_context(b))
        parts.append("")
    if prev_hour_text:
        parts.append(
            f"PREVIOUS HOUR digest (what happened right before this hour; use it ONLY to spot "
            f"continuity or a clear task switch -- do not repeat it):\n"
            f"{prev_hour_label}:\n{prev_hour_text.strip()}"
        )
        parts.append("")
    parts.append("Produce the compact digest for this hour (one short bullet per activity block).")
    user = "\n".join(parts).strip()
    return ollama(writer_model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                  num_ctx=16384, num_predict=4096)


def write_day_overview(writer_model, day, hour_breakdowns) -> str:
    """Final: writer composes the DAY OVERVIEW narrative + PATTERNS.

    The SCHEDULE section is NOT written here -- `process_day` assembles it in
    code from the per-hour digests (exact time blocks) and the real captured
    timestamps (exact no-data spans), so hour markers and gaps are always right.
    """
    blocks = "\n\n".join(f"## {label}\n{text}" for label, text in hour_breakdowns)
    profile_text = _load_profile(_profile_path)
    personality = (
        "\n\nSTYLE: Write crisp, factual, plain English. No jokes, no sarcasm, no drama, no "
        "moralizing, no praise, no invented emotion. This is a neutral timeline, not an "
        "editorial. Never describe the user's mood, energy, intentions, attention, or "
        "thoughts -- only observable facts from the hourly digests.\n"
    )
    system = (
        "You are a personal digital-memory analyst. You have the hourly digests "
        "of a person's whole day, each listing what was on screen and when.\n\n"
        "You produce the two closing sections of the day report, in this order:\n\n"
        "1. 'DAY OVERVIEW' -- a brief, high-level overview of the day: what it was "
        "mostly spent on and the general shape of the day. Do NOT walk through it "
        "chronologically hour by hour and do NOT list every site/app -- group related "
        "activity and name only the few main themes. Scale the length to how much "
        "actually happened: a short or quiet day gets one or two sentences and a long "
        "day still stays compact; never pad to fill space just because there are many "
        "hours. Include any adult/porn activity plainly and uncensored (what was "
        "watched, roughly how long) as part of the record.\n\n"
        "2. 'PATTERNS' -- 3-5 bullet points, but ONLY patterns that are directly visible in "
        "the digests (e.g. 'Discord DMs with @Incubus from 20:00 to 23:00', 'porn on Site X "
        "every evening'). No speculation about reasons or psychology.\n\n"
        "Use the exact markdown headings '# DAY OVERVIEW' and '# PATTERNS'. "
        "Use bold (**label:**) and bullets so it renders as a readable page: in PATTERNS "
        "start each bullet with a bold **short label** followed by the detail, and bold the "
        "key site/game/app names inside DAY OVERVIEW text.\n\n"
        "Describe everything plainly, including explicit adult content in full, uncensored "
        "detail. This is the user's private record on their own machine -- no censorship."
    )
    if profile_text:
        system += (
            "\n\nUSER PROFILE (optional background only -- never use it to invent activity, "
            "mood, or intent that the digests do not show):\n"
            f"{profile_text}"
        )
    system += personality
    user = (
        f"Day {day} hourly analyses:\n\n{blocks}\n\n"
        "Produce DAY OVERVIEW, then PATTERNS."
    )
    return ollama(writer_model, [{"role": "system", "content": system}, {"role": "user", "content": user}], num_ctx=_writer_ctx)


def _no_data_spans(by_ts, min_gap_s: int = 30 * 60):
    """Real no-data gaps: consecutive captured timestamps more than `min_gap_s`
    apart. Returns [(start_ts, end_ts)] with the actual gap boundaries."""
    ts_list = sorted(by_ts)
    return [(a, b) for a, b in zip(ts_list, ts_list[1:]) if b - a > min_gap_s]


def _hour_floor(ts: int) -> int:
    dt = datetime.datetime.fromtimestamp(ts)
    return int(datetime.datetime(dt.year, dt.month, dt.day, dt.hour).timestamp())


_BLOCK_RE = re.compile(
    r"^\s*(?:[-*\u2022]\s*)?(?:\*\*)?(\d{1,2}:\d{2})\s*[-\u2013\u2014]\s*"
    r"(\d{1,2}:\d{2})\s*(?:\*\*)?\s*[:|]?\s*(.*)$"
)


def _parse_blocks(text) -> list:
    """Turns an hour digest into [{start, end, text}] blocks using its
    'HH:MM-HH:MM ...' time ranges; continuation lines append to the last block."""
    blocks = []
    for ln in text.splitlines():
        m = _BLOCK_RE.match(ln.strip())
        if m:
            blocks.append({"start": m.group(1), "end": m.group(2), "text": m.group(3).strip()})
        elif blocks and ln.strip():
            blocks[-1]["text"] += " " + ln.strip()
    return [b for b in blocks if b["text"]]


def build_schedule(day, hour_breakdowns, by_ts) -> dict:
    """Structured schedule for the day: per-hour time-ranged blocks plus the real
    no-data spans. This is what the web UI uses to show only the current hour's
    schedule while the user browses moments, and what the .md is built from."""
    hours = {}
    for label, text in hour_breakdowns.items():
        blocks = _parse_blocks(text)
        if blocks:
            hh = label.split()[1][:2]
            hours[hh] = blocks
    spans = _no_data_spans(by_ts)
    if by_ts:
        last = max(by_ts)
        spans = [(a, b) for a, b in spans if b < last - 60]
    return {
        "day": day,
        "hours": hours,
        "no_data": [{"start": _ts_str(a), "end": _ts_str(b)} for a, b in spans],
    }


def schedule_md(day, hour_breakdowns, by_ts) -> list:
    """Markdown rows for the SCHEDULE section, one clean '## HH:00 - HH+1:00'
    section per hour in chronological order. A real no-data span (computed from
    the captured timestamps themselves) is shown once, on the first hour it
    touches; the silent hours inside it are omitted so the timeline stays
    readable. Hours with captures but no digest get an honest '[active ..]' row."""
    spans = _no_data_spans(by_ts)
    last = max(by_ts)
    # A span that runs out to the very last capture is the tail of the session
    # (nothing after it to be separated from) -- no hole to report.
    spans = [(sa, sb) for (sa, sb) in spans if sb < last - 60]
    ts_list = sorted(by_ts)
    if not ts_list:
        return []
    cur, last_hour = _hour_floor(ts_list[0]), _hour_floor(ts_list[-1])
    used = set()
    rows = []
    while cur <= last_hour:
        label = datetime.datetime.fromtimestamp(cur).strftime("%Y-%m-%d %H:00")
        hh = int(datetime.datetime.fromtimestamp(cur).strftime("%H"))
        header = f"## {hh:02d}:00 - {(hh + 1) % 24:02d}:00"
        h_end = cur + 3600
        blocks = _parse_blocks(hour_breakdowns.get(label, ""))
        overlaps = [i for i, (sa, sb) in enumerate(spans) if sa < h_end and sb > cur]
        fresh = [i for i in overlaps if i not in used]
        inside_used_span = any(
            i in used and cur >= sa and h_end <= sb for i, (sa, sb) in enumerate(spans)
        )
        if inside_used_span and not blocks:
            cur += 3600
            continue  # silent hour already covered by an emitted [no data ...] line
        if blocks:
            rows.append(header)
            for b in blocks:
                rows.append(f"- **{b['start']} - {b['end']}** | {b['text']}")
            for i in fresh:
                sa, sb = spans[i]
                rows.append(f"[no data {_ts_str(sa)} - {_ts_str(sb)}]")
                used.add(i)
        elif fresh:
            sa, sb = spans[fresh[0]]
            rows.append(header)
            rows.append(f"[no data {_ts_str(sa)} - {_ts_str(sb)}]")
            used.update(fresh)
        elif label in hour_breakdowns:
            rows.append(header)
            rows.append("- " + " ".join(ln.strip() for ln in hour_breakdowns[label].splitlines() if ln.strip()))
        elif any(cur <= t < h_end for t in by_ts):
            lo = _ts_str(min(t for t in by_ts if cur <= t < h_end))
            hi = _ts_str(max(t for t in by_ts if cur <= t < h_end))
            rows.append(header)
            rows.append(f"[active {lo} - {hi}]")
        # else: no captures at all in this hour and no span to report (day
        # boundary/seed) -- omit the heading.
        cur += 3600
    return rows


def find_days_with_entries():
    """Session keys (a "day" = a run of moments with no >2h gap), oldest first.

    Returns start-date based keys, e.g. ``2026-09-13`` or ``2026-09-13_2``.
    """
    return [s.key for s in get_sessions()]


def _load_vision_cache(day):
    """Load cached vision descriptions for a day, keyed by timestamp."""
    p = os.path.join(SUMMARIES_DIR, f"{day}_vision.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}


def _save_vision_cache(day, scenes):
    p = os.path.join(SUMMARIES_DIR, f"{day}_vision.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {str(k): v for k, v in sorted(scenes.items(), key=lambda x: x[0])},
            f, indent=2, ensure_ascii=False,
        )
    os.replace(tmp, p)


def process_day(day, vision, writer, sample_min, overwrite, vision_only=False, writer_only=False, out_name=None):
    _clear_b64_cache()  # image payloads are per-moment; don't grow across days
    same_model = (vision == writer)  # one resident model for the whole run; skip swaps
    entries = get_entries_for_session(day)
    if not entries:
        return False, 0
    os.makedirs(SUMMARIES_DIR, exist_ok=True)
    out = os.path.join(SUMMARIES_DIR, out_name or f"{day}.md")
    if (not vision_only) and os.path.exists(out) and not overwrite:
        return True, len(entries)

    batches, by_ts = batches_for_day(entries, sample_min)
    if not batches:
        return False, len(entries)

    print(f"  {day}: {len(entries)} snapshots, sampling {len(batches)} moment(s) every {sample_min} min")

    # Stage 1: vision describes each sampled moment (full multi-monitor picture),
    # reusing the on-disk cache so already-described moments are skipped. Empty
    # placeholder entries (e.g. "[13:27] " from a model that blanked out) count
    # as NOT done so interrupted/partial runs get re-attempted.
    scenes = _load_vision_cache(day)

    def _cached_ok(ts):
        d = scenes.get(ts)
        if not d:
            return False
        if d.get("fmt") != VISION_FMT:
            return False
        body = (d.get("description") or "").strip()
        return len(body.split("]", 1)[-1].strip()) >= 15

    total = len(batches)
    missing = [b for b in batches if not _cached_ok(b["timestamp"])]
    if missing and len(missing) < len(batches):
        print(f"    {len(batches) - len(missing)} moment(s) cached, {len(missing)} to analyze"
              f" ({sum(1 for b in batches if _cached_ok(b['timestamp']))} usable)")
    else:
        print(f"    Using {len(scenes)} cached moment(s)")

    if not writer_only:
        for i, b in enumerate(batches, 1):
            if _cached_ok(b["timestamp"]):
                print(f"    [{i}/{total}] {datetime.datetime.fromtimestamp(b['timestamp']).strftime('%H:%M')} (cached)")
                continue
            print(f"    [{i}/{total}] Vision analyzing moment at {datetime.datetime.fromtimestamp(b['timestamp']).strftime('%H:%M')} ({len(b['monitors'])} monitor(s))...")
            try:
                result = describe_batch(vision, b)
            except RuntimeError as e:
                print(f"      [ERROR] {e}")
                _save_vision_cache(day, scenes)
                return False, len(entries)
            scenes[b["timestamp"]] = result
            _save_vision_cache(day, scenes)  # incremental cache so interrupted runs resume
            print(f"      SCENE: {result['description'].strip()}")
            print()

    if vision_only:
        print(f"    Vision stage done: {len(scenes)} moment(s), saved to {day}_vision.json")
        unload_model(vision)
        return True, len(entries)

    # Vision goes back to disk here: only ONE model stays resident at a time so
    # the run shares the machine with normal use (games, browsers). The writer
    # stage loads next; gap-fill vision below unloads the writer first and
    # loads vision only for the requests, then unloads it again. When vision
    # and writer are the SAME model this is skipped entirely -- the model stays
    # resident from the first vision call through the final overview.

    if not same_model:
        unload_model(vision)

    # Stage 2: group the sampled moments by hour, then the writer produces each
    # hour's digest in ONE pass. The writer is fed the vision scene descriptions
    # PLUS the exact captured OS context: a minute-level activity rollup (every
    # timestamp of the hour, not just the samples) and the full per-sample app /
    # active-tab / window / OCR context. If the writer needs on-screen visual
    # detail BETWEEN samples it may REQUEST_MORE extra moments (tightly budgeted,
    # only re-analyzing hours that gained a usable scene).
    print("\n  Analysis phase (writer)...\n")
    hour_groups = group_by_hour(batches)
    hour_breakdowns = {}

    def _fresh_batch(ts):
        """A batch dict for a previously-sampled/fresh timestamp, reusing cache."""
        return _make_batch(by_ts, ts)

    def _run_breakdown(hour_label, group, prev_hour_text="", prev_hour_label=""):
        """One writer call for one hour. Returns (text, requests) or (None, [])."""
        hour_descs = [scenes[b["timestamp"]]["description"] for b in group
                      if b["timestamp"] in scenes]
        if not hour_descs:
            print(f"      [SKIP] hour {hour_label} has no described moments "
                  "(run vision stage first); leaving it out of the report.")
            return "", []
        try:
            text = write_hour_breakdown(writer, day, hour_label, hour_descs,
                                        hour_batches=group, by_ts=by_ts,
                                        prev_hour_text=prev_hour_text,
                                        prev_hour_label=prev_hour_label)
        except RuntimeError as e:
            print(f"      [ERROR] hour {hour_label}: {e}")
            unload_model(writer)
            return None, []
        requests = parse_requests(text)
        # The digest must not carry the REQUEST_MORE line into the day report.
        if requests:
            text = "\n".join(ln for ln in text.splitlines()
                             if not ln.strip().upper().startswith("REQUEST_MORE"))
        return text, requests

    def _recheck_breakdown(hour_label, group, extra_ts, prev_hour_text="", prev_hour_label=""):
        """Re-run an hour now that extra gap scenes were added (scenes + context)."""
        all_ts = sorted({b["timestamp"] for b in group} | set(extra_ts))
        hour_descs = [scenes[t]["description"] for t in all_ts]
        return write_hour_breakdown(writer, day, hour_label, hour_descs,
                                    hour_batches=group + [_fresh_batch(t) for t in extra_ts],
                                    by_ts=by_ts,
                                    prev_hour_text=prev_hour_text,
                                    prev_hour_label=prev_hour_label)

    # Phase A: ONE writer pass over every hour (writer loads here, stays
    # resident). Each hour is a stateless call (fresh messages, no growing
    # history), so no reload is needed to "flush context". Each hour after the
    # first also receives the PREVIOUS hour's digest so the writer sees task
    # continuity and transitions across hour boundaries. We remember which hours
    # asked for gap-fill moments (or blanked out) so the vision work below runs
    # as a single batched session with the already-resident vision model.
    extra_used = 0
    extra_this_hour = 0
    pending_gaps = []  # (hour_label, group, [requested HH:MM, ...])
    prev_text, prev_label = "", ""
    for hour_label, group in hour_groups:
        print(f"    Analyzing hour {hour_label} ({len(group)} moment(s))...")
        clean, requests = _run_breakdown(hour_label, group, prev_text, prev_label)
        if clean is None:
            return False, len(entries)
        hour_breakdowns[hour_label] = clean
        if clean:
            prev_text, prev_label = clean, hour_label
        print(f"      Hour breakdown complete ({len(clean)} chars)")
        reqs = requests
        if not reqs and len(clean) < 40:
            reqs = midpoint_requests(hour_label, group, by_ts)
        if not _ns.vision_only and not _ns.writer_only and reqs:
            pending_gaps.append((hour_label, group, reqs))

    # Phase B: describe ALL requested gap-fill moments in one vision session.
    # Only one model is resident at a time: the writer is unloaded first so the
    # vision model can load into the freed memory, then vision is unloaded again
    # before Phase C re-loads the writer.
    wanted = {}  # hour_label -> [gap timestamps actually described]
    extra_scenes = {}  # ts -> description dict, dedup across hours
    if pending_gaps and extra_used < MAX_EXTRA_PER_DAY:
        if not same_model:
            unload_model(writer)
        for hour_label, group, reqs in pending_gaps:
            budget_hour = MAX_EXTRA_PER_HOUR - extra_this_hour
            budget_day = MAX_EXTRA_PER_DAY - extra_used
            slots = max(0, min(budget_hour, budget_day))
            if reqs and slots <= 0:
                print("      Reached daily gap-fill budget; ignoring further requests.")
            hour_ts = {b["timestamp"] for b in group}
            picked = []
            used_now = 0
            for i, r in enumerate(reqs[:slots], 1):
                try:
                    t = resolve_request_time(day, r, by_ts)
                except ValueError:
                    continue
                if t in hour_ts or t in scenes or t in extra_scenes:
                    continue
                picked.append(t)
                print(f"        [{i}/{slots}] Vision analyzing moment at {_ts_str(t)} (gap-fill)...")
                try:
                    extra = describe_batch(vision, _fresh_batch(t))
                except RuntimeError as e:
                    print(f"        [ERROR] gap at {_ts_str(t)}: {e}")
                    continue
                scenes[t] = extra
                extra_scenes[t] = extra
                _save_vision_cache(day, scenes)
                used_now += 1
                print(f"      SCENE: {extra['description'].strip()}")
            extra_this_hour += used_now
            extra_used += used_now
            if picked:
                wanted[hour_label] = picked
        if extra_used:
            print(f"      {extra_used} extra moment(s) requested in total")
        if not same_model:
            unload_model(vision)
        # Writer cleared below before Phase C reloads it.

    # Phase C: re-run (writer reloads after the vision gap-fill session had the
    # memory) every hour that actually gained a usable gap scene. The writer is
    # NOT unloaded between hours or before the day overview, so one daily writer
    # load covers the whole remaining analysis.
    reanalyzed = 0
    for hour_label, group, _reqs in pending_gaps:
        fresh = [t for t in wanted.get(hour_label, []) if t in scenes]
        if not fresh:
            continue
        usable = sum(1 for t in fresh
                     if len((scenes.get(t, {}).get("description") or "").split("]", 1)[-1].strip()) >= 15)
        if not usable:
            continue
        prev_text, prev_label = "", ""
        for plabel, _pg in hour_groups:
            if plabel >= hour_label:
                break
            if hour_breakdowns.get(plabel):
                prev_text, prev_label = hour_breakdowns[plabel], plabel
        print(f"        Analyzing hour {hour_label} ({len(group) + len(fresh)} moment(s), re-analysis)...")
        try:
            new_clean = _recheck_breakdown(hour_label, group, fresh, prev_text, prev_label)
        except RuntimeError as e:
            print(f"      [ERROR] re-analysis hour {hour_label}: {e}")
            new_clean = None
        if new_clean and len(new_clean) > 0:
            # The re-analysis may itself ask for more moments; we already gave
            # the hour its budget, so strip the REQUEST_MORE line.
            new_clean = "\n".join(ln for ln in new_clean.splitlines()
                                  if not ln.strip().upper().startswith("REQUEST_MORE")).strip()
            hour_breakdowns[hour_label] = new_clean
            print(f"      Hour {hour_label} re-analyzed ({len(new_clean)} chars)")
        else:
            print(f"      Hour {hour_label} re-analyzed (0 chars)")
        reanalyzed += 1
    if reanalyzed:
        print(f"      {reanalyzed} hour(s) re-analyzed with extra context")
    print()

    # --- Final: writer produces the full day report (writer stays loaded) ---
    ordered = [(label, hour_breakdowns[label]) for label, _g in hour_groups if label in hour_breakdowns]
    print("    Building comprehensive day schedule and narrative...")
    try:
        overview = write_day_overview(writer, day, ordered)
    except RuntimeError as e:
        print(f"      [ERROR] overview: {e}")
        unload_model(writer)
        return False, len(entries)

    unload_model(writer)  # free memory when done
    unload_model(vision)  # idempotent; already unloaded before the writer

    # Save raw vision descriptions (all sampled moments) to a JSON file for review.
    try:
        raw_path = os.path.join(SUMMARIES_DIR, f"{day}_raw_descriptions.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"timestamp": s["timestamp"], "when": s["when"], "description": s["description"]}
                    for s in sorted(scenes.values(), key=lambda x: x["timestamp"])
                ],
                f,
                indent=2,
                ensure_ascii=False,
            )
        print(f"    Saved raw descriptions to {raw_path}")
    except Exception as e:
        print(f"    [WARN] could not save raw descriptions: {e}")

    # Structured schedule for the web UI (per-hour blocks + exact no-data spans).
    schedule = build_schedule(day, hour_breakdowns, by_ts)
    try:
        sched_path = os.path.join(SUMMARIES_DIR, f"{day}_schedule.json")
        tmp = sched_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(schedule, f, indent=2, ensure_ascii=False)
        os.replace(tmp, sched_path)
    except Exception as e:
        print(f"    [WARN] could not save schedule: {e}")

    # Assemble the day report: the SCHEDULE is built in code from the exact
    # per-hour digests and real capture timestamps (clean hour markers, accurate
    # no-data spans); the writer contributes DAY OVERVIEW + PATTERNS.
    sections = ["# Day Summary - " + day, ""]
    sections.append(f"_Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} "
                    f"| vision {vision} | writer {writer}_")
    sections.append("")
    sections.append("## SCHEDULE")
    sections.extend(schedule_md(day, hour_breakdowns, by_ts))
    sections.append("")
    sections.append(overview.strip())
    sections.append("")

    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(sections) + "\n")
    return True, len(entries)


def hour_label_to_display(label: str) -> str:
    """'2026-09-01 13:00' -> '2026-09-01 13:00 - 14:00'."""
    try:
        hhmm = label.split()[-1]  # '13:00'
        hour = int(hhmm.split(":")[0])
        nxt = f"{hour + 1:02d}:00"
        return f"{label} - {nxt}"
    except Exception:
        return label


def _schedule_shutdown():
    """Schedules a one-time Windows shutdown after a grace period (best-effort)."""
    grace = _ns.shutdown_grace
    msg = (f"OpenRecall bulk summary complete - shutting down in {grace} seconds. "
           "Run 'shutdown /a' to cancel.")
    print(f"  Scheduling shutdown ({grace}s grace)...")
    try:
        subprocess.Popen(["shutdown", "/s", "/t", str(grace), "/c", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  [ERROR] could not schedule shutdown: {e!r}")


class _Tee:
    """Mirrors every write to the console AND the shared progress log.

    Used to redirect stdout/stderr for the whole run so the tray and web UI can
    follow the pipeline live no matter who launched it (tray, .bat, or shell).
    """

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass
        if "\n" in s:
            self.flush()

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    # One authoritative progress log for tray + web UI, regardless of launcher.
    try:
        from openrecall.ai_status import progress_log_path

        _log_path = progress_log_path()
        os.makedirs(os.path.dirname(_log_path), exist_ok=True)
        _log_fh = open(_log_path, "a", encoding="utf-8")
        sys.stdout = _Tee(sys.stdout, _log_fh)
        sys.stderr = _Tee(sys.stderr, _log_fh)
    except Exception:
        pass

    create_db()
    try:
        ensure_server()
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
    vision, writer, sample_min = _ns.vision, _ns.writer, _ns.sample_min

    if _ns.day:
        days = [_ns.day]
    else:
        days = find_days_with_entries()
        if _ns.no_today:
            # A session only closes SESSION_GAP_HOURS after its last moment;
            # anything still inside that window may still grow -> skip it.
            sess_map = {s.key: s for s in get_sessions()}
            now = int(time.time())
            done = [
                d for d in days
                if d not in sess_map or sess_map[d].end < now - SESSION_GAP_HOURS * 3600
            ]
            if len(done) != len(days):
                print("    Skipping still-open session(s) (--no-today); a session only "
                      f"closes {SESSION_GAP_HOURS}h after its last captured moment")
            days = done
        if _ns.since is not None:
            cutoff = (datetime.date.today() - datetime.timedelta(days=_ns.since)).isoformat()
            days = [d for d in days if d >= cutoff]

    if not days:
        print("No snapshots found. Run snapshot_loop.bat (or snapshot.py) first.")
        return

    print(f"Vision model: {vision}")
    print(f"Writer model: {writer}")
    print(f"Sampling: 1 full multi-monitor moment every {sample_min} min; "
          f"hour bundles (vision scenes + minute-level OS activity) to writer\n")

    vision_only = _ns.vision_only
    writer_only = _ns.writer_only
    out_name = _ns.out

    # Skip-already-summarized only makes sense for the normal full pipeline
    # writing to the default <day>.md file. Custom outputs / single-stage runs
    # must not be silently skipped.
    if not _ns.all and not vision_only and not writer_only and not out_name:
        pending = [d for d in days if not os.path.exists(os.path.join(SUMMARIES_DIR, f"{d}.md"))]
        skipped = len(days) - len(pending)
        if skipped:
            print(f"Skipping {skipped} day(s) already summarized. Use --all to regenerate.\n")
        if not pending:
            print("Nothing new to summarize. Run with --all to regenerate everything.")
            if _ns.shutdown:
                _schedule_shutdown()
            return
        days = pending

    _t0 = time.time()
    failures = 0
    for day in days:
        ok, n = process_day(day, vision, writer, sample_min, _ns.all,
                            vision_only=vision_only, writer_only=writer_only, out_name=out_name)
        failures += int(not ok)
        print(f"  {'OK ' if ok else 'FAIL'} {day} ({n} snapshots)\n")

    _elapsed = time.time() - _t0
    print(f"Done. Summaries in {SUMMARIES_DIR}")
    print(f"Total time: {_elapsed/60:.1f} min ({_elapsed:.0f} s) over {len(days)} day(s)")

    if _ns.shutdown:
        if failures:
            print(f"  --shutdown set but {failures} day(s) FAILED; NOT shutting down.")
        else:
            _schedule_shutdown()


if __name__ == "__main__":
    main()
