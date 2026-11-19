"""媒体层：探测 / 执行 / 扫描 / 路径 / 裁剪 / 章节。

按职责拆成子模块，这里统一再导出，业务代码仍然只写 `from ..media import xxx`：

    probe.py      ffprobe 探测、自检、预检打分
    ffmpeg.py     跑 ffmpeg（进度解析 + 可取消）、抽帧
    scan.py       目录扫描与分组统计
    paths.py      输出命名、覆盖保护、工作目录、资源管理器定位
    crop.py       黑边检测
    chapters.py   章节元数据
"""
from __future__ import annotations

from .chapters import build_chapter_meta, build_chapters
from .crop import detect_crop
from .ffmpeg import extract_frame, run_ffmpeg
from .paths import (compute_output_path, fmt_size, guard_output, natural_key,
                    output_family, parse_trim_seconds, reveal_path, trim_span,
                    unique_path, work_dir_for)
from .probe import (analyze_items, get_media_duration, get_stream_duration,
                    probe_chapters, probe_full, probe_media,
                    probe_output_summary, sig_label, signature)
from .scan import list_video_files, scan_folder

__all__ = [
    "analyze_items", "build_chapter_meta", "build_chapters",
    "compute_output_path", "detect_crop", "extract_frame", "fmt_size",
    "get_media_duration", "get_stream_duration", "guard_output",
    "list_video_files", "natural_key", "output_family", "parse_trim_seconds",
    "probe_chapters", "probe_full", "probe_media", "probe_output_summary",
    "reveal_path", "run_ffmpeg", "scan_folder", "sig_label", "signature",
    "trim_span", "unique_path", "work_dir_for",
]
