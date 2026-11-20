"""视频裁剪：剪片头片尾 + 画面裁剪（去水印/黑边），输出新文件，不动源文件。

两种入口共用同一套裁剪/编码逻辑：
- `target`：只处理一个视频（原来的行为）
- `folder`：批量处理整个文件夹，每个视频各自输出一个文件，**不合并**
  （各集不是同一类型，拼在一起没意义；这里只是把重复的点选变成一次排队）
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .. import config as C
from .. import encoders, subtitles
from ..media import (fmt_size, guard_output, probe_full, scan_folder,
                     trim_span)
from ..task import JOB
from .merge import CANCELED, encode_one

_GEN_SUFFIX = re.compile(r"_(cropped|edited)(_\d+)?$")


def _fail(e: Exception) -> None:
    if str(e) == CANCELED:
        JOB.update(state="canceled", message="已取消", error="用户取消")
        JOB.log("任务已取消")
    else:
        JOB.update(state="error", message=str(e), error=str(e))
        JOB.log("ERROR: " + str(e))


def _crop_params(params: dict):
    cfg = params.get("crop") or {}
    left = max(0, int(cfg.get("left", 0) or 0))
    top = max(0, int(cfg.get("top", 0) or 0))
    right = max(0, int(cfg.get("right", 0) or 0))
    bottom = max(0, int(cfg.get("bottom", 0) or 0))
    crop_on = bool(cfg.get("enabled")) and any((left, top, right, bottom))
    return crop_on, left, top, right, bottom, bool(cfg.get("scale_back"))


def _trim_params(params: dict):
    head = max(0.0, float(params.get("trim_head") or 0))
    tail = max(0.0, float(params.get("trim_tail") or 0))
    return head, tail


def _dims(width: int, height: int, crop_on: bool, box, scale_back: bool,
          name: str = "视频"):
    """按裁剪设置算输出尺寸；裁剪过大时直接报错（别等 ffmpeg 失败）。"""
    left, top, right, bottom = box
    if not crop_on:
        return width, height
    cw = ((width - left - right) // 2) * 2
    ch = ((height - top - bottom) // 2) * 2
    if width <= 0 or height <= 0 or cw < 64 or ch < 64:
        raise RuntimeError(
            f"{name} 分辨率 {width}x{height} 裁掉 左{left}/上{top}/右{right}/下{bottom}"
            f" 后只剩 {cw}x{ch}，裁剪范围过大")
    return (width, height) if scale_back else (cw, ch)


def _encoder_params(params: dict):
    encoder = str(params.get("encoder") or "libx264")
    crf = int(params.get("crf", 20) or 20)
    preset = str(params.get("preset") or "veryfast")
    if not encoders.ensure(encoder).get("ok"):
        raise RuntimeError(encoders.unavailable_msg(encoder))
    return encoder, crf, preset


def _fps(info: dict) -> str:
    fps = str(info.get("fps") or "30/1")
    if not re.match(r"^\d+/\d+$", fps):
        try:
            fps = f"{int(float(fps))}/1"
        except ValueError:
            fps = "30/1"
    return "30/1" if fps.split("/")[0] == "0" else fps


def _resolve_output(src: Path, out_arg: str, crop_on: bool) -> Path:
    default_name = src.stem + ("_cropped" if crop_on else "_edited") + ".mp4"
    out_arg = (out_arg or "").strip()
    if not out_arg:
        return src.parent / default_name
    cand = Path(out_arg).expanduser()
    # 单文件模式允许直接写成一个确切文件路径
    if cand.suffix.lower() in C.VIDEO_EXTS:
        return cand
    cand.mkdir(parents=True, exist_ok=True)
    return cand / default_name


def _batch_output_dir(out_arg: str) -> Path:
    """批量模式：输出参数一律当目录，保留每个视频自己的文件名。"""
    out_arg = (out_arg or "").strip()
    out = Path(out_arg).expanduser() if out_arg else None
    if out and out.suffix.lower() in C.VIDEO_EXTS:
        out = out.parent
    return out


def _generated_excludes(folder: str, recursive: bool) -> list[Path]:
    """排除本工具上次生成的 `_cropped` / `_edited` 文件。

    只有当同目录里还存在对应的源视频时才排除——避免把用户自己命名成
    `xxx_cropped.mp4` 的正常素材误删出清单。
    """
    try:
        root = Path(folder).expanduser()
        it = root.rglob("*") if recursive else root.iterdir()
        files = [p for p in it if p.is_file() and p.suffix.lower() in C.VIDEO_EXTS]
        sources = {p.stem for p in files if not _GEN_SUFFIX.search(p.stem)}
        return [p for p in files
                if _GEN_SUFFIX.search(p.stem)
                and _GEN_SUFFIX.sub("", p.stem) in sources]
    except Exception:
        return []


# ================================================================ 单个视频
def _run_single(params: dict, raw: str) -> None:
    src = Path(raw).expanduser()
    if not src.is_file():
        raise RuntimeError(f"文件不存在：{src}")

    info = probe_full(src) or {}
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    duration = float(info.get("duration") or 0)
    if not width or not height or duration <= 0:
        raise RuntimeError("无法读取视频信息（可能不是有效视频）")

    crop_on, left, top, right, bottom, scale_back = _crop_params(params)
    head, tail = _trim_params(params)
    eff_dur = trim_span(duration, head, tail)
    if eff_dur <= 0.3:
        raise RuntimeError("片头片尾裁剪过度，已经没有内容了")
    encoder, crf, preset = _encoder_params(params)
    target_w, target_h = _dims(width, height, crop_on,
                               (left, top, right, bottom), scale_back, src.name)

    out = _resolve_output(src, str(params.get("output") or ""), crop_on)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise RuntimeError(f"无法创建输出目录：{e}")
    out, out_note = guard_output(out, [src],
                                 overwrite=C.overwrite_allowed(params))
    if out_note:
        JOB.log("🛡 " + out_note)

    subs = subtitles.plan([info.get("subtitle_codecs") or []], out.suffix)
    if subs.get("note"):
        JOB.log("🔤 " + subs["note"])

    fps = _fps(info)
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


# ================================================================ 批量文件夹
def _run_batch(params: dict, folder: str) -> None:
    recursive = bool(params.get("recursive"))
    out_dir = _batch_output_dir(str(params.get("output") or ""))
    overwrite = C.overwrite_allowed(params)
    crop_on, left, top, right, bottom, scale_back = _crop_params(params)
    head, tail = _trim_params(params)
    encoder, crf, preset = _encoder_params(params)
    try:
        parallel = max(1, min(8, int(params.get("parallel", 2) or 2)))
    except (TypeError, ValueError):
        parallel = 2
    limit = encoders.max_parallel(encoder)
    parallel = min(parallel, limit) if limit else parallel
    on_error = str(params.get("on_error") or "abort").lower()
    if on_error not in ("abort", "skip"):
        on_error = "abort"

    JOB.update(state="scanning", message="正在扫描视频文件…")
    sc = scan_folder(folder, recursive,
                     exclude_paths=_generated_excludes(folder, recursive))
    items = [it for it in sc.get("files", []) if it.get("ok")]
    if not items:
        raise RuntimeError("文件夹里没有可用的视频文件")
    if out_dir:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"无法创建输出目录：{e}")

    # 逐个算时长与输出尺寸：有些视频分辨率小，统一裁剪会裁没
    jobs = []
    for it in items:
        dur = float(it.get("duration") or 0)
        eff = trim_span(dur, head, tail)
        if eff < 0.5:
            continue
        box = (left, top, right, bottom)
        w, h = _dims(int(it.get("width") or 0), int(it.get("height") or 0),
                     crop_on, box, scale_back, it["name"])
        jobs.append((it, eff, w, h))
    if not jobs:
        raise RuntimeError("片头片尾裁剪过度，所有文件都没有内容了")

    total_sec = sum(j[1] for j in jobs)
    JOB.update(total_files=len(jobs), done_files=0,
               source_duration=total_sec, state="encoding",
               message=f"批量裁剪 {len(jobs)} 个视频 · {parallel} 路并行")
    JOB.log(f"🎬 批量裁剪：{len(jobs)} 个视频 · {sc['folder']}")
    JOB.log(f"   方式：{'画面裁剪 + ' if crop_on else ''}"
            f"片头 -{head:g}s / 片尾 -{tail:g}s · {parallel} 路并行")
    if crop_on:
        JOB.log(f"✂️ 四边：左{left} 上{top} 右{right} 下{bottom} px"
                f"{'（缩放回原尺寸）' if scale_back else ''}")

    lock = threading.Lock()
    done_sec = {i: 0.0 for i in range(len(jobs))}
    produced, skipped = [], []
    notes = []

    def _emit():
        with lock:
            done = sum(done_sec.values())
        JOB.update(progress=max(0.0, min(1.0, done / (total_sec or 1.0))))

    def _one(idx):
        it, eff, w, h = jobs[idx]
        src = Path(it["path"])
        if JOB.cancel:
            return idx, None, CANCELED
        suffix = "_cropped" if crop_on else "_edited"
        dst = (out_dir or src.parent) / f"{src.stem}{suffix}.mp4"
        try:
            dst, note = guard_output(dst, [src], overwrite=overwrite)
        except Exception as e:
            return idx, None, str(e)
        if note:
            with lock:
                notes.append(f"{src.name}: {note}")
            JOB.log(f"  🛡 {src.name} → {dst.name}（{note}）")

        sub_plan = subtitles.plan([it.get("subtitle_codecs") or []], dst.suffix)
        box = (left, top, right, bottom) if crop_on else None

        def _cb(frac, i=idx):
            with lock:
                done_sec[i] = max(0.0, min(1.0, float(frac or 0))) * eff
            _emit()

        try:
            encode_one(src, dst, w, h, _fps(it),
                       bool(it.get("acodec")), crf, preset, encoder,
                       float(it.get("duration") or 0), _cb,
                       lambda: JOB.cancel,
                       target_sr=str(it.get("sample_rate") or "48000"),
                       trim=(head, tail), crop=box,
                       keep_subs=sub_plan["keep"])
        except Exception as e:
            return idx, None, str(e)
        if not dst.exists() or dst.stat().st_size == 0:
            return idx, None, "输出文件为空"

        with lock:
            done_sec[idx] = eff
        _emit()
        return idx, {"name": dst.name, "path": str(dst),
                     "size": dst.stat().st_size, "duration": eff}, None

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = {ex.submit(_one, i): i for i in range(len(jobs))}
        for future in as_completed(futures):
            idx, result, err = future.result()
            src_name = jobs[idx][0]["name"]
            if err == CANCELED or JOB.cancel:
                raise RuntimeError(CANCELED)
            if err is None:
                produced.append((idx, result))
                JOB.log(f"  ✅ [{len(produced) + len(skipped)}/{len(jobs)}] "
                        f"{src_name} → {result['name']}")
            else:
                skipped.append({"name": src_name, "reason": err[:200]})
                JOB.log(f"  ❌ {src_name}：{err[:120]}")
                if on_error == "abort":
                    raise RuntimeError(f"{src_name} 裁剪失败：{err}")
            JOB.update(done_files=len(produced) + len(skipped))

    produced.sort(key=lambda x: x[0])
    first_out = produced[0][1]["path"] if produced else str(out_dir or folder)
    total_size = sum(p["size"] for _, p in produced)
    report = {
        "mode": "batch", "level": "ok" if not skipped else "warn",
        "count": len(produced), "total": len(jobs),
        "skipped": skipped[:50],
        "outputs": [p for _, p in produced[:50]],
        "duration": sum(p["duration"] for _, p in produced),
        "size": total_size,
        "crop": {"left": left, "top": top, "right": right,
                 "bottom": bottom, "scale_back": scale_back},
        "notes": notes[:50],
    }
    JOB.update(state="done", progress=1.0, message=f"批量裁剪完成 · {len(produced)}/{len(jobs)} 个",
               output=first_out, final_size=total_size,
               final_duration=report["duration"],
               encoded_count=len(produced), copied_count=0, report=report)
    JOB.log("─" * 46)
    JOB.log(f"✅ 完成：{len(produced)} 个视频 · 合计 {report['duration'] / 60:.1f} 分钟"
            f" · {fmt_size(total_size)}")
    if skipped:
        JOB.log(f"⚠️ {len(skipped)} 个失败（详见任务报告）")


# ================================================================ 入口
def video_run(params: dict):
    JOB.reset()
    try:
        if not C.FFMPEG or not C.FFPROBE:
            raise RuntimeError("尚未设置 ffmpeg/ffprobe 路径")
        target = str(params.get("target") or "").strip()
        folder = str(params.get("folder") or "").strip()
        if target:
            _run_single(params, target)
        elif folder:
            if not Path(folder).expanduser().is_dir():
                raise RuntimeError(f"文件夹不存在：{folder}")
            _run_batch(params, folder)
        else:
            raise RuntimeError("请选择视频文件或文件夹")
    except Exception as e:
        _fail(e)
