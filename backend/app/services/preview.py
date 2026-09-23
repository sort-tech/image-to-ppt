"""Describe a BUILT deck as a render-ready payload the UI can draw.

The whole point of this module is where it reads from: it walks the shapes of a
real :class:`pptx.presentation.Presentation` and reports the geometry and
styling python-pptx **actually** produced. It never looks at the ``SlideSpec``.

That is deliberate, and it is the only design that can be correct. Re-deriving
the preview from the spec would mean a second implementation of
``pptx_builder.scale_bbox`` - and the moment the two disagreed, the user would
be shown a picture of a slide that does not exist. Reading the built deck means
the preview shows, for free and by construction:

* the min-dimension floor (``MIN_DIMENSION_IN``), so a 5-unit-tall band is
  previewed at the height it was actually enlarged to;
* the off-slide clipping, so a bbox that overshot 1000 is previewed where it
  really landed;
* the cleaned text - control characters dropped, CRLF split into paragraphs,
  lone surrogates replaced - because the runs are read back out of the deck;
* the z-order, because ``slide.shapes`` is in document order, which *is* the
  z-order, back to front.

Output units
------------
Geometry is emitted as **percentages of the slide** so the UI can scale to any
width with no arithmetic, and font sizes in **points** so text can be sized
against ``slide.height_pt`` (540pt for the 7.5in slide). Every numeric field is
a JSON number. Percentages are deliberately NOT clamped: a shape may legitimately
sit partly off the slide, and clipping it is the UI's job, not this module's.

Colours
-------
Only an explicit RGB colour is reported. A theme colour, an inherited colour or
no fill at all comes back as ``None`` - never a guess and never an exception.
This matters more than it looks: on python-pptx 1.0.2 (verified)
``fill.fore_color.rgb`` RAISES rather than returning a falsy value - ``TypeError``
when the fill is not solid, ``AttributeError`` when the colour is a scheme
colour - so every colour read here is guarded.

Connectors and pictures
-----------------------
Two element kinds do not fit the "a box with styling" mould and are read
differently:

* a **connector** has no box at all - it is two points - so it is reported as
  ``start_pct``/``end_pct`` instead of ``left/top/width/height``. It is detected
  as ``MSO_SHAPE_TYPE.LINE``, because ``MSO_SHAPE_TYPE.CONNECTOR`` does not
  exist on python-pptx 1.0.2 (verified) and matching on it would match nothing;
* a **picture** is now a legitimate element - an ``image`` region the analyzer
  could not express as a shape, a table or text, cropped out of the source
  image - so it is reported as ``kind: "image"`` carrying the crop itself. The
  crop is read back out of the DECK (``shape.image.blob``), never from the
  source image, so the preview shows the bytes that were really embedded.

Robustness
----------
Every accessor is wrapped. A shape this module cannot make sense of degrades to
``{"kind": "unsupported"}`` with its geometry rather than raising. The caller
(``converter.preview_for``) additionally treats any escape from here as "no
preview, plus a warning", so a preview can never fail a conversion. Where a
field has to be given up on - a crop too big to ship, a picture whose bytes
cannot be read - the field goes to ``null`` AND a line is written to stderr,
which ``converter.warning_lines`` turns into the response's ``warnings`` array:
a missing crop is visible, never silent.
"""

from __future__ import annotations

import base64
import io
import re
import sys
from typing import Any

from pptx.enum.dml import MSO_COLOR_TYPE, MSO_FILL
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn
from pptx.util import Emu

from .. import config

__all__ = [
    "build_preview",
    "PREVIEW_IMAGE_MAX_PX",
    "PREVIEW_IMAGE_MAX_URI_BYTES",
    "WARN_PREFIX",
]

#: python-pptx auto-shape preset -> the name the preview contract uses.
#: Anything else is reported as ``"other"``; the UI draws it as a plain box.
_AUTOSHAPE_NAMES: dict[str, str] = {
    "RECTANGLE": "rectangle",
    "ROUNDED_RECTANGLE": "rounded_rectangle",
    "OVAL": "oval",
}

