"""
CHAMELEON engine — structure-aware reflow (pure Python, fully offline).

The engine never touches pixels. It reads the design as a set of boxes,
recovers the *structure* a designer put into it, and lays that structure out
again for the new canvas:

  1. measure      real glyph widths from the brand fonts, with the renderer's
                  own greedy wrap, so every text box is predicted exactly
  2. classify     background / decorative / panel / flow blocks, plus implicit
                  groups (button = shape + label, text sitting on an image)
  3. structure    an XY-cut tree of the flow blocks: rows and columns found
                  from the whitespace between them (reading order preserved)
  4. re-orient    the tree is read as a vertical stack for tall/square targets
                  or as a horizontal flow for wide targets (consecutive text
                  rows merge into one text column; legal lines become a footer)
  5. scale        a single global scale k is searched: every candidate is laid
                  out, checked for fit and scored (bigger is better; extra
                  line-wraps, under-floor fonts and over-full canvases lose)
  6. position     lanes keep their relative placement (proportional centres +
                  a 1-D spacing solver) when the axis is unchanged and are
                  justified when it flips; cross-axis anchoring keeps the
                  left / centre / right alignment of the source
  7. finish       backgrounds cover the canvas, decorations re-anchor to the
                  same spot, panels wrap their children, and everything is
                  clamped inside the canvas with image boxes kept in proportion

Only `resize()` is called by engine/main.py.
"""

from __future__ import annotations

import math
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ----------------------------------------------------------------------------
# Font measurement — mirrors renderer/converter/wrap.py (skia glyph advances,
# greedy wrap that only turns spaces into newlines).  Falls back to Pillow and
# then to a width table, so the engine still runs if fonts are missing.
# ----------------------------------------------------------------------------

try:  # the renderer's measuring backend; installed with cr-renderer
    import skia  # type: ignore
except Exception:  # pragma: no cover
    skia = None

try:
    from PIL import Image, ImageFilter, ImageFont  # type: ignore
except Exception:  # pragma: no cover
    Image = ImageFilter = ImageFont = None

