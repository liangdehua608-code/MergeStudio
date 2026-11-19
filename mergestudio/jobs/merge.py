"""合并任务：无损直拷 / 智能转码 / 全量转码，含批量裁剪与合并后自检。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .. import config as C
from .. import encoders
from ..media import (
    build_chapter_meta,
    build_chapters,
    compute_output_path,
    fmt_size,
    get_media_duration,
    get_stream_duration,
    parse_trim_seconds,
    probe_full,
    probe_output_summary,
    work_dir_for,
    run_ffmpeg,
    scan_folder,
    signature,
    trim_span,
)
from ..task import JOB, RUNNING_STATES

# ======================== 拼接底层函数 ========================
SEGMENT_RETRIES = 2
_FATAL_HINTS = ("no space left", "permission denied", "invalid data",
                "moov atom", "could not open", "cannot open",
                "no such file", "文件不存在", "拒绝访问")


def _retryable(err: str) -> bool:
    """只有「看起来像偶发问题」的失败才自动重试；磁盘满/坏文件不重试。"""
    low = (err or "").lower()
    return not any(h in low for h in _FATAL_HINTS)


def _params_hash(params: dict, files_info: list) -> str:
    payload = {k: params.get(k) for k in
               ("mode", "encoder", "crf", "preset", "size", "fps",
                "trim_head", "trim_tail", "chapters", "crop")}
    payload["files"] = [(i.get("path"), i.get("size")) for i in files_info]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def keyframe_at_or_before(path: Path, t: float):
    """
    v4: 找 t 之前最近的关键帧时间；探测失败返回 None。
    -c copy 裁剪只能从关键帧开始，先把 -ss 对齐到关键帧，
    再用 -copyts + 绝对 -t 计算终点，才能保证分段时长精确等于目标值。
    """
    if t <= 0.05:
        return 0.0
    try:
        start = max(0.0, t - 12)
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-skip_frame", "nokey",
             "-read_intervals", f"{start:.3f}%+{t - start + 1:.3f}",
             "-show_entries", "frame=pts_time",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        best = None
        for line in (r.stdout or "").splitlines():
            val = line.strip().rstrip(",").strip()
            if not val:
                continue
            try:
                v = float(val)
            except ValueError:
                continue
            if v <= t + 0.001:
                best = v if best is None else max(best, v)
        return best
    except Exception:
        return None


def probe_audio_sr(path: Path):
    """探测音频采样率，返回 (sample_rate, channels)，无音频返回 (None, None)"""
    try:
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace")
        if r.returncode != 0 or not r.stdout.strip():
            return None, None
        parts = [x.strip() for x in r.stdout.strip().split(",") if x.strip()]
        if not parts:
            return None, None
        sr = parts[0]
        ch = parts[1] if len(parts) > 1 else None
        return (sr if sr.isdigit() else None), ch
    except Exception:
        return None, None


def decide_audio_target(files):
    """
    统计所有文件音频采样率，返回 (target_sr, need_reencode, dist)
    - target_sr: 多数派采样率（无音频则 None）
    - need_reencode: 是否存在采样率不一致，需要统一重编码音频
    - dist: {采样率: 文件数}
    """
    dist = {}
    for f in files:
        sr, _ = probe_audio_sr(f)
        if sr:
            dist[sr] = dist.get(sr, 0) + 1
    if not dist:
        return None, False, {}
    target_sr = max(dist.items(), key=lambda x: x[1])[0]
    need_reencode = len(dist) > 1
    return target_sr, need_reencode, dist


def do_copy(files, out: Path, total_sec: float,
            on_progress=None, progress_scale=1.0, target_sr=None,
            target_ts=None, trims=None, chapter_titles=None):
    """
    v3.1 拼接核心（已修正音频采样率不一致导致的时长错乱）

    真正的根因（实测确认）：
      MP4 里视频时间基 1/16000、音频时间基 1/44100 或 1/48000 是**绑定在流上的**。
      concat demuxer 以**第一个文件**的流参数为基准解释后续所有包的 PTS。
      当首文件是 48000Hz、后续是 44100Hz 时，44100 的音频包被当成 48000 解读，
      时长被压缩到 44100/48000 = 91.875%，音频整体"变短"，视频轨 PTS 被撑出空隙，
      ffprobe 读出的 format duration 因此虚高。

    修复策略：
      1. 统计所有分段音频采样率，取多数派作为统一目标
      2. 视频流 -c:v copy（无损、快）
      3. 音频流：采样率不一致时重编码到统一采样率（AAC 重编码很快）
         采样率完全一致时才允许 -c:a copy
      4. 移除 copy 模式下无效且有害的 -async/-vsync
    """
    out = Path(os.path.abspath(str(out)))
    tmp = Path(os.path.abspath(f"{out}.concat_list_{os.getpid()}.txt"))
    tmp_segs_dir = None
    meta_file = None
    try:
        # 第一步：统计音频采样率，决定是否需要统一重编码
        if target_sr is None:
            target_sr, need_reencode, dist = decide_audio_target(files)
        else:
            _, _, dist = decide_audio_target(files)
            need_reencode = len(dist) > 1

        if dist:
            dist_str = " / ".join(f"{k}Hz×{v}" for k, v in
                                  sorted(dist.items(), key=lambda x: -x[1]))
            JOB.log(f"🎵 音频采样率分布：{dist_str}")
            if need_reencode:
                JOB.log(f"🎵 采样率不一致 → 统一重编码音频到 {target_sr}Hz"
                        f"（视频保持无损拷贝）")
            else:
                JOB.log(f"🎵 采样率一致（{target_sr}Hz）→ 音频可直接拷贝")

        # 第二步：逐个重封装，统一音频采样率、清除 Edit List、修正 PTS
        tmp_segs_dir = out.parent / f".concat_seg_{os.getpid()}_{int(time.time())}"
        tmp_segs_dir.mkdir(parents=True, exist_ok=True)
        normalized_files = []
        seg_durs = []
        # 进度条用的计数：本次要重封装/拼接的分段数
        JOB.update(total_files=len(files), done_files=0)

        for idx, src in enumerate(files):
            if JOB.cancel:
                raise RuntimeError("已取消")
            dst = tmp_segs_dir / f"seg_{idx:05d}.mp4"

            src_sr, src_ch = probe_audio_sr(src)
            # 该文件音频是否与目标一致
            audio_ok = (src_sr is None) or (target_sr is None) or (src_sr == target_sr)

            # v4: 裁剪参数（head/tail/dur）
            head, tail, src_dur = 0.0, 0.0, 0.0
            if trims:
                t = trims[idx] if idx < len(trims) else None
                if isinstance(t, dict):
                    head = float(t.get("head") or 0)
                    tail = float(t.get("tail") or 0)
                    src_dur = float(t.get("dur") or 0)
                elif isinstance(t, (tuple, list)) and len(t) >= 2:
                    head, tail = float(t[0]), float(t[1])
                    src_dur = float(t[2]) if len(t) > 2 else 0.0
            eff = trim_span(src_dur, head, tail) if src_dur else 0.0

            cmd = [
                C.FFMPEG, "-hide_banner", "-nostdin", "-y",
                "-fflags", "+genpts+igndts+discardcorrupt",
            ]
            # v4: 片头裁剪 —— 先把 -ss 对齐到真实关键帧，再配 -copyts 用绝对 -t 收尾
            # 说明：-c copy 只能从关键帧起剪；-t 在 -copyts 下是「输入时间轴绝对终点」，
            # 因此终点 = 实际起点 + 目标时长，这样分段时长才精确（否则会多剪/少剪一个 GOP）
            trimming = (head > 0.05 or tail > 0.05)
            seg_start = 0.0
            if trimming:
                if head > 0.05:
                    kf = keyframe_at_or_before(src, head)
                    seg_start = kf if kf is not None else head
                    cmd += ["-ss", f"{seg_start:.3f}"]
                cmd += ["-copyts"]
            cmd += [
                "-i", str(src),
                "-map", "0:v:0",
                "-map", "0:a:0?",
                # 视频始终无损拷贝
                "-c:v", "copy",
            ]
            if src_sr is None:
                # 探测失败时再用完整 probe 兜底，避免误判「无音频」把音轨丢掉
                _info = probe_full(src) or {}
                if _info.get("acodec"):
                    src_sr = str(_info.get("sample_rate") or target_sr or "48000")
                    audio_ok = (target_sr is None) or (src_sr == target_sr)
                else:
                    # 确认无音频，无需处理
                    cmd += ["-an"]
            elif audio_ok:
                # 该文件采样率已与目标一致 → 音频直接拷贝（无损且无编码误差）
                cmd += ["-c:a", "copy"]
            else:
                # 采样率不匹配 → 重编码到统一采样率
                cmd += ["-c:a", "aac", "-b:a", "192k",
                        "-ar", str(target_sr), "-ac", "2"]
            # v3.2: 统一视频时间基，避免 concat 时因 time_base 不一致导致时长爆炸
            if target_ts and out.suffix.lower() in (".mp4", ".m4v", ".mov"):
                cmd += ["-video_track_timescale", str(target_ts)]
            # v4: 片尾裁剪 / 输出总时长限制（-copyts 下为输入时间轴绝对终点）
            if trimming and eff > 0:
                cmd += ["-t", f"{seg_start + eff:.3f}"]
            else:
                cmd += ["-avoid_negative_ts", "make_zero"]
            cmd += ["-movflags", "+faststart", str(dst)]

            rc, err, canceled = run_ffmpeg(cmd, should_cancel=lambda: JOB.cancel)
            if canceled:
                raise RuntimeError("已取消")
            if rc != 0:
                raise RuntimeError(
                    f"重封装失败 [{src.name}]: {err[:300] or f'ffmpeg 退出码 {rc}'}\n"
                    f"  → 建议改用「全部转码」模式处理该文件"
                )

            # 校验重封装后的音视频时长是否正常（允许 0.3s 编码误差）
            v_dur = get_stream_duration(dst, "v:0")
            a_dur = get_stream_duration(dst, "a:0")
            if v_dur and a_dur and abs(v_dur - a_dur) > 0.5:
                JOB.log(f"  ⚠️ 分段 [{src.name[:30]}] 音视频时长差 "
                        f"{v_dur - a_dur:+.2f}s，已记录")

            normalized_files.append(dst)
            # v4: 记录分段实测时长，用于精确计算章节与预期总长
            seg_durs.append(float(v_dur or eff or 0.0))

            mark = ""
            if src_sr and target_sr and src_sr != target_sr:
                mark = f" (音频 {src_sr}→{target_sr}Hz)"
            if head > 0.05 or tail > 0.05:
                mark += f" ✂ -{head:.1f}s/-{tail:.1f}s"
            JOB.log(f"  [{idx+1}/{len(files)}] 处理 {src.name[:50]}{mark}")
            JOB.update(done_files=idx + 1)

            if on_progress:
                on_progress(0.1 * (idx + 1) / len(files))

        # 第三步：生成拼接列表并执行最终合并
        with open(tmp, "w", encoding="utf-8") as f:
            for p in normalized_files:
                s = str(p).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{s}'\n")

        # v4: 章节元数据（按分段实测时长累加，保证时间点精确）
        meta_file = None
        chapters = []
        if chapter_titles:
            chapters = build_chapters(chapter_titles, seg_durs)
            if chapters:
                meta_file = out.parent / f".chapters_{os.getpid()}_{int(time.time())}.txt"
                build_chapter_meta(chapters, meta_file)
                JOB.log(f"🔖 写入 {len(chapters)} 个章节标记")

        actual_total = sum(seg_durs)
        if total_sec and abs(actual_total - total_sec) > 0.5:
            JOB.log(f"ℹ️ 分段实测合计 {actual_total:.2f}s"
                    f"（理论 {total_sec:.2f}s，裁剪关键帧对齐差 "
                    f"{actual_total - total_sec:+.2f}s）")

        cmd = [C.FFMPEG, "-hide_banner", "-nostdin", "-y",
               "-fflags", "+genpts",
               "-progress", "pipe:1", "-nostats",
               "-f", "concat", "-safe", "0", "-i", str(tmp)]
        if meta_file:
            cmd += ["-i", str(meta_file)]
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        if meta_file:
            cmd += ["-map_chapters", "1"]
        cmd += ["-c", "copy",
                "-avoid_negative_ts", "make_zero"]
        if out.suffix.lower() in (".mp4", ".m4v", ".mov"):
            cmd += ["-movflags", "+faststart"]
        cmd += [str(out)]

        def _merge_progress(f):
            if on_progress:
                on_progress(0.1 + 0.9 * f)

        cb = _merge_progress if on_progress else (
            lambda f: JOB.update(progress=(0.1 + 0.9 * f) * progress_scale)
        )
        rc, err, canceled = run_ffmpeg(cmd, total_sec, on_progress=cb,
                                       should_cancel=lambda: JOB.cancel)
        if canceled:
            raise RuntimeError("已取消")
        if rc != 0:
            raise RuntimeError(err or f"ffmpeg 退出码 {rc}")
        return {"total": actual_total, "chapters": len(chapters)}
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
        if meta_file:
            try:
                Path(meta_file).unlink()
            except Exception:
                pass
        if tmp_segs_dir and tmp_segs_dir.exists():
            shutil.rmtree(tmp_segs_dir, ignore_errors=True)

def _has_edit_list_quick(path: Path) -> bool:
    """快速检测文件是否有 Edit List（用于日志显示）"""
    try:
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "side_data=display_matrix",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace")
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return False

def encode_one(src: Path, dst: Path, w: int, h: int, fps: str,
               has_audio: bool, crf: int, preset: str, encoder: str,
               duration: float, on_progress, should_cancel,
               target_sr: str = "48000", target_ts: str = None, trim=None,
               crop=None):
    """单文件转码，支持多种编码器，传入 duration 用于进度计算
    v3.1: 音频采样率跟随全局统一目标 target_sr，不再硬编码 48000
    v3.2: 视频 timescale 跟随全局统一目标 target_ts，避免与 copy 段冲突
    v4:   trim=(head, tail) 时做帧级精确裁剪（转码天然精确）
    v6:   crop=(left, top, right, bottom) 时先按源画面裁掉边框，再缩放到目标尺寸"""
    vargs = encoders.video_args(encoder, preset, crf)

    head, tail = (trim or (0.0, 0.0))[:2]
    eff = trim_span(duration, float(head or 0), float(tail or 0)) if duration else 0.0

    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
          f"fps={fps},format=yuv420p")
    if crop:
        cl, ct, cr, cb = [max(0, int(x or 0)) for x in crop]
        if cl or ct or cr or cb:
            # 宽高取偶数，避免 yuv420p 报错
            vf = (f"crop=trunc((iw-{cl + cr})/2)*2:"
                  f"trunc((ih-{ct + cb})/2)*2:{cl}:{ct}," + vf)

    cmd = [C.FFMPEG, "-hide_banner", "-nostdin", "-y",
           "-progress", "pipe:1", "-nostats"]
    if head and float(head) > 0.05:
        cmd += ["-ss", f"{float(head):.3f}"]
    cmd += ["-i", str(src)]

    if has_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={target_sr}:cl=stereo",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest"]

    cmd += ["-vf", vf]
    cmd += vargs

    # v3.1: -ar 跟随统一采样率，避免与其它分段不一致
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", str(target_sr), "-ac", "2"]
    # v3.2: 强制视频 timescale，确保转码段与 copy 段的 time_base 一致
    if target_ts and dst.suffix.lower() in (".mp4", ".m4v", ".mov"):
        cmd += ["-video_track_timescale", str(target_ts)]
    # v4: 精确裁剪的输出时长
    if eff > 0 and (float(head or 0) > 0.05 or float(tail or 0) > 0.05):
        cmd += ["-t", f"{eff:.3f}"]
    cmd += ["-movflags", "+faststart", str(dst)]

    # 传入单文件时长，让内部进度回调正常触发
    rc, err, canceled = run_ffmpeg(cmd, total_sec=(eff or duration),
                                   on_progress=on_progress,
                                   should_cancel=should_cancel)
    if canceled:
        raise RuntimeError("已取消")
    if rc != 0:
        raise RuntimeError(err or f"ffmpeg 退出码 {rc}")

def finish_success(out: Path, fallback_duration: float,
                   encoded_count: int, copied_count: int,
                   source_duration: float = 0.0,
                   expected_duration: float = 0.0,
                   chapter_count: int = 0,
                   expected_frames: int = 0,
                   skipped=None):
    """v4: 合并后自检 —— 时长 / 音视频同步 / 帧数 / 时间基 / 采样率 逐项核对"""
    info = probe_output_summary(out)
    final_dur = info.get("duration") or get_media_duration(out) or fallback_duration
    final_size = info.get("size") or (out.stat().st_size if out.exists() else 0)
    v_dur = info.get("video_duration")
    a_dur = info.get("audio_duration")
    sync = abs(v_dur - a_dur) if (v_dur and a_dur) else None
    frames = info.get("frames") or 0

    exp = expected_duration or source_duration
    delta = final_dur - exp if exp > 0 else 0.0
    tol = max(2.0, exp * 0.002)  # 容差：2 秒或 0.2%

    notes, level = [], "ok"
    if exp > 0 and abs(delta) > tol:
        level = "warn"
        notes.append(f"时长偏差 {delta:+.2f}s，超过容差 ±{tol:.1f}s")
    if sync is not None and sync > 1.0:
        level = "warn"
        notes.append(f"音视频不同步 {sync:.2f}s")
    if expected_frames and frames and abs(frames - expected_frames) > 2:
        level = "warn"
        notes.append(f"帧数 {frames} 与源 {expected_frames} 不一致")

    report = {
        "duration": final_dur,
        "video_duration": v_dur,
        "audio_duration": a_dur,
        "sync_delta": sync,
        "delta": delta,
        "source_duration": source_duration,
        "expected_duration": exp,
        "frames": frames,
        "expected_frames": expected_frames,
        "time_base": info.get("time_base"),
        "sample_rate": info.get("sample_rate"),
        "chapters": chapter_count,
        "level": level,
        "notes": notes,
        "skipped": skipped or [],
    }
    if skipped:
        level = "warn"
        report["level"] = "warn"
        report["notes"].append(f"已跳过 {len(skipped)} 个失败分段")

    JOB.update(state="done", progress=1.0, message="拼接完成",
               final_duration=final_dur, final_size=final_size,
               encoded_count=encoded_count, copied_count=copied_count,
               source_duration=source_duration, duration_delta=delta,
               report=report)

    JOB.log(f"✅ 输出：{out}")
    JOB.log(f"   时长 {final_dur:.2f}s · 大小 {fmt_size(final_size)} · "
            f"转码 {encoded_count} 段 / 拷贝 {copied_count} 段")
    if skipped:
        JOB.log(f"⚠️ 跳过 {len(skipped)} 个失败分段："
                + "、".join(s.get("name", "?") for s in skipped[:5])
                + ("…" if len(skipped) > 5 else ""))

    # ---- v4 自检报告 ----
    JOB.log("─" * 46)
    JOB.log("🔎 自检报告")
    if exp > 0:
        flag = "✅" if abs(delta) <= tol else "⚠️"
        JOB.log(f"   {flag} 时长：源(裁剪后) {exp:.2f}s → 输出 {final_dur:.2f}s "
                f"({delta:+.2f}s)")
    if v_dur and a_dur:
        flag = "✅" if (sync or 0) <= 1.0 else "⚠️"
        JOB.log(f"   {flag} 同步：视频轨 {v_dur:.2f}s / 音频轨 {a_dur:.2f}s "
                f"(差 {sync:.2f}s)")
    if frames:
        if expected_frames:
            flag = "✅" if abs(frames - expected_frames) <= 2 else "⚠️"
            JOB.log(f"   {flag} 帧数：{frames}（源 {expected_frames}）")
        else:
            JOB.log(f"   ℹ️ 帧数：{frames}（裁剪后无法直接比对）")
    if info.get("time_base"):
        JOB.log(f"   ✅ 时间基：{info.get('time_base')}")
    if info.get("sample_rate"):
        JOB.log(f"   ✅ 采样率：{info.get('sample_rate')}Hz")
    if chapter_count:
        JOB.log(f"   ✅ 章节：{chapter_count} 个")
    for n in notes:
        JOB.log(f"   ⚠️ {n}")
    if level == "ok":
        JOB.log("   结论：全部指标正常 ✅")
    else:
        JOB.log("   结论：存在偏差，请检查上方 ⚠️ 项")
    JOB.log("─" * 46)

# ======================== 拼接主流程（并行转码+自定义顺序）========================
def job_run(params: dict):
    JOB.reset()
    work = None
    try:
        folder = str(params.get("folder", "")).strip()
        if not folder:
            raise ValueError("请先选择源文件夹")
        if not C.FFMPEG or not C.FFPROBE:
            raise RuntimeError("尚未设置 ffmpeg/ffprobe 路径")

        recursive = bool(params.get("recursive"))
        mode = params.get("mode", "auto")
        crf = int(params.get("crf", 20))
        preset = params.get("preset", "veryfast")
        encoder = params.get("encoder", "libx264")
        parallel = int(params.get("parallel", 2))
        size_arg = (params.get("size") or "").strip()
        fps_arg = (params.get("fps") or "").strip()
        out_arg = (params.get("output") or "").strip()
        custom_order = params.get("file_order", [])

        out = compute_output_path(folder, out_arg)
        if out.suffix == "":
            out = out.with_suffix(".mp4")
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"无法创建输出目录：{e}")

        JOB.update(output=str(out))
        JOB.update(state="scanning", message="正在扫描与探测…")

        sc = scan_folder(folder, recursive, exclude_path=out)
        files_info_map = {item["path"]: item for item in sc["files"]}

        # 按自定义顺序或默认顺序排列
        if custom_order and isinstance(custom_order, list) and len(custom_order) > 0:
            files_info = [files_info_map[p] for p in custom_order if p in files_info_map]
        else:
            files_info = sc["files"]

        if not files_info:
            raise RuntimeError("文件夹里没有可用的视频文件")

        files = [Path(i["path"]) for i in files_info]
        durations = [max(float(i.get("duration") or 0), 0) for i in files_info]

        # ---- v4: 批量裁剪片头片尾 ----
        trim_head = parse_trim_seconds(params.get("trim_head", 0))
        trim_tail = parse_trim_seconds(params.get("trim_tail", 0))
        want_chapters = bool(params.get("chapters", True))

        # 失败处理由用户选择：abort = 中止整批（默认） / skip = 跳过失败集继续
        on_error = str(params.get("on_error") or "abort").lower()
        if on_error not in ("abort", "skip"):
            on_error = "abort"
        resume = bool(params.get("_resume"))
        skipped_segments = []

        # ---- v6: 画面边框裁剪（去台标 / 去黑边） ----
        crop_cfg = params.get("crop") or {}
        crop_on = bool(crop_cfg.get("enabled"))
        crop_l = max(0, int(crop_cfg.get("left", 0) or 0))
        crop_t = max(0, int(crop_cfg.get("top", 0) or 0))
        crop_r = max(0, int(crop_cfg.get("right", 0) or 0))
        crop_b = max(0, int(crop_cfg.get("bottom", 0) or 0))
        crop_scale_back = bool(crop_cfg.get("scale_back"))

        # 编码器可用性检查：不可用就直接报人话，不要跑到一半才失败
        if mode != "copy":
            _st = encoders.ensure(encoder)
            if not _st.get("ok"):
                raise RuntimeError(
                    f"编码器 {encoder} 不可用：{_st.get('reason') or '未知原因'}。"
                    f"请在「设置」页改用 libx264（CPU）后重试。")
            _lim = encoders.max_parallel(encoder)
            if _lim and parallel > _lim:
                JOB.log(f"ℹ️ {encoder} 建议最多 {_lim} 路并发，"
                        f"已从 {parallel} 调整为 {_lim}")
                parallel = _lim
        crop_rect = (crop_l, crop_t, crop_r, crop_b) if crop_on else None
        if crop_on and not any(crop_rect):
            crop_on, crop_rect = False, None

        trims, eff_durations = [], []
        for d in durations:
            trims.append({"head": trim_head, "tail": trim_tail, "dur": d})
            eff_durations.append(trim_span(d, trim_head, trim_tail))

        # 裁剪后没内容的文件剔除（避免产出 0 时长分段）
        keep = [i for i, e in enumerate(eff_durations) if e >= 0.5]
        if len(keep) < len(files):
            JOB.log(f"⚠️ {len(files) - len(keep)} 个文件裁剪后不足 0.5s，已跳过")
            files = [files[i] for i in keep]
            files_info = [files_info[i] for i in keep]
            durations = [durations[i] for i in keep]
            trims = [trims[i] for i in keep]
            eff_durations = [eff_durations[i] for i in keep]
        if not files:
            raise RuntimeError("裁剪参数过大，所有文件都被剪空了")

        raw_duration = sum(durations)          # 原始总时长
        total_sec = sum(eff_durations)         # 裁剪后的预期总时长
        source_duration = total_sec            # 对比基准 = 裁剪后
        expected_duration = total_sec
        JOB.update(source_duration=source_duration)

        if trim_head > 0.05 or trim_tail > 0.05:
            saved = raw_duration - total_sec
            JOB.log(f"✂️ 裁剪：每集片头 -{trim_head:g}s / 片尾 -{trim_tail:g}s")
            JOB.log(f"   原始总时长 {raw_duration/3600:.2f}h → 裁剪后 "
                    f"{total_sec/3600:.2f}h（共省 {saved/60:.1f} 分钟）")

        # v4: 章节标题（时间点由各分段实测时长决定，合并时生成）
        chapter_titles = None
        if want_chapters:
            chapter_titles = [Path(i["path"]).stem for i in files_info]

        # v3: 如果有 Edit List 文件，提示用户
        el_count = sum(1 for i in files_info if i.get("has_edit_list"))
        if el_count > 0:
            JOB.log(f"ℹ️ 检测到 {el_count} 个文件包含 Edit List，将自动清除")

        # 单文件直接复制（启用裁剪时必须走转码，不能直拷）
        if len(files) == 1 and not crop_on:
            JOB.update(state="copying", message="仅一个文件，直接重封装清除 Edit List…")
            # v3: 单文件也走重封装，确保清除 Edit List
            res = do_copy(files, out, total_sec, trims=trims,
                          chapter_titles=chapter_titles)
            res = res or {}
            finish_success(out, total_sec, 0, 1, source_duration,
                           expected_duration=res.get("total") or expected_duration,
                           chapter_count=res.get("chapters", 0))
            return

        # 分析分组
        groups = {}
        for idx, item in enumerate(files_info):
            if not item.get("ok"):
                continue
            groups.setdefault(signature(item), []).append(idx)

        if not groups:
            raise RuntimeError("所有视频都无法读取，无法拼接")

        majority_sig, majority_indices = max(groups.items(), key=lambda x: len(x[1]))
        majority_set = set(majority_indices)
        need_encode_indices = [i for i in range(len(files_info))
                               if i not in majority_set]
        need_encode_set = set(need_encode_indices)
        majority_item = files_info[majority_indices[0]]

        # 目标参数
        if size_arg:
            w, h = [int(x) for x in size_arg.lower().split("x")]
        else:
            w = int(majority_item.get("width") or 1920)
            h = int(majority_item.get("height") or 1080)
        w -= w % 2
        h -= h % 2

        # v6: 裁剪后目标尺寸
        if crop_on:
            src_w = int(majority_item.get("width") or 0)
            src_h = int(majority_item.get("height") or 0)
            cw = ((src_w - crop_l - crop_r) // 2) * 2
            ch = ((src_h - crop_t - crop_b) // 2) * 2
            if src_w <= 0 or src_h <= 0 or cw < 64 or ch < 64:
                raise RuntimeError(
                    f"裁剪参数过大：源 {src_w}x{src_h} 裁掉 "
                    f"{crop_l}/{crop_r}/{crop_t}/{crop_b} 后只剩 {cw}x{ch}")
            # 逐个体检，避免个别文件分辨率偏小导致 crop 失败
            for it in files_info:
                iw, ih = int(it.get("width") or 0), int(it.get("height") or 0)
                if it.get("ok") and (iw <= crop_l + crop_r + 16 or
                                     ih <= crop_t + crop_b + 16):
                    raise RuntimeError(
                        f"{it['name']} 分辨率 {iw}x{ih} 小于裁剪范围")
            if crop_scale_back:
                JOB.log(f"✂️ 画面裁剪：左 {crop_l} / 上 {crop_t} / 右 {crop_r} / "
                        f"下 {crop_b} px，再缩放回 {w}x{h}")
            else:
                w, h = cw, ch
                JOB.log(f"✂️ 画面裁剪：左 {crop_l} / 上 {crop_t} / 右 {crop_r} / "
                        f"下 {crop_b} px → 输出 {w}x{h}（原 {src_w}x{src_h}）")
        fps = fps_arg or majority_item.get("fps") or "30/1"
        if not re.match(r"^\d+/\d+$", fps):
            try:
                fps = f"{int(float(fps))}/1"
            except ValueError:
                fps = "30/1"
        if fps.split("/")[0] == "0":
            fps = "30/1"

        # v3.1: 统一音频采样率（这是时长错乱的真正根因）
        # 统计所有文件采样率取多数派，作为全局统一目标
        target_sr, need_audio_reencode, sr_dist = decide_audio_target(files)
        if target_sr is None:
            target_sr = "48000"
        if sr_dist:
            dist_str = " / ".join(f"{k}Hz×{v}" for k, v in
                                  sorted(sr_dist.items(), key=lambda x: -x[1]))
            JOB.log(f"🎵 音频采样率分布：{dist_str}")
            if need_audio_reencode:
                JOB.log(f"🎵 检测到采样率不一致 → 统一重编码音频至 {target_sr}Hz"
                        f"（视频无损拷贝，不会明显变慢）")
            else:
                JOB.log(f"🎵 采样率一致（{target_sr}Hz）→ 音频直接拷贝")

        # v3.2: 统一视频 timescale（这是视频时长爆炸的真正根因）
        # 转码段输出默认 time_base=1/12288，与原始 1/16000 不一致，
        # concat 按首段 time_base 解释，时长被拉伸 16000/12288=1.302 倍
        ts_dist = {}
        for it in files_info:
            if it.get("ok"):
                tb = it.get("time_base") or ""
                if "/" in tb:
                    ts = tb.split("/")[-1]
                    if ts.isdigit():
                        ts_dist[ts] = ts_dist.get(ts, 0) + 1
        target_ts = max(ts_dist.items(), key=lambda x: x[1])[0] if ts_dist else None
        if ts_dist:
            ts_str = " / ".join(f"1/{k}×{v}" for k, v in
                                sorted(ts_dist.items(), key=lambda x: -x[1]))
            JOB.log(f"🎬 视频时间基分布：{ts_str}")
            if len(ts_dist) > 1:
                JOB.log(f"🎬 时间基不一致 → 统一为 1/{target_ts}（防止拼接时长爆炸）")
            else:
                JOB.log(f"🎬 时间基一致（1/{target_ts}）")

        actual_mode = mode
        if mode == "auto":
            actual_mode = "copy" if len(groups) == 1 else "smart"
        if crop_on and actual_mode != "encode":
            JOB.log("✂️ 已启用画面裁剪 → 强制全部转码（裁剪无法无损直拷）")
            actual_mode = "encode"

        # v4: 只有纯拷贝模式才能直接比对帧数（转码/裁剪会改变帧数）
        expected_frames = 0
        if trim_head < 0.05 and trim_tail < 0.05 and not crop_on:
            expected_frames = sum(int(i.get("nb_frames") or 0) for i in files_info)

        # ---- 断点续跑：只对需要转码的分段保留中间结果 ----
        enc_dir = None
        reuse = {}
        p_hash = _params_hash(params, files_info)
        if mode != "copy":
            work = work_dir_for(out, str(JOB.id))
            state_file = work / "state.json"
            if work.exists():
                ok_resume = False
                if resume and state_file.is_file():
                    try:
                        saved = json.loads(state_file.read_text(encoding="utf-8"))
                        ok_resume = (saved.get("hash") == p_hash)
                    except Exception:
                        ok_resume = False
                if not ok_resume:
                    shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            enc_dir = work / "enc"
            enc_dir.mkdir(parents=True, exist_ok=True)
            try:
                state_file.write_text(json.dumps({"v": 1, "hash": p_hash},
                                                 ensure_ascii=False),
                                      encoding="utf-8")
            except Exception:
                pass
            for i in range(len(files)):
                cand = enc_dir / f"{i:05d}.mp4"
                if cand.exists() and cand.stat().st_size > 1024:
                    reuse[i] = cand
            if reuse:
                JOB.log(f"♻️ 断点续跑：复用 {len(reuse)} 个已完成分段"
                        f"（共 {len(files)} 个）")

        def _finish(enc_count, cp_count, kept_total, res, kept_frames=None):
            res = res or {}
            if kept_frames is None:
                kept_frames = expected_frames
            finish_success(out, kept_total, enc_count, cp_count, kept_total,
                           expected_duration=res.get("total") or kept_total,
                           chapter_count=res.get("chapters", 0),
                           expected_frames=kept_frames,
                           skipped=skipped_segments)
            if work:
                shutil.rmtree(work, ignore_errors=True)

        # ============ 模式 1：无损直拷 ============
        if actual_mode == "copy":
            JOB.update(state="copying", message=f"无损直拷 · {len(files)} 个文件",
                       total_files=len(files))
            JOB.log(f"模式：无损直拷 · {len(files)} 个文件（v4: 统一采样率+时间基+裁剪）")
            try:
                res = do_copy(files, out, total_sec, target_sr=target_sr,
                              target_ts=target_ts, trims=trims,
                              chapter_titles=chapter_titles)
                _finish(0, len(files), total_sec, res, expected_frames)
                return
            except RuntimeError as e:
                if str(e) == "已取消":
                    raise
                if mode == "copy":
                    raise
                JOB.log(f"⚠️ 无损直拷失败：{e}，切换到智能模式")
                actual_mode = "smart"

        # ============ 模式 2：智能（只转少数派 + 并行转码）============
        if actual_mode == "smart":
            n_enc = len(need_encode_indices)
            n_cp = len(majority_indices)
            JOB.update(state="encoding", total_files=n_enc,
                       message=f"智能模式 · 转码 {n_enc} 个 / 拷贝 {n_cp} 个 · {parallel} 路并行")
            JOB.log(f"模式：智能 · 多数派 {n_cp} 个无损拷贝，"
                    f"少数派 {n_enc} 个转码至 {w}x{h} @ {fps} · {parallel} 路并行")

            tmp_dir = enc_dir

            need_encode_secs = sum(eff_durations[i] for i in need_encode_indices) or 1.0
            progress_lock = threading.Lock()
            task_progress = {i: 0.0 for i in need_encode_indices}
            enc_counter = [0]

            def _update_total():
                done_sec = sum(task_progress.values())
                JOB.update(progress=0.9 * min(done_sec / need_encode_secs, 1.0))

            def _encode_task(idx):
                f = files[idx]
                if JOB.cancel:
                    raise RuntimeError("已取消")

                with progress_lock:
                    enc_counter[0] += 1
                    cur_cnt = enc_counter[0]
                JOB.update(current_file=f.name, done_files=cur_cnt - 1,
                           message=f"转码 {cur_cnt}/{n_enc}")
                JOB.log(f"[转码 {cur_cnt}/{n_enc}] {f.name}")

                dst = tmp_dir / f"{idx:05d}.mp4"
                file_dur = eff_durations[idx]

                def _file_progress(frac):
                    with progress_lock:
                        task_progress[idx] = frac * file_dur
                        _update_total()

                attempt = 0
                while True:
                    try:
                        encode_one(f, dst, w, h, fps,
                                   bool(files_info[idx].get("acodec")),
                                   crf, preset, encoder, durations[idx],
                                   _file_progress, lambda: JOB.cancel,
                                   target_sr=target_sr, target_ts=target_ts,
                                   trim=(trim_head, trim_tail), crop=crop_rect)
                        with progress_lock:
                            task_progress[idx] = file_dur
                            _update_total()
                        return idx, dst, True
                    except Exception as e:
                        msg = str(e)
                        if msg == "已取消":
                            return idx, dst, False, msg
                        # 底层自动重试（不改变输出，只是重跑失败的转码）
                        if attempt < SEGMENT_RETRIES and _retryable(msg):
                            attempt += 1
                            JOB.log(f"  ↻ 自动重试 {attempt}/{SEGMENT_RETRIES}："
                                    f"{f.name[:34]}")
                            time.sleep(1.0)
                            continue
                        return idx, dst, False, msg

            # 线程池并行执行（断点续跑：已完成的分段直接复用）
            results = {i: reuse[i] for i in need_encode_indices if i in reuse}
            for i in results:
                task_progress[i] = eff_durations[i]
            if reuse:
                _update_total()
            pending = [i for i in need_encode_indices if i not in reuse]
            with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
                futures = {ex.submit(_encode_task, i): i for i in pending}
                for future in as_completed(futures):
                    res = future.result()
                    if JOB.cancel:
                        raise RuntimeError("已取消")
                    idx, dst, ok = res[0], res[1], res[2]
                    if not ok:
                        if on_error == "skip":
                            skipped_segments.append({"name": files[idx].name,
                                                     "reason": str(res[3])[:300]})
                            JOB.log(f"⚠️ 跳过失败分段：{files[idx].name[:40]}")
                            continue
                        raise RuntimeError(f"转码失败 {files[idx].name}: {res[3]}")
                    results[idx] = dst

            if JOB.cancel:
                raise RuntimeError("已取消")

            # 按原始顺序组装分段列表（失败且选择跳过的不参与合并）
            segs, seg_trims, kept_titles, kept_idx = [], [], [], []
            for i in range(len(files)):
                if i in need_encode_set:
                    if i not in results:
                        continue  # 转码失败且用户选择了「跳过」
                    # 已转码的分段：裁剪已在转码时完成，不能再剪一次
                    segs.append(results[i])
                    seg_trims.append({"head": 0.0, "tail": 0.0,
                                      "dur": eff_durations[i]})
                else:
                    segs.append(files[i])
                    seg_trims.append(trims[i])
                kept_idx.append(i)
                if chapter_titles:
                    kept_titles.append(chapter_titles[i])

            if not segs:
                raise RuntimeError("所有分段都失败了，没有可合并的内容")

            kept_total = sum(eff_durations[i] for i in kept_idx)
            if skipped_segments:
                JOB.log(f"⚠️ 已跳过 {len(skipped_segments)} 个分段，"
                        f"按剩余 {len(segs)} 段合并")

            JOB.update(state="copying", message="合并分段文件…",
                       progress=0.9, current_file="")
            JOB.log(f"合并 {len(segs)} 个分段…")
            res = do_copy(segs, out, kept_total, target_sr=target_sr,
                          target_ts=target_ts, trims=seg_trims,
                          chapter_titles=kept_titles or None,
                          on_progress=lambda f: JOB.update(progress=0.9 + 0.1 * f))
            _finish(len(results), n_cp, kept_total, res,
                    0 if skipped_segments else expected_frames)
            return

        # ============ 模式 3：全部转码 + 并行 ============
        if actual_mode == "encode":
            JOB.update(state="encoding", total_files=len(files),
                       message=f"全部转码至 {w}x{h} @ {fps} · {parallel} 路并行")
            JOB.log(f"模式：全部转码 · {len(files)} 个 → {w}x{h} @ {fps} · {parallel} 路并行")

            tmp_dir = enc_dir

            progress_lock = threading.Lock()
            task_progress = {i: 0.0 for i in range(len(files))}
            done_counter = [0]

            def _update_total():
                done_sec = sum(task_progress.values())
                JOB.update(progress=0.9 * min(done_sec / total_sec, 1.0))

            def _encode_task(idx):
                f = files[idx]
                if JOB.cancel:
                    raise RuntimeError("已取消")

                with progress_lock:
                    done_counter[0] += 1
                    cur_cnt = done_counter[0]
                JOB.update(current_file=f.name, done_files=cur_cnt - 1,
                           message=f"转码 {cur_cnt}/{len(files)}")
                JOB.log(f"[{cur_cnt}/{len(files)}] 转码 {f.name}")

                dst = tmp_dir / f"{idx:05d}.mp4"
                file_dur = eff_durations[idx]

                def _file_progress(frac):
                    with progress_lock:
                        task_progress[idx] = frac * file_dur
                        _update_total()

                attempt = 0
                while True:
                    try:
                        encode_one(f, dst, w, h, fps,
                                   bool(files_info[idx].get("acodec")),
                                   crf, preset, encoder, durations[idx],
                                   _file_progress, lambda: JOB.cancel,
                                   target_sr=target_sr, target_ts=target_ts,
                                   trim=(trim_head, trim_tail), crop=crop_rect)
                        with progress_lock:
                            task_progress[idx] = file_dur
                            _update_total()
                        return idx, dst, True
                    except Exception as e:
                        msg = str(e)
                        if msg == "已取消":
                            return idx, dst, False, msg
                        # 底层自动重试（不改变输出，只是重跑失败的转码）
                        if attempt < SEGMENT_RETRIES and _retryable(msg):
                            attempt += 1
                            JOB.log(f"  ↻ 自动重试 {attempt}/{SEGMENT_RETRIES}："
                                    f"{f.name[:34]}")
                            time.sleep(1.0)
                            continue
                        return idx, dst, False, msg

            # 断点续跑：已完成的分段直接复用
            results = {i: reuse[i] for i in range(len(files)) if i in reuse}
            for i in results:
                task_progress[i] = eff_durations[i]
            if reuse:
                _update_total()
            pending = [i for i in range(len(files)) if i not in reuse]
            with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
                futures = {ex.submit(_encode_task, i): i for i in pending}
                for future in as_completed(futures):
                    res = future.result()
                    if JOB.cancel:
                        raise RuntimeError("已取消")
                    idx, dst, ok = res[0], res[1], res[2]
                    if not ok:
                        if on_error == "skip":
                            skipped_segments.append({"name": files[idx].name,
                                                     "reason": str(res[3])[:300]})
                            JOB.log(f"⚠️ 跳过失败分段：{files[idx].name[:40]}")
                            continue
                        raise RuntimeError(f"转码失败 {files[idx].name}: {res[3]}")
                    results[idx] = dst

            if JOB.cancel:
                raise RuntimeError("已取消")

            kept_idx = [i for i in range(len(files)) if i in results]
            if not kept_idx:
                raise RuntimeError("所有分段都失败了，没有可合并的内容")
            segs = [results[i] for i in kept_idx]
            # 全部已转码（含裁剪），合并阶段不再二次裁剪
            seg_trims = [{"head": 0.0, "tail": 0.0, "dur": eff_durations[i]}
                         for i in kept_idx]
            kept_titles = ([chapter_titles[i] for i in kept_idx]
                           if chapter_titles else None)
            kept_total = sum(eff_durations[i] for i in kept_idx)
            if skipped_segments:
                JOB.log(f"⚠️ 已跳过 {len(skipped_segments)} 个分段，"
                        f"按剩余 {len(segs)} 段合并")

            JOB.update(state="copying", message="合并分段文件…",
                       progress=0.9, current_file="")
            res = do_copy(segs, out, kept_total, target_sr=target_sr,
                          target_ts=target_ts, trims=seg_trims,
                          chapter_titles=kept_titles,
                          on_progress=lambda f: JOB.update(progress=0.9 + 0.1 * f))
            _finish(len(segs), 0, kept_total, res,
                    0 if skipped_segments else expected_frames)
            return

        raise RuntimeError(f"未知模式：{actual_mode}")
    except Exception as e:
        if str(e) == "已取消":
            JOB.update(state="canceled", message="已取消", error="用户取消")
            JOB.log("任务已取消")
        else:
            JOB.update(state="error", message=str(e), error=str(e))
            JOB.log("ERROR: " + str(e))
            JOB.log(traceback.format_exc(limit=3))
        # 保留已完成分段，用户可在任务详情里选择「断点续跑」
        if work:
            cnt = 0
            try:
                cnt = len(list((work / "enc").glob("*.mp4")))
            except Exception:
                pass
            JOB.update(work_dir=str(work), resume_count=cnt,
                       resumable=bool(cnt))
            if cnt:
                JOB.log(f"💾 已保留 {cnt} 个已完成分段，可点「断点续跑」继续")
