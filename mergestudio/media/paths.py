"""路径与小工具：输出命名、覆盖保护、工作目录、资源管理器定位。

覆盖保护是两层，缺一不可：

1. **防丢文件**：输出路径绝不允许等于任何源文件。拼接/切割/裁剪都是一边读一边写，
   目标如果就是源文件，等于边读边截断，源视频必毁。
2. **防丢内容**：目标已存在时默认**改名**（`xxx_2.mp4`）而不是覆盖。上一版辛苦拼了
   三小时的结果，不该被一次误点抹掉。确实想覆盖时显式打开 overwrite。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .. import config as C


# ======================== 输出路径 ========================
def compute_output_path(src_folder: str, out_arg: str) -> Path:
    src_root = Path(src_folder).expanduser()
    name = src_root.name or "merged"
    if out_arg:
        cand = Path(out_arg).expanduser()
        if cand.suffix.lower() in C.VIDEO_EXTS:
            return cand
        return cand / f"{name}_merged.mp4"
    return src_root / f"{name}_merged.mp4"


def output_family(out: Path) -> list[Path]:
    """同一输出名的所有历史变体（`x_merged.mp4` / `x_merged_2.mp4` …）。

    扫描时要整体排除：否则第二次跑会把上次的成品当成素材拼进去，内容直接翻倍。
    """
    out = Path(out)
    try:
        pattern = re.compile(r"^" + re.escape(out.stem) + r"(_\d+)?$")
        suffix = out.suffix.lower()
        return [p for p in out.parent.iterdir()
                if p.is_file() and p.suffix.lower() == suffix
                and pattern.match(p.stem)]
    except Exception:
        return []


def unique_path(out: Path) -> Path:
    """返回一个当前不存在的路径：`a.mp4` → `a_2.mp4` → `a_3.mp4` …"""
    out = Path(out)
    if not out.exists():
        return out
    for i in range(2, 1000):
        cand = out.with_name(f"{out.stem}_{i}{out.suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"同名文件过多，无法生成新文件名：{out.name}")


def guard_output(out: Path, sources=(), overwrite: bool = False):
    """输出前的两道保护，返回 (最终路径, 给用户看的说明)。

    - 与源文件同路径 → 直接报错，任务失败也比毁掉素材强
    - 已存在同名文件 → 默认改名；overwrite=True 时才允许覆盖
    """
    out = Path(out)
    srcs = {Path(s).resolve() for s in sources if s}
    if out.resolve() in srcs:
        raise RuntimeError(f"输出路径与源文件相同，已阻止（否则会把素材写坏）：{out}")

    if not out.exists():
        return out, ""
    if overwrite:
        return out, f"覆盖已有文件：{out.name}"

    cand = unique_path(out)
    if cand.resolve() in srcs:
        raise RuntimeError(f"改名后的输出仍与源文件冲突，请换一个输出目录：{cand}")
    return cand, f"已存在同名文件，自动改名为 {cand.name}（原文件保留）"


def work_dir_for(out: Path, task_id: str) -> Path:
    """断点续跑的工作目录：放在输出目录下，和任务一起清理。"""
    safe = re.sub(r"[^0-9A-Za-z_-]", "", str(task_id or "")) or "adhoc"
    return Path(out).parent / f".merge_work_{safe}"


def reveal_path(p: Path):
    """在资源管理器里定位文件（或打开目录）。

    Windows 上必须用 `explorer /select,"路径"` 这种「整串命令」形式：
    传参数列表时 Python 会把 `/select,C:/a b/c.mp4` 整体加引号，
    资源管理器解析失败就会退回到默认目录——表现就是「点了没反应」。
    """
    if os.name == "nt":
        target = str(p)
        try:
            if p.is_file():
                subprocess.Popen(f'explorer /select,"{target}"')
            else:
                subprocess.Popen(f'explorer "{target}"')
            return
        except Exception:
            try:
                os.startfile(str(p.parent if p.is_file() else p))
                return
            except Exception:
                pass
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)])


# ======================== 小工具 ========================
def natural_key(p: Path):
    """自然排序：第 2 集排在第 10 集前面。"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", p.name)]


def fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def parse_trim_seconds(v) -> float:
    """解析裁剪秒数，限制 0~3600。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return max(0.0, min(f, 3600.0))


def trim_span(dur, head: float, tail: float) -> float:
    """裁剪后的有效时长：至少留 0.2s，避免产出空文件。"""
    dur = float(dur or 0)
    if dur <= 0:
        return 0.0
    start = head if head < dur - 0.2 else 0.0
    remain = dur - start
    cut_tail = min(tail, max(0.0, remain - 0.2))
    return max(0.0, remain - cut_tail)
