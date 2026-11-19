"""编码器能力表：预设/质量参数映射、可用性探测、并发上限。

不同硬件编码器对 `-preset`、质量参数的取值完全不同：
- libx264：ultrafast … slow + -crf
- h264_nvenc：p1 … p7 + -cq（-preset veryfast 会直接报错）
- h264_qsv：veryfast … slow + -global_quality
- h264_amf：speed/balanced/quality + -qp_i/-qp_p
这里统一转换，并在真正跑任务前探测一次可用性，避免跑到一半才失败。
"""
from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from . import config as C

PROFILES = {
    "libx264": {
        "codec": "libx264",
        "label": "CPU 软件编码 (libx264)",
        "vendor": "cpu",
        "presets": {"ultrafast": "ultrafast", "veryfast": "veryfast",
                    "fast": "fast", "medium": "medium", "slow": "slow"},
        "quality": lambda q: ["-crf", str(q)],
        "max_parallel": 0,          # 0 = 不限制
    },
    "h264_nvenc": {
        "codec": "h264_nvenc",
        "label": "NVIDIA NVENC 硬件加速",
        "vendor": "nvidia",
        "presets": {"ultrafast": "p1", "veryfast": "p2", "fast": "p3",
                    "medium": "p4", "slow": "p6"},
        "quality": lambda q: ["-rc", "vbr", "-cq", str(q), "-b:v", "0"],
        "max_parallel": 3,          # 消费级驱动有并发 session 限制
    },
    "h264_qsv": {
        "codec": "h264_qsv",
        "label": "Intel QSV 核显加速",
        "vendor": "intel",
        "presets": {"ultrafast": "veryfast", "veryfast": "veryfast",
                    "fast": "fast", "medium": "medium", "slow": "slow"},
        "quality": lambda q: ["-global_quality", str(q)],
        "max_parallel": 2,
    },
    "h264_amf": {
        "codec": "h264_amf",
        "label": "AMD AMF 硬件加速",
        "vendor": "amd",
        "presets": {"ultrafast": "speed", "veryfast": "speed",
                    "fast": "balanced", "medium": "balanced",
                    "slow": "quality"},
        "quality": lambda q: ["-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q)],
        "max_parallel": 2,
    },
}

_LOCK = threading.Lock()
_STATUS = {}          # encoder -> {"ok": bool, "error": str}


def profile(encoder: str) -> dict:
    return PROFILES.get(encoder) or PROFILES["libx264"]


def preset_name(encoder: str, preset: str) -> str:
    presets = profile(encoder)["presets"]
    return presets.get(preset, presets["veryfast"])


def video_args(encoder: str, preset: str, quality: int) -> list:
    """返回 -c:v 之后到输出前的视频参数。"""
    prof = profile(encoder)
    args = ["-c:v", prof["codec"], "-preset", preset_name(encoder, preset)]
    args += prof["quality"](int(quality))
    return args


def max_parallel(encoder: str) -> int:
    return int(profile(encoder).get("max_parallel") or 0)


def _reason(encoder: str, err: str) -> str:
    low = (err or "").lower()
    if "mfx session" in low or "not supported" in low:
        return "未检测到可用的 Intel 核显或驱动"
    if "cannot load" in low or "no capable devices" in low or "nvenc" in low and "not" in low:
        return "未检测到 NVIDIA 显卡或驱动不支持 NVENC"
    if "amf" in low and ("not" in low or "failed" in low):
        return "未检测到 AMD 显卡或驱动不支持 AMF"
    if "undefined constant" in low or "unable to parse" in low:
        return "该编码器不支持当前预设参数"
    return (err or "未知错误").splitlines()[0][:120] if err else "未知错误"


def probe(encoder: str, timeout: float = 40.0) -> dict:
    """跑一段 1 秒的测试编码，判断编码器在这台机器上是否真的可用。"""
    prof = profile(encoder)
    cmd = [C.FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
           "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=1",
           "-c:v", prof["codec"],
           "-preset", preset_name(encoder, "veryfast")]
    cmd += prof["quality"](30)
    cmd += ["-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": str(e), "reason": str(e)}
    if r.returncode == 0:
        return {"ok": True, "error": "", "reason": ""}
    err = (r.stderr or "").strip()
    return {"ok": False, "error": err[:400], "reason": _reason(encoder, err)}


def status(force: bool = False) -> dict:
    """返回所有编码器的可用状态（进程内缓存，force 重新探测）。"""
    with _LOCK:
        if force:
            _STATUS.clear()
        pending = [e for e in PROFILES if e not in _STATUS]
    if pending:
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as ex:
            results = list(ex.map(probe, pending))
        with _LOCK:
            for enc, res in zip(pending, results):
                _STATUS[enc] = res
    out = {}
    with _LOCK:
        for enc, prof in PROFILES.items():
            st = _STATUS.get(enc, {"ok": False, "reason": "未检测"})
            out[enc] = {"ok": bool(st.get("ok")),
                        "label": prof["label"],
                        "vendor": prof["vendor"],
                        "reason": st.get("reason", ""),
                        "max_parallel": prof.get("max_parallel", 0)}
    return out


def ensure(encoder: str) -> dict:
    """任务开始前调用：不可用就直接给出人话原因。"""
    if not C.FFMPEG:
        return {"ok": False, "reason": "尚未设置 ffmpeg 路径"}
    st = status().get(encoder) or {"ok": False, "reason": "未知编码器"}
    return st
