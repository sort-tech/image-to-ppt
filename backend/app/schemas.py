"""Pydantic v2 data models for the image-to-pptx vision pipeline.

The Vision LLM returns JSON describing a slide; these models parse it into a
:class:`SlideSpec` that ``pptx_builder`` can draw 1:1 with python-pptx.

Design rules (see the project contract):

* Coordinates live in a normalized integer system, ``0..1000`` on BOTH axes.
* Parsing is *tolerant*: sloppy LLM output (bbox as a list, floats, numeric
  strings, out-of-range values, junk colours, ragged table rows, unknown extra
  keys) is coerced/ignored instead of blowing up the whole conversion.
* Nothing in this module reaches out to the network or imports heavy optional
  dependencies, so it stays importable in isolation.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

__all__ = [
    "Align",
    "BBox",
    "BaseElement",
    "COORD_MAX",
    "COORD_MIN",
    "ConnectorElement",
    "ConnectorKind",
    "DashStyle",
    "Element",
    "ElementsAllDroppedError",
    "ImageElement",
    "Point",
    "ShapeElement",
    "ShapeKind",
    "SlideSpec",
    "TableElement",
    "TextElement",
    "all_dropped_reasons",
    "llm_json_schema",
    "normalize_hex_color",
    "normalize_spec_payload",
    "validate_elements",
]

ShapeKind = Literal[
    "rectangle",
    "rounded_rectangle",
    "circle",
    # Block arrows: a thick, filled arrow BODY, not a line. A thin diagram
    # arrow joining two boxes is a ``connector`` instead.
    "arrow_right",
    "arrow_left",
    "arrow_up",
    "arrow_down",
]
ConnectorKind = Literal["straight", "elbow"]
DashStyle = Literal["solid", "dashed"]
Align = Literal["left", "center", "right"]

COORD_MIN = 0
COORD_MAX = 1000

# ---------------------------------------------------------------------------
# low level coercion helpers (never raise)
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_HEX_RE = re.compile(r"\A[0-9a-fA-F]+\Z")

_SENTINEL = object()


def _to_number(value: Any) -> float | None:
    """Best-effort conversion of *value* to a finite float, else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            num = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return num if math.isfinite(num) else None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8", "ignore")
        except Exception:  # pragma: no cover - defensive
            return None
    if isinstance(value, str):
        match = _NUMBER_RE.search(value.strip())
        if match is None:
            return None
        try:
            num = float(match.group(0))
        except (TypeError, ValueError, OverflowError):
            return None
        return num if math.isfinite(num) else None
    # Decimal, numpy scalars, anything with __float__ ...
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return num if math.isfinite(num) else None


def _round_half_up(num: float) -> int:
    return int(math.floor(num + 0.5))


def _clamp_int(value: Any, *, minimum: int, maximum: int, default: int) -> int:
    """Coerce *value* to an int clamped into ``[minimum, maximum]``."""
    num = _to_number(value)
    if num is None:
        return max(minimum, min(maximum, default))
    return max(minimum, min(maximum, _round_half_up(num)))


def _optional_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    num = _to_number(value)
    if num is None:
        return None
    return max(minimum, min(maximum, _round_half_up(num)))


def _optional_float(value: Any, *, minimum: float, maximum: float) -> float | None:
    num = _to_number(value)
    if num is None:
        return None
    return max(minimum, min(maximum, num))


# ---------------------------------------------------------------------------
# colours
# ---------------------------------------------------------------------------


def normalize_hex_color(value: Any) -> str | None:
    """Normalize a colour to ``#RRGGBB`` upper-case form.

    ``"#FFF"``/``"fff"``/``"FFFFFF"``/``"#ffffff"`` all become ``"#FFFFFF"``.
    8-digit ``#RRGGBBAA`` (and 4-digit ``#RGBA``) input drops the alpha channel.
    Anything unusable - ``None``, ``""``, ``"transparent"``, ``"red"``, a list,
    a number - returns ``None``. This function NEVER raises.
    """
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "ignore")
        if not isinstance(value, str):
            return None
        text = value.strip().replace("0x", "").replace("0X", "")
        if text.startswith("#"):
            text = text[1:]
        text = text.strip()
        if not text or not _HEX_RE.match(text):
            return None
        if len(text) in (3, 4):  # #RGB / #RGBA -> expand, drop alpha
            text = "".join(ch * 2 for ch in text[:3])
        elif len(text) in (6, 8):  # #RRGGBB / #RRGGBBAA -> drop alpha
            text = text[:6]
        else:
            return None
        return "#" + text.upper()
    except Exception:  # pragma: no cover - belt and braces, must never raise
        return None


def _colour_field(value: Any) -> str | None:
    return normalize_hex_color(value)


# ---------------------------------------------------------------------------
# bbox
# ---------------------------------------------------------------------------

_X_KEYS = ("x", "left", "x0", "l")
_Y_KEYS = ("y", "top", "y0", "t")
_W_KEYS = ("w", "width", "cx")
_H_KEYS = ("h", "height", "cy")

#: The four components a bbox must supply, in reporting order.
_BBOX_COMPONENTS = ("x", "y", "w", "h")


