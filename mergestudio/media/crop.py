"""黑边检测：像素亮度为主（抗噪点/台标），cropdetect 兜底。"""
from __future__ import annotations

import re
import subprocess
from collections import Counter

from .. import config as C
from .probe import probe_full

NO_BARS = {"top": 0, "bottom": 0, "left": 0, "right": 0}


def _luma_bars(path, at: float, dark: int = 40, ratio: float = 0.88) -> dict:
    """用一帧的亮度统计黑边：整行/整列绝大多数像素都很暗，就认为是黑边。

    比 cropdetect 更抗噪点、抗台标（台标只占一行里的一小部分像素）。
    """
    info = probe_full(path) or {}
    sw, sh = int(info.get("width") or 0), int(info.get("height") or 0)
    if not sw or not sh:
        return dict(NO_BARS)
    cmd = [C.FFMPEG, "-hide_banner", "-v", "error",
           "-ss", f"{max(0.0, at):.2f}", "-i", str(path),
           "-frames:v", "1", "-vf", "scale=320:-2,format=gray",
           "-f", "rawvideo", "pipe:1"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=60).stdout
    except Exception:
        raw = b""
    w = 320
    if not raw or len(raw) < w * 16:
        return dict(NO_BARS)
    h = len(raw) // w
    frame = raw[:w * h]

    def dark_row(y):
        row = frame[y * w:(y + 1) * w]
        return sum(1 for v in row if v < dark) >= w * ratio

    top = 0
    while top < h and dark_row(top):
        top += 1
    bottom = 0
    while bottom < h - top and dark_row(h - 1 - bottom):
        bottom += 1

    # 左右方向只在「去掉上下黑边后的内容行」里统计，
    # 否则黑边行会把每一列都算成暗列，误报左右裁切
    y0, y1 = top, h - bottom
    rows = max(1, y1 - y0)

    def dark_col(x):
        cnt = 0
        for y in range(y0, y1):
            if frame[y * w + x] < dark:
                cnt += 1
        return cnt >= rows * ratio

    left = 0
    while left < w and dark_col(left):
        left += 1
    right = 0
    while right < w - left and dark_col(w - 1 - right):
        right += 1

    sy, sx = sh / h, sw / w
    return {"top": int(round(top * sy)), "bottom": int(round(bottom * sy)),
            "left": int(round(left * sx)), "right": int(round(right * sx))}


def _cropdetect_bars(path, start: float, span: float, limit: int = 24) -> dict:
    cmd = [C.FFMPEG, "-hide_banner", "-nostats",
           "-ss", f"{max(0.0, start):.2f}", "-i", str(path),
           "-t", f"{max(1.0, span):.2f}",
           "-vf", f"cropdetect={int(limit)}:2:0", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception:
        return dict(NO_BARS)
    info = probe_full(path) or {}
    sw, sh = int(info.get("width") or 0), int(info.get("height") or 0)
    cands = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr or "")
    if not sw or not sh or not cands:
        return dict(NO_BARS)
    # 只统计「确实裁掉了内容」的候选，避免整帧结果把众数带偏
    useful = []
    for cw, ch, cx, cy in cands:
        cw, ch, cx, cy = int(cw), int(ch), int(cx), int(cy)
        if cw < sw - 8 or ch < sh - 8:
            useful.append((cw, ch, cx, cy))
    if not useful:
        return dict(NO_BARS)
    (cw, ch, cx, cy), _ = Counter(useful).most_common(1)[0]
    return {"top": cy, "bottom": max(0, sh - cy - ch),
            "left": cx, "right": max(0, sw - cx - cw)}


def _median(values):
    vals = sorted(values)
    return vals[len(vals) // 2] if vals else 0


def detect_crop(path, start: float = 3.0, sample_sec: float = 12.0,
                limit: int = 24) -> dict:
    """自动检测黑边：三点采样取中位数，避免某一帧黑场把结果带偏。"""
    info = probe_full(path) or {}
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if not width or not height:
        raise RuntimeError("无法读取视频分辨率")
    dur = float(info.get("duration") or 0)

    points = [_luma_bars(path, dur * frac if dur > 1 else 0.0)
              for frac in (0.2, 0.5, 0.8)]
    top = _median([p["top"] for p in points])
    bottom = _median([p["bottom"] for p in points])
    left = _median([p["left"] for p in points])
    right = _median([p["right"] for p in points])
    method = "luma"

    if not any((top, bottom, left, right)):
        ss = min(max(0.0, float(start)), max(0.0, dur * 0.2)) if dur > 0.5 else 0.0
        span = (min(max(1.0, float(sample_sec)), max(1.0, dur - ss - 0.2))
                if dur else sample_sec)
        cd = _cropdetect_bars(path, ss, span, limit)
        if any(cd.values()):
            top, bottom, left, right = (cd["top"], cd["bottom"],
                                        cd["left"], cd["right"])
            method = "cropdetect"

    # 防护：单边最多裁掉 45%，避免整帧偏暗时把画面裁没
    top = min(top, int(height * 0.45))
    bottom = min(bottom, int(height * 0.45))
    left = min(left, int(width * 0.45))
    right = min(right, int(width * 0.45))

    return {"ok": bool(top or bottom or left or right),
            "width": width, "height": height,
            "crop_width": max(16, width - left - right),
            "crop_height": max(16, height - top - bottom),
            "left": left, "top": top, "right": right, "bottom": bottom,
            "method": method}
