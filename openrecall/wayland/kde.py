"""
KDE Plasma Wayland backend for window enumeration and monitor info.

Uses KWin scripting via DBus and falls back to X11 tools for XWayland apps.
"""
import json
import os
import re
import subprocess
import sys
from typing import List, Dict, Any, Optional, Tuple


def _run(cmd: List[str], timeout: float = 2.0) -> Optional[str]:
    """Run command and return stdout, or None on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _run_json(cmd: List[str], timeout: float = 2.0) -> Optional[Any]:
    """Run command and parse JSON output."""
    out = _run(cmd, timeout)
    if out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return None


# ============================================================
# KWin DBus access via PyGObject (Gio) — the reliable native path.
#
# KWin exposes window data on the session bus but NOT through a
# synchronous "evalScript" (that method does not exist). Window
# enumeration works by asking KRunner's window runner for the running
# window UUIDs, then asking KWin for each window's full info. This is
# the sanctioned, portal-free way to see BOTH native Wayland and
# XWayland windows on Plasma.
# ============================================================

_GI = None


def _gi():
    """Lazily import Gio/GLib; return (Gio, GLib) or None."""
    global _GI
    if _GI is None:
        try:
            import gi  # type: ignore
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib  # type: ignore
            _GI = (Gio, GLib)
        except Exception:
            _GI = False
    return _GI


def _session_bus():
    g = _gi()
    if not g:
        return None
    Gio, _GLib = g
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception:
        return None


def _dbus_call(dest: str, path: str, iface: str, method: str,
               in_sig: str, in_args: tuple, reply_sig: str,
               timeout_ms: int = 3000) -> Optional[Any]:
    """Synchronous session-bus method call; returns unpacked reply or None."""
    bus = _session_bus()
    g = _gi()
    if bus is None or not g:
        return None
    Gio, GLib = g
    try:
        res = bus.call_sync(
            dest, path, iface, method,
            GLib.Variant(in_sig, in_args),
            GLib.VariantType(reply_sig),
            Gio.DBusCallFlags.NONE, timeout_ms, None,
        )
        return res.unpack()
    except Exception:
        return None


def _window_uuids() -> List[str]:
    """UUIDs of all running windows, via KRunner's window runner."""
    # The runner's reply signature differs between Plasma versions
    # (introspection reports `u` but the runtime emits `i` for one field).
    out = None
    for reply_sig in ("(a(sssida{sv}))", "(a(sssuda{sv}))", "(a(ssssda{sv}))"):
        out = _dbus_call(
            "org.kde.KWin", "/WindowsRunner", "org.kde.krunner1", "Match",
            "(s)", ("",), reply_sig,
        )
        if out is not None:
            break
    if not out or not out[0]:
        return []
    uuids: List[str] = []
    seen = set()
    for match in out[0]:
        mid = match[0] if match else ""
        # matchId looks like "0_{99094abc-...-...}"; extract inside braces.
        if "{" in mid and "}" in mid:
            uuid = mid[mid.index("{") + 1:mid.index("}")]
            if uuid not in seen:
                seen.add(uuid)
                uuids.append(uuid)
    return uuids


def _window_info(uuid: str) -> Optional[Dict[str, Any]]:
    """Full info dict for one window uuid (caption, resourceClass, x/y/w/h...)."""
    out = _dbus_call(
        "org.kde.KWin", "/KWin", "org.kde.KWin", "getWindowInfo",
        "(s)", (uuid,), "(a{sv})",
    )
    if not out:
        return None
    return out[0]