def _pick(mapping: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    # case-insensitive second pass
    lowered = {str(k).strip().lower(): v for k, v in mapping.items()}
    for key in keys:
        if key in lowered:
            return lowered[key]
    return _SENTINEL


class BBox(BaseModel):
    """Normalized bounding box in the 0..1000 integer coordinate system.

    All four of ``x``/``y``/``w``/``h`` are REQUIRED in the input. The field
    defaults below exist only so that a validated box always carries the four
    attributes; :meth:`_coerce` never lets an input reach them (see its
    docstring for why a missing component may not be defaulted away).
    """

    model_config = ConfigDict(extra="ignore")

    x: int = 0
    y: int = 0
    w: int = 1
    h: int = 1

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        """Accept ``[x, y, w, h]``, ``(x, y, w, h)`` or ``{"x":..., ...}``.

        Values that are PRESENT are parsed tolerantly: floats, numeric strings
        and out-of-range numbers are rounded and clamped to ``0..1000``, with
        ``w``/``h`` floored at 1.

        A component that is ABSENT or unparseable is a different thing entirely
        and must not be defaulted. Defaulting an absent ``w``/``h`` to 1 made the
        element validate "successfully" and render as a 0.05in sliver the user
        cannot see, while the CLI reported success with an empty stderr - the one
        case where a broken deck was handed over as a good one. So an unreadable
        component raises, which makes the *element* invalid; the per-element
        isolation in :func:`validate_elements` then drops exactly that element
        and names the component in its reason.

        Raises:
            PydanticCustomError: when any of x/y/w/h is missing or unparseable.
                Surfaces to callers as an ordinary ``ValidationError`` at
                ``loc=("bbox",)``.
        """
        if isinstance(data, BBox):
            return data

        raw_x: Any = _SENTINEL
        raw_y: Any = _SENTINEL
        raw_w: Any = _SENTINEL
        raw_h: Any = _SENTINEL

        if isinstance(data, (list, tuple)):
            items = list(data)[:4]
            while len(items) < 4:
                items.append(_SENTINEL)
            raw_x, raw_y, raw_w, raw_h = items
        elif isinstance(data, dict):
            raw_x = _pick(data, _X_KEYS)
            raw_y = _pick(data, _Y_KEYS)
            raw_w = _pick(data, _W_KEYS)
            raw_h = _pick(data, _H_KEYS)
            # tolerate x1/y1 (or right/bottom) style corners
            if raw_w is _SENTINEL:
                far = _pick(data, ("x1", "right", "r"))
                left = _to_number(raw_x)
                far_num = _to_number(far)
                if far_num is not None and left is not None:
                    raw_w = far_num - left
            if raw_h is _SENTINEL:
                far = _pick(data, ("y1", "bottom", "b"))
                top = _to_number(raw_y)
                far_num = _to_number(far)
                if far_num is not None and top is not None:
                    raw_h = far_num - top
        else:
            # Not a recognizable container - let pydantic report a clean error.
            return data

        unreadable = [
            name
            for name, raw in zip(_BBOX_COMPONENTS, (raw_x, raw_y, raw_w, raw_h))
            if raw is _SENTINEL or _to_number(raw) is None
        ]
        if unreadable:
            quoted = ", ".join(repr(name) for name in unreadable)
            raise PydanticCustomError(
                "bbox_incomplete",
                "{quoted} {verb} missing or unreadable",
                {
                    "quoted": quoted,
                    "verb": "is" if len(unreadable) == 1 else "are",
                },
            )

        return {
            "x": _clamp_int(raw_x, minimum=COORD_MIN, maximum=COORD_MAX, default=0),
            "y": _clamp_int(raw_y, minimum=COORD_MIN, maximum=COORD_MAX, default=0),
            "w": _clamp_int(raw_w, minimum=1, maximum=COORD_MAX, default=1),
            "h": _clamp_int(raw_h, minimum=1, maximum=COORD_MAX, default=1),
        }

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)


# ---------------------------------------------------------------------------
# point (connector endpoints)
# ---------------------------------------------------------------------------

#: The two components a point must supply, in reporting order.
_POINT_COMPONENTS = ("x", "y")


class Point(BaseModel):
    """One normalized ``(x, y)`` position in the same 0..1000 system as :class:`BBox`.

    A connector has no bbox - it IS its two endpoints - so this is the geometry
    a :class:`ConnectorElement` carries instead.

    Tolerance and strictness mirror :class:`BBox` exactly, for the same reason:
    a value that is PRESENT is coerced (floats, numeric strings, out-of-range
    numbers are rounded and clamped to ``0..1000``), while a component that is
    ABSENT or unparseable may NOT be defaulted. Defaulting a missing ``y`` to 0
    would draw a connector to the top-left corner of the slide and report
    success, which is the silent-wrong-deck failure this project refuses to
    ship. An unreadable component therefore raises, the *element* becomes
    invalid, and :func:`validate_elements` drops exactly that element naming the
    component, e.g. ``"element 3 (connector): start_point - 'y' is missing or
    unreadable"``.
    """

    model_config = ConfigDict(extra="ignore")

    # As on BBox, these defaults only guarantee that a VALIDATED point carries
    # both attributes; `_coerce` never lets an incomplete input reach them.
    x: int = 0
    y: int = 0

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        """Accept ``[x, y]``, ``(x, y)`` or ``{"x": ..., "y": ...}``.

        Raises:
            PydanticCustomError: when x or y is missing or unparseable.
        """
        if isinstance(data, Point):
            return data

        raw_x: Any = _SENTINEL
        raw_y: Any = _SENTINEL

        if isinstance(data, (list, tuple)):
            items = list(data)[:2]
            while len(items) < 2:
                items.append(_SENTINEL)
            raw_x, raw_y = items
        elif isinstance(data, dict):
            raw_x = _pick(data, _X_KEYS)
            raw_y = _pick(data, _Y_KEYS)
        else:
            # Not a recognizable container - let pydantic report a clean error.
            return data

        unreadable = [
            name
            for name, raw in zip(_POINT_COMPONENTS, (raw_x, raw_y))
            if raw is _SENTINEL or _to_number(raw) is None
        ]
        if unreadable:
            quoted = ", ".join(repr(name) for name in unreadable)
            raise PydanticCustomError(
                "point_incomplete",
                "{quoted} {verb} missing or unreadable",
                {
                    "quoted": quoted,
                    "verb": "is" if len(unreadable) == 1 else "are",
                },
            )

        return {
            "x": _clamp_int(raw_x, minimum=COORD_MIN, maximum=COORD_MAX, default=0),
            "y": _clamp_int(raw_y, minimum=COORD_MIN, maximum=COORD_MAX, default=0),
        }

    def as_tuple(self) -> tuple[int, int]:
        return (self.x, self.y)


