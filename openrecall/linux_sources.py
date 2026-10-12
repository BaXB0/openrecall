"""
Comprehensive Linux/KDE data sources for OpenRecall.

Integrates multiple rich data sources beyond basic window enumeration:
- KWin Scripting (real-time window events, virtual desktops)
- KActivities (user activity contexts)
- PipeWire/WirePlumber (audio/video streams, screen sharing)
- Browser Native Messaging (reliable Firefox/LibreWolf tab data)
- AT-SPI2 (accessibility tree for actual app text content)
- systemd journal (system events)
"""
import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable
from pathlib import Path

try:
    import dbus
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False

try:
    import psutil
except ImportError:
    psutil = None

from openrecall.wayland import get_compositor, get_firefox_windows, is_firefox_window
from openrecall.wayland import get_windows as _wayland_get_windows
from openrecall.wayland import get_virtual_desktops as _wayland_get_virtual_desktops


# ============================================================
# DBus Helpers
# ============================================================

def _get_session_bus():
    """Get DBus session bus."""
    if not DBUS_AVAILABLE:
        return None
    try:
        return dbus.SessionBus()
    except Exception:
        return None


def _get_system_bus():
    """Get DBus system bus."""
    if not DBUS_AVAILABLE:
        return None
    try:
        return dbus.SystemBus()
    except Exception:
        return None


def _dbus_call(bus, service: str, path: str, interface: str, method: str, *args):
    """Make a DBus call and return result or None."""
    if not bus:
        return None
    try:
        obj = bus.get_object(service, path)
        iface = dbus.Interface(obj, interface)
        method_proxy = getattr(iface, method)
        return method_proxy(*args)
    except Exception:
        return None


# ============================================================
# KWin Scripting (Real-time window events)
# ============================================================

class KWinWatcher:
    """
    Watches KWin for real-time window events via DBus.
    Emits events for: windowAdded, windowRemoved, windowActivated,
    windowDesktopChanged, windowMinimized, windowMaximized, etc.
    """

    def __init__(self, callback: Callable[[str, Dict], None]):
        self.callback = callback
        self.bus = _get_session_bus()
        self.running = False
        self._thread = None

    def start(self):
        """Start watching KWin events."""
        if not self.bus or self.running:
            return False

        try:
            # Connect to KWin signals
            self.bus.add_signal_receiver(
                self._on_window_added,
                signal_name="windowAdded",
                dbus_interface="org.kde.KWin",
                path="/org/kde/KWin"
            )
            self.bus.add_signal_receiver(
                self._on_window_removed,
                signal_name="windowRemoved",
                dbus_interface="org.kde.KWin",
                path="/org/kde/KWin"
            )
            self.bus.add_signal_receiver(
                self._on_active_window_changed,
                signal_name="activeWindowChanged",
                dbus_interface="org.kde.KWin",
                path="/org/kde/KWin"
            )
            self.bus.add_signal_receiver(
                self._on_window_desktop_changed,
                signal_name="windowDesktopChanged",
                dbus_interface="org.kde.KWin",
                path="/org/kde/KWin"
            )
            self.bus.add_signal_receiver(
                self._on_window_minimized,
                signal_name="windowMinimized",
                dbus_interface="org.kde.KWin",
                path="/org/kde/KWin"
            )
            self.bus.add_signal_receiver(
                self._on_window_maximized,
                signal_name="windowMaximized",
                dbus_interface="org.kde.KWin",
                path="/org/kde/KWin"
            )
            self.running = True
            return True
        except Exception:
            return False

    def stop(self):
        """Stop watching."""
        self.running = False

    def _emit(self, event_type: str, data: Dict):
        if self.callback:
            try:
                self.callback(event_type, data)
            except Exception:
                pass

    def _on_window_added(self, window_id: int):
        self._emit("window_added", {"window_id": window_id, "timestamp": time.time()})

    def _on_window_removed(self, window_id: int):
        self._emit("window_removed", {"window_id": window_id, "timestamp": time.time()})

    def _on_active_window_changed(self, window_id: int):
        self._emit("window_activated", {"window_id": window_id, "timestamp": time.time()})

    def _on_window_desktop_changed(self, window_id: int, desktop: int):
        self._emit("window_desktop_changed", {"window_id": window_id, "desktop": desktop, "timestamp": time.time()})

    def _on_window_minimized(self, window_id: int, minimized: bool):
        self._emit("window_minimized", {"window_id": window_id, "minimized": minimized, "timestamp": time.time()})

    def _on_window_maximized(self, window_id: int, maximized: bool):
        self._emit("window_maximized", {"window_id": window_id, "maximized": maximized, "timestamp": time.time()})


