"""Rough note -> ONE precise instruction for the slide analyzer.

The "추가 지시사항" box is the hardest part of the product to use: the user can
see that the slide came back wrong but cannot easily phrase what the analyzer
should do differently. This module takes their rough note and the CONTEXT of the
slide they are looking at, and hands back a single instruction that can be
pasted straight into ``extra_instructions`` on the next ``POST /api/convert``.

Like :mod:`app.services.converter` this module is **framework-free** - it
imports nothing from fastapi/starlette - so the whole feature is exercisable
from a test or a script, and ``cli.py`` could grow a ``--refine`` with no new
logic. ``client`` is injectable everywhere for the same reason: the test suite
never reaches the network.

Three decisions are worth reading before changing anything here:

* **The spec is SUMMARIZED, never dumped.** A 14-element SlideSpec is thousands
  of tokens of bbox integers that no language model needs in order to know
  which element the user means. :func:`spec_digest` turns it into a handful of
  short lines - type, rough position in words, one short excerpt - and caps
  both the element count and the character count. Coordinates never appear.
* **The user's note is never lost.** Every degradation path - an empty reply, a
  reply that is not prose, a reply full of markdown fences - ends in the
  *tidied note*, never in ``""``. The frontend puts this value straight into
  the textarea, so returning an empty string would delete what the user typed.
* **A fallback is reported, not hidden - and it is reported as ITSELF.** When
  the answer is only the tidied note, ``context_used`` is downgraded to
  ``"none"``, because no part of the spec or the image reached the result and
  claiming otherwise would make the UI's "슬라이드 분석 결과를 참고했습니다" a
  lie. But "none" alone conflated two different things: a genuine no-context
  run (the common first use, before any conversion - where the model really
  does write a new instruction) and this fallback (where a digest WAS built and
  sent and the model simply gave nothing back). So ``fell_back`` travels in the
  200 body next to it. The UI needs both to say what happened without inventing
  a cause: a missing context is not what went wrong when the context was there.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .. import config, vision_analyzer
from . import converter
from .converter import ConversionFailure

__all__ = [
    "SYSTEM_PROMPT",
    "CONTEXT_NONE",
    "CONTEXT_SPEC",
    "CONTEXT_IMAGE",
    "MAX_INSTRUCTION_CHARS",
    "DIGEST_MAX_ELEMENTS",
    "DIGEST_MAX_CHARS",
    "RefineResult",
    "build_messages",
    "coerce_spec",
    "mime_for_upload",
    "refine_instruction",
    "spec_digest",
    "tidy_note",
]

logger = logging.getLogger("app.services.instructions")

#: The three values ``context_used`` can take, as the contract pins them.
CONTEXT_SPEC = "spec"
CONTEXT_IMAGE = "image"
CONTEXT_NONE = "none"

#: Longest instruction handed back. An ``extra_instructions`` value far longer
#: than this stops being guidance and starts competing with the analyzer's own
#: prompt, and it also has to fit in a textarea the user can still read.
MAX_INSTRUCTION_CHARS = 600

#: How many elements :func:`spec_digest` describes individually.
DIGEST_MAX_ELEMENTS = 30

#: Hard cap on the digest, whatever the element count.
DIGEST_MAX_CHARS = 2000

#: Longest note echoed INTO the prompt (the response still echoes it in full).
#: A note is one or two sentences; anything past this is paste accident.
NOTE_PROMPT_CHARS = 2000

#: Longest spec STRING :func:`coerce_spec` will even try to parse. Shares
#: ``converter``'s JSON body cap, because it is the same kind of payload
#: arriving by a different door.
SPEC_MAX_CHARS = converter.MAX_JSON_BODY_BYTES

#: Longest excerpt taken from one text element.
_EXCERPT_CHARS = 40

#: Longest excerpt taken from one table cell, and from a whole header row.
_CELL_CHARS = 16
_HEADER_CHARS = 90


# --------------------------------------------------------------------------- #
# the prompt - this IS the feature
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You rewrite a rough note into ONE precise instruction for a \
slide-layout analyzer.

THE PIPELINE YOU ARE PART OF
A vision model is shown ONE image of a slide and must return that slide's layout as
structured data: text boxes, tables and autoshapes, each with a bounding box on a
normalized 0..1000 grid. The data is then drawn as a native, editable PowerPoint
slide. The user has usually just looked at that result, found something wrong, and
typed a short note about it. Your instruction is appended to the ANALYZER's prompt on
the next run, so you are writing for the analyzer - not replying to the user.

OUTPUT RULES
- Output the instruction and nothing else: no greeting, no "물론이죠", no preamble, no
  explanation of what you changed, no markdown code fences, no surrounding quotes, no
  closing remark.
- Address the analyzer in the imperative. Never answer the user, never ask a question,
  never offer alternatives to choose between.
- One short paragraph, 1-3 sentences. Use short numbered lines ONLY if the note really
  asks for several unrelated changes.
- Stay under 600 characters.

LANGUAGE
- Write in the SAME LANGUAGE as the user's note: a Korean note gets a Korean
  instruction, an English note an English one, and so on.
- The slide context is machine-generated and may mix languages. Ignore its language;
  match the note's.

HOW TO TURN A NOTE INTO AN INSTRUCTION
1. Work out what the user is pointing at, then name it UNAMBIGUOUSLY using the slide
   context - by position and by content ("우측 상단의 3열 3행 표"), never just "the
   table" or "that part".
2. State the corrective ACTION the analyzer can actually take: what to treat as one
   object, what not to split or merge, which row is a header, which text to transcribe
   verbatim, what to widen, what to keep, what to ignore.
3. Add nothing that is not in the note or in the slide context. Do not invent colours,
   numbers, wording or elements. If the note is vague and there is no context, keep the
   instruction short and general rather than guessing.
4. Do not merely restate the note. "표가 이상해" is a complaint; the instruction is what
   to DO about it.
5. Never mention JSON, schemas, output formats or coordinates you were not given: the
   analyzer already has its own output contract and repeating it breaks it.

EXAMPLE (the note is Korean, so the instruction is Korean)
note: 표가 이상해
context: 14 elements (text 9, shape 4, table 1); table - 우측 상단 - 3 rows x 3 cols -
header: Region | Users | Growth
instruction: 우측 상단 영역은 3열 3행 표입니다. 첫 행(Region / Users / Growth)을 헤더로 \
인식하고, 표 안의 값을 개별 텍스트 상자로 분리하지 마세요."""

