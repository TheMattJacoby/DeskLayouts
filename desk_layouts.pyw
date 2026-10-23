"""
Desk Layouts - remembers where your app windows go for each monitor setup.

  A "profile" belongs to a set of connected monitors. When you plug into the
  same monitors again, that profile becomes active automatically.

  Ctrl+Alt+R         put every remembered window back where it belongs
  Ctrl+Alt+S         remember the focused window's current position
  Tray icon          left-click = restore, right-click = choose windows, profiles, settings

Setup:
  pip install pystray pillow
  Double-click desk_layouts.pyw (the .pyw extension runs it with no console window)

Settings and profiles are saved to %APPDATA%\\DeskLayouts\\settings.json
(hotkeys can be changed there; restart the app afterwards).
"""
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from ctypes import wintypes as W
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

VERSION = "1.2"
APP_NAME = "Desk Layouts"
REPO_URL = "https://github.com/TheMattJacoby/DeskLayouts"
INSTANCE_TITLE = "DeskLayouts-Instance"
MUTEX_NAME = "DeskLayouts_SingleInstance_Mutex"

DEFAULTS = {
    "version": 1,
    "hotkeys": {"restore": "Ctrl+Alt+R", "save": "Ctrl+Alt+S"},
    "auto_restore": False,        # restore automatically when the monitors change
    "auto_restore_delay": 4,      # seconds to wait for displays to settle first
    "profiles": {},
}

SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "DeskLayouts" / "settings.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# Apps whose windows all share one process name, so the title must match too.
TITLE_REQUIRED = {"applicationframehost.exe"}
SKIP_EXES = {"textinputhost.exe", "shellexperiencehost.exe", "searchhost.exe",
             "startmenuexperiencehost.exe", "lockapp.exe", "searchapp.exe"}
FRIENDLY = {"olk": "Outlook", "outlook": "Outlook", "ms-teams": "Teams", "teams": "Teams",
            "chrome": "Chrome", "msedge": "Edge", "code": "VS Code", "explorer": "File Explorer",
            "obsidian": "Obsidian", "spotify": "Spotify", "winword": "Word", "excel": "Excel",
            "powerpnt": "PowerPoint", "onenote": "OneNote", "slack": "Slack", "claude": "Claude",
            "windowsterminal": "Terminal", "applicationframehost": "Windows app"}

# --------------------------------------------------------------- win32 constants
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_CLOSE, WM_QUIT = 0x0312, 0x0010, 0x0012
GW_OWNER, GWL_EXSTYLE, WS_EX_TOOLWINDOW = 4, -20, 0x00000080
GA_ROOT = 2
DWMWA_CLOAKED = 14
MONITOR_DEFAULTTONEAREST = 2
SW_RESTORE, SW_SHOWMAXIMIZED = 9, 3
SWP_NOZORDER, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x4, 0x10, 0x200
EDD_GET_DEVICE_INTERFACE_NAME = 1
DISPLAY_DEVICE_ACTIVE = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183


class RECT(ctypes.Structure):
    _fields_ = [("left", W.LONG), ("top", W.LONG), ("right", W.LONG), ("bottom", W.LONG)]

    def t(self):
        return (self.left, self.top, self.right, self.bottom)


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", W.DWORD), ("szDevice", W.WCHAR * 32)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", W.DWORD), ("DeviceName", W.WCHAR * 32), ("DeviceString", W.WCHAR * 128),
                ("StateFlags", W.DWORD), ("DeviceID", W.WCHAR * 128), ("DeviceKey", W.WCHAR * 128)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", W.UINT), ("flags", W.UINT), ("showCmd", W.UINT),
                ("ptMinPosition", W.POINT), ("ptMaxPosition", W.POINT), ("rcNormalPosition", RECT)]


user32 = kernel32 = dwmapi = None
EnumWindowsProc = MonitorEnumProc = None
_mutex = None


