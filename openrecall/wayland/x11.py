"""
X11/XWayland fallback backend using standard X11 tools (xprop, wmctrl, xrandr).
Works for X11 sessions and XWayland apps on Wayland.
"""
import json
import os
import re
import subprocess
from typing import List, Dict, Any, Optional


def _run(cmd: List[str], timeout: float = 2.0) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def get_monitors_xrandr() -> List[Dict[str, Any]]:
    """Get monitor geometries via xrandr."""
    out = _run(["xrandr", "--listmonitors"])
    if not out:
        return []
    monitors = []
    for line in out.splitlines()[1:]:  # Skip "Monitors: N"
        parts = line.strip().split()
        if len(parts) >= 4:
            idx = int(parts[0].rstrip(":"))
            geom = parts[2]  # "1920/508x1080/286+0+0"
            name = parts[3] if len(parts) > 3 else f"Monitor {idx}"
            m = re.match(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)", geom)
            if m:
                w, h, x, y = map(int, m.groups())
                monitors.append({"left": x, "top": y, "width": w, "height": h, "name": name, "id": idx})
    monitors.sort(key=lambda m: (m.get("left", 0), m.get("top", 0)))
    return monitors


def get_wm_class(win_id: str) -> Optional[str]:
    """Get WM_CLASS instance name for a window ID."""
    out = _run(["xprop", "-id", win_id, "WM_CLASS"])
    if not out:
        return None
    m = re.search(r'WM_CLASS\(STRING\) = "([^"]+)"', out)
    if m:
        return m.group(1)
    return None


def get_windows_x11() -> List[Dict[str, Any]]:
    """Get all visible windows via wmctrl (X11/XWayland)."""
    out = _run(["wmctrl", "-lG"])
    if not out:
        return []
    windows = []
    current_desktop = get_current_desktop_x11()
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        win_id, desk_str, x, y, w, h, host, title = parts
        desk = int(desk_str)
        # Skip sticky windows (desk == -1) and desktop itself
        if desk == -1:
            continue
        # Only keep windows on current desktop
        if current_desktop is not None and desk != current_desktop:
            continue
        # Skip known desktop/window manager windows
        title_lower = title.lower()
        if title_lower in ("desktop", "kde plasma", "plasmashell", "krunner", "plasma", "kwin"):
            continue
        app = get_wm_class(win_id)
        if not app:
            continue
        windows.append({
            "app": app.lower().replace(".exe", ""),
            "title": title.strip(),
            "pid": 0,
            "monitor": None,  # Would need monitor mapping
            "rect": [int(x), int(y), int(w), int(h)],
            "desktop": desk,
            "minimized": False,
        })
    return windows


def get_current_desktop_x11() -> Optional[int]:
    """Get current desktop number via xprop."""
    out = _run(["xprop", "-root", "_NET_CURRENT_DESKTOP"])
    if not out:
        return None
    m = re.search(r"_NET_CURRENT_DESKTOP\s*=\s*(\d+)", out)
    if m:
        return int(m.group(1))
    return None


def get_active_window_x11() -> Optional[Dict[str, Any]]:
    """Get active window via xprop."""
    out = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    if not out:
        return None
    m = re.search(r"window id # (0x[0-9a-fA-F]+)", out)
    if not m:
        return None
    win_id = m.group(1)
    # Get title
    title = ""
    title_out = _run(["xprop", "-id", win_id, "_NET_WM_NAME", "WM_NAME"])
    if title_out:
        m = re.search(r'_NET_WM_NAME\(UTF8_STRING\) = "([^"]*)"', title_out)
        if not m:
            m = re.search(r'WM_NAME\([^)]*\) = "([^"]*)"', title_out)
        if m:
            title = m.group(1)
    app = get_wm_class(win_id)
    if not app:
        return None
    return {
        "app": app.lower().replace(".exe", ""),
        "title": title.strip(),
        "pid": 0,
        "rect": None,
    }


def get_idle_time_x11() -> Optional[float]:
    """Get idle time via xprintidle."""
    out = _run(["xprintidle"])
    if out and out.isdigit():
        return int(out) / 1000.0
    return None


def is_x11_session() -> bool:
    """Check if running on X11 (not Wayland)."""
    return bool(os.environ.get("DISPLAY")) and not bool(os.environ.get("WAYLAND_DISPLAY"))


def is_xwayland() -> bool:
    """Check if running on Wayland with XWayland (DISPLAY set but WAYLAND_DISPLAY also set)."""
    return bool(os.environ.get("DISPLAY")) and bool(os.environ.get("WAYLAND_DISPLAY"))