_USER_TEMPLATE = """USER NOTE - this is the language to answer in:
<<<
{note}
>>>

SLIDE CONTEXT
{context}

Now output the single instruction for the analyzer, and nothing else."""

_CONTEXT_IMAGE_TEXT = (
    "The source image is attached below. Read it to identify the element the note "
    "refers to, and describe that element by position and content."
)

_CONTEXT_NONE_TEXT = (
    "None available - there is no analysis of the slide and no image. Turn the note "
    "into a clear, general instruction and add no details of your own."
)


# --------------------------------------------------------------------------- #
# failures - the contract's codes, built here so the route stays a thin shell
# --------------------------------------------------------------------------- #
def empty_note_failure() -> ConversionFailure:
    """400 ``empty_note``: nothing to work from."""
    return ConversionFailure(
        400,
        "empty_note",
        "No note was supplied, so there is nothing to complete.",
        "Write at least one line saying what you want changed - even '표가 "
        "이상해' is enough - and try again.",
    )


def _refine_failed_failure(exc: BaseException) -> ConversionFailure:
    """502 ``refine_failed``: the model was reached but gave nothing usable.

    ``ConversionFailure`` scrubs the text it is handed, so the upstream error
    body - which can quote the proxy's ``Received API Key = sk-...`` line - is
    redacted before it can reach a response. The unredacted copy went to the
    server log a moment earlier.

    The hint carries the upstream text and NOTHING else of its own except the
    pointer to the unredacted copy - the same line ``converter`` appends to
    ``analysis_failed``. The advice that used to sit here ("Your note was not
    changed - edit it and try again...") was a word-for-word English
    restatement of the Korean sentence the UI already prints above it, and
    ``refine_failed`` keeps ``keepServerDetail``, so the banner showed the same
    guidance twice, once per language. What belongs here is only what a fixed
    Korean sentence cannot know.
    """
    return ConversionFailure(
        502,
        "refine_failed",
        "The model could not complete the instruction "
        f"({type(exc).__name__}).",
        f"{converter.scrub_secrets(str(exc)).rstrip()}\n"
        f"{converter._UPSTREAM_LOG_HINT}",
    )