def get_kwin_client_list() -> List[Dict[str, Any]]:
    """Get the current window list (KWin/KDE, or the active compositor).

    Delegates to the Wayland/KDE backend, which enumerates windows via
    KRunner's window runner plus ``KWin.getWindowInfo`` and therefore sees
    both native Wayland and XWayland windows. The previous implementation
    used ``org.kde.kwin.Scripting.evalScript`` (plus the python ``dbus``
    package, which is not installed), so it always returned an empty list.
    """
    try:
        windows = _wayland_get_windows()
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    for w in windows or []:
        rect = list(w.get("rect") or [0, 0, 0, 0])
        left, top, right, bottom = (rect + [0, 0, 0, 0])[:4]
        full = bool(w.get("fullscreen", False))
        out.append({
            "internalId": w.get("uuid"),
            "windowId": None,
            "app": w.get("app", "unknown"),
            "title": w.get("title", ""),
            "pid": int(w.get("pid", 0) or 0),
            "desktop": w.get("desktop", 0),
            "minimized": bool(w.get("minimized", False)),
            "maximized": full,
            "fullScreen": full,
            "active": bool(w.get("active", False)),
            "onAllDesktops": False,
            # Keep the [x, y, w, h] shape the previous callers expected.
            "rect": [left, top, right - left, bottom - top],
            "screen": w.get("monitor"),
        })
    return out


def get_kwin_virtual_desktops() -> List[Dict[str, Any]]:
    """Get virtual desktop info from KWin (delegates to the Wayland backend)."""
    try:
        desktops = _wayland_get_virtual_desktops()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for d in desktops or []:
        out.append({
            "id": d.get("id"),
            "name": d.get("name", ""),
            "current": bool(d.get("current", False)),
        })
    return out


# ============================================================
# KActivities (Activity contexts)
# ============================================================

class KActivitiesWatcher:
    """Watches KActivities for current activity changes."""

    def __init__(self, callback: Callable[[str, Dict], None]):
        self.callback = callback
        self.bus = _get_session_bus()
        self.current_activity = None

    def start(self):
        if not self.bus:
            return False
        try:
            self.bus.add_signal_receiver(
                self._on_activity_changed,
                signal_name="CurrentActivityChanged",
                dbus_interface="org.kde.ActivityManager.Activities",
                path="/ActivityManager/Activities"
            )
            # Get initial activity
            self._update_current()
            return True
        except Exception:
            return False

    def _update_current(self):
        bus = _get_session_bus()
        if not bus:
            return
        try:
            obj = bus.get_object("org.kde.ActivityManager", "/ActivityManager/Activities")
            iface = dbus.Interface(obj, "org.kde.ActivityManager.Activities")
            current = iface.CurrentActivity()
            if current and current != self.current_activity:
                self.current_activity = current
                info = self.get_activity_info(current)
                if self.callback:
                    self.callback("activity_changed", info)
        except Exception:
            pass

    def _on_activity_changed(self, activity_id: str):
        self._update_current()

    def get_activity_info(self, activity_id: str) -> Dict[str, Any]:
        """Get activity name, icon, etc."""
        bus = _get_session_bus()
        if not bus:
            return {"id": activity_id}
        try:
            obj = bus.get_object("org.kde.ActivityManager", "/ActivityManager/Activities")
            iface = dbus.Interface(obj, "org.kde.ActivityManager.Activities")
            name = iface.ActivityName(activity_id)
            icon = iface.ActivityIcon(activity_id)
            return {"id": activity_id, "name": name, "icon": icon}
        except Exception:
            return {"id": activity_id}