def get_monitors_kde() -> List[Dict[str, Any]]:
    """
    Get monitor geometries on KDE Plasma (Wayland or X11).

    Uses ``kscreen-doctor -j``. The authoritative current geometry is each
    output's ``pos`` + ``size`` — NOT the ``modes`` list: the active mode is
    identified by ``currentModeId`` and the mode list is not reliably ordered,
    which previously produced wrong sizes when a mirror output was present
    (a 1920x1080 DP-1 was reported as 1280x960 and captured half cut off).

    Mirrored/cloned outputs (``replicationSource`` != 0) are skipped: a clone
    shows the same content as its source and only confuses capture splitting.
    Set ``OPENRECALL_IGNORE_MONITORS`` to a comma-separated list of output
    names to skip additional outputs.

    Returns list of dicts with: left, top, width, height, name, id (0-based).
    """
    ignore = {n.strip() for n in os.environ.get("OPENRECALL_IGNORE_MONITORS", "").split(",") if n.strip()}
    out = _run(["kscreen-doctor", "-j"])
    if out:
        try:
            data = json.loads(out)
            monitors: List[Dict[str, Any]] = []
            for output in data.get("outputs", []):
                if not output.get("enabled", False):
                    continue
                name = output.get("name", "")
                if name in ignore:
                    continue
                # Skip mirror/clone outputs: they show the same image as their
                # source, so capturing them adds nothing but confusion.
                if output.get("replicationSource"):
                    continue
                pos = output.get("pos") or {}
                size = output.get("size") or {}
                left = int(pos.get("x", 0) or 0)
                top = int(pos.get("y", 0) or 0)
                width = int(size.get("width", 0) or 0)
                height = int(size.get("height", 0) or 0)
                # kscreen rotation: 1=normal, 2=90deg, 4=270deg (2/4 swap w/h)
                if output.get("rotation") in (2, 4):
                    width, height = height, width
                if width <= 0 or height <= 0:
                    continue
                monitors.append({
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                    "name": name,
                    "id": 0,
                })
            if monitors:
                monitors.sort(key=lambda m: (m["left"], m["top"]))
                for i, m in enumerate(monitors):
                    m["id"] = i
                return monitors
        except Exception:
            pass

    # Fallback: xrandr (X11/XWayland)
    return get_monitors_xrandr()


def get_monitors_xrandr() -> List[Dict[str, Any]]:
    """Get monitor geometries via xrandr (X11/XWayland)."""
    out = _run(["xrandr", "--listmonitors"])
    if not out:
        return []
    monitors = []
    for line in out.splitlines()[1:]:  # Skip "Monitors: N"
        parts = line.strip().split()
        if len(parts) >= 4:
            # Format: " 0: +*DP-1 1920/508x1080/286+0+0  DP-1"
            idx = int(parts[0].rstrip(":"))
            geom = parts[2]  # "1920/508x1080/286+0+0"
            name = parts[3] if len(parts) > 3 else f"Monitor {idx}"
            # Parse WxH+X+Y
            m = re.match(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)", geom)
            if m:
                w, h, x, y = map(int, m.groups())
                monitors.append({"left": x, "top": y, "width": w, "height": h, "name": name, "id": idx})
    monitors.sort(key=lambda m: (m.get("left", 0), m.get("top", 0)))
    return monitors


