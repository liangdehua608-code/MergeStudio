"""任务队列：单 worker 串行执行，支持暂停、排序、取消、重试与历史落盘。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import config as C
from .jobs import run_task
from .media import list_video_files, natural_key
from .task import RUNNING_STATES, Task, bind_active

class TaskQueue:
    """单 worker 串行队列：同一时刻只跑一个 ffmpeg 任务，避免抢占磁盘与 CPU。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.tasks: list[Task] = []
        self.active_id = ""
        self.paused = False
        self.seq = 0
        self._stop = False
        self._load_history()
        threading.Thread(target=self._loop, daemon=True, name="merge-queue").start()

    # ---------- 历史记录（只保留已结束的任务，重启后仍可查报告） ----------
    _HISTORY_FIELDS = ("id", "name", "folder", "state", "message", "progress",
                       "output", "total_files", "done_files", "final_duration",
                       "final_size", "encoded_count", "copied_count",
                       "source_duration", "duration_delta", "report", "error",
                       "created_at", "started_at", "finished_at", "options")

    def _persist(self):
        try:
            with self.lock:
                rows = []
                for t in self.tasks:
                    if t.state not in ("done", "error", "canceled"):
                        continue
                    snap = t.snapshot()
                    row = {k: snap.get(k) for k in self._HISTORY_FIELDS}
                    row["params"] = t.params
                    rows.append(row)
                rows = rows[-40:]
            tmp = C.HISTORY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(C.HISTORY_FILE)
        except Exception:
            pass

    def _load_history(self):
        if not C.HISTORY_FILE.is_file():
            return
        try:
            rows = json.loads(C.HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        max_seq = 0
        for row in rows if isinstance(rows, list) else []:
            try:
                tid = str(row.get("id") or "")
                params = row.get("params") or {}
                t = Task(tid, str(row.get("name") or "历史任务"), params)
                for k in ("state", "message", "progress", "output", "total_files",
                          "done_files", "final_duration", "final_size",
                          "encoded_count", "copied_count", "source_duration",
                          "duration_delta", "report", "error", "created_at",
                          "started_at", "finished_at"):
                    if k in row:
                        setattr(t, k, row[k])
                t.logs = ["（历史记录：来自上次运行，可查看报告或重新排队）"]
                self.tasks.append(t)
                if tid.startswith("T") and tid[1:].isdigit():
                    max_seq = max(max_seq, int(tid[1:]))
            except Exception:
                continue
        self.seq = max_seq

    # ---------- 入队 ----------
    def add(self, params: dict, name: str = "") -> dict:
        with self.lock:
            self.seq += 1
            folder = str(params.get("folder", "")).strip()
            auto_name = Path(folder).name or "拼接任务"
            tid = f"T{self.seq:03d}"
            task = Task(tid, name or auto_name, params)
            self.tasks.append(task)
            self.cond.notify_all()
            return task.snapshot()

    def batch_add(self, parent: str, params: dict) -> dict:
        """父目录下每个「直接包含视频」的子文件夹各建一个任务。"""
        root = Path(parent).expanduser()
        if not root.is_dir():
            raise ValueError(f"文件夹不存在：{root}")
        added, skipped = [], []
        try:
            subs = sorted([p for p in root.iterdir() if p.is_dir()],
                          key=natural_key)
        except Exception as e:
            raise RuntimeError(f"读取目录失败：{e}")
        for sub in subs:
            if sub.name.startswith("."):
                continue
            try:
                _, files = list_video_files(str(sub), False)
            except Exception:
                files = []
            if not files:
                skipped.append(sub.name)
                continue
            p = dict(params)
            p["folder"] = str(sub)
            p.pop("file_order", None)
            p.pop("name", None)
            added.append(self.add(p, sub.name))
        return {"added": added, "skipped": skipped}

    # ---------- 查询 ----------
    def get(self, tid: str):
        with self.lock:
            return next((t for t in self.tasks if t.id == tid), None)

    def _counts(self) -> dict:
        counts = {"queued": 0, "running": 0, "done": 0, "error": 0, "canceled": 0}
        for t in self.tasks:
            if t.state in RUNNING_STATES:
                counts["running"] += 1
            elif t.state in ("queued", "idle"):
                counts["queued"] += 1
            elif t.state in counts:
                counts[t.state] += 1
        return counts

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "paused": self.paused,
                "active_id": self.active_id,
                "counts": self._counts(),
                "total": len(self.tasks),
                "tasks": [t.snapshot() for t in self.tasks],
            }

    def status_snapshot(self) -> dict:
        """给 /api/status 用：优先返回正在跑的任务，否则返回最后一个有结果的任务。"""
        with self.lock:
            active = self.get(self.active_id)
            if active:
                return active.snapshot()
            for t in reversed(self.tasks):
                if t.state in ("done", "error", "canceled"):
                    return t.snapshot()
            return _IDLE_TASK.snapshot()

    # ---------- 操作 ----------
    def cancel(self, tid: str) -> bool:
        with self.lock:
            task = self.get(tid)
            if not task:
                return False
            if task.state == "queued":
                task.update(state="canceled", message="已取消", error="用户取消",
                            finished_at=time.time())
                task.log("⏹ 排队中被取消")
                self._persist()
                return True
            if task.id != self.active_id:
                return False
            task.update(cancel=True, message="正在取消…")
            task.log("⏹ 收到取消指令，等待 ffmpeg 退出…")
            return True

    def retry(self, tid: str) -> bool:
        with self.lock:
            task = self.get(tid)
            if not task or task.state in RUNNING_STATES or task.state == "queued":
                return False
            with task.lock:
                task._reset_fields()
                task.state = "queued"
                task.message = "排队中"
                task.cancel = False
                task.started_at = 0.0
                task.finished_at = 0.0
                task.logs = ["🔁 已重新排队"]
            self.cond.notify_all()
            self._persist()
            return True

    def remove(self, tid: str) -> bool:
        with self.lock:
            task = self.get(tid)
            if not task or task.id == self.active_id:
                return False
            self.tasks.remove(task)
            self._persist()
            return True

    def clear(self, scope: str = "done") -> int:
        with self.lock:
            if scope == "all":
                keep = [t for t in self.tasks if t.id == self.active_id
                        or t.state == "queued"]
            else:
                keep = [t for t in self.tasks
                        if t.state not in ("done", "error", "canceled")]
            removed = len(self.tasks) - len(keep)
            self.tasks = keep
            self._persist()
            return removed

    def move(self, tid: str, direction: str) -> bool:
        with self.lock:
            task = self.get(tid)
            if not task or task.state != "queued":
                return False
            idx = self.tasks.index(task)
            step = -1 if direction == "up" else 1
            swap = idx + step
            # 跳过正在运行/已结束的条目，只和最近的排队任务换位
            while 0 <= swap < len(self.tasks) and self.tasks[swap].state != "queued":
                swap += step
            if swap < 0 or swap >= len(self.tasks):
                return False
            self.tasks[idx], self.tasks[swap] = self.tasks[swap], self.tasks[idx]
            return True

    def set_paused(self, paused: bool):
        with self.lock:
            self.paused = bool(paused)
            self.cond.notify_all()

    # ---------- worker ----------
    def _loop(self):
        while True:
            with self.cond:
                task = None
                while True:
                    if self._stop:
                        return
                    if not self.paused:
                        task = next((t for t in self.tasks if t.state == "queued"),
                                    None)
                    if task:
                        break
                    self.cond.wait(timeout=1.0)
                self.active_id = task.id
                task.update(state="running", message="启动中…",
                            started_at=time.time(), finished_at=0.0)
            bind_active(task)
            try:
                run_task(task.params)
            except Exception as e:  # job_run 内部已兜底，这里只防万一
                task.update(state="error", message=str(e), error=str(e))
                try:
                    task.log("ERROR: " + str(e))
                except Exception:
                    pass
            finally:
                with self.cond:
                    if task.state in RUNNING_STATES or task.state in ("idle", "queued"):
                        task.update(state="error", message="任务异常结束",
                                    error="任务异常结束")
                    task.finished_at = task.finished_at or time.time()
                    self.active_id = ""
                    bind_active(None)
                    self.cond.notify_all()
                self._persist()


QUEUE = TaskQueue()