def get_current_activity() -> Optional[Dict[str, Any]]:
    """Get the current KDE activity."""
    bus = _get_session_bus()
    if not bus:
        return None
    try:
        obj = bus.get_object("org.kde.ActivityManager", "/ActivityManager/Activities")
        iface = dbus.Interface(obj, "org.kde.ActivityManager.Activities")
        current = iface.CurrentActivity()
        if current:
            name = iface.ActivityName(current)
            icon = iface.ActivityIcon(current)
            return {"id": current, "name": name, "icon": icon}
    except Exception:
        pass
    return None


# ============================================================
# PipeWire/WirePlumber (Audio/Video streams)
# ============================================================

def get_pipewire_streams() -> List[Dict[str, Any]]:
    """Get active PipeWire streams (screen sharing, audio capture, etc.)."""
    try:
        # Use pw-cli to list streams
        out = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=3)
        if out.returncode != 0:
            return []

        streams = []
        current = {}
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.startswith("id "):
                if current:
                    streams.append(current)
                current = {"id": line.split()[1]}
            elif "=" in line and current:
                key, val = line.split("=", 1)
                current[key.strip()] = val.strip().strip('"')

        if current:
            streams.append(current)

        # Filter for interesting streams
        interesting = []
        for s in streams:
            media_class = s.get("media.class", "")
            if any(x in media_class for x in ["Stream/Output/Audio", "Stream/Input/Audio", "Video/Source", "Video/Sink"]):
                interesting.append({
                    "id": s.get("id"),
                    "media_class": media_class,
                    "name": s.get("node.name", ""),
                    "app_name": s.get("application.name", ""),
                    "app_id": s.get("application.id", ""),
                    "pid": s.get("application.process.id", ""),
                })
        return interesting
    except Exception:
        return []


def is_screen_sharing_active() -> bool:
    """Check if screen sharing/recording is active via PipeWire."""
    streams = get_pipewire_streams()
    for s in streams:
        if "Video/Source" in str(s.get("media_class", "") or "") or "screen" in str(s.get("name", "") or "").lower():
            return True
    return False


def is_in_call() -> bool:
    """Check if user is likely in a voice/video call.

    Heuristic: a call needs an active microphone (input) stream *and* an
    output stream at the same time. Audio-only playback (music/video) has no
    input stream and is not a call.
    """
    streams = get_pipewire_streams()
    has_input = False
    has_output = False
    for s in streams:
        mc = str(s.get("media_class", "") or "")
        if "Stream/Input/Audio" in mc:
            has_input = True
        elif "Stream/Output/Audio" in mc:
            has_output = True
    return has_input and has_output


# ============================================================
# Browser Native Messaging (Firefox/LibreWolf)
# ============================================================

# Default install location of the native-messaging host (per-user, portable).
_DEFAULT_HOST_PATH = os.path.expanduser("~/.local/bin/openrecall-browser-host")

# Native messaging host manifest for Firefox/LibreWolf
NATIVE_MANIFEST = {
    "name": "openrecall_browser",
    "description": "OpenRecall browser tab data collector",
    "path": _DEFAULT_HOST_PATH,
    "type": "stdio",
    "allowed_extensions": ["openrecall-browser@localhost"]
}

# The webextension would be installed separately. For now, we provide
# a helper to generate the manifest and host script.

def generate_native_manifest(install_path: str = None) -> Dict[str, Any]:
    """Generate native messaging manifest for Firefox/LibreWolf."""
    path = install_path or _DEFAULT_HOST_PATH
    return {
        "name": "openrecall_browser",
        "description": "OpenRecall browser tab data collector",
        "path": path,
        "type": "stdio",
        "allowed_extensions": ["openrecall-browser@localhost"]
    }


