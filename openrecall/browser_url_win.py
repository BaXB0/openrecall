"""
Windows UI Automation browser tab capture (original implementation).
Kept separate for cross-platform compatibility.
"""
import re

_BROWSERS = (
    "chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "chromium",
    "librewolf", "waterfox", "pale moon", "tor browser", "floorp", "zen browser",
)

_ZWSP = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")

_URLISH = re.compile(
    r"^(https?://|ftp://|about:|file://|chrome://|edge://|moz-extension://"
    r"|chrome-extension://|localhost:\d+|[\w.-]+\.\w{2,}([/:?#]|$))",
    re.IGNORECASE,
)

_HOSTISH = re.compile(r"^[\w.-]+\.\w{2,}([/:?#]|$)", re.IGNORECASE)

_BROWSER_SUFFIX = re.compile(
    r"\s*[-–—]\s*(?:google chrome|microsoft edge|personal - microsoft edge"
    r"|mozilla firefox|firefox|librewolf|brave|opera|vivaldi|chromium"
    r"|palemoon|internet explorer)\s*$",
    re.IGNORECASE,
)

_MORE_PAGES = re.compile(r"\s+and\s+\d+\s+more\s+pages\s*$", re.IGNORECASE)

_CHROMIUM_TAB_NOISE = re.compile(
    r"\s*-\s*(?:(?:Sleeping|Discarded)\s*[-–—]\s*)*Memory usage.*$"
    r"|\s*-\s*Sleeping\s*$"
    r"|\s*-\s*Discarded\s*$",
    re.IGNORECASE,
)

_PREFERRED_NAMES = ("urlbar-input", "address and search bar", "address bar", "ribbonaddressbar")

_MAX_DEPTH = 9


def looks_like_browser(app_name):
    if not app_name:
        return False
    low = app_name.lower()
    return any(b in low for b in _BROWSERS)


def _proc_owns_browser(pid, psutil):
    try:
        if not psutil:
            return False
        name = psutil.Process(pid).name().lower()
    except Exception:
        return False
    return any(b in name for b in _BROWSERS)


class _Budget:
    __slots__ = ("limit",)

    def __init__(self, limit):
        self.limit = limit

    def used(self):
        return self.limit <= 0

    def dec(self):
        self.limit -= 1


def _iter_children(elem, depth, budget=None):
    if depth <= 0 or (budget is not None and budget.used()):
        return
    try:
        children = elem.GetChildren()
    except Exception:
        return
    for c in children:
        if budget is not None:
            if budget.used():
                return
            budget.dec()
        yield c
        yield from _iter_children(c, depth - 1, budget)


def _normalize_url(val):
    val = (val or "").strip()
    if not val:
        return None
    if _HOSTISH.match(val):
        return "http://" + val
    return val


def _edit_url(edit):
    try:
        import uiautomation as auto
        if edit.ControlType != auto.ControlType.EditControl:
            return None
    except Exception:
        return None
    try:
        val = (edit.GetValuePattern().Value or "").strip()
    except Exception:
        return None
    if not val or not _URLISH.match(val):
        return None
    return _normalize_url(val)


def _clean_title(s):
    s = (s or "").strip()
    return _CHROMIUM_TAB_NOISE.sub("", _ZWSP.sub("", s)).strip()


def _strip_browser_suffix(title):
    s = _ZWSP.sub("", title or "").strip()
    m = _BROWSER_SUFFIX.search(s)
    if m:
        s = s[:m.start()].strip()
    m = _MORE_PAGES.search(s)
    if m:
        s = s[:m.start()].strip()
    return s


def _control_is_selected(ctrl):
    try:
        pat = ctrl.GetSelectionItemPattern()
        return bool(pat) and bool(pat.IsSelected)
    except Exception:
        return False