# ---------------------------------------------------------------------------
# elements
# ---------------------------------------------------------------------------

_KIND_ALIASES = {
    "rectangle": "rectangle",
    "rect": "rectangle",
    "square": "rectangle",
    "box": "rectangle",
    "rounded_rectangle": "rounded_rectangle",
    "rounded-rectangle": "rounded_rectangle",
    "roundedrectangle": "rounded_rectangle",
    "rounded_rect": "rounded_rectangle",
    "rounded": "rounded_rectangle",
    "round_rect": "rounded_rectangle",
    "circle": "circle",
    "oval": "circle",
    "ellipse": "circle",
    "round": "circle",
    # Block arrows. "arrow" with no direction is the one ambiguous spelling;
    # it maps to the overwhelmingly common right-pointing arrow, because
    # degrading it to a rectangle would draw a BOX where an arrow belongs.
    "arrow": "arrow_right",
    "block_arrow": "arrow_right",
    "arrow_right": "arrow_right",
    "arrow-right": "arrow_right",
    "arrowright": "arrow_right",
    "right_arrow": "arrow_right",
    "right-arrow": "arrow_right",
    "rightarrow": "arrow_right",
    "block_arrow_right": "arrow_right",
    "arrow_left": "arrow_left",
    "arrow-left": "arrow_left",
    "arrowleft": "arrow_left",
    "left_arrow": "arrow_left",
    "left-arrow": "arrow_left",
    "leftarrow": "arrow_left",
    "block_arrow_left": "arrow_left",
    "arrow_up": "arrow_up",
    "arrow-up": "arrow_up",
    "arrowup": "arrow_up",
    "up_arrow": "arrow_up",
    "up-arrow": "arrow_up",
    "uparrow": "arrow_up",
    "block_arrow_up": "arrow_up",
    "arrow_down": "arrow_down",
    "arrow-down": "arrow_down",
    "arrowdown": "arrow_down",
    "down_arrow": "arrow_down",
    "down-arrow": "arrow_down",
    "downarrow": "arrow_down",
    "block_arrow_down": "arrow_down",
}

_CONNECTOR_KIND_ALIASES = {
    "straight": "straight",
    "line": "straight",
    "direct": "straight",
    "elbow": "elbow",
    "bent": "elbow",
    "bend": "elbow",
    "angle": "elbow",
    "angled": "elbow",
    "orthogonal": "elbow",
    "right_angle": "elbow",
    "right-angle": "elbow",
}

_DASH_STYLE_ALIASES = {
    "solid": "solid",
    "none": "solid",
    "plain": "solid",
    "continuous": "solid",
    "dashed": "dashed",
    "dash": "dashed",
    "dashdot": "dashed",
    "dash_dot": "dashed",
    "dotted": "dashed",
    "dot": "dashed",
    "long_dash": "dashed",
    "longdash": "dashed",
}

_ALIGN_ALIASES = {
    "left": "left",
    "start": "left",
    "l": "left",
    "center": "center",
    "centre": "center",
    "middle": "center",
    "c": "center",
    "right": "right",
    "end": "right",
    "r": "right",
    "justify": "left",
    "justified": "left",
}

_TRUE_WORDS = {"true", "t", "yes", "y", "1", "on", "bold"}
_FALSE_WORDS = {"false", "f", "no", "n", "0", "off", "none", "normal", "regular", ""}


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
        return default
    return default


class BaseElement(BaseModel):
    """Fields shared by every BOX-shaped slide element.

    NOT every element: :class:`ConnectorElement` deliberately does not inherit
    from this, because a connector has no bounding box at all - it is defined by
    two endpoints. Anything iterating :attr:`SlideSpec.elements` must therefore
    not read ``element.bbox`` unconditionally.
    """

    model_config = ConfigDict(extra="ignore")

    bbox: BBox


class ShapeElement(BaseElement):
    """A native autoshape (rectangle / rounded rectangle / oval)."""

    type: Literal["shape"]
    kind: ShapeKind = "rectangle"
    fill_color: str | None = None
    border_color: str | None = None
    border_width: float | None = None

    _norm_colors = field_validator("fill_color", "border_color", mode="before")(
        _colour_field
    )

    @field_validator("kind", mode="before")
    @classmethod
    def _norm_kind(cls, value: Any) -> str:
        if value is None:
            return "rectangle"
        text = str(value).strip().lower().replace(" ", "_")
        return _KIND_ALIASES.get(text, "rectangle")

    @field_validator("border_width", mode="before")
    @classmethod
    def _norm_border_width(cls, value: Any) -> float | None:
        return _optional_float(value, minimum=0.0, maximum=100.0)


