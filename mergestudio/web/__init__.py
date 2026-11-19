"""内置 HTTP 服务：静态页面 + JSON API。"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .. import config as C
from ..config import pick_exe_dialog, pick_folder_dialog
from ..media import (compute_output_path, detect_crop, extract_frame,
                     list_video_files, reveal_path, scan_folder)
from ..queue import QUEUE

_SHUTDOWN = {"fn": None}


def set_shutdown_hook(fn):
    """由入口注册：收到 /api/shutdown 时优雅退出。"""
    _SHUTDOWN["fn"] = fn


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = f"MergeStudio/{C.APP_VERSION}"

    def log_message(self, fmt, *args):
        pass

    # ---------- 基础 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            page = C.WEB_DIR / "index.html"
            if page.is_file():
                self._send(200, page.read_text(encoding="utf-8"), MIME[".html"])
            else:
                self._send(404, "index.html not found", "text/plain; charset=utf-8")
        elif path.startswith("/static/"):
            name = path[len("/static/"):]
            if "/" in name or "\\" in name or name.startswith("."):
                self._send(404, "not found", "text/plain")
                return
            f = C.WEB_DIR / "static" / name
            if f.is_file() and f.suffix.lower() in MIME:
                self._send(200, f.read_bytes(), MIME[f.suffix.lower()])
            else:
                self._send(404, "not found", "text/plain")
        elif path == "/api/thumb":
            qs = parse_qs(parsed.query)
            raw = (qs.get("path") or [""])[0]
            f = Path(raw)
            if not raw or not f.is_file() or f.suffix.lower() not in C.VIDEO_EXTS:
                self._send(400, "bad path", "text/plain; charset=utf-8")
                return
            try:
                tsec = float((qs.get("t") or ["2"])[0])
            except Exception:
                tsec = 2.0
            try:
                data = extract_frame(f, tsec)
                self._send(200, data, "image/jpeg")
            except Exception as e:
                self._send(500, str(e), "text/plain; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif path == "/api/config":
            cfg = C.load_config()
            st = C.config_status()
            self._json({"ffmpeg_input": cfg.get("ffmpeg", ""),
                        "ffprobe_input": cfg.get("ffprobe", ""), **st})
        elif path == "/api/ping":
            self._json({"app": C.APP_ID, "version": C.APP_VERSION,
                        "pid": os.getpid()})
        elif path == "/api/queue":
            self._json(QUEUE.snapshot())
        elif path == "/api/task":
            qs = parse_qs(parsed.query)
            tid = (qs.get("id") or [""])[0]
            if tid == "active":
                tid = QUEUE.active_id
            task = QUEUE.get(tid)
            if not task:
                self._json({"error": "任务不存在"}, 404)
                return
            self._json(task.snapshot(full=True))
        elif path == "/api/status":
            self._json(QUEUE.status_snapshot())
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    # ---------- 辅助 ----------
    @staticmethod
    def _clean_params(body: dict) -> dict:
        keys = ("op", "folder", "recursive", "output", "mode", "encoder",
                "parallel", "crf", "preset", "size", "fps", "trim_head",
                "trim_tail", "chapters", "file_order", "crop",
                "audio_format", "audio_quality")
        return {k: body[k] for k in keys if k in body}

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/api/config":
                new_cfg = {"ffmpeg": str(body.get("ffmpeg", "") or "").strip(),
                           "ffprobe": str(body.get("ffprobe", "") or "").strip()}
                C.apply_config(new_cfg)
                st = C.config_status()
                if not st["ready"]:
                    errors = []
                    if not st["ffmpeg_ok"]:
                        errors.append(f"ffmpeg: {st['ffmpeg_version']}")
                    if not st["ffprobe_ok"]:
                        errors.append(f"ffprobe: {st['ffprobe_version']}")
                    self._json({"error": "验证失败 · " + " · ".join(errors),
                                "ffmpeg_input": new_cfg["ffmpeg"],
                                "ffprobe_input": new_cfg["ffprobe"], **st}, 400)
                    return
                C.save_config(new_cfg)
                self._json({"ok": True, "ffmpeg_input": new_cfg["ffmpeg"],
                            "ffprobe_input": new_cfg["ffprobe"], **st})

            elif path == "/api/pick-exe":
                which = str(body.get("which", "ffmpeg"))
                title = "选择 ffmpeg.exe" if which == "ffmpeg" else "选择 ffprobe.exe"
                r = pick_exe_dialog(title)
                if not r["ok"]:
                    self._json({"error": r["error"]}, 500)
                    return
                self._json({"path": r["path"]})

            elif path == "/api/pick-folder":
                r = pick_folder_dialog(str(body.get("title") or "选择文件夹"))
                if r["ok"]:
                    self._json({"path": r["path"]})
                else:
                    self._json({"error": r["error"]}, 500)

            elif path == "/api/scan":
                if not C.FFMPEG or not C.FFPROBE:
                    self._json({"error": "尚未设置 ffmpeg/ffprobe 路径"}, 400)
                    return
                folder = str(body.get("folder", "")).strip()
                recursive = bool(body.get("recursive"))
                out_dir = str(body.get("out_dir", "") or "").strip()
                if not folder:
                    self._json({"error": "请填写源文件夹路径"}, 400)
                    return
                try:
                    target = compute_output_path(folder, out_dir)
                except Exception:
                    target = None
                self._json(scan_folder(folder, recursive, exclude_path=target))

            elif path == "/api/detect-crop":
                if not C.FFMPEG or not C.FFPROBE:
                    self._json({"error": "尚未设置 ffmpeg/ffprobe 路径"}, 400)
                    return
                raw = str(body.get("path") or "").strip()
                folder = str(body.get("folder") or "").strip()
                target = Path(raw) if raw else None
                if target is None:
                    if not folder:
                        self._json({"error": "缺少 folder 或 path"}, 400)
                        return
                    _, files = list_video_files(folder, False)
                    if not files:
                        self._json({"error": "文件夹里没有视频文件"}, 400)
                        return
                    target = files[0]
                if not target.is_file():
                    self._json({"error": "文件不存在"}, 400)
                    return
                self._json(detect_crop(target))

            elif path in ("/api/queue/add", "/api/merge"):
                if not C.FFMPEG or not C.FFPROBE:
                    self._json({"error": "尚未设置 ffmpeg/ffprobe 路径"}, 400)
                    return
                params = self._clean_params(body)
                folder = str(params.get("folder", "")).strip()
                if not folder:
                    self._json({"error": "请填写源文件夹路径"}, 400)
                    return
                if not Path(folder).expanduser().is_dir():
                    self._json({"error": f"文件夹不存在：{folder}"}, 400)
                    return
                task = QUEUE.add(params, str(body.get("name") or "").strip())
                self._json({"ok": True, "task": task})

            elif path == "/api/queue/batch":
                if not C.FFMPEG or not C.FFPROBE:
                    self._json({"error": "尚未设置 ffmpeg/ffprobe 路径"}, 400)
                    return
                parent = str(body.get("parent") or body.get("folder") or "").strip()
                if not parent:
                    self._json({"error": "请填写父文件夹路径"}, 400)
                    return
                params = self._clean_params(body)
                params.pop("folder", None)
                params["recursive"] = False
                self._json({"ok": True, **QUEUE.batch_add(parent, params)})

            elif path == "/api/queue/cancel":
                self._json({"ok": QUEUE.cancel(str(body.get("id", "")))})

            elif path == "/api/queue/retry":
                self._json({"ok": QUEUE.retry(str(body.get("id", "")))})

            elif path == "/api/queue/remove":
                self._json({"ok": QUEUE.remove(str(body.get("id", "")))})

            elif path == "/api/queue/move":
                tid = str(body.get("id", ""))
                self._json({"ok": QUEUE.move(tid, str(body.get("dir", "up")))})

            elif path == "/api/queue/clear":
                removed = QUEUE.clear(str(body.get("scope", "done")))
                self._json({"ok": True, "removed": removed})

            elif path == "/api/queue/pause":
                QUEUE.set_paused(bool(body.get("paused", True)))
                self._json({"ok": True, "paused": QUEUE.paused})

            elif path == "/api/cancel":
                ok = QUEUE.cancel(QUEUE.active_id) if QUEUE.active_id else False
                self._json({"ok": ok})

            elif path == "/api/shutdown":
                self._json({"ok": True})
                fn = _SHUTDOWN["fn"]
                if fn:
                    threading.Thread(target=fn, daemon=True).start()

            elif path == "/api/reveal":
                p = Path(str(body.get("path", "")))
                if not str(p) or not p.exists():
                    self._json({"error": "文件不存在"}, 400)
                    return
                reveal_path(p)
                self._json({"ok": True})

            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
