import sys
import datetime
import re
from typing import List, Dict, Any, Optional, Tuple

# Platform-specific imports with graceful fallback
try:
    import psutil
except ImportError:
    psutil = None

# Wayland/X11 backend (Linux)
try:
    from openrecall.wayland import (
        get_monitors as _get_monitors,
        get_windows as _get_windows,
        get_active_window as _get_active_window,
        get_idle_time as _get_idle_time,
        is_user_active as _is_user_active,
        is_firefox_window,
        get_firefox_windows,
        detect_compositor,
        invalidate_monitor_cache,
    )
    _WAYLAND_AVAILABLE = True
except ImportError:
    _WAYLAND_AVAILABLE = False

# macOS (kept for compatibility)
try:
    from AppKit import NSWorkspace
except ImportError:
    NSWorkspace = None

try:
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListOptionOnScreenOnly,
    )
except ImportError:
    CGWindowListCopyWindowInfo = None
    kCGNullWindowID = None
    kCGWindowListOptionOnScreenOnly = None


def human_readable_time(timestamp: int) -> str:
    """Converts a Unix timestamp into a human-readable relative time string."""
    now = datetime.datetime.now()
    dt_object = datetime.datetime.fromtimestamp(timestamp)
    diff = now - dt_object
    if diff.days > 0:
        return f"{diff.days} days ago"
    elif diff.seconds < 60:
        return f"{diff.seconds} seconds ago"
    elif diff.seconds < 3600:
        return f"{diff.seconds // 60} minutes ago"
    else:
        return f"{diff.seconds // 3600} hours ago"


def timestamp_to_human_readable(timestamp: int) -> str:
    """Converts a Unix timestamp into a human-readable absolute date/time string."""
    try:
        dt_object = datetime.datetime.fromtimestamp(timestamp)
        return dt_object.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


# ============================================================
# macOS implementations (unchanged)
# ============================================================

def get_active_app_name_osx() -> str:
    if NSWorkspace is None:
        return ""
    try:
        active_app = NSWorkspace.sharedWorkspace().activeApplication()
        return active_app.get("NSApplicationName", "")
    except Exception:
        return ""


def get_active_window_title_osx() -> str:
    if CGWindowListCopyWindowInfo is None or kCGNullWindowID is None or kCGWindowListOptionOnScreenOnly is None:
        return ""
    try:
        app_name = get_active_app_name_osx()
        if not app_name:
            return ""
        options = kCGWindowListOptionOnScreenOnly
        window_list = CGWindowListCopyWindowInfo(options, kCGNullWindowID)
        for window in window_list:
            if window.get("kCGWindowOwnerName") == app_name:
                if window.get("kCGWindowLayer") == 0 and "kCGWindowName" in window:
                    title = window.get("kCGWindowName", "")
                    if title:
                        return title
        return ""
    except Exception:
        return ""


# ============================================================
# Linux implementations (new Wayland/X11 backend)
# ============================================================

def get_active_app_name_linux() -> str:
    """Get active application name on Linux (Wayland/X11)."""
    if not _WAYLAND_AVAILABLE:
        return ""
    try:
        win = _get_active_window()
        if win:
            return win.get("app", "")
    except Exception:
        pass
    return ""


def get_active_window_title_linux() -> str:
    """Get active window title on Linux (Wayland/X11)."""
    if not _WAYLAND_AVAILABLE:
        return ""
    try:
        win = _get_active_window()
        if win:
            return win.get("title", "")
    except Exception:
        pass
    return ""


def is_user_active_linux() -> bool:
    """Check if user is active on Linux (Wayland/X11)."""
    if not _WAYLAND_AVAILABLE:
        return True  # Assume active if backend unavailable
    try:
        return _is_user_active()
    except Exception:
        return True


# ============================================================
# Unified cross-platform API
# ============================================================

