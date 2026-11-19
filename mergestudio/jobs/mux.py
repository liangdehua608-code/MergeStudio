"""音视频合并：把一段音频配到视频上（替换原音轨，或与原声混合）。"""
from __future__ import annotations

from pathlib import Path

from .. import config as C
from ..media import probe_full, probe_media, run_ffmpeg
from ..task import JOB


def _resolve_output(video: Path, out_arg: str) -> Path:
    default = video.stem + "_muxed.mp4"
    out_arg = (out_arg or "").strip()
    if not out_arg:
        return video.parent / default
    cand = Path(out_arg).expanduser()
    if cand.suffix.lower() in C.VIDEO_EXTS:
        return cand
    cand.mkdir(parents=True, exist_ok=True)
    return cand / default


def mux_run(params: dict):
    JOB.reset()
    try:
        v_raw = str(params.get("video") or "").strip()
        a_raw = str(params.get("audio") or "").strip()
        if not v_raw or not a_raw:
            raise RuntimeError("请同时选择视频文件和音频文件")
        if not C.FFMPEG or not C.FFPROBE:
            raise RuntimeError("尚未设置 ffmpeg/ffprobe 路径")

        video = Path(v_raw).expanduser()
        audio = Path(a_raw).expanduser()
        if not video.is_file():
            raise RuntimeError(f"视频文件不存在：{video}")
        if not audio.is_file():
            raise RuntimeError(f"音频文件不存在：{audio}")

        v_info = probe_full(video) or {}
        a_info = probe_media(audio) or {}
        v_dur = float(v_info.get("duration") or 0)
        a_dur = float(a_info.get("duration") or 0)
        if not v_info.get("vcodec"):
            raise RuntimeError("所选文件里没有视频轨")
        if not a_info.get("acodec"):
            raise RuntimeError("所选文件里没有音频轨")

        mode = str(params.get("mode") or "replace").lower()
        if mode not in ("replace", "mix"):
            mode = "replace"
        if mode == "mix" and not v_info.get("acodec"):
            JOB.log("ℹ️ 原视频没有音轨，混合模式自动改为替换")
            mode = "replace"
        fit = str(params.get("fit") or "shortest").lower()
        if fit not in ("shortest", "video"):
            fit = "shortest"
        try:
            volume = max(0, min(200, int(params.get("volume", 100))))
        except Exception:
            volume = 100

        out = _resolve_output(video, str(params.get("output") or ""))
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"无法创建输出目录：{e}")
        if out.resolve() == video.resolve():
            raise RuntimeError("输出文件不能与源视频相同，请换一个输出目录")

        target_dur = v_dur if fit == "video" else min(v_dur or a_dur, a_dur or v_dur)

        JOB.update(output=str(out), total_files=1, done_files=0,
                   state="copying", source_duration=target_dur,
                   message="正在合并音视频…")
        JOB.log(f"🎬 视频：{video.name}（{v_dur:.1f}s）")
        JOB.log(f"🎧 音频：{audio.name}（{a_dur:.1f}s）")
        JOB.log(f"   模式：{'替换原音轨' if mode == 'replace' else '与原声混合'}"
                + (f"，新音频音量 {volume}%" if mode == "mix" else ""))
        JOB.log(f"   对齐：{'以较短者为准' if fit == 'shortest' else '以视频时长为准'}")

        cmd = [C.FFMPEG, "-hide_banner", "-nostdin", "-y",
               "-progress", "pipe:1", "-nostats",
               "-i", str(video), "-i", str(audio)]

        if mode == "mix":
            vol = volume / 100.0
            graph = (f"[1:a]volume={vol:.2f}[a1];"
                     f"[0:a][a1]amix=inputs=2:duration=first:"
                     f"dropout_transition=2[aout]")
            cmd += ["-filter_complex", graph,
                    "-map", "0:v:0", "-map", "[aout]"]
        else:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]

        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]

        if fit == "video":
            if mode == "replace" and a_dur and v_dur and a_dur < v_dur - 0.2:
                cmd += ["-af", "apad"]
            cmd += ["-t", f"{v_dur:.3f}"]
        else:
            cmd += ["-shortest"]
        cmd += ["-movflags", "+faststart", str(out)]

        def _cb(frac):
            JOB.update(progress=max(0.0, min(0.99, frac)),
                       message=f"合并中 {int(frac * 100)}%")

        rc, err, canceled = run_ffmpeg(cmd, total_sec=target_dur,
                                       on_progress=_cb,
                                       should_cancel=lambda: JOB.cancel)
        if canceled:
            raise RuntimeError("已取消")
        if rc != 0 or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("音视频合并失败：" + (err or f"ffmpeg 退出码 {rc}"))

        new_info = probe_full(out) or {}
        final_dur = float(new_info.get("duration") or target_dur)
        report = {"duration": final_dur,
                  "video_duration": new_info.get("video_duration"),
                  "audio_duration": new_info.get("audio_duration"),
                  "source_duration": target_dur,
                  "expected_duration": target_dur,
                  "delta": final_dur - target_dur,
                  "mode": mode, "fit": fit, "volume": volume,
                  "level": "ok", "notes": []}
        if abs(final_dur - target_dur) > max(1.0, target_dur * 0.003):
            report["level"] = "warn"
            report["notes"].append(
                f"输出时长 {final_dur:.2f}s 与目标 {target_dur:.2f}s 相差较多")

        JOB.update(state="done", progress=1.0, message="音视频合并完成",
                   final_duration=final_dur, final_size=out.stat().st_size,
                   encoded_count=0, copied_count=1,
                   duration_delta=report["delta"], report=report)
        JOB.log("─" * 46)
        JOB.log(f"✅ 输出：{out}")
        JOB.log(f"   时长 {final_dur:.2f}s · {out.stat().st_size / 1048576:.1f}MB")

    except Exception as e:
        if str(e) == "已取消":
            JOB.update(state="canceled", message="已取消", error="用户取消")
            JOB.log("任务已取消")
        else:
            JOB.update(state="error", message=str(e), error=str(e))
            JOB.log("ERROR: " + str(e))
