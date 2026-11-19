"""完成通知 + 系统托盘（Windows 原生实现，零第三方依赖）。

长任务（几小时合并）跑完总得让人知道，所以这里直接调 Win32：

- 托盘：`Shell_NotifyIconW` + 独立消息循环线程，右键菜单可打开界面 / 暂停队列 / 退出；
- 通知：复用托盘气泡（`NIF_INFO`），比每次弹 PowerShell 快得多，也不占窗口焦点；
- 降级：非 Windows、无桌面会话、被安全软件拦住时，通知改用 PowerShell Toast，
  再失败就静默——**通知永远不能影响任务本身**，所有入口都吞异常并返回状态。

线程模型：托盘线程只做消息循环；`notify()` 可以在任意线程调用（Shell_NotifyIcon
本身是线程安全的），菜单回调也在托盘线程内执行，回调里要避免长耗时操作。
"""
from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import threading
import time

_IS_WIN = os.name == "nt"

_STATE = {
    "enabled": True,
    "want_tray": True,
    "tray": None,
    "url": "",
    "callbacks": {},
    "error": "",
}
_LOCK = threading.Lock()

# ------------------------------------------------------------------ 通用入口
def notify(title: str, body: str, level: str = "info") -> bool:
    """弹一条系统通知（不阻塞调用方）。"""
    if not _STATE["enabled"]:
        return False
    tray = _STATE["tray"]
    if tray is not None and tray.alive:
        try:
            if tray.balloon(title, body, level):
                return True
        except Exception:
            pass
    if _IS_WIN:
        return _async_toast(title, body)
    return False


def update_tip(text: str) -> None:
    """更新托盘悬浮提示（长任务时可以显示当前进度）。"""
    tray = _STATE["tray"]
    if tray is not None and tray.alive:
        try:
            tray.tip(text)
        except Exception:
            pass


def task_finished(task) -> bool:
    """任务结束时的通知：只提醒完成与失败，取消是用户自己点的，不打扰。"""
    state = getattr(task, "state", "")
    if state not in ("done", "error"):
        update_tip("MergeStudio · 队列空闲")
        return False

    name = (getattr(task, "name", "") or "任务").strip()
    elapsed = max(0.0, (getattr(task, "finished_at", 0) or time.time())
                  - (getattr(task, "started_at", 0) or 0))
    if state == "done":
        bits = [_fmt_dur(elapsed)]
        size = getattr(task, "final_size", 0) or 0
        if size:
            bits.append(_fmt_size(size))
        if getattr(task, "duration_delta", 0):
            bits.append(f"时长偏差 {task.duration_delta:+.1f}s")
        title, body, level = "✅ 任务完成", f"{name} · " + " · ".join(bits), "info"
    else:
        reason = (getattr(task, "error", "") or "未知原因").strip().splitlines()
        title = "⚠️ 任务失败"
        body = f"{name} · {reason[0][:80] if reason else '未知原因'}"
        level = "error"

    update_tip("MergeStudio · 队列空闲")
    return notify(title, body, level)


