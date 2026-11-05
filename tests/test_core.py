import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from codex_image_mcp import core  # noqa: E402

FAKE = str(HERE / "fake_codex.py")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "codex_home"
        self.home.mkdir()
        self.logs = self.root / "logs"
        self.logs.mkdir()
        os.environ.update(FAKE_LOG_DIR=str(self.logs), FAKE_MODE="ok", FAKE_DELAY="0")

    def tearDown(self):
        self.tmp.cleanup()

    def calls(self):
        return [json.loads(p.read_text()) for p in sorted(self.logs.glob("*.json"))]

    def gen(self, out, **kw):
        return core.generate("pixel miner", str(out), codex_bin=FAKE, codex_home=str(self.home), **kw)


class GenerateTest(Base):
    def test_copies_generated_png_and_reports_size_and_usage(self):
        out = self.root / "sprites" / "miner.png"
        result = self.gen(out)
        self.assertTrue(out.exists())
        self.assertEqual((result["width"], result["height"]), (48, 32))
        self.assertEqual(result["path"], str(out))
        self.assertTrue(result["thread_id"].startswith("thread-"))
        self.assertEqual(result["usage"]["input_tokens"], 1000)

    def test_runs_codex_headless_lean_and_without_user_config_or_apps(self):
        ref = self.root / "ref.png"
        ref.write_bytes(b"x")
        self.gen(self.root / "o.png", reference_images=[str(ref)])
        [call] = self.calls()
        argv = call["argv"]
        self.assertEqual(argv[0], "exec")
        for flag in ("--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json"):
            self.assertIn(flag, argv)
        self.assertIn("apps", argv[argv.index("--disable") + 1:])
        self.assertIn('model_reasoning_effort="low"', argv)
        self.assertEqual(argv[argv.index("-i") + 1], str(ref))
        self.assertEqual(argv[-1], "-")
        self.assertIn("pixel miner", call["stdin"])
        self.assertNotEqual(Path(call["cwd"]).resolve(), Path.cwd().resolve())

    def test_reasoning_effort_can_be_raised(self):
        self.gen(self.root / "o.png", reasoning_effort="medium")
        self.assertIn('model_reasoning_effort="medium"', self.calls()[0]["argv"])

    def test_refuses_to_overwrite_unless_asked(self):
        out = self.root / "o.png"
        out.write_bytes(b"old")
        with self.assertRaisesRegex(core.CodexImageError, "already exists"):
            self.gen(out)
        self.assertEqual(out.read_bytes(), b"old")
        self.gen(out, overwrite=True)
        self.assertNotEqual(out.read_bytes(), b"old")

    def test_rejects_non_png_output(self):
        with self.assertRaisesRegex(core.CodexImageError, r"\.png"):
            self.gen(self.root / "o.jpg")

    def test_missing_reference_fails_before_running(self):
        with self.assertRaisesRegex(core.CodexImageError, "not found"):
            self.gen(self.root / "o.png", reference_images=[str(self.root / "nope.png")])
        self.assertEqual(self.calls(), [])

    def test_no_image_reports_codex_reply(self):
        os.environ["FAKE_MODE"] = "noimage"
        with self.assertRaisesRegex(core.CodexImageError, "can't call the image tool"):
            self.gen(self.root / "o.png")

    def test_failed_turn_reports_error(self):
        os.environ["FAKE_MODE"] = "fail"
        with self.assertRaisesRegex(core.CodexImageError, "usage limit reached"):
            self.gen(self.root / "o.png")


