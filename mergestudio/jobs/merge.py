"""合并任务：无损直拷 / 智能转码 / 全量转码，含批量裁剪、字幕保留与合并后自检。

三种模式其实只有「哪些分段要转码」这一处差别，所以转码、重试、断点复用、
失败策略都收在 `_BatchEncoder` 里，作业流程只剩「选模式 → 转码 → 组装 → 合并」。
"""
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
from .. import encoders, subtitles
from ..media import (
    build_chapter_meta,
    build_chapters,
    compute_output_path,
    fmt_size,
    get_media_duration,
    get_stream_duration,
    guard_output,
    output_family,
    parse_trim_seconds,
    probe_full,
    probe_output_summary,
    run_ffmpeg,
    scan_folder,
    signature,
    trim_span,
    work_dir_for,
)
from ..task import JOB

# ======================== 拼接底层函数 ========================
SEGMENT_RETRIES = 2
_FATAL_HINTS = ("no space left", "permission denied", "invalid data",
                "moov atom", "could not open", "cannot open",
                "no such file", "文件不存在", "拒绝访问")
CANCELED = "已取消"


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
    找 t 之前最近的关键帧时间；探测失败返回 None。
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
    """探测音频采样率，返回 (sample_rate, channels)，无音频返回 (None, None)。"""
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
        return (sr if sr.isdigit() else None), (parts[1] if len(parts) > 1 else None)
    except Exception:
        return None, None


def decide_audio_target(files):
    """统计所有文件音频采样率，返回 (多数派采样率, 是否需重编码, 分布)。"""
    dist = {}
    for f in files:
        sr, _ = probe_audio_sr(f)
        if sr:
            dist[sr] = dist.get(sr, 0) + 1
    if not dist:
        return None, False, {}
    target_sr = max(dist.items(), key=lambda x: x[1])[0]
    return target_sr, len(dist) > 1, dist


