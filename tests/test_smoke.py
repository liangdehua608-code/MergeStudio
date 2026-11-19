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
from mergestudio.jobs.audio import audio_run
from mergestudio.jobs.merge import job_run
from mergestudio.media import detect_crop, probe_full

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