_FONT_DIRS = [
    Path(__file__).resolve().parent.parent / "brand_kit" / "fonts",
    Path("brand_kit") / "fonts",
    Path("/app/brand_kit/fonts"),
]
_ASSET_DIRS = [
    Path(__file__).resolve().parent.parent / "brand_kit" / "assets",
    Path("brand_kit") / "assets",
    Path("/app/brand_kit/assets"),
]
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def _norm_family(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class FontBook:
    """Loads the brand-kit fonts once and answers per-character advances."""

    def __init__(self) -> None:
        self.by_family: dict[str, list[tuple[bool, bool, Path]]] = {}
        for d in _FONT_DIRS:
            if d.is_dir():
                self._scan(d)
                if self.by_family:
                    break
        self._skia_fonts: dict[tuple[str, int], Any] = {}
        self._pil_fonts: dict[tuple[str, int], Any] = {}
        self._width_tables: dict[tuple[str, int], dict[str, float]] = {}
        self._metrics: dict[tuple[str, int], tuple[float, float]] = {}
        # The scale search re-measures the same strings at the same sizes many
        # times, so font lookups and per-string widths are memoized.
        self._paths: dict[tuple[str, bool, bool], Optional[Path]] = {}
        self._str_widths: dict[tuple[str, bool, bool, int, str], list[float]] = {}

    def _scan(self, d: Path) -> None:
        for p in sorted(d.iterdir()):
            if p.suffix.lower() not in (".ttf", ".otf"):
                continue
            prefix, _, suffix = p.stem.partition("-")
            fam = _norm_family(_CAMEL.sub(" ", prefix))
            s = suffix.lower()
            italic = "italic" in s or "oblique" in s
            bold = any(t in s for t in ("bold", "black", "heavy", "semibold", "extrabold"))
            self.by_family.setdefault(fam, []).append((bold, italic, p))

    def path(self, family: str, bold: bool, italic: bool) -> Optional[Path]:
        key = (family, bold, italic)
        if key not in self._paths:
            self._paths[key] = self._find_path(family, bold, italic)
        return self._paths[key]

    def _find_path(self, family: str, bold: bool, italic: bool) -> Optional[Path]:
        cands = self.by_family.get(_norm_family(family))
        if not cands:
            if not self.by_family:
                return None
            cands = max(self.by_family.values(), key=len)  # unknown family
        best = min(cands, key=lambda c: (c[0] != bold) + (c[1] != italic))
        return best[2]

    def _skia_font(self, path: Path, size: int):
        key = (str(path), size)
        f = self._skia_fonts.get(key)
        if f is None:
            tf = skia.Typeface.MakeFromData(skia.Data.MakeFromFileName(str(path)))
            f = skia.Font(tf, size)
            self._skia_fonts[key] = f
        return f

    def _pil_font(self, path: Path, size: int):
        key = (str(path), size)
        f = self._pil_fonts.get(key)
        if f is None:
            f = ImageFont.truetype(str(path), size)
            self._pil_fonts[key] = f
        return f

    def char_widths(self, family: str, bold: bool, italic: bool, size: int, text: str) -> list[float]:
        """Per-character advances. The returned list is shared: read it, don't modify it."""
        size = max(1, int(size))
        key = (family, bold, italic, size, text)
        widths = self._str_widths.get(key)
        if widths is None:
            widths = self._char_widths(family, bold, italic, size, text)
            self._str_widths[key] = widths
        return widths

    def _char_widths(self, family: str, bold: bool, italic: bool, size: int, text: str) -> list[float]:
        path = self.path(family, bold, italic)
        if path is None or (skia is None and ImageFont is None):
            return [self._fallback_width(c, size) for c in text]
        key = (str(path), size)
        table = self._width_tables.setdefault(key, {})
        missing = sorted({c for c in text if c not in table})
        if missing:
            try:
                if skia is not None:
                    f = self._skia_font(path, size)
                    for c in missing:
                        ws = list(f.getWidths(f.textToGlyphs(c)))
                        table[c] = float(sum(ws)) if ws else 0.0
                else:
                    f = self._pil_font(path, size)
                    for c in missing:
                        table[c] = float(f.getlength(c)) * 1.02
            except Exception:
                for c in missing:
                    table[c] = self._fallback_width(c, size)
        return [table[c] for c in text]

    def ascent(self, family: str, bold: bool, italic: bool, size: int) -> float:
        return self._asc_desc(family, bold, italic, size)[0]

    def line_box(self, family: str, bold: bool, italic: bool, size: int) -> float:
        """Vertical extent (ascent + descent) of one line at `size`."""
        a, d = self._asc_desc(family, bold, italic, size)
        return a + d

    def _asc_desc(self, family: str, bold: bool, italic: bool, size: int) -> tuple[float, float]:
        size = max(1, int(size))
        path = self.path(family, bold, italic)
        if path is None:
            return size * 1.0, size * 0.25
        key = (str(path), size)
        m = self._metrics.get(key)
        if m is None:
            try:
                if skia is not None:
                    fm = self._skia_font(path, size).getMetrics()
                    m = (-fm.fAscent, fm.fDescent)
                elif ImageFont is not None:
                    a, d = self._pil_font(path, size).getmetrics()
                    m = (float(a), float(d))
                else:
                    m = (size * 1.0, size * 0.25)
            except Exception:
                m = (size * 1.0, size * 0.25)
            self._metrics[key] = m
        return m

    @staticmethod
    def _fallback_width(c: str, size: int) -> float:
        if c == " ":
            return size * 0.28
        if c in "iljtf.,;:'|!":
            return size * 0.3
        if c.isupper() or c in "mwMW@%":
            return size * 0.68
        return size * 0.54


_FONTS: Optional[FontBook] = None


def fonts() -> FontBook:
    global _FONTS
    if _FONTS is None:
        _FONTS = FontBook()
    return _FONTS


def wrap_lines(text: str, widths: list[float], max_width: float) -> list[str]:
    """Port of the renderer's greedy wrap: only spaces become newlines; a word
    wider than the box overflows rather than being split."""
    if not text:
        return [""]
    if max_width <= 0:
        return text.split("\n")
    chars = list(text)
    line_w = 0.0
    last_space = None
    for i, c in enumerate(chars):
        if c == "\n":
            line_w = 0.0
            last_space = None
            continue
        w = widths[i] if i < len(widths) else 0.0
        if line_w + w > max_width and last_space is not None:
            chars[last_space] = "\n"
            line_w = sum(widths[j] for j in range(last_space + 1, i + 1) if chars[j] != "\n")
            last_space = None
        else:
            line_w += w
        if c == " ":
            last_space = i
    return "".join(chars).split("\n")


# ----------------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------------

Box = tuple[float, float, float, float]  # x0, y0, x1, y1


def rot_aabb_dims(w: float, h: float, rot: float) -> tuple[float, float]:
    if not rot:
        return w, h
    t = math.radians(rot)
    c, s = abs(math.cos(t)), abs(math.sin(t))
    return w * c + h * s, w * s + h * c


def rect_aabb(x: float, y: float, w: float, h: float, rot: float) -> Box:
    aw, ah = rot_aabb_dims(w, h, rot)
    cx, cy = x + w / 2, y + h / 2
    return (cx - aw / 2, cy - ah / 2, cx + aw / 2, cy + ah / 2)


def box_union(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def box_area(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def box_inter(a: Box, b: Box) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def box_contains(outer: Box, inner: Box, tol: float = 0.0) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def parse_alpha(color: Any) -> float:
    if not isinstance(color, str):
        return 1.0
    m = re.match(r"\s*rgba?\(([^)]*)\)", color)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) >= 4:
            try:
                return max(0.0, min(1.0, float(parts[3])))
            except ValueError:
                return 1.0
    m = re.match(r"\s*#([0-9a-fA-F]{8})\s*$", color)
    if m:
        return int(m.group(1)[6:8], 16) / 255.0
    return 1.0


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def parse_rgb(color: Any) -> Optional[tuple[float, float, float]]:
    if not isinstance(color, str):
        return None
    m = re.match(r"\s*rgba?\(([^)]*)\)", color)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) >= 3:
            try:
                return tuple(clamp(float(x), 0, 255) for x in parts[:3])  # type: ignore[return-value]
            except ValueError:
                return None
    m = re.match(r"\s*#([0-9a-fA-F]{6})", color)
    if m:
        h = m.group(1)
        return float(int(h[0:2], 16)), float(int(h[2:4], 16)), float(int(h[4:6], 16))
    return None


def rel_luminance(rgb: tuple[float, float, float]) -> float:
    def ch(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# ----------------------------------------------------------------------------
# Background map: what will be under the content on the target canvas.
# The engine only READS asset pixels here (to decide where text goes); it
# never writes or alters them.
# ----------------------------------------------------------------------------

# Background analysis is read-only and depends only on (asset, canvas, fit),
# so decoded images and finished maps are reused across tasks.
_BG_CACHE: dict[tuple, tuple[list[list[float]], list[list[float]]]] = {}
_RGB_CACHE: dict[str, Any] = {}


def _load_rgb(path: Path):
    im = _RGB_CACHE.get(str(path))
    if im is None:
        with Image.open(path) as src:
            im = src.convert("RGB")
        if len(_RGB_CACHE) >= 4:
            _RGB_CACHE.clear()
        _RGB_CACHE[str(path)] = im
    return im


class BgMap:
    GRID = 48  # cells along the long side

    def __init__(self, TW: float, TH: float, backgrounds: list) -> None:
        self.ok = False
        self.TW, self.TH = TW, TH
        self.gw = self.gh = 0
        self.lum: list[list[float]] = []
        self.busy: list[list[float]] = []
        try:
            self._build(backgrounds)
        except Exception:
            self.ok = False

    @staticmethod
    def _asset_path(asset_id: str) -> Optional[Path]:
        if not asset_id:
            return None
        for d in _ASSET_DIRS:
            cand = d / asset_id
            if cand.is_file():
                return cand
        return None

    def _build(self, backgrounds: list) -> None:
        if Image is None or not backgrounds:
            return
        top = sorted(backgrounds, key=lambda e: e.z)[-1]
        if self.TW >= self.TH:
            self.gw, self.gh = self.GRID, max(2, int(round(self.GRID * self.TH / self.TW)))
        else:
            self.gh, self.gw = self.GRID, max(2, int(round(self.GRID * self.TW / self.TH)))
        if top.type == "shape":
            rgb = parse_rgb(top.raw.get("fill_color"))
            if rgb is None:
                return
            lum = rel_luminance(rgb)
            self.lum = [[lum] * self.gw for _ in range(self.gh)]
            self.busy = [[0.0] * self.gw for _ in range(self.gh)]
            self.ok = True
            return
        path = self._asset_path(str(top.raw.get("asset_id", "")))
        if path is None:
            return
        fit = top.raw.get("fit", "cover")
        key = (str(path), self.TW, self.TH, fit)
        cached = _BG_CACHE.get(key)
        if cached is None:
            cached = self._analyze(path, fit)
            if len(_BG_CACHE) > 64:
                _BG_CACHE.clear()
            _BG_CACHE[key] = cached
        self.lum, self.busy = cached
        self.ok = True

    def _analyze(self, path: Path, fit: str) -> tuple[list[list[float]], list[list[float]]]:
        im = _load_rgb(path)
        iw, ih = im.size
        if fit == "cover":  # the renderer's centred cover crop
            crop_w = min(iw, ih * self.TW / self.TH)
            crop_h = min(ih, iw * self.TH / self.TW)
            left, topy = (iw - crop_w) / 2, (ih - crop_h) / 2
            im = im.crop((int(left), int(topy), int(left + crop_w), int(topy + crop_h)))
        # analysis resolution: 4 px per grid cell
        aw, ah = self.gw * 4, self.gh * 4
        small = im.resize((aw, ah), Image.BILINEAR)
        lum_img = small.convert("L")
        edges = lum_img.filter(ImageFilter.FIND_EDGES)
        # One bulk read instead of per-pixel access; same summation order.
        lum_px = [v / 255.0 for v in lum_img.tobytes()]
        edge_px = [v / 255.0 for v in edges.tobytes()]
        lum = [[0.0] * self.gw for _ in range(self.gh)]
        busy = [[0.0] * self.gw for _ in range(self.gh)]
        for gy in range(self.gh):
            for gx in range(self.gw):
                acc_l = 0.0
                acc_e = 0.0
                for yy in range(gy * 4, gy * 4 + 4):
                    row = yy * aw
                    for xx in range(gx * 4 + row, gx * 4 + 4 + row):
                        acc_l += lum_px[xx]
                        acc_e += edge_px[xx]
                lum[gy][gx] = rel_luminance((acc_l / 16 * 255,) * 3)
                busy[gy][gx] = min(1.0, acc_e / 16 * 3.0)
        return lum, busy

    def sample(self, box: Box) -> tuple[float, float]:
        """Mean (luminance, busyness) of the background under `box`."""
        if not self.ok:
            return 0.0, 0.0
        x0 = clamp(box[0] / self.TW * self.gw, 0, self.gw)
        x1 = clamp(box[2] / self.TW * self.gw, 0, self.gw)
        y0 = clamp(box[1] / self.TH * self.gh, 0, self.gh)
        y1 = clamp(box[3] / self.TH * self.gh, 0, self.gh)
        if x1 <= x0 or y1 <= y0:
            return 0.0, 0.0
        tot = 0.0
        acc_l = 0.0
        acc_b = 0.0
        for gy in range(int(y0), min(self.gh, int(math.ceil(y1)))):
            wy = min(y1, gy + 1) - max(y0, gy)
            for gx in range(int(x0), min(self.gw, int(math.ceil(x1)))):
                w = wy * (min(x1, gx + 1) - max(x0, gx))
                tot += w
                acc_l += w * self.lum[gy][gx]
                acc_b += w * self.busy[gy][gx]
        if tot <= 0:
            return 0.0, 0.0
        return acc_l / tot, acc_b / tot


# ----------------------------------------------------------------------------
# Parsed elements
# ----------------------------------------------------------------------------

@dataclass
class El:
    raw: dict
    id: str
    type: str
    x: float
    y: float
    w: float
    h: float
    rot: float
    opacity: float
    z: int
    group: Optional[str]
    content: str = ""
    family: str = ""
    fs: int = 0
    bold: bool = False
    italic: bool = False
    lh: float = 1.2
    align: str = "left"
    alpha: float = 1.0
    tight: Box = (0.0, 0.0, 0.0, 0.0)
    role: str = ""
    src_lines: int = 1
    src_line_w: float = 0.0

    @property
    def area(self) -> float:
        return self.w * self.h

    @property
    def aabb(self) -> Box:
        return rect_aabb(self.x, self.y, self.w, self.h, self.rot)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def parse_element(raw: dict) -> El:
    etype = str(raw.get("type", "shape"))
    el = El(
        raw=raw,
        id=str(raw.get("id", "")),
        type=etype,
        x=_num(raw.get("x")),
        y=_num(raw.get("y")),
        w=max(0.0, _num(raw.get("width"))),
        h=max(0.0, _num(raw.get("height"))),
        rot=_num(raw.get("rotation")) % 360.0,
        opacity=clamp(_num(raw.get("opacity", 1.0), 1.0), 0.0, 1.0),
        z=int(_num(raw.get("z_index", 0))),
        group=raw.get("group_id") or None,
    )
    if etype == "text":
        el.content = str(raw.get("content", ""))
        el.family = str(raw.get("font_family", ""))
        el.fs = max(1, int(round(_num(raw.get("font_size"), 24))))
        el.bold = str(raw.get("font_weight", "regular")).lower() in ("bold", "semibold", "extrabold", "black", "heavy")
        el.italic = str(raw.get("font_style", "normal")).lower() == "italic"
        el.lh = _num(raw.get("line_height"), 1.2) or 1.2
        el.align = str(raw.get("text_align", "left"))
        el.alpha = el.opacity * parse_alpha(raw.get("color"))
    elif etype == "shape":
        el.alpha = el.opacity * parse_alpha(raw.get("fill_color"))
    else:
        el.alpha = el.opacity
    return el


# ----------------------------------------------------------------------------
# Text metrics
# ----------------------------------------------------------------------------

@dataclass
class TextFit:
    """Wrapped text at a font size. The renderer puts the first baseline at
    box_top + ascent, so painted glyphs start `top_off` below the box top
    (ascent minus cap/ascender height) and span `painted_h`."""
    fs: int
    box_w: float
    lines: list[str]
    line_ws: list[float]
    top_off: float
    painted_h: float

    @property
    def n(self) -> int:
        return len(self.lines)

    @property
    def max_line_w(self) -> float:
        return max(self.line_ws) if self.line_ws else 0.0

    @property
    def height(self) -> float:
        return self.top_off + self.painted_h


_FIT_CACHE: dict[tuple, TextFit] = {}
_WORD_CACHE: dict[tuple, float] = {}


def measure_text(el: El, fs: int, box_w: float) -> TextFit:
    """Wrap and measure `el` at `fs` in a box `box_w` wide. Results are cached
    and shared, so callers must treat the returned TextFit as read-only."""
    fs = max(1, int(fs))
    key = (el.content, el.family, el.bold, el.italic, el.lh, _num(el.raw.get("letter_spacing"), 0.0), fs, box_w)
    fit = _FIT_CACHE.get(key)
    if fit is None:
        fit = _measure_text(el, fs, box_w)
        _FIT_CACHE[key] = fit
    return fit


def _measure_text(el: El, fs: int, box_w: float) -> TextFit:
    fb = fonts()
    widths = fb.char_widths(el.family, el.bold, el.italic, fs, el.content)
    lines = wrap_lines(el.content, widths, box_w)
    ls = _num(el.raw.get("letter_spacing"), 0.0)
    line_ws = []
    idx = 0
    for ln in lines:
        line_ws.append(sum(widths[idx:idx + len(ln)]) + ls * max(0, len(ln) - 1))
        idx += len(ln) + 1
    n = max(1, len(lines))
    ascent = fb.ascent(el.family, el.bold, el.italic, fs)
    top_off = max(0.0, ascent - 0.78 * fs)
    painted = (n - 1) * fs * el.lh + 1.03 * fs
    return TextFit(fs=fs, box_w=box_w, lines=lines, line_ws=line_ws, top_off=top_off, painted_h=painted)


def _word_widths(el: El, fs: int, split_spaces: bool) -> float:
    key = (el.content, el.family, el.bold, el.italic, _num(el.raw.get("letter_spacing"), 0.0), fs, split_spaces)
    w = _WORD_CACHE.get(key)
    if w is None:
        w = _WORD_CACHE[key] = _word_widths_uncached(el, fs, split_spaces)
    return w


def _word_widths_uncached(el: El, fs: int, split_spaces: bool) -> float:
    widths = fonts().char_widths(el.family, el.bold, el.italic, fs, el.content)
    ls = _num(el.raw.get("letter_spacing"), 0.0)
    best, cur = 0.0, 0.0
    for c, w in zip(el.content, widths):
        if c == "\n" or (split_spaces and c == " "):
            best = max(best, cur)
            cur = 0.0
        else:
            cur += w + ls
    return max(best, cur)


def longest_word_w(el: El, fs: int) -> float:
    return _word_widths(el, fs, True)


def single_line_w(el: El, fs: int) -> float:
    return _word_widths(el, fs, False)


# ----------------------------------------------------------------------------
# Blocks (layout units) and classification
# ----------------------------------------------------------------------------

ROLE_IMPORTANCE = {
    "headline": 1.0, "subhead": 0.75, "body": 0.6, "kicker": 0.45, "legal": 0.25,
    "hero": 0.9, "logo": 0.8, "image": 0.6, "stamp": 0.3,
    "cta": 0.9, "badge": 0.5, "shape": 0.4, "divider": 0.3, "group": 0.6,
}


@dataclass
class Block:
    kind: str                     # text | image | shape | button | group
    els: list[El]
    role: str
    box: Box                      # source AABB (tight for text)
    shape: Optional[El] = None    # button only
    label: Optional[El] = None    # button only
    panel: Optional["Block"] = None

    @property
    def w(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def h(self) -> float:
        return self.box[3] - self.box[1]

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def importance(self) -> float:
        return ROLE_IMPORTANCE.get(self.role, 0.5)

    @property
    def is_textlike(self) -> bool:
        return self.kind == "text" or (self.kind == "button" and self.role == "badge")

    @property
    def has_flex_text(self) -> bool:
        return self.kind == "text"


@dataclass
class Analysis:
    W: float
    H: float
    els: list[El]
    backgrounds: list[El]
    decor: list[El]
    panels: list[Block]
    blocks: list[Block]
    base_fs: float = 0.0          # body-copy size: hierarchy-aware floors scale from it


def _tight_text_box(el: El) -> Box:
    fit = measure_text(el, el.fs, el.w)
    el.src_lines = fit.n
    el.src_line_w = fit.max_line_w
    tw = min(el.w, fit.max_line_w) if el.w > 0 else fit.max_line_w
    th = fit.painted_h
    y0 = el.y + fit.top_off
    if el.align == "center":
        x0 = el.x + (el.w - tw) / 2
    elif el.align == "right":
        x0 = el.x + el.w - tw
    else:
        x0 = el.x
    cx, cy = el.cx, el.cy
    tcx, tcy = x0 + tw / 2, y0 + th / 2
    if el.rot:
        t = math.radians(el.rot)
        dx, dy = tcx - cx, tcy - cy
        rx = cx + dx * math.cos(t) - dy * math.sin(t)
        ry = cy + dx * math.sin(t) + dy * math.cos(t)
        aw, ah = rot_aabb_dims(tw, th, el.rot)
        return (rx - aw / 2, ry - ah / 2, rx + aw / 2, ry + ah / 2)
    return (x0, y0, x0 + tw, y0 + th)


def analyze(source: dict) -> Analysis:
    W = max(1.0, _num(source["canvas"].get("width"), 1))
    H = max(1.0, _num(source["canvas"].get("height"), 1))
    els = [parse_element(r) for r in source.get("elements", [])]
    canvas = (0.0, 0.0, W, H)
    cA = W * H

    for el in els:
        el.tight = _tight_text_box(el) if el.type == "text" else el.aabb

    # --- backgrounds: anything covering (almost) the whole canvas -----------
    backgrounds: list[El] = []
    rest: list[El] = []
    for el in els:
        cover = box_inter(el.aabb, canvas) / cA
        if el.type in ("image", "shape") and cover >= 0.80 and el.group is None:
            backgrounds.append(el)
        else:
            rest.append(el)

    def union_of(members: list[El]) -> Box:
        b = members[0].tight
        for m in members[1:]:
            b = box_union(b, m.tight)
        return b

    blocks: list[Block] = []
    consumed: set[str] = set()

    def make_button(shape: El, label: El, members: list[El]) -> Block:
        return Block(kind="button", els=members, role="badge", box=union_of(members), shape=shape, label=label)

    # --- explicit groups -----------------------------------------------------
    groups: dict[str, list[El]] = {}
    for el in rest:
        if el.group:
            groups.setdefault(el.group, []).append(el)
    for members in groups.values():
        shapes = [m for m in members if m.type == "shape"]
        texts = [m for m in members if m.type == "text"]
        if (len(members) == 2 and len(shapes) == 1 and len(texts) == 1
                and box_contains(shapes[0].aabb, texts[0].aabb, tol=0.25 * max(texts[0].w, texts[0].h, 1.0))):
            blocks.append(make_button(shapes[0], texts[0], members))
        else:
            blocks.append(Block(kind="group", els=members, role="group", box=union_of(members)))
        consumed.update(m.id for m in members)

    singles = [el for el in rest if not el.group]

    # --- implicit containment: buttons, overlays, panels ---------------------
    def contained_in(container: El) -> list[El]:
        tol = 0.02 * max(container.w, container.h)
        out = []
        for e in singles:
            if e is container or e.id in consumed or e.z < container.z:
                continue
            if e.type == "shape" and e.alpha < 0.5:
                continue
            if box_contains(container.aabb, e.tight, tol):
                out.append(e)
        return out

    # buttons: a small opaque shape holding exactly one text
    for s in sorted(singles, key=lambda e: e.area):
        if s.type != "shape" or s.id in consumed or s.alpha < 0.5:
            continue
        inside = contained_in(s)
        if len(inside) == 1 and inside[0].type == "text":
            t = inside[0]
            line_box = t.fs * max(t.lh, 1.0)
            tw = t.tight[2] - t.tight[0]
            if s.h <= 4.0 * line_box and s.w <= 5.0 * max(tw, 1.0):
                blocks.append(make_button(s, t, [s, t]))
                consumed.update((s.id, t.id))

    # overlays: text (or small things) sitting fully on a non-background image
    for im in sorted(singles, key=lambda e: -e.area):
        if im.type != "image" or im.id in consumed:
            continue
        inside = [e for e in contained_in(im) if e.type == "text" or e.area < 0.5 * im.area]
        if inside:
            members = [im] + inside
            blocks.append(Block(kind="group", els=members, role="image", box=union_of(members)))
            consumed.update(m.id for m in members)

    # panels: opaque shapes holding several things
    panels: list[Block] = []
    panel_of: dict[str, Block] = {}
    for s in sorted(singles, key=lambda e: -e.area):
        if s.type != "shape" or s.id in consumed or s.alpha < 0.5:
            continue
        tol = 0.02 * max(s.w, s.h)
        inner_els = contained_in(s)
        inner_blocks = [b for b in blocks if b.kind in ("button", "group") and b.panel is None
                        and all(m.z >= s.z for m in b.els) and box_contains(s.aabb, b.box, tol)]
        count = len(inner_els) + len(inner_blocks)
        if count == 0:
            continue
        one_area = box_area(inner_els[0].tight) if inner_els else box_area(inner_blocks[0].box)
        if count >= 2 or s.area > 4 * max(one_area, 1.0):
            pb = Block(kind="shape", els=[s], role="panel", box=s.aabb)
            panels.append(pb)
            consumed.add(s.id)
            for b in inner_blocks:
                b.panel = pb
            for e in inner_els:
                panel_of[e.id] = pb

    # --- the rest: decorative vs flow leaves ---------------------------------
    decor: list[El] = []
    leaves: list[El] = []
    for el in singles:
        if el.id in consumed:
            continue
        if el.type == "shape":
            bleed = 1.0 - (box_inter(el.aabb, canvas) / max(el.area, 1e-6))
            thin = min(el.w, el.h) < 0.015 * min(W, H)
            if el.alpha < 0.5 or bleed > 0.05 or (el.area >= 0.10 * cA and not thin):
                decor.append(el)
                continue
            leaves.append(el)
        elif el.type == "image":
            bleed = 1.0 - (box_inter(el.aabb, canvas) / max(el.area, 1e-6))
            if bleed > 0.25 and el.opacity < 0.5:
                decor.append(el)
            else:
                leaves.append(el)
        else:
            leaves.append(el)

    # --- roles -----------------------------------------------------------------
    images = [e for e in leaves if e.type == "image"]
    texts = [e for e in rest if e.type == "text"]
    by_area = sorted(images, key=lambda e: e.area, reverse=True)
    biggest_img = by_area[0] if by_area else None
    clear_hero = len(by_area) < 2 or by_area[0].area >= 1.2 * by_area[1].area
    for e in images:
        frac = e.area / cA
        if e is biggest_img and frac >= 0.04 and clear_hero:
            e.role = "hero"
        elif frac < 0.08 and e.cy < 0.30 * H:
            e.role = "logo"
        elif frac < 0.05 and e.cy > 0.70 * H:
            e.role = "stamp"
        elif frac < 0.05 and (e.cx < 0.2 * W or e.cx > 0.8 * W):
            e.role = "logo"
        else:
            e.role = "image"
    if texts:
        def tsize(t: El) -> float:
            return t.fs * (1.15 if t.bold else 1.0)
        order = sorted(texts, key=tsize, reverse=True)
        top = tsize(order[0])
        for i, t in enumerate(order):
            r = tsize(t) / top if top else 1.0
            nwords = len(t.content.split())
            small_footer = t.fs / max(W, H) <= 0.013 and t.cy > 0.80 * H and len(t.content) >= 40
            if i == 0:
                t.role = "headline"
            elif small_footer:
                t.role = "legal"
            elif r <= 0.3 or (nwords <= 3 and r < 0.6):
                t.role = "kicker"
            elif r >= 0.42 and i == 1:
                t.role = "subhead"
            else:
                t.role = "body"
    for e in leaves:
        if e.type == "shape":
            e.role = "divider" if min(e.w, e.h) < 0.015 * min(W, H) else "shape"

    for e in leaves:
        blocks.append(Block(kind=e.type, els=[e], role=e.role, box=e.tight, panel=panel_of.get(e.id)))

    # buttons: the dominant one in the lower part is the CTA, the others badges
    buttons = [b for b in blocks if b.kind == "button"]
    if buttons:
        def bscore(b: Block) -> float:
            return (b.label.fs if b.label else 1) * (b.shape.area if b.shape else 1.0)
        main = max(buttons, key=bscore)
        for b in buttons:
            b.role = "cta" if (b is main and b.cy >= 0.45 * H) else "badge"
        if main.role != "cta" and main.cy >= 0.3 * H:
            main.role = "cta"
    for b in blocks:
        for e in b.els:
            if not e.role:
                e.role = b.role

    body_fs = [t.fs for t in texts if t.role in ("body", "subhead")]
    if not body_fs:
        body_fs = [t.fs for t in texts if t.role != "legal"]
    base_fs = float(min(body_fs)) if body_fs else 0.0
    return Analysis(W=W, H=H, els=els, backgrounds=backgrounds, decor=decor, panels=panels, blocks=blocks,
                    base_fs=base_fs)


# ----------------------------------------------------------------------------
# XY-cut layout tree
# ----------------------------------------------------------------------------

@dataclass
class Node:
    axis: str                              # 'v' (stack), 'h' (row), 'leaf'
    children: list["Node"] = field(default_factory=list)
    block: Optional[Block] = None
    box: Box = (0.0, 0.0, 0.0, 0.0)
    overlay: bool = False
    synthetic: bool = False   # built by the engine (column of lanes), not found in the source

    @property
    def is_leaf(self) -> bool:
        return self.axis == "leaf"

    def blocks(self) -> list[Block]:
        if self.is_leaf:
            return [self.block]  # type: ignore[list-item]
        out: list[Block] = []
        for c in self.children:
            out.extend(c.blocks())
        return out

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def w(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def h(self) -> float:
        return self.box[3] - self.box[1]

    def has_text(self) -> bool:
        return any(b.has_flex_text for b in self.blocks())

    def gaps(self) -> list[float]:
        """Source whitespace between consecutive children along `axis`."""
        g = []
        for a, b in zip(self.children, self.children[1:]):
            if self.axis == "v":
                g.append(max(0.0, b.box[1] - a.box[3]))
            else:
                g.append(max(0.0, b.box[0] - a.box[2]))
        return g


def _split_by_gaps(items: list[Node], axis: str, min_gap: float) -> list[list[Node]]:
    i0, i1 = (1, 3) if axis == "v" else (0, 2)
    items = sorted(items, key=lambda n: (n.box[i0], n.box[i1]))
    groups: list[list[Node]] = [[items[0]]]
    end = items[0].box[i1]
    for n in items[1:]:
        if n.box[i0] >= end + min_gap:
            groups.append([n])
            end = n.box[i1]
        else:
            groups[-1].append(n)
            end = max(end, n.box[i1])
    return groups


def _node_box(children: list[Node]) -> Box:
    b = children[0].box
    for c in children[1:]:
        b = box_union(b, c.box)
    return b


def _largest_gap_split(items: list[Node], axis: str, tol: float) -> Optional[tuple[list[Node], list[Node], float]]:
    """Binary split of `items` at the widest whitespace band along `axis`.
    Returns (before, after, gap) or None when nothing separates them."""
    groups = _split_by_gaps(items, axis, -tol)
    if len(groups) < 2:
        return None
    i0, i1 = (1, 3) if axis == "v" else (0, 2)
    best_i, best_gap = 0, -1.0
    for gi, (a, b) in enumerate(zip(groups, groups[1:])):
        gap = min(n.box[i0] for n in b) - max(n.box[i1] for n in a)
        if gap > best_gap:
            best_i, best_gap = gi, gap
    before = [n for g in groups[: best_i + 1] for n in g]
    after = [n for g in groups[best_i + 1:] for n in g]
    return before, after, best_gap


def build_tree(leaves: list[Node], W: float, H: float) -> Node:
    """Recursive XY-cut: split at the widest whitespace band (horizontal or
    vertical), recurse, then flatten same-axis nesting so a stack of rows
    is one node with all its rows as children."""
    if len(leaves) == 1:
        return leaves[0]
    sv = _largest_gap_split(leaves, "v", max(1.0, 0.005 * H))
    sh = _largest_gap_split(leaves, "h", max(1.0, 0.005 * W))
    if sv is None and sh is None:  # overlapping cluster: reading order, as a stack
        ordered = sorted(leaves, key=lambda n: (n.cy, n.cx))
        return Node(axis="v", children=ordered, box=_node_box(ordered), overlay=True)
    if sv is not None and sh is not None:
        gv, gh = sv[2] / H, sh[2] / W
        use_v = (gv > gh * 1.25) if W > H else (gv * 1.25 >= gh)
    else:
        use_v = sv is not None
    before, after, _ = sv if use_v else sh  # type: ignore[misc]
    axis = "v" if use_v else "h"
    children: list[Node] = []
    for part in (before, after):
        child = build_tree(part, W, H)
        if not child.is_leaf and child.axis == axis and not child.overlay:
            children.extend(child.children)  # flatten same-axis nesting
        else:
            children.append(child)
    return Node(axis=axis, children=children, box=_node_box(children))


def prune_role(node: Node, role: str) -> tuple[Optional[Node], list[Node]]:
    """Remove leaves with `role` from the tree; returns (tree, removed)."""
    if node.is_leaf:
        return (None, [node]) if node.block.role == role else (node, [])
    removed: list[Node] = []
    kept: list[Node] = []
    for c in node.children:
        nc, r = prune_role(c, role)
        removed.extend(r)
        if nc is not None:
            kept.append(nc)
    if not kept:
        return None, removed
    if len(kept) == 1:
        return kept[0], removed
    return Node(axis=node.axis, children=kept, box=_node_box(kept), overlay=node.overlay), removed


# ----------------------------------------------------------------------------
# Layout plans
# ----------------------------------------------------------------------------

@dataclass
class LeafPlan:
    w: float                      # AABB width in target
    h: float                      # AABB height in target
    scale: float = 1.0            # for rigid parts
    fs: int = 0                   # text font size
    text_w: float = 0.0           # text box width (unrotated)
    text_h: float = 0.0           # painted text height (unrotated)
    text_top: float = 0.0         # renderer box top sits this far above the paint
    label_h: float = 0.0          # button label painted height
    shape_w: float = 0.0
    shape_h: float = 0.0
    penalty: float = 0.0


@dataclass
class Plan:
    w: float
    h: float
    leaf: Optional[LeafPlan] = None
    block: Optional[Block] = None
    children: list[tuple[float, float, "Plan"]] = field(default_factory=list)  # (dx, dy, plan)
    penalty: float = 0.0
    wrapped: bool = False

    def flatten(self, ox: float, oy: float, out: list) -> None:
        if self.leaf is not None:
            out.append((self.block, self.leaf, ox, oy))
        for dx, dy, p in self.children:
            p.flatten(ox + dx, oy + dy, out)


@dataclass
class Ctx:
    W: float            # source
    H: float
    TW: float           # target
    TH: float
    k: float
    kw: float
    kh: float
    khi: float
    mode: str           # 'v' | 'h'
    margin: float
    gmin: float
    fmin: float
    floor_relax: float = 1.0   # fmin / fmin_top: image floors relax with the text floor
    hero_boost: float = 1.0    # the hero image may grow past k by this factor


def _anchor_offset(rel_left: float, rel_right: float, rel_c: float, span: float, size: float) -> float:
    """Cross-axis offset of a child inside `span`, from its source alignment."""
    if size >= span:
        return 0.0
    if abs(rel_c - 0.5) < 0.06:
        return (span - size) / 2
    if rel_left < 0.06:
        return 0.0
    if rel_right < 0.06:
        return span - size
    return clamp(rel_c * span - size / 2, 0.0, span - size)


def share_width(prefs: dict[int, float], remaining: float) -> dict[int, float]:
    """Split `remaining` among flexible children proportionally to their
    preferred widths, never handing out more than the preference."""
    remaining = max(remaining, 0.0)
    widths = {i: 0.0 for i in prefs}
    open_ids = set(prefs)
    budget = remaining
    while open_ids:
        tot = sum(prefs[i] for i in open_ids)
        if tot <= budget:
            for i in open_ids:
                widths[i] = prefs[i]
            break
        for i in open_ids:
            widths[i] = budget * prefs[i] / max(tot, 1e-6)
        break
    return widths


class Layouter:
    def __init__(self, an: Analysis, ctx: Ctx):
        self.an = an
        self.ctx = ctx
        self.compact = False  # buttons trim their padding (~1.1 * fs) to fit a tight row
        self.keep_pad = False  # a lifted label keeps the source padding (rows of chips that fit)

    # ---- leaves -------------------------------------------------------------
    def role_fmin(self, role: str, el: Optional[El] = None) -> int:
        f = self.ctx.fmin
        if role == "legal":
            f *= 0.7
        elif role in ("kicker", "badge"):
            f *= 0.8
        elif el is not None and self.an.base_fs > 0 and el.fs > self.an.base_fs:
            # keep the hierarchy when everything sits on the floor: a headline
            # stays bigger than the body copy it was bigger than in the source
            f *= min(2.0, math.sqrt(el.fs / self.an.base_fs))
        return max(1, int(round(f)))

    # smallest sensible size (fraction of the shorter target side) per role
    IMAGE_FLOORS = {"logo": 0.14, "hero": 0.30, "image": 0.20, "stamp": 0.08}

    def text_plan(self, block: Block, k: float, max_w: float, packed: bool) -> LeafPlan:
        el = block.els[0]
        c = self.ctx
        fmin = self.role_fmin(block.role, el)
        fs = max(fmin, int(round(el.fs * k)))
        max_w = max(1.0, max_w)
        if packed:
            box_w = min(max_w, single_line_w(el, fs) + 2)
        else:
            box_w = el.w * (max(k, c.kw) if c.mode == "v" else k)
            if el.src_lines == 1:  # a one-liner may keep its natural width
                box_w = max(box_w, single_line_w(el, fs) + 2)
            box_w = min(max_w, box_w)
        box_w = max(1.0, box_w)
        lw = longest_word_w(el, fs)
        if lw > box_w > 0:
            fs = max(1, int(fs * box_w / lw))
        fit = measure_text(el, fs, box_w)
        if packed:
            box_w = max(1.0, min(box_w, fit.max_line_w + 2))
            fit = measure_text(el, fs, box_w)
        elif fit.n > el.src_lines and box_w < max_w:
            # wrapping more than the source did: use the room the lane offers
            wide = measure_text(el, fs, max_w)
            if wide.n < fit.n:
                box_w = max(1.0, min(max_w, wide.max_line_w + 2))
                fit = measure_text(el, fs, box_w)
        pen = block.importance * max(0, fit.n - el.src_lines) / max(1, el.src_lines)
        if fs < fmin:
            pen += block.importance * 0.6
        aw, ah = rot_aabb_dims(box_w, fit.painted_h, el.rot)
        return LeafPlan(w=aw, h=ah, scale=k, fs=fs, text_w=box_w, text_h=fit.painted_h,
                        text_top=fit.top_off, penalty=pen)

    def rigid_plan(self, block: Block, k: float, max_w: float, max_h: float) -> LeafPlan:
        s = k
        pen = 0.0
        c = self.ctx
        if block.role == "hero":
            s = max(k, min(k * c.hero_boost, c.khi))
        if block.kind == "image" and block.role in self.IMAGE_FLOORS and block.h > 0:
            floor_h = self.IMAGE_FLOORS[block.role] * min(c.TW, c.TH)
            if block.role != "logo":  # the brand mark keeps its full floor
                floor_h *= c.floor_relax
            s = max(s, min(floor_h / block.h, c.khi))
        if block.kind == "group":
            for e in block.els:
                if e.type == "text" and e.fs > 0:
                    s = max(s, self.role_fmin(e.role or "body", e) / e.fs)
        w, h = block.w * s, block.h * s
        if w > max_w or h > max_h:
            f = min(max_w / max(w, 1e-6), max_h / max(h, 1e-6))
            s *= f
            w, h = block.w * s, block.h * s
            if f < 0.6:
                pen += block.importance * (0.6 - f)
        return LeafPlan(w=w, h=h, scale=s, penalty=pen)

    def button_plan(self, block: Block, k: float, max_w: float, max_h: float) -> LeafPlan:
        sh, lb = block.shape, block.label
        assert sh is not None and lb is not None
        fmin = self.role_fmin(block.role, lb)
        fs = max(fmin, int(round(lb.fs * k)))
        s = max(k, fs / max(lb.fs, 1))
        shape_w, shape_h = sh.w * s, sh.h * s
        lw = single_line_w(lb, fs)
        source_pad = max(0.0, (sh.w - lb.src_line_w) / 2)
        if s > k * 1.05:
            # label lifted to the floor: wrap the shape around the label instead
            # of scaling the whole shape up, so a tag never balloons
            pad = max(0.7 * fs, source_pad * (s if self.keep_pad else k))
            shape_w = lw + 2 * pad
            shape_h = min(shape_h, max(sh.h * k, 2 * fs))
        else:
            pad = max(0.6 * fs, source_pad * s)
            if lw + 2 * pad > shape_w:
                shape_w = lw + 2 * pad
        tight_w = lw + 2 * max(0.6 * fs, min(pad, 1.1 * fs))
        if self.compact:
            shape_w = min(shape_w, tight_w)
        pen = 0.0
        aw, ah = rot_aabb_dims(shape_w, shape_h, sh.rot)
        if aw > max_w and shape_w > tight_w:  # trim the padding before the label
            shape_w = tight_w
            aw, ah = rot_aabb_dims(shape_w, shape_h, sh.rot)
        if aw > max_w or ah > max_h:
            f = min(max_w / max(aw, 1e-6), max_h / max(ah, 1e-6))
            shape_w *= f
            shape_h *= f
            fs = max(1, int(fs * f))
            pen += block.importance * (1 - f) * 0.5
            aw, ah = rot_aabb_dims(shape_w, shape_h, sh.rot)
        if fs < fmin:
            pen += block.importance * 0.6
        lfit = measure_text(lb, fs, 1e9)
        return LeafPlan(w=aw, h=ah, scale=s, fs=fs, label_h=lfit.painted_h, text_top=lfit.top_off,
                        shape_w=shape_w, shape_h=shape_h, penalty=pen)

    def leaf_plan(self, block: Block, k: float, max_w: float, max_h: float, packed: bool) -> LeafPlan:
        if block.kind == "text":
            return self.text_plan(block, k, max_w, packed)
        if block.kind == "button":
            return self.button_plan(block, k, max_w, max_h)
        return self.rigid_plan(block, k, max_w, max_h)

    # ---- nodes --------------------------------------------------------------
    def pref_w(self, node: Node, k: float, max_w: float) -> float:
        if node.is_leaf:
            b = node.block
            if b.kind == "text":
                el = b.els[0]
                fs = max(self.role_fmin(b.role, el), int(round(el.fs * k)))
                return min(max_w, single_line_w(el, fs) + 2)
            return self.leaf_plan(b, k, max_w, 1e9, True).w
        if node.axis == "v":
            return max(self.pref_w(c, k, max_w) for c in node.children)
        return sum(self.pref_w(c, k, max_w) for c in node.children) + sum(self.node_gaps(node, k))

    def node_gaps(self, node: Node, k: float, packed: bool = False) -> list[float]:
        c = self.ctx
        if node.axis == "v":
            fss = [float(self.role_fmin("body"))]
            for b in node.blocks():
                for e in b.els:
                    if e.type == "text":
                        fss.append(e.fs * k)
            cap = max(0.8 * max(fss), 0.05 * min(c.TW, c.TH))
        else:
            cap = max((0.02 if packed else 0.05) * c.TW, 3 * c.gmin)
        if node.synthetic:  # lanes stacked by the engine: give them real separation
            lo = max(2 * c.gmin, 0.05 * min(c.TW, c.TH))
            return [clamp(g * k, lo, max(lo, 0.1 * min(c.TW, c.TH))) for g in node.gaps()]
        return [c.gmin if node.overlay else clamp(g * k, c.gmin, max(c.gmin, cap)) for g in node.gaps()]

    def plan(self, node: Node, k: float, max_w: float, max_h: float, packed: bool = False) -> Plan:
        if node.is_leaf:
            lp = self.leaf_plan(node.block, k, max_w, max_h, packed)
            return Plan(w=lp.w, h=lp.h, leaf=lp, block=node.block, penalty=lp.penalty)
        if node.axis == "v":
            return self.plan_stack(node, k, max_w, max_h, packed)
        return self.plan_row(node, k, max_w, max_h, packed)

    def plan_stack(self, node: Node, k: float, max_w: float, max_h: float, packed: bool) -> Plan:
        gaps = self.node_gaps(node, k)
        plans = [self.plan(c, k, max_w, max_h, packed) for c in node.children]
        if node.synthetic:
            self._restack_small_row(node, plans, gaps, k, max_w, max_h, packed)
        w = max(p.w for p in plans)
        h = sum(p.h for p in plans) + sum(gaps)
        out = Plan(w=w, h=h, penalty=sum(p.penalty for p in plans))
        align = self._column_align(node) if node.synthetic else None
        y = 0.0
        for i, (c, p) in enumerate(zip(node.children, plans)):
            side = self._stamp_side(c) if node.synthetic else None
            if side == "left" or (side is None and align == "left"):
                x = 0.0
            elif side == "right":
                x = w - p.w
            elif align == "right":
                x = w - p.w
            elif align == "center":
                x = (w - p.w) / 2
            else:
                nw = max(node.w, 1e-6)
                x = _anchor_offset((c.box[0] - node.box[0]) / nw, (node.box[2] - c.box[2]) / nw,
                                   (c.cx - node.box[0]) / nw, w, p.w)
            out.children.append((x, y, p))
            y += p.h + (gaps[i] if i < len(gaps) else 0.0)
        return out

    def _stamp_side(self, c: Node) -> Optional[str]:
        """A small stamp keeps the side of the canvas it sat on in the source
        (a bottom-right seal stays right) instead of the column's alignment."""
        if not c.is_leaf or c.block.role != "stamp":
            return None
        rel = c.cx / max(self.ctx.W, 1e-6)
        if rel > 0.6:
            return "right"
        if rel < 0.4:
            return "left"
        return None

    def _column_align(self, node: Node) -> str:
        """One alignment for every child of an engine-built column, read from
        how the children line up in the source."""
        tol = 0.04 * self.ctx.W
        kids = node.children

        def spread(vals: list[float]) -> float:
            return max(vals) - min(vals)
        if spread([c.cx for c in kids]) <= tol:
            return "center"
        if spread([c.box[0] for c in kids]) <= tol:
            return "left"
        if spread([c.box[2] for c in kids]) <= tol:
            return "right"
        return "center"

    def _restack_small_row(self, node: Node, plans: list[Plan], gaps: list[float], k: float,
                           max_w: float, max_h: float, packed: bool) -> None:
        """In an engine-built column, a row of a few small non-text items (logo
        + sticker) that sets the column width is stacked instead when that is
        narrower and the column still fits, so the column is not wide and empty."""
        cA = self.ctx.W * self.ctx.H
        widest = max(range(len(plans)), key=lambda i: plans[i].w)
        c = node.children[widest]
        if c.is_leaf or c.axis != "h" or len(c.children) > 4:
            return
        if not all(ch.is_leaf and ch.block.kind != "text" and box_area(ch.box) < 0.06 * cA for ch in c.children):
            return
        stack = Node(axis="v", children=list(c.children), box=c.box, synthetic=True)
        sp = self.plan(stack, k, max_w, max_h, packed)
        h = sum(p.h for p in plans) + sum(gaps) - plans[widest].h + sp.h
        if sp.w < plans[widest].w and h <= max_h:
            plans[widest] = sp

    def plan_row(self, node: Node, k: float, max_w: float, max_h: float, packed: bool = False) -> Plan:
        gaps = self.node_gaps(node, k, packed)
        kids = node.children
        done, total_w = self._plan_row_children(node, k, max_w, max_h, packed, gaps)
        if total_w > max_w * 1.001 and len(kids) > 1:
            if self.compact or not any(b.kind == "button" for b in node.blocks()):
                return self.plan_wrapped_row(node, k, max_w, max_h)
            natural = done
            self.compact = True  # trim button padding before wrapping the row
            try:
                done, total_w = self._plan_row_children(node, k, max_w, max_h, packed, gaps)
            finally:
                self.compact = False
            if total_w > max_w * 1.001:
                return self.plan_wrapped_row(node, k, max_w, max_h)
            total_w += self._give_back_padding(kids, natural, done, 0.85 * (max_w - total_w))
        elif not self.compact and len(kids) > 1 and all(c.is_leaf and c.block.kind == "button" for c in kids):
            # a row of chips keeps its source padding when that still fits, so
            # the chips stay evenly weighted instead of shrink-wrapped
            self.keep_pad = True
            try:
                roomy, roomy_w = self._plan_row_children(node, k, max_w, max_h, packed, gaps)
            finally:
                self.keep_pad = False
            if roomy_w <= max_w:
                done, total_w = roomy, roomy_w
        h = max(p.h for p in done)
        out = Plan(w=total_w, h=h, penalty=sum(p.penalty for p in done))
        x = 0.0
        nh = max(node.h, 1e-6)
        for i, (c, p) in enumerate(zip(kids, done)):
            if packed:
                y = (h - p.h) / 2
            else:
                y = _anchor_offset((c.box[1] - node.box[1]) / nh, (node.box[3] - c.box[3]) / nh,
                                   (c.cy - node.box[1]) / nh, h, p.h)
            out.children.append((x, y, p))
            x += p.w + (gaps[i] if i < len(gaps) else 0.0)
        return out

    @staticmethod
    def _give_back_padding(kids: list[Node], natural: list[Plan], compact: list[Plan], slack: float) -> float:
        """Compact padding trims every button to ~1.1 * fs; hand the row's
        leftover width back so the buttons only lose the padding they must.
        Returns the width added."""
        lost = []
        for c, n, p in zip(kids, natural, compact):
            ok = c.is_leaf and c.block.kind == "button" and p.leaf is not None and n.leaf is not None
            lost.append(max(0.0, n.leaf.shape_w - p.leaf.shape_w) if ok else 0.0)
        tot = sum(lost)
        if tot <= 0 or slack <= 0:
            return 0.0
        r = min(1.0, slack / tot)
        added = 0.0
        for c, p, d in zip(kids, compact, lost):
            if d <= 0:
                continue
            lp = p.leaf
            lp.shape_w += d * r
            aw, ah = rot_aabb_dims(lp.shape_w, lp.shape_h, c.block.shape.rot)
            added += aw - lp.w
            lp.w, lp.h = aw, ah
            p.w, p.h = aw, ah
        return added

    def _plan_row_children(self, node: Node, k: float, max_w: float, max_h: float, packed: bool,
                           gaps: list[float]) -> tuple[list[Plan], float]:
        kids = node.children
        flex = [i for i, c in enumerate(kids) if c.has_text()]
        plans: list[Optional[Plan]] = [None] * len(kids)
        for i, c in enumerate(kids):
            if i not in flex:
                plans[i] = self.plan(c, k, max_w, max_h, packed=True)
        rigid_w = sum(p.w for p in plans if p is not None) + sum(gaps)
        if flex:
            prefs = {i: self.pref_w(kids[i], k, max_w) for i in flex}
            widths = share_width(prefs, max_w - rigid_w)
            for i in flex:
                plans[i] = self.plan(kids[i], k, max(1.0, widths[i]), max_h, packed=True)
        done: list[Plan] = [p for p in plans if p is not None]
        return done, sum(p.w for p in done) + sum(gaps)

    def plan_wrapped_row(self, node: Node, k: float, max_w: float, max_h: float) -> Plan:
        """A row that does not fit flex-wraps its children into several rows."""
        c = self.ctx
        gap = max(c.gmin, 0.02 * c.TW)
        rows: list[list[Plan]] = [[]]
        x = 0.0
        was_compact, self.compact = self.compact, True
        try:
            for child in node.children:
                p = self.plan(child, k, max_w, max_h, packed=True)
                if rows[-1] and x + p.w > max_w:
                    rows.append([])
                    x = 0.0
                rows[-1].append(p)
                x += p.w + gap
        finally:
            self.compact = was_compact
        row_gap = max(c.gmin, 0.5 * gap)
        w = max(sum(p.w for p in r) + gap * (len(r) - 1) for r in rows)
        out = Plan(w=w, h=0.0, penalty=0.4 + 0.1 * len(rows) + sum(p.penalty for r in rows for p in r), wrapped=True)
        y = 0.0
        for r in rows:
            rw = sum(p.w for p in r) + gap * (len(r) - 1)
            rh = max(p.h for p in r)
            x = (w - rw) / 2
            for p in r:
                out.children.append((x, y + (rh - p.h) / 2, p))
                x += p.w + gap
            y += rh + row_gap
        out.h = y - row_gap
        return out


# ----------------------------------------------------------------------------
# 1-D spacing solver: keep desired centres, enforce order, gaps and bounds
# ----------------------------------------------------------------------------

def solve_1d(desired: list[float], sizes: list[float], gaps: list[float], lo: float, hi: float) -> list[float]:
    n = len(sizes)
    if n == 0:
        return []
    span = hi - lo
    if sum(sizes) + sum(gaps) > span:  # compress the gaps first
        slack = max(0.0, span - sum(sizes))
        gsum = sum(gaps) or 1.0
        gaps = [g * slack / gsum for g in gaps]
    starts = [d - s / 2 for d, s in zip(desired, sizes)]
    starts[0] = max(lo, starts[0])
    for i in range(1, n):
        starts[i] = max(starts[i], starts[i - 1] + sizes[i - 1] + gaps[i - 1])
    starts[-1] = min(starts[-1], hi - sizes[-1])
    for i in range(n - 2, -1, -1):
        starts[i] = min(starts[i], starts[i + 1] - gaps[i] - sizes[i])
    return starts


def justify(sizes: list[float], base_gaps: list[float], span: float, max_gap: float) -> tuple[float, list[float]]:
    """Centre the sequence in `span`, growing gaps (capped) with leftover."""
    leftover = max(0.0, span - sum(sizes) - sum(base_gaps))
    gaps = list(base_gaps)
    if gaps and leftover > 0:
        room = [max(0.0, max_gap - g) for g in gaps]
        tot_room = sum(room)
        add = min(leftover, tot_room)
        if tot_room > 0:
            gaps = [g + add * r / tot_room for g, r in zip(gaps, room)]
        leftover -= add
    return leftover / 2, gaps


# ----------------------------------------------------------------------------
# Main engine
# ----------------------------------------------------------------------------

@dataclass
class Placement:
    x: float
    y: float
    w: float
    h: float
    fs: Optional[int] = None


class Engine:
    def __init__(self, source: dict, target: dict):
        self.an = analyze(source)
        self.TW = max(1.0, _num(target.get("width"), 1))
        self.TH = max(1.0, _num(target.get("height"), 1))
        W, H = self.an.W, self.an.H
        self.kw, self.kh = self.TW / W, self.TH / H
        tmin = min(self.TW, self.TH)
        self.margin = max(2.0, 0.03 * tmin)
        self.gmin = max(2.0, 0.012 * tmin)
        self.fmin = max(10.0, 0.0085 * max(self.TW, self.TH))
        self.fmin_top = self.fmin
        self.mode = "h" if self.TW / self.TH >= 1.3 else "v"
        self.out: dict[str, Placement] = {}
        self.k_used = min(self.kw, self.kh)
        self.hero_boost = 1.0

    # ---- lanes ----------------------------------------------------------------
    def lanes(self, root: Node) -> tuple[Node, list[Node], list[Node]]:
        """Main-axis lanes for the target (+ footer lanes in horizontal mode)."""
        footer: list[Node] = []
        if self.mode == "h":
            pruned, footer = prune_role(root, "legal")
            if pruned is not None:
                root = pruned
            else:
                footer = []
        if root.is_leaf:
            kids = [root]
        elif self.mode == "v":
            kids = list(root.children)
        else:
            kids = list(root.children) if root.axis == "h" else self._merge_text_runs(root.children)
        return root, kids, footer

    def _merge_text_runs(self, kids: list[Node]) -> list[Node]:
        def textish(n: Node) -> bool:
            if n.is_leaf:
                return n.block.is_textlike
            return all(textish(c) for c in n.children)

        out: list[Node] = []
        run: list[Node] = []

        def flush() -> None:
            if len(run) == 1:
                out.append(run[0])
            elif run:
                out.append(Node(axis="v", children=list(run), box=_node_box(run)))
            run.clear()

        for n in kids:
            if textish(n):
                run.append(n)
            else:
                flush()
                out.append(n)
        flush()
        return out

    # ---- driver ---------------------------------------------------------------
    def layout(self) -> None:
        an = self.an
        if an.blocks:
            root = build_tree(self._top_level_nodes(), an.W, an.H)
            root, lanes, footer = self.lanes(root)
            self._layout_flow(root, lanes, footer)
            self._shift_for_background()
        self._place_backgrounds()
        self._place_decor()
        self._place_panels()
        self._enforce_bounds()

    def _shift_for_background(self) -> None:
        """Nudge the whole content block so free-standing text avoids busy or
        low-contrast regions of the background. Cheap grid search over a few
        candidate offsets, with a displacement penalty so a calm background
        leaves the layout exactly where the structure put it."""
        bg = BgMap(self.TW, self.TH, self.an.backgrounds)
        if not bg.ok:
            return
        flow_ids = [e.id for b in self.an.blocks for e in b.els if e.id in self.out]
        if not flow_ids:
            return
        texts = []
        for b in self.an.blocks:
            if b.kind != "text":
                continue
            el = b.els[0]
            if el.id not in self.out:
                continue
            rgb = parse_rgb(el.raw.get("color")) or (0.0, 0.0, 0.0)
            texts.append((el, rel_luminance(rgb), b.importance))
        if not texts:
            return
        els = {e.id: e for e in self.an.els}
        u: Optional[Box] = None
        for eid in flow_ids:
            p = self.out[eid]
            b = rect_aabb(p.x, p.y, p.w, p.h, els[eid].rot)
            u = b if u is None else box_union(u, b)
        assert u is not None
        m = self.margin if (self.TW > 2 * self.margin + 1 and self.TH > 2 * self.margin + 1) else 0.0
        free_l, free_r = max(0.0, u[0] - m), max(0.0, self.TW - m - u[2])
        free_t, free_b = max(0.0, u[1] - m), max(0.0, self.TH - m - u[3])

        def cands(neg: float, pos: float, n: int = 7) -> list[float]:
            vals = {0.0}
            for i in range(1, n + 1):
                vals.add(-neg * i / n)
                vals.add(pos * i / n)
            return sorted(vals)

        def cost(dx: float, dy: float) -> float:
            tot_w = 0.0
            acc = 0.0
            for el, tl, imp in texts:
                p = self.out[el.id]
                box = rect_aabb(p.x + dx, p.y + dy, p.w, p.h, el.rot)
                lum, busy = bg.sample(box)
                cr = contrast_ratio(tl, lum)
                c = busy + 0.6 * max(0.0, 3.0 - cr) / 3.0
                acc += imp * c
                tot_w += imp
            base = acc / max(tot_w, 1e-6)
            return base + 0.25 * (abs(dx) / self.TW + abs(dy) / self.TH)

        best = (cost(0.0, 0.0), 0.0, 0.0)
        base_cost = best[0]
        xs = cands(free_l, free_r) if free_l + free_r > 1 else [0.0]
        ys = cands(free_t, free_b) if free_t + free_b > 1 else [0.0]
        for dx in xs:
            for dy in ys:
                if dx == 0.0 and dy == 0.0:
                    continue
                c = cost(dx, dy)
                if c < best[0] - 1e-9:
                    best = (c, dx, dy)
        if base_cost - best[0] < 0.015:
            return
        _, dx, dy = best
        for eid in flow_ids:
            self.out[eid].x += dx
            self.out[eid].y += dy

    def _top_level_nodes(self) -> list[Node]:
        """Leaves for the XY-cut. The contents of a panel are pre-grouped into
        their own subtree so a card travels as one unit."""
        an = self.an
        nodes: list[Node] = []
        for pb in an.panels:
            members = [Node(axis="leaf", block=b, box=b.box) for b in an.blocks if b.panel is pb]
            if len(members) >= 2:
                nodes.append(build_tree(members, an.W, an.H))
            else:
                nodes.extend(members)
        nodes.extend(Node(axis="leaf", block=b, box=b.box) for b in an.blocks if b.panel is None)
        return nodes

    def _ctx(self, k: float) -> Ctx:
        return Ctx(W=self.an.W, H=self.an.H, TW=self.TW, TH=self.TH, k=k, kw=self.kw, kh=self.kh,
                   khi=max(self.kw, self.kh), mode=self.mode, margin=self.margin, gmin=self.gmin, fmin=self.fmin,
                   floor_relax=self.fmin / self.fmin_top, hero_boost=self.hero_boost)

    # readability floors tried in order when the top floor cannot fit (px)
    FLOOR_LADDER = (8.5, 7.0, 5.5)

    def _layout_flow(self, root: Node, lanes: list[Node], footer: list[Node]) -> None:
        """Search the scale at the top readability floor; only if nothing fits
        there, step down the floor ladder and keep the best feasible result,
        charging each step down (score + 1.5 * log(floor / top))."""
        top = self.fmin_top
        floors = [top] + [f for f in self.FLOOR_LADDER if f < top]
        chosen = None
        chosen_floor = top
        best_relaxed = None
        last_infeasible = None
        for f in floors:
            self.fmin = f
            res, feasible = self._search_scale(root, lanes, footer)
            if res is None:
                continue
            if feasible and f == top:
                chosen, chosen_floor = res, f
                break
            if feasible:
                adj = res[0] + 1.5 * math.log(f / top)
                if best_relaxed is None or adj > best_relaxed[0]:
                    best_relaxed = (adj, res, f)
            else:
                last_infeasible = (res, f)
        if chosen is None:
            if best_relaxed is not None:
                _, chosen, chosen_floor = best_relaxed
            elif last_infeasible is not None:
                chosen, chosen_floor = last_infeasible
        self.fmin = chosen_floor
        if chosen is None:
            return
        _, k, data = chosen
        self.k_used = k
        self._commit(k, root, lanes, footer, data)

    def _search_scale(self, root: Node, lanes: list[Node], footer: list[Node]):
        """Best global scale at the current floor: returns ((score, k, data), feasible)."""
        self.hero_boost = 1.0
        k_hi = max(self.kw, self.kh)
        k_lo = k_hi / 40.0
        n = 56
        cands = [k_hi * (k_lo / k_hi) ** (i / (n - 1)) for i in range(n)]
        best = None
        fallback = None
        for k in cands:
            feasible, score, data = self._try(k, root, lanes, footer)
            if feasible:
                if best is None or score > best[0]:
                    best = (score, k, data)
            elif fallback is None or score > fallback[0]:
                fallback = (score, k, data)
        if best is not None:
            # local refinement between the best feasible k and the next larger candidate
            k_best = best[1]
            idx = cands.index(k_best)
            k_up = cands[idx - 1] if idx > 0 else k_best
            lo, hi = k_best, k_up
            for _ in range(6):
                mid = (lo + hi) / 2
                if mid <= k_best:
                    break
                feasible, score, data = self._try(mid, root, lanes, footer)
                if feasible and score > best[0]:
                    best = (score, mid, data)
                    lo = mid
                else:
                    hi = mid
        if best is not None:
            return self._grow_hero(best, root, lanes, footer), True
        return fallback, False

    HERO_BOOSTS = (1.15, 1.3, 1.5, 1.75, 2.0)
    HERO_MAX_COVER = 0.62

    def _grow_hero(self, best, root: Node, lanes: list[Node], footer: list[Node]):
        """At the chosen k, let the hero image grow while the layout stays
        feasible, the score (+ 0.3 * log boost) does not drop and the hero
        covers at most ~62% of the canvas. Stops at the first failure."""
        if not any(b.role == "hero" for b in self.an.blocks):
            return best
        k = best[1]
        kept = 1.0
        for boost in self.HERO_BOOSTS:
            self.hero_boost = boost
            feasible, score, data = self._try(k, root, lanes, footer)
            score += 0.3 * math.log(boost)
            placed: list = []
            for p in data["plans"] + data.get("foot", []):
                p.flatten(0.0, 0.0, placed)
            cover = max((lp.w * lp.h for b, lp, _, _ in placed if b.role == "hero"), default=0.0)
            if not feasible or score < best[0] or cover > self.HERO_MAX_COVER * self.TW * self.TH:
                break
            best = (score, k, data)
            kept = boost
        self.hero_boost = kept
        return best

    def _avail(self) -> tuple[float, float, float]:
        m = self.margin
        if self.TW - 2 * m <= 1 or self.TH - 2 * m <= 1:
            m = 0.0
        return m, self.TW - 2 * m, self.TH - 2 * m

    def _try(self, k: float, root: Node, lanes: list[Node], footer: list[Node]):
        ctx = self._ctx(k)
        L = Layouter(self.an, ctx)
        m, avail_w, avail_h = self._avail()
        if self.mode == "v":
            plans = []
            for ln in lanes:
                if ln.axis == "h":
                    plans.append(self._plan_root_row(L, ln, k, avail_w, avail_h))
                else:
                    plans.append(L.plan(ln, k, avail_w, avail_h))
            gaps = self._lane_gaps(root, lanes, k, ctx)
            min_gaps = [max(ctx.gmin, 0.5 * g) for g in gaps]
            total_h = sum(p.h for p in plans) + sum(min_gaps)
            over_w = max([p.w / avail_w for p in plans] + [0.0])
            feasible = total_h <= avail_h + 0.5 and over_w <= 1.001
            fill = (sum(p.h for p in plans) + sum(gaps)) / avail_h
            penalty = sum(p.penalty for p in plans)
            # breathing room: penalise filling the axis more than the source did
            fill_ok = max(0.9, min(1.0, root.h / self.an.H))
            score = math.log(k) - 0.35 * penalty - 0.15 * max(0.0, fill - fill_ok) / 0.1
            if not feasible:
                score -= 2.0 * (max(0.0, total_h / avail_h - 1) + max(0.0, over_w - 1))
            return feasible, score, {"plans": plans, "gaps": gaps, "min_gaps": min_gaps}
        # horizontal flow: lanes side by side, or grouped into 2-3 columns ------
        foot_plans = [L.plan(fn, k, avail_w, avail_h, packed=True) for fn in footer]
        foot_h = sum(fp.h for fp in foot_plans) + ctx.gmin * len(foot_plans)
        band_h = max(1.0, avail_h - foot_h)
        best = None
        for cols, grouped in self._column_partitions(lanes):
            res = self._try_columns(L, ctx, k, root, cols, grouped, avail_w, band_h, foot_plans)
            if best is None or res[1] > best[1]:
                best = res
        return best

    def _column_partitions(self, lanes: list[Node]) -> list[tuple[list[Node], bool]]:
        """Candidate groupings of consecutive lanes into columns: the plain
        row, a single stack, and every 2- or 3-column split (capped)."""
        n = len(lanes)
        if n == 1:
            return [(lanes, False)]

        def col(group: list[Node]) -> Node:
            if len(group) == 1:
                return group[0]
            return Node(axis="v", children=list(group), box=_node_box(group), synthetic=True)

        out: list[tuple[list[Node], bool]] = [(list(lanes), False)]
        if n <= 6:
            out.append(([col(lanes)], True))
        splits: list[list[Node]] = []
        for i in range(1, n):
            splits.append([col(lanes[:i]), col(lanes[i:])])
        if n >= 3:
            for i in range(1, n - 1):
                for j in range(i + 1, n):
                    splits.append([col(lanes[:i]), col(lanes[i:j]), col(lanes[j:])])
        if len(splits) > 24:
            step = len(splits) / 24
            splits = [splits[int(t * step)] for t in range(24)]
        return out + [(sp, True) for sp in splits]

    def _try_columns(self, L: Layouter, ctx: Ctx, k: float, root: Node, lanes: list[Node], grouped: bool,
                     avail_w: float, band_h: float, foot_plans: list[Plan]):
        gaps = self._lane_gaps(root, lanes, k, ctx)
        plans: list[Optional[Plan]] = [None] * len(lanes)
        flex = [i for i, ln in enumerate(lanes) if ln.has_text()]
        for i, ln in enumerate(lanes):
            if i not in flex:
                plans[i] = self._plan_band_lane(L, ln, k, avail_w, band_h * 0.92)
        rigid_w = sum(p.w for p in plans if p is not None) + sum(gaps)
        remaining = avail_w - rigid_w
        if flex:
            prefs = {i: L.pref_w(lanes[i], k, 0.6 * avail_w) for i in flex}
            widths = share_width(prefs, remaining)
            for i in flex:
                plans[i] = L.plan(lanes[i], k, max(1.0, widths[i]), band_h, packed=True)
        done: list[Plan] = [p for p in plans if p is not None]
        total_w = sum(p.w for p in done) + sum(gaps)
        max_h = max(p.h for p in done)
        min_h = min(p.h for p in done)
        feasible = total_w <= avail_w + 0.5 and max_h <= band_h + 0.5 and (not flex or remaining > 0)
        fill = total_w / avail_w
        penalty = sum(p.penalty for p in done) + sum(fp.penalty for fp in foot_plans)
        score = math.log(k) - 0.35 * penalty
        if grouped and len(done) > 1:
            score -= 0.35 * (max_h - min_h) / band_h  # unbalanced columns
        elif not grouped:
            fill_ok = max(0.92, min(1.0, root.w / self.an.W)) if root.axis == "h" else 0.92
            score -= 0.15 * max(0.0, fill - fill_ok) / 0.08  # a packed single row wants margins
            if root.axis == "h":
                score += 0.2  # same axis as the source: keep its composition when close
        if not feasible:
            score -= 2.0 * (max(0.0, total_w / avail_w - 1) + max(0.0, max_h / band_h - 1))
        return feasible, score, {"plans": done, "gaps": gaps, "foot": foot_plans, "band_h": band_h,
                                 "lanes": lanes, "grouped": grouped}

    def _plan_band_lane(self, L: Layouter, ln: Node, k: float, avail_w: float, band_h: float) -> Plan:
        """A rigid lane in a horizontal flow. A row of small items (badges,
        a logo next to a tag) is stacked instead when the stack fits the
        band, which saves width for the content that matters."""
        row = L.plan(ln, k, avail_w, band_h, packed=True)
        if ln.axis != "h" or len(ln.children) > 4:
            return row
        cA = self.an.W * self.an.H
        if any(box_area(c.box) > 0.06 * cA for c in ln.children):
            return row
        stack = Node(axis="v", children=list(ln.children), box=ln.box)
        sp = L.plan(stack, k, avail_w, band_h, packed=True)
        if sp.h <= band_h and sp.w < row.w:
            return sp
        return row

    def _lane_gaps(self, root: Node, lanes: list[Node], k: float, ctx: Ctx) -> list[float]:
        same_axis = root.axis == self.mode
        gaps = []
        for a, b in zip(lanes, lanes[1:]):
            if same_axis:
                g = (b.box[1] - a.box[3]) if self.mode == "v" else (b.box[0] - a.box[2])
                g = clamp(g * k, ctx.gmin, 0.12 * (self.TH if self.mode == "v" else self.TW))
            else:
                g = max(ctx.gmin, 0.03 * (self.TW if self.mode == "h" else self.TH))
            gaps.append(g)
        return gaps

    def _plan_root_row(self, L: Layouter, ln: Node, k: float, avail_w: float, avail_h: float) -> Plan:
        """A row lane at the root in vertical mode: its children keep their
        own horizontal anchoring across the canvas (left badge, centred logo)."""
        p = L.plan(ln, k, avail_w, avail_h)
        if p.wrapped or p.w > avail_w + 0.5 or len(p.children) != len(ln.children):
            return p
        sizes = [cp.w for _, _, cp in p.children]
        if all(c.is_leaf and c.block.kind == "button" for c in ln.children):
            # a row of chips: equal gaps across the row's source span, so the
            # chips read as an evenly spaced set whatever their label widths
            span = min(avail_w, max(sum(sizes), ln.w / self.an.W * avail_w))
            gap = max(min(L.node_gaps(ln, k)), (span - sum(sizes)) / max(1, len(sizes) - 1))
            total = sum(sizes) + gap * (len(sizes) - 1)
            x0 = clamp(ln.cx / self.an.W * avail_w - total / 2, 0.0, max(0.0, avail_w - total))
            starts = []
            for sz in sizes:
                starts.append(x0)
                x0 += sz + gap
        else:
            desired = [c.cx / self.an.W * avail_w for c in ln.children]
            starts = solve_1d(desired, sizes, L.node_gaps(ln, k), 0.0, avail_w)
        p.children = [(sx, dy, cp) for (_, dy, cp), sx in zip(p.children, starts)]
        p.w = avail_w
        return p

    # ---- commit ---------------------------------------------------------------
    def _commit(self, k: float, root: Node, lanes: list[Node], footer: list[Node], data: dict) -> None:
        m, avail_w, avail_h = self._avail()
        plans: list[Plan] = data["plans"]
        placed: list[tuple[Block, LeafPlan, float, float]] = []
        if self.mode == "v":
            same_axis = root.axis != "h"
            sizes = [p.h for p in plans]
            if same_axis:
                desired = [ln.cy / self.an.H * avail_h for ln in lanes]
            else:
                desired = [ln.cx / self.an.W * avail_h for ln in lanes]
            gaps = data["gaps"]
            if sum(sizes) + sum(gaps) > avail_h:
                gaps = data["min_gaps"]
            starts = solve_1d(desired, sizes, gaps, 0.0, avail_h)
            for ln, p, sy in zip(lanes, plans, starts):
                if ln.axis == "h" and p.w >= avail_w - 0.5:
                    sx = 0.0
                elif same_axis:
                    sx = _anchor_offset(ln.box[0] / self.an.W, (self.an.W - ln.box[2]) / self.an.W,
                                        ln.cx / self.an.W, avail_w, p.w)
                else:
                    sx = (avail_w - p.w) / 2
                p.flatten(m + sx, m + sy, placed)
        else:
            band_h = data["band_h"]
            gaps = data["gaps"]
            sizes = [p.w for p in plans]
            used_lanes = data.get("lanes", lanes)
            if root.axis == "h" and not data.get("grouped"):
                # same axis as the source: keep each lane's proportional position
                desired = [ln.cx / self.an.W * avail_w for ln in used_lanes]
                starts = solve_1d(desired, sizes, gaps, 0.0, avail_w)
                for ln, p, sx in zip(used_lanes, plans, starts):
                    sy = _anchor_offset(ln.box[1] / self.an.H, (self.an.H - ln.box[3]) / self.an.H,
                                        ln.cy / self.an.H, band_h, p.h)
                    p.flatten(m + sx, m + sy, placed)
            else:
                max_gap = max(3 * (gaps[0] if gaps else self.gmin), 0.08 * self.TW)
                left, gaps2 = justify(sizes, gaps, avail_w, max_gap)
                x = m + left
                for i, p in enumerate(plans):
                    p.flatten(x, m + (band_h - p.h) / 2, placed)
                    x += p.w + (gaps2[i] if i < len(gaps2) else 0.0)
            y = m + band_h
            for fp in data["foot"]:
                y += self.gmin
                fp.flatten(m + (avail_w - fp.w) / 2, y, placed)
                y += fp.h
        for block, lp, x, y in placed:
            self._realize(block, lp, x, y)

    def _realize(self, block: Block, lp: LeafPlan, X: float, Y: float) -> None:
        """Turn a leaf plan whose AABB top-left is (X, Y) into element boxes."""
        cx, cy = X + lp.w / 2, Y + lp.h / 2
        if block.kind == "text":
            el = block.els[0]
            self.out[el.id] = Placement(cx - lp.text_w / 2, cy - lp.text_h / 2 - lp.text_top,
                                        lp.text_w, lp.text_h + lp.text_top, lp.fs)
            return
        if block.kind == "button":
            sh, lb = block.shape, block.label
            assert sh is not None and lb is not None
            self.out[sh.id] = Placement(cx - lp.shape_w / 2, cy - lp.shape_h / 2, lp.shape_w, lp.shape_h)
            self.out[lb.id] = Placement(cx - lp.shape_w / 2, cy - lp.label_h / 2 - lp.text_top,
                                        lp.shape_w, lp.label_h + lp.text_top, lp.fs)
            return
        s = lp.scale  # rigid: scale every member about the block centre
        for e in block.els:
            w, h = e.w * s, e.h * s
            ecx, ecy = cx + (e.cx - block.cx) * s, cy + (e.cy - block.cy) * s
            fs = max(1, int(round(e.fs * s))) if e.type == "text" else None
            self.out[e.id] = Placement(ecx - w / 2, ecy - h / 2, w, h, fs)

    # ---- the rest -------------------------------------------------------------
    def _place_backgrounds(self) -> None:
        for el in self.an.backgrounds:
            self.out[el.id] = Placement(0.0, 0.0, self.TW, self.TH)

    def _place_decor(self) -> None:
        k = self.k_used
        for el in self.an.decor:
            w, h = el.w * k, el.h * k
            aw, ah = rot_aabb_dims(w, h, el.rot)
            if aw > self.TW or ah > self.TH:
                f = min(self.TW / max(aw, 1e-6), self.TH / max(ah, 1e-6))
                w, h = w * f, h * f
                aw, ah = rot_aabb_dims(w, h, el.rot)
            cx = clamp(el.cx / self.an.W * self.TW, aw / 2, self.TW - aw / 2)
            cy = clamp(el.cy / self.an.H * self.TH, ah / 2, self.TH - ah / 2)
            self.out[el.id] = Placement(cx - w / 2, cy - h / 2, w, h)

    def _place_panels(self) -> None:
        k = self.k_used
        for pb in self.an.panels:
            s = pb.els[0]
            kids = [b for b in self.an.blocks if b.panel is pb]
            boxes = []
            src_boxes = []
            for b in kids:
                for e in b.els:
                    p = self.out.get(e.id)
                    if p:
                        boxes.append(rect_aabb(p.x, p.y, p.w, p.h, e.rot))
                        src_boxes.append(e.tight)
            if not boxes:  # nothing inside survived: behave like a decoration
                cx, cy = s.cx / self.an.W * self.TW, s.cy / self.an.H * self.TH
                w, h = min(s.w * k, self.TW), min(s.h * k, self.TH)
                self.out[s.id] = Placement(clamp(cx - w / 2, 0, self.TW - w), clamp(cy - h / 2, 0, self.TH - h), w, h)
                continue
            u, su = boxes[0], src_boxes[0]
            for b, sb in zip(boxes[1:], src_boxes[1:]):
                u, su = box_union(u, b), box_union(su, sb)
            pad_l, pad_t = max(0.0, su[0] - pb.box[0]) * k, max(0.0, su[1] - pb.box[1]) * k
            pad_r, pad_b = max(0.0, pb.box[2] - su[2]) * k, max(0.0, pb.box[3] - su[3]) * k
            x0, y0 = max(0.0, u[0] - pad_l), max(0.0, u[1] - pad_t)
            x1, y1 = min(self.TW, u[2] + pad_r), min(self.TH, u[3] + pad_b)
            # a band that touched a canvas edge in the source keeps touching it
            W, H = self.an.W, self.an.H
            if pb.box[0] <= 0.02 * W:
                x0 = 0.0
            if pb.box[2] >= 0.98 * W:
                x1 = self.TW
            if pb.box[1] <= 0.02 * H:
                y0 = 0.0
            if pb.box[3] >= 0.98 * H:
                y1 = self.TH
            self.out[s.id] = Placement(x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0))

    def _enforce_bounds(self) -> None:
        """Keep every element inside the canvas (global squeeze if needed)."""
        bg_ids = {e.id for e in self.an.backgrounds}
        els = [e for e in self.an.els if e.id in self.out and e.id not in bg_ids]
        if not els:
            return
        u: Optional[Box] = None
        for e in els:
            p = self.out[e.id]
            b = rect_aabb(p.x, p.y, p.w, p.h, e.rot)
            u = b if u is None else box_union(u, b)
        assert u is not None
        uw, uh = u[2] - u[0], u[3] - u[1]
        s = min(1.0, (self.TW - 1) / max(uw, 1e-6), (self.TH - 1) / max(uh, 1e-6))
        if s < 0.999:
            ucx, ucy = (u[0] + u[2]) / 2, (u[1] + u[3]) / 2
            for e in els:
                p = self.out[e.id]
                cx, cy = ucx + (p.x + p.w / 2 - ucx) * s, ucy + (p.y + p.h / 2 - ucy) * s
                p.w, p.h = p.w * s, p.h * s
                p.x, p.y = cx - p.w / 2, cy - p.h / 2
                if p.fs is not None:
                    p.fs = max(1, int(p.fs * s))
        for e in els:
            p = self.out[e.id]
            b = rect_aabb(p.x, p.y, p.w, p.h, e.rot)
            dx = -b[0] if b[0] < 0 else (self.TW - b[2] if b[2] > self.TW else 0.0)
            dy = -b[1] if b[1] < 0 else (self.TH - b[3] if b[3] > self.TH else 0.0)
            p.x += dx
            p.y += dy

    def fallback_layout(self) -> None:
        """Uniform scale (fit) with canvas-proportional centres — the safe
        answer when structural analysis fails for any reason."""
        s = min(self.kw, self.kh)
        self._place_backgrounds()
        bg = {e.id for e in self.an.backgrounds}
        for e in self.an.els:
            if e.id in bg:
                continue
            w, h = e.w * s, e.h * s
            self.out[e.id] = Placement(e.cx * self.kw - w / 2, e.cy * self.kh - h / 2, w, h,
                                       max(1, int(e.fs * s)) if e.type == "text" else None)
        self._enforce_bounds()

    # ---- output ---------------------------------------------------------------
    def build_output(self) -> dict:
        elements = []
        for e in self.an.els:
            raw = dict(e.raw)
            p = self.out.get(e.id)
            if p is None:  # proportional fallback — only reached on internal errors
                s = min(self.kw, self.kh)
                p = Placement(e.cx * self.kw - e.w * s / 2, e.cy * self.kh - e.h * s / 2, e.w * s, e.h * s,
                              max(1, int(e.fs * s)) if e.type == "text" else None)
            raw["x"] = round(p.x, 2)
            raw["y"] = round(p.y, 2)
            raw["width"] = round(max(0.0, p.w), 2)
            raw["height"] = round(max(0.0, p.h), 2)
            if e.type == "text":
                raw["font_size"] = int(max(1, p.fs if p.fs is not None else e.fs))
            elements.append(raw)
        return {"canvas": {"width": int(round(self.TW)), "height": int(round(self.TH))}, "elements": elements}


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    """Resize `source` into `target_canvas` by re-laying out its structure."""
    # Keep the text caches bounded across many tasks in one process.
    if len(_FIT_CACHE) > 200_000:
        _FIT_CACHE.clear()
        _WORD_CACHE.clear()
        fonts()._str_widths.clear()
    engine = Engine(source, target_canvas)
    try:
        engine.layout()
    except Exception:  # never fail the run: fall back to anchored uniform scaling
        traceback.print_exc(file=sys.stderr)
        engine.out = {}
        try:
            engine.fallback_layout()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            engine.out = {}
    return engine.build_output()
