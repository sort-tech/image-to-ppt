"""Vision LLM front-end: image -> structured :class:`SlideSpec`.

The module is deliberately defensive: LLMs wrap JSON in markdown fences, prepend
prose, invent extra keys and occasionally reject the ``response_format``
parameter.  Every one of those failure modes is handled here so that the rest of
the pipeline only ever sees a validated :class:`schemas.SlideSpec` or a single,
actionable :class:`VisionAnalysisError`.

No network call happens at import time; ``analyze_image`` accepts an injectable
``client`` so tests can run fully offline.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from typing import Any

from . import config
from .schemas import SlideSpec, all_dropped_reasons, normalize_spec_payload

__all__ = [
    "VisionAnalysisError",
    "SYSTEM_PROMPT",
    "USER_PROMPT",
    "encode_image",
    "extract_json",
    "build_messages",
    "analyze_image",
]


class VisionAnalysisError(Exception):
    """Raised for any failure while turning an image into a ``SlideSpec``."""


SYSTEM_PROMPT = """You are a precise slide-layout analyzer.

You receive ONE image of a slide, document page or diagram. You reconstruct its
layout as structured data so it can be redrawn with native, editable PowerPoint
objects. You never describe the image in prose and you never summarize it - you
transcribe and measure it.

OUTPUT FORMAT
- Return PURE JSON only. No markdown, no code fences, no commentary, no
  explanation before or after the JSON.
- The top-level value is a single JSON object with exactly these keys:
    {
      "background_color": "#RRGGBB" or null,
      "elements": [ ... ]
    }

COORDINATE SYSTEM
- All positions are INTEGERS from 0 to 1000 on BOTH axes (a normalized grid,
  NOT pixels and NOT percentages of a square).
- Every element has "bbox": [x, y, w, h] where
    x = distance from the LEFT edge, as 0..1000 of the image WIDTH
    w = box WIDTH, as 0..1000 of the image WIDTH
    y = distance from the TOP edge, as 0..1000 of the image HEIGHT
    h = box HEIGHT, as 0..1000 of the image HEIGHT
- Keep x + w <= 1000 and y + h <= 1000. Never use negative numbers.
- Measure carefully: a box must tightly enclose the thing it represents.

ELEMENT TYPES (use these exact type names and attribute names)

1) Shape - a filled and/or outlined box, panel, band, divider, circle or BLOCK
   arrow:
   {
     "type": "shape",
     "bbox": [x, y, w, h],
     "kind": "rectangle" | "rounded_rectangle" | "circle"
             | "arrow_right" | "arrow_left" | "arrow_up" | "arrow_down",
     "fill_color": "#RRGGBB" or null,
     "border_color": "#RRGGBB" or null,
     "border_width": 1.0
   }
   - "fill_color": null means no fill (transparent, content behind stays visible).
   - "border_color": null means no outline.
   - "border_width" is in points (typically 0.75 - 3.0).
   - The "arrow_*" kinds are BLOCK arrows: a thick, filled arrow BODY you could
     put a label inside, pointing right / left / up / down. A thin arrow that
     merely joins two boxes is NOT a shape - it is a "connector" (type 4).