# ------------------------------------------------------------------ 托盘
if _IS_WIN:  # pragma: no cover - 需要 Windows 桌面会话
    from ctypes import wintypes

    WM_CLOSE, WM_DESTROY = 0x0010, 0x0002
    WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
    WM_TRAY = 0x8000 + 1

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
    NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x01, 0x02, 0x03
    MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
    TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x0100, 0x0002
    IDI_APPLICATION = 32512

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class _NOTIFYICONDATAW(ctypes.Structure):
        """Vista+ 的布局；少一个字段 Shell_NotifyIcon 就会返回失败。"""
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", _GUID), ("hBalloonIcon", wintypes.HICON),
        ]

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)

    # 显式声明签名：64 位下漏掉 restype 会把句柄截断成 int32，句柄就废了
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.WORD
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = ctypes.c_void_p
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                    ctypes.c_size_t, wintypes.LPCWSTR]
    _user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       ctypes.c_void_p]
    _user32.TrackPopupMenu.restype = ctypes.c_int
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                           ctypes.POINTER(_NOTIFYICONDATAW)]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    class _Tray:
        def __init__(self, callbacks: dict):
            self.callbacks = dict(callbacks or {})
            self.hwnd = None
            self.alive = False
            self._thread = None
            self._wndproc = None
            self._nid = None
            self._hicon = None
            self._ready = threading.Event()

        # ---- 生命周期 ----
        def start(self) -> bool:
            self._thread = threading.Thread(target=self._run, name="ms-tray",
                                            daemon=True)
            self._thread.start()
            self._ready.wait(timeout=2.0)
            return self.alive

        def stop(self) -> None:
            self.alive = False
            if self.hwnd:
                try:
                    _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                except Exception:
                    pass
                try:
                    _user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
                except Exception:
                    pass
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.5)
            self._thread = None

        # ---- 内部 ----
        def _run(self) -> None:
            try:
                self._create_window()
                self._add_icon()
                self.alive = True
                msg = wintypes.MSG()
                while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    _user32.TranslateMessage(ctypes.byref(msg))
                    _user32.DispatchMessageW(ctypes.byref(msg))
            except Exception as e:
                _STATE["error"] = f"托盘不可用：{e}"
                self.alive = False
            finally:
                self._ready.set()

        def _create_window(self) -> None:
            hinst = _kernel32.GetModuleHandleW(None)
            self._wndproc = _WNDPROC(self._on_message)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = "MergeStudioTrayWnd"
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err not in (0, 1410):          # 1410 = 类已注册，可直接复用
                    raise OSError(f"RegisterClassW 失败（{err}）")
            self.hwnd = _user32.CreateWindowExW(0, "MergeStudioTrayWnd",
                                                "MergeStudio", 0, 0, 0, 0, 0,
                                                None, None, hinst, None)
            if not self.hwnd:
                raise OSError(f"CreateWindowExW 失败（{ctypes.get_last_error()}）")

        def _add_icon(self) -> None:
            self._hicon = _user32.LoadIconW(None, IDI_APPLICATION)
            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._hicon or 0
            nid.szTip = _clip(_STATE["url"] or "MergeStudio", 127)
            if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                raise OSError("Shell_NotifyIcon(NIM_ADD) 失败")
            self._nid = nid

        # ---- 对外 ----
        def balloon(self, title: str, body: str, level: str = "info") -> bool:
            if not self.alive or self._nid is None:
                return False
            nid = self._nid
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = _clip(title, 63)
            nid.szInfo = _clip(body, 255)
            nid.dwInfoFlags = {"info": NIIF_INFO, "warn": NIIF_WARNING,
                               "error": NIIF_ERROR}.get(level, NIIF_INFO)
            ok = bool(_shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            return ok

        def tip(self, text: str) -> None:
            if not self.alive or self._nid is None:
                return
            nid = self._nid
            nid.uFlags = NIF_TIP
            nid.szTip = _clip(text, 127)
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP

        # ---- 消息 ----
        def _on_message(self, hwnd, msg, wparam, lparam):
            try:
                if msg == WM_TRAY:
                    ev = int(lparam) & 0xFFFF
                    if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                        self._fire("open")
                    elif ev == WM_RBUTTONUP:
                        self._popup()
                elif msg == WM_CLOSE:
                    _user32.DestroyWindow(hwnd)
                    return 0
                elif msg == WM_DESTROY:
                    _user32.PostQuitMessage(0)
                    return 0
            except Exception:
                pass
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _popup(self) -> None:
            paused = bool(self._safe("is_paused"))
            hmenu = _user32.CreatePopupMenu()
            _user32.AppendMenuW(hmenu, MF_STRING, 1, "打开 MergeStudio")
            _user32.AppendMenuW(hmenu, MF_STRING, 2, "继续队列" if paused else "暂停队列")
            _user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(hmenu, MF_STRING, 3, "退出")
            pt = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            _user32.SetForegroundWindow(self.hwnd)      # 不设前台，菜单点外面不消失
            cmd = _user32.TrackPopupMenu(
                hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                pt.x, pt.y, 0, self.hwnd, None)
            _user32.PostMessageW(self.hwnd, 0, 0, 0)
            _user32.DestroyMenu(hmenu)
            if cmd == 1:
                self._fire("open")
            elif cmd == 2:
                self._fire("pause")
            elif cmd == 3:
                self._fire("quit")

        def _safe(self, name):
            fn = self.callbacks.get(name)
            try:
                return fn() if fn else None
            except Exception:
                return None

        def _fire(self, name: str) -> None:
            fn = self.callbacks.get(name)
            if not fn:
                return
            try:
                # 回调里可能要停服务，别在消息线程里同步做
                threading.Thread(target=fn, daemon=True).start()
            except Exception:
                pass


def _clip(text: str, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit]


def _async_toast(title: str, body: str) -> bool:
    """降级通道：PowerShell + WinRT Toast（约 0.5s 起，所以丢到后台线程）。"""
    try:
        threading.Thread(target=_toast_worker, args=(title, body),
                         daemon=True).start()
        return True
    except Exception:
        return False


def _toast_worker(title: str, body: str) -> None:
    def q(s):
        return "'" + str(s or "").replace("'", "''") + "'"

    script = (
        "$null = [Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType=WindowsRuntime]\n"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]"
        "::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]"
        "::ToastText02)\n"
        "$x = $t.GetElementsByTagName('text')\n"
        f"$x.Item(0).AppendChild($t.CreateTextNode({q(title)})) > $null\n"
        f"$x.Item(1).AppendChild($t.CreateTextNode({q(body)})) > $null\n"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($t)\n"
        "[Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier('MergeStudio').Show($n)\n"
    )
    try:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-EncodedCommand", encoded],
                       capture_output=True, timeout=15)
    except Exception:
        pass