#: Paragraph alignment -> the contract's three values. ``JUSTIFY`` and friends
#: have no preview representation, so they read as the default.
_ALIGN_NAMES: dict[str, str] = {
    "LEFT": "left",
    "CENTER": "center",
    "RIGHT": "right",
}

#: The contract's default alignment, used whenever none is set in the file.
_DEFAULT_ALIGN = "left"

#: Percentages are rounded here: enough precision that a 1px feature on a 4K
#: screen still lands right, few enough digits that the JSON stays readable.
_PCT_DIGITS = 4

#: Point sizes are rounded to the precision PowerPoint itself stores (1/100 pt).
_PT_DIGITS = 2

#: Shape types that carry an embedded (or linked) bitmap. These are the
#: ``image`` elements: a region the analyzer could not express natively, cropped
#: out of the source image by the builder. They are legitimate - what is NOT
#: legitimate is a deck made only of them, which is what
#: ``pptx_builder.flattening_report`` exists to catch.
_PICTURE_TYPES = (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE)

#: How a connector reports itself. NOT ``MSO_SHAPE_TYPE.CONNECTOR``: that member
#: does not exist on python-pptx 1.0.2, and a shape added with
#: ``slide.shapes.add_connector`` comes back as ``LINE`` (value 9) - verified.
_CONNECTOR_TYPES = (MSO_SHAPE_TYPE.LINE,)

#: ``<a:prstGeom prst>`` values that mean "elbow". python-pptx writes ``line``
#: for ``MSO_CONNECTOR.STRAIGHT`` and ``bentConnector3`` for ``.ELBOW``
#: (verified); the other bent presets are listed because PowerPoint itself uses
#: them when a user drags an elbow's midpoint, and such a deck may be re-read.
_ELBOW_PRESETS = frozenset(
    {"bentconnector2", "bentconnector3", "bentconnector4", "bentconnector5"}
)

#: The contract's two dash values. Anything that is not literally ``SOLID`` -
#: including the dotted and dash-dot presets PowerPoint can set - previews as
#: "dashed", because the UI has exactly one non-solid stroke to draw.
_SOLID_DASH_NAME = "SOLID"

#: An ``<a:headEnd>``/``<a:tailEnd>`` with this type is an explicit "no arrow".
_NO_ARROW_TYPE = "none"

#: Longest side of the crop shipped in a preview, in pixels. The preview is a
#: thumbnail in a panel a few hundred pixels wide; anything larger is bytes the
#: client pays for and cannot see.
PREVIEW_IMAGE_MAX_PX: int = 480

#: Hard cap on one ``data_uri``, in bytes of the URI itself. Past this the field
#: goes to ``null`` with a warning: a preview is a convenience and may not turn
#: a JSON response into a multi-megabyte download.
PREVIEW_IMAGE_MAX_URI_BYTES: int = 96 * 1024

#: JPEG qualities tried, in order, when the PNG re-encode will not fit the cap.
#: A photographic crop at 480px is routinely ~200-400 KB as PNG, so a PNG-only
#: encoder would answer ``null`` for exactly the elements this feature exists to
#: show. The crop is already being re-encoded (it is resized), so trading
#: losslessness for a visible thumbnail costs nothing the preview was promising.
_JPEG_QUALITIES: tuple[int, ...] = (80, 60, 40)

#: What a stderr line from this module is prefixed with, so
#: ``converter.warning_lines`` can strip it (and so a test can find it).
WARN_PREFIX: str = "[preview] warning: "

#: python-pptx's own default alt text for a picture added from a stream is the
#: literal ``image.png`` (verified), and from a path it is the basename. Neither
#: is a description, so a ``descr`` that is merely a filename is discarded - the
#: alternative is showing the user "image.png" as the model's description of the
#: region, which would be a false claim.
_FILENAME_DESCR_RE = re.compile(r"(?i)\A[^\s/\\]{1,120}\.(png|jpe?g|webp|gif|bmp|tiff?)\Z")