def do_copy(files, out: Path, total_sec: float,
            on_progress=None, progress_scale=1.0, target_sr=None,
            target_ts=None, trims=None, chapter_titles=None,
            keep_subs: bool = False):
    """无损拼接核心：逐段重封装归一再 concat。

    根因备忘：MP4 里视频时间基 1/16000、音频 1/44100 或 1/48000 是**绑定在流上的**。
    concat demuxer 以**第一个文件**的流参数解释后续所有包的 PTS。首文件 48000、
    后续 44100 时，音频包被当 48000 解读，时长被压缩到 91.875%，视频轨 PTS 被撑出空隙，
    ffprobe 读出的时长因此虚高。所以：

      1. 统计采样率取多数派，采样率不一致时**只重编码音频**（视频仍无损）；
      2. 统一 `-video_track_timescale`，避免转码段与拷贝段 time_base 冲突；
      3. copy 模式下不再加 `-async/-vsync`（对 copy 无效且有害）。
    """
    out = Path(os.path.abspath(str(out)))
    tmp = Path(os.path.abspath(f"{out}.concat_list_{os.getpid()}.txt"))
    tmp_segs_dir = None
    meta_file = None
    sub_args = ["-map", "0:s?", "-c:s",
                "mov_text" if out.suffix.lower() in C.MP4_EXTS else "copy"] \
        if keep_subs else []
    try:
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

        # 逐个重封装：统一音频采样率、清除 Edit List、修正 PTS
        tmp_segs_dir = out.parent / f".concat_seg_{os.getpid()}_{int(time.time())}"
        tmp_segs_dir.mkdir(parents=True, exist_ok=True)
        normalized_files, seg_durs = [], []
        JOB.update(total_files=len(files), done_files=0)

        for idx, src in enumerate(files):
            if JOB.cancel:
                raise RuntimeError(CANCELED)
            dst = tmp_segs_dir / f"seg_{idx:05d}.mp4"

            src_sr, _ = probe_audio_sr(src)
            audio_ok = (src_sr is None) or (target_sr is None) or (src_sr == target_sr)

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
                C.FFMPEG, "-hide_banner", "-y",
                "-fflags", "+genpts+igndts+discardcorrupt",
            ]
            # 片头裁剪：先把 -ss 对齐到真实关键帧，再配 -copyts 用绝对 -t 收尾。
            # -c copy 只能从关键帧起剪；-copyts 下 -t 是「输入时间轴绝对终点」，
            # 因此终点 = 实际起点 + 目标时长，分段时长才精确。
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
            cmd += sub_args
            if src_sr is None:
                # 探测失败时用完整 probe 兜底，避免误判「无音频」把音轨丢掉
                _info = probe_full(src) or {}
                if _info.get("acodec"):
                    src_sr = str(_info.get("sample_rate") or target_sr or "48000")
                    audio_ok = (target_sr is None) or (src_sr == target_sr)
                else:
                    cmd += ["-an"]
            elif audio_ok:
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "192k",
                        "-ar", str(target_sr), "-ac", "2"]
            if target_ts and out.suffix.lower() in C.MP4_EXTS:
                cmd += ["-video_track_timescale", str(target_ts)]
            if trimming and eff > 0:
                cmd += ["-t", f"{seg_start + eff:.3f}"]
            else:
                cmd += ["-avoid_negative_ts", "make_zero"]
            cmd += ["-movflags", "+faststart", str(dst)]

            rc, err, canceled = run_ffmpeg(cmd, should_cancel=lambda: JOB.cancel)
            if canceled:
                raise RuntimeError(CANCELED)
            if rc != 0:
                raise RuntimeError(
                    f"重封装失败 [{src.name}]: {err[:300] or f'ffmpeg 退出码 {rc}'}\n"
                    f"  → 建议改用「全部转码」模式处理该文件"
                )

            # 校验重封装后的音视频时长（允许 0.3s 编码误差）
            v_dur = get_stream_duration(dst, "v:0")
            a_dur = get_stream_duration(dst, "a:0")
            if v_dur and a_dur and abs(v_dur - a_dur) > 0.5:
                JOB.log(f"  ⚠️ 分段 [{src.name[:30]}] 音视频时长差 "
                        f"{v_dur - a_dur:+.2f}s，已记录")

            normalized_files.append(dst)
            seg_durs.append(float(v_dur or eff or 0.0))

            mark = ""
            if src_sr and target_sr and src_sr != target_sr:
                mark = f" (音频 {src_sr}→{target_sr}Hz)"
            if head > 0.05 or tail > 0.05:
                mark += f" ✂ -{head:.1f}s/-{tail:.1f}s"
            JOB.log(f"  [{idx + 1}/{len(files)}] 处理 {src.name[:50]}{mark}")
            JOB.update(done_files=idx + 1)
            if on_progress:
                on_progress(0.1 * (idx + 1) / len(files))

        # 生成拼接列表
        with open(tmp, "w", encoding="utf-8") as f:
            for p in normalized_files:
                s = str(p).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{s}'\n")

        # 章节元数据按分段实测时长累加，时间点才准
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

        cmd = [C.FFMPEG, "-hide_banner", "-y",
               "-fflags", "+genpts",
               "-progress", "pipe:1", "-nostats",
               "-f", "concat", "-safe", "0", "-i", str(tmp)]
        if meta_file:
            cmd += ["-i", str(meta_file)]
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        if keep_subs:
            cmd += ["-map", "0:s?"]
        if meta_file:
            cmd += ["-map_chapters", "1"]
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        if out.suffix.lower() in C.MP4_EXTS:
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
            raise RuntimeError(CANCELED)
        if rc != 0:
            raise RuntimeError(err or f"ffmpeg 退出码 {rc}")
        return {"total": actual_total, "chapters": len(chapters)}
    finally:
        for path in (tmp, meta_file):
            if not path:
                continue
            try:
                Path(path).unlink()
            except Exception:
                pass
        if tmp_segs_dir and tmp_segs_dir.exists():
            shutil.rmtree(tmp_segs_dir, ignore_errors=True)