def _failure_for(exc: BaseException) -> ConversionFailure:
    """Classify an upstream failure onto this endpoint's codes, BY TYPE.

    The split is ``/api/convert``'s: a missing key is the server's 503, an
    unreachable proxy is a 502 ``proxy_unreachable``, and everything else the
    model layer can raise is a 502 ``refine_failed``. ``build_failed`` has no
    meaning here - nothing is built - so it is deliberately not reachable.
    """
    if isinstance(exc, ConversionFailure):
        return exc

    # Reuses converter's own logging seam, so the unredacted upstream text ends
    # up exactly where the conversion pipeline's does.
    converter._log_upstream(exc, context="refine")

    missing_key_type = getattr(config, "MissingApiKeyError", None)
    if isinstance(missing_key_type, type) and converter._first_in_chain(
        exc, missing_key_type
    ):
        return ConversionFailure(
            503,
            "missing_api_key",
            "No API key (GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY or "
            "LITELLM_API_KEY) is set on the server, so the note cannot be "
            "completed.",
            converter._MISSING_KEY_HINT,
        )

    connection = converter._first_in_chain(exc, *converter._connection_error_types())
    if connection is not None:
        return ConversionFailure(
            502,
            "proxy_unreachable",
            "The vision model endpoint could not be reached "
            f"({type(connection).__name__}: {connection}).",
            converter._proxy_unreachable_hint(),
        )

    return _refine_failed_failure(exc)


# --------------------------------------------------------------------------- #
# the note
# --------------------------------------------------------------------------- #
def tidy_note(note: Any, *, limit: int = MAX_INSTRUCTION_CHARS) -> str:
    """The note as a one-line, length-capped string.

    This is the FALLBACK instruction: whenever the model gives nothing usable,
    this is what goes back into the textarea, because the one thing this feature
    must never do is hand the user an empty box. Whitespace runs (including the
    newlines a pasted note carries) collapse to single spaces so the value is a
    well-formed single instruction rather than a paste artifact.
    """
    text = " ".join(str(note or "").split())
    return _truncate(text, limit)