def get_windows_kde() -> List[Dict[str, Any]]:
    """
    Get all running windows on KDE Plasma (Wayland or X11) via KWin DBus.

    Uses KRunner's window runner for the window list + KWin.getWindowInfo()
    for each window's details. This sees both native Wayland and XWayland
    windows — unlike wmctrl/xprop, which only see XWayland.

    Returns list of dicts ordered topmost-first, with keys: app, title, pid,
    monitor, rect (rect = [left, top, right, bottom]), desktop, minimized,
    fullscreen, layer, active.
    """
    # Which window is focused right now (via the KWin bridge). The focused
    # window should win the "topmost" contest for its monitor — a fullscreen
    # game, for instance, is focused while you play it even when another
    # window (also covering that monitor) appears earlier in the list.
    active_key = None
    try:
        from .kde_active import get_active_window as _bridge_active
        aw = _bridge_active()
        if aw:
            active_key = (aw.get("app") or "", aw.get("title") or "")
    except Exception:
        pass

    windows: List[Dict[str, Any]] = []
    for uuid in _window_uuids():
        info = _window_info(uuid)
        if not info:
            continue
        title = str(info.get("caption") or "").strip()
        app = str(info.get("resourceClass") or info.get("resourceName") or "").strip()
        if not title or not app:
            continue
        try:
            x, y = int(info.get("x", 0)), int(info.get("y", 0))
            w, h = int(info.get("width", 0)), int(info.get("height", 0))
        except (TypeError, ValueError):
            x = y = w = h = 0
        norm_app = app.lower().replace(".exe", "")
        is_active = active_key is not None and (norm_app, title) == active_key
        windows.append({
            "app": norm_app,
            "title": title,
            "pid": int(info.get("pid", 0) or 0),
            "monitor": None,
            "rect": [x, y, x + w, y + h],
            "desktop": 0,
            "minimized": bool(info.get("minimized", False)),
            "fullscreen": bool(info.get("fullscreen", False)),
            "layer": int(info.get("layer", 0) or 0),
            "active": is_active,
        })

    if windows:
        # Order topmost-first so get_top_window_per_monitor (which keeps the
        # FIRST window covering each monitor) picks the right one:
        #   1. not minimized
        #   2. the focused window
        #   3. fullscreen windows (a game/player covers its screen)
        #   4. higher KWin layer
        # Python's sort is stable, so ties keep KRunner's original order.
        windows.sort(key=lambda w: (
            bool(w.get("minimized")),
            not w.get("active"),
            not w.get("fullscreen"),
            -int(w.get("layer", 0) or 0),
        ))
        return windows

    # Fallback: X11 tools (only XWayland apps will be visible)
    return get_windows_x11()


def get_virtual_desktops_kde() -> List[Dict[str, Any]]:
    """KWin virtual desktops as [{"id", "name", "current"}, ...].

    Reads the ``org.kde.KWin.VirtualDesktopManager`` properties over the
    session bus (Gio). KWin 6 does not implement ``org.kde.kwin.Scripting``'s
    ``evalScript`` in a usable way, so the property path is the reliable one.
    """
    out: List[Dict[str, Any]] = []
    desktops = _dbus_call(
        "org.kde.KWin", "/VirtualDesktopManager",
        "org.freedesktop.DBus.Properties", "Get",
        "(ss)", ("org.kde.KWin.VirtualDesktopManager", "desktops"), "(v)",
    )
    current = _dbus_call(
        "org.kde.KWin", "/VirtualDesktopManager",
        "org.freedesktop.DBus.Properties", "Get",
        "(ss)", ("org.kde.KWin.VirtualDesktopManager", "current"), "(v)",
    )
    current_id = (current[0] if current else None)
    rows = desktops[0] if desktops else []
    for pos, uuid, name in (rows or []):
        out.append({
            "id": uuid,
            "position": int(pos),
            "name": str(name),
            "current": bool(uuid == current_id),
        })
    return out


def get_windows_x11() -> List[Dict[str, Any]]:
    """Get windows via wmctrl/xprop (X11/XWayland fallback)."""
    # wmctrl -lG gives: win_id desktop x y w h host title
    out = _run(["wmctrl", "-lG"])
    if not out:
        return []
    windows = []
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        win_id, desk_str, x, y, w, h, host, title = parts
        desk = int(desk_str)
        if desk == -1:  # Sticky/on all desktops
            desk = 0
        # Skip desktop/window manager windows
        if title.lower() in ("desktop", "kde plasma", "plasmashell", "krunner"):
            continue
        # Get WM_CLASS for app name
        app = get_wm_class(win_id)
        if not app:
            continue
        windows.append({
            "app": app.lower().replace(".exe", ""),
            "title": title.strip(),
            "pid": 0,  # Not available via wmctrl
            "monitor": None,  # Would need xrandr mapping
            "rect": [int(x), int(y), int(w), int(h)],
            "desktop": desk,
            "minimized": False,
        })
    return windows


def get_wm_class(win_id: str) -> Optional[str]:
    """Get WM_CLASS (instance) for a window ID via xprop."""
    out = _run(["xprop", "-id", win_id, "WM_CLASS"])
    if not out:
        return None
    # WM_CLASS(STRING) = "instance", "class"
    m = re.search(r'WM_CLASS\(STRING\) = "([^"]+)"', out)
    if m:
        return m.group(1)
    return None


