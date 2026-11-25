"""Generate assets/icon.ico: a rounded gradient tile with the 译 glyph.

Run: python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZES = [16, 24, 32, 48, 64, 128, 256]
ACCENT_TOP = (0, 120, 212)
ACCENT_BOTTOM = (22, 148, 190)
GLYPH = "译"
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def gradient(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size), ACCENT_TOP)
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(
            int(ACCENT_TOP[i] + (ACCENT_BOTTOM[i] - ACCENT_TOP[i]) * ratio) for i in range(3)
        )
        draw.line([(0, y), (size, y)], fill=color)
    return image


def rounded_mask(size: int, radius_ratio: float = 0.24) -> Image.Image:
    mask = Image.new("L", (size * 4, size * 4), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        [0, 0, size * 4 - 1, size * 4 - 1],
        radius=int(size * 4 * radius_ratio),
        fill=255,
    )
    return mask.resize((size, size), Image.LANCZOS)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render(size: int) -> Image.Image:
    base = gradient(size).convert("RGBA")
    base.putalpha(rounded_mask(size))

    glyph_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glyph_layer)
    font = load_font(int(size * 0.58))
    box = draw.textbbox((0, 0), GLYPH, font=font)
    width, height = box[2] - box[0], box[3] - box[1]
    draw.text(
        ((size - width) / 2 - box[0], (size - height) / 2 - box[1]),
        GLYPH,
        font=font,
        fill=(255, 255, 255, 245),
    )
    base.alpha_composite(glyph_layer)
    return base


def main() -> None:
    out_dir = Path(__file__).resolve().parents[1] / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    images = [render(size) for size in SIZES]
    images[-1].save(
        out_dir / "icon.ico",
        format="ICO",
        sizes=[(size, size) for size in SIZES],
    )
    images[-1].save(out_dir / "icon.png", format="PNG")
    print(f"icon written to {out_dir / 'icon.ico'}")


if __name__ == "__main__":
    main()
