"""媒体处理底层：ffprobe 探测、扫描分组、ffmpeg 执行、路径与格式化工具。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as C

# ======================== 输出路径 ========================
def compute_output_path(src_folder: str, out_arg: str) -> Path:
    src_root = Path(src_folder).expanduser()
    name = src_root.name or "merged"
    if out_arg:
        cand = Path(out_arg).expanduser()
        if cand.suffix.lower() in C.VIDEO_EXTS:
            return cand
        return cand / f"{name}_merged.mp4"
    return src_root / f"{name}_merged.mp4"

# ======================== 通用工具 ========================
def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", p.name)]

def fmt_size(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"

def run_ffmpeg(cmd, total_sec=0.0, on_progress=None, should_cancel=None):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    err_buf = []
    def _read_err():
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    err_buf.append(line)
                    if len(err_buf) > 400:
                        del err_buf[:200]
        except Exception:
            pass
    threading.Thread(target=_read_err, daemon=True).start()

    last_emit = 0.0
    canceled = False
    try:
        for line in proc.stdout:
            if should_cancel and should_cancel():
                canceled = True
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, val = line.partition("=")
            sec = None
            if key in ("out_time_us", "out_time_ms"):
                try:
                    sec = int(val) / 1_000_000.0
                except ValueError:
                    sec = None
            elif key == "out_time":
                m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", val)
                if m:
                    sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            elif key == "progress" and val == "end":
                if on_progress:
                    on_progress(1.0)
                continue
            if sec is not None and sec >= 0 and total_sec > 0 and on_progress:
                now = time.time()
                if now - last_emit >= 0.15:
                    on_progress(min(sec / total_sec, 0.999))
                    last_emit = now
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
    return proc.returncode, "\n".join(err_buf[-60:]), canceled

def probe_full(path: Path):
    """v3: 增强 probe，检测 Edit List，优先用视频流 duration"""
    try:
        r = subprocess.run([C.FFPROBE, "-v", "error", "-print_format", "json",
                            "-show_streams", "-show_format",
                            "-show_entries", "stream_side_data=edit_list",
                            str(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        if r.returncode != 0:
            return None
        info = json.loads(r.stdout)
        v = next((s for s in info.get("streams", [])
                  if s.get("codec_type") == "video"), None)
        a = next((s for s in info.get("streams", [])
                  if s.get("codec_type") == "audio"), None)
        if not v:
            return None

        # v3: 检测 Edit List
        has_edit_list = False
        # 方法1: 检查 side_data
        for s in info.get("streams", []):
            for sd in s.get("side_data_list", []) or []:
                if sd.get("side_data_type") == "Display Matrix":
                    has_edit_list = True
        # 方法2: 检查 format tags 里有没有 elst 相关标记
        fmt_tags = info.get("format", {}).get("tags", {}) or {}
        if any("edit" in k.lower() for k in fmt_tags):
            has_edit_list = True

        # v3: 优先使用视频流的 duration，而非 format duration
        # format duration 包含 Edit List 偏移，视频流 duration 是实际媒体时长
        video_duration = None
        try:
            video_duration = float(v.get("duration") or 0)
        except (ValueError, TypeError):
            video_duration = None
        format_duration = None
        try:
            format_duration = float(info.get("format", {}).get("duration") or 0)
        except (ValueError, TypeError):
            format_duration = None

        # 选择更可靠的时长：优先视频流时长，其次 format 时长
        if video_duration and video_duration > 0:
            duration = video_duration
        elif format_duration and format_duration > 0:
            duration = format_duration
        else:
            duration = 0.0

        # 如果 format duration 和 video duration 差距大，标记有 Edit List
        if format_duration and video_duration and abs(format_duration - video_duration) > 0.05:
            has_edit_list = True

        # v4: 记录音频流时长与帧数，供合并前预检使用
        audio_duration = None
        if a:
            try:
                audio_duration = float(a.get("duration") or 0) or None
            except (ValueError, TypeError):
                audio_duration = None
        try:
            nb_frames = int(v.get("nb_frames") or 0)
        except (ValueError, TypeError):
            nb_frames = 0

        return {
            "vcodec": v.get("codec_name"),
            "width": v.get("width") or 0,
            "height": v.get("height") or 0,
            "pix_fmt": v.get("pix_fmt"),
            "fps": v.get("r_frame_rate") or "30/1",
            "time_base": v.get("time_base") or "1/12800",
            "acodec": a.get("codec_name") if a else None,
            "sample_rate": a.get("sample_rate") if a else None,
            "channels": a.get("channels") if a else None,
            "audio_time_base": a.get("time_base") if a else None,
            "duration": duration,
            "video_duration": video_duration,
            "format_duration": format_duration,
            "audio_duration": audio_duration,
            "nb_frames": nb_frames,
            "has_edit_list": has_edit_list,
        }
    except Exception:
        return None

def get_stream_duration(path: Path, stream: str = "v:0"):
    """读取指定流的时长（v:0 / a:0）"""
    if not C.FFPROBE:
        return None
    try:
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            val = r.stdout.strip().split("\n")[0].strip()
            if val and val != "N/A":
                return float(val)
    except Exception:
        pass
    return None


def get_media_duration(path: Path):
    """v3: 用视频流 duration 而非 format duration，避免 Edit List 干扰"""
    if not C.FFPROBE:
        return None
    try:
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    # 回退到 format duration
    try:
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return float(r.stdout.strip())
    except Exception:
        pass
    return None

def signature(item: dict):
    """
    v3.2: 移除了 fps —— 实测证明帧率不同(24fps vs 23.976fps)只要 time_base 一致，
    -c copy 拼接完全正确（误差 0.1s 级），无需转码。
    之前的版本把 fps 计入签名，导致 23.976fps 文件被误判为"少数派"转码，
    转码输出 time_base=1/12288 与原始的 1/16000 不一致，
    concat 时按首段 time_base 解释后续包，时长被拉伸 16000/12288=1.302 倍，
    造成 4 小时以上的时长虚高。
    """
    return (
        item.get("vcodec"), item.get("width"), item.get("height"),
        item.get("pix_fmt"), item.get("time_base"),
        bool(item.get("acodec")),
        item.get("sample_rate") if item.get("acodec") else None,
        item.get("channels") if item.get("acodec") else None,
    )

def sig_label(item: dict) -> str:
    w, h = item.get("width") or 0, item.get("height") or 0
    codec = (item.get("vcodec") or "?").upper()
    el = " [EL]" if item.get("has_edit_list") else ""
    return f"{w}x{h} {codec}{el}"

# ======================== v4: 预检 / 裁剪 / 章节 ========================
def _ts_of(item: dict) -> str:
    tb = item.get("time_base") or ""
    return tb.split("/")[-1] if "/" in tb else ""


def analyze_items(items):
    """
    v4 合并前预检：给每个文件打健康度标签
    返回 (summary, majority_ts, majority_sr)
      health: ok / warn / bad
      issues: 人类可读的问题列表（前端 hover 显示）
    """
    ts_dist, sr_dist = {}, {}
    for it in items:
        if not it.get("ok"):
            continue
        ts = _ts_of(it)
        if ts.isdigit():
            ts_dist[ts] = ts_dist.get(ts, 0) + 1
        sr = it.get("sample_rate")
        if sr:
            sr_dist[sr] = sr_dist.get(sr, 0) + 1

    maj_ts = max(ts_dist.items(), key=lambda x: x[1])[0] if ts_dist else None
    maj_sr = max(sr_dist.items(), key=lambda x: x[1])[0] if sr_dist else None

    for it in items:
        issues = []
        level = "ok"

        if not it.get("ok"):
            it["health"] = "bad"
            it["issues"] = ["无法读取该文件（损坏或编码不支持）"]
            continue

        dur = float(it.get("duration") or 0)
        if dur <= 0.5:
            issues.append("时长过短（< 0.5s）")
            level = "bad"

        vd = float(it.get("video_duration") or dur or 0)
        ad = it.get("audio_duration")
        if ad and vd and abs(vd - float(ad)) > 0.5:
            issues.append(f"音视频时长差 {vd - float(ad):+.1f}s")
            if level == "ok":
                level = "warn"

        ts = _ts_of(it)
        if maj_ts and ts and ts != maj_ts:
            issues.append(f"时间基 1/{ts} 与多数派 1/{maj_ts} 不同")
            if level == "ok":
                level = "warn"

        sr = it.get("sample_rate")
        if maj_sr and sr and sr != maj_sr:
            issues.append(f"采样率 {sr}Hz 与多数派 {maj_sr}Hz 不同")
            if level == "ok":
                level = "warn"

        if not it.get("acodec"):
            issues.append("没有音频轨")
            if level == "ok":
                level = "warn"

        if it.get("has_edit_list"):
            issues.append("含 Edit List（拼接前会自动清除）")

        it["health"] = level
        it["issues"] = issues

    summary = {
        "ok": sum(1 for i in items if i.get("health") == "ok"),
        "warn": sum(1 for i in items if i.get("health") == "warn"),
        "bad": sum(1 for i in items if i.get("health") == "bad"),
    }
    return summary, maj_ts, maj_sr


def parse_trim_seconds(v) -> float:
    """解析裁剪秒数，限制 0~3600"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return max(0.0, min(f, 3600.0))


