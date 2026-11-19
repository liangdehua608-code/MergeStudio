"""单个视频处理：画面裁剪（去水印/黑边）+ 剪片头片尾，输出为新文件。"""
from __future__ import annotations

from pathlib import Path

from .. import config as C
from .. import encoders, subtitles
from ..media import guard_output, probe_full, trim_span
from ..task import JOB
from .merge import encode_one


def _resolve_output(src: Path, out_arg: str, crop_on: bool) -> Path:
    suffix = "_cropped" if crop_on else "_edited"
    default_name = src.stem + suffix + ".mp4"
    out_arg = (out_arg or "").strip()
    if not out_arg:
        return src.parent / default_name
    cand = Path(out_arg).expanduser()
    if cand.suffix.lower() in C.VIDEO_EXTS:
        return cand
    cand.mkdir(parents=True, exist_ok=True)
    return cand / default_name


def video_run(params: dict):
    JOB.reset()
    try:
        raw = str(params.get("target") or "").strip()
        if not raw:
            raise RuntimeError("请先选择要处理的视频文件")
        if not C.FFMPEG or not C.FFPROBE:
            raise RuntimeError("尚未设置 ffmpeg/ffprobe 路径")
        src = Path(raw).expanduser()
        if not src.is_file():
            raise RuntimeError(f"文件不存在：{src}")

        info = probe_full(src) or {}
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        duration = float(info.get("duration") or 0)
        if not width or not height or duration <= 0:
            raise RuntimeError("无法读取视频信息（可能不是有效视频）")

        crop_cfg = params.get("crop") or {}
        crop_on = bool(crop_cfg.get("enabled"))
        left = max(0, int(crop_cfg.get("left", 0) or 0))
        top = max(0, int(crop_cfg.get("top", 0) or 0))
        right = max(0, int(crop_cfg.get("right", 0) or 0))
        bottom = max(0, int(crop_cfg.get("bottom", 0) or 0))
        scale_back = bool(crop_cfg.get("scale_back"))
        if crop_on and not any((left, top, right, bottom)):
            crop_on = False

        cw = ((width - left - right) // 2) * 2
        ch = ((height - top - bottom) // 2) * 2
        if crop_on and (cw < 64 or ch < 64):
            raise RuntimeError(f"裁剪范围过大：{width}x{height} 只剩 {cw}x{ch}")

        head = max(0.0, float(params.get("trim_head") or 0))
        tail = max(0.0, float(params.get("trim_tail") or 0))
        eff_dur = trim_span(duration, head, tail)
        if eff_dur <= 0.3:
            raise RuntimeError("片头片尾裁剪过度，已经没有内容了")

        encoder = str(params.get("encoder") or "libx264")
        crf = int(params.get("crf", 20) or 20)
        preset = str(params.get("preset") or "veryfast")

        st = encoders.ensure(encoder)
        if not st.get("ok"):
            raise RuntimeError(f"编码器 {encoder} 不可用：{st.get('reason')}。"
                               f"请改用 libx264（CPU）。")

        out = _resolve_output(src, str(params.get("output") or ""), crop_on)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"无法创建输出目录：{e}")
        # 覆盖保护：既不让输出撞上源文件，也不悄悄覆盖旧结果
        out, out_note = guard_output(out, [src],
                                     overwrite=C.overwrite_allowed(params))
        if out_note:
            JOB.log("🛡 " + out_note)

        subs = subtitles.plan([info.get("subtitle_codecs") or []], out.suffix)
        if subs.get("note"):
            JOB.log("🔤 " + subs["note"])

        target_w = width if (not crop_on or scale_back) else cw
        target_h = height if (not crop_on or scale_back) else ch
        fps = str(info.get("fps") or "30/1")

        JOB.update(output=str(out), output_note=out_note, total_files=1,
                   done_files=0, source_duration=eff_dur,
                   state="encoding", message="正在处理…")
        JOB.log(f"🎬 {src.name}")
        JOB.log(f"   源 {width}x{height} · {duration:.1f}s · {fps}")
        if crop_on:
            JOB.log(f"✂️ 裁剪 左{left} 上{top} 右{right} 下{bottom} px → "
                    f"{target_w}x{target_h}"
                    f"{'（缩放回原尺寸）' if scale_back else ''}")
        if head > 0.05 or tail > 0.05:
            JOB.log(f"✂️ 片头 -{head:g}s / 片尾 -{tail:g}s → {eff_dur:.1f}s")

        def _cb(frac):
            JOB.update(progress=max(0.0, min(1.0, frac)),
                       message=f"处理中 {int(frac * 100)}%")

        encode_one(src, out, target_w, target_h, fps,
                   bool(info.get("acodec")), crf, preset, encoder,
                   duration, _cb, lambda: JOB.cancel,
                   target_sr=str(info.get("sample_rate") or "48000"),
                   trim=(head, tail),
                   crop=(left, top, right, bottom) if crop_on else None,
                   keep_subs=subs["keep"])

        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("处理结束但没有生成输出文件")

        new_info = probe_full(out) or {}
        final_dur = float(new_info.get("duration") or eff_dur)
        delta = final_dur - eff_dur
        level = "ok" if abs(delta) <= max(1.0, eff_dur * 0.002) else "warn"
        report = {"duration": final_dur,
                  "video_duration": new_info.get("video_duration"),
                  "audio_duration": new_info.get("audio_duration"),
                  "expected_duration": eff_dur,
                  "delta": delta,
                  "width": new_info.get("width"),
                  "height": new_info.get("height"),
                  "crop": {"left": left, "top": top, "right": right,
                           "bottom": bottom, "scale_back": scale_back},
                  "subtitles": len(new_info.get("subtitles") or []),
                  "level": level,
                  "notes": [] if level == "ok" else [f"时长偏差 {delta:+.2f}s"]}

        JOB.update(state="done", progress=1.0,
                   message="处理完成", final_duration=final_dur,
                   final_size=out.stat().st_size,
                   encoded_count=1, copied_count=0,
                   duration_delta=delta, report=report)
        JOB.log("─" * 46)
        JOB.log(f"✅ 输出：{out}")
        JOB.log(f"   {new_info.get('width')}x{new_info.get('height')} · "
                f"{final_dur:.2f}s · {out.stat().st_size / 1048576:.1f}MB")

    except Exception as e:
        if str(e) == "已取消":
            JOB.update(state="canceled", message="已取消", error="用户取消")
            JOB.log("任务已取消")
        else:
            JOB.update(state="error", message=str(e), error=str(e))
            JOB.log("ERROR: " + str(e))
