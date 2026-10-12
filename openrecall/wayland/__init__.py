"""
Wayland/X11 compositor detection and unified window/monitor interface.

Detects the running compositor at runtime and provides a consistent API:
- get_monitors() -> List[{"left", "top", "width", "height", "name", "id"}]
- get_windows() -> List[{"app", "title", "pid", "monitor", "rect", "desktop", "minimized"}]
- get_active_window() -> {"app", "title", "pid", "rect"} | None
- get_idle_time() -> float (seconds) | None
- is_firefox_window(app) -> bool
- get_firefox_windows() -> List[window dicts]
"""
import os
import sys
from typing import List, Dict, Any, Optional

# Import backends
from .kde import (
    get_monitors_kde, get_windows_kde, get_active_window_kde, get_idle_time_kde,
    is_kde_wayland, is_kde_x11, is_firefox_window, get_firefox_windows,
    get_virtual_desktops_kde,
)
from .wlr import get_monitors_wlrctl, get_windows_wlrctl, get_active_window_wlrctl, get_idle_time_wlrctl, is_wlroots_compositor
from .x11 import (
    get_monitors_xrandr, get_windows_x11, get_active_window_x11, get_idle_time_x11,
    is_x11_session, is_xwayland
)


_COMPOSITOR = None
_MONITORS_CACHE = None


def detect_compositor() -> str:
    """Detect the running compositor/window system.

    Returns one of: 'kde-wayland', 'kde-x11', 'wlr-hyprland', 'wlr-sway', 'wlr-generic', 'x11', 'xwayland', 'unknown'
    """
    global _COMPOSITOR
    if _COMPOSITOR:
        return _COMPOSITOR

    # KDE Plasma Wayland
    if is_kde_wayland():
        _COMPOSITOR = "kde-wayland"
        return _COMPOSITOR

    # KDE Plasma X11
    if is_kde_x11():
        _COMPOSITOR = "kde-x11"
        return _COMPOSITOR

    # wlroots-based (Hyprland, Sway, etc.)
    if is_wlroots_compositor():
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            _COMPOSITOR = "wlr-hyprland"
        elif os.environ.get("SWAYSOCK"):
            _COMPOSITOR = "wlr-sway"
        else:
            _COMPOSITOR = "wlr-generic"
        return _COMPOSITOR

    # Pure X11
    if is_x11_session():
        _COMPOSITOR = "x11"
        return _COMPOSITOR

    # XWayland (X11 apps on Wayland)
    if is_xwayland():
        _COMPOSITOR = "xwayland"
        return _COMPOSITOR

    _COMPOSITOR = "unknown"
    return _COMPOSITOR


def get_compositor() -> str:
    """Get cached compositor detection."""
    if _COMPOSITOR is None:
        detect_compositor()
    return _COMPOSITOR


def get_monitors() -> List[Dict[str, Any]]:
    """Get monitor geometries for the current compositor."""
    global _MONITORS_CACHE
    if _MONITORS_CACHE is not None:
        return _MONITORS_CACHE

    comp = get_compositor()

    if comp == "kde-wayland" or comp == "kde-x11":
        monitors = get_monitors_kde()
    elif comp.startswith("wlr-"):
        monitors = get_monitors_wlrctl()
        if not monitors:
            monitors = get_monitors_xrandr()  # Fallback
    else:
        monitors = get_monitors_xrandr()

    # Ensure we have valid monitors
    if not monitors:
        # Ultimate fallback: single virtual monitor
        monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080, "name": "Default", "id": 0}]

    _MONITORS_CACHE = monitors
    return monitors


def invalidate_monitor_cache():
    """Force re-detection of monitors (e.g., after display config change)."""
    global _MONITORS_CACHE
    _MONITORS_CACHE = None


def get_windows() -> List[Dict[str, Any]]:
    """Get all visible windows for the current compositor."""
    comp = get_compositor()

    if comp == "kde-wayland" or comp == "kde-x11":
        return get_windows_kde()
    elif comp.startswith("wlr-"):
        # wlrctl doesn't support window listing well; try X11 fallback
        windows = get_windows_x11()
        if windows:
            return windows
        # For Hyprland/Sway, we'd need compositor-specific tools
        # This is a known limitation - users on pure Wayland without KDE
        # need hyprctl/swaymsg backends
        return []
    else:
        return get_windows_x11()


def get_active_window() -> Optional[Dict[str, Any]]:
    """Get the currently active/focused window."""
    comp = get_compositor()

    if comp == "kde-wayland" or comp == "kde-x11":
        return get_active_window_kde()
    elif comp.startswith("wlr-"):
        # Try compositor-specific first, then X11
        active = get_active_window_wlrctl()
        if active:
            return active
        return get_active_window_x11()
    else:
        return get_active_window_x11()


def get_virtual_desktops() -> List[Dict[str, Any]]:
    """Get virtual desktops for the current compositor (KDE only for now)."""
    comp = get_compositor()
    if comp == "kde-wayland" or comp == "kde-x11":
        return get_virtual_desktops_kde()
    return []


def get_idle_time() -> Optional[float]:
    """Get user idle time in seconds."""
    comp = get_compositor()

    if comp == "kde-wayland" or comp == "kde-x11":
        return get_idle_time_kde()
    elif comp.startswith("wlr-"):
        idle = get_idle_time_wlrctl()
        if idle is not None:
            return idle
        return get_idle_time_x11()
    else:
        return get_idle_time_x11()


def is_user_active(idle_threshold: float = 5.0) -> bool:
    """Check if user has been active within idle_threshold seconds."""
    idle = get_idle_time()
    if idle is None:
        return True  # Assume active if we can't detect
    return idle < idle_threshold


# Firefox/LibreWolf helpers (re-export from kde module)
__all__ = [
    "detect_compositor",
    "get_compositor",
    "get_monitors",
    "invalidate_monitor_cache",
    "get_windows",
    "get_active_window",
    "get_virtual_desktops",
    "get_idle_time",
    "is_user_active",
    "is_firefox_window",
    "get_firefox_windows",
]