def encode_one(src: Path, dst: Path, w: int, h: int, fps: str,
               has_audio: bool, crf: int, preset: str, encoder: str,
               duration: float, on_progress, should_cancel,
               target_sr: str = "48000", target_ts: str = None, trim=None,
               crop=None, keep_subs: bool = False):
    """单文件转码，支持多种编码器。

    - 音频采样率跟随全局统一目标，避免与拷贝段不一致
    - 视频 timescale 跟随全局统一目标，避免与 copy 段冲突
    - trim=(head, tail) 时做帧级精确裁剪（转码天然精确）
    - crop=(left, top, right, bottom) 时先裁边框再缩放到目标尺寸
    - keep_subs 时把字幕轨一起带过去（mp4 统一转 mov_text）
    """
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

    cmd = [C.FFMPEG, "-hide_banner", "-y",
           "-progress", "pipe:1", "-nostats"]
    if head and float(head) > 0.05:
        cmd += ["-ss", f"{float(head):.3f}"]
    cmd += ["-i", str(src)]

    if has_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        # 无音轨时补一条静音轨；时长由下面的 -t 控制（不能用 -shortest，
        # 否则字幕轨的事件时长会把它一起截短）
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={target_sr}:cl=stereo",
                "-map", "0:v:0", "-map", "1:a:0"]
    if keep_subs:
        cmd += ["-map", "0:s?", "-c:s",
                "mov_text" if dst.suffix.lower() in C.MP4_EXTS else "copy"]

    cmd += ["-vf", vf] + vargs
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", str(target_sr), "-ac", "2"]
    if target_ts and dst.suffix.lower() in C.MP4_EXTS:
        cmd += ["-video_track_timescale", str(target_ts)]
    # 输出时长：裁剪过的按裁剪后算；补静音轨的按源时长收尾
    limit = eff if eff > 0 and (float(head or 0) > 0.05
                                or float(tail or 0) > 0.05
                                or not has_audio) else 0.0
    if limit:
        cmd += ["-t", f"{limit:.3f}"]
    cmd += ["-movflags", "+faststart", str(dst)]

    rc, err, canceled = run_ffmpeg(cmd, total_sec=(eff or duration),
                                   on_progress=on_progress,
                                   should_cancel=should_cancel)
    if canceled:
        raise RuntimeError(CANCELED)
    if rc != 0:
        raise RuntimeError(err or f"ffmpeg 退出码 {rc}")


def finish_success(out: Path, fallback_duration: float,
                   encoded_count: int, copied_count: int,
                   source_duration: float = 0.0,
                   expected_duration: float = 0.0,
                   chapter_count: int = 0,
                   expected_frames: int = 0,
                   skipped=None):
    """合并后自检：时长 / 音视频同步 / 帧数 / 时间基 / 采样率 / 字幕 逐项核对。"""
    info = probe_output_summary(out)
    final_dur = info.get("duration") or get_media_duration(out) or fallback_duration
    final_size = info.get("size") or (out.stat().st_size if out.exists() else 0)
    v_dur = info.get("video_duration")
    a_dur = info.get("audio_duration")
    sync = abs(v_dur - a_dur) if (v_dur and a_dur) else None
    frames = info.get("frames") or 0

    exp = expected_duration or source_duration
    delta = final_dur - exp if exp > 0 else 0.0
    tol = max(2.0, exp * 0.002)      # 容差：2 秒或 0.2%

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
        "subtitles": info.get("subtitles", 0),
        "level": level,
        "notes": notes,
        "skipped": skipped or [],
    }
    if skipped:
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
    if report["subtitles"]:
        JOB.log(f"   ✅ 字幕轨：{report['subtitles']} 条")
    if chapter_count:
        JOB.log(f"   ✅ 章节：{chapter_count} 个")
    for n in notes:
        JOB.log(f"   ⚠️ {n}")
    JOB.log("   结论：全部指标正常 ✅" if level == "ok"
            else "   结论：存在偏差，请检查上方 ⚠️ 项")
    JOB.log("─" * 46)


