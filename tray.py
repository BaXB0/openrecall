"""System-tray launcher for OpenRecall (Linux/KDE Plasma compatible).

Run with: python tray.py (or via tray.sh). Keeps an icon in the system tray
and manages the OpenRecall app (capture loop + web UI on port 8082) as a
background child process.

The tray IS the program: 'Exit OpenRecall' (or the tray process ending)
force-kills the openrecall.app child AND any running bulk_summary.
Every tray start first kills any stale/orphaned app instance.

The tray ALSO watches the AI day-summary pipeline (bulk_summary.py):
- Auto-starts a run over all finished days (--no-today) on launch
- Shows live stage (vision / writer-hour-analysis / overview) in tooltip
- "AI Summary" submenu shows per-day progress, current hour, latest scene
- Balloon notifications when run starts/stops or a day's summary finishes
- Can START/STOP bulk_summary from the submenu

Progress is read from logs/bulk_summary.out.log + summaries/ folder.
"""
import atexit
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from openrecall import ai_status
from openrecall import config

DATA_FOLDER = ai_status.appdata_folder
SUMMARY_LOG_PATH = ai_status.progress_log_path()
SUMMARIES_DIR_PATH = ai_status.summaries_path


def log(msg):
    try:
        log_dir = os.path.join(DATA_FOLDER, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "tray.log"), "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")
    except Exception:
        pass


log("tray starting")

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    import traceback
    log("import error:\n" + traceback.format_exc())
    raise


# Use the current Python interpreter (assumes running from venv)
PY = sys.executable
VENV_SITE = os.path.join(ROOT, ".venv", "lib", "python3.11", "site-packages")
URL = "http://localhost:8082"


def _spawn_env():
    env = dict(os.environ)
    bits = [p for p in (VENV_SITE, env.get("PYTHONPATH")) if p]
    env["PYTHONPATH"] = os.pathsep.join(bits)
    return env


_child = {"proc": None}
_bulk = {"proc": None, "fh": None}


def _pid_cmdlines():
    """Yields (pid, cmdline parts) for every process."""
    try:
        import psutil as _psutil
    except Exception:
        return
    for proc in _psutil.process_iter(["pid", "cmdline"]):
        try:
            yield proc.info["pid"], proc.info["cmdline"] or []
        except Exception:
            continue


