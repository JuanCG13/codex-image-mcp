"""Generate images through the official Codex CLI, billed to the signed-in ChatGPT plan.

Codex saves each image it generates under $CODEX_HOME/generated_images/<thread_id>/.
This module runs `codex exec` headless, finds that image, optionally keys out a flat
chroma background, trims and downscales it, and writes it where asked.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

DEFAULT_TIMEOUT = 600
DEFAULT_TOLERANCE = 60
REASONING_EFFORTS = ("minimal", "low", "medium", "high")
# Apps would route the request to connectors such as Canva, and the user config may pin a
# model ChatGPT accounts cannot use. Both are switched off for a predictable, lean run.
DISABLED_FEATURES = ("apps", "plugins", "browser_use", "multi_agent")

PROMPT_TEMPLATE = """Generate exactly one image with your built-in image generation tool.
Do not run shell commands, read files or write files; just generate the image.
{refs}
Image request:
{prompt}

When the image is generated, reply only with DONE."""


class CodexImageError(RuntimeError):
    """Raised when Codex cannot produce the requested image."""


def resolve_home(override: str | None = None) -> Path:
    return Path(override or os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def resolve_bin(override: str | None = None) -> str:
    return override or os.environ.get("CODEX_BIN") or "codex"


def parse_color(value: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if not m:
        raise CodexImageError(f"chroma_key must be a hex colour like #FF00FF, got {value!r}")
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


DESPILL_RING = 3  # px from the transparent area, at the generator's full resolution


def _despill_edges(img: Image.Image, key: tuple[int, int, int]) -> None:
    """Remove the key colour's tint from the pixels bordering the keyed-out background.

    Anti-aliased outlines blend into the background colour and keep a halo after keying.
    Only a thin ring next to transparency is touched, so legitimate colours inside the
    subject survive. The tint is the pixel's chroma along the key's chroma direction."""
    px = img.load()
    w, h = img.size
    kg = sum(key) / 3
    d = [key[0] - kg, key[1] - kg, key[2] - kg]
    norm = sum(c * c for c in d) ** 0.5
    if norm == 0:
        return  # a grey key has no tint to remove
    d = [c / norm for c in d]
    ring = {(x, y) for y in range(h) for x in range(w) if px[x, y][3] == 0}
    seen = set(ring)
    for _ in range(DESPILL_RING):
        nxt = set()
        for x, y in ring:
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < w and 0 <= ny < h and (nx, ny) not in seen and px[nx, ny][3]:
                    seen.add((nx, ny))
                    nxt.add((nx, ny))
        for x, y in nxt:
            r, g, b, a = px[x, y]
            grey = (r + g + b) / 3
            spill = (r - grey) * d[0] + (g - grey) * d[1] + (b - grey) * d[2]
            if spill > 0:
                px[x, y] = tuple(max(0, min(255, round(v - spill * dc)))
                                 for v, dc in zip((r, g, b), d)) + (a,)
        ring = nxt


def postprocess(path: Path, chroma_key: str | None = None, tolerance: int = DEFAULT_TOLERANCE,
                trim: bool = False, max_size: int | None = None) -> None:
    """Key out a flat background colour (and its halo), crop to the subject and downscale, in place."""
    img = Image.open(path).convert("RGBA")
    if chroma_key:
        key = parse_color(chroma_key)
        limit = tolerance * tolerance
        px = img.load()
        for y in range(img.height):
            for x in range(img.width):
                r, g, b, a = px[x, y]
                if (r - key[0]) ** 2 + (g - key[1]) ** 2 + (b - key[2]) ** 2 <= limit:
                    px[x, y] = (0, 0, 0, 0)
        _despill_edges(img, key)
    if trim:
        box = img.getchannel("A").getbbox()
        if box:
            img = img.crop(box)
    if max_size and max(img.size) > max_size:
        scale = max_size / max(img.size)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.Resampling.NEAREST)
    img.save(path)


def _run_codex(text: str, refs: list[Path], timeout: int, reasoning_effort: str,
               codex_bin: str | None, codex_home: str | None) -> tuple[list[Path], str, dict]:
    """Run one headless Codex turn and return the images it saved, oldest first."""
    home = resolve_home(codex_home)
    cmd = [resolve_bin(codex_bin), "exec", "--skip-git-repo-check", "--ignore-rules",
           "--ignore-user-config", "-s", "read-only", "--json",
           "-c", f'model_reasoning_effort="{reasoning_effort}"']
    for feature in DISABLED_FEATURES:
        cmd += ["--disable", feature]
    for ref in refs:
        cmd += ["-i", str(ref)]
    cmd.append("-")  # prompt on stdin, so -i cannot swallow it

    env = dict(os.environ, CODEX_HOME=str(home))
    with tempfile.TemporaryDirectory(prefix="codex-image-") as work:
        try:
            run = subprocess.run(cmd, input=text, capture_output=True, text=True,
                                 timeout=timeout, cwd=work, env=env)
        except FileNotFoundError as e:
            raise CodexImageError(f"codex CLI not found ({cmd[0]}); install it or set CODEX_BIN") from e
        except subprocess.TimeoutExpired as e:
            raise CodexImageError(f"codex timed out after {timeout}s") from e

    thread_id, errors, replies, usage = None, [], [], {}
    for line in run.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "turn.failed":
            errors.append((event.get("error") or {}).get("message", "turn failed"))
        elif kind == "error":
            errors.append(event.get("message", "error"))
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
        elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
            replies.append(event["item"].get("text", ""))

    images = sorted((home / "generated_images" / thread_id).glob("*.png"),
                    key=lambda p: p.stat().st_mtime) if thread_id else []
    if not images:
        detail = "; ".join(errors) or (replies[-1] if replies else run.stderr.strip()[-500:])
        raise CodexImageError(f"Codex finished without generating an image: {detail}")
    return images, thread_id, usage


def generate(
    prompt: str,
    output_path: str,
    reference_images: list[str] | tuple[str, ...] = (),
    overwrite: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    chroma_key: str | None = None,
    tolerance: int = DEFAULT_TOLERANCE,
    trim: bool = False,
    max_size: int | None = None,
    reasoning_effort: str = "low",
    codex_bin: str | None = None,
    codex_home: str | None = None,
) -> dict:
    out = Path(output_path).expanduser().resolve()
    if out.suffix.lower() != ".png":
        raise CodexImageError(f"output_path must end in .png: {out}")
    if out.exists() and not overwrite:
        raise CodexImageError(f"{out} already exists; pass overwrite=true to replace it")
    refs = [Path(r).expanduser().resolve() for r in reference_images]
    for ref in refs:
        if not ref.is_file():
            raise CodexImageError(f"reference image not found: {ref}")
    if chroma_key:
        parse_color(chroma_key)
    if reasoning_effort not in REASONING_EFFORTS:
        raise CodexImageError(f"reasoning_effort must be one of {REASONING_EFFORTS}")

    ref_note = ("The attached image(s) are references; follow them for character design, "
                "palette and style.\n") if refs else ""
    text = PROMPT_TEMPLATE.format(refs=ref_note, prompt=prompt.strip())
    images, thread_id, usage = _run_codex(text, refs, timeout, reasoning_effort, codex_bin, codex_home)

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(images[-1], out)
    if chroma_key or trim or max_size:
        postprocess(out, chroma_key, tolerance, trim, max_size)
    with Image.open(out) as img:
        width, height = img.size
    return {"path": str(out), "width": width, "height": height, "thread_id": thread_id,
            "raw_path": str(images[-1]), "images_generated": len(images), "usage": usage}


EDIT_PROMPT = """Edit the first attached image with your built-in image generation tool and output exactly one image.
The second attached image is a mask of the same picture: white marks the only area to change,
black marks the areas that must stay exactly as they are.
Keep the same framing, scale, position, pose, style and palette, and the flat {background} background.
Redraw only what the edit needs inside the white area.
Do not run shell commands, read files or write files.

