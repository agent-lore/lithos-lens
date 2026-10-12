"""Quantize the manual's screenshots to 256-colour PNGs.

Playwright writes 24-bit PNGs; a 1280px-wide full-page capture of Lens runs
to 0.5–1.2 MB each, which bloats the repo on every regeneration and is more
than GitHub's PR image preview copes with. Palette PNGs of UI screenshots are
visually identical at roughly a third of the size.

Run from the repo root, after capturing and before committing:

    uv run --no-project --with pillow python .claude/skills/regen-manual/compress_screenshots.py

Idempotent: re-running on already-quantized files changes nothing material.
"""

from __future__ import annotations

import pathlib
import sys

from PIL import Image

SHOTS = (
    pathlib.Path(__file__).resolve().parents[3] / "docs" / "user-manual" / "screenshots"
)


def main() -> int:
    total_in = total_out = 0
    for path in sorted(SHOTS.glob("*.png")):
        before = path.stat().st_size
        image = Image.open(path).convert("RGB")
        palette = image.quantize(
            colors=256,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.FLOYDSTEINBERG,
        )
        palette.save(path, optimize=True)
        after = path.stat().st_size
        total_in += before
        total_out += after
        print(f"{path.name:28s} {before // 1024:5d} KB -> {after // 1024:5d} KB")
    print(f"total {total_in // 1024} KB -> {total_out // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
