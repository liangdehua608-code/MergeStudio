"""ffmpeg 进程封装：带进度解析与可取消。"""
from __future__ import annotations

import re
import subprocess
import threading
import time

from .. import config as C


def run_ffmpeg(cmd, total_sec=0.0, on_progress=None, should_cancel=None):
    """执行 ffmpeg，按 `-progress pipe:1` 回报进度。

    返回 (退出码, 最后 60 行 stderr, 是否被取消)。
    取消走 terminate 而不是 kill，让 ffmpeg 自己收尾、释放输出文件句柄。
    标准输入接 DEVNULL：等价于旧命令里的 `-nostdin`，但不依赖那个 3.3 才有的参数
    （老版本 ffmpeg 见到它会报 `Failed to set value '-f' for option 'nostdin'`）。
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1)
    err_buf = []

    def _read_err():
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    err_buf.append(line)
                    if len(err_buf) > 400:
                        del err_buf[:200]
        except Exception:
            pass

    threading.Thread(target=_read_err, daemon=True).start()

    last_emit = 0.0
    canceled = False
    try:
        for line in proc.stdout:
            if should_cancel and should_cancel():
                canceled = True
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, val = line.partition("=")
            sec = None
            if key in ("out_time_us", "out_time_ms"):
                try:
                    sec = int(val) / 1_000_000.0
                except ValueError:
                    sec = None
            elif key == "out_time":
                m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", val)
                if m:
                    sec = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                           + float(m.group(3)))
            elif key == "progress" and val == "end":
                if on_progress:
                    on_progress(1.0)
                continue
            if sec is not None and sec >= 0 and total_sec > 0 and on_progress:
                now = time.time()
                if now - last_emit >= 0.15:
                    on_progress(min(sec / total_sec, 0.999))
                    last_emit = now
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
    return proc.returncode, "\n".join(err_buf[-60:]), canceled


def extract_frame(path, t: float = 2.0, width: int = 720) -> bytes:
    """抽一帧转成 JPEG，用于网页里的裁剪预览。"""
    try:
        t = max(0.0, float(t))
    except Exception:
        t = 0.0
    cmd = [C.FFMPEG, "-hide_banner", "-v", "error",
           "-ss", f"{t:.2f}", "-i", str(path),
           "-frames:v", "1",
           "-vf", f"scale={max(160, int(width))}:-2",
           "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=60)
    if r.returncode != 0 or not r.stdout:
        cmd[cmd.index("-ss") + 1] = "0"
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=60)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("抽帧失败")
    return r.stdout