# ======================== 并行转码 ========================
class _BatchEncoder:
    """把一组分段按并行度转码：断点复用 + 分段重试 + 失败策略。

    `results` 只包含成功的分段；选了「跳过失败集」时失败项记进 `skipped`，
    作业层再按原始顺序组装，缺哪段就少哪段。
    """

    def __init__(self, *, files, files_info, durations, eff_durations,
                 width, height, fps, encoder, crf, preset, target_sr,
                 target_ts, trim_head, trim_tail, crop, enc_dir, reuse,
                 parallel, on_error, progress_total, keep_subs):
        self.files = files
        self.files_info = files_info
        self.durations = durations
        self.eff_durations = eff_durations
        self.width, self.height, self.fps = width, height, fps
        self.encoder, self.crf, self.preset = encoder, crf, preset
        self.target_sr, self.target_ts = target_sr, target_ts
        self.trim_head, self.trim_tail = trim_head, trim_tail
        self.crop, self.keep_subs = crop, keep_subs
        self.enc_dir = enc_dir
        self.reuse = dict(reuse or {})
        self.parallel = max(1, int(parallel or 1))
        self.on_error = on_error
        self.progress_total = float(progress_total or 1.0)

        self.results: dict[int, Path] = {}
        self.skipped: list[dict] = []
        self._lock = threading.RLock()
        self._frac: dict[int, float] = {}
        self._seq = 0
        self._count = 0

    def run(self, indices) -> dict:
        indices = list(indices)
        self._count = len(indices)
        with self._lock:
            self._frac = {i: 0.0 for i in indices}

        reused = [i for i in indices if i in self.reuse]
        for i in reused:
            self.results[i] = self.reuse[i]
            self._frac[i] = self.eff_durations[i]
        if reused:
            JOB.log(f"♻️ 断点续跑：复用 {len(reused)} 个已完成分段"
                    f"（共 {self._count} 个）")
        self._emit()

        pending = [i for i in indices if i not in self.results]
        if pending:
            with ThreadPoolExecutor(max_workers=self.parallel) as ex:
                futures = {ex.submit(self._one, i): i for i in pending}
                for future in as_completed(futures):
                    idx, dst, err = future.result()
                    if JOB.cancel:
                        raise RuntimeError(CANCELED)
                    if err is None:
                        self.results[idx] = dst
                        continue
                    if err == CANCELED:
                        raise RuntimeError(CANCELED)
                    if self.on_error == "skip":
                        self.skipped.append({"name": self.files[idx].name,
                                             "reason": str(err)[:300]})
                        JOB.log(f"⚠️ 跳过失败分段：{self.files[idx].name[:40]}")
                    else:
                        raise RuntimeError(f"转码失败 {self.files[idx].name}: {err}")
        return self.results

    def _one(self, idx):
        f = self.files[idx]
        if JOB.cancel:
            return idx, None, CANCELED
        with self._lock:
            self._seq += 1
            seq = self._seq
        JOB.update(current_file=f.name, done_files=seq - 1,
                   message=f"转码 {seq}/{self._count}")
        JOB.log(f"[转码 {seq}/{self._count}] {f.name}")

        dst = self.enc_dir / f"{idx:05d}.mp4"
        attempt = 0
        while True:
            try:
                encode_one(f, dst, self.width, self.height, self.fps,
                           bool(self.files_info[idx].get("acodec")),
                           self.crf, self.preset, self.encoder,
                           self.durations[idx], self._file_cb(idx),
                           lambda: JOB.cancel, target_sr=self.target_sr,
                           target_ts=self.target_ts,
                           trim=(self.trim_head, self.trim_tail),
                           crop=self.crop, keep_subs=self.keep_subs)
                with self._lock:
                    self._frac[idx] = self.eff_durations[idx]
                self._emit()
                return idx, dst, None
            except Exception as e:
                msg = str(e)
                if msg == CANCELED:
                    return idx, None, msg
                # 底层自动重试：只重跑失败的分段，不改变输出内容
                if attempt < SEGMENT_RETRIES and _retryable(msg):
                    attempt += 1
                    JOB.log(f"  ↻ 自动重试 {attempt}/{SEGMENT_RETRIES}："
                            f"{f.name[:34]}")
                    time.sleep(1.0)
                    continue
                return idx, None, msg

    def _file_cb(self, idx):
        def cb(frac):
            with self._lock:
                frac = max(0.0, min(1.0, float(frac or 0)))
                self._frac[idx] = frac * self.eff_durations[idx]
            self._emit()
        return cb

    def _emit(self):
        with self._lock:
            done = sum(self._frac.values())
        JOB.update(progress=0.9 * min(done / self.progress_total, 1.0))