def install_native_manifest(browser: str = "firefox"):
    """Install native messaging manifest for Firefox or LibreWolf."""
    manifest = generate_native_manifest()
    manifest_json = json.dumps(manifest, indent=2)

    if browser == "firefox":
        base = Path.home() / ".mozilla" / "native-messaging-hosts"
    elif browser == "librewolf":
        base = Path.home() / ".librewolf" / "native-messaging-hosts"
    else:
        return False

    base.mkdir(parents=True, exist_ok=True)
    manifest_path = base / "openrecall_browser.json"
    manifest_path.write_text(manifest_json)
    return True


# Host script that the browser extension would communicate with
NATIVE_HOST_SCRIPT = """#!/usr/bin/env python3
\"\"\"
Native messaging host for Firefox/LibreWolf.
Receives tab data from the webextension and writes to stdout for OpenRecall to collect.
\"\"\"
import json
import sys
import struct
import os

def send_message(message):
    \"\"\"Send a message to the browser.\"\"\"
    encoded = json.dumps(message).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('@I', len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()

def read_message():
    \"\"\"Read a message from the browser.\"\"\"
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        return None
    length = struct.unpack('@I', raw_length)[0]
    encoded = sys.stdin.buffer.read(length)
    return json.loads(encoded.decode('utf-8'))

def main():
    # This host receives tab updates from the webextension
    # and forwards them to OpenRecall via a local file/socket
    data_file = os.path.expanduser("~/.local/share/openrecall/browser_tabs.json")
    os.makedirs(os.path.dirname(data_file), exist_ok=True)

    while True:
        msg = read_message()
        if msg is None:
            break
        if msg.get("type") == "tabs_update":
            with open(data_file, "w") as f:
                json.dump(msg["data"], f)
        elif msg.get("type") == "ping":
            send_message({"type": "pong"})

if __name__ == "__main__":
    main()
"""


def read_browser_tabs_native() -> List[Dict[str, Any]]:
    """Read browser tabs from native messaging host data file."""
    data_file = Path.home() / ".local" / "share" / "openrecall" / "browser_tabs.json"
    if data_file.exists():
        try:
            with open(data_file) as f:
                return json.load(f)
        except Exception:
            pass
    return []


# ============================================================
# AT-SPI2 (Accessibility) - for actual app text content
# ============================================================

class ATSPIWatcher:
    """Uses AT-SPI2 to get actual text content from applications."""

    def __init__(self):
        self.registry = None
        self._init_atspi()

    def _init_atspi(self):
        try:
            import gi
            gi.require_version('Atspi', '2.0')
            from gi.repository import Atspi
            Atspi.init()
            self.registry = Atspi.get_registry()
            self.Atspi = Atspi
        except Exception:
            self.registry = None

    def get_text_for_window(self, pid: int) -> Optional[str]:
        """Get text content for a window by PID."""
        if not self.registry:
            return None
        try:
            desktop = self.registry.get_desktop(0)
            for app in desktop:
                if app.get_process_id() == pid:
                    return self._extract_text(app)
        except Exception:
            pass
        return None

    def _extract_text(self, accessible) -> str:
        """Recursively extract text from accessible tree."""
        texts = []
        try:
            # Get text from this node
            if accessible.get_role() in (self.Atspi.Role.TEXT, self.Atspi.Role.LABEL,
                                          self.Atspi.Role.ENTRY, self.Atspi.Role.TERMINAL):
                text = accessible.get_text(0, -1)
                if text:
                    texts.append(text)

            # Recurse children
            for i in range(accessible.get_child_count()):
                child = accessible.get_child_at_index(i)
                if child:
                    texts.append(self._extract_text(child))
        except Exception:
            pass
        return " ".join(texts)


# ============================================================
# systemd Journal
# ============================================================

