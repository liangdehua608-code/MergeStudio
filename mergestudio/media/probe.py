"""ffprobe 探测：单文件全量信息、流时长、章节、输出自检与预检打分。"""
from __future__ import annotations

import json
import subprocess

from .. import config as C


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _run_json(args, timeout=30) -> dict:
    try:
        r = subprocess.run([C.FFPROBE, "-v", "error", "-print_format", "json",
                            *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if r.returncode != 0:
            return {}
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


def probe_full(path):
    """全量探测一个视频：分辨率、帧率、时间基、音轨、字幕、Edit List、时长。

    时长优先取**视频流** duration：format duration 含 Edit List 偏移，
    用它算总长会虚高，这是 v3 之前拼完时长错乱的直接原因之一。
    """
    info = _run_json(["-show_streams", "-show_format",
                      "-show_entries", "stream_side_data=edit_list", str(path)])
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not v:
        return None

    # Edit List：side_data 里的 Display Matrix，或 format 标签带 edit 字样
    has_edit_list = False
    for s in streams:
        for sd in s.get("side_data_list", []) or []:
            if sd.get("side_data_type") == "Display Matrix":
                has_edit_list = True
    fmt_tags = (info.get("format") or {}).get("tags") or {}
    if any("edit" in k.lower() for k in fmt_tags):
        has_edit_list = True

    video_duration = _f(v.get("duration"))
    format_duration = _f((info.get("format") or {}).get("duration"))
    duration = video_duration if (video_duration and video_duration > 0) \
        else (format_duration or 0.0)
    if (format_duration and video_duration
            and abs(format_duration - video_duration) > 0.05):
        has_edit_list = True

    audio_duration = _f(a.get("duration")) if a else None
    try:
        nb_frames = int(v.get("nb_frames") or 0)
    except (TypeError, ValueError):
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
        "subtitle_codecs": [(s.get("codec_name") or "") for s in subs],
        "subtitles": [{"codec": s.get("codec_name") or "",
                       "lang": ((s.get("tags") or {}).get("language") or ""),
                       "title": ((s.get("tags") or {}).get("title") or "")}
                      for s in subs],
    }


def probe_media(path) -> dict:
    """通用探测：音频文件没有视频轨时也能拿到 acodec / duration。"""
    data = _run_json(["-show_streams", "-show_format", str(path)])
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    v = next((x for x in streams if x.get("codec_type") == "video"), None)
    a = next((x for x in streams if x.get("codec_type") == "audio"), None)
    dur = _f(fmt.get("duration") or (a or {}).get("duration")
             or (v or {}).get("duration")) or 0.0
    return {"vcodec": (v or {}).get("codec_name"),
            "acodec": (a or {}).get("codec_name"),
            "duration": dur,
            "width": (v or {}).get("width"),
            "height": (v or {}).get("height"),
            "sample_rate": (a or {}).get("sample_rate")}


def get_stream_duration(path, stream: str = "v:0"):
    """读取指定流的时长（v:0 / a:0）。"""
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


def get_media_duration(path):
    """视频流时长，失败时退回 format 时长。"""
    dur = get_stream_duration(path, "v:0")
    if dur is not None:
        return dur
    if not C.FFPROBE:
        return None
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


def probe_chapters(path) -> list:
    """读取视频内嵌章节（mp4 的章节会被识别为章节轨，不是字幕）。"""
    data = _run_json(["-show_chapters", str(path)], timeout=60)
    out = []
    for ch in data.get("chapters", []) or []:
        try:
            start = float(ch.get("start_time") or 0)
            end = float(ch.get("end_time") or 0)
        except Exception:
            continue
        if end > start:
            out.append({"start": start, "end": end,
                        "title": ((ch.get("tags") or {}).get("title") or "").strip()})
    return out


def probe_output_summary(path) -> dict:
    """合并后自检：一次性读取输出的关键指标。"""
    if not C.FFPROBE:
        return {}
    data = _run_json(["-show_streams", "-show_format", "-show_chapters",
                      str(path)], timeout=60)
    if not data:
        return {}
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    vd = _f((v or {}).get("duration"))
    ad = _f((a or {}).get("duration"))
    fmt = data.get("format") or {}
    frames = 0
    if v:
        try:
            frames = int(v.get("nb_frames") or 0)
        except (TypeError, ValueError):
            frames = 0
    return {
        "video_duration": vd,
        "audio_duration": ad,
        "format_duration": _f(fmt.get("duration")),
        "duration": vd or _f(fmt.get("duration")) or 0.0,
        "frames": frames,
        "time_base": (v or {}).get("time_base"),
        "sample_rate": (a or {}).get("sample_rate"),
        "size": int(fmt.get("size") or 0),
        "chapters": len(data.get("chapters", []) or []),
        "subtitles": len(subs),
        "subtitle_codecs": [(s.get("codec_name") or "") for s in subs],
    }


# ======================== 参数一致性签名 ========================
def signature(item: dict):
    """判断能否与多数派一起「无损直拷」的参数组合。

    刻意包含 time_base：转码输出默认 1/12288，与源 1/16000 不一致时 concat
    会按首段 time_base 解释后续包，时长被拉伸 1.3 倍（v3.2 的坑）。
    刻意不含 fps：实测 24 与 23.976 只要 time_base 相同，copy 拼接就正确，
    把 fps 算进来反而会把正常文件误判成少数派去转码。
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
    return f"{w}x{h} {codec}" + (" [EL]" if item.get("has_edit_list") else "")


def _ts_of(item: dict) -> str:
    tb = item.get("time_base") or ""
    return tb.split("/")[-1] if "/" in tb else ""


def analyze_items(items):
    """合并前预检：给每个文件打健康度标签（ok / warn / bad）。

    返回 (summary, 多数派时间基, 多数派采样率)。
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
        if not it.get("ok"):
            it["health"] = "bad"
            it["issues"] = ["无法读取该文件（损坏或编码不支持）"]
            continue

        issues, level = [], "ok"
        dur = float(it.get("duration") or 0)
        if dur <= 0.5:
            issues.append("时长过短（< 0.5s）")
            level = "bad"

        vd = float(it.get("video_duration") or dur or 0)
        ad = it.get("audio_duration")
        if ad and vd and abs(vd - float(ad)) > 0.5:
            issues.append(f"音视频时长差 {vd - float(ad):+.1f}s")
            level = level if level != "ok" else "warn"

        ts = _ts_of(it)
        if maj_ts and ts and ts != maj_ts:
            issues.append(f"时间基 1/{ts} 与多数派 1/{maj_ts} 不同")
            level = level if level != "ok" else "warn"

        sr = it.get("sample_rate")
        if maj_sr and sr and sr != maj_sr:
            issues.append(f"采样率 {sr}Hz 与多数派 {maj_sr}Hz 不同")
            level = level if level != "ok" else "warn"

        if not it.get("acodec"):
            issues.append("没有音频轨")
            level = level if level != "ok" else "warn"

        if it.get("has_edit_list"):
            issues.append("含 Edit List（拼接前会自动清除）")

        it["health"] = level
        it["issues"] = issues

    summary = {"ok": sum(1 for i in items if i.get("health") == "ok"),
               "warn": sum(1 for i in items if i.get("health") == "warn"),
               "bad": sum(1 for i in items if i.get("health") == "bad")}
    return summary, maj_ts, maj_sr
