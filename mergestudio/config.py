"""全局配置与 ffmpeg 路径解析。

配置文件默认放在项目根目录的 `.mergestudio/config.json`（已加入 .gitignore），
首次启动会自动尝试 PATH 里的 ffmpeg，也可以在网页「设置」里手动指定。
配置项只有三块：可执行文件路径、同名输出策略、完成通知/托盘开关。
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

APP_NAME = "视频拼接工坊"
APP_VERSION = "6.4.0"
APP_ID = "merge-studio"
DEFAULT_PORT = 8760

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parent
WEB_DIR = PACKAGE_DIR / "web"
DATA_DIR = ROOT_DIR / ".mergestudio"
CONFIG_FILE = DATA_DIR / "config.json"
QUEUE_FILE = DATA_DIR / "queue.json"          # 队列快照（含排队中的任务）
HISTORY_FILE = DATA_DIR / "history.json"      # 旧版只存已完成任务的记录，仅用于迁移
RUNTIME_FILE = DATA_DIR / "runtime.json"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".flv", ".wmv",
              ".webm", ".ts", ".mpg", ".mpeg"}
AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
MP4_EXTS = {".mp4", ".m4v", ".mov"}

FFMPEG = ""
FFPROBE = ""
ON_EXISTS = "rename"       # rename（默认，发现同名就改名）/ overwrite
TK_LOCK = threading.Lock()

# 默认配置：新增字段直接加在这里，load_config 会自动补齐
DEFAULT_CONFIG = {
    "ffmpeg": "",
    "ffprobe": "",
    "on_exists": "rename",
    "notify": {"enabled": True, "tray": True},
}

ENCODERS = {
    "libx264": {"name": "CPU 软件编码 (libx264)", "hw": False, "codec": "libx264"},
    "h264_nvenc": {"name": "NVIDIA NVENC 硬件加速", "hw": True, "codec": "h264_nvenc"},
    "h264_qsv": {"name": "Intel QSV 核显加速", "hw": True, "codec": "h264_qsv"},
    "h264_amf": {"name": "AMD AMF 硬件加速", "hw": True, "codec": "h264_amf"},
}

# ======================== 配置读写 ========================
def _normalize(cfg: dict) -> dict:
    """补齐默认值并做类型收敛，任何脏数据都不该让程序起不来。"""
    out = copy.deepcopy(DEFAULT_CONFIG)
    src = cfg if isinstance(cfg, dict) else {}
    out["ffmpeg"] = str(src.get("ffmpeg", "") or "")
    out["ffprobe"] = str(src.get("ffprobe", "") or "")
    mode = str(src.get("on_exists", "") or "").lower()
    out["on_exists"] = mode if mode in ("rename", "overwrite") else "rename"
    n = src.get("notify")
    if isinstance(n, dict):
        out["notify"] = {"enabled": bool(n.get("enabled", True)),
                         "tray": bool(n.get("tray", True))}
    return out

def load_config() -> dict:
    if CONFIG_FILE.is_file():
        try:
            return _normalize(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return _normalize({})

def save_config(cfg: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(_normalize(cfg), ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"写入配置文件失败：{e}")

def overwrite_allowed(params: dict) -> bool:
    """任务是否允许覆盖同名输出：任务自带参数优先，否则跟随全局设置。"""
    value = (params or {}).get("overwrite")
    if value is None:
        return ON_EXISTS == "overwrite"
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "overwrite")
    return bool(value)

# ======================== ffmpeg 配置 ========================
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
    """把配置写进模块级全局（FFMPEG/FFPROBE/ON_EXISTS），返回 (ffmpeg, ffprobe)。"""
    global FFMPEG, FFPROBE, ON_EXISTS
    cfg = _normalize(cfg)
    FFMPEG = resolve_exe(cfg.get("ffmpeg", ""), "ffmpeg") or (shutil.which("ffmpeg") or "")
    fp_in = cfg.get("ffprobe", "")
    FFPROBE = resolve_exe(fp_in, "ffprobe") if _strip_quotes(fp_in) else derive_ffprobe(FFMPEG)
    ON_EXISTS = cfg["on_exists"]
    return FFMPEG, FFPROBE

def config_status() -> dict:
    ff_ok, ff_msg = verify_bin(FFMPEG)
    fp_ok, fp_msg = verify_bin(FFPROBE)
    return {"ffmpeg": FFMPEG, "ffprobe": FFPROBE,
            "ffmpeg_ok": ff_ok, "ffprobe_ok": fp_ok,
            "ffmpeg_version": ff_msg, "ffprobe_version": fp_msg,
            "ready": ff_ok and fp_ok,
            "on_exists": ON_EXISTS,
            "config_file": str(CONFIG_FILE)}

# ======================== 系统对话框 ========================
def _dialog(kind: str, title: str, patterns=None):
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
            if kind == "folder":
                path = filedialog.askdirectory(title=title)
            else:
                ft = [("所有文件", "*.*")]
                if patterns:
                    ft.insert(0, ("视频文件", " ".join(patterns)))
                elif os.name == "nt":
                    ft.insert(0, ("可执行文件", "*.exe"))
                path = filedialog.askopenfilename(title=title, filetypes=ft)
            root.destroy()
            return {"ok": True, "path": path or ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

def pick_exe_dialog(title: str):
    return _dialog("file", title)

def pick_folder_dialog(title="选择文件夹"):
    return _dialog("folder", title)

def pick_file_dialog(title="选择文件", patterns=None):
    """选择单个文件（用于切割目标 / 音视频合并）。"""
    return _dialog("file", title, patterns)