def get_active_window_kde() -> Optional[Dict[str, Any]]:
    """Get the active/focused window on KDE Plasma.

    Prefers the true focused window reported by the KWin bridge script; falls
    back to KWin's ``queryWindowInfo`` (the window under the pointer) if the
    bridge is unavailable.
    """
    try:
        from .kde_active import get_active_window as _bridge_active
        bridged = _bridge_active()
        if bridged and (bridged.get("app") or bridged.get("title")):
            return bridged
    except Exception:
        pass

    out = _dbus_call(
        "org.kde.KWin", "/KWin", "org.kde.KWin", "queryWindowInfo",
        "()", (), "(a{sv})",
    )
    if out and out[0]:
        info = out[0]
        title = str(info.get("caption") or "").strip()
        app = str(info.get("resourceClass") or info.get("resourceName") or "").strip()
        if title and app:
            try:
                x, y = int(info.get("x", 0)), int(info.get("y", 0))
                w, h = int(info.get("width", 0)), int(info.get("height", 0))
            except (TypeError, ValueError):
                x = y = w = h = 0
            return {
                "app": app.lower().replace(".exe", ""),
                "title": title,
                "pid": int(info.get("pid", 0) or 0),
                "rect": [x, y, x + w, y + h],
            }

    # Fallback: first (most recent) window from the full list
    windows = get_windows_kde()
    if windows:
        w = windows[0]
        return {"app": w["app"], "title": w["title"], "pid": w.get("pid", 0), "rect": w.get("rect")}
    return None


def get_active_window_x11() -> Optional[Dict[str, Any]]:
    """Get active window via xprop (X11/XWayland)."""
    out = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    if not out:
        return None
    m = re.search(r"window id # (0x[0-9a-fA-F]+)", out)
    if not m:
        return None
    win_id = m.group(1)
    title_out = _run(["xprop", "-id", win_id, "_NET_WM_NAME", "WM_NAME"])
    title = ""
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


def get_idle_time_kde() -> Optional[float]:
    """
    Get idle time in seconds on KDE Plasma.
    Uses the Plasma screen-locker's GetSessionIdleTime DBus method, then
    xprintidle (X11) as a last resort. Returns None if unavailable.
    """
    # Plasma screen locker exposes idle time (seconds) on the session bus.
    for dest in ("org.kde.screensaver", "org.kde.ScreenSaver"):
        out = _dbus_call(
            dest, "/ScreenSaver", "org.freedesktop.ScreenSaver",
            "GetSessionIdleTime", "()", (), "(u)", timeout_ms=1500,
        )
        if out and isinstance(out[0], int):
            return float(out[0])

    # Fallback: xprintidle (X11 / XWayland)
    out = _run(["xprintidle"])
    if out and out.isdigit():
        return int(out) / 1000.0

    return None


def is_kde_wayland() -> bool:
    """Check if running on KDE Plasma Wayland."""
    return (
        os.environ.get("XDG_SESSION_TYPE") == "wayland"
        and "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    )


def is_kde_x11() -> bool:
    """Check if running on KDE Plasma X11."""
    return (
        os.environ.get("XDG_SESSION_TYPE") == "x11"
        and "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    )


# ============================================================
# Firefox/LibreWolf window detection (for browser URL capture)
# ============================================================

_FIREFOX_CLASSES = {"firefox", "librewolf", "librewolf-default", "nightly", "firefoxdeveloperedition"}


def is_firefox_window(app: str) -> bool:
    """Check if a window belongs to Firefox or LibreWolf."""
    app_lower = app.lower()
    return any(fx in app_lower for fx in _FIREFOX_CLASSES)


def get_firefox_windows() -> List[Dict[str, Any]]:
    """Get all Firefox/LibreWolf windows with their monitor assignment."""
    windows = get_windows_kde()
    ff_windows = [w for w in windows if is_firefox_window(w.get("app", ""))]
    return ff_windows