def _truncate(text: str, limit: int) -> str:
    """``text`` capped at ``limit`` characters, cut at a readable boundary.

    A hard slice mid-word reads as corruption, so a sentence end inside the
    second half of the window wins, then a word boundary, and only then a blunt
    cut - the last two marked with an ellipsis so the reader can see that text
    was removed rather than wonder why the sentence stops.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    window = text[:limit]
    floor = limit // 2

    sentence_end = max(window.rfind(char) for char in ".。!?！？\n")
    if sentence_end >= floor:
        return window[: sentence_end + 1].strip()

    space = window.rfind(" ")
    if space >= floor:
        return window[:space].rstrip() + "…"
    return window[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# the spec digest
# --------------------------------------------------------------------------- #
def _safe_text(text: str) -> str:
    """``text`` guaranteed to survive a UTF-8 encode.

    ``json.loads`` accepts ``"\\ud800"`` and hands back a lone surrogate, which
    no UTF-8 encoder will take. Copied into the digest, it did not fail HERE -
    it failed inside the openai client's request build, and a spec the contract
    promises is "parsed leniently; unusable JSON is ignored" became a 502 the
    caller could not work around, with a raw python codec message in the hint of
    a Korean UI. Sanitizing where the text is BUILT keeps the promise: the digest
    is encodable by construction, so the request goes out with the unpaired
    halves replaced instead of not going out at all.
    """
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def coerce_spec(spec: Any) -> dict | None:
    """``spec`` as a plain dict with usable elements, or ``None``.

    Parsed LENIENTLY and on purpose: the spec is only CONTEXT here. A truncated
    JSON string, a spec under a wrapper key, a bare element array, a validated
    :class:`SlideSpec` object and ``None`` are all accepted, and anything that
    cannot be read at all returns ``None`` so the caller falls back to the image
    (or to no context) instead of rejecting a request the user can still be
    helped with. Nothing here raises.
    """
    if spec is None:
        return None

    payload: Any = spec

    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - decode with 'replace' cannot fail
            return None

    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        if len(text) > SPEC_MAX_CHARS:
            # Bounded before ``json.loads``, which would materialize it twice
            # more as python objects. A spec this big is not the context of a
            # slide the user is looking at, and it is only context - so it is
            # ignored (and reported as such via ``context_used``) rather than
            # turned into an error the user cannot act on.
            logger.warning(
                "refine: ignoring a %d-character spec (cap is %d)",
                len(text),
                SPEC_MAX_CHARS,
            )
            return None
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError):
            return None

    # A validated SlideSpec (or any pydantic model) - dump it to plain JSON
    # types so the reader below only ever deals with dicts and lists.
    if not isinstance(payload, (dict, list, tuple)) and hasattr(payload, "model_dump"):
        try:
            payload = payload.model_dump(mode="json")
        except Exception:  # pragma: no cover - defensive
            return None

    if not isinstance(payload, (dict, list, tuple)):
        return None

    try:
        from ..schemas import normalize_spec_payload

        normalized = normalize_spec_payload(payload)
    except Exception:  # pragma: no cover - never let the helper break context
        normalized = payload

    if not isinstance(normalized, dict):
        return None

    elements = normalized.get("elements")
    if not isinstance(elements, (list, tuple)):
        return None
    if not any(isinstance(item, dict) for item in elements):
        return None
    return normalized


def _element_type(item: dict) -> str:
    value = item.get("type")
    # Sanitized like every other string that reaches the digest: a spec is only
    # context, and an exotic ``type`` must not be able to break the request.
    text = _safe_text(str(value or "")).strip().lower()
    return text or "unknown"


def _bbox_numbers(bbox: Any) -> tuple[float, float, float, float] | None:
    """A bbox in either accepted shape as ``(x, y, w, h)`` floats, or ``None``.

    Both accepted input shapes are read - the object form the API returns and
    the ``[x, y, w, h]`` list a hand-edited spec may use - because this is the
    same lenient door :func:`coerce_spec` opens.
    """
    if isinstance(bbox, dict):
        raw = [bbox.get("x"), bbox.get("y"), bbox.get("w"), bbox.get("h")]
    elif isinstance(bbox, (list, tuple)):
        raw = list(bbox)[:4]
    else:
        return None

    if len(raw) < 4:
        return None

    numbers: list[float] = []
    for value in raw:
        if isinstance(value, bool) or value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number or number in (float("inf"), float("-inf")):
            return None  # NaN/inf: unusable, and it must not reach a comparison
        numbers.append(number)

    x, y, w, h = numbers
    return x, y, w, h


def _bbox_center(bbox: Any) -> tuple[float, float] | None:
    """The centre of a bbox in the 0..COORD_SCALE grid, or ``None``."""
    numbers = _bbox_numbers(bbox)
    if numbers is None:
        return None
    x, y, w, h = numbers
    return x + w / 2.0, y + h / 2.0


def _position_words(bbox: Any) -> str:
    """A bbox as words - "우측 상단", "상단 중앙", "중앙".

    Words, not numbers, because this is the whole point of the digest: the model
    has to know WHICH element the user means, and "우측 상단" does that in four
    characters where ``[720, 190, 250, 210]`` costs twenty and invites the model
    to quote coordinates back at the analyzer.
    """
    centre = _bbox_center(bbox)
    if centre is None:
        return "위치 불명"

    scale = float(getattr(config, "COORD_SCALE", 1000) or 1000)
    third = scale / 3.0
    cx, cy = centre

    horizontal = "좌측" if cx < third else ("우측" if cx >= 2 * third else "")
    vertical = "상단" if cy < third else ("하단" if cy >= 2 * third else "")

    if horizontal and vertical:
        return f"{horizontal} {vertical}"
    if horizontal:
        return f"{horizontal} 중앙"
    if vertical:
        return f"{vertical} 중앙"
    return "중앙"


#: A side thinner than this fraction of the slide is a RULE, not a box.
_HAIRLINE_FRAC = 0.03

#: How long the other side must be before a thin box counts as a line at all -
#: otherwise a 5x5 dot would be described as a hairline in both directions.
_HAIRLINE_RUN_FRAC = 0.12

#: Both sides at least this fraction: the element covers the whole slide.
_FULL_FRAC = 0.9

#: Wide and low enough to be a band (a header bar, a one-line title, a footer).
_BAND_WIDE_FRAC = 0.55
_BAND_LOW_FRAC = 0.2

#: Tall and narrow enough to be a column (a sidebar, a vertical divider).
_COLUMN_TALL_FRAC = 0.5
_COLUMN_NARROW_FRAC = 0.2

#: Share of the slide's AREA above which an element is "big" and below which it
#: is "small". Area rather than either side, so a 42%x26% table and a 33%x33%
#: card both read as big while a badge and a caption both read as small.
_BIG_AREA = 0.09
_SMALL_AREA = 0.012


#: Types whose thin-and-wide case is a LINE OF TEXT, not a rule. A 930x11 text
#: element is a caption; calling it "얇은 가로선" would point the analyzer at a
#: divider that is not there, which is the mirror image of the bug this fixes.
_TEXT_LIKE = frozenset({"text", "table"})


def _size_words(bbox: Any, kind: str = "") -> str:
    """A bbox's EXTENT as words - "얇은 가로선", "큰 영역" - or ``""``.

    Without this the digest described type, position and content but never how
    big anything is, so a 460x5 amber rule and a full-slide card produced
    identical lines - and the commonest complaint class there is (너무 크다 /
    작다 / 두껍다 / 얇다) had nothing to attach to. Measured against the live
    proxy, "구분선이 너무 두꺼워" mis-targeted the 1000x155 header band in 5 of 8
    runs without an extent word and hit the real 460x5 rule in 3 of 3 with one.
    A hairline is therefore called a hairline, and a card a card.

    Words, not numbers, for the same reason as :func:`_position_words` - and one
    more: a digest carrying "460x5" got quoted straight back at the analyzer,
    which works on the 0..1000 grid and has its own idea of the geometry.

    ``kind`` only picks the vocabulary for the thinnest bucket: see
    :data:`_TEXT_LIKE`.
    """
    numbers = _bbox_numbers(bbox)
    if numbers is None:
        return ""

    scale = float(getattr(config, "COORD_SCALE", 1000) or 1000)
    _, _, w, h = numbers
    # Clamped, because a spec is only context: an oversized or negative box is
    # described by what it can be, not rejected.
    fw = min(max(w, 0.0) / scale, 1.0)
    fh = min(max(h, 0.0) / scale, 1.0)

    text_like = kind in _TEXT_LIKE

    if fw <= 0 or fh <= 0:
        return "크기 없음"
    if fw >= _FULL_FRAC and fh >= _FULL_FRAC:
        return "슬라이드 전체 크기"
    if fh <= _HAIRLINE_FRAC and fw >= _HAIRLINE_RUN_FRAC:
        return "한 줄 높이" if text_like else "얇은 가로선"
    if fw <= _HAIRLINE_FRAC and fh >= _HAIRLINE_RUN_FRAC and not text_like:
        return "얇은 세로선"
    if fw >= _BAND_WIDE_FRAC and fh <= _BAND_LOW_FRAC:
        return "가로로 넓고 낮은 띠"
    if fh >= _COLUMN_TALL_FRAC and fw <= _COLUMN_NARROW_FRAC:
        return "세로로 좁고 긴 영역"

    area = fw * fh
    if area >= _BIG_AREA:
        return "큰 영역"
    if area <= _SMALL_AREA:
        return "작은 영역"
    return "중간 크기"


def _excerpt(value: Any, limit: int = _EXCERPT_CHARS) -> str:
    """One short, single-line, UTF-8-encodable excerpt of a value."""
    text = " ".join(_safe_text(str(value if value is not None else "")).split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _table_summary(item: dict) -> str:
    """``3 rows x 3 cols - header: Region | Users | Growth``."""
    data = item.get("data")
    rows = [row for row in data if isinstance(row, (list, tuple))] if isinstance(
        data, (list, tuple)
    ) else []
    row_count = len(rows)
    col_count = max((len(row) for row in rows), default=0)

    parts = [f"{row_count} rows x {col_count} cols"]
    if rows:
        header = " | ".join(_excerpt(cell, _CELL_CHARS) for cell in rows[0])
        header = _excerpt(header, _HEADER_CHARS)
        if header.strip(" |"):
            parts.append(f"header: {header}")
    return " - ".join(parts)


def _shape_summary(item: dict) -> str:
    """``rectangle - fill #1F3B73``, or the border when there is no fill."""
    kind = _excerpt(item.get("kind") or "rectangle", 24)
    parts = [kind]

    fill = item.get("fill_color")
    fill_text = _excerpt(fill, 12) if fill else ""
    if fill_text:
        parts.append(f"fill {fill_text}")
    else:
        # An unfilled shape is a FRAME around other content, which is exactly
        # the thing a user complains about ("테두리만 남았어"), so say so.
        parts.append("no fill")
        border = item.get("border_color")
        border_text = _excerpt(border, 12) if border else ""
        if border_text:
            parts.append(f"border {border_text}")
    return " - ".join(parts)