def get_active_app_name() -> str:
    """Get the active application name for the current platform."""
    if sys.platform == "win32":
        return get_active_app_name_windows()
    elif sys.platform == "darwin":
        return get_active_app_name_osx()
    elif sys.platform.startswith("linux"):
        return get_active_app_name_linux()
    else:
        raise NotImplementedError(f"Platform '{sys.platform}' not supported for get_active_app_name")


def get_active_window_title() -> str:
    """Get the active window title for the current platform."""
    if sys.platform == "win32":
        return get_active_window_title_windows()
    elif sys.platform == "darwin":
        return get_active_window_title_osx()
    elif sys.platform.startswith("linux"):
        return get_active_window_title_linux()
    else:
        print(f"Warning: Active window title retrieval not implemented for platform '{sys.platform}'")
        raise NotImplementedError(f"Platform '{sys.platform}' not supported for get_active_window_title")


def is_user_active() -> bool:
    """Check if the user is active on the current platform."""
    if sys.platform == "win32":
        return is_user_active_windows()
    elif sys.platform == "darwin":
        return is_user_active_osx()
    elif sys.platform.startswith("linux"):
        return is_user_active_linux()
    else:
        print(f"Warning: User active check not supported for platform '{sys.platform}', assuming active.")
        return True


# ============================================================
# Window enumeration (Linux: Wayland/X11 unified)
# ============================================================

# Windows ignore lists (kept for reference, not used on Linux)
_WINDOWS_IGNORE_CLASSES = {"progman", "shell_traywnd", "button", "rzmonitorforegroundwindow"}
_WINDOWS_IGNORE_APPS = {"razerappengine.exe"}
_WINDOWS_IGNORE_TITLES = {"rzmonitorforegroundwindow"}

# Linux ignore lists (KDE/Plasma specific)
_LINUX_IGNORE_APPS = {
    "plasmashell", "kwin_wayland", "kwin_x11", "krunner", "plasma",
    "polkit-kde-authentication-agent-1", "xembedsniproxy",
}
_LINUX_IGNORE_TITLES = {"desktop", "kde plasma", "plasmashell", "krunner"}