class TableElement(BaseElement):
    """A native PowerPoint table; ``data`` is row-major, first row = header."""

    type: Literal["table"]
    data: list[list[str]]
    header_bg_color: str | None = None
    header_font_bold: bool = True
    font_size: int | None = None

    _norm_colors = field_validator("header_bg_color", mode="before")(_colour_field)

    @field_validator("data", mode="before")
    @classmethod
    def _norm_data(cls, value: Any) -> list[list[str]]:
        """Pad ragged rows with ``""`` and guarantee at least a 1x1 table."""
        rows: list[list[str]] = []
        if isinstance(value, (list, tuple)):
            candidates: list[Any] = list(value)
        elif value is None:
            candidates = []
        elif isinstance(value, str):
            candidates = [[value]]
        else:
            candidates = [[value]]

        for raw_row in candidates:
            if isinstance(raw_row, (list, tuple)):
                cells = [_cell_text(cell) for cell in raw_row]
            elif raw_row is None:
                cells = []
            else:
                cells = [_cell_text(raw_row)]
            rows.append(cells)

        width = max((len(row) for row in rows), default=0)
        if width == 0:
            # fully-empty data: drop it, but stay valid with a minimal 1x1 table
            return [[""]]
        return [row + [""] * (width - len(row)) for row in rows]

    @field_validator("font_size", mode="before")
    @classmethod
    def _norm_font_size(cls, value: Any) -> int | None:
        return _optional_int(value, minimum=1, maximum=400)

    @field_validator("header_font_bold", mode="before")
    @classmethod
    def _norm_header_bold(cls, value: Any) -> bool:
        return _coerce_bool(value, True)

    @property
    def rows(self) -> int:
        return len(self.data)

    @property
    def cols(self) -> int:
        return len(self.data[0]) if self.data else 0


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple)):
        return " ".join(_cell_text(item) for item in value).strip()
    try:
        return str(value)
    except Exception:  # pragma: no cover - defensive
        return ""


class TextElement(BaseElement):
    """A native text box. ``content`` may contain ``\\n`` for multiple lines.

    ``content`` is REQUIRED - exactly as :func:`llm_json_schema` advertises it,
    and exactly like a table's ``data``. It used to default to ``""``, so a text
    element the model described without any text validated "successfully" and
    rendered as an invisible, empty text box: a visually blank slide handed over
    as a success with an empty stderr. An ABSENT or ``null`` ``content`` is
    therefore invalid and the element is dropped with a reason.

    An explicitly supplied ``""`` is still accepted: an intentionally empty text
    box is a legitimate editable object.
    """

    type: Literal["text"]
    content: str
    font_size: int | None = None
    font_color: str | None = None
    bold: bool = False
    align: Align = "left"

    _norm_colors = field_validator("font_color", mode="before")(_colour_field)

    @field_validator("content", mode="before")
    @classmethod
    def _norm_content(cls, value: Any) -> str:
        if value is None:
            # Not coerced to "": see the class docstring. `"content": null` is
            # the same defect as a missing key, and must be reported as one.
            raise PydanticCustomError(
                "content_missing",
                'must not be null - a text box with no content renders invisible; '
                'use "" for an intentionally empty text box',
            )
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return "\n".join(_cell_text(item) for item in value)
        return _cell_text(value)

    @field_validator("font_size", mode="before")
    @classmethod
    def _norm_font_size(cls, value: Any) -> int | None:
        return _optional_int(value, minimum=1, maximum=400)

    @field_validator("bold", mode="before")
    @classmethod
    def _norm_bold(cls, value: Any) -> bool:
        return _coerce_bool(value, False)

    @field_validator("align", mode="before")
    @classmethod
    def _norm_align(cls, value: Any) -> str:
        if value is None:
            return "left"
        text = str(value).strip().lower()
        return _ALIGN_ALIASES.get(text, "left")