def _kill_process_matching(marker: str):
    """Force-kills every process whose cmdline mentions `marker`."""
    targets = set()
    for pid, cmd in _pid_cmdlines():
        if any(marker in c for c in cmd):
            targets.add(pid)
    for pid in targets:
        try:
            # Linux: use kill -9 (SIGKILL)
            subprocess.Popen(["kill", "-9", str(pid)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log(f"stopped {marker} pid={pid}")
        except Exception as e:
            log(f"kill {marker}: {e}")


def _kill_app_processes():
    """Force-kills every `openrecall.app` instance."""
    _kill_process_matching("openrecall.app")


def _kill_bulk_summary_processes():
    """Stops any running bulk_summary."""
    _kill_process_matching("bulk_summary.py")
    _bulk["proc"] = None
    fh = _bulk.get("fh")
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
        _bulk["fh"] = None


def _wait_port_closed(timeout: float = 15.0) -> bool:
    """Blocks until nothing is listening on 8082."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 8082), timeout=0.25):
                time.sleep(0.25)
        except OSError:
            return True
    return False


def ensure_app():
    """Kill stale app, wait for port, launch fresh child."""
    _kill_app_processes()
    if not _wait_port_closed():
        log("WARNING: port 8082 still bound after stopping the app")
    _child["proc"] = subprocess.Popen(
        [PY, "-m", "openrecall.app"],
        cwd=ROOT,
        env=_spawn_env(),
        # No CREATE_NO_WINDOW on Linux
    )
    log(f"started openrecall.app pid={_child['proc'].pid}")


def _tray_already_running():
    """PID file lock to prevent multiple tray instances."""
    lock_path = os.path.join(DATA_FOLDER, "tray.lock")
    try:
        if os.path.exists(lock_path):
            with open(lock_path, "r", encoding="utf-8") as f:
                pid_str = f.read().strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                if pid != os.getpid():
                    try:
                        import psutil as _psutil
                        p = _psutil.Process(pid)
                        if p.is_running():
                            cmd = p.cmdline() or []
                            if any(c.endswith("tray.py") or c == "tray.py" for c in cmd):
                                log(f"guard match via PID file: pid={pid}")
                                return True
                    except (_psutil.NoSuchProcess, _psutil.AccessDenied, _psutil.ZombieProcess):
                        pass
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return False


def _open_path(path: str):
    """Open file/folder with xdg-open (Linux standard)."""
    try:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"xdg-open failed: {e}")


def open_ui(icon, item):
    try:
        webbrowser.open(URL)
    except Exception as e:
        log(f"open_ui: {e}")


def open_data_folder(icon, item):
    _open_path(DATA_FOLDER)


def shutdown_all():
    """Stop all OpenRecall workers: app child + bulk_summary."""
    _kill_app_processes()
    _kill_bulk_summary_processes()
    # Clean up PID lock file
    try:
        lock_path = os.path.join(DATA_FOLDER, "tray.lock")
        if os.path.exists(lock_path):
            with open(lock_path, "r", encoding="utf-8") as f:
                pid_str = f.read().strip()
            if pid_str == str(os.getpid()):
                os.remove(lock_path)
    except Exception:
        pass


def stop_openrecall(icon, item):
    shutdown_all()
    icon.stop()


# ---------------------------------------------------------------------------
# AI summary monitoring
# ---------------------------------------------------------------------------

_state = {}


def _summary_process_running():
    return ai_status.summary_process_running()


def _launch_bulk_summary(extra_args=None):
    """Launch bulk_summary.py headless. Returns True if started."""
    if _summary_process_running():
        return False
    cmd = [PY, os.path.join(ROOT, "bulk_summary.py")] + list(extra_args or [])
    _bulk["proc"] = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_spawn_env(),
    )
    _bulk["fh"] = None
    log(f"started bulk_summary pid={_bulk['proc'].pid} args={list(extra_args or [])}")
    return True


def _start_bulk_summary(icon, item):
    if not _launch_bulk_summary(["--no-today"]):
        _notify(icon, "OpenRecall: bulk summary", "A bulk summary is already running.")


def _start_bulk_summary_shutdown(icon, item):
    if not _launch_bulk_summary(["--no-today", "--shutdown"]):
        _notify(icon, "OpenRecall: bulk summary", "A bulk summary is already running.")


def _start_day(day):
    def _cb(icon, item):
        if not _launch_bulk_summary(["--day", day]):
            _notify(icon, "OpenRecall: bulk summary", "A bulk summary is already running.")
    return _cb


def _stop_bulk_summary(icon, item):
    targets = []
    p = _bulk.get("proc")
    if p is not None and p.poll() is None:
        targets.append(p.pid)
    try:
        import psutil as _psutil
        for proc in _psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = proc.info["cmdline"] or []
            except Exception:
                continue
            if any("bulk_summary.py" in part for part in cmd):
                targets.append(proc.info["pid"])
    except Exception:
        pass
    for pid in set(targets):
        try:
            subprocess.Popen(["kill", "-9", str(pid)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log(f"stopped bulk_summary pid={pid}")
        except Exception as e:
            log(f"stop_bulk_summary: {e}")
    _bulk["proc"] = None
    fh = _bulk.get("fh")
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
        _bulk["fh"] = None
    _notify(icon, "OpenRecall: bulk summary stopped", "Bulk summary shutting down.")


def _read_summary_status():
    return ai_status.read_status()


def _stage_label(st):
    return ai_status.stage_label(st)


def _tray_title(st):
    return ai_status.tray_title(st)


def _notify(icon, title, message):
    try:
        icon.notify(message or "", title)
    except Exception as e:
        log(f"notify error: {e!r}")
    # Also send via notify-send for KDE Plasma (more reliable)
    try:
        subprocess.Popen(["notify-send", title, message or ""],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _monitor_thread(icon):
    """Polls summary status, updates tooltip + menu, fires notifications."""
    prev_running = None
    prev_done = set()
    last_menu_str = None
    first = True
    while True:
        try:
            st = _read_summary_status()
            _state.update(st)

            try:
                title = _tray_title(st)
                if icon.title != title:
                    icon.title = title
            except Exception:
                pass

            # Rebuild menu when text changes (needed for some pystray backends)
            try:
                menu_str = str(icon.menu)
            except Exception:
                menu_str = None
            if menu_str != last_menu_str:
                last_menu_str = menu_str
                try:
                    icon.update_menu()
                except Exception:
                    pass

            # Reap finished bulk-summary process
            bp = _bulk.get("proc")
            if bp is not None and bp.poll() is not None:
                fh = _bulk.get("fh")
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
                _bulk["proc"] = None
                _bulk["fh"] = None

            if first:
                prev_running = st["running"]
                prev_done = set(st["days_done"])
                first = False
            else:
                if st["running"] and not prev_running:
                    _notify(icon, "OpenRecall: AI summary started",
                            f"Bulk summary is running" + (f" for {st['day']}" if st.get("day") else "") + ".")
                elif prev_running and not st["running"]:
                    _notify(icon, "OpenRecall: AI summary stopped", "The bulk summary process is no longer running.")
                for d in st["days_done"]:
                    if d not in prev_done:
                        _notify(icon, "OpenRecall: day summary complete", f"The AI summary for {d} is ready.")
                prev_running = st["running"]
                prev_done = set(st["days_done"])
        except Exception as e:
            log(f"monitor error: {e!r}")
        time.sleep(2)


def _open_summary_log(icon, item):
    if os.path.exists(SUMMARY_LOG_PATH):
        _open_path(SUMMARY_LOG_PATH)
    else:
        _notify(icon, "No summary log yet", "Run bulk_summary first.")


def _open_latest_summary(icon, item):
    try:
        if os.path.isdir(SUMMARIES_DIR_PATH):
            mds = [f for f in os.listdir(SUMMARIES_DIR_PATH)
                   if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f)]
            if mds:
                newest = os.path.join(SUMMARIES_DIR_PATH, sorted(mds)[-1])
                _open_path(newest)
                return
        _open_summaries_folder(icon, item)
    except Exception as e:
        log(f"open_latest_summary: {e}")


def _open_summaries_folder(icon, item):
    if os.path.isdir(SUMMARIES_DIR_PATH):
        _open_path(SUMMARIES_DIR_PATH)
    else:
        _notify(icon, "No summaries yet", "Run bulk_summary first.")


def _disp(text):
    """Read-only menu line (disabled)."""
    return pystray.MenuItem(text, None, enabled=False)


def _summary_items():
    """Dynamic submenu items for AI Summary."""
    st = _state or _read_summary_status()
    items = []

    if st.get("running"):
        items.append(_disp(f"Running: {_stage_label(st)}"))
        if st.get("day"):
            items.append(_disp(f"Day: {st['day']}"))
        if st.get("pending_day"):
            items.append(_disp(f"Pending: {st['pending_day']}"))
        if st.get("stage") == "vision" and st.get("total"):
            pct = int(100 * st["cur"] / st["total"])
            when = f" @ {st['minute']}" if st.get("minute") else ""
            items.append(_disp(f"Sampled moments: {st['cur']}/{st['total']} ({pct}%){when}"))
        elif st.get("stage") == "writer" and st.get("hour_now"):
            items.append(_disp(f"Hour: {st['hour_now']}:00"))
    else:
        items.append(_disp(f"Summary: {_stage_label(st)}"))
        if st.get("pending_day"):
            items.append(_disp(f"Pending: {st['pending_day']}"))

    if st.get("latest_done"):
        items.append(_disp(f"Latest done: {st['latest_done']}"))

    if st.get("scene"):
        sc = st["scene"].replace("\n", " ")
        items.append(_disp("Latest scene: " + sc[:160] + ("..." if len(sc) > 160 else "")))

    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("Open live AI progress", _open_live_ai_progress))
    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("Start bulk summary", _start_bulk_summary,
                                  enabled=not st.get("running")))
    items.append(pystray.MenuItem("Summarize a specific day...", _day_submenu(),
                                  enabled=not st.get("running")))
    items.append(pystray.MenuItem("Start bulk summary (shutdown after)",
                                  _start_bulk_summary_shutdown,
                                  enabled=not st.get("running")))
    items.append(pystray.MenuItem("Stop bulk summary", _stop_bulk_summary,
                                  enabled=bool(st.get("running"))))
    items.append(pystray.MenuItem("Open summary log", _open_summary_log))
    items.append(pystray.MenuItem("Open latest summary", _open_latest_summary))
    items.append(pystray.MenuItem("Open summaries folder", _open_summaries_folder))
    return tuple(items)


def _open_live_ai_progress(icon, item):
    try:
        webbrowser.open(f"http://localhost:{config.WEB_PORT}/ai")
    except Exception as e:
        log(f"open_live_ai_progress: {e}")


def _day_items():
    """Days needing summary for 'specific day' submenu."""
    days = set()
    try:
        if os.path.isdir(SUMMARIES_DIR_PATH):
            for name in os.listdir(SUMMARIES_DIR_PATH):
                m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
                if m:
                    days.add(m.group(1))
    except OSError:
        pass
    pending = [d for d in days
               if not os.path.exists(os.path.join(SUMMARIES_DIR_PATH, f"{d}.md"))]
    items = [_disp("Pick a day to summarize:")]
    if not pending:
        items.append(_disp("No pending days"))
        return tuple(items)
    for day in sorted(pending, reverse=True)[:20]:
        items.append(pystray.MenuItem(day, _start_day(day)))
    return tuple(items)


def _day_submenu():
    return pystray.Menu(_day_items)


def _summary_submenu():
    return pystray.Menu(_summary_items)


def make_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(186, 85, 211, 255))
    d.ellipse((14, 14, 50, 50), fill=(240, 200, 250, 255))
    d.rectangle((26, 22, 38, 42), fill=(120, 40, 140, 255))
    return img


def main():
    try:
        if _tray_already_running():
            log("tray already running - this instance exits")
            return
        atexit.register(shutdown_all)
        ensure_app()
        _launch_bulk_summary(["--no-today"])
        menu = pystray.Menu(
            pystray.MenuItem("Open OpenRecall", open_ui, default=True),
            pystray.MenuItem("Open data folder", open_data_folder),
            pystray.MenuItem("AI Summary", _summary_submenu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit OpenRecall", stop_openrecall),
        )
        icon = pystray.Icon(
            "OpenRecall",
            make_icon(),
            "OpenRecall - click to open http://localhost:8082",
            menu,
        )
        threading.Thread(target=_monitor_thread, args=(icon,), daemon=True).start()
        icon.run()
        log("tray stopped")
    except Exception as e:
        log(f"tray main error: {e!r}")
        raise


if __name__ == "__main__":
    main()