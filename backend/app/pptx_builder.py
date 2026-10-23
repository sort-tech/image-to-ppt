"""Render a :class:`schemas.SlideSpec` into a real, native PowerPoint deck.

Almost everything this module emits is a genuine, editable PowerPoint object: auto
shapes, block arrows, connectors, graphic-frame tables and text boxes. The ONE
exception is an ``image`` element: a region the analyzer could not express as any of
those (a photo, a rendered chart, a logo) is cropped out of the source image and
placed with ``add_picture``, because a wrong native approximation of a photograph is
worse than the photograph. That is a deliberate, bounded exception, and
:func:`flattening_report` is the fence around it: a picture may not cover the slide,
pictures may not tile it, and a picture-only slide is rejected outright. Pasting the
input image onto a slide remains the one thing this tool exists NOT to do.

Design notes
------------
* Coordinates arrive as integers in ``0..config.COORD_SCALE`` on both axes and are
  mapped linearly onto the 16:9 slide (13.333in x 7.5in) by :func:`scale_bbox`
  (boxes) and :func:`scale_point` (connector endpoints).
* ``spec.elements`` order *is* the z-order: earlier elements are drawn first and later
  elements paint on top.
* Robustness is a hard requirement: a single malformed element (or colour) must never
  abort the whole build. :func:`build_presentation` isolates every element, reports the
  failure on stderr and carries on with the rest.

Verified against python-pptx 1.0.2:

* ``GraphicFrame.shadow`` raises ``NotImplementedError``, hence the guarded shadow
  helper;
* ``LineFormat`` exposes only ``color``, ``dash_style``, ``fill`` and ``width`` -
  there is NO arrowhead API anywhere (not on ``line``, not on the connector), so
  arrowheads are written as the schema's own ``a:headEnd``/``a:tailEnd`` children;
* a connector reports ``MSO_SHAPE_TYPE.LINE``; there is no ``CONNECTOR`` member.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from typing import Any, Union

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from . import config
from .schemas import (
    BBox,
    ConnectorElement,
    ImageElement,
    Point,
    ShapeElement,
    SlideSpec,
    TableElement,
    TextElement,
)

__all__ = [
    "PptxBuildError",
    "PictureSourceError",
    "SHAPE_MAP",
    "ALIGN_MAP",
    "CONNECTOR_MAP",
    "DASH_MAP",
    "ARROW_HEAD_TYPE",
    "MIN_DIMENSION_IN",
    "SLIDE_SIZE_TYPE_16X9",
    "SLIDE_SIZE_TYPE_CUSTOM",
    "PLAIN_TABLE_STYLE_ID",
    "TABLE_FONT_DARK",
    "TABLE_FONT_LIGHT",
    "FULL_BLEED_PICTURE_PCT",
    "MAX_PICTURE_AREA_PCT",
    "SourceImage",
    "to_rgb",
    "scale_bbox",
    "scale_point",
    "add_shape",
    "add_table",
    "add_textbox",
    "add_connector",
    "add_picture",
    "flattening_report",
    "build_presentation",
    "build_pptx",
]

#: What :func:`add_picture` and :func:`build_presentation` accept as the original
#: image to crop from: the raw bytes (what the HTTP layer holds), a path (what the
#: CLI holds), or an already-decoded ``PIL.Image.Image`` (what a build reuses across
#: several image elements). ``None`` means "there is no source image", which is the
#: normal state of ``/api/preview``, ``/api/build-from-json`` and ``--from-json``.
SourceImage = Union[bytes, bytearray, memoryview, str, Path, Any]


class PptxBuildError(Exception):
    """Raised when a deck (or a single element of it) cannot be rendered."""


class PictureSourceError(PptxBuildError):
    """The crop behind an ``image`` element could not be produced.

    Raised by :func:`add_picture` when there is no source image at all, when the
    source cannot be decoded, or when the crop itself fails. It travels through the
    per-element render isolation in :func:`build_presentation` like any other
    element failure, so one unusable image never costs the rest of the slide.
    """


#: Schema shape kind -> python-pptx auto-shape preset.
SHAPE_MAP: dict[str, MSO_SHAPE] = {
    "rectangle": MSO_SHAPE.RECTANGLE,
    "rounded_rectangle": MSO_SHAPE.ROUNDED_RECTANGLE,
    "circle": MSO_SHAPE.OVAL,
    "diamond": MSO_SHAPE.DIAMOND,
    # Block arrows: real auto shapes with text frames and handles, not connectors.
    "arrow_right": MSO_SHAPE.RIGHT_ARROW,
    "arrow_left": MSO_SHAPE.LEFT_ARROW,
    "arrow_up": MSO_SHAPE.UP_ARROW,
    "arrow_down": MSO_SHAPE.DOWN_ARROW,
}

#: Schema connector kind -> python-pptx connector preset. ``ELBOW`` is the
#: orthogonal two-segment route an architecture diagram uses between boxes that do
#: not line up.
CONNECTOR_MAP: dict[str, MSO_CONNECTOR] = {
    "straight": MSO_CONNECTOR.STRAIGHT,
    "elbow": MSO_CONNECTOR.ELBOW,
}

#: Schema dash style -> DrawingML preset dash. Written EXPLICITLY in both
#: directions (never left to inherit) so the built deck - which is what the
#: preview reads back - states what the spec asked for.
DASH_MAP: dict[str, MSO_LINE_DASH_STYLE] = {
    "solid": MSO_LINE_DASH_STYLE.SOLID,
    "dashed": MSO_LINE_DASH_STYLE.DASH,
}

#: Schema alignment -> python-pptx paragraph alignment.
ALIGN_MAP: dict[str, PP_ALIGN] = {
    "left": PP_ALIGN.LEFT,
    "center": PP_ALIGN.CENTER,
    "right": PP_ALIGN.RIGHT,
}

#: Degenerate boxes (w or h of 0/1 units) would be invisible; floor them at this size.
MIN_DIMENSION_IN: float = 0.05

#: ``ST_SlideSizeType`` values for ``<p:sldSz type>``. python-pptx's default
#: template ships ``type="screen4x3"`` and never updates it when cx/cy change, so
#: the saved package used to advertise 4:3 while measuring 16:9. PowerPoint
#: renders from cx/cy, but linters and other tooling do read ``type``.
SLIDE_SIZE_TYPE_16X9: str = "screen16x9"
SLIDE_SIZE_TYPE_CUSTOM: str = "custom"

#: How far the slide's aspect ratio may stray from 16:9 and still be called that.
_ASPECT_TOLERANCE: float = 0.01

#: Geometry comparisons are in inches; ignore float noise below ~1 EMU.
_GEOM_EPS_IN: float = 1e-6

#: Internal cell padding for tables - deliberately tight so tables stay compact.
_CELL_MARGIN_H = Inches(0.04)
_CELL_MARGIN_V = Inches(0.01)

#: Built-in PowerPoint table style "No Style, Table Grid": plain rules, no theme
#: fills, no banding. python-pptx defaults every new table to "Medium Style 2 -
#: Accent 1" instead, which would decide the table's fills and font colours from
#: the renderer's theme rather than from the analyzed image.
PLAIN_TABLE_STYLE_ID: str = "{5940675A-B579-460E-94D1-54222C63F5DA}"

#: Explicit table glyph colours. Without them the (built-in) table style picks
#: the font colour, e.g. white first-row text on a light header fill.
TABLE_FONT_DARK: RGBColor = RGBColor(0x00, 0x00, 0x00)
TABLE_FONT_LIGHT: RGBColor = RGBColor(0xFF, 0xFF, 0xFF)

#: Perceived-luminance cut-off (0..255) for choosing light vs dark header text.
_LUMINANCE_CUTOFF: float = 140.0

#: Control characters python-pptx turns into literal "_xNNNN_" text in <a:t>.
#: TAB (\x09) and LF (\x0A) are exempt there, so they are kept here as well.
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")

#: Lone UTF-16 surrogates (U+D800..U+DFFF). ``json.loads('"\\ud800"')`` hands
#: back exactly such a character, and it cannot be encoded as UTF-8, so lxml
#: raises ``UnicodeEncodeError`` the moment it is assigned to a run or a cell -
#: mid-element, i.e. AFTER the shape was added to the slide.
_SURROGATES_RE = re.compile("[\ud800-\udfff]")

#: What an unencodable character becomes: U+FFFD REPLACEMENT CHARACTER, the
#: standard "there was a character here that could not be represented" glyph.
UNENCODABLE_REPLACEMENT: str = "�"

#: ``ST_LineEndType`` / ``ST_LineEndWidth`` / ``ST_LineEndLength`` values for the
#: ``a:headEnd``/``a:tailEnd`` arrowheads. python-pptx has no arrowhead API at all
#: (verified on 1.0.2: ``LineFormat`` offers only colour, dash style, fill and
#: width), so :func:`_set_arrow_heads` writes these attributes itself.
ARROW_HEAD_TYPE: str = "triangle"
ARROW_HEAD_SIZE: str = "med"

#: Anti-flattening rule 1: a picture this large on BOTH axes is a screenshot of the
#: slide, not an illustration on it.
FULL_BLEED_PICTURE_PCT: float = 95.0

#: Anti-flattening rule 2: the share of the slide all pictures together may cover.
MAX_PICTURE_AREA_PCT: float = 80.0

#: Percentage comparisons in :func:`flattening_report` ignore float noise below this.
_PCT_EPS: float = 1e-9

#: The "there should have been an image here" outline: a thin dashed grey rectangle,
#: deliberately quiet enough not to be mistaken for slide content.
PLACEHOLDER_LINE_COLOR: RGBColor = RGBColor(0xB0, 0xB0, 0xB0)
PLACEHOLDER_LINE_WIDTH_PT: float = 0.75


# --------------------------------------------------------------------------- #
# Colour / geometry helpers
# --------------------------------------------------------------------------- #


def to_rgb(hex_color: str | None) -> RGBColor | None:
    """Convert a hex colour string to :class:`RGBColor`, or ``None`` if unusable.

    Accepts ``#RRGGBB``, ``RRGGBB``, the 3-digit shorthand and 8-digit ``#RRGGBBAA``
    (alpha is dropped). Never raises: anything it cannot parse degrades to ``None``,
    which callers render as "no fill" / "no line" rather than failing the build.
    """
    if isinstance(hex_color, RGBColor):
        return hex_color
    if not isinstance(hex_color, str):
        # None, numbers, bools, objects: not a colour we can trust - degrade quietly.
        return None

    text = hex_color.strip().lstrip("#")

    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    elif len(text) == 8:  # #RRGGBBAA -> drop alpha
        text = text[:6]

    if len(text) != 6:
        return None

    try:
        return RGBColor.from_string(text.upper())
    except (ValueError, TypeError):
        return None


def _emu_in(inches: float, minimum: float = 0.0) -> Emu:
    """Inches -> :class:`Emu`, floored at ``minimum`` inches."""
    return Emu(int(Inches(max(float(inches), float(minimum)))))


def _clean_text(value) -> str:
    """Make *value* safe for ``<a:t>``: real line breaks, no control junk.

    python-pptx escapes every control character in ``\\x00-\\x08`` and
    ``\\x0B-\\x1F`` into a literal ``_xNNNN_`` token before writing the run text
    (``CT_RegularTextRun._escape_ctrl_chars``), and DrawingML has no such escape,
    so PowerPoint shows those seven characters as visible garbage. A model
    transcribing a Windows-authored document happily returns ``"a\\r\\nb"``, so:

    * ``\\r\\n``, ``\\r``, ``\\x0B`` (PowerPoint's own soft break) and ``\\x0C``
      all become ``"\\n"``, i.e. a real paragraph break;
    * every remaining escapable control character is dropped;
    * ``\\t`` and ``\\n`` survive untouched (python-pptx does not escape them).

    Unencodable lone surrogates are replaced as well - see
    :func:`_clean_text_counted`, which additionally reports how many.
    """
    text, _replaced = _clean_text_counted(value)
    return text


def _encodable(text: str) -> bool:
    """True when *text* can actually be written into the .pptx package."""
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _clean_text_counted(value) -> tuple[str, int]:
    """:func:`_clean_text`, plus how many unencodable characters were replaced.

    A lone UTF-16 surrogate has no UTF-8 encoding, so assigning it to a run or a
    table cell raises ``UnicodeEncodeError`` *after* python-pptx has already put
    the shape on the slide. The element was then reported as "skipped" while a
    hollow, empty shape stayed in the deck - a warning that was simply untrue.
    Replacing the character keeps the rest of the text, renders as close to the
    source as the format allows, and the count lets the caller say so.
    """
    if value is None:
        return "", 0
    text = value if isinstance(value, str) else str(value)
    text = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x0b", "\n")
        .replace("\x0c", "\n")
    )
    text = _CTRL_CHARS_RE.sub("", text)

    text, replaced = _SURROGATES_RE.subn(UNENCODABLE_REPLACEMENT, text)
    if not _encodable(text):  # pragma: no cover - surrogates are the only case
        text = text.encode("utf-8", "replace").decode("utf-8", "replace")
        replaced = max(replaced, 1)
    return text, replaced


def _warn_replaced(where: str, replaced: int) -> None:
    """Report replaced-but-rendered characters (never call this "skipped")."""
    if replaced <= 0:
        return
    if replaced == 1:
        what, tail = "1 character", "only that character differs"
    else:
        what, tail = f"{replaced} characters", "only those characters differ"
    _warn(
        f"{where}: replaced {what} that cannot be stored in a .pptx (lone UTF-16 "
        f"surrogate) with {UNENCODABLE_REPLACEMENT!r}. The element WAS rendered, with "
        f"the rest of its text intact - {tail} from the spec."
    )


def _warn(message: str) -> None:
    """Emit a non-fatal diagnostic on stderr."""
    print(f"[pptx_builder] warning: {message}", file=sys.stderr)


def _sentence(detail: str) -> str:
    """*detail* with exactly one closing full stop, ready to be joined onto more.

    Exception messages in this module end in a period about half the time, and a
    blind ``f"{detail}."`` produced "...from a saved spec.." in the warning the UI
    prints verbatim - in the very panel the user is being asked to read.
    """
    text = str(detail or "").strip()
    if not text:
        return ""
    return text if text.endswith((".", "!", "?", ":", ";")) else f"{text}."


def _fit_span(
    start_in: float, size_in: float, extent_in: float
) -> tuple[float, float, bool, bool]:
    """Intersect one axis of a box with the slide.

    ``BBox`` clamps x/y/w/h independently, so ``x + w`` may reach 2000: a model
    that answers with corner coordinates ``[x0, y0, x1, y1]`` systematically
    overshoots the right/bottom edge and the element would render completely off
    the slide. Keep the origin where the model put it, trim the overflow, and
    report whether anything had to be adjusted.

    Returns ``(start, size, clipped, floored)``: the fitted origin and size in
    inches, whether the box had to be moved or trimmed to stay on the slide, and
    whether it was thinner than :data:`MIN_DIMENSION_IN` and had to be enlarged.
    Both adjustments change what the user sees versus what the spec asked for, so
    both are reported and neither may be silent.
    """
    minimum = min(MIN_DIMENSION_IN, extent_in)
    start = float(start_in)
    size = float(size_in)

    # `floored` MUST be decided against the size the caller actually asked for,
    # before the floor is applied - comparing the floored size with itself is
    # equal by construction, which is why this case never used to report itself.
    floored = size < minimum - _GEOM_EPS_IN
    size = max(size, minimum)

    fitted_start = min(max(start, 0.0), max(extent_in - minimum, 0.0))
    fitted_end = min(start + size, extent_in)
    fitted_size = max(fitted_end - fitted_start, minimum)

    clipped = (
        abs(fitted_start - start) > _GEOM_EPS_IN or abs(fitted_size - size) > _GEOM_EPS_IN
    )
    return fitted_start, fitted_size, clipped, floored


def scale_bbox(bbox: BBox) -> tuple[Emu, Emu, Emu, Emu]:
    """Map a normalized bbox onto slide EMU coordinates.

    Returns ``(left, top, width, height)``. Width and height are floored at
    :data:`MIN_DIMENSION_IN` so a degenerate box still renders something visible,
    and the box is intersected with the slide rectangle so an out-of-frame bbox
    stays visible instead of being drawn off the slide. BOTH adjustments are
    reported on stderr: either one means the rendered geometry is not the
    geometry the spec asked for.
    """
    scale = float(config.COORD_SCALE) or 1000.0
    slide_w = float(config.SLIDE_WIDTH_IN)
    slide_h = float(config.SLIDE_HEIGHT_IN)

    left_in, width_in, clipped_x, floored_x = _fit_span(
        bbox.x / scale * slide_w, bbox.w / scale * slide_w, slide_w
    )
    top_in, height_in, clipped_y, floored_y = _fit_span(
        bbox.y / scale * slide_h, bbox.h / scale * slide_h, slide_h
    )

    if clipped_x or clipped_y:
        _warn(
            f"bbox {bbox.as_tuple()} reaches past the {slide_w:g}x{slide_h:g}in slide "
            "(x+w or y+h > 1000); clipped it back onto the slide. If the model answered "
            "with corner coordinates [x0,y0,x1,y1] instead of [x,y,w,h], re-run or steer "
            "it with --extra."
        )

    if floored_x or floored_y:
        names = [
            name for name, floored in (("width", floored_x), ("height", floored_y)) if floored
        ]
        axes = " and ".join(names)
        verb = "is" if len(names) == 1 else "are"
        _warn(
            f"bbox {bbox.as_tuple()}: {axes} {verb} below the {MIN_DIMENSION_IN:g}in "
            f"minimum; enlarged it so the element stays visible. The rendered {axes} "
            f"{verb} NOT what the spec asked for - check that bbox."
        )

    return (
        _emu_in(left_in),
        _emu_in(top_in),
        _emu_in(width_in, MIN_DIMENSION_IN),
        _emu_in(height_in, MIN_DIMENSION_IN),
    )


def scale_point(p: Point) -> tuple[Emu, Emu]:
    """Map one normalized connector endpoint onto slide EMU coordinates.

    Returns ``(x, y)``, scaled exactly like :func:`scale_bbox` does - x by the
    slide WIDTH, y by the slide HEIGHT - so a connector lands on the same grid as
    the boxes it joins.

    There is no minimum size and no clipping to apply to a single point: a point has
    no extent, and :class:`schemas.Point` already clamps both components to
    ``0..COORD_SCALE``. The clamp here is belt and braces for a value written
    straight onto the model, and it reports itself if it ever has to move
    something, because a moved endpoint is not the geometry the spec asked for.

    A component that cannot be read as a number raises, exactly as an unusable
    ``bbox`` does in :func:`scale_bbox`; the per-element isolation in
    :func:`build_presentation` then skips that one connector with a reason.
    """
    scale = float(config.COORD_SCALE) or 1000.0
    x_in = float(p.x) / scale * float(config.SLIDE_WIDTH_IN)
    y_in = float(p.y) / scale * float(config.SLIDE_HEIGHT_IN)

    fitted_x = min(max(x_in, 0.0), float(config.SLIDE_WIDTH_IN))
    fitted_y = min(max(y_in, 0.0), float(config.SLIDE_HEIGHT_IN))
    if abs(fitted_x - x_in) > _GEOM_EPS_IN or abs(fitted_y - y_in) > _GEOM_EPS_IN:
        _warn(
            f"connector point ({p.x}, {p.y}) lies outside the "
            f"{config.SLIDE_WIDTH_IN:g}x{config.SLIDE_HEIGHT_IN:g}in slide; moved it "
            "back onto the edge. The rendered endpoint is NOT where the spec put it."
        )

    return _emu_in(fitted_x), _emu_in(fitted_y)


def _slide_size_element(prs):
    """The ``<p:sldSz>`` lxml element of *prs*, or ``None`` if it cannot be reached.

    python-pptx exposes no property for the element's ``type`` attribute, so the
    element itself has to be found. Verified on python-pptx 1.0.2:
    ``Presentation._element`` is the ``<p:presentation>`` CT element (the same
    object as ``Presentation.part._element``) and carries a ``sldSz`` property
    holding the ``CT_SlideSize`` child. Every step is optional and guarded, and
    the ``p:sldSz`` lookup by tag is a fallback for a python-pptx that renames or
    drops that property, so this degrades to a no-op instead of breaking a build.
    """
    root = getattr(prs, "_element", None)
    if root is None:
        root = getattr(getattr(prs, "part", None), "_element", None)
    if root is None:
        return None
    element = getattr(root, "sldSz", None)
    if element is None:
        try:
            element = root.find(qn("p:sldSz"))
        except (AttributeError, TypeError, SyntaxError):  # pragma: no cover - defensive
            return None
    return element


def _tag_slide_size(prs) -> None:
    """Make ``<p:sldSz type>`` agree with the cx/cy actually written.

    Cosmetic metadata only - PowerPoint lays the slide out from cx/cy - so a
    python-pptx that no longer exposes the element degrades to a no-op rather
    than failing the build. The value is derived from the real ratio rather than
    hard-coded, so a reconfigured (non-16:9) slide says ``custom`` instead of
    swapping one wrong label for another.
    """
    try:
        element = _slide_size_element(prs)
        if element is None:  # pragma: no cover - default template always has one
            return
        width, height = int(prs.slide_width or 0), int(prs.slide_height or 0)
        ratio = (width / height) if height else 0.0
        is_16x9 = abs(ratio - 16.0 / 9.0) <= _ASPECT_TOLERANCE
        element.set("type", SLIDE_SIZE_TYPE_16X9 if is_16x9 else SLIDE_SIZE_TYPE_CUSTOM)
    except Exception:  # pragma: no cover - metadata must never break a build
        pass


def _disable_shadow(shape) -> None:
    """Turn off the theme's inherited drop shadow where the API supports it.

    python-pptx 1.0.2 raises ``NotImplementedError`` for ``GraphicFrame.shadow``, and
    older versions lack the property entirely, so this is best-effort by design.
    """
    try:
        shape.shadow.inherit = False
    except (NotImplementedError, AttributeError, ValueError):
        pass


def _set_alt_text(shape, text: str | None) -> None:
    """Record *text* as the shape's alt text (``<p:cNvPr descr>``).

    This is what carries an ``image`` element's ``description`` INTO the deck, and
    it is load-bearing rather than cosmetic: ``services.preview`` derives its
    payload from the built deck and reads the description back off the picture's
    alt text, so a description that is never written here is one the preview can
    only ever report as ``null``. It is also the accessibility field PowerPoint
    itself shows, which is where a "team photo" label belongs anyway.

    The value is CLEANED first, exactly as run text is. lxml refuses a control
    character or a lone UTF-16 surrogate inside an attribute value, and the write
    is all-or-nothing, so an unsanitized description used to be dropped whole and
    in silence - the picture was in the deck, its label was gone, and the preview
    could only ever report ``null``. That is the ``warnings`` case in this
    project's own terms ("in the deck but adjusted ... cleaned text"), so the
    cleaning is reported.

    Best-effort in the sense that matters: a description is never worth losing the
    picture over. It is NOT best-effort in the sense of staying quiet - a label
    that could not be stored is said out loud.
    """
    raw = (text or "").strip()
    if not raw:
        return
    label, replaced = _clean_text_counted(raw)
    label = label.strip()
    if not label:
        _warn(
            "an image element's description contained nothing a .pptx can store, so "
            "the picture WAS rendered without a description."
        )
        return
    if replaced:
        _warn_replaced("an image element's description", replaced)
    elif label != raw:
        # Cleaning covers both removals (control characters) and normalization
        # (CRLF -> LF), so the message names the outcome rather than guessing
        # which of the two happened.
        _warn(
            "an image element's description had to be cleaned before it could be "
            "stored in a .pptx (control characters removed, line breaks "
            f"normalized). The picture WAS rendered, with the description {label!r}."
        )
    try:
        shape._element._nvXxPr.cNvPr.set("descr", label)
    except Exception as exc:  # noqa: BLE001 - alt text may never break a build
        _warn(
            f"an image element's description could not be recorded as the picture's "
            f"alt text ({type(exc).__name__}: {exc}); the picture WAS rendered, but "
            "the preview can only report it without a description."
        )


def _set_table_style(table, style_id: str) -> None:
    """Replace the inherited ``a:tableStyleId`` (best effort, never fatal).

    python-pptx hard-codes "Medium Style 2 - Accent 1"; that style - not the
    analyzed image - would then decide every fill and font colour in the table.
    """
    try:
        tbl_pr = table._tbl.find(qn("a:tblPr"))
        if tbl_pr is None:
            return
        style = tbl_pr.find(qn("a:tableStyleId"))
        if style is None:
            style = tbl_pr.makeelement(qn("a:tableStyleId"), {})
            tbl_pr.append(style)
        style.text = style_id
    except Exception:  # pragma: no cover - defensive, styling is cosmetic
        pass


def _disable_banding(table) -> None:
    """Stop the table asking for banded rows/columns the image never had."""
    for flag in ("horz_banding", "vert_banding"):
        try:
            setattr(table, flag, False)
        except (AttributeError, ValueError, NotImplementedError):  # pragma: no cover
            pass


def _luminance(rgb: RGBColor) -> float:
    """Perceived luminance (0..255) of an RGB colour."""
    red, green, blue = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    return 0.299 * red + 0.587 * green + 0.114 * blue


def _readable_on(background: RGBColor | None) -> RGBColor:
    """Black or white text, whichever stays readable on *background*."""
    if background is None:
        return TABLE_FONT_DARK
    try:
        return TABLE_FONT_LIGHT if _luminance(background) < _LUMINANCE_CUTOFF else TABLE_FONT_DARK
    except (TypeError, ValueError, IndexError):  # pragma: no cover - defensive
        return TABLE_FONT_DARK


def _style_font(font, *, size, bold: bool, color: RGBColor | None) -> None:
    """Write size / bold / colour explicitly (never "inherit")."""
    font.size = size
    font.bold = bold
    if color is not None:
        font.color.rgb = color


def _positive(value, fallback: float) -> float:
    """Coerce ``value`` to a positive float, falling back when absent or nonsensical."""
    try:
        if value is None:
            return float(fallback)
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return number if number > 0 else float(fallback)


# --------------------------------------------------------------------------- #
# Element renderers
# --------------------------------------------------------------------------- #


def add_shape(slide, el: ShapeElement):
    """Draw a native auto shape (rectangle / rounded rectangle / oval)."""
    preset = SHAPE_MAP.get(getattr(el, "kind", "rectangle"), MSO_SHAPE.RECTANGLE)
    left, top, width, height = scale_bbox(el.bbox)

    shape = slide.shapes.add_shape(preset, left, top, width, height)
    _disable_shadow(shape)

    # A fresh auto shape owns an empty text frame that PowerPoint may still
    # centre-align later content into; make sure the shape carries no text at all.
    if getattr(shape, "has_text_frame", False):
        text_frame = shape.text_frame
        text_frame.clear()
        try:
            text_frame.word_wrap = True
            text_frame.auto_size = MSO_AUTO_SIZE.NONE
        except (AttributeError, ValueError):  # pragma: no cover - defensive
            pass

    # Fill: no colour means "transparent" so underlying content stays visible.
    fill_rgb = to_rgb(el.fill_color)
    if fill_rgb is None:
        shape.fill.background()
    else:
        shape.fill.solid()
        shape.fill.fore_color.rgb = fill_rgb

    # Outline: no colour means no visible line at all.
    border_rgb = to_rgb(el.border_color)
    if border_rgb is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = border_rgb

    if el.border_width is not None:
        shape.line.width = Pt(_positive(el.border_width, config.DEFAULT_BORDER_WIDTH_PT))
    elif border_rgb is not None:
        shape.line.width = Pt(_positive(config.DEFAULT_BORDER_WIDTH_PT, 1.0))

    return shape


def add_table(slide, el: TableElement):
    """Draw a native PowerPoint table (a graphic frame, never an image)."""
    data = [list(row) for row in (el.data or []) if row is not None]
    if not data:
        data = [[""]]

    rows = len(data)
    cols = max((len(row) for row in data), default=1) or 1

    left, top, width, height = scale_bbox(el.bbox)
    graphic_frame = slide.shapes.add_table(rows, cols, left, top, width, height)
    table = graphic_frame.table
    _disable_shadow(graphic_frame)

    header_rgb = to_rgb(el.header_bg_color)
    header_bold = bool(el.header_font_bold)
    # `first_row` marks row 0 as the header row.
    table.first_row = bool(header_rgb is not None or header_bold)
    has_header = table.first_row

    # Everything visual must come from the spec, not from the renderer's default
    # table style: no accent-tinted row banding, no style-supplied font colour.
    _set_table_style(table, PLAIN_TABLE_STYLE_ID)
    _disable_banding(table)

    header_font_rgb = _readable_on(header_rgb)
    font_pt = Pt(_positive(el.font_size, config.DEFAULT_TABLE_FONT_SIZE))

    # Distribute the requested geometry evenly; keeping rows compact stops the
    # table from growing past the slide (python-pptx tables never shrink below
    # their content, but they will honour a small requested height).
    row_height = Emu(max(int(height / rows), 1))
    col_width = Emu(max(int(width / cols), 1))
    for row in table.rows:
        row.height = row_height
    for column in table.columns:
        column.width = col_width

    replaced_total = 0
    for r_idx in range(rows):
        source_row = data[r_idx]
        is_header = has_header and r_idx == 0
        # Write the bold state either way: `False` must land in the file as
        # b="0", otherwise "do not bold the header" silently means "inherit".
        bold = bool(header_bold) if is_header else False
        font_rgb = header_font_rgb if is_header else TABLE_FONT_DARK
        for c_idx in range(cols):
            cell = table.cell(r_idx, c_idx)

            raw = source_row[c_idx] if c_idx < len(source_row) else ""
            cell_text, replaced = _clean_text_counted(raw)
            replaced_total += replaced
            cell.text = cell_text

            cell.margin_left = _CELL_MARGIN_H
            cell.margin_right = _CELL_MARGIN_H
            cell.margin_top = _CELL_MARGIN_V
            cell.margin_bottom = _CELL_MARGIN_V
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE

            if is_header and header_rgb is not None:
                cell.fill.solid()
                cell.fill.fore_color.rgb = header_rgb

            # A cell only owns a run *after* text was assigned, and an empty cell
            # owns none at all - so style runs when present and always set the
            # paragraph font as the fallback that empty cells inherit.
            for paragraph in cell.text_frame.paragraphs:
                _style_font(paragraph.font, size=font_pt, bold=bold, color=font_rgb)
                for run in paragraph.runs:
                    _style_font(run.font, size=font_pt, bold=bold, color=font_rgb)

    _warn_replaced("table element", replaced_total)
    return graphic_frame


def add_textbox(slide, el: TextElement):
    """Draw a native text box, one paragraph per newline-separated line."""
    left, top, width, height = scale_bbox(el.bbox)
    textbox = slide.shapes.add_textbox(left, top, width, height)
    _disable_shadow(textbox)

    text_frame = textbox.text_frame
    text_frame.word_wrap = True
    try:
        text_frame.auto_size = MSO_AUTO_SIZE.NONE
    except (AttributeError, ValueError):  # pragma: no cover - defensive
        pass
    text_frame.vertical_anchor = MSO_ANCHOR.TOP
    text_frame.margin_left = Emu(0)
    text_frame.margin_right = Emu(0)
    text_frame.margin_top = Emu(0)
    text_frame.margin_bottom = Emu(0)

    # _clean_text turns CRLF / CR / VT / FF into real paragraph breaks and drops
    # the control characters python-pptx would otherwise escape into a literal
    # "_x000D_" in the slide text. It also replaces lone surrogates, which lxml
    # cannot encode at all - reported, because the rendered text then differs
    # from the spec.
    content, replaced = _clean_text_counted(el.content)
    _warn_replaced("text element", replaced)
    lines = content.split("\n") or [""]

    font_pt = Pt(_positive(el.font_size, config.DEFAULT_TEXT_FONT_SIZE))
    font_rgb = to_rgb(el.font_color)
    alignment = ALIGN_MAP.get(getattr(el, "align", "left"), PP_ALIGN.LEFT)
    bold = bool(el.bold)

    for index, line in enumerate(lines):
        paragraph = text_frame.paragraphs[0] if index == 0 else text_frame.add_paragraph()
        paragraph.alignment = alignment
        paragraph.font.size = font_pt
        paragraph.font.bold = bold
        if font_rgb is not None:
            paragraph.font.color.rgb = font_rgb

        run = paragraph.add_run()
        run.text = line
        run.font.size = font_pt
        run.font.bold = bold
        if font_rgb is not None:
            run.font.color.rgb = font_rgb

    return textbox


def _set_arrow_heads(shape, *, start: bool, end: bool) -> None:
    """Write the DrawingML arrowheads onto the shape's ``a:ln`` - as raw XML.

    This is raw lxml on purpose, not for lack of looking: python-pptx 1.0.2 has no
    arrowhead API anywhere. ``LineFormat`` exposes ``color``, ``dash_style``,
    ``fill`` and ``width`` and nothing else, and ``begin_arrow_head`` /
    ``end_arrow_head`` exist neither on the connector nor on its ``.line``. The
    only way an arrowhead reaches the package is the schema's own children of
    ``a:ln``, which is what this writes; it survives a save/reload unchanged.

    ``a:headEnd`` is the arrowhead at the line's BEGIN point, ``a:tailEnd`` the one
    at its END point - so ``arrow_start`` -> ``headEnd`` and ``arrow_end`` ->
    ``tailEnd``. ``CT_LineProperties`` orders its children
    ``[fill, prstDash, ..., headEnd, tailEnd]``: the caller therefore sets colour,
    width and dash style FIRST and these are appended LAST, in that order, so the
    saved XML is schema-valid. Both children are removed before being re-added, so
    the file says exactly what the element asked for rather than accumulating.
    """
    ln = shape.line._get_or_add_ln()
    for tag, wanted in ((qn("a:headEnd"), start), (qn("a:tailEnd"), end)):
        for existing in ln.findall(tag):
            ln.remove(existing)
        if not wanted:
            continue
        element = ln.makeelement(tag, {})
        element.set("type", ARROW_HEAD_TYPE)
        element.set("w", ARROW_HEAD_SIZE)
        element.set("len", ARROW_HEAD_SIZE)
        ln.append(element)


def add_connector(slide, el: ConnectorElement):
    """Draw a native diagram connector between two points.

    A connector has no bbox: it is defined by its two endpoints, and PowerPoint
    keeps it editable as a line (and re-routes an elbow itself when its ends move).

    Unlike a shape's outline, a connector with no colour is NOT invisible: no
    explicit colour means the theme's line colour, because a line nobody can see is
    never what "connector" meant. Width falls back to
    ``config.DEFAULT_BORDER_WIDTH_PT`` the same way a shape border does.
    """
    kind = getattr(el, "kind", "straight")
    preset = CONNECTOR_MAP.get(kind, MSO_CONNECTOR.STRAIGHT)

    begin_x, begin_y = scale_point(el.start_point)
    end_x, end_y = scale_point(el.end_point)

    connector = slide.shapes.add_connector(preset, begin_x, begin_y, end_x, end_y)
    _disable_shadow(connector)

    line = connector.line
    # Colour / width / dash FIRST, arrowheads LAST: see _set_arrow_heads.
    line_rgb = to_rgb(getattr(el, "color", None))
    if line_rgb is not None:
        line.color.rgb = line_rgb
    line.width = Pt(_positive(getattr(el, "width_pt", None), config.DEFAULT_BORDER_WIDTH_PT))
    line.dash_style = DASH_MAP.get(getattr(el, "dash_style", "solid"), MSO_LINE_DASH_STYLE.SOLID)

    _set_arrow_heads(
        connector,
        start=bool(getattr(el, "arrow_start", False)),
        end=bool(getattr(el, "arrow_end", True)),
    )
    return connector


# --------------------------------------------------------------------------- #
# Sub-images: the one region kind that is cropped out of the source image
# --------------------------------------------------------------------------- #


def _apply_exif_orientation(image):
    """Rotate *image* into the frame it is DISPLAYED in, per its EXIF Orientation.

    This is a correctness fix, not a nicety. A bbox is measured in the frame the
    analyzer and the user looked at: the frontend shows the upload in an ``<img>``
    and every browser applies EXIF Orientation, and the vision provider decodes the
    same bytes with orientation applied too. Pillow does NOT - ``Image.open`` hands
    back the STORED grid. For the ordinary portrait-phone photo (Orientation=6) the
    two grids are transposed, so "the top-left region" named a completely different
    part of the picture and the deck embedded the wrong crop, silently.

    ``exif_transpose`` is a no-op for an image with no orientation tag (and it
    clears the tag, so applying it twice is harmless). A failure here degrades to
    the untransposed image rather than costing the element - the house rule is to
    degrade where degrading is possible.
    """
    try:
        from PIL import ImageOps

        return ImageOps.exif_transpose(image) or image
    except Exception:  # noqa: BLE001 - an unreadable EXIF block may not cost the crop
        return image


def _decode_source_image(source: SourceImage):
    """Decode *source* into a PIL image, or raise :class:`PictureSourceError`.

    Accepts raw bytes (the HTTP layer), a path (the CLI) or an already-decoded PIL
    image (a build that places several crops). Pillow is imported lazily and every
    failure - a missing Pillow, a missing file, a truncated or undecodable image -
    becomes one ``PictureSourceError``, so an unusable source costs the image
    element and nothing else.
    """
    if source is None:
        raise PictureSourceError(
            "there is no source image to crop from, so nothing could be embedded. An "
            "image element can only be rendered by the conversion that still has the "
            "original image - re-run the conversion instead of rebuilding the deck "
            "from a saved spec."
        )

    # An already-decoded PIL image: used as-is (decoded once per build, not per
    # element). Duck-typed so nothing here needs Pillow imported at module scope.
    # It is taken in the frame the CALLER decoded it in - orientation is applied
    # where the bytes are decoded (see _apply_exif_orientation), and re-deriving it
    # from an object that may not even be a PIL image is not this branch's job.
    if hasattr(source, "crop") and hasattr(source, "size"):
        return source

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Pillow ships with python-pptx
        raise PictureSourceError(
            f"Pillow is required to crop an image element out of the source image "
            f"({exc}); install it with: pip install -r requirements.txt"
        ) from exc

    try:
        if isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)
            if not data:
                raise ValueError("the source image is empty (0 bytes)")
        else:
            path = Path(source).expanduser()
            data = path.read_bytes()
            if not data:
                raise ValueError(f"the source image at {path} is empty (0 bytes)")
        # Read into memory and decode from there: the crop must not depend on a
        # file handle staying open, and `load()` forces the decode NOW so a
        # truncated file fails here rather than half way through the save.
        image = Image.open(io.BytesIO(data))
        image.load()
        return _apply_exif_orientation(image)
    except PictureSourceError:  # pragma: no cover - defensive
        raise
    except Exception as exc:  # noqa: BLE001 - every decode failure is the same failure
        raise PictureSourceError(
            f"the source image could not be decoded ({type(exc).__name__}: {exc})"
        ) from exc


class _SourceImage:
    """Decode the build's source image at most once, and remember the outcome.

    A slide may carry several image elements and the source can be a multi-megabyte
    PNG; decoding it per element would be wasteful, and re-reporting the same
    failure per element would be noise. Both the image and the failure are cached,
    and the failure is re-raised for every element that needs it, so each of them is
    still skipped with its own reason.
    """

    __slots__ = ("_source", "_image", "_error", "_loaded")

    def __init__(self, source: SourceImage | None) -> None:
        self._source = source
        self._image: Any = None
        self._error: PictureSourceError | None = None
        self._loaded = False

    @property
    def given(self) -> bool:
        """True when a source image was supplied at all (decodable or not)."""
        return self._source is not None

    def image(self):
        """The decoded image, or raise the (cached) :class:`PictureSourceError`."""
        if not self._loaded:
            self._loaded = True
            try:
                self._image = _decode_source_image(self._source)
            except PictureSourceError as exc:
                self._error = exc
        if self._error is not None:
            raise self._error
        return self._image


def _crop_box_px(bbox: BBox, width_px: int, height_px: int) -> tuple[int, int, int, int]:
    """The element's normalized bbox as a pixel crop box on the source image.

    x/w map onto the image WIDTH and y/h onto its HEIGHT - the same independent
    per-axis mapping the normalized system uses everywhere else. The box is
    intersected with the image and is never empty: a degenerate bbox yields a 1px
    crop rather than a ``ValueError`` from Pillow.
    """
    scale = float(config.COORD_SCALE) or 1000.0
    left = int(round(bbox.x / scale * width_px))
    top = int(round(bbox.y / scale * height_px))
    right = int(round((bbox.x + bbox.w) / scale * width_px))
    bottom = int(round((bbox.y + bbox.h) / scale * height_px))

    left = min(max(left, 0), max(width_px - 1, 0))
    top = min(max(top, 0), max(height_px - 1, 0))
    right = min(max(right, left + 1), width_px)
    bottom = min(max(bottom, top + 1), height_px)
    return left, top, right, bottom


def _crop_png(image, bbox: BBox) -> bytes:
    """Crop *image* to *bbox* and encode the crop as PNG bytes.

    PNG (lossless) because the crop is often a chart or a logo with hard edges,
    where JPEG artefacts would be visible. Modes PNG cannot store (CMYK, integer
    or float modes) are converted to RGB rather than failing the element.
    """
    try:
        width_px, height_px = (int(image.size[0]), int(image.size[1]))
    except Exception as exc:  # noqa: BLE001 - a source with no readable size
        raise PictureSourceError(
            f"the source image has no readable size ({type(exc).__name__}: {exc})"
        ) from exc
    if width_px <= 0 or height_px <= 0:
        raise PictureSourceError(
            f"the source image has a degenerate size ({width_px}x{height_px}px)"
        )

    box = _crop_box_px(bbox, width_px, height_px)
    try:
        crop = image.crop(box)
        if crop.mode not in ("1", "L", "LA", "P", "PA", "RGB", "RGBA"):
            crop = crop.convert("RGB")
        buffer = io.BytesIO()
        try:
            crop.save(buffer, format="PNG")
        except (OSError, ValueError):
            # e.g. a mode PNG refuses after all: one honest fallback, then give up.
            buffer = io.BytesIO()
            crop.convert("RGB").save(buffer, format="PNG")
        data = buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 - one bad crop may not sink the deck
        raise PictureSourceError(
            f"the region {bbox.as_tuple()} could not be cropped out of the "
            f"{width_px}x{height_px}px source image "
            f"({type(exc).__name__}: {exc})"
        ) from exc

    if not data:  # pragma: no cover - defensive
        raise PictureSourceError(
            f"the crop of region {bbox.as_tuple()} encoded to 0 bytes"
        )
    return data


def add_picture(slide, el: ImageElement, source: SourceImage | None = None):
    """Crop *el*'s region out of *source* and place it on the slide.

    ``source`` is the ORIGINAL image the analyzer looked at: bytes, a path, or an
    already-decoded PIL image. With no usable source this raises
    :class:`PictureSourceError` instead of inventing something; the caller
    (:func:`build_presentation`) turns that into a skipped element, a warning and a
    placeholder outline.

    The crop fills the element's bbox exactly. Its pixel aspect ratio need NOT match
    the bbox's on-slide aspect ratio, so the picture may be stretched: the
    normalized coordinate system scales the two axes independently, exactly as it
    already does for every shape, table and text box, and matching the deck's own
    geometry matters more here than preserving the crop's proportions. Said out loud
    rather than left to drift.
    """
    # Resolve the crop BEFORE any geometry is computed: with no source image there
    # is nothing to place, and scale_bbox would otherwise report its floor/clip
    # adjustments for an element that is about to be skipped (the placeholder path
    # reports them instead, once).
    image = _decode_source_image(source)
    left, top, width, height = scale_bbox(el.bbox)
    # NB: the crop is taken from the bbox the spec asked for, while the placement
    # uses the fitted geometry - so a bbox that scale_bbox had to clip or floor is
    # stretched into the adjusted box. scale_bbox has already warned in that case.
    png = _crop_png(image, el.bbox)

    picture = slide.shapes.add_picture(
        io.BytesIO(png), left, top, width=width, height=height
    )
    _disable_shadow(picture)
    _set_alt_text(picture, getattr(el, "description", None))
    return picture


def _add_image_placeholder(slide, el: ImageElement) -> bool:
    """Outline where an image element would have gone; True when it was drawn.

    A skipped image otherwise leaves a mysterious hole: the deck looks finished and
    the missing photo is only mentioned in a warning nobody re-reads. A thin dashed
    grey rectangle is quiet enough not to read as slide content, and it is a native
    shape, so the gap stays visible and editable in PowerPoint.
    """
    try:
        left, top, width, height = scale_bbox(el.bbox)
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
        _disable_shadow(shape)
        if getattr(shape, "has_text_frame", False):
            shape.text_frame.clear()
        shape.fill.background()
        shape.line.color.rgb = PLACEHOLDER_LINE_COLOR
        shape.line.width = Pt(PLACEHOLDER_LINE_WIDTH_PT)
        shape.line.dash_style = MSO_LINE_DASH_STYLE.DASH
        return True
    except Exception:  # noqa: BLE001 - a marker may never break a build
        return False


# --------------------------------------------------------------------------- #
# Anti-flattening: the fence around the picture exception
# --------------------------------------------------------------------------- #

#: A picture shape, as python-pptx reports one. ``LINKED_PICTURE`` is included for
#: the same reason ``services.preview`` includes it: it is still a picture.
_PICTURE_TYPES = (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE)


def _is_picture(shape) -> bool:
    """True when *shape* is a picture, by shape type or by raw tag."""
    try:
        if shape.shape_type in _PICTURE_TYPES:
            return True
    except Exception:  # noqa: BLE001 - some shapes raise on shape_type
        pass
    try:  # pragma: no cover - the tag check only matters if shape_type failed
        return shape._element.tag == qn("p:pic")
    except Exception:  # noqa: BLE001
        return False


def _shape_area(shape) -> int:
    """A shape's on-slide area in EMU^2 (0 when it cannot be measured)."""
    try:
        width = max(int(shape.width or 0), 0)
        height = max(int(shape.height or 0), 0)
    except Exception:  # noqa: BLE001 - defensive
        return 0
    return width * height


def _shape_rect(shape, clip_w: int, clip_h: int) -> tuple[int, int, int, int] | None:
    """A shape's on-slide box, clipped to the slide; ``None`` when it has no area."""
    try:
        left = int(shape.left or 0)
        top = int(shape.top or 0)
        right = left + max(int(shape.width or 0), 0)
        bottom = top + max(int(shape.height or 0), 0)
    except Exception:  # noqa: BLE001 - defensive: an unmeasurable shape
        return None
    left = max(left, 0)
    top = max(top, 0)
    right = min(right, clip_w)
    bottom = min(bottom, clip_h)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _union_area(rects: list[tuple[int, int, int, int]]) -> int:
    """The area of the UNION of *rects* (EMU^2), counting overlap once.

    Summing the rectangles instead would double-count it, and that is not a
    rounding detail: two copies of the same 45%-of-slide region - the classic
    vision-model repetition - summed to 90% and tripped the area rule over a slide
    that is 45% covered, dropping a picture that should have been kept while the
    warning quoted a coverage the deck never had. Three stacked crops reported
    135%, a percentage that cannot exist.

    A sweep over the x edges: within one vertical slab the set of rectangles is
    constant, so the union is (merged y length) * (slab width). Exact, and a
    handful of pictures is all this ever sees.
    """
    if not rects:
        return 0
    edges = sorted({edge for left, _, right, _ in rects for edge in (left, right)})
    total = 0
    for x0, x1 in zip(edges, edges[1:]):
        if x1 <= x0:  # pragma: no cover - a sorted set cannot produce this
            continue
        spans = sorted(
            (top, bottom) for left, top, right, bottom in rects if left <= x0 < right
        )
        covered = 0
        run_top, run_bottom = None, None
        for top, bottom in spans:
            if run_bottom is None or top > run_bottom:
                if run_bottom is not None:
                    covered += run_bottom - run_top
                run_top, run_bottom = top, bottom
            elif bottom > run_bottom:
                run_bottom = bottom
        if run_bottom is not None:
            covered += run_bottom - run_top
        total += covered * (x1 - x0)
    return total


def _picture_area(pictures: list, slide_w: int, slide_h: int) -> int:
    """How much of the slide the pictures together really cover (EMU^2)."""
    rects = [
        rect
        for rect in (_shape_rect(shape, slide_w, slide_h) for shape in pictures)
        if rect is not None
    ]
    return _union_area(rects)


def _pct_of(part: float, whole: float) -> float:
    """``part`` as a percentage of ``whole``, rounded; 0.0 when whole is unusable."""
    try:
        if not whole:
            return 0.0
        return round(float(part) / float(whole) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):  # pragma: no cover
        return 0.0


# The two predicates below are the ONLY place each numeric rule is evaluated, and
# both :func:`flattening_report` and :func:`_enforce_not_flattened` call them. If
# the reporter and the enforcer computed the same rule separately, they could
# disagree by a rounding step - and then the builder would happily write a deck its
# own report calls flattened.


def _is_full_bleed(shape, slide_w: int, slide_h: int) -> bool:
    """Rule 1: does this ONE picture cover the slide on both axes?"""
    try:
        width = int(shape.width or 0)
        height = int(shape.height or 0)
    except Exception:  # noqa: BLE001 - pragma: no cover - defensive
        return False
    return (
        width * 100.0 >= FULL_BLEED_PICTURE_PCT * slide_w
        and height * 100.0 >= FULL_BLEED_PICTURE_PCT * slide_h
    )


def _exceeds_picture_area(total_area: float, slide_area: float) -> bool:
    """Rule 2: do the pictures together cover more than the area limit?"""
    return _pct_of(total_area, slide_area) > MAX_PICTURE_AREA_PCT + _PCT_EPS


def _picture_bytes_resolve(shape) -> bool:
    """True when the picture's image bytes can actually be found in the package.

    A ``<a:blip r:embed="rIdN">`` whose relationship is missing is an OPC violation
    - PowerPoint answers it with its "found a problem with content" repair prompt,
    and python-pptx raises ``KeyError`` on ``shape.image``. It is worth a check
    because a deck like that is indistinguishable from a healthy one by shape
    count, size or geometry: every other number in this report reads normally.
    """
    try:
        return bool(shape.image.blob)
    except Exception:  # noqa: BLE001 - any failure to resolve is the failure
        return False


def flattening_report(presentation) -> dict:
    """Is this deck a real editable slide, or a flattened image of one?

    THE definition of "flattened" for this project - the tests, the API and the
    build itself all read it from here, so there is exactly one. It replaces the old
    "a deck must contain zero picture shapes" assertion, which stopped being true
    the moment a genuine photo/chart region became a legitimate ``image`` element.

    Three rules, per slide:

    1. **No single picture covering the slide** (>= :data:`FULL_BLEED_PICTURE_PCT`
       of the slide width AND of its height). This is the literal thing this tool
       exists not to do: the input image pasted onto a slide.
    2. **All pictures together cover at most** :data:`MAX_PICTURE_AREA_PCT` **of the
       slide.** Rule 1 alone has a hole: four pictures of 50%x50% each pass a 95%
       per-picture check while tiling the slide into exactly the flattened
       screenshot rule 1 forbids. Area closes it. The figure is the UNION of the
       picture boxes, clipped to the slide, so overlapping or duplicated crops are
       counted once and ``picture_area_pct`` really is the share of the slide the
       pictures cover (see :func:`_union_area`).
    3. **A picture-only slide is a flattened slide.** If a slide carries any
       picture it must ALSO carry at least one native object (auto shape,
       connector, table or text box). A deck whose only content is pictures is not
       an editable deck, whatever the individual sizes say.

    ``unreadable_picture_count`` is not one of the three rules and does not set
    ``flattened``: it counts pictures whose image bytes cannot be resolved out of
    the package, i.e. a deck PowerPoint would offer to repair. It is reported here
    because this is the seam every caller already trusts for "is the deck we are
    about to ship sound", and a corrupt picture must not pass that check unseen.

    Returns a dict::

        {"flattened": bool,            # any rule violated anywhere
         "violations": [str, ...],     # one human-readable line per violation
         "slide_count": int,
         "picture_count": int,
         "unreadable_picture_count": int,
         "slides": [{"index": int, "picture_count": int, "native_count": int,
                     "picture_area_pct": float, "full_bleed_count": int,
                     "unreadable_picture_count": int,
                     "flattened": bool, "violations": [str, ...]}, ...]}

    A deck with no pictures at all is trivially not flattened.
    """
    slide_w = int(getattr(presentation, "slide_width", 0) or 0)
    slide_h = int(getattr(presentation, "slide_height", 0) or 0)
    slide_area = slide_w * slide_h

    slides: list[dict] = []
    violations: list[str] = []
    picture_total = 0
    unreadable_total = 0

    for index, slide in enumerate(presentation.slides):
        shapes = list(slide.shapes)
        pictures = [shape for shape in shapes if _is_picture(shape)]
        natives = [shape for shape in shapes if not _is_picture(shape)]
        picture_total += len(pictures)
        unreadable = [shape for shape in pictures if not _picture_bytes_resolve(shape)]
        unreadable_total += len(unreadable)

        area = _picture_area(pictures, slide_w, slide_h)
        area_pct = _pct_of(area, slide_area)
        full_bleed = [
            shape for shape in pictures if _is_full_bleed(shape, slide_w, slide_h)
        ]

        own: list[str] = []
        if full_bleed:
            own.append(
                f"{len(full_bleed)} picture(s) cover >= {FULL_BLEED_PICTURE_PCT:g}% of "
                "both slide axes: that is the source image pasted onto the slide"
            )
        if _exceeds_picture_area(area, slide_area):
            own.append(
                f"pictures cover {area_pct:g}% of the slide, above the "
                f"{MAX_PICTURE_AREA_PCT:g}% limit: tiled pictures flatten a slide "
                "just as well as one big one"
            )
        if pictures and not natives:
            own.append(
                f"the slide holds {len(pictures)} picture(s) and no native shape, "
                "table or text box at all: a picture-only slide is not an editable "
                "deck"
            )

        slides.append(
            {
                "index": index,
                "picture_count": len(pictures),
                "native_count": len(natives),
                "picture_area_pct": area_pct,
                "full_bleed_count": len(full_bleed),
                "unreadable_picture_count": len(unreadable),
                "flattened": bool(own),
                "violations": own,
            }
        )
        violations.extend(f"slide {index + 1}: {reason}" for reason in own)

    return {
        "flattened": bool(violations),
        "violations": violations,
        "slide_count": len(slides),
        "picture_count": picture_total,
        "unreadable_picture_count": unreadable_total,
        "slides": slides,
    }


#: Every attribute a slide part can point at a relationship with. ``r:id`` is the
#: generic one, ``r:embed`` is what a picture's ``<a:blip>`` uses and ``r:link`` is
#: the linked-picture spelling. All three are counted before a relationship is
#: released - see :func:`_release_image_rel`.
_REL_REF_ATTRS = ("//@r:id", "//@r:embed", "//@r:link")


def _rel_is_still_referenced(part, rel_id: str) -> bool | None:
    """Does anything in *part*'s XML still point at ``rel_id``?

    ``None`` means "could not be determined", which callers treat as "yes": an
    unused image part costs a few kilobytes, while a dropped relationship that was
    still in use costs the whole file.
    """
    try:
        element = part._element
        for xpath in _REL_REF_ATTRS:
            if rel_id in set(element.xpath(xpath)):
                return True
        return False
    except Exception:  # noqa: BLE001 - pragma: no cover - defensive
        return None


def _release_image_rel(part, rel_id: str) -> None:
    """Drop a removed picture's image relationship - only if nothing else uses it.

    Deliberately NOT ``part.drop_rel()``. python-pptx 1.0.2 gates that on
    ``_rel_ref_count``, which counts ``//@r:id`` only; a picture points at its image
    part through ``r:embed``, so the count is 0 for an image relationship however
    many ``<p:pic>`` elements still reference it, and the relationship is popped
    unconditionally. That is fatal for identical crops: python-pptx de-duplicates
    image parts by SHA-1 and ``relate_to`` hands the SAME rId to both pictures, so
    dropping one of two byte-identical crops used to delete the relationship the
    SURVIVOR still points at. The result was a dangling ``r:embed`` and no
    ``ppt/media`` part at all - the OPC violation PowerPoint offers to "repair",
    shipped with a 200 and a warning claiming the .pptx was fine.

    Dropping the relationship is still worth doing when it IS unused: python-pptx
    serializes the parts it can reach through the relationship graph, so a kept
    relationship means the rejected crop's bytes travel in the package unused.
    """
    if _rel_is_still_referenced(part, rel_id) is not False:
        return  # still in use, or not provably unused: keep the bytes, keep the file
    try:
        part.rels.pop(rel_id)
    except Exception:  # noqa: BLE001 - pragma: no cover - an orphan part only
        _warn(
            "a rejected picture's image relationship could not be released; the deck "
            "may carry that crop's bytes as an unused part. The slide itself is "
            "correct - nothing references them."
        )


def _remove_shape(shape) -> bool:
    """Take *shape* off its slide again; True when it is really gone.

    For a picture the image RELATIONSHIP is released as well, and that is not a
    detail: removing only the ``<p:pic>`` element leaves the image part behind in
    the package (measured - ``ppt/media/image1.png`` was still written), so a deck
    that refuses to show a flattened image would still be shipping its bytes.

    The slide part is captured BEFORE the element is detached: ``shape.part`` walks
    up the XML tree, so afterwards there is no handle on it any more.
    """
    rel_id = None
    part = None
    if _is_picture(shape):
        try:
            rel_id = getattr(shape._element, "blip_rId", None)
            part = shape.part
        except Exception:  # noqa: BLE001 - pragma: no cover - defensive
            rel_id, part = None, None
    try:
        element = shape._element
        parent = element.getparent()
        if parent is None:  # pragma: no cover - defensive
            return False
        parent.remove(element)
    except Exception:  # noqa: BLE001 - pragma: no cover - defensive
        return False
    if rel_id and part is not None:
        _release_image_rel(part, rel_id)
    return True


def _enforce_not_flattened(prs, slide, pictures: list[tuple[int, Any]]) -> None:
    """Drop the picture(s) that would make this slide a flattened image.

    The analyzer can ask for a flattened slide - one huge ``image`` element, or four
    quarter-slide ones - and the honest answer is to refuse that one element and say
    so, not to write the deck the rule exists to prevent. The rules are
    :func:`flattening_report`'s; this applies them while the element indices are
    still known, so every warning names the element the user can go and fix.

    No placeholder outline is drawn here (unlike the missing-source case): these
    elements were rejected on purpose, and a full-bleed "missing image" outline
    would just be a second flattened rectangle.
    """
    if not pictures:
        return
    slide_w = int(getattr(prs, "slide_width", 0) or 0)
    slide_h = int(getattr(prs, "slide_height", 0) or 0)
    if slide_w <= 0 or slide_h <= 0:  # pragma: no cover - defensive
        return
    slide_area = slide_w * slide_h
    kept = list(pictures)

    def drop(entry: tuple[int, Any], reason: str) -> bool:
        index, shape = entry
        if _remove_shape(shape):
            kept.remove(entry)
            _warn(f"element #{index} (image) was NOT rendered: {reason}")
            return True
        # Only reachable if the shape's XML was already detached from the slide.
        _warn(
            f"element #{index} (image) {reason} - and it could NOT be taken off the "
            "slide again, so the deck may be flattened; do not trust this deck."
        )
        return False

    # Rule 1: one picture covering the slide.
    for entry in list(kept):
        _, shape = entry
        if _is_full_bleed(shape, slide_w, slide_h):
            drop(
                entry,
                f"its crop would cover >= {FULL_BLEED_PICTURE_PCT:g}% of both slide "
                "axes, i.e. the whole slide. That is the source image pasted onto a "
                "slide, which is exactly what this tool exists to avoid; skipped it. "
                "Re-run the conversion so the region is described as shapes, text and "
                "tables instead.",
            )

    # Rule 2: pictures tiling the slide. Largest first, until the covered area
    # fits. The figure is the UNION of the boxes (:func:`_picture_area`), the same
    # one the report publishes: summing them instead double-counted overlap, so two
    # copies of one 45% region read as 90% and a picture was dropped - with a
    # warning quoting a coverage the slide never had - over a slide 45% covered.
    while kept:
        total = _picture_area([shape for _, shape in kept], slide_w, slide_h)
        if not _exceeds_picture_area(total, slide_area):
            break
        entry = max(kept, key=lambda item: _shape_area(item[1]))
        if not drop(
            entry,
            f"the image elements together would cover "
            f"{_pct_of(total, slide_area):g}% of the slide, above the "
            f"{MAX_PICTURE_AREA_PCT:g}% limit - tiled crops flatten a slide just as "
            "well as one big one; skipped the largest of them.",
        ):
            break  # pragma: no cover - drop() only fails if the XML is detached

    # Rule 3: nothing on the slide but pictures.
    if kept and not [shape for shape in slide.shapes if not _is_picture(shape)]:
        for entry in list(kept):
            if not drop(
                entry,
                "the slide would hold nothing but picture(s) - no shape, table or "
                "text box at all - and a picture-only slide is a flattened image, not "
                "an editable deck; skipped it.",
            ):
                break  # pragma: no cover - as above


# --------------------------------------------------------------------------- #
# Deck assembly
# --------------------------------------------------------------------------- #


def _apply_background(slide, color: str | None) -> None:
    """Paint the slide background, degrading silently when the colour is unusable."""
    rgb = to_rgb(color)
    if rgb is None:
        return
    try:
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = rgb
    except (AttributeError, NotImplementedError, ValueError) as exc:
        _warn(f"could not set slide background ({exc}); continuing without it")


def _render_element(slide, el, source: _SourceImage | None = None):
    """Dispatch one element to its renderer; returns the shape it added.

    ``source`` is only consulted by an ``image`` element and stays optional so the
    other renderers can be called (and tested) without one.
    """
    if isinstance(el, ShapeElement):
        return add_shape(slide, el)
    if isinstance(el, TableElement):
        return add_table(slide, el)
    if isinstance(el, TextElement):
        return add_textbox(slide, el)
    if isinstance(el, ConnectorElement):
        return add_connector(slide, el)
    if isinstance(el, ImageElement):
        return add_picture(slide, el, source.image() if source is not None else None)
    raise PptxBuildError(f"unsupported element type: {getattr(el, 'type', type(el).__name__)!r}")


def _shape_elements(slide) -> list:
    """The lxml elements of the shapes currently on *slide* (best effort)."""
    try:
        return [shape._element for shape in slide.shapes]
    except Exception:  # pragma: no cover - defensive, only feeds the rollback
        return []


def _rollback_shapes(slide, before: list) -> bool:
    """Remove shapes added since *before*; True when the slide is clean again.

    A renderer can fail half way through - ``slide.shapes.add_textbox()``
    succeeds and only the text assignment raises - which used to leave a hollow
    shape in the deck while the warning called the element "skipped". Undoing
    the partial shape makes the message true; if the undo itself cannot be done,
    the caller says "partially rendered" instead of lying the other way.
    """
    try:
        kept = {id(element) for element in before}
        for shape in list(slide.shapes):
            element = shape._element
            if id(element) in kept:
                continue
            parent = element.getparent()
            if parent is None:  # pragma: no cover - defensive
                return False
            parent.remove(element)
        return True
    except Exception:  # pragma: no cover - the rollback may never raise either
        return False


def build_presentation(
    spec: SlideSpec, source_image: SourceImage | None = None
) -> Presentation:
    """Build a one-slide 16:9 deck from ``spec``.

    Elements are rendered in order, which is the z-order. One failing element is
    reported on stderr and skipped; it never aborts the build.

    Args:
        spec: the slide to draw.
        source_image: the ORIGINAL image the analyzer looked at - raw bytes (the
            HTTP layer), a path (the CLI), or an already-decoded PIL image -
            needed only to crop ``image`` elements out of. It is optional and
            defaults to ``None`` so every existing caller keeps working unchanged;
            with no source image, image elements are skipped with a warning and a
            placeholder outline while the rest of the slide builds normally. The
            image is decoded at most once per build, and only if it is needed.
    """
    if spec is None:
        raise PptxBuildError("build_presentation() requires a SlideSpec, got None")

    prs = Presentation()
    prs.slide_width = Inches(config.SLIDE_WIDTH_IN)
    prs.slide_height = Inches(config.SLIDE_HEIGHT_IN)
    # cx/cy alone leave the template's inherited type="screen4x3" contradicting them.
    _tag_slide_size(prs)

    try:
        blank_layout = prs.slide_layouts[6]
    except IndexError:  # pragma: no cover - default template always has 11 layouts
        blank_layout = prs.slide_layouts[-1]
    slide = prs.slides.add_slide(blank_layout)

    _apply_background(slide, getattr(spec, "background_color", None))

    source = _SourceImage(source_image)
    #: (element index, picture shape) for every crop that reached the slide, so the
    #: anti-flattening pass can name the element behind a picture it has to drop.
    pictures: list[tuple[int, Any]] = []

    for index, el in enumerate(getattr(spec, "elements", None) or []):
        before = _shape_elements(slide)
        try:
            shape = _render_element(slide, el, source)
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            kind = getattr(el, "type", type(el).__name__)
            detail = f"{type(exc).__name__}: {exc}"
            # Say "skipped" only when nothing of the element is left behind.
            if not _rollback_shapes(slide, before):  # pragma: no cover - rollback fails
                _warn(
                    f"element #{index} ({kind}) failed half way through and is only "
                    f"PARTIALLY rendered (an empty shape may remain): {detail}"
                )
            elif isinstance(el, ImageElement):
                # An image element leaves a visible hole, so mark it: the message
                # says plainly that nothing was rendered, and the outline shows
                # where.
                marked = _add_image_placeholder(slide, el)
                tail = (
                    " A dashed placeholder outline marks its bbox so the gap is "
                    "visible in the deck."
                    if marked
                    else ""
                )
                _warn(
                    f"element #{index} (image) was NOT rendered: "
                    f"{_sentence(detail)}{tail}"
                )
            else:
                _warn(f"skipped element #{index} ({kind}): {detail}")
        else:
            if shape is not None and _is_picture(shape):
                pictures.append((index, shape))

    # Last, because rule 3 can only be judged once the whole slide is on the page.
    _enforce_not_flattened(prs, slide, pictures)

    return prs


def build_pptx(
    spec: SlideSpec, output_path: str | Path, source_image: SourceImage | None = None
) -> Path:
    """Build the deck and write it to ``output_path``; returns the resolved path.

    ``source_image`` is passed straight to :func:`build_presentation` and is
    optional for the same reason it is optional there.
    """
    path = Path(output_path).expanduser()
    prs = build_presentation(spec, source_image)

    parent = path.parent
    try:
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PptxBuildError(f"cannot create output directory {parent}: {exc}") from exc

    try:
        prs.save(str(path))
    except OSError as exc:
        raise PptxBuildError(f"cannot write presentation to {path}: {exc}") from exc

    return path.resolve()
