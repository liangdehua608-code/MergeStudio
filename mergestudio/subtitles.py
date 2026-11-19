"""字幕轨：探测、是否保留、ffmpeg 映射参数。

字幕跟音视频不一样，坑在于**拼接要求每段都有同样的一条字幕流**：
只要有一个文件没字幕、或字幕流数量不同，concat 就会错位甚至失败。
所以这里的策略是「宁可整体不保留，也不拼出坏文件」：

1. 所有文件都恰好有一条字幕轨；
2. 编码一致；
3. 是文本字幕（subrip / ass / mov_text …），位图字幕（PGS、DVD）mp4 装不下。

三条都满足才保留，MP4 系容器统一转成 `mov_text`，其他容器直接复制。
"""
from __future__ import annotations

import subprocess

from . import config as C

TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}


def probe(path) -> list[str]:
    """返回文件的字幕流编码列表；探测失败按「无字幕」处理。"""
    if not C.FFPROBE:
        return []
    try:
        r = subprocess.run(
            [C.FFPROBE, "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return []
        return [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    except Exception:
        return []


def plan(codecs_per_file, out_suffix: str) -> dict:
    """根据每个文件的字幕编码列表，决定输出要不要保留字幕。

    返回 {"keep": bool, "encode": "mov_text"|"copy"|None, "codec": str|None, "note": str}
    """
    lists = [list(c or []) for c in codecs_per_file]
    if not lists or not any(lists):
        return {"keep": False, "encode": None, "codec": None, "note": ""}

    if any(len(l) != 1 for l in lists):
        missing = sum(1 for l in lists if not l)
        return {"keep": False, "encode": None, "codec": None,
                "note": f"字幕轨不一致（{missing} 个文件无字幕或条数不同），"
                        f"为保证拼接正确已放弃字幕"}

    codecs = {l[0] for l in lists}
    if len(codecs) > 1:
        return {"keep": False, "encode": None, "codec": None,
                "note": "字幕编码不一致（" + "、".join(sorted(codecs)) +
                        "），已放弃字幕"}
    codec = codecs.pop()
    if codec not in TEXT_CODECS:
        return {"keep": False, "encode": None, "codec": None,
                "note": f"字幕是位图格式（{codec}），mp4 无法保存，已跳过"}

    encode = "mov_text" if str(out_suffix).lower() in C.MP4_EXTS else "copy"
    return {"keep": True, "encode": encode, "codec": codec,
            "note": f"保留字幕轨（{codec} → {encode}）"}


def map_args(sub_plan: dict, prefix: str = "0") -> list[str]:
    """把保留策略翻译成 ffmpeg 参数；不保留时返回空列表。"""
    if not sub_plan or not sub_plan.get("keep"):
        return []
    return ["-map", f"{prefix}:s?", "-c:s", sub_plan["encode"]]
