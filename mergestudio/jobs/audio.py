"""提取音频任务：把网课等视频里的音轨批量导出为 m4a / mp3。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config as C
from ..media import list_video_files, probe_full, run_ffmpeg
from ..task import JOB

AUDIO_FORMATS = {"m4a": "m4a", "mp3": "mp3"}


def _target_path(src: Path, out_dir, ext: str) -> Path:
    name = src.stem + "." + ext
    return (Path(out_dir) / name) if out_dir else (src.parent / name)


def audio_run(params: dict):
    JOB.reset()
    try:
        folder = str(params.get("folder", "")).strip()
        single = str(params.get("target", "")).strip()
        if not folder and not single:
            raise ValueError("请先选择视频文件夹或单个视频")
        if not C.FFMPEG:
            raise RuntimeError("尚未设置 ffmpeg 路径")

        recursive = bool(params.get("recursive"))
        out_arg = str(params.get("output") or "").strip()
        fmt = str(params.get("audio_format") or "m4a").lower()
        if fmt not in AUDIO_FORMATS:
            fmt = "m4a"
        quality = int(params.get("audio_quality", 2) or 2)

        out_dir = Path(out_arg).expanduser() if out_arg else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        JOB.update(state="scanning", message="正在准备文件…")
        if single:
            f = Path(single).expanduser()
            if not f.is_file():
                raise RuntimeError(f"文件不存在：{f}")
            files = [f]
            JOB.log("🎧 单文件模式")
        else:
            _, files = list_video_files(folder, recursive)
            if not files:
                raise RuntimeError("文件夹里没有可用的视频文件")
        JOB.update(total_files=len(files), done_files=0)
        JOB.log(f"🎧 目标格式：{fmt.upper()} · 共 {len(files)} 个文件")
        if out_dir:
            JOB.log(f"   输出目录：{out_dir}")

        with ThreadPoolExecutor(max_workers=8) as ex:
            infos = list(ex.map(probe_full, files))

        total = len(files)
        done = ok = 0
        skipped = []
        total_dur = 0.0
        out_size = 0
        first_out = ""

        for idx, (src, info) in enumerate(zip(files, infos)):
            if JOB.cancel:
                raise RuntimeError("已取消")
            if not info or not info.get("acodec"):
                skipped.append({"name": src.name, "reason": "没有音频轨"})
                JOB.log(f"  ⚠️ 跳过 {src.name[:46]}（无音频轨）")
                done += 1
                JOB.update(done_files=done, progress=done / total)
                continue

            dur = float(info.get("duration") or 0)
            total_dur += dur
            dst = _target_path(src, out_dir, fmt)
            JOB.update(state="encoding", current_file=src.name,
                       message=f"提取音频 {done + 1}/{total}")
            JOB.log(f"  [{done + 1}/{total}] {src.name[:46]}")

            cmd = [C.FFMPEG, "-hide_banner", "-nostdin", "-y",
                   "-i", str(src), "-vn"]
            if fmt == "mp3":
                cmd += ["-c:a", "libmp3lame", "-q:a", str(max(0, min(9, quality)))]
            else:
                cmd += ["-c:a", "copy"]
            cmd += ["-map_metadata", "0", str(dst)]

            def _cb(frac, _idx=idx):
                JOB.update(progress=(_idx + max(0.0, min(1.0, frac))) / total)

            rc, err, canceled = run_ffmpeg(cmd, total_sec=dur or 0,
                                           on_progress=_cb,
                                           should_cancel=lambda: JOB.cancel)
            if canceled:
                raise RuntimeError("已取消")

            if rc != 0 or not dst.exists() or dst.stat().st_size == 0:
                skipped.append({"name": src.name,
                                "reason": (err or "ffmpeg 退出码 %s" % rc)[:200]})
                JOB.log(f"  ❌ 失败：{src.name[:40]}")
                JOB.update(error=(err or "")[:300])
            else:
                ok += 1
                out_size += dst.stat().st_size
                if not first_out:
                    first_out = str(dst)
            done += 1
            JOB.update(done_files=done, progress=done / total)

        JOB.update(state="done", progress=1.0,
                   message=f"提取完成 · {ok}/{total} 个",
                   output=first_out or str(out_dir or (files[0].parent if files else folder)),
                   final_size=out_size,
                   final_duration=total_dur,
                   encoded_count=ok, copied_count=0,
                   report={"level": "ok" if ok else "warn",
                           "count": ok, "total": total,
                           "format": fmt.upper(),
                           "skipped": skipped[:50],
                           "duration": total_dur,
                           "size": out_size})
        JOB.log("─" * 46)
        JOB.log(f"✅ 完成：{ok} 个 {fmt.upper()} · 合计 {total_dur/3600:.2f} 小时")
        if skipped:
            JOB.log(f"⚠️ 跳过 {len(skipped)} 个（详见任务详情）")

    except Exception as e:
        if str(e) == "已取消":
            JOB.update(state="canceled", message="已取消", error="用户取消")
            JOB.log("任务已取消")
        else:
            JOB.update(state="error", message=str(e), error=str(e))
            JOB.log("ERROR: " + str(e))