class ConnectorElement(BaseModel):
    """A diagram connector: a straight or elbowed line joining two places.

    Deliberately NOT a :class:`BaseElement`: a connector carries no ``bbox``,
    its geometry is the pair of :class:`Point` endpoints. Arrowheads are opt-in
    per end (``arrow_end`` defaults to True, the one-way diagram arrow).

    A thick, filled BLOCK arrow is not this: that is a ``shape`` with
    ``kind="arrow_right"`` and friends.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["connector"]
    kind: ConnectorKind = "straight"
    start_point: Point
    end_point: Point
    arrow_start: bool = False
    arrow_end: bool = True
    dash_style: DashStyle = "solid"
    color: str | None = None
    width_pt: float | None = None

    _norm_colors = field_validator("color", mode="before")(_colour_field)

    @field_validator("kind", mode="before")
    @classmethod
    def _norm_kind(cls, value: Any) -> str:
        """Unknown/absent -> ``"straight"``: a plain line is the safe default.

        Degrading rather than raising, exactly like ``ShapeElement.kind``: the
        connector still joins the right two points, which is the information
        that matters, so losing the element over its routing style would be a
        strictly worse deck.
        """
        if value is None:
            return "straight"
        text = str(value).strip().lower().replace(" ", "_")
        return _CONNECTOR_KIND_ALIASES.get(text, "straight")

    @field_validator("dash_style", mode="before")
    @classmethod
    def _norm_dash_style(cls, value: Any) -> str:
        if value is None:
            return "solid"
        text = str(value).strip().lower().replace(" ", "_")
        return _DASH_STYLE_ALIASES.get(text, "solid")

    @field_validator("arrow_start", mode="before")
    @classmethod
    def _norm_arrow_start(cls, value: Any) -> bool:
        return _coerce_bool(value, False)

    @field_validator("arrow_end", mode="before")
    @classmethod
    def _norm_arrow_end(cls, value: Any) -> bool:
        return _coerce_bool(value, True)

    @field_validator("width_pt", mode="before")
    @classmethod
    def _norm_width_pt(cls, value: Any) -> float | None:
        """``None`` means "unspecified"; the builder falls back to the config
        default (``config.DEFAULT_BORDER_WIDTH_PT``) rather than guessing here,
        so this module stays free of config imports."""
        return _optional_float(value, minimum=0.0, maximum=100.0)


class ImageElement(BaseElement):
    """A region of the SOURCE image that cannot be drawn as a native object.

    A photo, a rendered chart, a logo, a complex illustration: the backend crops
    :attr:`bbox` out of the source image and places the crop there. It is the
    escape hatch for the genuinely un-transcribable, NOT a way to flatten a
    slide into a picture - see the anti-flattening rules in ``pptx_builder``.

    ``description`` is a short human label (used by the preview), never a
    transcription. A blank description normalizes to ``None`` so nothing has to
    render an empty caption.
    """

    type: Literal["image"]
    description: str | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _norm_description(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = (value if isinstance(value, str) else _cell_text(value)).strip()
        return text or None


Element = Annotated[
    Union[
        ShapeElement,
        TableElement,
        TextElement,
        ConnectorElement,
        ImageElement,
    ],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# per-element validation: one bad element must not sink the whole slide
# ---------------------------------------------------------------------------
#: Validates ONE element of the discriminated union, in isolation.
_ELEMENT_ADAPTER: TypeAdapter = TypeAdapter(Element)

#: Cap on the pydantic messages quoted per dropped element, so a single very
#: wrong element cannot produce a screenful of reasons.
_MAX_REASONS_PER_ELEMENT = 3


class ElementsAllDroppedError(ValueError):
    """Every element in the payload failed validation - nothing is drawable.

    Deliberately a ``ValueError``: raised from inside a pydantic validator it
    surfaces to callers as an ordinary ``ValidationError`` (so anything that
    already catches ``ValidationError`` keeps working), while
    :func:`all_dropped_reasons` can still dig the per-element reasons back out
    of it - pydantic keeps the original exception in
    ``ValidationError.errors()[i]["ctx"]["error"]``.
    """

    def __init__(self, reasons: Any = ()) -> None:
        self.reasons: list[str] = [str(reason) for reason in (reasons or ())]
        detail = "; ".join(self.reasons) if self.reasons else "no usable element"
        super().__init__(
            f"every element failed validation ({len(self.reasons)} dropped): {detail}"
        )


def _declared_type(item: Any) -> str:
    """The element's self-declared ``type``, for a human-readable reason."""
    raw = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        return text if len(text) <= 24 else text[:21] + "..."
    if isinstance(item, dict):
        return "no type"
    return type(item).__name__


def _error_text(error: dict, tag: str) -> str:
    """One pydantic error as ``"field.path - message"`` (or just the message)."""
    loc = tuple(error.get("loc") or ())
    if loc and str(loc[0]) == tag:
        loc = loc[1:]  # strip the discriminated-union tag ("table", "text", ...)
    where = ".".join(str(part) for part in loc)
    message = str(error.get("msg") or "").strip() or "invalid"
    return f"{where} - {message}" if where else message


def validate_elements(raw: Any) -> tuple[list[Element], list[str]]:
    """Validate *raw* elements ONE AT A TIME, dropping only the broken ones.

    ``list[Element]`` validation is all-or-nothing: a single element missing a
    required field (vision models do this regularly) fails the whole
    :class:`SlideSpec` and throws away every good element with it. Validating
    each entry on its own turns that total loss into a partial one.

    Args:
        raw: the ``elements`` array. ``None`` means "nothing to do"; a single
            element mapping is accepted as a one-item list.

    Returns:
        ``(kept, reasons)`` - the elements that validated, in input order, plus
        one human-readable reason per dropped entry, e.g.
        ``"element 1 (table): data - Field required"``.

    This function never raises; the caller decides what a drop means (see
    :class:`ElementsAllDroppedError` for the all-dropped case).
    """
    if raw is None:
        return [], []
    items = list(raw) if isinstance(raw, (list, tuple)) else [raw]

    kept: list[Element] = []
    reasons: list[str] = []
    for index, item in enumerate(items):
        try:
            kept.append(_ELEMENT_ADAPTER.validate_python(item))
            continue
        except ValidationError as exc:
            errors = exc.errors()
            tag = _declared_type(item)
            quoted = [_error_text(error, tag) for error in errors[:_MAX_REASONS_PER_ELEMENT]]
            hidden = len(errors) - len(quoted)
            if hidden > 0:
                quoted.append(f"(+{hidden} more)")
            detail = "; ".join(part for part in quoted if part) or "invalid element"
        except Exception as exc:  # noqa: BLE001 - one entry may never break the parse
            tag = _declared_type(item)
            detail = f"{exc.__class__.__name__}: {exc}"
        reasons.append(f"element {index} ({tag}): {detail}")
    return kept, reasons