# ------------------------------------------------------------------ 配置
def setup(url: str, callbacks: dict | None = None) -> dict:
    """由入口调用：登记界面地址与菜单回调，并按当前开关起停托盘。"""
    _STATE["url"] = url or ""
    _STATE["callbacks"] = dict(callbacks or {})
    return refresh()


def configure(cfg: dict) -> dict:
    cfg = cfg if isinstance(cfg, dict) else {}
    _STATE["enabled"] = bool(cfg.get("enabled", True))
    _STATE["want_tray"] = bool(cfg.get("tray", True))
    return refresh()


def refresh() -> dict:
    """让托盘状态与开关一致（幂等，可以随时调用）。"""
    with _LOCK:
        if not _IS_WIN:
            return status()
        want = _STATE["enabled"] and _STATE["want_tray"] and bool(_STATE["url"])
        tray = _STATE["tray"]
        if want and (tray is None or not tray.alive):
            candidate = _Tray(_STATE["callbacks"])
            if candidate.start():
                _STATE["tray"] = candidate
                _STATE["error"] = ""
            else:
                _STATE["tray"] = None
        elif not want and tray is not None:
            tray.stop()
            _STATE["tray"] = None
        return status()


def status() -> dict:
    tray = _STATE["tray"]
    return {"platform": _IS_WIN,
            "enabled": bool(_STATE["enabled"]),
            "tray_wanted": bool(_STATE["want_tray"]),
            "tray": bool(tray is not None and tray.alive),
            "error": _STATE["error"]}


def stop() -> None:
    tray = _STATE["tray"]
    if tray is not None:
        tray.stop()
    _STATE["tray"] = None


# ------------------------------------------------------------------ 小工具
def _fmt_dur(sec: float) -> str:
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