def setup_winapi():
    """Load the Windows DLLs and declare signatures (64-bit handles need these)."""
    global user32, kernel32, dwmapi, EnumWindowsProc, MonitorEnumProc
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    EnumWindowsProc = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    MonitorEnumProc = ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(RECT), W.LPARAM)

    def sig(fn, args, res=W.BOOL):
        fn.argtypes, fn.restype = args, res

    u = user32
    sig(u.EnumWindows, [EnumWindowsProc, W.LPARAM])
    sig(u.IsWindowVisible, [W.HWND])
    sig(u.IsIconic, [W.HWND])
    sig(u.IsZoomed, [W.HWND])
    sig(u.GetWindowTextLengthW, [W.HWND], ctypes.c_int)
    sig(u.GetWindowTextW, [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int)
    sig(u.GetWindowLongPtrW, [W.HWND, ctypes.c_int], ctypes.c_ssize_t)
    sig(u.GetWindow, [W.HWND, W.UINT], W.HWND)
    sig(u.GetAncestor, [W.HWND, W.UINT], W.HWND)
    sig(u.GetForegroundWindow, [], W.HWND)
    sig(u.SetForegroundWindow, [W.HWND])
    sig(u.GetWindowThreadProcessId, [W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD)
    sig(u.GetWindowRect, [W.HWND, ctypes.POINTER(RECT)])
    sig(u.GetWindowPlacement, [W.HWND, ctypes.POINTER(WINDOWPLACEMENT)])
    sig(u.ShowWindow, [W.HWND, ctypes.c_int])
    sig(u.SetWindowPos, [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT])
    sig(u.MonitorFromWindow, [W.HWND, W.DWORD], W.HMONITOR)
    sig(u.MonitorFromPoint, [W.POINT, W.DWORD], W.HMONITOR)
    sig(u.GetMonitorInfoW, [W.HMONITOR, ctypes.POINTER(MONITORINFOEXW)])
    sig(u.EnumDisplayMonitors, [W.HDC, ctypes.POINTER(RECT), MonitorEnumProc, W.LPARAM])
    sig(u.EnumDisplayDevicesW, [W.LPCWSTR, W.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), W.DWORD])
    sig(u.GetCursorPos, [ctypes.POINTER(W.POINT)])
    sig(u.RegisterHotKey, [W.HWND, ctypes.c_int, W.UINT, W.UINT])
    sig(u.UnregisterHotKey, [W.HWND, ctypes.c_int])
    sig(u.GetMessageW, [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT], ctypes.c_int)
    sig(u.PostThreadMessageW, [W.DWORD, W.UINT, W.WPARAM, W.LPARAM])
    sig(u.FindWindowW, [W.LPCWSTR, W.LPCWSTR], W.HWND)
    sig(u.PostMessageW, [W.HWND, W.UINT, W.WPARAM, W.LPARAM])
    k = kernel32
    sig(k.OpenProcess, [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
    sig(k.QueryFullProcessImageNameW, [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)])
    sig(k.CloseHandle, [W.HANDLE])
    sig(k.CreateMutexW, [ctypes.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE)
    sig(k.GetCurrentThreadId, [], W.DWORD)
    sig(dwmapi.DwmGetWindowAttribute, [W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD], ctypes.c_long)


def make_dpi_aware():
    """Work in real pixels so positions are exact on mixed-scaling monitors."""
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --------------------------------------------------------------- settings

def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        s.update({k: v for k, v in data.items() if k != "hotkeys"})
        s["hotkeys"].update(data.get("hotkeys", {}))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_PATH)
    except Exception:
        pass


def startup_command():
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}"'


def startup_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def parse_hotkey(spec):
    mods, key = 0, None
    for part in spec.replace(" ", "").split("+"):
        p = part.lower()
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in ("win", "windows"):
            mods |= MOD_WIN
        else:
            key = part.upper()
    if not key:
        return None
    if len(key) == 1 and key.isalnum():
        return mods, ord(key)
    if key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return mods, 0x6F + int(key[1:])
    return None


# --------------------------------------------------------------- monitors

@dataclass
class Monitor:
    slot: str        # stable id inside a profile, e.g. "GSM5B7F@2560x1440#1"
    key: str         # model + resolution
    rect: tuple      # full monitor rect (l, t, r, b)
    work: tuple      # area excluding the taskbar
    primary: bool
    number: int      # 1 = leftmost


def monitor_hwid(device_name):
    """Hardware id of the monitor attached to e.g. \\\\.\\DISPLAY1 (like 'GSM5B7F')."""
    dd = DISPLAY_DEVICEW()
    dd.cb = ctypes.sizeof(dd)
    i = 0
    while user32.EnumDisplayDevicesW(device_name, i, ctypes.byref(dd), EDD_GET_DEVICE_INTERFACE_NAME):
        if dd.StateFlags & DISPLAY_DEVICE_ACTIVE and dd.DeviceID:
            did = dd.DeviceID
            if did.startswith("\\\\?\\"):
                parts = did.split("#")
                if len(parts) > 1:
                    return parts[1]
            parts = did.split("\\")
            if len(parts) > 1:
                return parts[1]
        i += 1
    return device_name.strip("\\.") or "UNKNOWN"


def get_monitors():
    raw = []

    def cb(hmon, hdc, lprect, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            raw.append((mi.szDevice, mi.rcMonitor.t(), mi.rcWork.t(), bool(mi.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    raw.sort(key=lambda m: (m[1][0], m[1][1]))
    mons, seen = [], {}
    for n, (dev, rc, wk, prim) in enumerate(raw, start=1):
        key = f"{monitor_hwid(dev)}@{rc[2] - rc[0]}x{rc[3] - rc[1]}"
        seen[key] = seen.get(key, 0) + 1
        mons.append(Monitor(f"{key}#{seen[key]}", key, rc, wk, prim, n))
    return mons


def fingerprint(mons):
    return "|".join(sorted(m.key for m in mons))


def monitor_info(hmon):
    mi = MONITORINFOEXW()
    mi.cbSize = ctypes.sizeof(mi)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    return mi


def monitor_of_window(hwnd, mons):
    rc = monitor_info(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)).rcMonitor.t()
    return next((m for m in mons if m.rect == rc), mons[0])


def cursor_work_area():
    pt = W.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return monitor_info(user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)).rcWork.t()


# --------------------------------------------------------------- windows

@dataclass
class Win:
    hwnd: int
    title: str
    exe: str


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def process_exe(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = W.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def is_cloaked(hwnd):
    val = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return val.value != 0


def list_windows():
    """Normal, visible app windows (what you'd see in Alt+Tab)."""
    out, me = [], os.getpid()

    def cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
                return True
            if user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW or is_cloaked(hwnd):
                return True
            title = window_title(hwnd)
            if not title or title == "Program Manager":
                return True
            pid = W.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == me:
                return True
            exe = process_exe(pid.value)
            if not exe or exe in SKIP_EXES:
                return True
            out.append(Win(hwnd, title, exe))
        except Exception:
            pass
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def app_name(exe):
    stem = exe.rsplit(".", 1)[0]
    return FRIENDLY.get(stem, stem[:1].upper() + stem[1:])


def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def assign(entries, wins):
    """Match saved entries to open windows: same app, best title match, each window used once.
    Returns {entry_index: Win}."""
    result, used = {}, set()
    for i, e in enumerate(entries):           # pass 1: exact titles win
        for w in wins:
            if w.hwnd not in used and w.exe == e["exe"] and w.title == e["title"]:
                result[i] = w
                used.add(w.hwnd)
                break
    for i, e in enumerate(entries):           # pass 2: closest title
        if i in result:
            continue
        cands = [w for w in wins if w.hwnd not in used and w.exe == e["exe"]]
        if not cands:
            continue
        best = max(cands, key=lambda w: similarity(w.title, e["title"]))
        if e["exe"] in TITLE_REQUIRED and similarity(best.title, e["title"]) < 0.5:
            continue
        result[i] = best
        used.add(best.hwnd)
    return result


def get_rect(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.t()


def get_placement(hwnd):
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
    return wp


def capture_entry(win, mons):
    """Snapshot a window's position relative to its monitor. None if minimized."""
    hwnd = win.hwnd
    if user32.IsIconic(hwnd):
        return None
    mon = monitor_of_window(hwnd, mons)
    if user32.IsZoomed(hwnd):
        r = get_placement(hwnd).rcNormalPosition
        x, y, w, h, state = 0, 0, r.right - r.left, r.bottom - r.top, "max"
    else:
        l, t, r_, b = get_rect(hwnd)
        x, y, w, h, state = l - mon.rect[0], t - mon.rect[1], r_ - l, b - t, "normal"
    return {"exe": win.exe, "title": win.title, "monitor": mon.slot,
            "x": x, "y": y, "w": w, "h": h, "state": state}


def place_window(hwnd, e, mon):
    ml, mt, mr, mb = mon.rect
    w, h = max(200, min(e["w"], mr - ml)), max(120, min(e["h"], mb - mt))
    if e["state"] == "max":
        if user32.IsZoomed(hwnd) and monitor_info(
                user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)).rcMonitor.t() == mon.rect:
            return True                        # already maximized on the right monitor
        x, y = ml + (mr - ml - w) // 2, mt + (mb - mt - h) // 2
    else:
        x, y = ml + e["x"], mt + e["y"]
        if not user32.IsIconic(hwnd) and not user32.IsZoomed(hwnd) and get_rect(hwnd) == (x, y, x + w, y + h):
            return True                        # already in place
    if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    flags = SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER
    if not user32.SetWindowPos(hwnd, None, x, y, w, h, flags):
        return False                           # usually an app running as administrator
    # Second pass: moving across monitors with different scaling can resize the window.
    user32.SetWindowPos(hwnd, None, x, y, w, h, flags)
    if e["state"] == "max":
        user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
    return True


# --------------------------------------------------------------- hotkeys

def hotkey_thread(app):
    app.hk_tid = kernel32.GetCurrentThreadId()
    names, failed = {}, []
    for hid, name in enumerate(("restore", "save"), start=1):
        spec = app.s["hotkeys"].get(name, "")
        parsed = parse_hotkey(spec)
        if parsed and user32.RegisterHotKey(None, hid, parsed[0] | MOD_NOREPEAT, parsed[1]):
            names[hid] = name
        else:
            failed.append(spec or name)
    if failed:
        app.q.put(("toast", f"Hotkey unavailable (in use by another app?): {', '.join(failed)}", TOAST_WARN_MS))
    msg = W.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam in names:
            app.q.put(("cmd", names[msg.wParam], None))
    for hid in names:
        user32.UnregisterHotKey(None, hid)


# --------------------------------------------------------------- app

# Toasts: same look in all of these tray apps; the accent matches the app's icon.
TOAST_BG, TOAST_FG = "#1e293b", "#f1f5f9"
TOAST_ACCENT = "#2dd4bf"
TOAST_MS, TOAST_WARN_MS = 2500, 4500   # normal notices / warnings

BG, FG, MUTED, ACCENT, ROW = "#1f2430", "#f2f4f8", "#9aa3b5", "#4f8cff", "#272d3b"
FONT = ("Segoe UI", 10)


def make_icon():
    """Tray icon: dark badge with a monitor showing tiled windows in teal.
    Same family as Monitor Mirror's icon (same badge and monitor, blue accent)."""
    from PIL import Image, ImageDraw
    S = 256
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bottom = (51, 65, 85), (30, 41, 59)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((8, 8, S - 8, S - 8), 56, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    frame = (226, 232, 240)
    d.rounded_rectangle((40, 52, 216, 168), 14, outline=frame, width=16)     # monitor
    d.rectangle((119, 168, 137, 190), fill=frame)                            # stand
    d.rounded_rectangle((90, 188, 166, 202), 7, fill=frame)                  # foot
    accent = (45, 212, 191)                                                  # teal
    d.rounded_rectangle((62, 74, 122, 146), 6, fill=accent)
    d.rounded_rectangle((132, 74, 194, 106), 6, fill=accent)
    d.rounded_rectangle((132, 114, 194, 146), 6, fill=accent)
    return img


class App:
    def __init__(self):
        import pystray
        self.pystray = pystray
        self.s = load_settings()
        self.q = queue.Queue()
        self.hk_tid = None
        self.pending_restore = None
        self.toast_win = None
        self.chooser = None

        self.root = tk.Tk()
        self.root.title(INSTANCE_TITLE)
        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.monitors = get_monitors()
        self.fp = fingerprint(self.monitors)

        self.icon = pystray.Icon("DeskLayouts", make_icon(), self.tooltip(), menu=self.build_menu())
        self.icon.run_detached()
        threading.Thread(target=hotkey_thread, args=(self,), daemon=True).start()

        self.root.after(100, self.drain)
        self.root.after(2000, self.poll_displays)
        self.toast(f"{APP_NAME} v{VERSION} running · {self.status()}")

    # ---- state helpers
    def active(self, fp=None):
        fp = fp or self.fp
        for pid, p in self.s["profiles"].items():
            if p.get("fingerprint") == fp:
                return pid, p
        return None, None

    def status(self):
        _, p = self.active()
        n = len(self.monitors)
        return f"Displays: {p['name']}" if p else f"Displays: unrecognized ({n} monitor{'s' * (n != 1)})"

    def tooltip(self):
        _, p = self.active()
        return f"{APP_NAME} v{VERSION} · {p['name'] if p else 'unrecognized displays'}"

    def refresh_tray(self):
        try:
            self.icon.title = self.tooltip()
            self.icon.update_menu()
        except Exception:
            pass

    # ---- tray menu
    def cmd(self, name, arg=None):
        return lambda icon=None, item=None: self.q.put(("cmd", name, arg))

    def build_menu(self):
        """Standard layout shared with Monitor Mirror:
        name + version, status / main actions / app settings / Start with Windows,
        settings file, GitHub / Quit."""
        I, S, hk = self.pystray.MenuItem, self.pystray.Menu.SEPARATOR, self.s["hotkeys"]
        note = lambda text: I(text, lambda: None, enabled=False)
        return self.pystray.Menu(
            note(f"{APP_NAME}  v{VERSION}"),
            note(lambda item: self.status()),
            S,
            I(f"Restore windows\t{hk['restore']}", self.cmd("restore"), default=True),
            I("Choose windows to remember…", self.cmd("chooser")),
            S,
            I("Profiles", self.pystray.Menu(self.profile_items)),
            I("Auto-restore when displays change", self.cmd("toggle_auto"),
              checked=lambda i: self.s["auto_restore"]),
            S,
            I("Start with Windows", self.cmd("toggle_startup"), checked=lambda i: startup_enabled()),
            I("Open settings file", self.cmd("open_settings")),
            I("View on GitHub", self.cmd("github")),
            S,
            I("Quit", self.cmd("quit")),
        )

    def profile_items(self):
        I = self.pystray.MenuItem
        active_id, _ = self.active()
        for pid, p in self.s["profiles"].items():
            n = len(p["windows"])
            label = f"{p['name']}  ({n} window{'s' * (n != 1)})" + ("  ← current" if pid == active_id else "")
            yield I(label, self.pystray.Menu(
                I("Rename…", self.cmd("rename", pid)),
                I("Delete…", self.cmd("delete", pid)),
            ))
        if self.s["profiles"]:
            yield self.pystray.Menu.SEPARATOR
        yield I("New profile for these displays…", self.cmd("new_profile"),
                enabled=lambda i: self.active()[1] is None)

    # ---- event loop
    def drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                try:
                    if item[0] == "toast":
                        self.toast(*item[1:])
                    else:
                        self.handle(item[1], item[2])
                except Exception as ex:
                    self.toast(f"Something went wrong: {ex}", TOAST_WARN_MS)
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def handle(self, name, arg):
        if name == "restore":
            self.restore()
        elif name == "save":
            self.save_focused()
        elif name == "chooser":
            self.open_chooser()
        elif name == "new_profile":
            self.ensure_profile(force_prompt=True)
        elif name == "rename":
            p = self.s["profiles"].get(arg)
            new = p and self.ask_text("Rename profile", "New name:", p["name"])
            if new:
                p["name"] = new
                save_settings(self.s)
        elif name == "delete":
            p = self.s["profiles"].get(arg)
            if p and self.confirm("Delete profile",
                                  f"Delete “{p['name']}” and its {len(p['windows'])} remembered windows?"):
                del self.s["profiles"][arg]
                save_settings(self.s)
        elif name == "toggle_auto":
            self.s["auto_restore"] = not self.s["auto_restore"]
            save_settings(self.s)
            self.toast("Auto-restore " + ("on: windows move when you plug in" if self.s["auto_restore"] else "off"))
        elif name == "toggle_startup":
            try:
                set_startup(not startup_enabled())
                self.toast("Will start with Windows" if startup_enabled() else "Won't start with Windows")
            except OSError as e:
                self.toast(f"Couldn't change the startup setting: {e}", TOAST_WARN_MS)
        elif name == "open_settings":
            save_settings(self.s)
            subprocess.Popen(["notepad.exe", str(SETTINGS_PATH)])
        elif name == "github":
            webbrowser.open(REPO_URL)
        elif name == "quit":
            self.quit()
            return
        self.refresh_tray()

    def poll_displays(self):
        try:
            mons = get_monitors()
            fp = fingerprint(mons)
            self.monitors = mons
            if fp != self.fp:
                self.fp = fp
                self.refresh_tray()
                if self.pending_restore:
                    self.root.after_cancel(self.pending_restore)
                    self.pending_restore = None
                _, prof = self.active()
                if prof and self.s["auto_restore"] and prof["windows"]:
                    self.toast(f"Displays: {prof['name']} · restoring windows…")
                    self.pending_restore = self.root.after(
                        int(self.s["auto_restore_delay"] * 1000), self.restore)
                elif prof:
                    self.toast(f"Displays: {prof['name']} · {self.s['hotkeys']['restore']} to restore windows")
                else:
                    self.toast("New display setup · right-click the tray icon to set up a profile", TOAST_WARN_MS)
        finally:
            self.root.after(2000, self.poll_displays)

    # ---- core actions
    def ensure_profile(self, force_prompt=False):
        self.monitors = get_monitors()
        self.fp = fingerprint(self.monitors)
        pid, prof = self.active()
        if prof:
            if force_prompt:
                self.toast(f"These displays already use “{prof['name']}”")
            return pid, prof
        n = len(self.monitors)
        suggestion = "Laptop only" if n == 1 else f"{n}-monitor desk"
        name = self.ask_text("New display setup",
                             f"These {n} display{'s' * (n != 1)} don't have a profile yet.\n"
                             "Give it a name (e.g. Home Office, Laptop only):", suggestion)
        if not name:
            return None, None
        pid = uuid.uuid4().hex[:8]
        prof = {"name": name, "fingerprint": self.fp, "windows": [],
                "monitors": [m.slot for m in self.monitors]}
        self.s["profiles"][pid] = prof
        save_settings(self.s)
        self.refresh_tray()
        self.toast(f"Profile “{name}” created")
        return pid, prof

    def restore(self):
        self.pending_restore = None
        mons = get_monitors()
        self.monitors, self.fp = mons, fingerprint(mons)
        _, prof = self.active()
        if not prof:
            self.toast("No profile for these displays yet · right-click the tray icon to create one", TOAST_WARN_MS)
            return
        entries = prof["windows"]
        if not entries:
            self.toast(f"“{prof['name']}” has no windows yet · use “Choose windows to remember…”", TOAST_WARN_MS)
            return
        fg = user32.GetForegroundWindow()
        amap = assign(entries, list_windows())
        by_slot = {m.slot: m for m in mons}
        moved = failed = 0
        for i, w in amap.items():
            mon = by_slot.get(entries[i]["monitor"])
            if mon and place_window(w.hwnd, entries[i], mon):
                moved += 1
            else:
                failed += 1
        if fg:
            user32.SetForegroundWindow(fg)
        parts = [f"{prof['name']}: placed {moved} window{'s' * (moved != 1)}"]
        if len(entries) - len(amap):
            parts.append(f"{len(entries) - len(amap)} not open")
        if failed:
            parts.append(f"{failed} couldn't move (running as admin?)")
        self.toast(" · ".join(parts), TOAST_WARN_MS if failed else TOAST_MS)

    def save_focused(self):
        hwnd = user32.GetAncestor(user32.GetForegroundWindow(), GA_ROOT)
        wins = list_windows()
        win = next((w for w in wins if w.hwnd == hwnd), None)
        if not win:
            self.toast("Click into an app window first, then press the hotkey")
            return
        _, prof = self.ensure_profile()
        if not prof:
            return
        entry = capture_entry(win, get_monitors())
        if not entry:
            self.toast("That window is minimized · restore it first")
            return
        amap = assign(prof["windows"], wins)
        idx = next((i for i, w in amap.items() if w.hwnd == hwnd), None)
        if idx is None:
            prof["windows"].append(entry)
        else:
            prof["windows"][idx] = entry
        save_settings(self.s)
        self.refresh_tray()
        mon = monitor_of_window(hwnd, self.monitors)
        self.toast(f"Remembered {app_name(win.exe)} on monitor {mon.number} · {prof['name']}")

    # ---- UI: toast + dialogs
    def toast(self, text, ms=TOAST_MS):
        """Small notice in the bottom-right corner of the screen the mouse is on.
        Same look and behavior in Desk Layouts and Monitor Mirror; click to dismiss."""
        if self.toast_win is not None:
            try:
                self.toast_win.destroy()
            except Exception:
                pass
        t = tk.Toplevel(self.root, bg=TOAST_BG)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        try:
            t.attributes("-alpha", 0.96)
        except Exception:
            pass
        tk.Frame(t, bg=TOAST_ACCENT, width=4).pack(side="left", fill="y")
        tk.Label(t, text=text, bg=TOAST_BG, fg=TOAST_FG, font=("Segoe UI", 10),
                 padx=14, pady=10, justify="left", wraplength=460).pack(side="left")
        for w in (t, *t.winfo_children()):
            w.bind("<Button-1>", lambda e: t.destroy())
        t.update_idletasks()
        l, top, r, b = cursor_work_area()
        t.geometry(f"+{r - t.winfo_reqwidth() - 24}+{b - t.winfo_reqheight() - 24}")
        t.after(ms, t.destroy)
        self.toast_win = t

    def center(self, win):
        win.update_idletasks()
        l, t, r, b = cursor_work_area()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{l + (r - l - w) // 2}+{t + (b - t - h) // 2}")

    def dialog(self, title):
        d = tk.Toplevel(self.root, bg=BG)
        d.title(title)
        d.attributes("-topmost", True)
        d.resizable(False, False)
        return d

    def button(self, parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd, font=FONT, relief="flat", cursor="hand2",
                         bg=ACCENT if primary else ROW, fg="white" if primary else FG,
                         activebackground=ACCENT, activeforeground="white", padx=14, pady=4, bd=0)

    def ask_text(self, title, prompt, initial=""):
        d, result = self.dialog(title), [None]
        tk.Label(d, text=prompt, bg=BG, fg=FG, font=FONT, justify="left").pack(padx=18, pady=(16, 8), anchor="w")
        var = tk.StringVar(value=initial)
        e = tk.Entry(d, textvariable=var, font=FONT, width=36, bg=ROW, fg=FG, insertbackground=FG, relief="flat")
        e.pack(padx=18, ipady=4)
        e.select_range(0, "end")

        def ok(*_):
            result[0] = var.get().strip() or None
            d.destroy()

        row = tk.Frame(d, bg=BG)
        row.pack(fill="x", padx=18, pady=14)
        self.button(row, "OK", ok, True).pack(side="right")
        self.button(row, "Cancel", d.destroy).pack(side="right", padx=8)
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda *_: d.destroy())
        self.center(d)
        d.focus_force()
        e.focus_set()
        d.grab_set()
        self.root.wait_window(d)
        return result[0]

    def confirm(self, title, msg):
        d, result = self.dialog(title), [False]
        tk.Label(d, text=msg, bg=BG, fg=FG, font=FONT, wraplength=380, justify="left").pack(padx=18, pady=(16, 8))

        def yes(*_):
            result[0] = True
            d.destroy()

        row = tk.Frame(d, bg=BG)
        row.pack(fill="x", padx=18, pady=14)
        self.button(row, "Delete", yes, True).pack(side="right")
        self.button(row, "Cancel", d.destroy).pack(side="right", padx=8)
        d.bind("<Escape>", lambda *_: d.destroy())
        self.center(d)
        d.focus_force()
        d.grab_set()
        self.root.wait_window(d)
        return result[0]

    def open_chooser(self):
        if self.chooser is not None and self.chooser.winfo_exists():
            self.chooser.lift()
            self.chooser.focus_force()
            return
        _, prof = self.ensure_profile()
        if not prof:
            return
        mons = get_monitors()
        wins = list_windows()
        entries = prof["windows"]
        amap = assign(entries, wins)
        hwnd_to_idx = {w.hwnd: i for i, w in amap.items()}

        d = self.dialog(f"Remember windows – {prof['name']}")
        d.resizable(False, True)
        self.chooser = d
        tk.Label(d, text=f"Tick the windows to remember for “{prof['name']}”.\n"
                         "Arrange them first; their current spot is saved when you click Save.",
                 bg=BG, fg=FG, font=FONT, justify="left").pack(padx=18, pady=(16, 10), anchor="w")

        outer = tk.Frame(d, bg=BG)
        outer.pack(fill="both", expand=True, padx=18)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, width=640, height=380)
        sb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw", width=620)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        d.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        rows = []

        def add_row(kind, obj, checked, name, title, where):
            var = tk.BooleanVar(value=checked)
            f = tk.Frame(inner, bg=ROW)
            f.pack(fill="x", pady=2)
            tk.Checkbutton(f, variable=var, bg=ROW, activebackground=ROW, selectcolor=BG,
                           fg=FG, bd=0, highlightthickness=0).pack(side="left", padx=(8, 4), pady=6)
            txt = tk.Frame(f, bg=ROW)
            txt.pack(side="left", fill="x", expand=True, pady=4)
            tk.Label(txt, text=name, bg=ROW, fg=FG, font=("Segoe UI Semibold", 10), anchor="w").pack(fill="x")
            tk.Label(txt, text=title[:90], bg=ROW, fg=MUTED, font=("Segoe UI", 9), anchor="w").pack(fill="x")
            tk.Label(f, text=where, bg=ROW, fg=MUTED, font=("Segoe UI", 9)).pack(side="right", padx=10)
            for wdg in (f, txt) + tuple(txt.winfo_children()):
                wdg.bind("<Button-1>", lambda e, v=var: v.set(not v.get()))
            rows.append((kind, obj, var))

        for w in wins:
            if user32.IsIconic(w.hwnd):
                where = "minimized"
            else:
                where = f"monitor {monitor_of_window(w.hwnd, mons).number}"
                if user32.IsZoomed(w.hwnd):
                    where += " · maximized"
            add_row("open", w, w.hwnd in hwnd_to_idx, app_name(w.exe), w.title, where)
        for i, e in enumerate(entries):
            if i not in amap:
                add_row("saved", e, True, app_name(e["exe"]), e["title"], "remembered · not open now")

        def set_all(v):
            for _, _, var in rows:
                var.set(v)

        def save():
            new, skipped = [], 0
            for kind, obj, var in rows:
                if not var.get():
                    continue
                if kind == "saved":
                    new.append(obj)
                    continue
                ent = capture_entry(obj, mons)
                if ent:
                    new.append(ent)
                elif obj.hwnd in hwnd_to_idx:
                    new.append(entries[hwnd_to_idx[obj.hwnd]])   # minimized: keep old position
                else:
                    skipped += 1
            prof["windows"] = new
            save_settings(self.s)
            d.destroy()
            self.refresh_tray()
            msg = f"Saved {len(new)} window{'s' * (len(new) != 1)} to {prof['name']}"
            if skipped:
                msg += f" · skipped {skipped} minimized"
            self.toast(msg)

        hk = self.s["hotkeys"]
        tk.Label(d, text=f"Tip: {hk['save']} remembers just the focused window · {hk['restore']} puts everything back",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(padx=18, pady=(10, 0), anchor="w")
        bar = tk.Frame(d, bg=BG)
        bar.pack(fill="x", padx=18, pady=14)
        self.button(bar, "All", lambda: set_all(True)).pack(side="left")
        self.button(bar, "None", lambda: set_all(False)).pack(side="left", padx=8)
        self.button(bar, "Save", save, True).pack(side="right")
        self.button(bar, "Cancel", d.destroy).pack(side="right", padx=8)
        d.bind("<Escape>", lambda *_: d.destroy())
        self.center(d)
        d.focus_force()

    # ---- lifecycle
    def run(self):
        self.root.mainloop()

    def quit(self):
        try:
            self.icon.stop()
        except Exception:
            pass
        if self.hk_tid:
            user32.PostThreadMessageW(self.hk_tid, WM_QUIT, 0, 0)
        self.root.destroy()


def take_over_previous():
    """If an older copy is running, close it so this one takes over."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return
    hwnd = user32.FindWindowW(None, INSTANCE_TITLE)
    if hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        for _ in range(40):
            time.sleep(0.1)
            if not user32.FindWindowW(None, INSTANCE_TITLE):
                break


def main():
    if os.name != "nt":
        sys.exit("Desk Layouts runs on Windows only.")
    make_dpi_aware()
    setup_winapi()
    take_over_previous()
    App().run()


if __name__ == "__main__":
    main()