def all_dropped_reasons(exc: BaseException | None) -> list[str] | None:
    """The per-element reasons behind an "every element was dropped" failure.

    Accepts the :class:`ElementsAllDroppedError` itself, the pydantic
    ``ValidationError`` it becomes when raised inside a validator, or anything
    wrapping either of those, and walks ``__cause__``/``__context__`` so a
    re-raised failure is still recognized. Returns ``None`` for an unrelated
    failure, which lets callers tell "nothing was usable" (worth its own error
    message) from "the payload was not a spec at all". Never raises.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ElementsAllDroppedError):
            return list(current.reasons)
        if isinstance(current, ValidationError):
            try:
                for error in current.errors():
                    inner = (error.get("ctx") or {}).get("error")
                    if isinstance(inner, ElementsAllDroppedError):
                        return list(inner.reasons)
            except Exception:  # pragma: no cover - diagnostics must never raise
                pass
        current = current.__cause__ or current.__context__
    return None


class SlideSpec(BaseModel):
    """One slide's worth of native, editable PowerPoint objects.

    ``elements`` order IS the z-order: background/container shapes first,
    foreground text and tables last so they paint on top.

    Elements are validated individually (see :func:`validate_elements`): a
    single malformed element is dropped, with its reason recorded in
    :attr:`dropped`, instead of failing the whole slide. If EVERY element fails,
    that is not a partial success but a blank deck, so it raises
    :class:`ElementsAllDroppedError` (as a ``ValidationError``) instead.
    """

    model_config = ConfigDict(extra="ignore")

    background_color: str | None = None
    elements: list[Element] = Field(default_factory=list)
    #: One human-readable reason per element dropped while parsing. Parse
    #: diagnostics, not slide content: ``exclude=True`` keeps it out of
    #: ``model_dump()``/``model_dump_json()``, so ``--save-json`` still writes a
    #: clean spec that ``--from-json`` can read straight back. It is also
    #: OUTPUT-ONLY - ``_isolate_unusable_elements`` strips any ``dropped`` key
    #: found in the payload, so it never echoes something the input said.
    dropped: list[str] = Field(default_factory=list, exclude=True)

    _norm_colors = field_validator("background_color", mode="before")(_colour_field)

    @model_validator(mode="before")
    @classmethod
    def _isolate_unusable_elements(cls, data: Any) -> Any:
        """Drop individually-invalid elements before the list is validated.

        Also strips any incoming ``dropped`` key, UNCONDITIONALLY and on every
        path out of here. ``dropped`` is tool-generated parse state, never input:
        left writable, a spec file - or the model's own reply - could hand us a
        ``dropped`` list that the CLI then printed as if it were this parser's
        own diagnostic, inventing losses that never happened. It can only ever
        hold reasons produced below.

        A non-list ``elements`` value is otherwise left alone so pydantic still
        reports it exactly as before ("Input should be a valid list").
        """
        if not isinstance(data, dict):
            return data
        if "dropped" in data:
            data = {key: value for key, value in data.items() if key != "dropped"}
        raw = data.get("elements")
        if not isinstance(raw, (list, tuple)):
            return data

        kept, reasons = validate_elements(raw)
        if raw and not kept:
            # Returning an empty spec here would build a silently BLANK deck.
            raise ElementsAllDroppedError(reasons)
        return {**data, "elements": kept, "dropped": reasons}

    @field_validator("elements", mode="before")
    @classmethod
    def _norm_elements(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, tuple):
            return list(value)
        return value

    def counts_by_type(self) -> dict[str, int]:
        """``{"shape": 3, "connector": 2, "text": 4}`` - handy for CLI summaries.

        Keyed on the element's own ``type`` tag, so a bbox-less element
        (``connector``) counts exactly like any other and a future type needs no
        change here. Types absent from the slide are absent from the mapping.
        """
        counts: dict[str, int] = {}
        for element in self.elements:
            counts[element.type] = counts.get(element.type, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# payload shape recovery (envelope keys / bare element arrays)
# ---------------------------------------------------------------------------

_ELEMENT_TYPE_NAMES = frozenset({"shape", "table", "text", "connector", "image"})
_SPEC_KEYS = ("elements", "background_color")

#: Keys only a connector ever carries. Used to recognize a connector that is
#: missing its ``type`` AND one of its endpoints - which must still be seen as
#: an ELEMENT so it can be dropped with a reason.
_CONNECTOR_ONLY_KEYS = ("arrow_start", "arrow_end", "dash_style")


def _looks_like_spec(value: Any) -> bool:
    """True when *value* is a mapping carrying a top-level SlideSpec key."""
    return isinstance(value, dict) and any(key in value for key in _SPEC_KEYS)


def _looks_like_connector(value: dict) -> bool:
    """True when *value* is plausibly a connector, which carries NO ``bbox``.

    The bbox rule in :func:`_looks_like_element` structurally cannot recognize a
    connector, so without this a bare top-level array of connectors whose
    ``type`` the model spelled wrong (or omitted) would be unrecoverable: the
    whole array would fail as a non-spec and every element - plus every
    per-element reason - would be lost.

    Both endpoints present is the clear case. ONE endpoint plus a
    connector-only attribute is the half-broken case, and it must still be
    recognized as an element: :func:`validate_elements` then drops it with a
    reason naming the missing endpoint, which is the whole point.
    """
    endpoints = [key for key in ("start_point", "end_point") if key in value]
    if len(endpoints) == 2:
        return True
    return bool(endpoints) and any(key in value for key in _CONNECTOR_ONLY_KEYS)


def _looks_like_element(value: Any) -> bool:
    """True when *value* is plausibly ONE element rather than a whole spec."""
    if not isinstance(value, dict):
        return False
    kind = value.get("type")
    if isinstance(kind, str) and kind.strip().lower() in _ELEMENT_TYPE_NAMES:
        return True
    # No usable "type", but the element-only keys are there.
    if "bbox" in value and any(k in value for k in ("content", "data", "kind")):
        return True
    return _looks_like_connector(value)


def _element_list(value: Any, *, permissive: bool = True) -> list | None:
    """*value* as a list of element mappings, or ``None`` if it is not one.

    With ``permissive=True`` (the default) recognition is ``any``, not ``all``:
    this runs BEFORE the per-element isolation, so requiring every entry to look
    like an element meant one unrecognized entry (an unknown ``type`` - probably
    the most common model mistake) made the whole bare-array recovery fail.
    :class:`SlideSpec` then got a bare ``list``, died with ``model_type``, and
    the user lost every good element plus every per-element reason. One
    element-looking entry is enough to call this an element array;
    :func:`validate_elements` drops the rest WITH reasons.

    That permissiveness is only safe where the list IS the whole payload, i.e. a
    bare top-level array: there is no sibling key that could hold the real spec.
    Applied during the envelope-key descent it let any junk list carrying one
    element-shaped dict (``{"notes": [1, 2, {...}], "slide": {...}}``) be
    accepted as THE element array, so the sibling holding the actual spec was
    never examined and its elements vanished silently. Hence the caller in
    :func:`normalize_spec_payload` keeps the permissive rule for ``_depth == 0``
    and only falls back to a nested list once no nested spec was found at all.

    A list with no element-looking entry at all (``[1, 2, 3]``) is never an
    element array and returns ``None``, so it is reported as a bad spec.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None
    items = list(value)
    enough = any if permissive else all
    if enough(_looks_like_element(item) for item in items):
        return items
    return None


