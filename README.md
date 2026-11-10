# codex-image-mcp

An MCP server that generates images (game sprites, concept art, icons) with **your ChatGPT
plan** through the official [Codex CLI](https://github.com/openai/codex). It needs no API key
and has no per-image billing: each image counts against your plan's Codex usage limits.

It runs `codex exec` headless and lets Codex's built-in image tool (gpt-image) draw. Then it
picks up the PNG Codex saves under `$CODEX_HOME/generated_images/<thread>/` and writes it where
you asked. Optionally it keys out a flat background, trims and downscales the image for sprites.

> Unofficial. It only drives the Codex CLI you installed and signed in to; it never reads or
> sends your tokens anywhere. Codex behaviour can change between versions.

## Requirements

- Codex CLI on `PATH` (or set `CODEX_BIN`), signed in with a **paid** ChatGPT plan (Plus, Pro,
  Business...). The image tool is not offered on free accounts.
- [`uv`](https://docs.astral.sh/uv/) (or any Python ≥ 3.10 environment).

Sign in with a device code if you have no browser on the machine:

```bash
codex login --device-auth
```

## Install

Claude Code:

```bash
claude mcp add -s user codex-image -- uvx --from git+https://github.com/JuanCG13/codex-image-mcp codex-image-mcp
```

Any other MCP client (stdio):

```json
{
  "mcpServers": {
    "codex-image": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/JuanCG13/codex-image-mcp", "codex-image-mcp"]
    }
  }
}
```

## Tools

| Tool | What it does |
| --- | --- |
| `generate_image` | One image to `output_path` (`.png`). Accepts `reference_images` to follow a character design or palette. |
| `generate_images` | Several jobs in parallel (`concurrency`, default 3), for example every pose of a sprite set. One result per job, in order. |
| `edit_image` | Changes only a region (`box` or `mask_path`) of an existing image. Everything outside it stays pixel-identical. |
| `normalize_frames` | Makes generated frames one consistent set: same body size, feet baseline and palette as a reference. Local and instant. |
| `login_status` | Whether Codex is signed in, and the plan (`plus`, `pro`...). |
| `start_device_login` | Starts `codex login --device-auth` and returns the URL and one-time code. |

Shared options:

- `chroma_key` (e.g. `"#FF00FF"`): makes that flat background transparent. gpt-image cannot
  output transparency, so ask for exactly that background colour in the prompt.
- `tolerance` (default 60): RGB distance counted as background.
- `trim`: crops the image to the subject.
- `max_size`: downscales with nearest neighbour so the longest side fits, which keeps pixel art crisp.
- `overwrite`: existing files are kept unless this is true.
- `reasoning_effort` (default `low`): how long Codex thinks before calling the tool.

The untouched original stays at `raw_path` in the result. Each result also reports
`width`, `height` and Codex token `usage`.

### Example: a sprite

```json
{
  "prompt": "Side-view pixel-art sprite of a stocky coal miner mid pickaxe swing, 16-bit style, full body, centered, on a flat pure magenta (#FF00FF) background",
  "output_path": "/abs/path/assets/miner_swing.png",
  "chroma_key": "#FF00FF",
  "trim": true,
  "max_size": 96
}
```

### Editing a region

Codex's built-in image tool has no mask: every edit redraws the whole picture, so the parts
you wanted kept drift (proportions, colours, details). `edit_image` makes edits safe:

1. It sends Codex the original (upscaled when small, transparent areas shown on `background`)
   and a black-and-white mask of the region, and asks for that change only.
2. It searches the scale and offset that best overlay the result on the original outside the
   region, so a re-framed or resized result still lines up.
3. It copies only the region's pixels from the result. Outside the region the output is the
   original, byte for byte, alpha included. Inside, the background colour is keyed back to
   transparency when the original had any.

```json
{
  "image_path": "/abs/path/assets/miner.png",
  "prompt": "Give the miner a red helmet lamp",
  "box": [20, 0, 60, 24],
  "output_path": "/abs/path/assets/miner_lamp.png"
}
```

The result includes `alignment` (`scale`: generated px per original px, `origin`, `error`: the
mean RGB difference outside the region) and a `warning` when the result matched poorly.

### Making frames a consistent set

Each generation is independent, so frames of one animation come back at different sizes and
positions, with slightly different colours. `normalize_frames` fixes that deterministically:

- **Scale:** every frame is resized so the character's body area matches the reference's. Area,
  unlike height, barely changes when the character crouches, leans or lunges. Thin parts such
  as a pick handle (under ~4% of the figure's size) are left out of the measure.
- **Anchor:** frames are placed on one shared canvas with the same feet baseline and the same
  centre, taken from the feet rows. The result reports that `anchor` point for the sprite offset.
- **Palette:** colours snap to up to `palette_size` (default 32) of the reference's own colours.
  They are picked farthest-first from the most common colour, so a small lamp glow or eye
  highlight wins a place over many near-identical shades. `0` keeps the generated colours. Alpha becomes fully opaque or transparent.

```json
{
  "frame_paths": ["/abs/gen/windup.png", "/abs/gen/strike.png"],
  "reference_path": "/abs/path/assets/miner_idle.png",
  "output_dir": "/abs/path/assets/miner_combat"
}
```

## Cost and speed

- About 30–60 s per image. `generate_images` runs them in parallel.
- Every tool runs Codex in a lean home of its own, `~/.cache/codex-image-mcp/codex-home`
  (override with `CODEX_IMAGE_LEAN_HOME`). It shares only your login: `auth.json` is a symlink to
  the real one, so your global `AGENTS.md`, skills, memories and history never reach the prompt.
  Measured on one image with one reference: about 135k input tokens with the full home, 30k lean. Pass `lean_context: false` to use
  your full Codex home instead.
- If Codex refreshes the login by replacing `auth.json` instead of writing through the symlink,
  the new tokens are copied back to the real file and the link is restored after the run. When
  there is no `auth.json` (for example credentials kept in the OS keyring), the full home is used.
- Reference images cost far more than the text: each one adds tens of thousands of input tokens.
  Pass only the references the image needs.
- The server runs Codex with `--ignore-user-config` and with apps, plugins, the browser and
  multi-agent turned off, so the request goes straight to the image tool and a model pinned in
  your config cannot break it.

## Development

```bash
uv run --with pillow python -m unittest discover -s tests
```

The tests use `tests/fake_codex.py` in place of the real CLI, so they spend no quota.

## License

MIT
