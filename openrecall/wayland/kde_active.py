"""Real active-window tracking for KDE Plasma (Wayland) via a KWin script.

KWin's DBus interface does not expose the *focused* window: ``queryWindowInfo``
returns the window under the pointer, and the window list has no focus order.
The sanctioned way to learn which window is actually focused is to run a small
KWin script inside the compositor, which reads ``workspace.activeWindow`` and
reports it back over the session bus (``callDBus``).

This module:

1. Registers a session-bus service (``org.openrecall.KWinBridge``) on a
   background thread with a GLib main loop.
2. Loads a tiny KWin script once; the script reports the active window on load
   and whenever ``windowActivated`` fires, calling back into our service.
3. Caches the latest value, which :func:`get_active_window` returns instantly.

Everything degrades to ``None`` if any step is unavailable, so callers can fall
back to their own heuristics.
"""
import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional

_BRIDGE_NAME = "org.openrecall.KWinBridge"
_BRIDGE_PATH = "/Bridge"
_SCRIPT_NAME = "openrecall_active_bridge"
_SCRIPT_PATH = os.path.join(tempfile.gettempdir(), "openrecall_active_bridge.js")

_IFACE_XML = f"""
<node>
  <interface name="{_BRIDGE_NAME}">
    <method name="ReportActive">
      <arg type="s" name="payload" direction="in"/>
    </method>
  </interface>
</node>
"""

# KWin 6 renamed clientList/clientActivated to windowList/windowActivated; the
# script handles both so it works on Plasma 5 and 6.
_KWIN_SCRIPT = r"""
function _oc_payload(c) {
    if (!c) return;
    var payload = JSON.stringify({
        app: (c.resourceClass || c.resourceName || ""),
        title: (c.caption || ""),
        pid: c.pid || 0
    });
    callDBus("org.openrecall.KWinBridge", "/Bridge",
             "org.openrecall.KWinBridge", "ReportActive", payload);
}

function _oc_reportActiveWindow() {
    var c = (typeof workspace.activeWindow !== "undefined" && workspace.activeWindow)
            ? workspace.activeWindow
            : (workspace.activeClient || null);
    _oc_payload(c);
}

(function () {
    _oc_reportActiveWindow();
    var sig = workspace.windowActivated || workspace.clientActivated;
    if (sig) { sig.connect(_oc_reportActiveWindow); }
})();
"""

_lock = threading.Lock()
_started = False
_active: Dict[str, Any] = {"app": "", "title": "", "pid": 0, "ts": 0.0}


def _gi():
    try:
        import gi  # type: ignore
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib  # type: ignore
        return Gio, GLib
    except Exception:
        return None


def _load_kwin_script(Gio, GLib, bus) -> bool:
    """(Re)load the KWin bridge script into the running compositor."""
    try:
        with open(_SCRIPT_PATH, "w", encoding="utf-8") as fh:
            fh.write(_KWIN_SCRIPT)
    except OSError:
        return False

    def call(method: str, in_sig: str, in_args: tuple, reply_sig: str):
        try:
            return bus.call_sync(
                "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", method,
                GLib.Variant(in_sig, in_args),
                GLib.VariantType(reply_sig),
                Gio.DBusCallFlags.NONE, 3000, None,
            )
        except Exception:
            return None

    call("unloadScript", "(s)", (_SCRIPT_NAME,), "(b)")  # ignore if absent
    loaded = call("loadScript", "(ss)", (_SCRIPT_PATH, _SCRIPT_NAME), "(i)")
    call("start", "()", (), "()")
    return loaded is not None


def _bridge_thread() -> None:
    g = _gi()
    if not g:
        return
    Gio, GLib = g
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception:
        return

    def on_method_call(conn, sender, path, iface_name, method, params, invocation):
        try:
            if method == "ReportActive":
                payload = params.unpack()[0]
                data = json.loads(payload)
                _active.update(
                    app=str(data.get("app", "") or "").lower().replace(".exe", ""),
                    title=str(data.get("title", "") or "").strip(),
                    pid=int(data.get("pid", 0) or 0),
                    ts=time.time(),
                )
        except Exception:
            pass
        try:
            invocation.return_value(None)
        except Exception:
            pass

    try:
        node = Gio.DBusNodeInfo.new_for_xml(_IFACE_XML)
        bus.register_object(_BRIDGE_PATH, node.interfaces[0], on_method_call, None, None)
        Gio.bus_own_name_on_connection(bus, _BRIDGE_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
    except Exception:
        return

    _load_kwin_script(Gio, GLib, bus)
    try:
        GLib.MainLoop().run()
    except Exception:
        pass


def ensure_bridge() -> None:
    """Start the DBus bridge + KWin script once (idempotent, non-blocking)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_bridge_thread, daemon=True, name="kwin-active-bridge").start()


def get_active_window() -> Optional[Dict[str, Any]]:
    """Return the focused window reported by KWin, or None if unavailable."""
    ensure_bridge()
    # Give the script a moment to call back the first time it is needed.
    if not _active["app"] and _active["ts"] == 0.0:
        deadline = time.time() + 1.5
        while time.time() < deadline and not _active["app"]:
            time.sleep(0.05)
    if _active["app"] or _active["title"]:
        return {
            "app": _active["app"],
            "title": _active["title"],
            "pid": _active["pid"],
            "rect": None,
        }
    return None
