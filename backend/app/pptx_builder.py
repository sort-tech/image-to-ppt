"""Render a :class:`schemas.SlideSpec` into a real, native PowerPoint deck.

Everything this module emits is a genuine, editable PowerPoint object: auto shapes,
graphic-frame tables and text boxes. No part of the input image is ever embedded as a
picture, so the resulting deck contains zero picture shapes.

Design notes
------------
* Coordinates arrive as integers in ``0..config.COORD_SCALE`` on both axes and are
  mapped linearly onto the 16:9 slide (13.333in x 7.5in) by :func:`scale_bbox`.
* ``spec.elements`` order *is* the z-order: earlier elements are drawn first and later
  elements paint on top.
* Robustness is a hard requirement: a single malformed element (or colour) must never
  abort the whole build. :func:`build_presentation` isolates every element, reports the
  failure on stderr and carries on with the rest.

Verified against python-pptx 1.0.2 (``GraphicFrame.shadow`` raises
``NotImplementedError`` there, hence the guarded shadow helper).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from . import config
from .schemas import BBox, ShapeElement, SlideSpec, TableElement, TextElement

__all__ = [
    "PptxBuildError",
    "SHAPE_MAP",
    "ALIGN_MAP",
    "MIN_DIMENSION_IN",
    "SLIDE_SIZE_TYPE_16X9",
    "SLIDE_SIZE_TYPE_CUSTOM",
    "PLAIN_TABLE_STYLE_ID",
    "TABLE_FONT_DARK",
    "TABLE_FONT_LIGHT",
    "to_rgb",
    "scale_bbox",
    "add_shape",
    "add_table",
    "add_textbox",
    "build_presentation",
    "build_pptx",
]


class PptxBuildError(Exception):
    """Raised when a deck (or a single element of it) cannot be rendered."""


#: Schema shape kind -> python-pptx auto-shape preset.
SHAPE_MAP: dict[str, MSO_SHAPE] = {
    "rectangle": MSO_SHAPE.RECTANGLE,
    "rounded_rectangle": MSO_SHAPE.ROUNDED_RECTANGLE,
    "circle": MSO_SHAPE.OVAL,
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


def _render_element(slide, el) -> None:
    """Dispatch one element to its renderer."""
    if isinstance(el, ShapeElement):
        add_shape(slide, el)
        return
    if isinstance(el, TableElement):
        add_table(slide, el)
        return
    if isinstance(el, TextElement):
        add_textbox(slide, el)
        return
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


def build_presentation(spec: SlideSpec) -> Presentation:
    """Build a one-slide 16:9 deck from ``spec``.

    Elements are rendered in order, which is the z-order. One failing element is
    reported on stderr and skipped; it never aborts the build.
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

    for index, el in enumerate(getattr(spec, "elements", None) or []):
        before = _shape_elements(slide)
        try:
            _render_element(slide, el)
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            kind = getattr(el, "type", type(el).__name__)
            detail = f"{type(exc).__name__}: {exc}"
            # Say "skipped" only when nothing of the element is left behind.
            if _rollback_shapes(slide, before):
                _warn(f"skipped element #{index} ({kind}): {detail}")
            else:  # pragma: no cover - only if the rollback itself fails
                _warn(
                    f"element #{index} ({kind}) failed half way through and is only "
                    f"PARTIALLY rendered (an empty shape may remain): {detail}"
                )

    return prs


def build_pptx(spec: SlideSpec, output_path: str | Path) -> Path:
    """Build the deck and write it to ``output_path``; returns the resolved path."""
    path = Path(output_path).expanduser()
    prs = build_presentation(spec)

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