def trim_span(dur, head: float, tail: float) -> float:
    """裁剪后的有效时长：保证至少留 0.2s 内容，避免产出空文件"""
    dur = float(dur or 0)
    if dur <= 0:
        return 0.0
    start = head if head < dur - 0.2 else 0.0
    remain = dur - start
    cut_tail = min(tail, max(0.0, remain - 0.2))
    return max(0.0, remain - cut_tail)


def _sanitize_chapter_title(t: str) -> str:
    """ffmetadata 里 = ; # \\ 都是特殊字符，替换掉避免解析失败"""
    s = str(t or "").replace("\r", " ").replace("\n", " ")
    for ch in ("=", ";", "#", "\\"):
        s = s.replace(ch, "-" if ch == "=" else " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:90] or "章节"


def build_chapter_meta(chapters, path: Path):
    """chapters: [(title, start_sec, end_sec)] → 写入 ffmetadata 文件"""
    lines = [";FFMETADATA1"]
    for title, st, en in chapters:
        if en - st < 0.05:
            continue
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={int(round(st * 1000))}")
        lines.append(f"END={int(round(en * 1000))}")
        lines.append("title=" + _sanitize_chapter_title(title))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_chapters(titles, eff_durations):
    """按裁剪后的有效时长累加，生成章节区间"""
    out, cur = [], 0.0
    for title, d in zip(titles, eff_durations):
        d = float(d or 0)
        if d <= 0.05:
            continue
        out.append((title, cur, cur + d))
        cur += d
    return out


def probe_output_summary(path: Path):
    """v4 合并后自检：一次性读取输出的关键指标"""
    if not C.FFPROBE:
        return {}
    try:
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", "-show_chapters", str(path)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return {}
        info = json.loads(r.stdout)
        v = next((s for s in info.get("streams", [])
                  if s.get("codec_type") == "video"), None)
        a = next((s for s in info.get("streams", [])
                  if s.get("codec_type") == "audio"), None)

        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None

        vd = _f(v.get("duration")) if v else None
        ad = _f(a.get("duration")) if a else None
        fmt_d = _f(info.get("format", {}).get("duration"))
        frames = 0
        if v:
            try:
                frames = int(v.get("nb_frames") or 0)
            except (TypeError, ValueError):
                frames = 0
        return {
            "video_duration": vd,
            "audio_duration": ad,
            "format_duration": fmt_d,
            "duration": vd or fmt_d or 0.0,
            "frames": frames,
            "time_base": (v or {}).get("time_base"),
            "sample_rate": (a or {}).get("sample_rate"),
            "size": int(info.get("format", {}).get("size") or 0),
            "chapters": len(info.get("chapters", []) or []),
        }
    except Exception:
        return {}


def list_video_files(folder: str, recursive: bool):
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise ValueError(f"文件夹不存在：{root}")
    it = root.rglob("*") if recursive else root.iterdir()
    files = [p for p in it if p.is_file() and p.suffix.lower() in C.VIDEO_EXTS]
    files.sort(key=natural_key)
    return root, files

def scan_folder(folder: str, recursive: bool, exclude_path=None) -> dict:
    if not C.FFPROBE:
        raise RuntimeError("尚未设置 ffprobe 路径")
    root, files = list_video_files(folder, recursive)
    if exclude_path:
        try:
            ex = Path(exclude_path).resolve()
            files = [f for f in files if f.resolve() != ex]
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = list(ex.map(probe_full, files))
    items = []
    for p, info in zip(files, infos):
        item = {"name": p.name, "path": str(p),
                "size": p.stat().st_size if p.exists() else 0}
        if info and info.get("duration", 0) > 0:
            item.update(info)
            item["ok"] = True
        else:
            item.update({"ok": False, "duration": 0.0, "error": "无法读取时长"})
        items.append(item)

    groups = {}
    for item in items:
        if item.get("ok"):
            groups.setdefault(signature(item), []).append(item)

    majority_sig = None
    majority_size = 0
    for sig, grp in groups.items():
        if len(grp) > majority_size:
            majority_sig, majority_size = sig, len(grp)

    for item in items:
        if not item.get("ok"):
            item["is_majority"] = False
            item["needs_encode"] = False
        else:
            is_maj = signature(item) == majority_sig
            item["is_majority"] = is_maj
            item["needs_encode"] = not is_maj

    need_encode_count = sum(1 for it in items if it.get("needs_encode"))
    edit_list_count = sum(1 for it in items if it.get("has_edit_list"))

    # v4: 合并前预检（红黄绿灯）
    health_summary, majority_ts, majority_sr = analyze_items(items)

    return {
        "folder": str(root),
        "files": items,
        "total_size": sum(i["size"] for i in items),
        "total_duration": sum(i.get("duration") or 0 for i in items),
        "all_ok": all(i.get("ok") for i in items),
        "signature_same": len(groups) <= 1,
        "majority_count": majority_size,
        "need_encode_count": need_encode_count,
        "edit_list_count": edit_list_count,
        "health_summary": health_summary,
        "majority_ts": majority_ts,
        "majority_sr": majority_sr,
        "signature_groups": [
            {"count": len(grp), "is_majority": sig == majority_sig,
             "label": sig_label(grp[0])}
            for sig, grp in sorted(groups.items(), key=lambda x: -len(x[1]))
        ],
    }

def reveal_path(p: Path):
    """在资源管理器里定位文件（或打开目录）。

    Windows 上必须用 `explorer /select,"路径"` 这种「整串命令」形式：
    传参数列表时 Python 会把 `/select,C:/a b/c.mp4` 整体加引号，
    资源管理器解析失败就会退回到默认目录——表现就是「点了没反应」。
    """
    if os.name == "nt":
        target = str(p)
        try:
            if p.is_file():
                subprocess.Popen(f'explorer /select,"{target}"')
            else:
                subprocess.Popen(f'explorer "{target}"')
            return
        except Exception:
            try:
                os.startfile(str(p.parent if p.is_file() else p))
                return
            except Exception:
                pass
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)])


