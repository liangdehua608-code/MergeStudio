"""按章节 / 固定时长切割视频。

切割范围与是否精确切割都由用户决定：
- 复制模式（默认）：切点对齐关键帧，速度快、无画质损失
- 精确模式：重编码，切点准确但慢
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import config as C
from .. import subtitles
from ..media import guard_output, run_ffmpeg
from ..task import JOB

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(text: str, limit: int = 40) -> str:
    text = _BAD_CHARS.sub("", text or "").strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    return text[:limit].strip()


def split_run(params: dict):
    JOB.reset()
    try:
        target = str(params.get("target") or "").strip()
        if not target:
            raise RuntimeError("请先选择要切割的视频文件")
        if not C.FFMPEG:
            raise RuntimeError("尚未设置 ffmpeg 路径")
        src = Path(target).expanduser()
        if not src.is_file():
            raise RuntimeError(f"文件不存在：{src}")

        segments = params.get("segments") or []
        if not isinstance(segments, list) or not segments:
            raise RuntimeError("请先生成切割方案并勾选要导出的片段")

        precise = bool(params.get("precise"))
        out_arg = str(params.get("output") or "").strip()
        out_dir = Path(out_arg).expanduser() if out_arg else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        preset = str(params.get("preset") or "veryfast")
        crf = int(params.get("crf", 20) or 20)
        encoder = str(params.get("encoder") or "libx264")

        total = len(segments)
        JOB.update(state="copying" if not precise else "encoding",
                   total_files=total, done_files=0,
                   message=f"切割 {total} 段 · {'精确（重编码）' if precise else '复制（关键帧对齐）'}")
        JOB.log(f"✂️ 切割：{src.name}")
        JOB.log(f"   共 {total} 段 · "
                f"{'精确切割（重编码）' if precise else '流复制（切点对齐关键帧）'}")
        JOB.log(f"   输出目录：{out_dir}")

        # 字幕：单文件切割只要有一条文本字幕就能带过去
        subs = subtitles.plan([subtitles.probe(src)], ".mp4")
        if not subs["keep"] and subs["note"]:
            JOB.log("🔤 " + subs["note"])
        subs_args = subtitles.map_args(subs)
        overwrite = C.overwrite_allowed(params)

        done = 0
        ok = 0
        total_out = 0.0
        total_size = 0
        produced = []
        failed = []
        first_out = ""

        for idx, seg in enumerate(segments):
            if JOB.cancel:
                raise RuntimeError("已取消")
            try:
                start = max(0.0, float(seg.get("start") or 0))
                end = float(seg.get("end") or 0)
            except Exception:
                continue
            dur = end - start
            if dur <= 0.2:
                JOB.log(f"  ⚠️ 跳过第 {idx + 1} 段：时长过短")
                done += 1
                continue

            title = _safe_name(str(seg.get("title") or ""))
            name = f"{src.stem}_P{idx + 1:02d}"
            if title:
                name += f"_{title}"
            dst, note = guard_output(out_dir / (name + ".mp4"),
                                     sources=[src], overwrite=overwrite)
            if note:
                JOB.log(f"  🛡 {note}")

            JOB.update(current_file=dst.name,
                       message=f"切割 {idx + 1}/{total}",
                       done_files=done)

            cmd = [C.FFMPEG, "-hide_banner", "-y",
                   "-progress", "pipe:1", "-nostats",
                   "-ss", f"{start:.3f}", "-i", str(src),
                   "-t", f"{dur:.3f}",
                   "-map", "0:v:0", "-map", "0:a:0?"]
            if precise:
                cmd += ["-c:v", encoder, "-preset", preset, "-crf", str(crf),
                        "-c:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
            cmd += subs_args
            cmd += ["-movflags", "+faststart", str(dst)]

            def _cb(frac, i=idx):
                JOB.update(progress=(i + max(0.0, min(1.0, frac))) / total)

            rc, err, canceled = run_ffmpeg(cmd, total_sec=dur, on_progress=_cb,
                                           should_cancel=lambda: JOB.cancel)
            if canceled:
                raise RuntimeError("已取消")

            if rc != 0 or not dst.exists() or dst.stat().st_size == 0:
                failed.append({"name": dst.name, "reason": (err or "")[:200]})
                JOB.log(f"  ❌ 第 {idx + 1} 段失败")
            else:
                ok += 1
                total_out += dur
                total_size += dst.stat().st_size
                if not first_out:
                    first_out = str(dst)
                produced.append({"name": dst.name, "start": start,
                                 "end": end, "size": dst.stat().st_size})
                JOB.log(f"  ✅ {dst.name}")
            done += 1
            JOB.update(done_files=done, progress=done / total)

        JOB.update(state="done", progress=1.0,
                   message=f"切割完成 · {ok}/{total} 段",
                   output=first_out or str(out_dir),
                   final_size=total_size,
                   final_duration=total_out,
                   encoded_count=ok, copied_count=0,
                   report={"level": "ok" if ok else "warn",
                           "count": ok, "total": total,
                           "precise": precise,
                           "duration": total_out,
                           "size": total_size,
                           "segments": produced,
                           "failed": failed})
        JOB.log("─" * 46)
        JOB.log(f"✅ 完成：{ok} 段 · 合计 {total_out / 60:.1f} 分钟")
        if failed:
            JOB.log(f"⚠️ {len(failed)} 段失败（见任务详情）")

    except Exception as e:
        if str(e) == "已取消":
            JOB.update(state="canceled", message="已取消", error="用户取消")
            JOB.log("任务已取消")
        else:
            JOB.update(state="error", message=str(e), error=str(e))
            JOB.log("ERROR: " + str(e))