def _filter_linux_window(w: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Filter out desktop/shell windows on Linux."""
    app = (w.get("app") or "").lower()
    title = (w.get("title") or "").strip().lower()

    if not title:
        return None
    if app in _LINUX_IGNORE_APPS:
        return None
    if title in _LINUX_IGNORE_TITLES:
        return None
    # Skip minimized
    if w.get("minimized"):
        return None
    return w


def get_all_windows_as_list(monitors=None) -> List[Dict[str, Any]]:
    """
    Gets the titles of all visible top-level windows.

    Returns: [{"app": "...", "title": "...", "monitor": 0,
               "active": bool, "fullscreen": bool}, ...]
    Sorted by monitor, then app, then title. Deduplicated.
    """
    if sys.platform == "win32":
        return get_windows_windows(monitors=monitors)
    elif sys.platform == "darwin":
        return get_windows_osx()
    elif sys.platform.startswith("linux"):
        if not _WAYLAND_AVAILABLE:
            return []
        try:
            windows = _get_windows()
            # Filter
            filtered = [_filter_linux_window(w) for w in windows]
            filtered = [w for w in filtered if w is not None]

            # Map to monitor if monitors provided
            if monitors:
                monitor_rects_list = monitor_rects(monitors)
                for w in filtered:
                    if w.get("rect"):
                        w["monitor"] = monitor_for_rect_area(w["rect"], monitors)
                    else:
                        w["monitor"] = None
            else:
                for w in filtered:
                    w["monitor"] = w.get("monitor")

            # Deduplicate
            seen, out = set(), []
            for w in filtered:
                key = (w.get("monitor"), w["app"].lower(), w["title"].lower())
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "app": w["app"],
                    "title": w["title"],
                    "monitor": w.get("monitor"),
                    "active": bool(w.get("active", False)),
                    "fullscreen": bool(w.get("fullscreen", False)),
                })
            out.sort(key=lambda w: (
                w["monitor"] if w["monitor"] is not None else -1,
                w["app"].lower(),
                w["title"].lower()
            ))
            return out
        except Exception:
            return []
    return []


def get_top_window_per_monitor(monitors=None) -> List[Dict[str, Any]]:
    """
    For each monitor, the topmost visible window that actually covers it.
    Returns: [{"monitor": 0, "app": ..., "title": ...}, ...]
    """
    if sys.platform == "win32":
        return get_top_window_per_monitor_windows(monitors)
    elif sys.platform.startswith("linux"):
        if not _WAYLAND_AVAILABLE:
            return []
        try:
            windows = _get_windows()
            filtered = [_filter_linux_window(w) for w in windows]
            filtered = [w for w in filtered if w is not None]

            if not monitors:
                monitors = _get_monitors()
            monitor_rects_list = monitor_rects(monitors)
            if not monitor_rects_list:
                return []

            # Assign each window to the monitor it covers the most area on
            min_cover_frac = 0.05
            mon_areas = [max(1, (m["right"] - m["left"]) * (m["bottom"] - m["top"])) for m in monitor_rects_list]

            # Sort windows by stacking order (we don't have z-order on Linux easily,
            # so we'll use the order returned - typically top-to-bottom)
            owned = {}
            for w in filtered:
                rect = w.get("rect")
                if not rect or rect[0] <= -1000:
                    continue
                warea = max(1, (rect[2] - rect[0]) * (rect[3] - rect[1]))
                best_idx, best_area = None, 0
                for i, m in enumerate(monitor_rects_list):
                    l, r = max(rect[0], m["left"]), min(rect[2], m["right"])
                    t, b = max(rect[1], m["top"]), min(rect[3], m["bottom"])
                    if r <= l or b <= t:
                        continue
                    area = (r - l) * (b - t)
                    meaningful = (area * (1.0 / min_cover_frac)) >= warea or \
                                 (area * (1.0 / min_cover_frac)) >= mon_areas[i]
                    if meaningful and area > best_area:
                        best_idx, best_area = i, area
                if best_idx is not None and best_idx not in owned:
                    owned[best_idx] = {"monitor": best_idx, "app": w["app"], "title": w["title"]}

            return [owned[i] for i in sorted(owned)]
        except Exception:
            return []
    return []


# ============================================================
# Monitor geometry helpers (cross-platform)
# ============================================================

def monitor_rects(monitors=None) -> List[Dict[str, int]]:
    """Normalizes monitor geometry to list of dicts with left/top/right/bottom."""
    if monitors:
        out = []
        for m in monitors:
            md = dict(m)
            if "right" not in md and "width" in md:
                md["right"] = md["left"] + md["width"]
                md["bottom"] = md["top"] + md["height"]
            out.append(md)
        return out

    if sys.platform == "win32":
        return monitor_rects_windows()
    elif sys.platform.startswith("linux"):
        if _WAYLAND_AVAILABLE:
            mons = _get_monitors()
            out = []
            for m in mons:
                out.append({
                    "left": m.get("left", 0),
                    "top": m.get("top", 0),
                    "right": m.get("left", 0) + m.get("width", 0),
                    "bottom": m.get("top", 0) + m.get("height", 0),
                })
            return out
    return []


def monitor_rects_windows() -> List[Dict[str, int]]:
    """Windows monitor rects via win32api."""
    try:
        import win32api
        out = []
        for _h, _dc, (left, top, right, bottom) in win32api.EnumDisplayMonitors():
            out.append({"left": left, "top": top, "right": right, "bottom": bottom})
        out.sort(key=lambda r: (r["left"], r["top"]))
        return out
    except Exception:
        return []


def monitor_for_rect(rect, monitors=None) -> Optional[int]:
    """0-based index of monitor containing the centre of rect."""
    rects = monitor_rects(monitors)
    if not rects or not rect:
        return None
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    for i, mb in enumerate(rects):
        if mb["left"] <= cx < mb["right"] and mb["top"] <= cy < mb["bottom"]:
            return i
    best, best_d = None, None
    for i, mb in enumerate(rects):
        dcx = mb["left"] + (mb["right"] - mb["left"]) // 2
        dcy = mb["top"] + (mb["bottom"] - mb["top"]) // 2
        d = (cx - dcx) ** 2 + (cy - dcy) ** 2
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


_TOP_WINDOW_MIN_COVER_FRAC = 0.05


def monitor_for_rect_area(rect, monitors=None, min_cover_frac=_TOP_WINDOW_MIN_COVER_FRAC) -> Optional[int]:
    """0-based index of monitor that rect overlaps the most (by area)."""
    if not rect or rect[0] <= -1000:
        return None
    rects = monitor_rects(monitors)
    if not rects:
        return None
    warea = max(1, (rect[2] - rect[0]) * (rect[3] - rect[1]))
    best_idx, best_area = None, 0.0
    for i, m in enumerate(rects):
        l, r = max(rect[0], m["left"]), min(rect[2], m["right"])
        t, b = max(rect[1], m["top"]), min(rect[3], m["bottom"])
        if r <= l or b <= t:
            continue
        area = (r - l) * (b - t)
        mon_area = max(1, (m["right"] - m["left"]) * (m["bottom"] - m["top"]))
        if (area / mon_area >= min_cover_frac or area / warea >= min_cover_frac) and area > best_area:
            best_idx, best_area = i, area
    return best_idx


# ============================================================
# Windows implementations (kept for cross-platform compatibility)
# ============================================================

try:
    import win32gui
    import win32process
    import win32api
except ImportError:
    win32gui = win32process = win32api = None


def get_active_app_name_windows() -> str:
    if not all([psutil, win32gui, win32process]):
        return ""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return ""
        exe = psutil.Process(pid).name()
        return exe
    except Exception:
        return ""


def get_active_window_title_windows() -> str:
    if win32gui is None:
        return ""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ""
        return win32gui.GetWindowText(hwnd)
    except Exception:
        return ""


def is_user_active_windows() -> bool:
    if win32api is None:
        return True
    try:
        last_input_info = win32api.GetLastInputInfo()
        current_time = win32api.GetTickCount()
        idle_milliseconds = current_time - last_input_info
        return (idle_milliseconds / 1000.0) < 5.0
    except Exception:
        return True


def is_cloaked(hwnd) -> bool:
    try:
        import ctypes
        import ctypes.wintypes
        fn = ctypes.windll.dwmapi.DwmGetWindowAttribute
        fn.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.DWORD), ctypes.wintypes.DWORD]
        fn.restype = ctypes.c_long
        val = ctypes.wintypes.DWORD(0)
        hr = fn(hwnd, 14, ctypes.byref(val), ctypes.sizeof(ctypes.wintypes.DWORD))
        if hr != 0:
            return False
        return val.value != 0
    except Exception:
        return False


def _describe_window(hwnd):
    if win32gui is None:
        return None
    try:
        if not win32gui.IsWindowVisible(hwnd):
            return None
        title = (win32gui.GetWindowText(hwnd) or "").strip()
        if not title or title.lower() in _WINDOWS_IGNORE_TITLES:
            return None
        cls = (win32gui.GetClassName(hwnd) or "").lower()
        if cls in _WINDOWS_IGNORE_CLASSES or cls.startswith("tooltips_class"):
            return None
        if is_cloaked(hwnd):
            return None
        app = ""
        if psutil and win32process:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid:
                    app = psutil.Process(pid).name() or ""
            except Exception:
                pass
        if not app or app.lower() in _WINDOWS_IGNORE_APPS:
            return None
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            rect = None
        return {"app": app, "title": title, "rect": rect}
    except Exception:
        return None


def get_windows_windows(monitors=None):
    seen, out = set(), []
    if win32gui is None:
        return []
    try:
        def _collect(hwnd, results):
            desc = _describe_window(hwnd)
            if desc:
                desc["monitor"] = monitor_for_rect_area(desc["rect"], monitors) if desc["rect"] else None
                results.append(desc)
            return True
        results = []
        win32gui.EnumWindows(_collect, results)
    except Exception:
        return []
    for w in results:
        key = (w.get("monitor"), w["app"].lower(), w["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"app": w["app"], "title": w["title"], "monitor": w.get("monitor")})
    out.sort(key=lambda w: ((w["monitor"] if w["monitor"] is not None else -1), w["app"].lower(), w["title"].lower()))
    return out


def get_top_window_per_monitor_windows(monitors=None):
    rects = monitor_rects(monitors)
    if not rects or win32gui is None:
        return []
    mon_areas = [max(1, (m["right"] - m["left"]) * (m["bottom"] - m["top"])) for m in rects]
    min_frac = _TOP_WINDOW_MIN_COVER_FRAC
    try:
        def _clip_area(wrect, m):
            l, r = max(wrect[0], m["left"]), min(wrect[2], m["right"])
            t, b = max(wrect[1], m["top"]), min(wrect[3], m["bottom"])
            if r <= l or b <= t:
                return 0
            return (r - l) * (b - t)

        def _collect(hwnd, owned):
            desc = _describe_window(hwnd)
            if not (desc and desc["rect"] and desc["rect"][0] > -1000):
                return True
            wrect = desc["rect"]
            warea = max(1, (wrect[2] - wrect[0]) * (wrect[3] - wrect[1]))
            best_idx, best_area = None, 0
            for i, m in enumerate(rects):
                area = _clip_area(wrect, m)
                if area <= 0:
                    continue
                meaningful = (area * (1.0 / min_frac)) >= warea or (area * (1.0 / min_frac)) >= mon_areas[i]
                if meaningful and area > best_area:
                    best_idx, best_area = i, area
            if best_idx is not None and best_idx not in owned:
                owned[best_idx] = {"monitor": best_idx, "app": desc["app"], "title": desc["title"]}
            return True
        owned = {}
        win32gui.EnumWindows(_collect, owned)
    except Exception:
        return []
    return [owned[i] for i in sorted(owned)]


def get_windows_osx():
    if CGWindowListCopyWindowInfo is None or kCGNullWindowID is None or kCGWindowListOptionOnScreenOnly is None:
        return []
    try:
        info = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        results = []
        for w in info:
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            app = w.get("kCGWindowOwnerName") or ""
            title = w.get("kCGWindowName") or ""
            if title and app:
                results.append({"app": app, "title": title})
        seen, out = set(), []
        for r in results:
            key = (r["app"].lower(), r["title"].lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        out.sort(key=lambda w: (w["app"].lower(), w["title"].lower()))
        return out
    except Exception:
        return []


def is_user_active_osx() -> bool:
    try:
        import subprocess
        output = subprocess.check_output(["ioreg", "-c", "IOHIDSystem", "-r", "-k", "HIDIdleTime"], timeout=1).decode()
        for line in output.splitlines():
            if "HIDIdleTime" in line:
                idle_time = int(line.split("=")[-1].strip())
                return (idle_time / 1_000_000_000) < 5.0
        return True
    except Exception:
        return True


# ============================================================
# Firefox/LibreWolf helpers (Linux)
# ============================================================

def get_firefox_windows_linux() -> List[Dict[str, Any]]:
    """Get Firefox/LibreWolf windows on Linux."""
    if sys.platform.startswith("linux") and _WAYLAND_AVAILABLE:
        try:
            return get_firefox_windows()
        except Exception:
            pass
    return []


def get_compositor_name() -> str:
    """Get the detected compositor name (Linux only)."""
    if sys.platform.startswith("linux") and _WAYLAND_AVAILABLE:
        return detect_compositor()
    return sys.platform