def _luma_bars(path: Path, at: float, dark: int = 40,
               ratio: float = 0.88) -> dict:
    """用一帧的亮度统计黑边：整行/整列绝大多数像素都很暗，就认为是黑边。

    比 cropdetect 更抗噪点、抗台标（台标只占一行里的一小部分像素）。
    """
    info = probe_full(path) or {}
    sw, sh = int(info.get("width") or 0), int(info.get("height") or 0)
    if not sw or not sh:
        return {"top": 0, "bottom": 0, "left": 0, "right": 0}
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
        return {"top": 0, "bottom": 0, "left": 0, "right": 0}
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

    # 回推到源分辨率
    sy, sx = sh / h, sw / w
    return {"top": int(round(top * sy)), "bottom": int(round(bottom * sy)),
            "left": int(round(left * sx)), "right": int(round(right * sx))}


def _cropdetect_bars(path: Path, start: float, span: float,
                     limit: int = 24) -> dict:
    cmd = [C.FFMPEG, "-hide_banner", "-nostats",
           "-ss", f"{max(0.0, start):.2f}", "-i", str(path),
           "-t", f"{max(1.0, span):.2f}",
           "-vf", f"cropdetect={int(limit)}:2:0", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception:
        return {"top": 0, "bottom": 0, "left": 0, "right": 0}
    info = probe_full(path) or {}
    sw, sh = int(info.get("width") or 0), int(info.get("height") or 0)
    cands = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr or "")
    if not sw or not sh or not cands:
        return {"top": 0, "bottom": 0, "left": 0, "right": 0}
    # 只统计「确实裁掉了内容」的候选，避免整帧结果把众数带偏
    from collections import Counter
    useful = []
    for cw, ch, cx, cy in cands:
        cw, ch, cx, cy = int(cw), int(ch), int(cx), int(cy)
        if cw < sw - 8 or ch < sh - 8:
            useful.append((cw, ch, cx, cy))
    if not useful:
        return {"top": 0, "bottom": 0, "left": 0, "right": 0}
    (cw, ch, cx, cy), _ = Counter(useful).most_common(1)[0]
    return {"top": cy, "bottom": max(0, sh - cy - ch),
            "left": cx, "right": max(0, sw - cx - cw)}