2) Table - any grid of cells, ruled or unruled:
   {
     "type": "table",
     "bbox": [x, y, w, h],
     "data": [["Header A", "Header B"], ["r1c1", "r1c2"], ["r2c1", "r2c2"]],
     "header_bg_color": "#RRGGBB" or null,
     "header_font_bold": true,
     "font_size": 10
   }
   - "data" is a row-major 2-D array of STRINGS (outer list = rows, inner list =
     that row's cells). The FIRST row is the header row.
   - Every row must have the SAME number of columns; use "" for empty cells.
   - Never put a table's text into separate text elements, and never flatten a
     table into one multi-line string.

3) Text - a title, label, paragraph, bullet list or caption:
   {
     "type": "text",
     "bbox": [x, y, w, h],
     "content": "Exact visible text",
     "font_size": 18,
     "font_color": "#RRGGBB" or null,
     "bold": false,
     "align": "left" | "center" | "right"
   }
   - Use "\\n" inside "content" for line breaks; each bullet or line is its own
     line in the same text element when the lines form one visual block.
   - Use a SINGLE "\\n" between lines, never "\\n\\n". Do not add blank lines to
     imitate loose line spacing - a blank line becomes a real empty paragraph
     and pushes the rest of the block off position.
   - Bullet lists: one item per line, in the same text element, each line
     starting with "- " . Do not copy decorative bullet glyphs and do not emit
     one text element per bullet.

   MEASURING "font_size" (do not guess - compute it):
   - Measure the CAP HEIGHT of the text (top of a capital letter to the
     baseline) as a fraction of the image height, then multiply by 750 and
     round. Example: caps about 1/20 of the image height -> 0.05 * 750 = 38pt.
   - Sanity band: slide titles land near 28-40, section headings 16-22, body
     and bullet text 11-16, captions and footnotes 8-11. If your computed value
     falls far outside the band that matches the text's role, re-measure.

   SIZING A TEXT BBOX (critical - a too-narrow box re-wraps the text):
   - "w" must be at least as wide as the text actually needs, PLUS about 10%
     headroom. PowerPoint re-flows text, so a single-line title in a box that
     is even slightly too narrow wraps onto two lines and ruins the layout.
   - Rough width check: a line of N characters at F points needs roughly
     N * F * 0.5 points of width, i.e. (N * F * 0.5 / 540 * 1000) units of "w".
     If your "w" is smaller than that, widen it (extend to the right, keeping
     "x" where the text starts, and stay within 1000).
   - "h" should cover the whole visible block: about (number of lines) *
     (font_size * 1.35 / 540 * 1000) units, and never less than one line.
   - For centred text, keep the box symmetric about the text's visual centre;
     for right-aligned text, extend the box to the LEFT of the text.

   JUDGING "bold":
   - Actually look at the stroke weight and set "bold": true for text that is
     visibly heavier than the body text around it - slide titles, section
     headings, emphasised labels, KPI numbers. Do not leave every element
     "bold": false out of caution; a bold title rendered as regular weight is a
     visible error. Set it false only for genuinely normal-weight text.

4) Connector - a line or arrow that JOINS two things (a diagram arrow, a flow
   line, a leader line, a bracket line):
   {
     "type": "connector",
     "kind": "straight" | "elbow",
     "start_point": [x, y],
     "end_point": [x, y],
     "arrow_start": false,
     "arrow_end": true,
     "dash_style": "solid" | "dashed",
     "color": "#RRGGBB" or null,
     "width_pt": 1.5
   }
   - A connector has NO "bbox". It IS its two endpoints, each [x, y] on the
     same 0..1000 grid. Put each endpoint where the line visibly touches the
     box it joins (on the box EDGE), not at the box centre.
   - "kind": "straight" for a direct line; "elbow" for a line that turns a
     right angle on its way, which is what most flow-chart arrows do when the
     two boxes are offset on both axes.
   - "arrow_start" / "arrow_end" say which END carries the arrowhead. A one-way
     arrow pointing at the target is "arrow_start": false, "arrow_end": true;
     reverse them for an arrow pointing back; set BOTH true for a
     double-headed arrow; set BOTH false for a plain line with no arrowhead.
   - "dash_style": "dashed" for a dashed or dotted line, otherwise "solid".
   - "width_pt" is the line thickness in points (typically 1.0 - 3.0).
   - Emit one connector per visible line. Do not draw connectors that are not
     in the image just to tidy up a diagram.