#: python-pptx names added pictures ``Picture 1``, ``Picture 2``, ... Those are
#: placeholders, not descriptions, and are discarded for the same reason.
_GENERIC_PICTURE_NAME_RE = re.compile(r"(?i)\A(picture|image|graphic|object)\s*\d*\Z")


def _warn(message: str) -> None:
    """Report a field this module had to give up on, on stderr.

    stderr is how every other module in this project reports a non-fatal
    problem, and ``converter.warning_lines`` already turns that stream into the
    response's ``warnings`` array - so a dropped crop reaches the user through
    the channel they already read, and the CLI keeps seeing it too. The preview
    payload itself gains no new top-level field, which is what keeps the
    existing contract byte-identical.
    """
    print(f"{WARN_PREFIX}{message}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# guarded readers - none of these may raise, whatever the file contains
# --------------------------------------------------------------------------- #
def _pct(value: Any, total: Any) -> float:
    """``value`` as a percentage of ``total``, as a JSON number."""
    try:
        divisor = float(total)
        if divisor <= 0:
            return 0.0
        return round(float(value) / divisor * 100.0, _PCT_DIGITS)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _rgb(color: Any) -> str | None:
    """``"#RRGGBB"`` when the colour is an explicit RGB, else ``None``.

    Reading ``.rgb`` on anything other than an RGB colour raises on python-pptx
    1.0.2, so the TYPE is checked first and the read is guarded anyway.
    """
    try:
        if color is None or color.type != MSO_COLOR_TYPE.RGB:
            return None
        return f"#{str(color.rgb).upper()}"
    except Exception:  # noqa: BLE001 - a colour may never break a preview
        return None


def _solid_fill(fill: Any) -> str | None:
    """The fill's explicit RGB colour, or ``None`` for no-fill / theme / other.

    ``None`` is what the UI draws as see-through, which is exactly right for
    both "the spec asked for no fill" and "this colour is not an RGB we can
    show" - in neither case may the preview invent a colour.
    """
    try:
        if fill is None or fill.type != MSO_FILL.SOLID:
            return None
        return _rgb(fill.fore_color)
    except Exception:  # noqa: BLE001
        return None


def _line_color(line: Any) -> str | None:
    """The outline's explicit RGB colour, or ``None`` when there is no outline."""
    try:
        if line is None or line.fill.type != MSO_FILL.SOLID:
            return None
        return _rgb(line.color)
    except Exception:  # noqa: BLE001
        return None


def _line_width_pt(line: Any) -> float | None:
    """The outline width in points, or ``None`` when there is none.

    An unset width reads back as ``Emu(0)`` rather than ``None``, and a 0pt
    outline is not an outline, so zero is reported as ``None`` too.
    """
    try:
        width = None if line is None else line.width
        if width is None:
            return None
        points = round(float(Emu(int(width)).pt), _PT_DIGITS)
        return points if points > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _font_size_pt(fonts: list[Any], *, default: float) -> float:
    """The first explicit size among ``fonts``, else the builder's default.

    The fallback is what keeps the UI from having to guess: a run that carries
    no size inherits one from PowerPoint, and the value it would inherit is the
    same one ``pptx_builder`` passes to ``_positive(...)``, so the preview
    reports that rather than ``null``.
    """
    for font in fonts:
        try:
            size = font.size
        except Exception:  # noqa: BLE001
            continue
        if size is None:
            continue
        try:
            return round(float(size.pt), _PT_DIGITS)
        except (AttributeError, TypeError, ValueError):
            continue
    return float(default)


def _font_color(fonts: list[Any]) -> str | None:
    """The first explicit RGB colour among ``fonts``, else ``None``."""
    for font in fonts:
        try:
            color = font.color
        except Exception:  # noqa: BLE001
            continue
        found = _rgb(color)
        if found is not None:
            return found
    return None


def _bold(fonts: list[Any]) -> bool:
    """The first explicit bold state among ``fonts``, else ``False``.

    ``None`` means "inherit", which the builder never writes for text it owns -
    it writes ``b="0"`` on purpose - so a ``None`` here really is an absence.
    """
    for font in fonts:
        try:
            value = font.bold
        except Exception:  # noqa: BLE001
            continue
        if value is not None:
            return bool(value)
    return False


def _align(paragraph: Any, default: str = _DEFAULT_ALIGN) -> str:
    """The paragraph's alignment as ``"left"``/``"center"``/``"right"``."""
    try:
        alignment = None if paragraph is None else paragraph.alignment
        if alignment is None:
            return default
        name = getattr(alignment, "name", None) or str(alignment)
        return _ALIGN_NAMES.get(str(name).upper(), default)
    except Exception:  # noqa: BLE001
        return default


def _autoshape(shape: Any) -> str:
    """The auto-shape preset as the contract names it, else ``"other"``."""
    try:
        preset = shape.auto_shape_type
        if preset is None:
            return "other"
        name = getattr(preset, "name", None) or str(preset)
        return _AUTOSHAPE_NAMES.get(str(name).upper(), "other")
    except Exception:  # noqa: BLE001
        return "other"


def _shape_type(shape: Any) -> Any:
    try:
        return shape.shape_type
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# connector readers
# --------------------------------------------------------------------------- #
def _emu_attr(shape: Any, name: str) -> int:
    """One of ``begin_x``/``begin_y``/``end_x``/``end_y`` as an int of EMU.

    These are the only geometry a connector has, and python-pptx reads them back
    through the ``flipH``/``flipV`` it wrote - so a right-to-left connector
    round-trips with its start still at the start (verified on 1.0.2, before and
    after a save/reload).
    """
    try:
        value = getattr(shape, name, None)
        return 0 if value is None else int(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0


def _prst_geom(shape: Any) -> str:
    """The shape's ``<a:prstGeom prst>`` value, lowercased, or ``""``."""
    try:
        preset = shape._element.spPr.find(qn("a:prstGeom"))
        if preset is None:
            return ""
        return str(preset.get("prst") or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _line_element(shape: Any) -> Any:
    """The shape's ``<a:ln>`` element, or ``None`` when it has no outline.

    Read off the lxml tree rather than through ``LineFormat``: the arrowheads
    live in ``<a:headEnd>``/``<a:tailEnd>``, and python-pptx 1.0.2 exposes
    NEITHER (``line`` offers only colour, dash_style, fill and width - verified),
    so the element is the only place they can be read from.
    """
    try:
        return shape._element.spPr.find(qn("a:ln"))
    except Exception:  # noqa: BLE001
        return None


def _has_arrow(line_element: Any, tag: str) -> bool:
    """Whether ``<a:ln>`` carries an arrowhead at ``tag``.

    ``a:headEnd`` is the BEGIN end of the line and ``a:tailEnd`` the END one,
    which is how ``arrow_start``/``arrow_end`` map onto the file.
    """
    if line_element is None:
        return False
    try:
        end = line_element.find(qn(tag))
        if end is None:
            return False
        kind = str(end.get("type") or "").strip().lower()
        # A missing type attribute defaults to "none" in the schema, so only an
        # explicit, non-"none" arrowhead counts as an arrow.
        return bool(kind) and kind != _NO_ARROW_TYPE
    except Exception:  # noqa: BLE001
        return False


def _dash(line: Any) -> str:
    """``"dashed"`` for any preset dash, ``"solid"`` otherwise.

    An untouched line reads back as ``dash_style is None``, which is solid.
    """
    try:
        style = None if line is None else line.dash_style
    except Exception:  # noqa: BLE001
        return "solid"
    if style is None:
        return "solid"
    name = str(getattr(style, "name", None) or style).upper()
    return "solid" if name == _SOLID_DASH_NAME else "dashed"


# --------------------------------------------------------------------------- #
# picture readers
# --------------------------------------------------------------------------- #
def _alt_text(shape: Any) -> str:
    """The picture's alt text (``<p:cNvPr descr>``), or ``""``."""
    try:
        return str(shape._element._nvXxPr.cNvPr.get("descr") or "")
    except Exception:  # noqa: BLE001
        return ""


def _picture_description(shape: Any) -> str | None:
    """The description the builder recorded on the picture, or ``None``.

    The preview reads the DECK, so the description has to be in the deck: the
    alt text is the semantically right place for it, and the shape name is
    accepted as a second spelling. Values that are only python-pptx's own
    defaults - ``image.png``, ``Picture 1`` - are discarded rather than shown,
    because reporting them as the analyzer's description would be untrue.
    """
    for raw in (_alt_text(shape), _safe_name(shape)):
        text = (raw or "").strip()
        if not text:
            continue
        if _FILENAME_DESCR_RE.match(text) or _GENERIC_PICTURE_NAME_RE.match(text):
            continue
        return text
    return None


def _safe_name(shape: Any) -> str:
    try:
        return str(shape.name or "")
    except Exception:  # noqa: BLE001
        return ""


def _encode_image(image: Any, image_format: str, **options: Any) -> bytes:
    """One Pillow encode into memory, or ``b""`` when it cannot be done."""
    try:
        buffer = io.BytesIO()
        image.save(buffer, image_format, **options)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - an encoder may never break a preview
        return b""


def _thumbnail_candidates(blob: bytes) -> list[tuple[str, bytes]] | None:
    """``[(content_type, bytes), ...]`` for ``blob``, best quality first.

    ``None`` means Pillow could not be used at all, which is a different case
    from "Pillow produced nothing usable" - see :func:`_crop_data_uri`.

    ``Image.thumbnail`` only ever shrinks, so a crop already under
    :data:`PREVIEW_IMAGE_MAX_PX` is re-encoded at its own size rather than
    blown up.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is in requirements.txt
        return None

    try:
        with Image.open(io.BytesIO(blob)) as opened:
            opened.load()
            thumbnail = opened.copy()
    except Exception:  # noqa: BLE001 - a corrupt blob is not a crash
        return []

    try:
        thumbnail.thumbnail(
            (PREVIEW_IMAGE_MAX_PX, PREVIEW_IMAGE_MAX_PX),
            getattr(Image, "LANCZOS", None) or Image.BICUBIC,
        )
    except Exception:  # noqa: BLE001 - previewing the full size beats nothing
        pass

    candidates: list[tuple[str, bytes]] = []
    png = _encode_image(thumbnail, "PNG", optimize=True)
    if png:
        candidates.append(("image/png", png))

    # JPEG cannot carry alpha or a palette, so flatten onto white first.
    flattened = thumbnail
    if thumbnail.mode not in {"RGB", "L"}:
        try:
            flattened = thumbnail.convert("RGB")
        except Exception:  # noqa: BLE001
            flattened = None  # type: ignore[assignment]
    if flattened is not None:
        for quality in _JPEG_QUALITIES:
            jpeg = _encode_image(flattened, "JPEG", quality=quality, optimize=True)
            if jpeg:
                candidates.append(("image/jpeg", jpeg))
    return candidates


def _data_uri(content_type: str, data: bytes) -> str:
    """``data:<type>;base64,<...>``, all ASCII, so ``len`` IS its byte count."""
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def _crop_data_uri(shape: Any) -> str | None:
    """The picture's own bytes as a capped, downscaled ``data:`` URI.

    Read from the DECK, not from the source image: the point of the preview is
    to show what was really embedded, so a builder that cropped the wrong region
    must be visible here rather than papered over.

    Returns ``None`` - and warns - when the bytes cannot be read, cannot be
    decoded, or will not fit :data:`PREVIEW_IMAGE_MAX_URI_BYTES` even at the
    lowest quality. ``None`` is a value the contract pins and the UI draws as a
    labelled placeholder, so it is a degradation, not a failure.
    """
    try:
        image = shape.image
        blob = bytes(image.blob or b"")
        content_type = str(image.content_type or "") or "application/octet-stream"
        pixels = tuple(int(value) for value in (image.size or (0, 0)))
    except Exception as exc:  # noqa: BLE001
        _warn(
            f"an image element's embedded bytes could not be read back from the "
            f"deck ({type(exc).__name__}: {exc}); the preview shows a placeholder "
            "instead of the crop. The .pptx itself is unaffected."
        )
        return None

    if not blob:
        _warn(
            "an image element carries no embedded bytes, so the preview shows a "
            "placeholder instead of the crop. The .pptx itself is unaffected."
        )
        return None

    candidates = _thumbnail_candidates(blob)
    if candidates is None:
        # No Pillow: the crop can only be shipped if it ALREADY satisfies the
        # size rule, because there is nothing here that can shrink it. Shipping
        # it unresized would break the contract's 480px promise.
        longest = max(pixels) if pixels else 0
        candidates = (
            [(content_type, blob)] if 0 < longest <= PREVIEW_IMAGE_MAX_PX else []
        )

    for candidate_type, data in candidates:
        uri = _data_uri(candidate_type, data)
        if len(uri) <= PREVIEW_IMAGE_MAX_URI_BYTES:
            return uri

    _warn(
        "an image element's crop is too large to include in the preview "
        f"(it does not fit {PREVIEW_IMAGE_MAX_URI_BYTES // 1024} KB even "
        f"downscaled to {PREVIEW_IMAGE_MAX_PX}px), so the preview shows a "
        "placeholder instead. The .pptx itself carries the full crop."
    )
    return None


def _fonts_of(paragraph: Any) -> list[Any]:
    """The fonts to consult for one paragraph, most specific first.

    A run's own font wins over the paragraph's, which is the order PowerPoint
    resolves them in. An empty paragraph has no runs at all - the builder styles
    the paragraph font precisely so that case still carries the right size - so
    the paragraph font is always kept as the fallback.
    """
    fonts: list[Any] = []
    try:
        runs = list(paragraph.runs)
    except Exception:  # noqa: BLE001
        runs = []
    if runs:
        try:
            fonts.append(runs[0].font)
        except Exception:  # noqa: BLE001
            pass
    try:
        fonts.append(paragraph.font)
    except Exception:  # noqa: BLE001
        pass
    return fonts


def _paragraph_text(paragraph: Any) -> str:
    """The paragraph's text, read back out of its runs."""
    try:
        runs = list(paragraph.runs)
    except Exception:  # noqa: BLE001
        runs = []
    if runs:
        return "".join(str(run.text or "") for run in runs)
    try:
        return str(paragraph.text or "")
    except Exception:  # noqa: BLE001
        return ""


def _track_pcts(sizes: list[int], fallback_total: Any) -> list[float]:
    """Column widths / row heights as percentages of the TABLE's own extent.

    The denominator is the sum of the tracks rather than the frame's width or
    height, so the list always sums to ~100 and the UI's grid fills its box
    exactly. ``pptx_builder`` distributes the frame evenly with integer EMU
    division, so the two denominators differ by at most a few EMU anyway - far
    below one screen pixel - and the frame extent is kept only as the fallback
    for a degenerate table whose tracks are all zero.
    """
    total = sum(sizes)
    if total <= 0:
        try:
            total = int(fallback_total or 0)
        except (TypeError, ValueError):
            total = 0
    if total <= 0:
        return [0.0 for _ in sizes]
    return [_pct(size, total) for size in sizes]


# --------------------------------------------------------------------------- #
# per-shape payloads
# --------------------------------------------------------------------------- #
def _geometry(shape: Any, slide_width: int, slide_height: int) -> dict[str, float]:
    """The shape's box as percentages of the slide.

    x and width scale by the slide's WIDTH, y and height by its HEIGHT. On a
    16:9 slide those differ by 1.78x, so swapping them is not a rounding error -
    it is a visibly wrong picture. ``tests/test_preview.py`` asserts the pairing
    against a deliberately non-square box for exactly that reason.
    """
    return {
        "left_pct": _pct(shape.left, slide_width),
        "top_pct": _pct(shape.top, slide_height),
        "width_pct": _pct(shape.width, slide_width),
        "height_pct": _pct(shape.height, slide_height),
    }


def _text_payload(shape: Any, geometry: dict[str, float]) -> dict[str, Any]:
    """A text box: one entry per paragraph, each with its own styling."""
    paragraphs: list[dict[str, Any]] = []
    try:
        frame_paragraphs = list(shape.text_frame.paragraphs)
    except Exception:  # noqa: BLE001
        frame_paragraphs = []

    for paragraph in frame_paragraphs:
        fonts = _fonts_of(paragraph)
        paragraphs.append(
            {
                "text": _paragraph_text(paragraph),
                "font_size_pt": _font_size_pt(
                    fonts, default=config.DEFAULT_TEXT_FONT_SIZE
                ),
                "font_color": _font_color(fonts),
                "bold": _bold(fonts),
                "align": _align(paragraph),
            }
        )

    return {
        "kind": "text",
        **geometry,
        # The block-level alignment the UI can apply before it reads the
        # paragraphs; the builder writes the same value on every one of them.
        "align": paragraphs[0]["align"] if paragraphs else _DEFAULT_ALIGN,
        "paragraphs": paragraphs,
    }


def _cell_payload(cell: Any) -> dict[str, Any]:
    """One table cell: its text, its fill and the font its runs really carry."""
    try:
        paragraphs = list(cell.text_frame.paragraphs)
    except Exception:  # noqa: BLE001
        paragraphs = []
    first = paragraphs[0] if paragraphs else None
    fonts = _fonts_of(first) if first is not None else []

    try:
        text = str(cell.text or "")
    except Exception:  # noqa: BLE001
        text = ""

    return {
        "text": text,
        "fill_color": _solid_fill(getattr(cell, "fill", None)),
        "font_size_pt": _font_size_pt(fonts, default=config.DEFAULT_TABLE_FONT_SIZE),
        "font_color": _font_color(fonts),
        "bold": _bold(fonts),
        "align": _align(first),
    }


def _table_payload(shape: Any, geometry: dict[str, float]) -> dict[str, Any]:
    """A native table: its track sizes and every cell, in row-major order."""
    table = shape.table

    col_widths = [int(column.width or 0) for column in table.columns]
    row_heights = [int(row.height or 0) for row in table.rows]

    rows: list[list[dict[str, Any]]] = []
    for row_index in range(len(row_heights)):
        cells: list[dict[str, Any]] = []
        for column_index in range(len(col_widths)):
            cells.append(_cell_payload(table.cell(row_index, column_index)))
        rows.append(cells)

    return {
        "kind": "table",
        **geometry,
        "col_widths_pct": _track_pcts(col_widths, getattr(shape, "width", 0)),
        "row_heights_pct": _track_pcts(row_heights, getattr(shape, "height", 0)),
        "rows": rows,
    }


def _shape_payload(shape: Any, geometry: dict[str, float]) -> dict[str, Any]:
    """An auto shape: its preset, its fill and its outline."""
    line = getattr(shape, "line", None)
    return {
        "kind": "shape",
        "autoshape": _autoshape(shape),
        **geometry,
        "fill_color": _solid_fill(getattr(shape, "fill", None)),
        "line_color": _line_color(line),
        "line_width_pt": _line_width_pt(line),
    }


def _connector_payload(
    shape: Any, slide_width: int, slide_height: int
) -> dict[str, Any]:
    """A connector: two points, its arrowheads and its stroke.

    Deliberately NO ``left/top/width/height``: a connector has no box in the
    schema either (``ConnectorElement`` carries ``start_point``/``end_point``
    and no ``bbox``), and the box python-pptx computes for it loses which end is
    which as soon as the line runs right-to-left. x scales by the slide's WIDTH
    and y by its HEIGHT, the same pairing as every other element.
    """
    line = getattr(shape, "line", None)
    line_element = _line_element(shape)
    return {
        "kind": "connector",
        "start_pct": {
            "x": _pct(_emu_attr(shape, "begin_x"), slide_width),
            "y": _pct(_emu_attr(shape, "begin_y"), slide_height),
        },
        "end_pct": {
            "x": _pct(_emu_attr(shape, "end_x"), slide_width),
            "y": _pct(_emu_attr(shape, "end_y"), slide_height),
        },
        "elbow": _prst_geom(shape) in _ELBOW_PRESETS,
        "arrow_start": _has_arrow(line_element, "a:headEnd"),
        "arrow_end": _has_arrow(line_element, "a:tailEnd"),
        "dash": _dash(line),
        "color": _line_color(line),
        "width_pt": _line_width_pt(line),
    }


def _image_payload(shape: Any, geometry: dict[str, float]) -> dict[str, Any]:
    """A picture: its box, the crop that was really embedded, its description."""
    return {
        "kind": "image",
        **geometry,
        "data_uri": _crop_data_uri(shape),
        "description": _picture_description(shape),
    }


def _one_shape(shape: Any, slide_width: int, slide_height: int) -> dict[str, Any]:
    """Dispatch one shape onto its payload, degrading rather than raising."""
    geometry = _geometry(shape, slide_width, slide_height)
    shape_type = _shape_type(shape)

    try:
        # Pictures and connectors are checked FIRST, because neither can be
        # previewed as anything else without lying about the slide: a picture
        # drawn as an empty box hides that a region was embedded as a bitmap,
        # and a connector drawn as its bounding box loses its direction.
        if shape_type in _PICTURE_TYPES:
            return _image_payload(shape, geometry)
        if shape_type in _CONNECTOR_TYPES:
            return _connector_payload(shape, slide_width, slide_height)
        if shape_type == MSO_SHAPE_TYPE.TABLE or getattr(shape, "has_table", False):
            return _table_payload(shape, geometry)
        if shape_type == MSO_SHAPE_TYPE.TEXT_BOX:
            return _text_payload(shape, geometry)
        if shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
            return _shape_payload(shape, geometry)
        # Nothing the builder emits reaches here. Fall back on what the shape
        # can actually do rather than on what it is called.
        if getattr(shape, "has_text_frame", False):
            return _text_payload(shape, geometry)
    except Exception:  # noqa: BLE001 - one odd shape may not lose the others
        return {"kind": "unsupported", **geometry}

    return {"kind": "unsupported", **geometry}


def _background_color(slide: Any) -> str | None:
    """The slide's explicit RGB background, or ``None`` when it is inherited.

    NOTE for callers: this read is not free of side effects. python-pptx
    materializes a ``<p:bg>`` element on a slide that had none, so a deck must
    be SAVED before its preview is derived - see ``converter.preview_for``.
    """
    try:
        return _solid_fill(slide.background.fill)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# the payload
# --------------------------------------------------------------------------- #
def build_preview(presentation: Any) -> dict[str, Any]:
    """Describe ``presentation``'s first slide as the pinned preview payload.

    Args:
        presentation: a built :class:`pptx.presentation.Presentation`, already
            saved (see :func:`_background_color`).

    Returns:
        ``{"slide": {...}, "background_color": ... , "shapes": [...]}`` with the
        shapes already in z-order, back to front.
    """
    slide_width = int(getattr(presentation, "slide_width", 0) or 0)
    slide_height = int(getattr(presentation, "slide_height", 0) or 0)

    slides = list(presentation.slides)
    slide = slides[0] if slides else None

    shapes: list[dict[str, Any]] = []
    if slide is not None:
        # Document order IS the z-order, so no sorting happens here - sorting
        # would be the bug.
        for shape in slide.shapes:
            shapes.append(_one_shape(shape, slide_width, slide_height))

    return {
        "slide": {
            "width_in": round(Emu(slide_width).inches, 3),
            "height_in": round(Emu(slide_height).inches, 3),
            "width_pt": round(Emu(slide_width).pt, _PT_DIGITS),
            "height_pt": round(Emu(slide_height).pt, _PT_DIGITS),
        },
        "background_color": _background_color(slide),
        "shapes": shapes,
    }