def _median(values):
    vals = sorted(values)
    return vals[len(vals) // 2] if vals else 0


def detect_crop(path: Path, start: float = 3.0, sample_sec: float = 12.0,
                limit: int = 24) -> dict:
    """自动检测黑边：像素亮度为主（抗噪点/台标），cropdetect 兜底。"""
    info = probe_full(path) or {}
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if not width or not height:
        raise RuntimeError("无法读取视频分辨率")
    dur = float(info.get("duration") or 0)

    # 取三个时间点，逐边取中位数，避免某一帧黑场/字幕把结果带偏
    points = []
    for frac in (0.2, 0.5, 0.8):
        at = dur * frac if dur > 1 else 0.0
        points.append(_luma_bars(path, at))
    top = _median([p["top"] for p in points])
    bottom = _median([p["bottom"] for p in points])
    left = _median([p["left"] for p in points])
    right = _median([p["right"] for p in points])
    method = "luma"

    if not any((top, bottom, left, right)):
        ss = min(max(0.0, float(start)), max(0.0, dur * 0.2)) if dur > 0.5 else 0.0
        span = min(max(1.0, float(sample_sec)), max(1.0, dur - ss - 0.2)) if dur else sample_sec
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

    cw = max(16, width - left - right)
    ch = max(16, height - top - bottom)
    return {"ok": bool(top or bottom or left or right),
            "width": width, "height": height,
            "crop_width": cw, "crop_height": ch,
            "left": left, "top": top, "right": right, "bottom": bottom,
            "method": method}


def probe_chapters(path: Path) -> list:
    """读取视频内嵌章节（mp4 的章节会被识别为章节轨，不是字幕）。"""
    try:
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-print_format", "json",
             "-show_chapters", str(path)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        data = json.loads(r.stdout or "{}")
    except Exception:
        return []
    out = []
    for ch in data.get("chapters", []) or []:
        try:
            start = float(ch.get("start_time") or 0)
            end = float(ch.get("end_time") or 0)
        except Exception:
            continue
        title = ((ch.get("tags") or {}).get("title") or "").strip()
        if end > start:
            out.append({"start": start, "end": end, "title": title})
    return out


def work_dir_for(out: Path, task_id: str) -> Path:
    """断点续跑的工作目录：放在输出目录下，和任务一起清理。"""
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(task_id or "")) or "adhoc"
    return Path(out).parent / f".merge_work_{safe}"


