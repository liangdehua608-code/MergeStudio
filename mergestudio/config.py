"""全局配置与 ffmpeg 路径解析。

配置文件默认放在项目根目录的 `.mergestudio/config.json`（已加入 .gitignore），
首次启动会自动尝试 PATH 里的 ffmpeg，也可以在网页「设置」里手动指定。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

APP_NAME = "视频拼接工坊"
APP_VERSION = "6.0.0"
APP_ID = "merge-studio"
DEFAULT_PORT = 8760

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent
WEB_DIR = PACKAGE_DIR / "web"
DATA_DIR = ROOT_DIR / ".mergestudio"
CONFIG_FILE = DATA_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.json"
RUNTIME_FILE = DATA_DIR / "runtime.json"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".flv", ".wmv",
              ".webm", ".ts", ".mpg", ".mpeg"}

FFMPEG = ""
FFPROBE = ""
TK_LOCK = threading.Lock()

ENCODERS = {
    "libx264": {"name": "CPU 软件编码 (libx264)", "hw": False, "codec": "libx264"},
    "h264_nvenc": {"name": "NVIDIA NVENC 硬件加速", "hw": True, "codec": "h264_nvenc"},
    "h264_qsv": {"name": "Intel QSV 核显加速", "hw": True, "codec": "h264_qsv"},
    "h264_amf": {"name": "AMD AMF 硬件加速", "hw": True, "codec": "h264_amf"},
}

# ======================== ffmpeg 配置 ========================
def load_config() -> dict:
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"ffmpeg": str(data.get("ffmpeg", "") or ""),
                        "ffprobe": str(data.get("ffprobe", "") or "")}
        except Exception:
            pass
    return {"ffmpeg": "", "ffprobe": ""}

def save_config(cfg: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"写入配置文件失败：{e}")

def _strip_quotes(s: str) -> str:
    return (s or "").strip().strip('"').strip("'").strip()

def resolve_exe(input_path: str, exe_name: str) -> str:
    raw = _strip_quotes(input_path)
    if not raw:
        return ""
    has_sep = ("/" in raw) or ("\\" in raw)
    looks_like_drive = len(raw) > 1 and raw[1] == ":"
    if not has_sep and not looks_like_drive:
        return shutil.which(raw) or ""
    p = Path(raw).expanduser()
    if p.is_file():
        return str(p)
    if p.is_dir():
        exe = exe_name + (".exe" if os.name == "nt" else "")
        for cand in (p / exe, p / "bin" / exe):
            if cand.is_file():
                return str(cand)
    return ""

def derive_ffprobe(ffmpeg_path: str) -> str:
    if not ffmpeg_path:
        return ""
    p = Path(ffmpeg_path)
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    cand = p.parent / name
    return str(cand) if cand.is_file() else ""

def verify_bin(path: str):
    if not path:
        return False, "未设置或无法解析"
    if not Path(path).is_file() and not shutil.which(path):
        return False, f"文件不存在：{path}"
    try:
        r = subprocess.run([path, "-version"], capture_output=True,
                           text=True, timeout=10, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or f"返回码 {r.returncode}")
            return False, tail.splitlines()[0][:200] if tail else "执行失败"
        first = (r.stdout or "").splitlines()
        return True, (first[0] if first else "OK")
    except FileNotFoundError:
        return False, "文件不存在或无法执行"
    except Exception as e:
        return False, str(e)

def apply_config(cfg: dict):
    global FFMPEG, FFPROBE
    FFMPEG = resolve_exe(cfg.get("ffmpeg", ""), "ffmpeg") or (shutil.which("ffmpeg") or "")
    fp_in = cfg.get("ffprobe", "")
    FFPROBE = resolve_exe(fp_in, "ffprobe") if _strip_quotes(fp_in) else derive_ffprobe(FFMPEG)
    return FFMPEG, FFPROBE

def config_status() -> dict:
    ff_ok, ff_msg = verify_bin(FFMPEG)
    fp_ok, fp_msg = verify_bin(FFPROBE)
    return {"ffmpeg": FFMPEG, "ffprobe": FFPROBE,
            "ffmpeg_ok": ff_ok, "ffprobe_ok": fp_ok,
            "ffmpeg_version": ff_msg, "ffprobe_version": fp_msg,
            "ready": ff_ok and fp_ok,
            "config_file": str(CONFIG_FILE),
            "encoders": ENCODERS}

def pick_exe_dialog(title: str):
    with TK_LOCK:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception as e:
            return {"ok": False, "error": f"tkinter 不可用：{e}"}
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.update()
            ft = [("可执行文件", "*.exe"), ("所有文件", "*.*")] if os.name == "nt" else [("所有文件", "*")]
            path = filedialog.askopenfilename(title=title, filetypes=ft)
            root.destroy()
            return {"ok": True, "path": path or ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

def pick_folder_dialog(title="选择文件夹"):
    with TK_LOCK:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception as e:
            return {"ok": False, "error": f"tkinter 不可用：{e}"}
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            root.update()
            path = filedialog.askdirectory(title=title)
            root.destroy()
            return {"ok": True, "path": path or ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}