#: How many wrapper keys deep the envelope descent looks for the real spec.
_MAX_ENVELOPE_DEPTH = 3


def _nested_spec(data: Any, _depth: int = 0) -> dict | None:
    """The spec-shaped mapping hidden under wrapper key(s), or ``None``.

    Only dict values are followed, exactly as the previous descent did: a spec
    buried inside a *list* was never recovered, and nothing here changes that.
    """
    if _looks_like_spec(data):
        return data
    if isinstance(data, dict) and _depth < _MAX_ENVELOPE_DEPTH:
        for value in data.values():
            if isinstance(value, dict):
                found = _nested_spec(value, _depth + 1)
                if found is not None:
                    return found
    return None


def _nested_element_list(
    data: Any, _depth: int = 0, *, permissive: bool = True
) -> dict | None:
    """``{"elements": [...]}`` recovered from a list under a wrapper key.

    The FALLBACK half of the descent: it runs only after :func:`_nested_spec`
    found no real spec anywhere, so a junk sibling list can no longer outrank
    the key that holds the slide.

    ``permissive`` selects the :func:`_element_list` rule. The caller runs the
    STRICT pass (every entry element-shaped) across the whole payload before the
    permissive one, because sibling keys compete: in ``{"notes": [1, 2,
    {element}], "result": [element, element]}`` both lists are candidates, and
    dict order alone would hand it to ``notes``. Strict-first makes the clean,
    fully element-shaped array win regardless of key order.
    """
    if not isinstance(data, dict) or _depth >= _MAX_ENVELOPE_DEPTH:
        return None
    for value in data.values():
        items = _element_list(value, permissive=permissive)
        if items is not None:
            return {"elements": items}
    for value in data.values():
        if isinstance(value, dict):
            found = _nested_element_list(value, _depth + 1, permissive=permissive)
            if found is not None:
                return found
    return None


def normalize_spec_payload(data: Any, _depth: int = 0) -> Any:
    """Coax a loosely shaped payload into :class:`SlideSpec` form.

    Vision models (and hand-written spec files) do not always hand back the
    exact ``{"background_color": ..., "elements": [...]}`` object:

    * a bare array of elements - ``[{...}, {...}]`` -> ``{"elements": [...]}``
    * the spec hidden under a wrapper key - ``{"slide": {...}}`` -> the inner
      object (also ``{"result": [element, ...]}``)

    Without this, ``extra="ignore"`` plus ``elements=[]`` would happily validate
    such a payload into an EMPTY spec, i.e. a silently blank deck. Anything that
    already looks like a spec, or that cannot be recognized at all, is returned
    unchanged. This function never raises.

    Recovery order matters, because a payload can offer more than one candidate:

    1. it already looks like a spec -> returned untouched;
    2. a BARE top-level array (``_depth == 0``) -> ``{"elements": [...]}``, with
       the permissive one-entry-is-enough rule (see :func:`_element_list`);
    3. a real spec object under a wrapper key, however deep - this runs BEFORE
       any sibling list is considered, so ``{"notes": [1, 2, {element}],
       "slide": {...}}`` returns the SLIDE and not the stray note;
    4. only if no nested spec exists anywhere: a nested element array
       (``{"result": [element, ...]}``), trying the STRICT rule across the whole
       payload before the permissive one so that a clean element array beats a
       junk sibling list that merely contains one element-shaped dict.
    """
    try:
        if _looks_like_spec(data):
            return data

        if _depth == 0:
            elements = _element_list(data)
            if elements is not None:
                return {"elements": elements}

        if isinstance(data, dict):
            found = _nested_spec(data, _depth)
            if found is not None:
                return found
            for permissive in (False, True):
                recovered = _nested_element_list(
                    data, _depth, permissive=permissive
                )
                if recovered is not None:
                    return recovered
        return data
    except Exception:  # pragma: no cover - must never break a parse
        return data


# ---------------------------------------------------------------------------
# schema description handed to the LLM
# ---------------------------------------------------------------------------


