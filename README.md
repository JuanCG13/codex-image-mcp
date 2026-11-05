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

## Cost and speed

- About 30–60 s per image. `generate_images` runs them in parallel.
- Each run sends Codex's own context: its system prompt, your global `~/.codex/AGENTS.md` and the
  skills list. Most of it is cached between runs. A large global `AGENTS.md` makes every image
  more expensive.
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
