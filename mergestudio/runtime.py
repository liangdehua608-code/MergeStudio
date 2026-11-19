"""单实例相关：进程探活、端口探测、运行记录读写。"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from . import config as C


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def probe_instance(port, timeout: float = 0.8) -> bool:
    """探测本机某端口是不是本程序（/api/ping，旧版退回 /api/queue）。"""
    try:
        port = int(port)
    except Exception:
        return False
    if port <= 0:
        return False
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/api/ping", timeout=timeout) as r:
            body = r.read(4096).decode("utf-8", "ignore")
            try:
                return bool(json.loads(body).get("app") == C.APP_ID)
            except Exception:
                return False
    except Exception:
        pass
    try:
        with urllib.request.urlopen(base + "/api/queue", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def read_runtime():
    try:
        data = json.loads(C.RUNTIME_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("app") == C.APP_ID:
            return data
    except Exception:
        pass
    return None


def write_runtime(port, url):
    try:
        C.DATA_DIR.mkdir(parents=True, exist_ok=True)
        C.RUNTIME_FILE.write_text(json.dumps({
            "app": C.APP_ID, "pid": os.getpid(), "port": int(port),
            "url": url, "started_at": time.time(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def clear_runtime():
    try:
        data = read_runtime()
        if data and str(data.get("pid")) == str(os.getpid()):
            C.RUNTIME_FILE.unlink(missing_ok=True)
    except Exception:
        pass
