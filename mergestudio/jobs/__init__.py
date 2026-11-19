"""任务分发：按 `op` 字段选择具体的处理流程。"""
from __future__ import annotations

from .. import config as C
from ..task import JOB


def run_task(params: dict):
    op = str((params or {}).get("op") or "merge").lower()
    if op == "audio":
        from .audio import audio_run
        return audio_run(params)
    if op == "mux":
        from .mux import mux_run
        return mux_run(params)
    if op == "video":
        from .video import video_run
        return video_run(params)
    if op == "split":
        from .split import split_run
        return split_run(params)
    # 默认合并；也接受显式的 merge
    from .merge import job_run
    return job_run(params)
