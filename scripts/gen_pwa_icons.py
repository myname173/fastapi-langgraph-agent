"""Generate PWA PNG icons (192/512) from scratch with Pillow.

Local one-shot tool — run on a machine with TrueType fonts (Windows has
arial.ttf). The generated PNGs are committed to frontend/; Docker only serves
them. Never run inside the container (python:3.13-slim has no fonts, and
load_default() would render a blurry dot).

Usage:  python scripts/gen_pwa_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "frontend"
FONT_CANDIDATES = [
    "arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]
BG = (13, 17, 23)  # #0d1117
FG = (47, 129, 247)  # #2f81f7


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load a TrueType font, raising RuntimeError if none found.

    Never falls back to load_default() — that renders a blurry dot in
    fontless environments (e.g. Docker slim images), producing a broken icon.
    """
    for cand in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    raise RuntimeError(
        "No TrueType font found in candidates: %s. Install arial.ttf or "
        "DejaVuSans-Bold.ttf before running this script." % FONT_CANDIDATES
    )


def render(size: int) -> Image.Image:
    """Render the icon at the given size, matching icon.svg visually."""
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)
    # Rounded border, scaled from the 512 design (inset 56, radius 72, width 14).
    s = size / 512.0
    inset, rad, w = 56 * s, 72 * s, 14 * s
    d.rounded_rectangle(
        (inset, inset, size - inset, size - inset),
        radius=rad,
        outline=FG,
        width=max(1, int(round(w))),
    )
    d.text(
        (size / 2, size / 2),
        "AI",
        font=load_font(int(200 * s)),
        fill=FG,
        anchor="mm",
    )
    return img


def main() -> None:
    """Generate icon-512.png and icon-192.png in frontend/."""
    OUT.mkdir(parents=True, exist_ok=True)
    big = render(512)
    big.save(OUT / "icon-512.png", "PNG")
    small = big.resize((192, 192), Image.LANCZOS)
    small.save(OUT / "icon-192.png", "PNG")
    print("wrote %s/icon-192.png and icon-512.png" % OUT)


if __name__ == "__main__":
    main()