Edit request:
{prompt}

When the image is generated, reply only with DONE."""

REFERENCE_MIN_SIDE = 512  # small sprites are upscaled (nearest) so the model sees them clearly
ALIGN_SIDE = 48           # coarse alignment search grid, in px on the longest side
REFINE_SIDE = 256         # fine alignment grid
ALIGN_WARNING = 30        # mean RGB difference outside the mask that suggests a bad match


def _edit_region(size: tuple[int, int], mask_path: str | None, box) -> np.ndarray:
    """Boolean array (height x width), True where the edit may change pixels."""
    if (mask_path is None) == (box is None):
        raise CodexImageError("pass exactly one of mask_path or box")
    w, h = size
    if box is not None:
        try:
            x0, y0, x1, y1 = (int(v) for v in box)
        except (TypeError, ValueError) as e:
            raise CodexImageError(f"box must be [x0, y0, x1, y1], got {box!r}") from e
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            raise CodexImageError(f"box {list(box)} must satisfy 0 <= x0 < x1 <= {w} and 0 <= y0 < y1 <= {h}")
        region = np.zeros((h, w), dtype=bool)
        region[y0:y1, x0:x1] = True
    else:
        path = Path(mask_path).expanduser().resolve()
        if not path.is_file():
            raise CodexImageError(f"mask not found: {path}")
        with Image.open(path) as m:
            mask = m.convert("RGBA")
        if mask.size != size:
            raise CodexImageError(f"mask size {mask.size} differs from image size {size}")
        flat = Image.new("RGBA", mask.size, (0, 0, 0, 255))
        flat.alpha_composite(mask)
        region = np.asarray(flat.convert("L")) >= 128
    if not region.any():
        raise CodexImageError("the mask selects no pixels")
    if region.all():
        raise CodexImageError("the mask covers the whole image; use generate_image instead")
    return region


def _sample(gen: np.ndarray, f: float, dx: float, dy: float, xs: np.ndarray, ys: np.ndarray):
    """Nearest-sample `gen` at original-image points (xs, ys), where original = gen * f + (dx, dy)."""
    gh, gw = gen.shape[:2]
    gx = np.floor((xs - dx) / f).astype(np.int64)
    gy = np.floor((ys - dy) / f).astype(np.int64)
    valid = ((gy >= 0) & (gy < gh))[:, None] & ((gx >= 0) & (gx < gw))[None, :]
    return gen[np.clip(gy, 0, gh - 1)][:, np.clip(gx, 0, gw - 1)], valid


def _align(gen: np.ndarray, orig: np.ndarray, keep: np.ndarray) -> tuple[float, float, float, float]:
    """Find the scale and offset that best overlay `gen` on `orig` where `keep` is True.

    Coarse-to-fine search; the cost is the mean RGB difference over the kept pixels, with
    pixels falling outside the generated image counted as fully different.
    Returns (f, dx, dy, error): original = generated * f + (dx, dy)."""
    h, w = orig.shape[:2]
    gh, gw = gen.shape[:2]

    def grid(side):
        r = min(1.0, side / max(w, h))
        cw, ch = max(1, round(w * r)), max(1, round(h * r))
        xs = (np.arange(cw) + 0.5) * w / cw
        ys = (np.arange(ch) + 0.5) * h / ch
        ix, iy = np.minimum(xs.astype(int), w - 1), np.minimum(ys.astype(int), h - 1)
        return xs, ys, orig[iy][:, ix], keep[iy][:, ix], w / cw

    def cost(f, dx, dy, xs, ys, target, kept):
        sample, valid = _sample(gen, f, dx, dy, xs, ys)
        diff = np.where(valid, np.abs(sample - target).mean(axis=2), 255.0)
        return float(diff[kept].mean())

    def search(scales, shifts_x, shifts_y, centred, g):
        best = (math.inf, 0.0, 0.0, 0.0)
        for f in scales:
            cx, cy = ((w - gw * f) / 2, (h - gh * f) / 2) if centred else (0.0, 0.0)
            for dy in shifts_y:
                for dx in shifts_x:
                    c = cost(f, cx + dx, cy + dy, *g[:4])
                    if c < best[0]:
                        best = (c, f, cx + dx, cy + dy)
        return best

    # Coarse: the model usually centres the picture, so search shifts around the centred fit.
    lo, hi = min(w / gw, h / gh) * 0.85, max(w / gw, h / gh) * 1.15
    scales = np.geomspace(lo, hi, max(2, math.ceil(math.log(hi / lo) / math.log(1.01)) + 1))
    g = grid(ALIGN_SIDE)
    step = g[4]
    span_x, span_y = np.arange(-0.15 * w, 0.15 * w + step, step), np.arange(-0.15 * h, 0.15 * h + step, step)
    _, f, dx, dy = search(scales, span_x, span_y, True, g)

    # Fine: a narrow window around the coarse result, on a denser grid.
    g = grid(REFINE_SIDE)
    window = np.linspace(-step, step, 9)
    err, f, dx, dy = search(f * np.linspace(0.98, 1.02, 9), dx + window, dy + window, False, g)
    return f, dx, dy, err


def edit(
    image_path: str,
    prompt: str,
    output_path: str,
    mask_path: str | None = None,
    box: list[int] | tuple[int, int, int, int] | None = None,
    overwrite: bool = False,
    background: str = "#FF00FF",
    tolerance: int = DEFAULT_TOLERANCE,
    timeout: int = DEFAULT_TIMEOUT,
    reasoning_effort: str = "low",
    codex_bin: str | None = None,
    codex_home: str | None = None,
) -> dict:
    """Edit only a region of an image; every pixel outside it stays identical to the original.

    Codex redraws the whole picture from the original and a mask. The result is aligned
    back onto the original, and only the pixels inside the region are taken from it."""
    src = Path(image_path).expanduser().resolve()
    if not src.is_file():
        raise CodexImageError(f"image not found: {src}")
    out = Path(output_path).expanduser().resolve()
    if out.suffix.lower() != ".png":
        raise CodexImageError(f"output_path must end in .png: {out}")
    if out.exists() and not overwrite:
        raise CodexImageError(f"{out} already exists; pass overwrite=true to replace it")
    if reasoning_effort not in REASONING_EFFORTS:
        raise CodexImageError(f"reasoning_effort must be one of {REASONING_EFFORTS}")
    bg = parse_color(background)
    with Image.open(src) as im:
        original = im.convert("RGBA")
    region = _edit_region(original.size, mask_path, box)

    flat = Image.new("RGBA", original.size, bg + (255,))
    flat.alpha_composite(original)
    flat = flat.convert("RGB")
    w, h = original.size
    k = max(1, math.ceil(REFERENCE_MIN_SIDE / max(w, h)))
    text = EDIT_PROMPT.format(background="#" + background.strip().lstrip("#").upper(),
                              prompt=prompt.strip())
    with tempfile.TemporaryDirectory(prefix="codex-image-edit-") as tmp:
        ref, mask_ref = Path(tmp, "original.png"), Path(tmp, "mask.png")
        flat.resize((w * k, h * k), Image.Resampling.NEAREST).save(ref)
        Image.fromarray((region * 255).astype(np.uint8)).resize(
            (w * k, h * k), Image.Resampling.NEAREST).save(mask_ref)
        images, thread_id, usage = _run_codex(text, [ref, mask_ref], timeout, reasoning_effort,
                                              codex_bin, codex_home)

    with Image.open(images[-1]) as gi:
        gen = np.asarray(gi.convert("RGB"), dtype=np.float32)
    target = np.asarray(flat, dtype=np.float32)
    f, dx, dy, err = _align(gen, target, ~region)

    sample, valid = _sample(gen, f, dx, dy, np.arange(w) + 0.5, np.arange(h) + 0.5)
    result = np.asarray(original).copy()
    take = region & valid
    result[take, :3] = sample[take].round().astype(np.uint8)
    result[take, 3] = 255
    if (result[..., 3] < 255).any() or (np.asarray(original)[..., 3] < 255).any():
        # The original has a transparent background: key the background colour back out.
        dist = np.sqrt(((sample - np.array(bg, dtype=np.float32)) ** 2).sum(axis=2))
        result[take & (dist <= tolerance)] = 0
        # Remove the background's tint from new anti-aliased edges, only inside the region.
        despilled = Image.fromarray(result, "RGBA").copy()
        _despill_edges(despilled, bg)
        result[take] = np.asarray(despilled)[take]

    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(result, "RGBA").save(out)
    report = {"path": str(out), "width": w, "height": h, "thread_id": thread_id,
              "raw_path": str(images[-1]), "pixels_changed": int(take.sum()), "usage": usage,
              "alignment": {"scale": round(float(1 / f), 3),
                            "origin": [round(float(-dx / f), 1), round(float(-dy / f), 1)],
                            "error": round(float(err), 1)}}
    if err > ALIGN_WARNING:
        report["warning"] = (f"the generated image matched the original poorly outside the mask "
                             f"(mean RGB difference {err:.0f}); check the result or retry")
    return report


def generate_batch(jobs: list[dict], concurrency: int = 3, **shared) -> list[dict]:
    """Run several generate() jobs in parallel. Each job is a dict of generate() arguments;
    `shared` arguments apply to every job unless the job overrides them.
    Results come back in job order; a failed job yields {"error": ...}."""
    def run(job: dict) -> dict:
        try:
            return generate(**{**shared, **job})
        except (CodexImageError, TypeError) as e:
            return {"error": str(e), "output_path": job.get("output_path")}

    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(jobs) or 1))) as pool:
        return list(pool.map(run, jobs))


def login_status(codex_bin: str | None = None, codex_home: str | None = None) -> dict:
    home = resolve_home(codex_home)
    env = dict(os.environ, CODEX_HOME=str(home))
    try:
        run = subprocess.run([resolve_bin(codex_bin), "login", "status"],
                             capture_output=True, text=True, timeout=30, env=env)
        status = (run.stdout + run.stderr).strip()
    except FileNotFoundError:
        status = "codex CLI not found"
    plan = None
    try:
        token = json.loads((home / "auth.json").read_text())["tokens"]["id_token"]
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        plan = claims.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type")
    except (OSError, KeyError, ValueError, IndexError):
        pass
    return {"status": status, "plan": plan}


_login_proc: subprocess.Popen | None = None
URL_RE = re.compile(r"https://\S+")
CODE_RE = re.compile(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b")


def start_device_login(codex_bin: str | None = None, wait_seconds: int = 30) -> dict:
    """Start `codex login --device-auth` and return its URL and one-time code.

    The process keeps running in the background until the user approves the code.
    """
    global _login_proc
    cancel_device_login()
    _login_proc = subprocess.Popen([resolve_bin(codex_bin), "login", "--device-auth"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    found: dict = {}

    def reader(proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            lines.append(line)
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
            if "url" not in found and (m := URL_RE.search(clean)):
                found["url"] = m.group(0)
            if "code" not in found and (m := CODE_RE.search(clean)):
                found["code"] = m.group(0)

    threading.Thread(target=reader, args=(_login_proc,), daemon=True).start()
    deadline = time.time() + wait_seconds
    while time.time() < deadline and not ("url" in found and "code" in found):
        if _login_proc.poll() is not None:
            break
        time.sleep(0.1)
    if not ("url" in found and "code" in found):
        raise CodexImageError("codex did not print a device code: " + "".join(lines)[-500:])
    return {"url": found["url"], "code": found["code"]}


def cancel_device_login() -> None:
    global _login_proc
    if _login_proc and _login_proc.poll() is None:
        _login_proc.terminate()
        _login_proc.wait(timeout=5)
    _login_proc = None