def _scan_window(win):
    try:
        import uiautomation as auto
    except Exception:
        return None, None
    title = None
    url_best = None
    url_preferred = None
    for c in _iter_children(win, _MAX_DEPTH):
        try:
            if c.ControlType == auto.ControlType.TabItemControl:
                name = (c.Name or "").strip()
                if name and _control_is_selected(c):
                    title = _clean_title(name) or title
            elif c.ControlType == auto.ControlType.EditControl:
                val = _edit_url(c)
                if val is None:
                    continue
                try:
                    ident = ((c.AutomationId or "") + "|" + (c.Name or "")).lower()
                except Exception:
                    ident = ""
                if any(p in ident for p in _PREFERRED_NAMES):
                    url_preferred = val
                elif url_best is None:
                    url_best = val
        except Exception:
            continue
    return title, url_preferred or url_best


def _window_active_tab(win):
    try:
        import uiautomation as auto
    except Exception:
        return None, None

    title, url = None, None
    for c in _iter_children(win, 5, _Budget(1500)):
        try:
            if c.ControlType == auto.ControlType.TabItemControl:
                name = (c.Name or "").strip()
                if name and _control_is_selected(c):
                    title = _clean_title(name) or title
            elif c.ControlType == auto.ControlType.EditControl:
                val = _edit_url(c)
                if val is None:
                    continue
                try:
                    ident = ((c.AutomationId or "") + "|" + (c.Name or "")).lower()
                except Exception:
                    ident = ""
                if any(p in ident for p in _PREFERRED_NAMES):
                    url = val
                elif url is None:
                    url = val
        except Exception:
            continue

        if title and url:
            break
    if not title or not url:
        t2, u2 = _scan_window(win)
        if not title:
            title = t2
        if not url:
            url = u2
    if not title:
        try:
            title = _clean_title(_strip_browser_suffix(win.Name or "")) or None
        except Exception:
            title = None
    return title, url


def _visible_top_windows():
    try:
        import win32gui
    except Exception:
        return []
    try:
        from openrecall.utils import is_cloaked
    except Exception:
        is_cloaked = None

    out = []

    def _cb(hwnd, results):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if is_cloaked is not None and is_cloaked(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if not title:
                return True
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
                return True
            results.append((hwnd, title, rect))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, out)
    except Exception:
        return []
    return out


def get_browser_tabs(monitors=None):
    """Returns the ACTIVE tabs of every visible browser window on screen (Windows)."""
    try:
        import uiautomation as auto
    except Exception:
        return []
    try:
        import psutil
        import win32process
    except Exception:
        psutil = None
        win32process = None
    try:
        from openrecall.utils import is_on_current_virtual_desktop
    except Exception:
        is_on_current_virtual_desktop = None
    try:
        from openrecall.utils import monitor_for_rect_area
    except Exception:
        monitor_for_rect_area = None

    out, seen = [], set()
    for hwnd, _title, _rect in _visible_top_windows():
        pid = 0
        if win32process is not None:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
        if not _proc_owns_browser(pid, psutil):
            continue
        if is_on_current_virtual_desktop is not None:
            try:
                if not is_on_current_virtual_desktop(hwnd):
                    continue
            except Exception:
                pass
        monitor = None
        if monitor_for_rect_area is not None and _rect:
            try:
                monitor = monitor_for_rect_area(_rect, monitors)
            except Exception:
                monitor = None
        try:
            win = auto.ControlFromHandle(hwnd)
        except Exception:
            continue
        title, url = None, None
        try:
            title, url = _window_active_tab(win)
        except Exception:
            pass
        title = title or None
        if not title and not url:
            continue
        if not title:
            title = _strip_browser_suffix(_title) or ""
        key = (title or "").strip().lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append({"title": title or "", "url": url, "active": True, "monitor": monitor})
    return out


def get_browser_url():
    """Returns the URL of the currently-active browser tab, or None."""
    try:
        for t in get_browser_tabs():
            if t.get("url"):
                return t["url"]
    except Exception:
        return None
    return None