def _text_summary(item: dict) -> str:
    """``18pt bold - "Quarterly Platform Review"``."""
    parts: list[str] = []

    size = item.get("font_size")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
        parts.append(f"{int(size)}pt")
    if item.get("bold") is True:
        parts.append("bold")

    align = str(item.get("align") or "").strip().lower()
    if align in {"center", "right"}:
        parts.append(align)

    head = " ".join(parts)
    excerpt = _excerpt(item.get("content"))
    body = f'"{excerpt}"' if excerpt else "(empty text)"
    return f"{head} - {body}" if head else body


def _element_line(index: int, item: dict) -> str:
    kind = _element_type(item)
    bbox = item.get("bbox")
    position = _position_words(bbox)
    size = _size_words(bbox, kind)

    if kind == "table":
        detail = _table_summary(item)
    elif kind == "shape":
        detail = _shape_summary(item)
    elif kind == "text":
        detail = _text_summary(item)
    else:
        detail = _excerpt(item.get("content") or item.get("kind") or "")

    # Geometry together (what it is, where it is, how big it is), then content.
    parts = [f"{index}. {kind}", position]
    if size:
        parts.append(size)
    if detail:
        parts.append(detail)
    return " - ".join(parts)


def spec_digest(
    spec: Any,
    *,
    max_elements: int = DIGEST_MAX_ELEMENTS,
    max_chars: int = DIGEST_MAX_CHARS,
) -> str:
    """A compact, human-readable digest of a SlideSpec - or ``""``.

    Deliberately NOT the spec. Raw JSON for a 14-element slide is several
    thousand tokens of bbox integers that cannot help a model decide which
    element "표가 이상해" is about, and feeding coordinates in invites the model
    to quote coordinates back out - which the analyzer then has to fight. What
    does help is a numbered list of "type - rough position - rough extent -
    short excerpt", which is what this builds, bounded twice: ``max_elements``
    individually described elements and ``max_chars`` overall. Whatever is left
    out is COUNTED in the last line rather than silently dropped.

    The result is always UTF-8-encodable (see :func:`_safe_text`) and this
    function never raises: an unusable spec is ``""``, which the caller reads as
    "no spec context" and reports as such.
    """
    payload = coerce_spec(spec)
    if payload is None:
        return ""

    elements = [
        item for item in (payload.get("elements") or []) if isinstance(item, dict)
    ]
    if not elements:
        return ""

    counts: dict[str, int] = {}
    for item in elements:
        name = _element_type(item)
        counts[name] = counts.get(name, 0) + 1
    breakdown = ", ".join(
        f"{name} {count}"
        for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    )

    header = f"{len(elements)} elements ({breakdown})"
    background = payload.get("background_color")
    if background:
        header += f", background {_excerpt(background, 12)}"

    lines = [header]
    used = len(header)
    shown = 0

    for index, item in enumerate(elements[:max_elements], start=1):
        line = _element_line(index, item)
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1

    omitted = len(elements) - shown
    if omitted > 0:
        lines.append(f"... and {omitted} more element(s) not listed")

    # Every piece was sanitized on the way in; this is the one gate that cannot
    # be bypassed by a new helper forgetting to call _safe_text.
    return _safe_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# the request payload