5) Image - a region you genuinely CANNOT rebuild with the types above:
   {
     "type": "image",
     "bbox": [x, y, w, h],
     "description": "team photo"
   }
   - Use it for a photograph, a rendered chart or graph whose numbers you
     cannot transcribe, a logo, a map, a screenshot, an icon set or a detailed
     illustration. That exact region is cropped out of the source image and
     placed on the slide, so keep "bbox" TIGHT around the picture itself.
   - "description" is a few words naming what the region shows. It is a label
     for the preview, not a transcription, and it is never drawn on the slide.
   - NEVER mark an ordinary box, panel, band, divider, text block, label,
     heading or table as "image". Those must stay native and editable: that is
     the entire purpose of this conversion. A chart drawn as coloured
     rectangles is shapes; a grid of printed numbers is a table; anything you
     can read is text.
   - NEVER emit one "image" covering the whole slide (or most of it), and never
     a few big "image" regions that together tile the slide. That flattens the
     slide into a screenshot and destroys every editable object; such output is
     rejected and the region is skipped.
   - If you are unsure whether a region needs an "image", prefer rebuilding it
     from shapes and text, and prefer leaving it out over covering the slide.

COLOURS
- Always "#RRGGBB" hex (6 digits, leading "#"). No colour names, no rgb(),
  no alpha. When a colour is absent or you are unsure, use null.
- "font_color" is the colour of the GLYPHS, sampled from the letter strokes -
  not the colour of the panel behind them. Light text on a dark band is
  "#FFFFFF" (or near-white); dark text on a light panel keeps its own dark
  hue. Never report black for text that is visibly coloured.

Z-ORDER RULE (critical)
- The "elements" array is painted in order: index 0 is at the BACK, the last
  element is at the FRONT.
- Emit background and container shapes EARLY (background panels, header bands,
  coloured boxes, dividers, circles) and emit foreground content LATE (tables
  first, then text that sits on top of shapes).
- Text and tables must come AFTER any shape they sit on, otherwise the shape
  covers them.
- An "image" region goes with the layer it belongs to: a photo panel sitting
  behind a caption comes BEFORE that caption.
- Connectors come AFTER the boxes they join, so the arrow is not hidden under a
  panel, but BEFORE the labels that sit on top of it.
- Do NOT wrap the whole slide in one giant shape covering everything. The slide
  background belongs in "background_color", not in a full-bleed shape.

TRANSCRIPTION RULES
- Transcribe visible text verbatim - VERBATIM means: same words, same capitalization, same
  punctuation, same numbers and units. Do not translate, correct, shorten or
  paraphrase. Keep natural reading order (top to bottom, left to right).
- Omit purely decorative artifacts you cannot represent at all: gradients,
  drop shadows, textures, glows and similar page furniture.
- A photograph, logo, icon, map or un-transcribable chart is NOT omitted any
  more - mark that region as an "image" element (type 5) so it is cropped out
  of the source image. The limits in type 5 still hold: never an "image" for
  something you could rebuild as a shape, table or text, and never one big
  "image" standing in for the whole slide.
- Prefer a HANDFUL OF ACCURATE elements over many speculative ones. If you
  cannot see something clearly, leave it out rather than guessing.

