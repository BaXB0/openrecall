"""
OpenRecall snapshot tool.

Captures ALL connected monitors at the same instant (one shared timestamp per
"batch"), runs OCR + text embedding on each screen that actually changed since
the last capture, records the active app and window title, and stores everything.

In loop mode (--delay), runs continuously. Every cycle it grabs all monitors; if
at least one screen changed, it writes a full "moment" (all monitors sharing one
timestamp). Only the changed screens get fresh OCR/screenshots — unchanged ones
reuse their existing image/text, which keeps CPU and disk usage reasonable.

Usage:
    python snapshot.py                       # capture once (all monitors)
    python snapshot.py --primary-only        # main monitor only
    python snapshot.py --delay 60            # loop every minute (recommended)
"""
import argparse
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# openrecall.config parses sys.argv at import time but uses parse_known_args(),
# so this script's own flags (--delay, --primary-only) pass through untouched.

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from openrecall.config import screenshots_path  # noqa: E402
from openrecall.browser_url import get_browser_tabs  # noqa: E402
from openrecall.database import create_db, insert_entry  # noqa: E402
from openrecall.nlp import get_embedding  # noqa: E402
from openrecall.ocr import extract_text_from_image  # noqa: E402
from openrecall.prune import prune_if_due  # noqa: E402
from openrecall.screenshot import take_screenshots  # noqa: E402
from openrecall.utils import (  # noqa: E402
    get_active_app_name,
    get_active_window_title,
    get_all_windows_as_list,
    get_top_window_per_monitor,
)

try:
    import numpy as np
    from PIL import Image
except ImportError as e:
    print(f"[ERROR] Missing dependency: {e}")
    sys.exit(1)


def _similarity(a: np.ndarray, b: np.ndarray, threshold: float = 0.9) -> bool:
    """Quick structural similarity check between two RGB images."""
    L = 255
    K1, K2 = 0.01, 0.03
    C1, C2 = (K1 * L) ** 2, (K2 * L) ** 2

    gray = lambda img: 0.2989 * img[..., 0] + 0.5870 * img[..., 1] + 0.1140 * img[..., 2]
    g1, g2 = gray(a), gray(b)
    mu1, mu2 = float(np.mean(g1)), float(np.mean(g2))
    s1sq, s2sq = float(np.var(g1)), float(np.var(g2))
    s12 = float(np.mean((g1 - mu1) * (g2 - mu2)))
    sim = ((2 * mu1 * mu2 + C1) * (2 * s12 + C2)) / (
        (mu1 ** 2 + mu2 ** 2 + C1) * (s1sq + s2sq + C2)
    )
    return sim >= threshold


def _context_snapshot(monitors) -> Tuple[list, list, dict, int]:
    """Best-effort per-screen capture of on-screen context.

    Returns (browser_tabs, windows, top_windows, main_mon):
      - browser_tabs: active tab (title+url) of every visible browser window,
        each tagged with the 0-based ``monitor`` index it sits on.
      - windows: every visible window (app+title), tagged with its monitor
        (None when minimised/off-screen).
      - top_windows: {monitor: {"app", "title"}} = topmost window per screen.
      - main_mon: the leftmost monitor index, used as the catch-all target for
        minimised windows so they stay visible to the AI.
    """
    browser_tabs: list = []
    try:
        browser_tabs = get_browser_tabs(monitors=monitors) or []
    except Exception:
        browser_tabs = []
    windows: list = []
    try:
        windows = get_all_windows_as_list(monitors=monitors) or []
    except Exception:
        windows = []
    top_windows: dict = {}
    try:
        for _tw in get_top_window_per_monitor(monitors) or []:
            top_windows[_tw["monitor"]] = _tw
    except Exception:
        top_windows = {}
    main_mon = min(range(len(monitors)), default=0)
    return browser_tabs or [], windows or [], top_windows, main_mon


def _per_monitor(monitor, browser_tabs, windows, top_windows, main_mon, active_app, active_title):
    """Per-screen subset of the desktop-wide context for one monitor row."""
    tabs_m = [t for t in browser_tabs if t.get("monitor") == monitor]
    wins_m = [w for w in windows if w.get("monitor") == monitor]
    if monitor == main_mon:
        tabs_m.extend(t for t in browser_tabs if t.get("monitor") is None)
        wins_m.extend(w for w in windows if w.get("monitor") is None)
    url_m = next((t.get("url") for t in tabs_m if t.get("url")), None)
    top = top_windows.get(monitor) or {}
    app_m = top.get("app") or active_app
    title_m = top.get("title") or active_title
    return url_m, tabs_m, wins_m, app_m, title_m


def _save_batch(
    captures: List[Tuple[np.ndarray, int, dict]],
    active_app: str,
    active_title: str,
) -> None:
    """Stores a batch of monitor images with shared timestamp, runs OCR + embedding."""
    create_db()
    os.makedirs(screenshots_path, exist_ok=True)

    timestamp = int(time.time())

    monitors = [info for _, _, info in captures]
    browser_tabs, windows, top_windows, main_mon = _context_snapshot(monitors)

    for shot, monitor_index, info in captures:
        filename = f"{timestamp}_{monitor_index}.webp"
        filepath = os.path.join(screenshots_path, filename)
        Image.fromarray(shot).save(filepath, format="webp", lossless=True)

        text = extract_text_from_image(shot)
        embedding = get_embedding(text)
        screen_x = info.get("left", 0) if info else 0
        browser_url, tabs_m, wins_m, app_m, title_m = _per_monitor(
            monitor_index, browser_tabs, windows, top_windows, main_mon,
            active_app, active_title,
        )
        insert_entry(
            text,
            timestamp,
            embedding,
            app_m,
            title_m,
            filename=filename,
            monitor=monitor_index,
            screen_x=screen_x,
            browser_url=browser_url,
            browser_tabs=tabs_m,
            windows=wins_m,
        )

    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    mons = ", ".join(f"m{i}" for _, i, _ in captures)
    print(f"[{when}] {active_app} | {active_title} | monitors: {mons}"
          f" | tabs: {len(browser_tabs)} | windows: {len(windows)}")


