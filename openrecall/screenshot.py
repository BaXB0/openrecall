import os
import time
import json
import subprocess
import tempfile
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from openrecall.config import screenshots_path, appdata_folder, args
from openrecall.browser_url import get_browser_tabs
from openrecall.database import insert_entry
from openrecall.nlp import get_embedding
from openrecall.ocr import extract_text_from_image
from openrecall.prune import prune_if_due
from openrecall.similarity import mean_structured_similarity_index
from openrecall.utils import (
    get_active_app_name,
    get_active_window_title,
    get_all_windows_as_list,
    get_top_window_per_monitor,
    is_user_active,
)
from openrecall.wayland import get_compositor

# Optional Linux rich data sources
try:
    from openrecall.linux_sources import collect_linux_snapshot, start_linux_watchers
    _LINUX_SOURCES_AVAILABLE = True
except ImportError:
    _LINUX_SOURCES_AVAILABLE = False

# Try mss for X11/XWayland
try:
    import mss
    _MSS_AVAILABLE = True
except ImportError:
    _MSS_AVAILABLE = False


# Capture timing / sensitivity tuning.
HEARTBEAT_INTERVAL = 300
SIMILARITY_THRESHOLD = float(os.environ.get("OPENRECALL_SIMILARITY_THRESHOLD", "0.75"))
INACTIVITY_WARN_HOURS = float(os.environ.get("OPENRECALL_INACTIVITY_WARN_HOURS", "4"))
INACTIVITY_LOG_PATH = os.path.join(appdata_folder, "inactivity.txt")
LINUX_SNAPSHOT_INTERVAL = 30


def _log_inactivity(message: str) -> None:
    try:
        with open(INACTIVITY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except Exception as e:
        print(f"[inactivity] log write failed: {e!r}")


def is_similar(img1: np.ndarray, img2: np.ndarray, similarity_threshold: float = 0.85) -> bool:
    similarity: float = mean_structured_similarity_index(img1, img2)
    return similarity >= similarity_threshold


# ============================================================
# Wayland screenshot via KDE Spectacle (KDE Plasma Wayland)
# ============================================================

def _take_screenshot_wayland() -> List[Tuple[np.ndarray, int, dict]]:
    """Take screenshots on Wayland using KDE Spectacle fullscreen + split."""
    try:
        from openrecall.wayland.kde_screenshot import take_screenshot_kde_multi
        return take_screenshot_kde_multi()
    except Exception as e:
        print(f"[kde] screenshot failed: {e!r}")
        return []


def _take_screenshot_mss() -> List[Tuple[np.ndarray, int, dict]]:
    """Take screenshots using mss (X11/XWayland)."""
    if not _MSS_AVAILABLE:
        return []
    
    screenshots: List[Tuple[np.ndarray, int, dict]] = []
    try:
        with mss.mss() as sct:
            monitor_indices = range(1, len(sct.monitors))
            if args.primary_monitor_only:
                monitor_indices = [1]
            
            for i in monitor_indices:
                if i < len(sct.monitors):
                    monitor_info = sct.monitors[i]
                    sct_img = sct.grab(monitor_info)
                    screenshot = np.array(sct_img)[:, :, [2, 1, 0]]
                    screenshots.append((screenshot, i - 1, monitor_info))
    except Exception as e:
        print(f"[mss] screenshot failed: {e!r}")
    return screenshots


def take_screenshots() -> List[Tuple[np.ndarray, int, dict]]:
    """Take screenshots - auto-detect Wayland vs X11."""
    compositor = get_compositor()
    
    # On KDE Wayland, use grim
    if compositor in ("kde-wayland", "wlr-hyprland", "wlr-sway", "wlr-generic"):
        return _take_screenshot_wayland()
    
    # On X11 or XWayland, use mss
    return _take_screenshot_mss()


def _save_linux_snapshot(snapshot: dict) -> None:
    try:
        log_path = os.path.join(appdata_folder, "linux_snapshots.jsonl")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, default=str) + "\n")
    except Exception as e:
        print(f"[linux_sources] save failed: {e!r}")