def extract_frame(path: Path, t: float = 2.0, width: int = 720) -> bytes:
    """抽取一帧转成 JPEG，用于网页里的裁剪预览。"""
    try:
        t = max(0.0, float(t))
    except Exception:
        t = 0.0
    cmd = [C.FFMPEG, "-hide_banner", "-nostdin", "-v", "error",
           "-ss", f"{t:.2f}", "-i", str(path),
           "-frames:v", "1",
           "-vf", f"scale={max(160, int(width))}:-2",
           "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        cmd[cmd.index("-ss") + 1] = "0"
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("抽帧失败")
    return r.stdout


def probe_media(path: Path) -> dict:
    """通用探测：音频文件没有视频轨时也能拿到 acodec / duration。"""
    try:
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        data = json.loads(r.stdout or "{}")
    except Exception:
        return {}
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    v = next((x for x in streams if x.get("codec_type") == "video"), None)
    a = next((x for x in streams if x.get("codec_type") == "audio"), None)
    try:
        dur = float(fmt.get("duration") or (a or {}).get("duration")
                    or (v or {}).get("duration") or 0)
    except Exception:
        dur = 0.0
    return {"vcodec": (v or {}).get("codec_name"),
            "acodec": (a or {}).get("codec_name"),
            "duration": dur,
            "width": (v or {}).get("width"),
            "height": (v or {}).get("height"),
            "sample_rate": (a or {}).get("sample_rate")}
