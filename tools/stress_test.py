"""
Stress test for the engine — not part of the scored pipeline.

    python -m tools.stress_test            # everything
    python -m tools.stress_test --quick    # skip rendering (structure checks only)

1. Runs every input/*.json design through resize() at a catalogue of real
   target formats and checks: ids preserved, required fields, nothing
   off-canvas, image aspect kept (non-cover), integer font sizes >= 1, and
   no unintended overlap between separate layout blocks.
2. Runs an adversarial battery (missing asset, rotated element, empty text,
   zero-size element, unknown font, no z_index, dozens of text boxes, 1x1 and
   4000x4000 targets ...) and reports any crash.
3. Renders contact sheets to output/stress/sheet_<design>.png so the layouts
   can be eyeballed across formats.
"""

from __future__ import annotations

import copy
import glob
import itertools
import json
import sys
import time
from pathlib import Path

from engine.main import self_check
from engine.resize import analyze, box_area, box_inter, rect_aabb, resize

FORMATS = [
    ("story", 1080, 1920), ("square", 1080, 1080), ("post45", 1080, 1350),
    ("fb_link", 1200, 628), ("yt_thumb", 1280, 720), ("hd", 1920, 1080),
    ("skyscraper", 160, 600), ("mrec", 300, 250), ("leaderboard", 728, 90),
    ("yt_banner", 2560, 1440), ("fb_cover", 820, 312), ("tw_header", 1500, 500),
    ("pin", 1000, 1500),
]


def structural_problems(src: dict, out: dict) -> list[str]:
    probs = list(self_check(src, out))
    if [e["id"] for e in out["elements"]] != [e["id"] for e in src["elements"]]:
        probs.append("element order/ids changed")
    for a, b in zip(src["elements"], out["elements"]):
        if a["type"] == "image" and a.get("fit") != "cover" and a["width"] > 0 and a["height"] > 0 and b["height"] > 0:
            r1, r2 = a["width"] / a["height"], b["width"] / b["height"]
            if abs(r1 / r2 - 1) > 0.05:
                probs.append(f"image '{a['id']}' aspect changed")
        if a["type"] == "text":
            fs = b.get("font_size")
            if not isinstance(fs, int) or fs < 1:
                probs.append(f"text '{a['id']}' font_size {fs!r}")
            if b.get("content") != a.get("content") or b.get("font_family") != a.get("font_family"):
                probs.append(f"text '{a['id']}' content/font changed")
    return probs


def overlap_problems(src: dict, out: dict) -> list[str]:
    an = analyze(src)
    skip = {e.id for e in an.backgrounds} | {e.id for e in an.decor} | {b.els[0].id for b in an.panels}
    block_of = {e.id: i for i, b in enumerate(an.blocks) for e in b.els}
    els = [e for e in out["elements"] if e["id"] not in skip and e["id"] in block_of]
    probs = []
    for a, b in itertools.combinations(els, 2):
        if block_of[a["id"]] == block_of[b["id"]]:
            continue
        A = rect_aabb(a["x"], a["y"], a["width"], a["height"], a.get("rotation", 0))
        B = rect_aabb(b["x"], b["y"], b["width"], b["height"], b.get("rotation", 0))
        inter = box_inter(A, B)
        small = min(box_area(A), box_area(B)) or 1.0
        if inter / small > 0.02:
            probs.append(f"{a['id']} x {b['id']} ({inter / small:.0%})")
    return probs


