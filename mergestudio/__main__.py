"""命令行入口：启动内置 Web 服务，或停止已有实例。

用法：
    python -m mergestudio                # 启动（默认 8760，自动开浏览器）
    python -m mergestudio --port 9000    # 指定端口
    python -m mergestudio --no-browser
    python -m mergestudio --stop         # 停止正在运行的实例
"""
from __future__ import annotations

import argparse
import atexit
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from . import config as C
from . import notify
from .queue import QUEUE
from .runtime import (clear_runtime, pid_alive, probe_instance,
                      read_runtime, write_runtime)
from .web import make_server, set_shutdown_hook

RUNNING_STATES = ("running", "scanning", "copying", "encoding")


def _stop_instance() -> int:
    prev = read_runtime() or {}
    pid = prev.get("pid")
    port = prev.get("port")
    running = bool(port and probe_instance(port, timeout=1.0))
    if running:
        ok = False
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{int(port)}/api/shutdown",
                data=b"{}", method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                ok = r.status == 200
        except Exception:
            ok = False
        if ok:
            # 给服务时间取消任务、等 ffmpeg 退出并清理临时目录
            for _ in range(45):
                if not pid_alive(pid):
                    break
                time.sleep(0.2)
        if pid_alive(pid):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except Exception:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True)
        print(f"✅ 已停止 {C.APP_NAME}（PID {pid}，端口 {port}）")
    else:
        print("没有正在运行的实例。")
    try:
        C.RUNTIME_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return 0


def main() -> int:
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        prog="mergestudio",
        description=f"{C.APP_NAME} v{C.APP_VERSION}（队列 · 合并 · 裁剪 · 提取音频）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None,
                    help=f"监听端口，默认 {C.DEFAULT_PORT}；0 表示随机")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的实例")
    args = ap.parse_args()

    if args.stop:
        return _stop_instance()

    # 已有实例则直接复用，避免起出第二个服务
    prev = read_runtime()
    if prev:
        port = prev.get("port")
        url = prev.get("url") or f"http://127.0.0.1:{port}/"
        if pid_alive(prev.get("pid")) and probe_instance(port):
            print("=" * 66)
            print(f"  检测到 {C.APP_NAME} 已经在运行，不再重复启动。")
            print(f"  地址：{url}")
            print("  已在浏览器中打开；本窗口可以直接关闭。")
            print("=" * 66)
            if not args.no_browser:
                webbrowser.open(url)
            return 3
        print("上次的运行记录已失效，正在清理…")
        try:
            C.RUNTIME_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    cfg = C.load_config()
    C.apply_config(cfg)
    st = C.config_status()

    print("=" * 66)
    print(f"  {C.APP_NAME} v{C.APP_VERSION}")
    print("=" * 66)
    print(f"  数据目录: {C.DATA_DIR}")
    print(f"  前端目录: {C.WEB_DIR}")
    if st["ready"]:
        print(f"  ffmpeg  : {C.FFMPEG}")
        print(f"  ffprobe : {C.FFPROBE}")
    else:
        print("  ⚠️  ffmpeg / ffprobe 尚未就绪。")
        print("     启动后请在网页「设置」页填写 → 验证并保存。")
    print("=" * 66)
    print("  功能：任务队列（重启不丢）· 合并/裁剪/章节 · 字幕保留 · 完成通知")
    print("=" * 66)

    if args.port is None:
        candidates = [C.DEFAULT_PORT] + [C.DEFAULT_PORT + i for i in range(1, 11)]
    elif args.port == 0:
        candidates = [0]
    else:
        candidates = [args.port]

    server = None
    last_err = None
    for p in candidates:
        try:
            server = make_server(args.host, p)
            break
        except OSError as e:
            last_err = e
    if server is None:
        print(f"\n❌ 无法监听端口：{last_err}")
        print("   端口可能被其他程序占用，可换一个：python -m mergestudio --port 8761")
        return 1

    port = server.server_port
    url = f"http://{args.host}:{port}/"
    write_runtime(port, url)
    atexit.register(clear_runtime)
    atexit.register(notify.stop)

    def _shutdown():
        try:
            tid = QUEUE.active_id
            if tid:
                QUEUE.cancel(tid)
                task = QUEUE.get(tid)
                t0 = time.time()
                while task and task.state in RUNNING_STATES and time.time() - t0 < 8:
                    time.sleep(0.2)
        except Exception:
            pass
        notify.stop()
        QUEUE.shutdown(wait=2.0)      # 停 worker 并把队列写盘，重启后接着跑
        time.sleep(0.3)
        try:
            server.shutdown()
        except Exception:
            pass

    set_shutdown_hook(_shutdown)

    # 托盘菜单与通知：地址要给，否则托盘点「打开界面」不知道去哪
    notify.setup(url, {
        "open": lambda: webbrowser.open(url),
        "pause": lambda: QUEUE.set_paused(not QUEUE.paused),
        "is_paused": lambda: QUEUE.paused,
        "quit": _shutdown,
    })
    notify.configure(C.load_config().get("notify") or {})
    QUEUE.start()                     # 恢复上次没跑完/没跑到的任务

    print(f"\n🌐 服务已启动： {url}")
    print("   浏览器会自动打开；关闭本窗口或按 Ctrl+C 停止服务")
    print("   也可以双击「停止.bat」安全退出（会先取消正在跑的任务）\n")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        notify.stop()
        QUEUE.shutdown(wait=2.0)
        server.server_close()
        clear_runtime()
        print("已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
