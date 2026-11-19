"""章节标记：按分段实测时长累加，写成 ffmetadata 后交给 ffmpeg。"""
from __future__ import annotations

import re

from pathlib import Path


def _sanitize_title(t: str) -> str:
    """ffmetadata 里 = ; # \\ 都是特殊字符，替换掉避免解析失败。"""
    s = str(t or "").replace("\r", " ").replace("\n", " ")
    for ch in ("=", ";", "#", "\\"):
        s = s.replace(ch, "-" if ch == "=" else " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:90] or "章节"


def build_chapters(titles, eff_durations):
    """titles + 每段有效时长 → [(title, start, end)]，跳过空段。"""
    out, cur = [], 0.0
    for title, d in zip(titles, eff_durations):
        d = float(d or 0)
        if d <= 0.05:
            continue
        out.append((title, cur, cur + d))
        cur += d
    return out


def build_chapter_meta(chapters, path: Path):
    lines = [";FFMETADATA1"]
    for title, st, en in chapters:
        if en - st < 0.05:
            continue
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={int(round(st * 1000))}")
        lines.append(f"END={int(round(en * 1000))}")
        lines.append("title=" + _sanitize_title(title))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