def adversarial_cases(base: dict) -> list[tuple[str, dict, dict]]:
    def mod(fn):
        s = copy.deepcopy(base)
        fn(s)
        return s

    def headline(s):
        return next(e for e in s["elements"] if e["type"] == "text")

    cases = [
        ("identity", base, {"width": base["canvas"]["width"], "height": base["canvas"]["height"]}),
        ("tiny 120x120", base, {"width": 120, "height": 120}),
        ("1x1", base, {"width": 1, "height": 1}),
        ("huge 4000x4000", base, {"width": 4000, "height": 4000}),
        ("ultra wide 3000x100", base, {"width": 3000, "height": 100}),
        ("ultra tall 100x3000", base, {"width": 100, "height": 3000}),
        ("empty text", mod(lambda s: headline(s).update(content="")), {"width": 1600, "height": 400}),
        ("one long word", mod(lambda s: headline(s).update(content="Supercalifragilisticexpialidocious_without_spaces")), {"width": 300, "height": 250}),
        ("rotated text", mod(lambda s: headline(s).update(rotation=25)), {"width": 1080, "height": 1080}),
        ("explicit newlines", mod(lambda s: headline(s).update(content="Line one\nLine two\n\nLine four")), {"width": 1080, "height": 1080}),
        ("unknown font", mod(lambda s: headline(s).update(font_family="Montserrat")), {"width": 1080, "height": 1920}),
        ("font_size 0.5", mod(lambda s: headline(s).update(font_size=0.5)), {"width": 1080, "height": 1920}),
        ("letter spacing", mod(lambda s: headline(s).update(letter_spacing=8, line_height=1.6)), {"width": 1080, "height": 1080}),
        ("no z_index", mod(lambda s: [e.pop("z_index", None) for e in s["elements"]]), {"width": 1080, "height": 1920}),
        ("zero-size element", mod(lambda s: s["elements"][1].update(width=0, height=0)), {"width": 1600, "height": 400}),
        ("only first element", mod(lambda s: s.update(elements=s["elements"][:1])), {"width": 1080, "height": 1920}),
        ("no background", mod(lambda s: s.update(elements=s["elements"][1:])), {"width": 1600, "height": 400}),
        ("missing asset", mod(lambda s: s["elements"].append({"id": "ghost", "type": "image", "x": 10, "y": 10, "width": 100, "height": 60, "z_index": 9, "asset_id": "does_not_exist.png"})), {"width": 1080, "height": 1080}),
        ("big translucent rect", mod(lambda s: s["elements"].append({"id": "veil", "type": "shape", "x": 100, "y": 100, "width": s["canvas"]["width"] - 200, "height": s["canvas"]["height"] - 200, "z_index": 1, "shape_type": "rectangle", "fill_color": "rgba(0,0,0,0.3)"})), {"width": 1600, "height": 400}),
        ("bottom band panel", mod(lambda s: s["elements"].append({"id": "band", "type": "shape", "x": 0, "y": s["canvas"]["height"] * 0.74, "width": s["canvas"]["width"], "height": s["canvas"]["height"] * 0.26, "z_index": 1, "shape_type": "rectangle", "fill_color": "rgba(0,0,0,0.9)"})), {"width": 1080, "height": 1920}),
    ]

    def many_texts(s):
        W, H = s["canvas"]["width"], s["canvas"]["height"]
        for i in range(24):
            s["elements"].append({"id": f"t{i}", "type": "text", "x": 40 + (i % 4) * (W / 4), "y": H * 0.5 + (i // 4) * 30,
                                  "width": W / 4 - 60, "height": 26, "z_index": 7, "content": f"Note number {i} about the flavor",
                                  "font_family": "Solstice Sans", "font_size": 14, "color": "rgba(255,255,255,1)", "text_align": "left"})
    cases.append(("24 extra text boxes wide", mod(many_texts), {"width": 1600, "height": 400}))
    cases.append(("24 extra text boxes tall", mod(many_texts), {"width": 1080, "height": 1920}))
    return cases


def main() -> int:
    quick = "--quick" in sys.argv
    inputs = sorted(glob.glob("input/*.json"))
    if not inputs:
        print("no input/*.json found; run from the repo root")
        return 1
    failures = 0
    out_dir = Path("output/stress")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("== format matrix")
    for f in inputs:
        src = json.load(open(f))["source"]
        stem = Path(f).stem.replace("chameleon_sample_", "")
        tiles = []
        for name, w, h in FORMATS:
            t0 = time.time()
            out = resize(src, {"width": w, "height": h})
            dt = time.time() - t0
            probs = structural_problems(src, out) + overlap_problems(src, out)
            status = "OK" if not probs else "; ".join(probs)
            failures += bool(probs)
            print(f"  {stem:18s} {name:12s} {w}x{h} {dt:5.2f}s  {status}")
            if not quick:
                try:
                    from PIL import Image, ImageDraw
                    from renderer.converter.convert import render_design_to_png
                    png = out_dir / f"{stem}_{name}.png"
                    render_design_to_png(out, "brand_kit/assets", str(png))
                    im = Image.open(png).convert("RGB")
                    sc = min(520 / im.width, 520 / im.height, 1.0)
                    im = im.resize((max(1, int(im.width * sc)), max(1, int(im.height * sc))))
                    tile = Image.new("RGB", (540, 560), (235, 235, 235))
                    tile.paste(im, ((540 - im.width) // 2, 30 + (520 - im.height) // 2))
                    ImageDraw.Draw(tile).text((8, 8), f"{name} {w}x{h}", fill=(0, 0, 0))
                    tiles.append(tile)
                except Exception as exc:  # rendering is a convenience, not the test
                    print(f"    (render skipped: {exc})")
        if tiles:
            cols = 4
            rows = (len(tiles) + cols - 1) // cols
            sheet = Image.new("RGB", (cols * 540, rows * 560), (255, 255, 255))
            for i, tl in enumerate(tiles):
                sheet.paste(tl, ((i % cols) * 540, (i // cols) * 560))
            sheet.save(out_dir / f"sheet_{stem}.png")
            print(f"  -> {out_dir / f'sheet_{stem}.png'}")

    print("== adversarial battery (on the first input design)")
    base = json.load(open(inputs[-1]))["source"]
    for name, src, tgt in adversarial_cases(base):
        t0 = time.time()
        try:
            out = resize(src, tgt)
            probs = structural_problems(src, out)
        except Exception as exc:
            probs = [f"CRASH {exc!r}"]
        failures += bool(probs)
        print(f"  {name:26s} {time.time() - t0:5.2f}s  {'OK' if not probs else '; '.join(probs)}")

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