def record_screenshots_thread() -> None:
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    if _LINUX_SOURCES_AVAILABLE:
        try:
            start_linux_watchers()
            print("[capture] Linux rich data watchers started")
        except Exception as e:
            print(f"[linux_sources] watcher start failed: {e!r}")

    last_screenshots: Optional[List[np.ndarray]] = None
    while last_screenshots is None:
        try:
            last_screenshots = [img for img, _, _ in take_screenshots()]
        except Exception as e:
            print(f"[capture] error taking initial baseline: {e!r}")
            time.sleep(60)

    if not last_screenshots:
        print("[capture] no monitors captured at startup; retrying later")
        return

    last_file: dict = {}
    last_text: dict = {}
    last_emb: dict = {}
    last_write_ts: Optional[int] = None
    inactive_start: Optional[float] = None
    inactive_warned: bool = False
    next_prune = 0.0
    next_linux_snapshot = 0.0

    while True:
        try:
            if time.time() >= next_prune:
                next_prune = time.time() + 3600
                try:
                    prune_if_due()
                except Exception as e:
                    print(f"[prune] {e}")

            current = take_screenshots()
            current_screenshots: List[np.ndarray] = [img for img, _, _ in current]

            if len(last_screenshots) != len(current_screenshots):
                last_screenshots = current_screenshots
                last_file = {}
                last_text = {}
                last_emb = {}
                time.sleep(60)
                continue

            ts = int(time.time())
            active = is_user_active()

            now = time.time()
            if not active:
                if inactive_start is None:
                    inactive_start = now
                elif not inactive_warned and now - inactive_start >= INACTIVITY_WARN_HOURS * 3600:
                    inactive_warned = True
                    _log_inactivity(f"INACTIVE {INACTIVITY_WARN_HOURS:g}h+ from " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(inactive_start)))
            elif inactive_start is not None:
                if inactive_warned:
                    _log_inactivity("inactive streak ended " + time.strftime("%Y-%m-%d %H:%M:%S") + f" (duration {(now - inactive_start) / 3600:.1f}h)")
                inactive_start = None
                inactive_warned = False

            changed_mons = []
            for i, (current_screenshot, monitor, _geometry) in enumerate(current):
                last_screenshot = last_screenshots[i]
                if not is_similar(current_screenshot, last_screenshot, SIMILARITY_THRESHOLD):
                    changed_mons.append(i)
                    last_screenshots[i] = current_screenshot

            heartbeat_due = last_write_ts is None or ts - last_write_ts >= HEARTBEAT_INTERVAL
            if not changed_mons and not (active and heartbeat_due):
                last_screenshots = current_screenshots[:]
                time.sleep(60)
                continue

            active_app_name: str = get_active_app_name() or "Unknown App"
            active_window_title: str = get_active_window_title() or "Unknown Title"

            monitor_geom = [g for _, _, g in current]
            browser_tabs: list = []
            try:
                browser_tabs = get_browser_tabs(monitors=monitor_geom) or []
            except Exception:
                browser_tabs = []

            windows: list = []
            try:
                windows = get_all_windows_as_list(monitors=monitor_geom) or []
            except Exception:
                windows = []

            top_windows: dict = {}
            try:
                for _tw in get_top_window_per_monitor(monitor_geom) or []:
                    top_windows[_tw["monitor"]] = _tw
            except Exception:
                top_windows = {}

            main_mon = min((_m for _, _m, _ in current), default=0)

            if _LINUX_SOURCES_AVAILABLE and time.time() >= next_linux_snapshot:
                next_linux_snapshot = time.time() + LINUX_SNAPSHOT_INTERVAL
                try:
                    linux_snapshot = collect_linux_snapshot()
                    linux_snapshot["capture_ts"] = ts
                    _save_linux_snapshot(linux_snapshot)
                except Exception as e:
                    print(f"[linux_sources] collection failed: {e!r}")

            for i, (current_screenshot, monitor, geometry) in enumerate(current):
                screen_x = geometry.get("left", 0) if geometry else 0

                tabs_m = [t for t in browser_tabs if t.get("monitor") == monitor]
                wins_m = [w for w in windows if w.get("monitor") == monitor]
                if monitor == main_mon:
                    tabs_m.extend(t for t in browser_tabs if t.get("monitor") is None)
                    wins_m.extend(w for w in windows if w.get("monitor") is None)
                url_m = next((t.get("url") for t in tabs_m if t.get("url")), None)
                top = top_windows.get(monitor) or {}
                app_m = top.get("app") or ""
                title_m = top.get("title") or ""

                changed = i in changed_mons or monitor not in last_file
                if changed:
                    image = Image.fromarray(current_screenshot)
                    filename = f"{ts}_{monitor}.webp"
                    filepath = os.path.join(screenshots_path, filename)
                    image.save(filepath, format="webp", lossless=True)
                    last_file[monitor] = filename

                    text: str = extract_text_from_image(current_screenshot)
                    embedding: np.ndarray = get_embedding(text)
                    last_text[monitor] = text
                    last_emb[monitor] = embedding

                insert_entry(
                    last_text.get(monitor, ""),
                    ts,
                    last_emb.get(monitor, get_embedding("")),
                    app_m,
                    title_m,
                    filename=last_file.get(monitor),
                    monitor=monitor,
                    screen_x=screen_x,
                    browser_url=url_m,
                    browser_tabs=tabs_m,
                    windows=wins_m,
                )

            last_write_ts = ts

            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            mons = ", ".join(f"m{m}{'*' if i in changed_mons else ''}" for i, (_, m, _) in enumerate(current))
            tabs_n = len(browser_tabs)
            wins_n = len(windows)
            browser_mons = sorted({t.get("monitor") for t in browser_tabs if t.get("monitor") is not None})
            print(f"[{when}] {active_app_name} | {active_window_title} | monitors: {mons} | tabs: {tabs_n}{' (m' + ','.join(map(str, browser_mons)) + ')' if browser_mons else ''} | windows: {wins_n}")

        except Exception as e:
            print(f"[capture] cycle error (will retry in 60s): {e!r}")
        time.sleep(60)


if __name__ == "__main__":
    record_screenshots_thread()