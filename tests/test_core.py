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


SKIN, GREEN, BODY = (220, 180, 140, 255), (0, 200, 0, 255), (128, 128, 128, 255)
HEAD_BOX = [6, 0, 14, 6]


class EditTest(Base):
    """A 20x16 sprite on a transparent background: a grey body and a skin-coloured head.
    The fake Codex returns it flattened on magenta, upscaled 8x, shifted by (5, 11) px on a
    bigger canvas, with the head turned green and the body slightly lighter."""

    def setUp(self):
        super().setUp()
        self.sprite = self.sprite_image()
        self.original = self.root / "miner.png"
        self.sprite.save(self.original)
        os.environ["FAKE_IMAGE"] = str(self.fake_result())

    def tearDown(self):
        os.environ.pop("FAKE_IMAGE", None)
        super().tearDown()

    def sprite_image(self, background=(0, 0, 0, 0)):
        img = Image.new("RGBA", (20, 16), background)
        for x in range(4, 16):
            for y in range(6, 16):
                img.putpixel((x, y), BODY)
        for x in range(7, 13):
            for y in range(1, 6):
                img.putpixel((x, y), SKIN)
        return img

    def fake_result(self):
        flat = Image.new("RGBA", self.sprite.size, (255, 0, 255, 255))
        flat.alpha_composite(self.sprite)
        px = flat.load()
        for x in range(20):
            for y in range(16):
                if px[x, y] == SKIN:
                    px[x, y] = GREEN
                elif px[x, y] == BODY:
                    px[x, y] = (140, 140, 140, 255)
        px[6, 3] = (200, 60, 200, 255)  # anti-aliased fringe blended with the magenta background
        canvas = Image.new("RGB", (176, 144), (255, 0, 255))
        canvas.paste(flat.convert("RGB").resize((160, 128), Image.Resampling.NEAREST), (5, 11))
        path = self.root / "fake_result.png"
        canvas.save(path)
        return path

    def edit(self, out=None, **kw):
        out = out or self.root / "edited.png"
        return core.edit(str(self.original), "make the head green", str(out),
                         codex_bin=FAKE, codex_home=str(self.home), **kw), out

    def test_box_edit_changes_only_the_box_and_keeps_the_rest_pixel_identical(self):
        result, out = self.edit(box=HEAD_BOX)
        edited = Image.open(out)
        self.assertEqual(edited.size, (20, 16))
        x0, y0, x1, y1 = HEAD_BOX
        for x in range(20):
            for y in range(16):
                if not (x0 <= x < x1 and y0 <= y < y1):
                    self.assertEqual(edited.getpixel((x, y)), self.sprite.getpixel((x, y)), (x, y))
        self.assertEqual(edited.getpixel((9, 3)), GREEN)
        self.assertEqual(edited.getpixel((6, 0))[3], 0)  # background inside the box stays transparent
        self.assertAlmostEqual(result["alignment"]["scale"], 8, delta=0.5)
        self.assertLess(result["alignment"]["error"], 20)
        json.dumps(result)  # plain JSON types, so the MCP layer can serialise it

    def test_removes_the_background_tint_from_new_edges_inside_the_region(self):
        _, out = self.edit(box=HEAD_BOX)
        r, g, b, a = Image.open(out).getpixel((6, 3))
        self.assertEqual(a, 255)
        self.assertLess(max(r, b) - g, 15)

    def test_mask_image_selects_its_white_pixels(self):
        mask = Image.new("L", (20, 16), 0)
        x0, y0, x1, y1 = HEAD_BOX
        mask.paste(255, (x0, y0, x1, y1))
        mask_path = self.root / "mask.png"
        mask.save(mask_path)
        _, out = self.edit(mask_path=str(mask_path))
        edited = Image.open(out)
        self.assertEqual(edited.getpixel((9, 3)), GREEN)
        self.assertEqual(edited.getpixel((9, 10)), BODY)

    def test_opaque_original_keeps_the_generated_background_inside_the_mask(self):
        self.sprite = self.sprite_image(background=(255, 0, 255, 255))
        self.sprite.save(self.original)
        _, out = self.edit(box=HEAD_BOX)
        self.assertEqual(Image.open(out).getpixel((6, 0)), (255, 0, 255, 255))

    def test_sends_the_original_and_the_mask_as_references(self):
        self.edit(box=HEAD_BOX)
        [call] = self.calls()
        self.assertEqual(call["argv"].count("-i"), 2)
        self.assertIn("make the head green", call["stdin"])
        self.assertIn("mask", call["stdin"].lower())

    def test_refuses_to_overwrite_unless_asked(self):
        out = self.root / "edited.png"
        out.write_bytes(b"old")
        with self.assertRaisesRegex(core.CodexImageError, "already exists"):
            self.edit(out, box=HEAD_BOX)
        self.edit(out, box=HEAD_BOX, overwrite=True)
        self.assertEqual(Image.open(out).getpixel((9, 3)), GREEN)

    def test_invalid_regions_fail_before_running(self):
        bad_mask = self.root / "small_mask.png"
        Image.new("L", (10, 10), 255).save(bad_mask)
        cases = [({}, "mask_path or box"),
                 ({"box": HEAD_BOX, "mask_path": str(bad_mask)}, "mask_path or box"),
                 ({"mask_path": str(bad_mask)}, "size"),
                 ({"box": [0, 0, 30, 5]}, "box"),
                 ({"box": [0, 0, 20, 16]}, "whole image")]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(core.CodexImageError, message):
                self.edit(**kwargs)
        self.assertEqual(self.calls(), [])


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