def get_recent_journal_entries(since_minutes: int = 60, limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent systemd journal entries."""
    try:
        since = f"-{since_minutes}min"
        out = subprocess.run(
            ["journalctl", "--since", since, "-n", str(limit), "-o", "json"],
            capture_output=True, text=True, timeout=5
        )
        if out.returncode != 0:
            return []

        entries = []
        for line in out.stdout.strip().splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries
    except Exception:
        return []


def get_user_session_events(since_minutes: int = 60) -> List[Dict[str, Any]]:
    """Get user session related events (login, unlock, etc.)."""
    entries = get_recent_journal_entries(since_minutes, 100)
    events = []
    for e in entries:
        msg = str(e.get("MESSAGE", "") or "").lower()
        unit = e.get("_SYSTEMD_UNIT", "")
        if any(x in msg for x in ["session", "unlock", "login", "authenticate"]):
            events.append({
                "timestamp": e.get("__REALTIME_TIMESTAMP"),
                "message": e.get("MESSAGE"),
                "unit": unit,
                "priority": e.get("PRIORITY"),
            })
    return events


# ============================================================
# Unified Data Collector
# ============================================================

class LinuxDataCollector:
    """
    Unified collector that aggregates all Linux/KDE data sources.
    Designed to be called periodically from the capture loop.
    """

    def __init__(self):
        self.kwin_watcher = None
        self.kactivities_watcher = None
        self.atspi = ATSPIWatcher()
        self._event_log = []
        self._lock = threading.Lock()
        self._last_collection = 0

    def start_watchers(self):
        """Start real-time watchers."""
        self.kwin_watcher = KWinWatcher(self._on_kwin_event)
        self.kwin_watcher.start()

        self.kactivities_watcher = KActivitiesWatcher(self._on_activity_event)
        self.kactivities_watcher.start()

    def stop_watchers(self):
        """Stop real-time watchers."""
        if self.kwin_watcher:
            self.kwin_watcher.stop()
        if self.kactivities_watcher:
            pass  # No stop method needed

    def _on_kwin_event(self, event_type: str, data: Dict):
        with self._lock:
            self._event_log.append({
                "source": "kwin",
                "type": event_type,
                "data": data,
                "timestamp": time.time()
            })
            # Keep only last 1000 events
            if len(self._event_log) > 1000:
                self._event_log = self._event_log[-1000:]

    def _on_activity_event(self, event_type: str, data: Dict):
        with self._lock:
            self._event_log.append({
                "source": "kactivities",
                "type": event_type,
                "data": data,
                "timestamp": time.time()
            })

    def collect_snapshot(self) -> Dict[str, Any]:
        """Collect a full snapshot of all data sources."""
        now = time.time()
        self._last_collection = now

        snapshot = {
            "timestamp": now,
            "compositor": get_compositor(),
            "kwin": {
                "windows": get_kwin_client_list(),
                "virtual_desktops": get_kwin_virtual_desktops(),
            },
            "kactivities": get_current_activity(),
            "pipewire": {
                "streams": get_pipewire_streams(),
                "screen_sharing": is_screen_sharing_active(),
                "in_call": is_in_call(),
            },
            "browser": {
                "native_tabs": read_browser_tabs_native(),
                "firefox_windows": get_firefox_windows(),
            },
            "journal": get_user_session_events(60),
            "events": self._get_recent_events(60),
        }

        return snapshot

    def _get_recent_events(self, seconds: int) -> List[Dict]:
        cutoff = time.time() - seconds
        with self._lock:
            return [e for e in self._event_log if e["timestamp"] >= cutoff]

    def get_window_text_content(self, pid: int) -> Optional[str]:
        """Get actual text content from a window using AT-SPI2."""
        return self.atspi.get_text_for_window(pid)


# Global collector instance
_collector = None


def get_collector() -> LinuxDataCollector:
    """Get or create the global collector instance."""
    global _collector
    if _collector is None:
        _collector = LinuxDataCollector()
    return _collector


def collect_linux_snapshot() -> Dict[str, Any]:
    """Convenience function to collect a snapshot."""
    return get_collector().collect_snapshot()


def start_linux_watchers():
    """Start real-time watchers."""
    get_collector().start_watchers()


def stop_linux_watchers():
    """Stop real-time watchers."""
    get_collector().stop_watchers()