Return the JSON object and nothing else."""


USER_PROMPT = (
    "Analyze this image and reconstruct its layout as native PowerPoint objects.\n"
    "Return a single JSON object with \"background_color\" and \"elements\", using "
    "integer bbox coordinates [x, y, w, h] on the 0..1000 grid described in the "
    "system instructions.\n"
    "Order \"elements\" back-to-front: background/container shapes first, then "
    "tables, then text.\n"
    "Transcribe all visible text verbatim and keep every table as a row-major 2-D "
    "\"data\" array of strings with the header row first.\n"
    "Emit every diagram line or arrow as a \"connector\" with \"start_point\" and "
    "\"end_point\" (no bbox), saying which end carries the arrowhead and whether it "
    "is dashed; use \"shape\" with \"kind\": \"arrow_right\" (or left/up/down) for a "
    "thick block arrow.\n"
    "Mark only a genuinely un-rebuildable region - a photo, a logo, an "
    "un-transcribable chart - as an \"image\"; never an ordinary box, text or table, "
    "and never one \"image\" covering the whole slide.\n"
    "Compute each \"font_size\" from the measured cap height, set \"bold\": true for "
    "visibly heavy text such as titles and headings, and make every text \"w\" wide "
    "enough (about 10% headroom) that the line cannot re-wrap.\n"
    "Respond with JSON only - no markdown fences and no commentary."
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _warn(message: str) -> None:
    """Emit a non-fatal diagnostic on stderr."""
    print(f"[vision_analyzer] {message}", file=sys.stderr)


def _schema_hint() -> str:
    """Compact schema description for the user turn.

    ``schemas.llm_json_schema`` is owned by another module; never let a problem
    there break the request.
    """
    try:
        from .schemas import llm_json_schema  # local import: keeps failures contained

        schema = llm_json_schema()
        if not schema:
            return ""
        return json.dumps(schema, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception as exc:  # pragma: no cover - defensive
        _warn(f"could not build schema hint ({exc.__class__.__name__}: {exc})")
        return ""


# --------------------------------------------------------------------------- #
# image encoding
# --------------------------------------------------------------------------- #
def encode_image(image_path: str | Path) -> tuple[str, str]:
    """Return ``(base64_data, mime_type)`` for *image_path*."""
    path = Path(image_path).expanduser()
    suffix = path.suffix.lower()

    supported = getattr(config, "SUPPORTED_EXTENSIONS", frozenset())
    if supported and suffix not in supported:
        allowed = ", ".join(sorted(supported))
        raise VisionAnalysisError(
            f"Unsupported image type '{suffix or path.name}'. Supported extensions: {allowed}."
        )

    mime = getattr(config, "MIME_BY_EXTENSION", {}).get(suffix)
    if not mime:
        raise VisionAnalysisError(
            f"No MIME type known for '{suffix or path.name}'. "
            "Convert the image to PNG, JPEG or WebP and try again."
        )

    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise VisionAnalysisError(f"Image not found: {path}") from exc
    except IsADirectoryError as exc:
        raise VisionAnalysisError(f"Expected an image file but got a directory: {path}") from exc
    except OSError as exc:
        raise VisionAnalysisError(f"Could not read image {path}: {exc}") from exc

    if not data:
        raise VisionAnalysisError(f"Image file is empty: {path}")

    return base64.b64encode(data).decode("ascii"), mime


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(
    r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)(?:```|\Z)",
    re.DOTALL,
)


def _fenced_blocks(text: str) -> list[str]:
    """Contents of markdown code fences, ```json blocks first."""
    json_blocks: list[str] = []
    other_blocks: list[str] = []
    for match in _FENCE_RE.finditer(text):
        lang = (match.group(1) or "").lower()
        body = match.group(2)
        if not body.strip():
            continue
        if lang in ("json", "json5", "jsonc", ""):
            json_blocks.append(body)
        else:
            other_blocks.append(body)
    return json_blocks + other_blocks


def _scan_objects(text: str) -> list[tuple[str, bool]]:
    """Every balanced ``{...}`` substring as ``(payload, salvaged)``.

    Brace counting is string-literal aware: braces inside JSON string values
    (including escaped quotes such as ``\\"``) never affect the depth.

    ``salvaged`` marks an object that was only found *inside* an opening brace
    that never closed - i.e. a fragment of a TRUNCATED reply. Such a fragment is
    typically one complete element out of an unfinished ``"elements"`` array;
    accepting it as the whole spec is silent, total data loss, so callers must
    treat it as a truncation signal rather than as the answer.
    """
    found: list[tuple[str, bool]] = []
    length = len(text)
    index = 0
    salvaged = False
    while index < length:
        if text[index] != "{":
            index += 1
            continue

        depth = 0
        in_string = False
        escaped = False
        end = -1
        for pos in range(index, length):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = pos
                    break

        if end != -1:
            found.append((text[index : end + 1], salvaged))
            index = end + 1  # continue after this outermost object
        else:
            # A '{' that never closes: either stray prose, or - far more often -
            # a reply cut off at the model's output limit. Everything after it is
            # nested inside that unfinished object.
            salvaged = True
            index += 1
    return found


def _balanced_objects(text: str) -> list[str]:
    """Every balanced ``{...}`` substring found in *text*, outermost first."""
    return [payload for payload, _salvaged in _scan_objects(text)]


#: Top-level keys that identify a reply as the slide spec rather than commentary.
_SPEC_KEYS = ("elements", "background_color")


def _looks_like_slide_spec(payload: Any) -> bool:
    """True when *payload* carries a top-level key from the slide-spec contract."""
    return isinstance(payload, dict) and any(key in payload for key in _SPEC_KEYS)


def _array_payload(source: str) -> dict | None:
    """A bare top-level array of elements, wrapped as ``{"elements": [...]}``.

    Models sometimes answer with just the element array. Parsing that as a spec
    yields zero elements (``extra="ignore"`` swallows everything), so recover it
    explicitly. Arrays that are not element-shaped (``[1, 2, 3]``) return None.
    """
    text = (source or "").strip()
    if not text:
        return None

    attempts: list[str] = []
    if text.startswith("["):
        attempts.append(text)
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        sliced = text[start : end + 1]
        if sliced not in attempts:
            attempts.append(sliced)

    for candidate in attempts:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(payload, list):
            continue
        normalized = normalize_spec_payload(payload)
        if _looks_like_slide_spec(normalized):
            return normalized
    return None


def extract_json(raw: str) -> str:
    """Pull the JSON object out of a raw LLM reply.

    Strips markdown fences and leading/trailing prose and returns a balanced
    ``{...}`` object.

    Candidates are ranked rather than taken blindly in order, because models
    routinely emit a chatty little object (``{"note": ...}``) *before* the real
    payload, wrap the spec in an envelope key, answer with a bare element array,
    or get cut off mid-reply. Preference is:

    1. an object that parses AND looks like a slide spec;
    2. a spec hidden under a wrapper key (``{"slide": {...}}``);
    3. a bare top-level array of elements -> ``{"elements": [...]}``;
    4. any other object that parses and is not a fragment of a truncated reply;
    5. otherwise a truncation / "no JSON" error - never a fragment passed off as
       the whole spec, which would silently produce a blank deck.

    Raises:
        VisionAnalysisError: when no usable JSON object can be located.
    """
    if raw is None:
        raise VisionAnalysisError("Model returned no content (None) - expected a JSON object.")
    if not isinstance(raw, str):
        raw = str(raw)

    text = raw.strip()
    if not text:
        raise VisionAnalysisError("Model returned an empty response - expected a JSON object.")

    sources = [*_fenced_blocks(text), text]

    scanned: list[tuple[str, bool]] = []
    for source in sources:
        scanned.extend(_scan_objects(source))

    parsed: list[tuple[str, bool, Any]] = []
    for candidate, salvaged in scanned:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        parsed.append((candidate, salvaged, payload))

    # 1. a spec-shaped object wins wherever in the reply it sits
    for candidate, _salvaged, payload in parsed:
        if _looks_like_slide_spec(payload):
            return candidate

    # 2. the spec hidden under an envelope key: {"slide": {...}}
    for _candidate, _salvaged, payload in parsed:
        normalized = normalize_spec_payload(payload)
        if normalized is not payload and _looks_like_slide_spec(normalized):
            return json.dumps(normalized, ensure_ascii=False)

    # 3. a bare top-level array of elements
    for source in sources:
        array_spec = _array_payload(source)
        if array_spec is not None:
            return json.dumps(array_spec, ensure_ascii=False)

    # 4. any object that parses and is not a fragment of an unterminated object
    for candidate, salvaged, _payload in parsed:
        if not salvaged:
            return candidate

    # 5. only fragments of an object that never closed -> the reply was truncated
    if parsed:
        raise VisionAnalysisError(
            "The model's reply looks truncated: the JSON object it started never "
            f"closed, and only {len(parsed)} incomplete fragment(s) could be found. "
            "Fix: re-run (the reply probably hit the model's output limit), try "
            "another --model, or narrow the request with --extra."
        )

    if scanned:
        # Balanced but not parseable - hand back the best candidate so the caller
        # can report a precise JSON error.
        return scanned[0][0]

    preview = text[:200].replace("\n", " ")
    if "{" in text:
        raise VisionAnalysisError(
            "No complete JSON object found in the model response: an object was "
            "opened but never closed, which usually means the reply was cut off. "
            f"Fix: re-run or try another --model. First 200 chars: {preview!r}"
        )
    raise VisionAnalysisError(
        "No JSON object found in the model response. The model replied with prose "
        f"instead of JSON. First 200 chars: {preview!r}"
    )


# --------------------------------------------------------------------------- #
# request payload
# --------------------------------------------------------------------------- #
def build_messages(b64: str, mime: str, extra_instructions: str | None = None) -> list[dict]:
    """Build the OpenAI-compatible Vision chat payload."""
    parts = [USER_PROMPT]

    hint = _schema_hint()
    if hint:
        parts.append(f"JSON shape reference:\n{hint}")

    if extra_instructions and str(extra_instructions).strip():
        parts.append(f"Additional instructions:\n{str(extra_instructions).strip()}")

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "\n\n".join(parts)},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        },
    ]


# --------------------------------------------------------------------------- #
# completion handling
# --------------------------------------------------------------------------- #
def _message_content(response: Any) -> str:
    """Extract assistant text from an OpenAI-shaped chat completion."""
    try:
        choice = response.choices[0]
    except Exception as exc:
        raise VisionAnalysisError(
            f"Malformed model response (no choices): {response!r}"
        ) from exc

    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None

    if content is None and message is not None:
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise VisionAnalysisError(f"The model refused the request: {refusal}")

    # Some proxies return the content as a list of parts instead of a string.
    if isinstance(content, (list, tuple)):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                chunks.append(str(part.get("text", "")))
            else:
                chunks.append(str(getattr(part, "text", "") or ""))
        content = "".join(chunks)

    if not content or not str(content).strip():
        finish = getattr(choice, "finish_reason", None)
        detail = f" (finish_reason={finish})" if finish else ""
        raise VisionAnalysisError(
            f"The model returned empty content{detail}. Try a different model via "
            "--model or VISION_MODEL."
        )

    return str(content)


# --------------------------------------------------------------------------- #
# verbose diagnostics (-v): what we sent, and what came back
# --------------------------------------------------------------------------- #
#: Longest raw reply echoed under --verbose before it is truncated.
_VERBOSE_REPLY_CHARS = 20000


def _redacted_messages(messages: list[dict]) -> list[dict]:
    """Copy of *messages* with the base64 image body replaced by its length.

    Everything else is kept verbatim so the dump really is the payload the proxy
    was sent; the image bytes are useless to a human and megabytes long.
    """
    redacted: list[dict] = []
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, (list, tuple)):
            redacted.append(dict(message) if isinstance(message, dict) else {"raw": str(message)})
            continue
        parts: list[Any] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = str((part.get("image_url") or {}).get("url", ""))
                head, sep, body = url.partition(";base64,")
                if sep:
                    url = f"{head};base64,<{len(body)} base64 chars omitted>"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                parts.append(part)
        redacted.append({**message, "content": parts})
    return redacted


def _log_request(model: str, messages: list[dict], *, response_format: bool) -> None:
    """Print the request shape and the (image-redacted) payload on stderr."""
    roles = ", ".join(str(m.get("role")) for m in messages if isinstance(m, dict))
    fmt = '{"type":"json_object"}' if response_format else "omitted"
    _warn(
        f"request: model={model!r}, temperature=0.1, response_format={fmt}, roles=[{roles}]"
    )
    for index, message in enumerate(messages):
        role = message.get("role") if isinstance(message, dict) else "?"
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, (list, tuple)):
            _warn(f"request: message[{index}] ({role}) plain text, {len(str(content))} chars")
            continue
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else type(part).__name__
            if kind == "image_url":
                url = str((part.get("image_url") or {}).get("url", ""))
                head, sep, body = url.partition(";base64,")
                mime = head[len("data:") :] if head.startswith("data:") else "<unknown>"
                _warn(
                    f"request: message[{index}] ({role}) image part, "
                    f"mime={mime or '<unknown>'}, base64 length={len(body) if sep else 0}"
                )
            else:
                text = part.get("text", "") if isinstance(part, dict) else str(part)
                _warn(
                    f"request: message[{index}] ({role}) {kind} part, {len(str(text))} chars"
                )
    try:
        dump = json.dumps(_redacted_messages(messages), ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        dump = f"<could not serialize the payload: {exc}>"
    _warn("request payload (image bytes omitted):\n" + dump)


def _log_reply(raw: str) -> None:
    """Print the model's raw reply on stderr, before any JSON extraction."""
    body = raw if len(raw) <= _VERBOSE_REPLY_CHARS else (
        raw[:_VERBOSE_REPLY_CHARS] + f"\n... [{len(raw) - _VERBOSE_REPLY_CHARS} more chars]"
    )
    _warn(f"raw model reply ({len(raw)} chars):\n{body}")


def _looks_like_response_format_rejection(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__}: {exc}".lower()
    return any(
        token in text
        for token in (
            "response_format",
            "json_object",
            "unexpected keyword",
            "unsupported parameter",
            "unsupported_value",
            "invalid_request_error",
            "400",
        )
    )


def _create_completion(
    client: Any, model: str, messages: list[dict], verbose: bool = False
) -> Any:
    """Call the chat completions API, retrying once without ``response_format``.

    Some LiteLLM-proxied providers reject ``response_format`` with a 400 (or the
    param is not in their signature at all), so the structured-output request is
    attempted first and a plain request is used as a fallback.
    """
    try:
        create = client.chat.completions.create
    except AttributeError as exc:
        raise VisionAnalysisError(
            "The supplied client does not look like an OpenAI client "
            "(missing .chat.completions.create)."
        ) from exc

    if verbose:
        _log_request(model, messages, response_format=True)

    try:
        return create(
            model=model,
            messages=messages,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
    except Exception as first_exc:  # noqa: BLE001 - any client/proxy error is retried once
        reason = "rejected response_format" if _looks_like_response_format_rejection(first_exc) else "failed"
        _warn(
            f"model '{model}' {reason} "
            f"({first_exc.__class__.__name__}: {first_exc}); retrying without "
            "response_format={'type':'json_object'}"
        )
        if verbose:
            _log_request(model, messages, response_format=False)
        try:
            return create(model=model, messages=messages, temperature=0.1)
        except Exception as second_exc:  # noqa: BLE001
            base_url = getattr(config, "LITELLM_BASE_URL", "<unset>")
            raise VisionAnalysisError(
                f"Vision request to model '{model}' failed.\n"
                f"  first attempt (with response_format): {first_exc.__class__.__name__}: {first_exc}\n"
                f"  retry (plain): {second_exc.__class__.__name__}: {second_exc}\n"
                f"Check that {base_url} is reachable and that the key "
                f"({getattr(config, 'KEY_SOURCE', 'LITELLM_API_KEY')}) and the "
                "model name are correct."
            ) from second_exc


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def analyze_image(
    image_path: str | Path,
    model: str | None = None,
    client: Any = None,
    extra_instructions: str | None = None,
    verbose: bool = False,
) -> SlideSpec:
    """Analyze *image_path* with a Vision LLM and return a validated ``SlideSpec``.

    Args:
        image_path: Path to a PNG / JPEG / WebP image.
        model: Model name; defaults to ``config.DEFAULT_MODEL``.
        client: OpenAI-compatible client (injectable for tests); defaults to
            ``config.get_client()``.
        extra_instructions: Extra text appended to the user prompt.
        verbose: Print payload diagnostics on stderr - the request shape and the
            (image-redacted) request payload, plus the model's raw reply before
            any JSON extraction. This is what ``main.py -v`` promises.

    Individually malformed elements are dropped rather than fatal: the usable
    elements are returned, each loss is warned about on stderr and listed in
    ``spec.dropped``. Losing *every* element is not a partial success (it would
    build a blank deck), so that raises.

    Raises:
        VisionAnalysisError: for any encoding, API, JSON or validation failure,
            including a reply that yields no usable elements.
    """
    b64, mime = encode_image(image_path)
    model_name = model or getattr(config, "DEFAULT_MODEL", "gemini-flash")

    if client is None:
        try:
            client = config.get_client()
        except getattr(config, "MissingApiKeyError", ()) as exc:
            # The key is missing, not wrong: say only that, once. The full
            # "export LITELLM_API_KEY=..." hint travels with the cause, which
            # the CLI prints after this message (main._diagnose recognizes the
            # exception TYPE), so repeating it here would just double it up.
            raise VisionAnalysisError(
                "No API key is configured, so the image cannot be analyzed "
                "(--from-json needs no key)."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise VisionAnalysisError(
                f"Could not create the LLM client ({exc.__class__.__name__}: {exc}). "
                "Is the 'openai' package installed and LITELLM_BASE_URL / "
                "LITELLM_API_KEY set correctly?"
            ) from exc

    messages = build_messages(b64, mime, extra_instructions)
    response = _create_completion(client, model_name, messages, verbose=verbose)
    raw = _message_content(response)
    if verbose:
        _log_reply(raw)
    payload = extract_json(raw)

    try:
        data = json.loads(payload)
    except ValueError as exc:
        preview = payload[:300].replace("\n", " ")
        raise VisionAnalysisError(
            f"Model '{model_name}' returned invalid JSON ({exc}). "
            f"First 300 chars of the extracted payload: {preview!r}"
        ) from exc

    if not isinstance(data, dict):
        raise VisionAnalysisError(
            f"Expected a JSON object from model '{model_name}' but got "
            f"{type(data).__name__}. Re-run with --verbose to inspect the payload."
        )

    try:
        spec = SlideSpec.model_validate(normalize_spec_payload(data))
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError and friends
        # Every element failed on its own: nothing survives to build, so this is
        # a hard failure - but it can say exactly WHICH elements went wrong.
        reasons = all_dropped_reasons(exc)
        if reasons:
            listing = "\n".join(f"  - {reason}" for reason in reasons)
            raise VisionAnalysisError(
                f"Model '{model_name}' described {len(reasons)} element(s) but not one "
                "of them matched the slide schema, so the deck would have been "
                f"blank:\n{listing}\n"
                "Fix: re-run (models are non-deterministic), sharpen the request with "
                '--extra "...", or try another --model; re-run with -v to see the raw '
                "reply."
            ) from exc
        raise VisionAnalysisError(
            f"Model '{model_name}' returned JSON that does not match the slide schema "
            f"({exc.__class__.__name__}: {exc}). Try a stronger vision model or add "
            "guidance with --extra."
        ) from exc

    # A PARTIAL drop is a success: the good elements still build. Say what was
    # lost on stderr (stdout belongs to the caller's own output).
    if spec.dropped:
        for reason in spec.dropped:
            _warn(f"dropped {reason}")
        total = len(spec.dropped) + len(spec.elements)
        _warn(
            f"{len(spec.dropped)} of {total} elements in the model's reply could not be "
            f"parsed and were skipped; building the remaining {len(spec.elements)}."
        )

    # A spec with no elements builds a perfectly valid, perfectly blank deck.
    # `extra="ignore"` means ANY JSON object validates, so this is the only place
    # that can tell "the model answered something else" from "success".
    if not spec.elements:
        preview = raw.strip()[:200].replace("\n", " ")
        raise VisionAnalysisError(
            f"Model '{model_name}' returned no usable slide elements, so the deck "
            "would have been blank. The reply may have been truncated, or it may "
            "describe something other than the slide layout. Fix: re-run (models are "
            'non-deterministic), sharpen the request with --extra "...", or try '
            "another --model; re-run with -v to see the raw reply. First 200 chars of "
            f"the reply: {preview!r}"
        )

    return spec