def capture_once(primary_only: bool) -> None:
    """Single capture: always saves + OCRs all monitors."""
    active_app = get_active_app_name() or "Unknown App"
    active_title = get_active_window_title() or "Unknown Title"
    captures = take_screenshots()  # Use the new cross-platform function
    _save_batch(captures, active_app, active_title)


def capture_loop(primary_only: bool, delay: float) -> None:
    """Continuous loop: every N seconds captures ALL monitors as one moment
    (shared timestamp). A moment is only written when at least one screen
    changes; screens that are unchanged since their last save reuse their
    existing image/text/embedding, so disk stays bounded."""
    create_db()
    os.makedirs(screenshots_path, exist_ok=True)

    last_images: Dict[int, np.ndarray] = {}  # monitor_index -> current RGB array
    last_file: Dict[int, str] = {}           # monitor_index -> last saved filename
    last_text: Dict[int, str] = {}           # monitor_index -> last OCR text
    last_emb: Dict[int, np.ndarray] = {}     # monitor_index -> last embedding

    active_app = ""
    active_title = ""

    print(f"Capturing every {delay}s (all monitors per moment). Press Ctrl+C to stop.")

    # Prune once on startup, then re-check hourly; the stamp file makes sure a
    # given calendar day only ever triggers one full prune.
    next_prune = 0.0

    try:
        while True:
            if time.time() >= next_prune:
                next_prune = time.time() + 3600
                try:
                    prune_if_due()
                except Exception as e:
                    print(f"[prune] {e}")

            cycle_start = time.time()
            active_app = get_active_app_name() or "Unknown App"
            active_title = get_active_window_title() or "Unknown Title"

            captures = take_screenshots()  # Use cross-platform function
            ts = int(cycle_start)

            changed_mons: List[int] = []
            for shot, mi, _ in captures:
                prev = last_images.get(mi)
                if prev is None or not _similarity(shot, prev):
                    last_images[mi] = shot
                    changed_mons.append(mi)

            if not captures:
                time.sleep(max(0.0, delay - (time.time() - cycle_start)))
                continue

            # If nothing changed since the last written moment, skip this cycle.
            if not changed_mons and last_file:
                time.sleep(max(0.0, delay - (time.time() - cycle_start)))
                continue

            monitors_geom = [info for _, _, info in captures]
            browser_tabs, windows, top_windows, main_mon = _context_snapshot(monitors_geom)

            # Build the moment: fresh file + OCR for changed screens, reuse for the rest.
            for shot, mi, info in captures:
                screen_x = info.get("left", 0) if info else 0

                if mi in changed_mons:
                    filename = f"{ts}_{mi}.webp"
                    filepath = os.path.join(screenshots_path, filename)
                    Image.fromarray(shot).save(filepath, format="webp", lossless=True)

                    text = extract_text_from_image(shot)
                    embedding = get_embedding(text)
                    last_file[mi] = filename
                    last_text[mi] = text
                    last_emb[mi] = embedding
                elif mi not in last_file:
                    # Brand new monitor with no prior save: treat as changed.
                    filename = f"{ts}_{mi}.webp"
                    filepath = os.path.join(screenshots_path, filename)
                    Image.fromarray(shot).save(filepath, format="webp", lossless=True)

                    text = extract_text_from_image(shot)
                    embedding = get_embedding(text)
                    last_file[mi] = filename
                    last_text[mi] = text
                    last_emb[mi] = embedding

                browser_url, tabs_m, wins_m, app_m, title_m = _per_monitor(
                    mi, browser_tabs, windows, top_windows, main_mon,
                    active_app, active_title,
                )
                insert_entry(
                    last_text[mi],
                    ts,
                    last_emb[mi],
                    app_m,
                    title_m,
                    filename=last_file[mi],
                    monitor=mi,
                    screen_x=screen_x,
                    browser_url=browser_url,
                    browser_tabs=tabs_m,
                    windows=wins_m,
                )

            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            mons = ", ".join(f"m{i}{'*' if i in changed_mons else ''}" for _, i, _ in captures)
            print(f"[{when}] {active_app} | {active_title} | monitors: {mons}"
                  f" | tabs: {len(browser_tabs)} | windows: {len(windows)}")

            elapsed = time.time() - cycle_start
            time.sleep(max(0.0, delay - elapsed))

    except KeyboardInterrupt:
        print("\nStopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenRecall snapshots")
    parser.add_argument(
        "--primary-only",
        action="store_true",
        help="Only capture the main monitor instead of all monitors",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0,
        help="Loop interval in seconds. 0 = run once. 60 = continuous every minute (recommended).",
    )
    args = parser.parse_args()

    if args.delay and args.delay > 0:
        capture_loop(args.primary_only, args.delay)
    else:
        capture_once(args.primary_only)


if __name__ == "__main__":
    main()