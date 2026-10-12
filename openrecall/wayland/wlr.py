"""
Generic Wayland backend using wlrctl (works on wlroots-based compositors:
Hyprland, Sway, Wayfire, etc.). Falls back to compositor-specific tools.
"""
import json
import os
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


def _run_json(cmd: List[str], timeout: float = 2.0) -> Optional[Any]:
    out = _run(cmd, timeout)
    if out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return None


def is_wlroots_compositor() -> bool:
    """Check if running on a wlroots-based compositor (Hyprland, Sway, etc.)."""
    return bool(os.environ.get("WAYLAND_DISPLAY")) and (
        os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        or os.environ.get("SWAYSOCK")
        or _run(["which", "wlrctl"])
    )


def get_monitors_wlrctl() -> List[Dict[str, Any]]:
    """Get monitors via wlrctl (wlroots compositors)."""
    out = _run_json(["wlrctl", "output", "list"])
    if not out:
        return []
    monitors = []
    for i, m in enumerate(out):
        # wlrctl output: {"name": "DP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, ...}
        monitors.append({
            "left": m.get("x", 0),
            "top": m.get("y", 0),
            "width": m.get("width", 0),
            "height": m.get("height", 0),
            "name": m.get("name", f"Monitor {i}"),
            "id": i,
        })
    monitors.sort(key=lambda m: (m.get("left", 0), m.get("top", 0)))
    return monitors


def get_windows_wlrctl() -> List[Dict[str, Any]]:
    """Get windows via wlrctl (wlroots compositors)."""
    # wlrctl doesn't have a direct "list windows" command
    # This is a placeholder - wlroots compositors need compositor-specific tools
    return []


def get_active_window_wlrctl() -> Optional[Dict[str, Any]]:
    """Get active window via wlrctl."""
    # Not directly supported by wlrctl
    return None


def get_idle_time_wlrctl() -> Optional[float]:
    """Get idle time via wlrctl idle protocol."""
    # wlrctl idle get-time returns milliseconds
    out = _run(["wlrctl", "idle", "get-time"])
    if out and out.isdigit():
        return int(out) / 1000.0
    return None