# --------------------------------------------------------------------------- #
def mime_for_upload(filename: str | None, *, default: str = "image/png") -> str:
    """An upload's MIME type from its own name, reusing the CLI's table."""
    extension = converter.upload_extension(filename)
    table = getattr(config, "MIME_BY_EXTENSION", {}) or {}
    return table.get(extension, default)


def build_messages(
    note: str,
    *,
    digest: str | None = None,
    image_b64: str | None = None,
    mime: str | None = None,
) -> list[dict]:
    """The chat payload: system rules, the note, and at most one context.

    The note is capped at :data:`NOTE_PROMPT_CHARS` *in the prompt only* - the
    response still echoes it in full - so a pasted wall of text cannot push the
    system rules out of the model's attention.
    """
    if digest:
        context = digest
    elif image_b64:
        context = _CONTEXT_IMAGE_TEXT
    else:
        context = _CONTEXT_NONE_TEXT

    text = _USER_TEMPLATE.format(
        # Sanitized for the same reason the digest is: a note handed straight to
        # this function (the CLI, a script) may carry an unpaired surrogate that
        # would make the request unsendable. The RESPONSE still echoes the
        # caller's own note untouched.
        note=_safe_text(_truncate(str(note or "").strip(), NOTE_PROMPT_CHARS)),
        context=context,
    )

    if digest or not image_b64:
        content: Any = text
    else:
        content = [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime or 'image/png'};base64,{image_b64}"
                },
            },
        ]

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


# --------------------------------------------------------------------------- #
# the reply
# --------------------------------------------------------------------------- #
#: Leading/trailing quote characters a model likes to wrap an answer in.
_QUOTE_CHARS = "\"'`“”„‘’«»「」『』"


def _has_message(response: Any) -> bool:
    """Whether the response really is a chat completion with a message.

    This is what separates "the model replied with nothing" (recoverable: the
    tidied note is a perfectly good answer) from "that was not a chat
    completion at all" (a real 502), since
    :func:`vision_analyzer._message_content` raises for both.
    """
    try:
        return getattr(response.choices[0], "message", None) is not None
    except Exception:
        return False


