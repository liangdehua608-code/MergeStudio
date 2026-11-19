"""任务队列：单 worker 串行执行；状态落盘，重启不丢排队任务。

为什么串行：合并/转码是磁盘密集型，两三个 ffmpeg 抢一块盘的吞吐，
结果往往是「每个都慢一倍」，还不如老老实实排队。

落盘策略：状态变化时（入队/开始/结束/取消/重试/删除/暂停）整体写一次
`queue.json`。文件只有几十 KB，写一次几毫秒，比「进度每秒存档」简单得多，
崩溃时最多丢掉一个任务的秒级进度——反正它会被当成「被中断」重新排队。
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from . import config as C
from . import notify, store
from .jobs import run_task
from .media import (compute_output_path, list_video_files, natural_key,
                    work_dir_for)
from .task import FINISHED_STATES, RUNNING_STATES, Task, bind_active

MAX_FINISHED_ROWS = 60      # 落盘时最多保留多少条已结束任务

_IDLE_TASK = Task("--", "空闲", {})
_IDLE_TASK.state = "idle"
_IDLE_TASK.message = "就绪"
_IDLE_TASK.logs = []


class TaskQueue:
    """单 worker 串行队列：同一时刻只跑一个 ffmpeg 任务。"""

    def __init__(self, state_file=None, autostart: bool = False):
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.tasks: list[Task] = []
        self.active_id = ""
        self.paused = False
        self.seq = 0
        self._stop = False
        self._thread = None
        self.state_file = Path(state_file or C.QUEUE_FILE)
        self._load()
        if autostart:
            self.start()

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """显式启动 worker。`--stop` 这类模式不该顺手把排队任务跑起来。"""
        with self.lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name="merge-queue",
                                            daemon=True)
            self._thread.start()

    def shutdown(self, wait: float = 8.0) -> None:
        """停止 worker：不再取新任务，等在跑的任务收尾，最后落盘。"""
        with self.cond:
            self._stop = True
            self.cond.notify_all()
        end = time.time() + max(0.0, wait)
        while time.time() < end and self.active_id:
            time.sleep(0.1)
        self._persist()

    # ---------- 落盘 ----------
    def _persist(self) -> None:
        try:
            with self.lock:
                rows = [t.to_row() for t in self.tasks]
                kept, finished = [], 0
                for row in reversed(rows):
                    if row.get("state") in FINISHED_STATES:
                        finished += 1
                        if finished > MAX_FINISHED_ROWS:
                            continue
                    kept.append(row)
                kept.reverse()
                payload = {"v": 2, "app": C.APP_ID, "saved_at": time.time(),
                           "seq": self.seq, "paused": self.paused,
                           "tasks": kept}
            store.write_json(self.state_file, payload)
        except Exception:
            pass

    def _load(self) -> None:
        data = store.read_json(self.state_file, None)
        rows, seq_hint, legacy = [], 0, False
        if isinstance(data, dict):
            rows = data.get("tasks") or []
            self.paused = bool(data.get("paused"))
            seq_hint = int(data.get("seq") or 0)
        elif isinstance(data, list):
            rows, legacy = data, True   # 旧版 history.json：纯列表
        elif self.state_file == C.QUEUE_FILE:
            old = store.read_json(C.HISTORY_FILE, None)
            if isinstance(old, list):
                rows, legacy = old, True    # 首次升级：把旧历史迁移过来
        if not isinstance(rows, list):
            rows = []

        max_seq = 0
        for row in rows:
            try:
                task = Task.from_row(row)
            except Exception:
                continue
            if not task.id:
                continue
            self.tasks.append(task)
            if task.id.startswith("T") and task.id[1:].isdigit():
                max_seq = max(max_seq, int(task.id[1:]))
        self.seq = max(max_seq, seq_hint)
        # 只有「旧格式迁移」才立刻回写；正常启动不回写，避免 --stop 这类短命进程
        # 去改正在运行那个实例的文件。
        if legacy:
            self._persist()

    def save(self) -> None:
        self._persist()

    # ---------- 入队 ----------
    def add(self, params: dict, name: str = "") -> dict:
        with self.lock:
            self.seq += 1
            folder = str(params.get("folder", "")).strip()
            target = str(params.get("target", "")).strip()
            if folder:
                auto_name = Path(folder).name
            elif target:
                auto_name = Path(target).stem
            else:
                auto_name = "任务"
            task = Task(f"T{self.seq:03d}", name or auto_name, params)
            self.tasks.append(task)
            self.cond.notify_all()
            snapshot = task.snapshot()
        self._persist()
        return snapshot

    def batch_add(self, parent: str, params: dict) -> dict:
        """父目录下每个「直接包含视频」的子文件夹各建一个任务。"""
        root = Path(parent).expanduser()
        if not root.is_dir():
            raise ValueError(f"文件夹不存在：{root}")
        try:
            subs = sorted([p for p in root.iterdir() if p.is_dir()],
                          key=natural_key)
        except Exception as e:
            raise RuntimeError(f"读取目录失败：{e}")

        added, skipped = [], []
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
                if t.state in FINISHED_STATES:
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

    @staticmethod
    def _cleanup_work(task) -> None:
        """删除任务留下的中间分段（输出目录下的 .merge_work_xxx）。"""
        try:
            out = compute_output_path(task.folder, task.params.get("output") or "")
            if out.suffix == "":
                out = out.with_suffix(".mp4")
            shutil.rmtree(work_dir_for(out, task.id), ignore_errors=True)
        except Exception:
            pass

    def retry(self, tid: str, resume: bool = False) -> bool:
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
                task.logs = ["♻️ 从断点继续（复用已完成分段）" if resume
                             else "🔁 已重新排队"]
                if resume:
                    task.params["_resume"] = True
                else:
                    task.params.pop("_resume", None)
            self.cond.notify_all()
            self._persist()
            return True

    def remove(self, tid: str) -> bool:
        with self.lock:
            task = self.get(tid)
            if not task or task.id == self.active_id:
                return False
            self.tasks.remove(task)
            self._cleanup_work(task)
            self._persist()
            return True

    def clear(self, scope: str = "done") -> int:
        with self.lock:
            if scope == "all":
                keep = [t for t in self.tasks
                        if t.id == self.active_id or t.state == "queued"]
            else:
                keep = [t for t in self.tasks
                        if t.state not in FINISHED_STATES]
            gone = [t for t in self.tasks if t not in keep]
            self.tasks = keep
            for t in gone:
                self._cleanup_work(t)
            self._persist()
            return len(gone)

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
            self._persist()
            return True

    def set_paused(self, paused: bool):
        with self.lock:
            self.paused = bool(paused)
            self.cond.notify_all()
            self._persist()

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
                self._persist()
            bind_active(task)
            notify.update_tip(f"MergeStudio · 运行中 {task.name[:36]}")
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
                try:
                    notify.task_finished(task)
                except Exception:
                    pass


QUEUE = TaskQueue()