class PostprocessTest(Base):
    def test_chroma_key_makes_background_transparent_and_keeps_raw(self):
        out = self.root / "o.png"
        result = self.gen(out, chroma_key="#FF00FF")
        img = Image.open(out)
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.getpixel((0, 0))[3], 0)
        self.assertEqual(img.getpixel((12, 14)), (128, 128, 128, 255))
        raw = Path(result["raw_path"])
        self.assertTrue(raw.exists())
        self.assertEqual(Image.open(raw).getpixel((0, 0))[:3], (255, 0, 255))

    def test_trim_crops_to_the_opaque_pixels(self):
        out = self.root / "o.png"
        result = self.gen(out, chroma_key="#FF00FF", trim=True)
        self.assertEqual((result["width"], result["height"]), (8, 8))
        self.assertEqual(Image.open(out).size, (8, 8))

    def test_max_size_downscales_with_nearest_neighbour(self):
        out = self.root / "o.png"
        result = self.gen(out, max_size=24)
        self.assertEqual((result["width"], result["height"]), (24, 16))
        colors = {c for _, c in Image.open(out).convert("RGB").getcolors(1000)}
        self.assertEqual(colors, {(255, 0, 255), (128, 128, 128)})

    def test_despill_neutralises_key_tint_on_the_edge_only(self):
        img = Image.new("RGB", (20, 20), (255, 0, 255))
        for x in range(3, 17):
            for y in range(3, 17):
                img.putpixel((x, y), (130, 40, 130))  # magenta-tinted outline
        for x in range(7, 13):
            for y in range(7, 13):
                img.putpixel((x, y), (70, 40, 120))  # legit purple-blue, deep inside
        path = self.root / "p.png"
        img.save(path)
        core.postprocess(path, chroma_key="#FF00FF")
        out = Image.open(path)
        for edge in ((3, 3), (5, 10)):
            r, g, b, a = out.getpixel(edge)
            self.assertEqual(a, 255)
            self.assertLess(max(r, b) - g, 15)
        self.assertEqual(out.getpixel((10, 10)), (70, 40, 120, 255))

    def test_bad_chroma_colour_fails_before_running(self):
        with self.assertRaisesRegex(core.CodexImageError, "chroma_key"):
            self.gen(self.root / "o.png", chroma_key="magenta-ish")
        self.assertEqual(self.calls(), [])


class BatchTest(Base):
    def test_runs_jobs_concurrently_and_reports_each(self):
        os.environ["FAKE_DELAY"] = "1"
        jobs = [{"prompt": f"coin {i}", "output_path": str(self.root / f"c{i}.png")} for i in range(3)]
        jobs.append({"prompt": "bad", "output_path": str(self.root / "bad.jpg")})
        results = core.generate_batch(jobs, concurrency=3, codex_bin=FAKE, codex_home=str(self.home))
        self.assertEqual(len(results), 4)
        for i in range(3):
            self.assertEqual(results[i]["path"], str(self.root / f"c{i}.png"))
        self.assertIn(".png", results[3]["error"])
        starts = sorted(c["start"] for c in self.calls())
        self.assertLess(starts[-1] - starts[0], 0.9)

    def test_batch_applies_shared_postprocess_options(self):
        jobs = [{"prompt": "coin", "output_path": str(self.root / "c.png")}]
        [r] = core.generate_batch(jobs, codex_bin=FAKE, codex_home=str(self.home), chroma_key="#ff00ff", trim=True)
        self.assertEqual((r["width"], r["height"]), (8, 8))


class LoginTest(unittest.TestCase):
    def test_status_reads_plan_from_id_token(self):
        with tempfile.TemporaryDirectory() as home:
            claims = {"https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
            payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            Path(home, "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": f"h.{payload}.s"}}))
            status = core.login_status(codex_bin=FAKE, codex_home=home)
        self.assertEqual(status["plan"], "plus")
        self.assertIn("Logged in using ChatGPT", status["status"])

    def test_device_login_returns_url_and_code(self):
        info = core.start_device_login(codex_bin=FAKE, wait_seconds=10)
        try:
            self.assertEqual(info["url"], "https://auth.openai.com/codex/device")
            self.assertEqual(info["code"], "ABCD-12345")
        finally:
            core.cancel_device_login()


if __name__ == "__main__":
    unittest.main()