def _reply_text(response: Any) -> str:
    """The assistant's text, or ``""`` when the model said nothing.

    ``vision_analyzer._message_content`` is reused rather than reimplemented:
    it already handles the list-of-parts content shape some proxies return, and
    the openai plumbing has exactly one owner. Its "empty content" error is
    downgraded to ``""`` here because an empty reply is fatal for a SPEC and
    merely disappointing for an instruction.
    """
    try:
        return vision_analyzer._message_content(response)
    except Exception:
        if _has_message(response):
            return ""
        raise


_FENCE_ONLY_RE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*$|^\s*```\s*$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Markdown fences removed, whether or not they were closed.

    The prompt forbids fences; models add them anyway, and a leading "```" in
    the textarea would then travel into the analyzer's prompt as literal noise.
    ``vision_analyzer._fenced_blocks`` is reused for the well-formed case (it
    already knows the fence grammar); a stray unpaired fence line is stripped
    by hand, because a half-fenced answer still has usable prose in it.
    """
    if "```" not in text:
        return text

    try:
        blocks = [block for block in vision_analyzer._fenced_blocks(text) if block.strip()]
    except Exception:  # pragma: no cover - defensive
        blocks = []

    if blocks:
        return blocks[0]
    return _FENCE_ONLY_RE.sub("", text)


#: Opening quote -> the closer that would actually pair with it.
_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "`": "`",
    "“": "”",
    "„": "“",
    "‘": "’",
    "«": "»",
    "「": "」",
    "『": "』",
}


def _strip_quotes(text: str) -> str:
    """Wrapping quotes removed - only when they really do wrap the whole thing.

    Three conditions, because an instruction that quotes the slide's own words
    is the GOOD case and must survive: the first character must be an opening
    quote, the last must be the closer that pairs with it, and neither may
    appear again inside. So ``"표를 하나로 인식하세요."`` loses its quotes while
    ``"Region"을 헤더로 쓰고 "Growth"는 마지막 열입니다`` - which merely begins and
    ends with a quoted word - keeps every one of them.
    """
    stripped = text.strip()
    while len(stripped) >= 2:
        opener = stripped[0]
        closer = _QUOTE_PAIRS.get(opener)
        if closer is None or stripped[-1] != closer:
            break
        inner = stripped[1:-1].strip()
        if not inner or opener in inner or closer in inner:
            break
        stripped = inner
    return stripped


def _clean_instruction(raw: str) -> str:
    """A model reply as the one instruction, or ``""`` when nothing survives."""
    text = str(raw or "")
    if not text.strip():
        return ""

    text = _strip_fences(text)
    text = _strip_quotes(text)

    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Not prose at all - punctuation, emoji or an empty fence body. Falling
    # back to the note beats pasting this into the user's textarea.
    if not re.search(r"\w", text, re.UNICODE):
        return ""

    return _truncate(text, MAX_INSTRUCTION_CHARS)


# --------------------------------------------------------------------------- #
# the result
# --------------------------------------------------------------------------- #
@dataclass
class RefineResult:
    """What one refine produced, ready for the JSON response.

    ``instruction`` is never empty by construction (see
    :func:`refine_instruction`): the frontend writes it straight into the
    textarea, so an empty value would silently delete the user's own words.

    ``fell_back`` IS reported in the body, because the UI cannot describe what
    happened without it: ``context_used == "none"`` alone covers both "there was
    nothing to go on, so the instruction was written from the note" and "there
    was a digest, the model gave nothing back, so this is your own note". Those
    need different sentences, and guessing between them is how a UI ends up
    stating a cause that did not happen.

    ``digest`` stays out: it is prompt-sized, it is for the server log and for
    tests, and no client has a use for it.
    """

    instruction: str
    note: str
    context_used: str = CONTEXT_NONE
    fell_back: bool = False
    digest: str = field(default="", repr=False)

    def payload(self) -> dict[str, Any]:
        """The 200 body, exactly as the contract pins it."""
        return {
            "status": "success",
            "instruction": self.instruction,
            "note": self.note,
            "context_used": self.context_used,
            "fell_back": self.fell_back,
        }


# --------------------------------------------------------------------------- #
# the public entry point
# --------------------------------------------------------------------------- #
def refine_instruction(
    note: str,
    *,
    spec: Any = None,
    image_bytes: bytes | None = None,
    mime: str | None = None,
    model: str | None = None,
    client: Any = None,
) -> RefineResult:
    """Complete ``note`` into one instruction for the slide analyzer.

    Context selection is cheapest-first and reported rather than assumed:
    ``spec`` wins when it parses and has elements (text only, no vision call),
    otherwise ``image_bytes`` is sent (one vision call), otherwise the note is
    tidied on its own. The choice travels back as ``context_used`` so the UI can
    say which one it was instead of implying the best case.

    The image is expected to be ALREADY VALIDATED - the route runs
    ``converter.validate_upload_bytes``, which owns the 20 MB cap, the extension
    list and the "is this really an image" decode - so this function only has to
    encode it.

    Args:
        note: the user's rough text, in any language. Blank is an error, not an
            empty instruction.
        spec: the current SlideSpec as a JSON string, a dict or a ``SlideSpec``.
            Unusable values are IGNORED, never fatal: it is only context.
        image_bytes: the source image, used only when ``spec`` gives nothing.
        mime: that image's MIME type; defaults to ``image/png``.
        model: overrides ``config.DEFAULT_MODEL``.
        client: an OpenAI-compatible client; built from ``config`` when absent,
            which is what keeps the test suite offline.

    Raises:
        ConversionFailure: 400 ``empty_note``, 503 ``missing_api_key``, 502
            ``proxy_unreachable`` or 502 ``refine_failed``, already carrying the
            envelope's ``{code, message, hint}``.
    """
    original_note = note if isinstance(note, str) else ("" if note is None else str(note))
    if not original_note.strip():
        raise empty_note_failure()

    fallback = tidy_note(original_note)

    try:
        digest = spec_digest(spec)
    except Exception as exc:  # noqa: BLE001 - context is never worth a failure
        # spec_digest is written not to raise, and _safe_text keeps the one
        # input that used to (a lone surrogate) from getting that far. This is
        # the structural version of the same promise: whatever a spec turns out
        # to contain, it can cost at most the spec CONTEXT - never the request.
        logger.warning(
            "refine: could not summarize the spec (%s: %s); ignoring it",
            type(exc).__name__,
            exc,
        )
        digest = ""

    image_b64 = ""
    if digest:
        context_used = CONTEXT_SPEC
    elif image_bytes:
        try:
            image_b64 = base64.b64encode(bytes(image_bytes)).decode("ascii")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("refine: could not encode the image (%s)", exc)
            image_b64 = ""
        context_used = CONTEXT_IMAGE if image_b64 else CONTEXT_NONE
    else:
        context_used = CONTEXT_NONE

    if client is None:
        # Checked before anything is encoded or sent, exactly as convert_upload
        # does, so a misconfigured server answers at once instead of after a
        # round trip.
        converter.require_api_key()
        try:
            client = config.get_client()
        except Exception as exc:  # noqa: BLE001 - re-raised as a mapped failure
            raise _failure_for(exc) from exc

    messages = build_messages(
        original_note,
        digest=digest or None,
        image_b64=image_b64 or None,
        mime=mime,
    )
    model_name = model or getattr(config, "DEFAULT_MODEL", "gemini-flash")

    try:
        create = client.chat.completions.create
    except AttributeError as exc:
        raise _failure_for(
            RuntimeError(
                "The supplied client does not look like an OpenAI client "
                "(missing .chat.completions.create)."
            )
        ) from exc

    try:
        # No ``response_format``: the answer is prose, not JSON, and asking a
        # proxied model for json_object here would be the one thing the prompt
        # forbids. Timeout and retries come from the client config.get_client
        # built (config.LLM_TIMEOUT / config.LLM_MAX_RETRIES).
        response = create(model=model_name, messages=messages, temperature=0.2)
    except Exception as exc:  # noqa: BLE001 - classified, never relayed raw
        raise _failure_for(exc) from exc

    try:
        raw = _reply_text(response)
    except Exception as exc:  # noqa: BLE001
        raise _failure_for(exc) from exc

    instruction = _clean_instruction(raw)

    if not instruction:
        # Never "" - that would wipe the textarea. And never a claim that the
        # spec or the image informed an answer that is only the user's own
        # words: context_used is downgraded so the UI stays honest. ``fell_back``
        # carries the REASON alongside it, so the UI can say "the model could
        # not complete it" instead of inventing a missing context - the digest
        # may well have been built and sent.
        logger.warning(
            "refine: model '%s' returned nothing usable (%d raw chars); "
            "falling back to the tidied note",
            model_name,
            len(str(raw or "")),
        )
        return RefineResult(
            instruction=fallback,
            note=original_note,
            context_used=CONTEXT_NONE,
            fell_back=True,
            digest=digest,
        )

    return RefineResult(
        instruction=instruction,
        note=original_note,
        context_used=context_used,
        fell_back=False,
        digest=digest,
    )
