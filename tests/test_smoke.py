"""端到端冒烟测试：需要本机可用的 ffmpeg / ffprobe。

    python -m unittest tests.test_smoke -v

没有配置 ffmpeg 时会自动跳过。测试用 ffmpeg 现场生成测试片，不依赖真实素材。
纯逻辑部分（覆盖保护、字幕策略、队列落盘）不需要 ffmpeg，始终会跑。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from mergestudio import config as C
from mergestudio import encoders, subtitles
from mergestudio.jobs import merge as merge_mod
from mergestudio.jobs.audio import audio_run
from mergestudio.jobs.merge import job_run
from mergestudio.jobs.mux import mux_run
from mergestudio.jobs.split import split_run
from mergestudio.jobs.video import video_run
from mergestudio.task import Task, _TLS
from mergestudio.media import (detect_crop, extract_frame, guard_output,
                               output_family, probe_chapters, probe_full,
                               probe_media, probe_output_summary, reveal_path,
                               unique_path)

READY = False


def setUpModule():
    global READY
    C.apply_config(C.load_config())
    READY = bool(C.config_status().get("ready"))


def _make_clip(path: Path, seconds: int = 2, letterbox: bool = False,
               subtitle: bool = False):
    """生成一段带黑条台标的测试视频（纯色图案，体积很小）。"""
    size = "640x300" if letterbox else "640x360"
    vf = "pad=640:360:0:60:black" if letterbox else \
         "drawbox=x=0:y=0:w=iw:h=60:color=black@0.9:t=fill"
    cmd = [C.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    extra = []
    if subtitle:
        srt = path.with_suffix(".srt")
        srt.write_text("1\n00:00:00,000 --> 00:00:10,000\n测试字幕\n",
                       encoding="utf-8")
        cmd += ["-i", str(srt)]
        extra = ["-c:s", "mov_text"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"] + extra + [str(path)]
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
        self.assertEqual(task.state, "done",
                         f"{task.error}\n" + "\n".join(task.logs[-12:]))
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

class VideoBatchCropTest(unittest.TestCase):
    """批量裁剪：每个视频各自输出，不合并；重复跑不把上次成品当素材。"""

    @classmethod
    def setUpClass(cls):
        C.apply_config(C.load_config())
        if not C.config_status().get("ready"):
            raise unittest.SkipTest("未配置 ffmpeg / ffprobe，跳过")
        cls.tmp = tempfile.TemporaryDirectory(prefix="mergestudio-batch-")
        cls.folder = Path(cls.tmp.name) / "clips"
        cls.folder.mkdir()
        for i in (1, 2, 3):
            _make_clip(cls.folder / f"p{i}.mp4")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, tid):
        task = Task(tid, "batch", {})
        _TLS.task = task
        try:
            video_run({
                "op": "video", "folder": str(self.folder),
                "crop": {"enabled": True, "top": 60},
                "encoder": "libx264", "crf": 32, "preset": "ultrafast",
                "parallel": 2, "on_error": "abort",
            })
        finally:
            _TLS.task = None
        return task

    def test_batch_crops_each_video_without_merging(self):
        task = self._run("TBATCH1")
        self.assertEqual(task.state, "done",
                         f"{task.error}\n" + "\n".join(task.logs[-10:]))
        self.assertEqual((task.report or {}).get("count"), 3)
        self.assertEqual((task.report or {}).get("mode"), "batch")
        outs = sorted(self.folder.glob("*_cropped.mp4"))
        self.assertEqual(len(outs), 3, "应逐个输出 3 个裁剪文件，不合并")
        for p in outs:
            info = probe_full(p)
            self.assertEqual(info.get("height"), 300, f"{p.name} 裁剪高度不对")
            self.assertTrue(info.get("acodec"), f"{p.name} 音轨丢失")

    def test_second_run_renames_and_skips_own_outputs(self):
        task = self._run("TBATCH2")
        self.assertEqual(task.state, "done", task.error)
        # 上次生成的 *_cropped.mp4 不该被当成素材，所以仍是 3 个
        self.assertEqual(task.total_files, 3, "上次成品被当成素材了")
        self.assertEqual(len(sorted(self.folder.glob("*_cropped_2.mp4"))), 3)


class AudioSingleAndMuxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        C.apply_config(C.load_config())
        if not C.config_status().get("ready"):
            raise unittest.SkipTest("未配置 ffmpeg / ffprobe，跳过")
        cls.tmp = tempfile.TemporaryDirectory(prefix="mergestudio-mux-")
        cls.video = Path(cls.tmp.name) / "clip.mp4"
        _make_clip(cls.video, seconds=2)
        cls.audio = Path(cls.tmp.name) / "bgm.mp3"
        subprocess.run([C.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
                        "-c:a", "libmp3lame", "-q:a", "4", str(cls.audio)],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_audio_from_single_file(self):
        task = Task("TAUDIO1", "audio", {})
        _TLS.task = task
        try:
            audio_run({"op": "audio", "target": str(self.video),
                       "audio_format": "m4a"})
        finally:
            _TLS.task = None
        self.assertEqual(task.state, "done", task.message)
        out = self.video.with_suffix(".m4a")
        self.assertTrue(out.exists(), "单文件未生成 m4a")
        self.assertGreater(out.stat().st_size, 0)

    def test_mux_replace(self):
        task = Task("TMUX", "mux", {})
        _TLS.task = task
        try:
            mux_run({"op": "mux", "video": str(self.video),
                     "audio": str(self.audio),
                     "output": str(Path(self.tmp.name) / "muxed"),
                     "mode": "replace", "fit": "shortest"})
        finally:
            _TLS.task = None
        self.assertEqual(task.state, "done", task.message)
        info = probe_media(Path(task.output))
        self.assertTrue(info.get("vcodec"))
        self.assertTrue(info.get("acodec"), "合并后应有音轨")
        self.assertAlmostEqual(info.get("duration", 0), 2.0, delta=0.4)

    def test_reveal_command_uses_select_quotes(self):
        calls = []

        class Fake:
            def __init__(self, cmd, *a, **kw):
                calls.append(cmd)

        old_popen = subprocess.Popen
        subprocess.Popen = Fake
        try:
            reveal_path(self.video)
        finally:
            subprocess.Popen = old_popen
        self.assertTrue(calls and calls[0].startswith('explorer /select,"'),
                        f"定位命令不对：{calls}")



class OutputGuardTest(unittest.TestCase):
    """覆盖保护：不覆盖已有内容，也绝不把输出写到源文件上。"""

    def test_unique_path_never_reuses_name(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "a_merged.mp4"
            base.write_bytes(b"x")
            (Path(d) / "a_merged_2.mp4").write_bytes(b"x")
            self.assertEqual(unique_path(base).name, "a_merged_3.mp4")

    def test_existing_output_is_renamed(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "a_merged.mp4"
            out.write_bytes(b"old")
            final, note = guard_output(out, sources=[])
            self.assertEqual(final.name, "a_merged_2.mp4")
            self.assertIn("改名", note)
            self.assertEqual(out.read_bytes(), b"old", "旧文件不能被改动")

    def test_overwrite_switch(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "a.mp4"
            out.write_bytes(b"old")
            final, note = guard_output(out, sources=[], overwrite=True)
            self.assertEqual(final, out)
            self.assertIn("覆盖", note)

    def test_output_same_as_source_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.mp4"
            src.write_bytes(b"src")
            with self.assertRaises(RuntimeError):
                guard_output(src, sources=[src])

    def test_family_covers_previous_runs(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("x_merged.mp4", "x_merged_2.mp4", "x_merged_extra.mp4"):
                (Path(d) / name).write_bytes(b"x")
            names = sorted(p.name for p in output_family(Path(d) / "x_merged.mp4"))
            self.assertEqual(names, ["x_merged.mp4", "x_merged_2.mp4"])


class EncoderDiagnosisTest(unittest.TestCase):
    """编码器报错的翻译：老版本 ffmpeg 的 `-nostdin` 报错必须指向 ffmpeg 本身。"""

    def test_old_ffmpeg_option_error_points_to_ffmpeg(self):
        msg = encoders._reason(
            "libx264", "Failed to set value '-f' for option 'nostdin': Option not found")
        self.assertIn("ffmpeg", msg)
        self.assertIn("5.0", msg, "应该告诉用户需要 5.0+")

    def test_probe_cmd_avoids_nostdin(self):
        captured = {}

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = cmd
            captured["kw"] = kw
            return FakeResult()

        old_run = encoders.subprocess.run
        encoders.subprocess.run = fake_run
        try:
            st = encoders.probe("libx264")
        finally:
            encoders.subprocess.run = old_run
        self.assertTrue(st["ok"])
        self.assertNotIn("-nostdin", captured["cmd"],
                         "老版本 ffmpeg 不认 -nostdin，会把后面的参数吃掉")
        self.assertEqual(captured["kw"].get("stdin"), subprocess.DEVNULL)

    def test_unavailable_msg_does_not_misguide(self):
        old_status = encoders.status
        encoders.status = lambda force=False: {
            e: {"ok": False, "reason": "这台机器上的 ffmpeg 是「ffmpeg version 2.8」"} 
            for e in encoders.PROFILES}
        try:
            msg = encoders.unavailable_msg("libx264")
        finally:
            encoders.status = old_status
        self.assertIn("ffmpeg", msg)
        self.assertNotIn("改用 libx264", msg, "ffmpeg 本身坏了，别再让用户换编码器")


class SubtitlePlanTest(unittest.TestCase):
    """字幕保留策略：三种情况都必须是「宁可丢字幕，不拼坏文件」。"""

    def test_uniform_text_subtitle_kept(self):
        plan = subtitles.plan([["mov_text"], ["mov_text"]], ".mp4")
        self.assertTrue(plan["keep"])
        self.assertEqual(plan["encode"], "mov_text")
        self.assertIn("-map", subtitles.map_args(plan))

    def test_missing_subtitle_dropped(self):
        plan = subtitles.plan([["mov_text"], []], ".mp4")
        self.assertFalse(plan["keep"])
        self.assertTrue(plan["note"])
        self.assertEqual(subtitles.map_args(plan), [])

    def test_bitmap_subtitle_dropped(self):
        plan = subtitles.plan([["hdmv_pgs_subtitle"]], ".mkv")
        self.assertFalse(plan["keep"])
        self.assertIn("位图", plan["note"])

    def test_mkv_keeps_original_codec(self):
        plan = subtitles.plan([["subrip"], ["subrip"]], ".mkv")
        self.assertTrue(plan["keep"])
        self.assertEqual(plan["encode"], "copy")


class QueueStateTest(unittest.TestCase):
    """队列持久化：排队中的任务要能活过重启，跑一半的要退回排队并标记续跑。"""

    def test_restore_after_restart(self):
        from mergestudio.queue import TaskQueue

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "queue.json"
            q = TaskQueue(state_file=state)
            q.add({"op": "merge", "folder": d}, "第一个任务")
            q.add({"op": "audio", "folder": d}, "第二个任务")
            q.paused = True
            # 模拟第二个任务正在跑（进程被杀时就是这个状态）
            q.tasks[1].state = "running"
            q.tasks[1].started_at = time.time()
            q.save()
            self.assertTrue(state.is_file())

            data = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(len(data["tasks"]), 2)
            self.assertTrue(data["paused"])

            q2 = TaskQueue(state_file=state)
            self.assertEqual([t.id for t in q2.tasks], ["T001", "T002"])
            self.assertTrue(q2.paused, "暂停状态也要恢复")
            self.assertEqual(q2.tasks[0].state, "queued")
            self.assertEqual(q2.tasks[1].state, "queued",
                             "跑一半的任务重启后应重新排队")
            self.assertTrue(q2.tasks[1].params.get("_resume"),
                            "重新排队时应带上续跑标记")
            self.assertEqual(q2.tasks[1].started_at, 0.0)

            # 序号接着往下走，不能和已有 ID 撞车
            q2.seq = 2
            self.assertEqual(q2.add({"op": "merge", "folder": d})["id"], "T003")


class SubtitleMergeTest(unittest.TestCase):
    """字幕轨真的能被拼进成品（不只是参数拼对了）。"""

    @classmethod
    def setUpClass(cls):
        C.apply_config(C.load_config())
        if not C.config_status().get("ready"):
            raise unittest.SkipTest("未配置 ffmpeg / ffprobe，跳过")
        cls.tmp = tempfile.TemporaryDirectory(prefix="mergestudio-sub-")
        cls.folder = Path(cls.tmp.name) / "sub"
        cls.folder.mkdir()
        for i in (1, 2):
            _make_clip(cls.folder / f"s{i}.mp4", subtitle=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_merge_keeps_subtitle_track(self):
        task = Task("TSUB", "sub", {})
        _TLS.task = task
        try:
            job_run({"op": "merge", "folder": str(self.folder), "mode": "auto",
                     "encoder": "libx264", "parallel": 1, "crf": 32,
                     "preset": "ultrafast", "chapters": True})
        finally:
            _TLS.task = None
        self.assertEqual(task.state, "done", f"{task.error}\n" + "\n".join(task.logs[-10:]))
        info = probe_output_summary(Path(task.output))
        self.assertEqual(info.get("subtitles"), 1, "成品应保留 1 条字幕轨")
        self.assertEqual((task.report or {}).get("subtitles"), 1)


if __name__ == "__main__":
    unittest.main()
