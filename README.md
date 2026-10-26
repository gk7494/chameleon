# CHAMELEON engine — structure-aware reflow

A deterministic, offline layout engine for the Adobe University Hackathon
"Chameleon" challenge. It takes a design (`canvas` + `elements`) and a target
canvas size and returns the **same elements, re-laid-out** for the new shape.
No pixels are generated or redrawn; no copy is rewritten; font families are
untouched. Everything happens in `engine/resize.py` (pure Python, ~2 300 lines,
no models, no network).

## Running it

```bash
pip install -r requirements.txt     # Pillow + cr-renderer (brings skia-python)
python -m engine.main               # input/*.json -> output/<task>_output.{json,png}
```

Docker, exactly as stage one scores it:

```bash
docker build --platform linux/amd64 -t my-chameleon-engine .
docker run --rm --network none \
  -v $(pwd)/input:/app/input -v $(pwd)/output:/app/output \
  my-chameleon-engine
```

No new dependencies were added to the starter kit, and `Dockerfile` /
`requirements.txt` are byte-identical to the upstream starter. The image was
built and run with the two commands above (`--network none`). The container
is the reference: skia's glyph advances differ slightly between platforms (a
40 px bold headline measures 755.0 px in the image and 756.7 px on macOS), so
a local macOS run can place some boxes a few pixels differently from the
container. The engine measures text
with the same skia glyph advances and the same greedy wrap the renderer uses
(it falls back to Pillow, then to a width table, if fonts are missing), so the
line breaks it plans are the line breaks that get rendered. One task takes
0.005–0.45 s on a laptop (about 0.05 s on average across the stress-test
formats; the slowest adversarial case, 24 extra text boxes in a wide banner,
takes about 1 s); the engine also runs under Python 3.11 (the image's
interpreter) and 3.13.

## How it works

The engine recovers the *structure* a designer put into the source and lays
that structure out again, instead of scaling a picture.

1. **Measure.** Every text element is wrapped at its source width with the real
   brand font to get its painted extent (line count, widest line, cap-height
   offset). These tight boxes drive all the geometry below.
2. **Classify.** Each element becomes one of:
   * *background* – covers ≥ 80 % of the canvas → stretched to the new canvas
     (`fit: cover` crops, as the rules allow);
   * *decorative* – low-opacity shapes, shapes bleeding off the canvas, large
     empty shapes → kept at the same relative spot, scaled, clamped on-canvas;
   * *panel* – an opaque shape holding several things → its children are kept
     together as one unit of the layout tree, laid out inset by the panel's
     padding (source padding × k, capped at 4 % of the shorter side), and the
     panel is re-wrapped around them afterwards, so a card never covers the
     logo or fine print beside it (it keeps the canvas edges it touched);
   * *flow block* – everything else. Implicit groups are detected from
     geometry: a small shape holding exactly one text is a **button**, text
     sitting fully on an image is an **overlay**, and `group_id` is always
     honoured. Roles (logo, hero, headline, subhead, body, kicker, legal, CTA,
     badge, stamp) are inferred from size, weight and position only — the input
     carries no labels.
3. **Structure.** An XY-cut tree is built over the flow blocks: the widest
   whitespace band splits the set into rows or columns, recursively, so reading
   order and "these three badges are one row" / "this caption belongs to this
   thumbnail" survive. Elements that truly overlap in the source are read in
   reading order.
4. **Re-orient.** Tall targets are laid out as a vertical stack of the tree's
   rows; wide targets (≥ 1.3 : 1) as a horizontal flow of its columns;
   near-square targets (0.8–1.3) try both and keep the one that fits, keeps
   text legible and holds the higher readability floor; on a tie, leaving the
   source's own orientation (stack for portrait or square designs, flow for
   landscape ones) must win the score by 0.3.
   When the axis flips, consecutive text rows merge into one text column, legal
   lines become a footer strip, rows of an image grid become one strip of
   cells, and the lanes can be grouped into 2–3 columns (every consecutive
   grouping is tried and scored) — this is what turns a poster into a
   two-column banner. In an engine-built column every child takes one
   alignment read from the source (centres, else left edges, else right
   edges), small stamps keep the side they sat on, and a logo + sticker row
   that would make the column wide and empty is stacked instead. A card that
   does not fit stacked may lay its contents out side by side (picture | rest)
   as long as no text drops below its floor.