# ======================== 主流程 ========================
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

        JOB.update(state="scanning", message="正在扫描与探测…")

        # 扫描时把自己产出的历史文件排除掉（否则第二次跑会把成品当素材）；
        # 扫描完再定最终输出名，因为覆盖保护可能会改名。
        sc = scan_folder(folder, recursive, exclude_paths=output_family(out))
        files_info_map = {item["path"]: item for item in sc["files"]}
        if custom_order and isinstance(custom_order, list):
            files_info = [files_info_map[p] for p in custom_order
                          if p in files_info_map]
        else:
            files_info = sc["files"]
        if not files_info:
            raise RuntimeError("文件夹里没有可用的视频文件")

        out, out_note = guard_output(
            out, [i["path"] for i in files_info],
            overwrite=C.overwrite_allowed(params))
        JOB.update(output=str(out), output_note=out_note)
        if out_note:
            JOB.log("🛡 " + out_note)

        files = [Path(i["path"]) for i in files_info]
        durations = [max(float(i.get("duration") or 0), 0) for i in files_info]

        trim_head = parse_trim_seconds(params.get("trim_head", 0))
        trim_tail = parse_trim_seconds(params.get("trim_tail", 0))
        want_chapters = bool(params.get("chapters", True))

        # 失败处理由用户选择：abort = 中止整批（默认） / skip = 跳过失败集继续
        on_error = str(params.get("on_error") or "abort").lower()
        if on_error not in ("abort", "skip"):
            on_error = "abort"
        resume = bool(params.get("_resume"))
        skipped_segments = []

        # 画面边框裁剪（去台标 / 去黑边）
        crop_cfg = params.get("crop") or {}
        crop_on = bool(crop_cfg.get("enabled"))
        crop_l = max(0, int(crop_cfg.get("left", 0) or 0))
        crop_t = max(0, int(crop_cfg.get("top", 0) or 0))
        crop_r = max(0, int(crop_cfg.get("right", 0) or 0))
        crop_b = max(0, int(crop_cfg.get("bottom", 0) or 0))
        crop_scale_back = bool(crop_cfg.get("scale_back"))

        # 编码器不可用就直接报人话，不要跑到一半才失败
        if mode != "copy":
            if not encoders.ensure(encoder).get("ok"):
                raise RuntimeError(encoders.unavailable_msg(encoder))
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

        raw_duration = sum(durations)
        total_sec = sum(eff_durations)
        source_duration = total_sec
        expected_duration = total_sec
        JOB.update(source_duration=source_duration)

        if trim_head > 0.05 or trim_tail > 0.05:
            saved = raw_duration - total_sec
            JOB.log(f"✂️ 裁剪：每集片头 -{trim_head:g}s / 片尾 -{trim_tail:g}s")
            JOB.log(f"   原始总时长 {raw_duration/3600:.2f}h → 裁剪后 "
                    f"{total_sec/3600:.2f}h（共省 {saved/60:.1f} 分钟）")

        # 字幕：所有文件一致才保留（concat 要求每段字幕流一致）
        sub_plan = subtitles.plan(
            [i.get("subtitle_codecs") or [] for i in files_info], out.suffix)
        if sub_plan.get("note"):
            JOB.log("🔤 " + sub_plan["note"])

        chapter_titles = ([Path(i["path"]).stem for i in files_info]
                          if want_chapters else None)

        el_count = sum(1 for i in files_info if i.get("has_edit_list"))
        if el_count > 0:
            JOB.log(f"ℹ️ 检测到 {el_count} 个文件包含 Edit List，将自动清除")

        # 单文件直接重封装（启用裁剪时必须走转码）
        if len(files) == 1 and not crop_on:
            JOB.update(state="copying", message="仅一个文件，直接重封装清除 Edit List…")
            res = do_copy(files, out, total_sec, trims=trims,
                          chapter_titles=chapter_titles,
                          keep_subs=sub_plan["keep"]) or {}
            finish_success(out, total_sec, 0, 1, source_duration,
                           expected_duration=res.get("total") or expected_duration,
                           chapter_count=res.get("chapters", 0))
            return

        # 按参数分组，多数派可以无损拷贝
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
        majority_item = files_info[majority_indices[0]]

        # 目标尺寸 / 帧率
        if size_arg:
            w, h = [int(x) for x in size_arg.lower().split("x")]
        else:
            w = int(majority_item.get("width") or 1920)
            h = int(majority_item.get("height") or 1080)
        w -= w % 2
        h -= h % 2

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

        # 统一音频采样率（时长错乱的真正根因）
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

        # 统一视频 time_base（时长爆炸的另一个根因）
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

        # 纯拷贝模式才能直接比对帧数（转码/裁剪都会改变帧数）
        expected_frames = 0
        if trim_head < 0.05 and trim_tail < 0.05 and not crop_on:
            expected_frames = sum(int(i.get("nb_frames") or 0) for i in files_info)

        # 断点续跑：只对需要转码的分段保留中间结果
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
                        ok_resume = (json.loads(
                            state_file.read_text(encoding="utf-8")
                        ).get("hash") == p_hash)
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

        def _finish(enc_count, cp_count, kept_total, res, kept_frames=None):
            res = res or {}
            finish_success(
                out, kept_total, enc_count, cp_count, kept_total,
                expected_duration=res.get("total") or kept_total,
                chapter_count=res.get("chapters", 0),
                expected_frames=(expected_frames if kept_frames is None
                                 else kept_frames),
                skipped=skipped_segments)
            if work:
                shutil.rmtree(work, ignore_errors=True)

        # ============ 模式 1：无损直拷 ============
        if actual_mode == "copy":
            JOB.update(state="copying", message=f"无损直拷 · {len(files)} 个文件",
                       total_files=len(files))
            JOB.log(f"模式：无损直拷 · {len(files)} 个文件"
                    f"（统一采样率 + 时间基 + 裁剪）")
            try:
                res = do_copy(files, out, total_sec, target_sr=target_sr,
                              target_ts=target_ts, trims=trims,
                              chapter_titles=chapter_titles,
                              keep_subs=sub_plan["keep"])
                _finish(0, len(files), total_sec, res, expected_frames)
                return
            except RuntimeError as e:
                if str(e) == CANCELED:
                    raise
                if mode == "copy":
                    raise
                JOB.log(f"⚠️ 无损直拷失败：{e}，切换到智能模式")
                actual_mode = "smart"

        # ============ 模式 2/3：转码（智能只转少数派，全部转码则全转）============
        if actual_mode in ("smart", "encode"):
            encode_set = set(need_encode_indices if actual_mode == "smart"
                             else range(len(files)))
            need = sorted(encode_set)
            copy_count = len(files) - len(need)
            progress_total = (total_sec if actual_mode == "encode"
                              else sum(eff_durations[i] for i in need)) or 1.0

            if actual_mode == "smart":
                JOB.update(state="encoding", total_files=len(need),
                           message=f"智能模式 · 转码 {len(need)} 个 / "
                                   f"拷贝 {copy_count} 个 · {parallel} 路并行")
                JOB.log(f"模式：智能 · 多数派 {copy_count} 个无损拷贝，"
                        f"少数派 {len(need)} 个转码至 {w}x{h} @ {fps} · "
                        f"{parallel} 路并行")
            else:
                JOB.update(state="encoding", total_files=len(need),
                           message=f"全部转码至 {w}x{h} @ {fps} · {parallel} 路并行")
                JOB.log(f"模式：全部转码 · {len(need)} 个 → {w}x{h} @ {fps} · "
                        f"{parallel} 路并行")

            encoder_job = _BatchEncoder(
                files=files, files_info=files_info, durations=durations,
                eff_durations=eff_durations, width=w, height=h, fps=fps,
                encoder=encoder, crf=crf, preset=preset, target_sr=target_sr,
                target_ts=target_ts, trim_head=trim_head, trim_tail=trim_tail,
                crop=crop_rect, enc_dir=enc_dir, reuse=reuse,
                parallel=parallel, on_error=on_error,
                progress_total=progress_total, keep_subs=sub_plan["keep"])
            results = encoder_job.run(need)
            skipped_segments.extend(encoder_job.skipped)
            if JOB.cancel:
                raise RuntimeError(CANCELED)

            # 按原始顺序组装分段（失败且选择跳过的不参与合并）
            segs, seg_trims, kept_titles, kept_idx = [], [], [], []
            for i in range(len(files)):
                if i in encode_set:
                    if i not in results:
                        continue
                    # 已转码的分段裁剪已在转码时完成，不能再剪一次
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
                          keep_subs=sub_plan["keep"],
                          on_progress=lambda f: JOB.update(progress=0.9 + 0.1 * f))
            _finish(len(results), copy_count, kept_total, res,
                    0 if skipped_segments else expected_frames)
            return

        raise RuntimeError(f"未知模式：{actual_mode}")
    except Exception as e:
        if str(e) == CANCELED:
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
