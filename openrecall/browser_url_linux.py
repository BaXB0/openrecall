"""
Firefox/LibreWolf browser tab capture on Linux via Remote Debugging Protocol.

Requires Firefox/LibreWolf to be launched with remote debugging enabled:
    firefox --start-debugger-server 6000
    # or
    MOZ_REMOTE_DEBUG=1 firefox

The protocol is Firefox's own Debugger Protocol (not CDP), communicated over TCP.
"""
import json
import socket
import subprocess
import time
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse

try:
    import psutil
except ImportError:
    psutil = None

from openrecall.wayland import get_firefox_windows, is_firefox_window
from openrecall.utils import monitor_for_rect_area


# Firefox Debugger Protocol constants
DEBUGGER_PORT = 6000
DEBUGGER_HOST = "127.0.0.1"
CONNECTION_TIMEOUT = 2.0
READ_TIMEOUT = 5.0


class FirefoxDebuggerClient:
    """Client for Firefox Remote Debugging Protocol."""

    def __init__(self, host: str = DEBUGGER_HOST, port: int = DEBUGGER_PORT):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.request_id = 0
        self._buffer = b""

    def connect(self) -> bool:
        """Connect to Firefox debugger server."""
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=CONNECTION_TIMEOUT)
            self.sock.settimeout(READ_TIMEOUT)
            # Read hello packet
            hello = self._read_line()
            if hello:
                return True
        except Exception:
            pass
        return False

    def disconnect(self):
        """Close connection."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _send(self, msg: Dict[str, Any]) -> int:
        """Send a message and return request ID."""
        if not self.sock:
            return 0
        self.request_id += 1
        msg["id"] = self.request_id
        data = json.dumps(msg) + "\n"
        try:
            self.sock.sendall(data.encode())
            return self.request_id
        except Exception:
            return 0

    def _read_line(self) -> Optional[str]:
        """Read a newline-delimited JSON message."""
        if not self.sock:
            return None
        try:
            while b"\n" not in self._buffer:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return None
                self._buffer += chunk
            line, self._buffer = self._buffer.split(b"\n", 1)
            return line.decode("utf-8", errors="replace").strip()
        except Exception:
            return None

    def _wait_for_response(self, request_id: int, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """Wait for a response matching the request ID."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._read_line()
            if not line:
                continue
            try:
                resp = json.loads(line)
                if resp.get("id") == request_id:
                    return resp
            except json.JSONDecodeError:
                continue
        return None

    def list_tabs(self) -> List[Dict[str, Any]]:
        """List all tabs across all windows."""
        req_id = self._send({"to": "root", "type": "listTabs"})
        if not req_id:
            return []
        resp = self._wait_for_response(req_id)
        if not resp or "tabs" not in resp:
            return []
        return resp["tabs"]

    def get_tab_details(self, tab_actor: str) -> Optional[Dict[str, Any]]:
        """Get detailed info for a specific tab actor."""
        req_id = self._send({"to": tab_actor, "type": "getTab"})
        if not req_id:
            return None
        resp = self._wait_for_response(req_id)
        if not resp or "tab" not in resp:
            return None
        return resp["tab"]


def _find_firefox_debugger_port() -> Optional[int]:
    """Try to find the Firefox debugger port by checking listening ports."""
    if not psutil:
        return None
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN" and conn.laddr.port in (6000, 6001, 6002, 6003, 6004, 6005):
                # Check if it's a Firefox process
                try:
                    proc = psutil.Process(conn.pid)
                    if proc.name().lower() in ("firefox", "librewolf", "firefox-bin", "librewolf-bin"):
                        return conn.laddr.port
                except Exception:
                    pass
    except Exception:
        pass
    return None


def _get_firefox_windows_with_pids() -> List[Dict[str, Any]]:
    """Get Firefox windows with their PIDs for port matching."""
    windows = get_firefox_windows()
    result = []
    for w in windows:
        pid = w.get("pid", 0)
        if pid:
            result.append({**w, "pid": pid})
    return result


def _match_window_to_debugger(window: Dict[str, Any], tabs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Match a Firefox window to its active tab from the debugger."""
    # The debugger doesn't directly give us window IDs, but we can match by title
    win_title = window.get("title", "").strip().lower()
    if not win_title:
        return None

    for tab in tabs:
        tab_title = (tab.get("title") or "").strip().lower()
        tab_url = tab.get("url") or ""
        selected = tab.get("selected", False)

        if selected and tab_title:
            # Title matching - Firefox window title usually contains the active tab title
            if tab_title in win_title or win_title in tab_title:
                return {
                    "title": tab.get("title", ""),
                    "url": tab_url,
                    "actor": tab.get("actor"),
                    "selected": True,
                }
    return None


def get_browser_tabs_firefox(monitors=None) -> List[Dict[str, Any]]:
    """
    Get active tabs from Firefox/LibreWolf windows on Linux.

    Returns: [{"title": "...", "url": "...", "monitor": 0, "active": True}, ...]
    """
    # Try to find debugger port
    port = _find_firefox_debugger_port()
    if not port:
        return []

    client = FirefoxDebuggerClient(port=port)
    if not client.connect():
        return []

    try:
        tabs = client.list_tabs()
        if not tabs:
            return []

        # Get Firefox windows from Wayland/X11
        ff_windows = _get_firefox_windows_with_pids()
        if not ff_windows:
            return []

        results = []
        for window in ff_windows:
            matched_tab = _match_window_to_debugger(window, tabs)
            if not matched_tab:
                continue

            # Determine monitor
            monitor = window.get("monitor")
            if monitor is None and window.get("rect") and monitors:
                monitor = monitor_for_rect_area(window["rect"], monitors)

            results.append({
                "title": matched_tab["title"],
                "url": matched_tab["url"],
                "monitor": monitor,
                "active": True,
            })

        return results
    finally:
        client.disconnect()


def get_browser_tabs_chromium_cdp(monitors=None) -> List[Dict[str, Any]]:
    """
    Get active tabs from Chromium-based browsers (Chrome, Edge, Brave, Vivaldi)
    via Chrome DevTools Protocol (CDP) on port 9222.

    Requires browser launched with: --remote-debugging-port=9222
    """
    # This is a placeholder for future implementation
    # CDP is well-documented and easier than Firefox's protocol
    return []


def get_browser_tabs(monitors=None) -> List[Dict[str, Any]]:
    """
    Main entry point: get active browser tabs for all supported browsers.

    On Linux: Firefox/LibreWolf via remote debugging (primary)
    On Windows: UI Automation (original implementation)
    """
    import sys

    if sys.platform.startswith("linux"):
        # Firefox/LibreWolf (primary for this user)
        tabs = get_browser_tabs_firefox(monitors)
        # Could add Chromium CDP here if needed
        return tabs
    else:
        # Windows: fall back to original UI Automation implementation
        # This will be called from the original browser_url.py on Windows
        return []


def get_browser_url() -> Optional[str]:
    """Convenience: get URL of the currently active browser tab."""
    tabs = get_browser_tabs()
    for t in tabs:
        if t.get("url"):
            return t["url"]
    return None