"""目录扫描：列视频文件 + 并行探测 + 分组统计（合并页的「预检清单」）。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config as C
from .paths import natural_key
from .probe import analyze_items, probe_full, sig_label, signature


def list_video_files(folder: str, recursive: bool):
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise ValueError(f"文件夹不存在：{root}")
    it = root.rglob("*") if recursive else root.iterdir()
    files = [p for p in it if p.is_file() and p.suffix.lower() in C.VIDEO_EXTS]
    files.sort(key=natural_key)
    return root, files


def _exclude_set(exclude_path, exclude_paths) -> set:
    items = []
    if exclude_path:
        items.append(exclude_path)
    if exclude_paths:
        items.extend(exclude_paths)
    out = set()
    for p in items:
        try:
            out.add(str(Path(p).resolve()))
        except Exception:
            continue
    return out


def scan_folder(folder: str, recursive: bool, exclude_path=None,
                exclude_paths=None) -> dict:
    """扫描 + 预检。

    exclude_* 用来排除「本任务自己的输出」（含历史变体）：否则重复跑同一个
    文件夹时，上次的成品会被当成素材再拼一遍。
    """
    if not C.FFPROBE:
        raise RuntimeError("尚未设置 ffprobe 路径")
    root, files = list_video_files(folder, recursive)
    skip = _exclude_set(exclude_path, exclude_paths)
    if skip:
        files = [f for f in files if _safe_resolve(f) not in skip]

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

    majority_sig, majority_size = None, 0
    for sig, grp in groups.items():
        if len(grp) > majority_size:
            majority_sig, majority_size = sig, len(grp)

    for item in items:
        is_maj = bool(item.get("ok")) and signature(item) == majority_sig
        item["is_majority"] = is_maj
        item["needs_encode"] = bool(item.get("ok")) and not is_maj

    health_summary, majority_ts, majority_sr = analyze_items(items)

    return {
        "folder": str(root),
        "files": items,
        "total_size": sum(i["size"] for i in items),
        "total_duration": sum(i.get("duration") or 0 for i in items),
        "all_ok": all(i.get("ok") for i in items),
        "signature_same": len(groups) <= 1,
        "majority_count": majority_size,
        "need_encode_count": sum(1 for i in items if i.get("needs_encode")),
        "edit_list_count": sum(1 for i in items if i.get("has_edit_list")),
        "subtitle_count": sum(1 for i in items if i.get("subtitle_codecs")),
        "health_summary": health_summary,
        "majority_ts": majority_ts,
        "majority_sr": majority_sr,
        "signature_groups": [
            {"count": len(grp), "is_majority": sig == majority_sig,
             "label": sig_label(grp[0])}
            for sig, grp in sorted(groups.items(), key=lambda x: -len(x[1]))
        ],
    }


def _safe_resolve(p: Path) -> str:
    try:
        return str(Path(p).resolve())
    except Exception:
        return str(p)
