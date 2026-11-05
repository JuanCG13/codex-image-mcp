"""MCP server: generate images (sprites, concept art) with the ChatGPT plan signed in to Codex."""
from mcp.server.fastmcp import FastMCP

from . import core

mcp = FastMCP("codex-image")

POSTPROCESS_DOC = """
    Post-processing (all optional, applied in this order):
    - chroma_key: hex colour of a flat background to make transparent, e.g. "#FF00FF".
      gpt-image cannot output transparency, so ask for that exact background in the prompt.
    - tolerance: RGB distance treated as background (default 60).
    - trim: crop to the non-transparent pixels.
    - max_size: downscale (nearest neighbour) so the longest side is at most this many px.
    The untouched original stays at raw_path."""


@mcp.tool()
def generate_image(prompt: str, output_path: str, reference_images: list[str] | None = None,
                   overwrite: bool = False, chroma_key: str | None = None, tolerance: int = 60,
                   trim: bool = False, max_size: int | None = None,
                   reasoning_effort: str = "low", timeout_seconds: int = 600) -> dict:
    """Generate one image with Codex's built-in image tool on the signed-in ChatGPT plan.

    Takes ~30-60 s and counts against the plan's Codex usage limits.
    output_path: absolute path ending in .png; existing files are kept unless overwrite=true.
    reference_images: local image paths Codex should follow for design, palette or style.
    reasoning_effort: Codex effort before calling the tool ("low" is fastest).
    """
    try:
        return core.generate(prompt, output_path, reference_images or [], overwrite, timeout_seconds,
                             chroma_key, tolerance, trim, max_size, reasoning_effort)
    except core.CodexImageError as e:
        return {"error": str(e)}


@mcp.tool()
def generate_images(jobs: list[dict], concurrency: int = 3, chroma_key: str | None = None,
                    tolerance: int = 60, trim: bool = False, max_size: int | None = None,
                    overwrite: bool = False, reasoning_effort: str = "low") -> list[dict]:
    """Generate several images in parallel (e.g. every pose of a sprite set).

    jobs: list of {"prompt", "output_path", optional "reference_images" and any per-job override
    of the shared options}. concurrency: parallel Codex runs (default 3; higher may hit rate limits).
    Returns one result per job in order; failed jobs carry "error".
    """
    return core.generate_batch(jobs, concurrency, chroma_key=chroma_key, tolerance=tolerance,
                               trim=trim, max_size=max_size, overwrite=overwrite,
                               reasoning_effort=reasoning_effort)


generate_image.__doc__ += POSTPROCESS_DOC
generate_images.__doc__ += POSTPROCESS_DOC


@mcp.tool()
def login_status() -> dict:
    """Report whether Codex is signed in and which ChatGPT plan it uses (plus, pro, ...)."""
    return core.login_status()


@mcp.tool()
def start_device_login() -> dict:
    """Start a ChatGPT device-code login for Codex and return the URL and one-time code.

    The user opens the URL, enters the code and approves; then call login_status to confirm.
    """
    try:
        return core.start_device_login()
    except core.CodexImageError as e:
        return {"error": str(e)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
