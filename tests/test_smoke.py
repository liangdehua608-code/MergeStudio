"""端到端冒烟测试：需要本机可用的 ffmpeg / ffprobe。

    python -m unittest tests.test_smoke -v

没有配置 ffmpeg 时会自动跳过。测试用 ffmpeg 现场生成测试片，不依赖真实素材。
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from mergestudio import config as C
from mergestudio import encoders
from mergestudio.jobs import merge as merge_mod
from mergestudio.jobs.audio import audio_run
from mergestudio.jobs.merge import job_run
from mergestudio.jobs.split import split_run
from mergestudio.jobs.video import video_run
from mergestudio.task import Task, _TLS
from mergestudio.media import detect_crop, extract_frame, probe_chapters, probe_full

READY = False


def setUpModule():
    global READY
    C.apply_config(C.load_config())
    READY = bool(C.config_status().get("ready"))


def _make_clip(path: Path, seconds: int = 2, letterbox: bool = False):
    """生成一段带黑条台标的测试视频（纯色图案，体积很小）。"""
    size = "640x300" if letterbox else "640x360"
    vf = "pad=640:360:0:60:black" if letterbox else \
         "drawbox=x=0:y=0:w=iw:h=60:color=black@0.9:t=fill"
    cmd = [C.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(cmd, check=True)


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        C.apply_config(C.load_config())
        if not C.config_status().get("ready"):
            raise unittest.SkipTest("未配置 ffmpeg / ffprobe，跳过")
        cls.tmp = tempfile.TemporaryDirectory(prefix="mergestudio-test-")
        cls.folder = Path(cls.tmp.name) / "clips"
        cls.folder.mkdir()
        for i in (1, 2):
            _make_clip(cls.folder / f"p{i}.mp4")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_detect_crop_letterbox(self):
        lb = Path(self.tmp.name) / "lb.mp4"
        if not lb.exists():
            _make_clip(lb, letterbox=True)
        info = detect_crop(lb)
        self.assertTrue(info["ok"])
        self.assertAlmostEqual(info["top"], 60, delta=4)
        self.assertAlmostEqual(info["crop_height"], 300, delta=4)

    def test_merge_with_crop(self):
        out = self.folder / "clips_merged.mp4"
        job_run({
            "op": "merge",
            "folder": str(self.folder),
            "mode": "auto",
            "encoder": "libx264",
            "parallel": 2,
            "crf": 30,
            "preset": "ultrafast",
            "chapters": True,
            "crop": {"enabled": True, "top": 60, "left": 0, "right": 0, "bottom": 0},
        })
        self.assertTrue(out.exists(), "合并输出不存在")
        info = probe_full(out)
        self.assertEqual(info.get("width"), 640)
        self.assertEqual(info.get("height"), 300, "裁剪后高度应为 300")
        self.assertTrue(info.get("acodec"), "裁剪合并后音轨丢失")

    def test_extract_audio(self):
        audio_run({"op": "audio", "folder": str(self.folder), "audio_format": "m4a"})
        files = list(self.folder.glob("*.m4a"))
        self.assertTrue(files, "没有生成 m4a")
        self.assertGreater(files[0].stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()

class ResilienceTest(unittest.TestCase):
    """失败处理：跳过失败集、断点续跑。用 monkeypatch 制造失败，避免依赖坏文件。"""

    @classmethod
    def setUpClass(cls):
        C.apply_config(C.load_config())
        if not C.config_status().get("ready"):
            raise unittest.SkipTest("未配置 ffmpeg / ffprobe，跳过")
        cls.tmp = tempfile.TemporaryDirectory(prefix="mergestudio-res-")
        cls.folder = Path(cls.tmp.name) / "clips"
        cls.folder.mkdir()
        for i in (1, 2, 3):
            _make_clip(cls.folder / f"p{i}.mp4")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, params, tid="TTEST"):
        task = Task(tid, "test", {})
        _TLS.task = task
        try:
            job_run(params)
        finally:
            _TLS.task = None
        return task

    def _params(self, **kw):
        base = {"op": "merge", "folder": str(self.folder), "mode": "encode",
                "encoder": "libx264", "parallel": 1, "crf": 34,
                "preset": "ultrafast", "chapters": True}
        base.update(kw)
        return base

    def _with_failing_p2(self, fn):
        orig_encode = merge_mod.encode_one
        orig_retries = merge_mod.SEGMENT_RETRIES

        def fake(src, dst, *a, **kw):
            if Path(src).name == "p2.mp4":
                raise RuntimeError("模拟转码失败")
            return orig_encode(src, dst, *a, **kw)

        merge_mod.encode_one = fake
        merge_mod.SEGMENT_RETRIES = 0
        try:
            return fn()
        finally:
            merge_mod.encode_one = orig_encode
            merge_mod.SEGMENT_RETRIES = orig_retries

    def test_skip_failed_segment(self):
        task = self._with_failing_p2(
            lambda: self._run(self._params(on_error="skip")))
        self.assertEqual(task.state, "done")
        skipped = (task.report or {}).get("skipped") or []
        self.assertEqual([s["name"] for s in skipped], ["p2.mp4"])
        self.assertTrue(Path(task.output).exists())

    def test_abort_then_resume(self):
        task = self._with_failing_p2(
            lambda: self._run(self._params(on_error="abort")))
        self.assertEqual(task.state, "error")
        self.assertTrue(task.resumable)
        self.assertGreaterEqual(task.resume_count, 2)
        self.assertTrue(Path(task.work_dir).exists(), "失败后应保留中间分段")

        resumed = self._run(self._params(on_error="abort", _resume=True))
        self.assertEqual(resumed.state, "done")
        self.assertTrue(any("复用" in line for line in resumed.logs))


class SplitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        C.apply_config(C.load_config())
        if not C.config_status().get("ready"):
            raise unittest.SkipTest("未配置 ffmpeg / ffprobe，跳过")
        cls.tmp = tempfile.TemporaryDirectory(prefix="mergestudio-split-")
        cls.src = Path(cls.tmp.name) / "long.mp4"
        _make_clip(cls.src, seconds=4)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_split_two_parts(self):
        out = Path(self.tmp.name) / "parts"
        task = Task("TSPLIT", "split", {})
        _TLS.task = task
        try:
            split_run({
                "op": "split", "target": str(self.src), "output": str(out),
                "precise": False,
                "segments": [{"start": 0, "end": 2, "title": "开头"},
                             {"start": 2, "end": 4, "title": "结尾"}],
            })
        finally:
            _TLS.task = None
        parts = sorted(out.glob("*.mp4"))
        self.assertEqual(len(parts), 2, f"应切出 2 段，实际 {parts}")
        self.assertIn("P01", parts[0].name)
        self.assertEqual(task.state, "done")
        self.assertEqual((task.report or {}).get("count"), 2)

class DetectCropTest(unittest.TestCase):
    """黑边检测：带台标/噪点/竖边都要能认出来。"""

    @classmethod
    def setUpClass(cls):
        C.apply_config(C.load_config())
        if not C.config_status().get("ready"):
            raise unittest.SkipTest("未配置 ffmpeg / ffprobe，跳过")
        cls.tmp = tempfile.TemporaryDirectory(prefix="mergestudio-bars-")
        cls.logo = Path(cls.tmp.name) / "logo.mp4"
        _make_clip(cls.logo, letterbox=True)
        # 在顶部黑边里放一个白色台标
        subprocess.run([C.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(cls.logo), "-vf",
                        "drawbox=x=20:y=10:w=60:h=30:color=white:t=fill",
                        "-c:a", "copy", str(cls.logo.with_name("logo2.mp4"))],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_letterbox_with_logo(self):
        info = detect_crop(Path(self.tmp.name) / "logo2.mp4")
        self.assertTrue(info["ok"])
        self.assertAlmostEqual(info["top"], 60, delta=6)
        self.assertAlmostEqual(info["bottom"], 0, delta=6)

    def test_no_bars(self):
        plain = Path(self.tmp.name) / "plain.mp4"
        if not plain.exists():
            subprocess.run([C.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=2",
                            "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", str(plain)], check=True)
        info = detect_crop(plain)
        self.assertFalse(info["ok"], f"无黑边不应报裁剪：{info}")

    def test_extract_frame(self):
        jpg = extract_frame(Path(self.tmp.name) / "logo.mp4", t=1.0)
        self.assertTrue(jpg.startswith(b"\xff\xd8"), "应返回 JPEG")
        self.assertGreater(len(jpg), 1000)


class EncoderTest(unittest.TestCase):
    def test_libx264_available(self):
        C.apply_config(C.load_config())
        if not C.config_status().get("ready"):
            raise unittest.SkipTest("未配置 ffmpeg / ffprobe，跳过")
        st = encoders.status()
        self.assertTrue(st["libx264"]["ok"])
        for key, info in st.items():
            if not info["ok"]:
                self.assertTrue(info["reason"], f"{key} 不可用时应给出原因")


class VideoJobTest(unittest.TestCase):
    """单个视频裁剪：只处理目标文件，输出新文件并保留音轨。"""

    @classmethod
    def setUpClass(cls):
        C.apply_config(C.load_config())
        if not C.config_status().get("ready"):
            raise unittest.SkipTest("未配置 ffmpeg / ffprobe，跳过")
        cls.tmp = tempfile.TemporaryDirectory(prefix="mergestudio-single-")
        cls.src = Path(cls.tmp.name) / "clip.mp4"
        _make_clip(cls.src)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_crop_single_video(self):
        task = Task("TSINGLE", "single", {})
        _TLS.task = task
        try:
            video_run({
                "op": "video", "target": str(self.src),
                "output": str(Path(self.tmp.name) / "out"),
                "crop": {"enabled": True, "top": 60},
                "trim_head": 0.3, "trim_tail": 0.3,
                "encoder": "libx264", "crf": 32, "preset": "ultrafast",
            })
        finally:
            _TLS.task = None
        self.assertEqual(task.state, "done", task.message)
        info = probe_full(Path(task.output))
        self.assertEqual(info.get("width"), 640)
        self.assertEqual(info.get("height"), 300, "应裁掉顶部 60px")
        self.assertTrue(info.get("acodec"), "应保留音轨")

