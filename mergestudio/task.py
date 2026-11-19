"""任务对象与 `JOB` 代理。

任务字段刻意与早期单文件版本的全局 JOB 保持一致，这样 jobs 里的处理函数
可以直接用 `JOB.update(...)` / `JOB.log(...)`，由代理转发到当前线程（或当前
活动任务）上，互不串台。
"""
from __future__ import annotations

import threading
import time

RUNNING_STATES = ("running", "scanning", "copying", "encoding")


class Task:
    """
    一个拼接任务。

    字段名刻意与 v4 的 JOB 保持一致（state/message/progress/logs/...），
    这样 job_run / do_copy / encode_one 里的 JOB.xxx 调用可以原封不动地复用，
    由下面的 _JobProxy 把读写转发到「当前线程正在跑的那个任务」。
    """

    def __init__(self, tid: str, name: str, params: dict):
        self.lock = threading.Lock()
        self.id = tid
        self.name = name or "未命名任务"
        self.params = dict(params or {})
        self.folder = str(self.params.get("folder", ""))
        self.created_at = time.time()
        self.started_at = 0.0
        self.finished_at = 0.0
        self._reset_fields()
        self.state = "queued"
        self.message = "排队中"
        self.logs = ["⏳ 已加入队列，等待执行"]

    def _reset_fields(self):
        self.state = "idle"
        self.message = "就绪"
        self.progress = 0.0
        self.current_file = ""
        self.output = ""
        self.total_files = 0
        self.done_files = 0
        self.cancel = False
        self.logs = []
        self.final_duration = 0.0
        self.final_size = 0
        self.encoded_count = 0
        self.copied_count = 0
        self.source_duration = 0.0
        self.duration_delta = 0.0
        self.report = {}
        self.error = ""

    # --- 与 v4 JOB 相同的接口 ---
    def reset(self):
        """job_run 入口会调用：只清进度，不动任务身份与队列时间戳。"""
        with self.lock:
            self._reset_fields()
            self.state = "running"
            self.message = "启动中…"
            self.logs = ["▶ 开始执行"]

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
            if kw.get("state") in ("done", "error", "canceled"):
                self.finished_at = self.finished_at or time.time()

    def log(self, line: str):
        with self.lock:
            self.logs.append(str(line))
            if len(self.logs) > 900:
                del self.logs[:450]

    def snapshot(self, full: bool = False) -> dict:
        with self.lock:
            now = time.time()
            if self.started_at:
                end = self.finished_at or now
                elapsed = max(0.0, end - self.started_at)
            else:
                elapsed = 0.0
            p = self.params or {}
            data = {
                "id": self.id,
                "name": self.name,
                "folder": self.folder,
                "state": self.state,
                "message": self.message,
                "progress": round(float(self.progress or 0), 4),
                "current_file": self.current_file,
                "output": self.output,
                "total_files": self.total_files,
                "done_files": self.done_files,
                "final_duration": self.final_duration,
                "final_size": self.final_size,
                "encoded_count": self.encoded_count,
                "copied_count": self.copied_count,
                "source_duration": self.source_duration,
                "duration_delta": self.duration_delta,
                "report": self.report,
                "error": self.error,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": round(elapsed, 1),
                "options": {
                    "op": p.get("op", "merge"),
                    "crop": p.get("crop") or {},
                    "audio_format": p.get("audio_format", "m4a"),
                    "audio_quality": p.get("audio_quality", 2),
                    "mode": p.get("mode", "auto"),
                    "encoder": p.get("encoder", "libx264"),
                    "parallel": p.get("parallel", 2),
                    "crf": p.get("crf", 20),
                    "preset": p.get("preset", "veryfast"),
                    "size": p.get("size", ""),
                    "fps": p.get("fps", ""),
                    "trim_head": p.get("trim_head", 0),
                    "trim_tail": p.get("trim_tail", 0),
                    "chapters": bool(p.get("chapters", True)),
                    "recursive": bool(p.get("recursive")),
                    "output": p.get("output", ""),
                },
            }
            if full:
                data["logs"] = list(self.logs[-400:])
                data["params"] = self.params
            else:
                data["log_tail"] = self.logs[-1] if self.logs else ""
            return data


_TLS = threading.local()
_ACTIVE = {"task": None}

_IDLE_TASK = Task("--", "空闲", {})
_IDLE_TASK.state = "idle"
_IDLE_TASK.message = "就绪"
_IDLE_TASK.logs = []


def bind_active(task):
    """队列在任务开始/结束时设置，供线程池里的子线程回退使用。"""
    _ACTIVE["task"] = task


class _JobProxy:
    """把 JOB 调用转发到「当前线程 / 当前活动任务」。"""

    def _target(self) -> Task:
        task = getattr(_TLS, "task", None)
        if task is not None:
            return task
        task = _ACTIVE.get("task")
        if task is not None:
            return task
        return _IDLE_TASK

    def __getattr__(self, item):
        return getattr(self._target(), item)

    def update(self, **kw):
        self._target().update(**kw)

    def log(self, line: str):
        self._target().log(line)

    def reset(self):
        self._target().reset()

    def snapshot(self):
        return self._target().snapshot()


JOB = _JobProxy()