def llm_json_schema() -> dict:
    """Compact, JSON-serializable description of the expected LLM output."""
    bbox_desc = (
        "[x, y, w, h] as integers 0..1000; x/w are relative to image width, "
        "y/h relative to image height"
    )
    point_desc = (
        "[x, y] as integers 0..1000; x relative to image width, y relative to "
        "image height"
    )
    return {
        "type": "object",
        "description": (
            "One PowerPoint slide described as native editable objects. "
            "Return PURE JSON, no markdown and no commentary."
        ),
        "coordinate_system": (
            "Integers 0..1000 on BOTH axes (0,0 = top-left, 1000,1000 = "
            "bottom-right). bbox is always [x, y, w, h]. A connector has NO "
            "bbox: it carries start_point and end_point, each [x, y] on the "
            "same grid."
        ),
        "z_order": (
            "The 'elements' array is painted in order: put background and "
            "container shapes EARLY, foreground tables and text LATE."
        ),
        "properties": {
            "background_color": {
                "type": ["string", "null"],
                "description": "slide background as #RRGGBB hex, or null",
            },
            "elements": {
                "type": "array",
                "description": "ordered back-to-front (z-order)",
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "title": "shape",
                            "required": ["type", "bbox"],
                            "properties": {
                                "type": {"const": "shape"},
                                "bbox": {"type": "array", "description": bbox_desc},
                                "kind": {
                                    "enum": [
                                        "rectangle",
                                        "rounded_rectangle",
                                        "circle",
                                        "arrow_right",
                                        "arrow_left",
                                        "arrow_up",
                                        "arrow_down",
                                    ],
                                    "default": "rectangle",
                                    "description": (
                                        "arrow_* are BLOCK arrows (a thick "
                                        "filled arrow body); a thin arrow "
                                        "joining two boxes is a connector"
                                    ),
                                },
                                "fill_color": {
                                    "type": ["string", "null"],
                                    "description": "#RRGGBB hex, or null for no fill",
                                },
                                "border_color": {
                                    "type": ["string", "null"],
                                    "description": "#RRGGBB hex, or null for no border",
                                },
                                "border_width": {
                                    "type": ["number", "null"],
                                    "description": "border width in points",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "title": "table",
                            "required": ["type", "bbox", "data"],
                            "properties": {
                                "type": {"const": "table"},
                                "bbox": {"type": "array", "description": bbox_desc},
                                "data": {
                                    "type": "array",
                                    "description": (
                                        "2-D array of strings, row-major, "
                                        "first row = header"
                                    ),
                                    "items": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "header_bg_color": {
                                    "type": ["string", "null"],
                                    "description": "#RRGGBB hex for the header row",
                                },
                                "header_font_bold": {
                                    "type": "boolean",
                                    "default": True,
                                },
                                "font_size": {
                                    "type": ["integer", "null"],
                                    "description": "points; keep small (8-12)",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "title": "text",
                            "required": ["type", "bbox", "content"],
                            "properties": {
                                "type": {"const": "text"},
                                "bbox": {"type": "array", "description": bbox_desc},
                                "content": {
                                    "type": "string",
                                    "description": (
                                        "verbatim visible text; use \\n for "
                                        "line breaks"
                                    ),
                                },
                                "font_size": {
                                    "type": ["integer", "null"],
                                    "description": "points",
                                },
                                "font_color": {
                                    "type": ["string", "null"],
                                    "description": "#RRGGBB hex",
                                },
                                "bold": {"type": "boolean", "default": False},
                                "align": {
                                    "enum": ["left", "center", "right"],
                                    "default": "left",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "title": "connector",
                            "description": (
                                "a line or arrow JOINING two things; it has no "
                                "bbox, only two endpoints"
                            ),
                            "required": ["type", "start_point", "end_point"],
                            "properties": {
                                "type": {"const": "connector"},
                                "start_point": {
                                    "type": "array",
                                    "description": point_desc,
                                },
                                "end_point": {
                                    "type": "array",
                                    "description": point_desc,
                                },
                                "kind": {
                                    "enum": ["straight", "elbow"],
                                    "default": "straight",
                                    "description": (
                                        "'elbow' for a line that turns a right "
                                        "angle on its way"
                                    ),
                                },
                                "arrow_start": {
                                    "type": "boolean",
                                    "default": False,
                                    "description": "arrowhead at start_point",
                                },
                                "arrow_end": {
                                    "type": "boolean",
                                    "default": True,
                                    "description": "arrowhead at end_point",
                                },
                                "dash_style": {
                                    "enum": ["solid", "dashed"],
                                    "default": "solid",
                                },
                                "color": {
                                    "type": ["string", "null"],
                                    "description": "#RRGGBB hex for the line",
                                },
                                "width_pt": {
                                    "type": ["number", "null"],
                                    "description": "line width in points",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "title": "image",
                            "description": (
                                "ONLY a region that cannot be drawn as shape, "
                                "table or text - a photo, a rendered chart, a "
                                "logo, a complex illustration. That region is "
                                "cropped out of the source image. NEVER mark "
                                "an ordinary box, text block or table as an "
                                "image, and NEVER emit one image covering the "
                                "whole slide."
                            ),
                            "required": ["type", "bbox"],
                            "properties": {
                                "type": {"const": "image"},
                                "bbox": {"type": "array", "description": bbox_desc},
                                "description": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "a few words naming what the region "
                                        "shows, not a transcription"
                                    ),
                                },
                            },
                        },
                    ]
                },
            },
        },
        "required": ["elements"],
    }
