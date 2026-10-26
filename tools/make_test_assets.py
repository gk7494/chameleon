"""
Generates the synthetic images used by the extra stress-test designs in
tools/designs/ (not part of the scored pipeline, not shipped in the image).

    python -m tools.make_test_assets      # writes tools/test_assets/*.png

* test_photo.png   a "photo" background: busy, high-contrast clutter on the
                   left half and a calm sky-like gradient on the right, so
                   the background-aware pass has something to avoid
* test_tile_*.png  four square tiles for the image-grid design
* test_card.png    a landscape picture for the card design
Deterministic (fixed seed), Pillow only.
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent / "test_assets"


def photo(w: int = 1600, h: int = 1067) -> Image.Image:
    rng = random.Random(7)
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):  # warm evening sky
        t = y / h
        for x in range(w):
            px[x, y] = (int(40 + 150 * t), int(70 + 70 * t), int(140 - 40 * t))
    d = ImageDraw.Draw(im)
    for _ in range(900):  # clutter on the left half
        x, y = rng.uniform(0, w * 0.5), rng.uniform(0, h)
        r = rng.uniform(4, 40)
        c = tuple(rng.randint(0, 255) for _ in range(3))
        if rng.random() < 0.5:
            d.ellipse([x - r, y - r, x + r, y + r], fill=c)
        else:
            d.rectangle([x - r, y - r / 3, x + r, y + r / 3], fill=c)
    d.polygon([(0, h), (w * 0.35, h * 0.62), (w * 0.7, h * 0.85), (w, h * 0.7), (w, h)], fill=(30, 40, 35))
    return im.filter(ImageFilter.GaussianBlur(1.2))


def tile(i: int, s: int = 600) -> Image.Image:
    palettes = [((233, 104, 58), (250, 214, 120)), ((60, 120, 90), (190, 225, 170)),
                ((70, 60, 140), (200, 180, 240)), ((150, 40, 60), (245, 170, 160))]
    a, b = palettes[i % 4]
    im = Image.new("RGB", (s, s), a)
    d = ImageDraw.Draw(im)
    for k in range(6):
        r = s * (0.45 - k * 0.07)
        d.ellipse([s / 2 - r, s / 2 - r, s / 2 + r, s / 2 + r], fill=b if k % 2 == 0 else a)
    return im


def card(w: int = 900, h: int = 560) -> Image.Image:
    im = Image.new("RGB", (w, h), (245, 228, 205))
    d = ImageDraw.Draw(im)
    d.rectangle([0, h * 0.65, w, h], fill=(214, 120, 70))
    d.ellipse([w * 0.6, h * 0.12, w * 0.85, h * 0.52], fill=(250, 190, 60))
    d.polygon([(w * 0.05, h * 0.65), (w * 0.3, h * 0.25), (w * 0.55, h * 0.65)], fill=(90, 60, 80))
    return im


def main() -> None:
    OUT.mkdir(exist_ok=True)
    photo().save(OUT / "test_photo.png", optimize=True)
    for i in range(4):
        tile(i).save(OUT / f"test_tile_{i + 1}.png", optimize=True)
    card().save(OUT / "test_card.png", optimize=True)
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))


if __name__ == "__main__":
    main()