5. **Scale.** A single global scale `k` is searched over 56 log-spaced
   candidates, then refined by bisection. Every candidate is fully laid out,
   checked for fit, and scored: `log k` rewards size; extra line-wraps, fonts
   pushed under a readability floor, wrapped rows, unbalanced columns and
   over-filled canvases are penalised. Text may rewrap (and widens to its lane
   rather than wrap more than the source did), rows may flex-wrap, images
   shrink individually only when they would not fit, and logos / hero images /
   legal text have floors so brand elements never vanish in extreme formats.
   * *Readability floor.* Text never goes below `max(10 px, 0.85 % of the long
     side)` (legal × 0.7, kickers and badges × 0.8). Text bigger than the body
     copy gets a proportionally higher floor (up to 2×, by √ of the size
     ratio), so the hierarchy survives when everything is on the floor. Only
     when nothing fits at that floor does the engine step down a ladder
     (8.5 → 7 → 5.5 px), charging each step, and non-logo image floors relax
     with it.
   * *Buttons and chips.* A label lifted to the floor no longer scales its
     whole shape: the shape wraps the label with modest padding, so a tag
     never balloons past the logo. A row of buttons that does not fit first
     trims padding (~1.1 × font size), handing back whatever width is left;
     a row of chips that fits keeps its source padding and is spaced evenly.
   * *Hero growth.* After `k` is chosen, the hero image may grow (up to 2×)
     while the layout stays feasible, the score holds and the hero covers at
     most ~62 % of the canvas.
6. **Position.** When the main axis is unchanged (tall→tall or wide→wide),
   each lane keeps its proportional position from the source and a 1-D solver
   enforces order, minimum gaps and bounds — so a logo stays far left, a CTA
   far right, and a composition with an empty lower third stays that way.
   When the axis flips, lanes are justified with capped gaps. Across the axis,
   left / centre / right alignment of the source is preserved.
7. **Background-aware placement.** The engine *reads* the background asset
   (never writes it), reproduces the renderer's centred `cover` crop for the
   target canvas, and builds a coarse luminance / edge-density map. The whole
   content block is then nudged within its free space to minimise busyness and
   low contrast under free-standing text (button labels and image overlays
   carry their own backing). A displacement penalty keeps the layout where
   the structure put it unless there is a real gain — on a calm gradient the
   layout does not move; on a half-busy photo the text moves to the calm half.
8. **Finish.** Backgrounds cover, decorations re-anchor, panels wrap, and a last
   pass guarantees every element is inside the canvas (a uniform squeeze about
   the centre if something had to overflow). Image boxes keep their aspect
   ratio exactly; text boxes get integer font sizes; ids, count and every
   other field are passed through unchanged, except that a shape's
   `corner_radius` scales with it (so a card stays a card on a small format).

If structural analysis ever raises, `resize()` falls back to anchored uniform
scaling and still returns a valid document.

## Design decisions and trade-offs

* **Heuristics over a model.** Hidden cases span many formats and element
  counts; a model-free engine is fully reproducible, runs in well under a
  second, and its failure modes are inspectable. The scoring function makes
  the trade-offs explicit (size vs. wrapping vs. balance) rather than baking
  them into weights.
* **Text is reflowed, not just scaled.** Boxes are re-measured at the target
  font size, so headlines wrap to two lines in a story and sit on one line in a
  banner; a single word that cannot fit shrinks the font instead of overflowing.
* **Everything stays on-canvas**, including decorative shapes that bled off
  the source — the structural check is treated as a hard rule.
* **Pixels are read, never written.** The only use of image content is the
  background map that steers text away from busy or low-contrast regions; no
  asset is regenerated, redrawn, recoloured or replaced.
* **Low-priority elements degrade first.** Legal text, badges and stamps have
  lower floors than headlines, logos and CTAs, so a 728×90 leaderboard keeps the
  headline and CTA readable even though seventeen elements cannot all be
  legible at that size.
* **What it does not do (by rule).** No image regeneration, no copy rewriting,
  no font-family changes, no element dropping, no rotation changes.

## Testing

```bash
python -m tools.stress_test          # renders contact sheets to output/stress/
python -m tools.stress_test --quick  # structure checks only, ~20 s
python -m tools.make_test_assets     # regenerates tools/test_assets/*.png
```

The stress test takes every `input/*.json` design, plus four extra designs in
`tools/designs/` (left-aligned copy beside a product, text over a photo
background with a busy half, a 2×2 image grid with captions, a story with a
product card panel; their images are generated by `tools/make_test_assets.py`
and are not shipped in the image), through thirteen real
formats (story, square, 4:5, 1200×628, 1280×720, 1920×1080, 160×600, 300×250,
728×90, 2560×1440, 820×312, 1500×500, 1000×1500) and checks ids, required
fields, bounds, image proportions, integer font sizes, unchanged copy/fonts,
that no two separate layout blocks overlap, and that a panel never covers a
block outside it. Every contact sheet starts with the source design. It then
runs an adversarial
battery (missing asset, rotated element, 140-character headline, empty text,
one unbreakable word, explicit newlines, letter spacing, unknown font,
font_size 0.5, no z-index, zero-size element, translucent veil, bottom band
panel, 24 extra text boxes, 1×1 / 4000×4000 / 3000×100 / 100×3000 targets).

All 91 format layouts and 22 adversarial cases pass. Known limitation: on a
photo background in very wide formats (820×312, 1500×500, 728×90) the content
spans the full width, so the background-aware nudge has no room to move the
headline off the busy half of the picture.
