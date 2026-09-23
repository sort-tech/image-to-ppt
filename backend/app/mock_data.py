"""A shared TEST FIXTURE: one hand-built sample spec, used across the suite.

This is **not** a product feature. Mock mode is gone - there is no ``--mock``
flag and no ``is_mock`` API field, and no code path a user can reach imports
this module. It stays because it is the suite's one canonical SlideSpec:
:func:`sample_spec` and :data:`SAMPLE_SPEC_JSON` are what
``tests/test_pptx_builder.py``, ``tests/test_schemas.py``,
``tests/test_vision_analyzer.py``, ``tests/test_preview.py``,
``tests/test_api.py`` and ``tests/test_cli.py`` build their decks and their
fake model replies from. Deleting it would delete that fixture, not a feature.

``tests/test_api.py::TestMockModeIsGone`` enforces that, scanning the whole of
``backend/`` - ``cli.py`` included, because ``--mock`` was a CLI flag - for an
import of this module. The README's keyless smoke test prints
:data:`SAMPLE_SPEC_JSON` from the *shell* to seed a spec file, which is a
contributor using the fixture directly and not a product code path importing
it; that is the line the guard draws.

Nothing in here touches the network or the filesystem.  The single source of
truth is :data:`_SPEC_DICT`; both :func:`sample_spec` (a validated
:class:`schemas.SlideSpec`) and :data:`SAMPLE_SPEC_JSON` (the raw JSON string)
are derived from it, so the object graph and the JSON can never drift apart.

The spec deliberately exercises *every* element type and the z-order rule:

===  ===================================  ===========================
 #   element                               why it is here
===  ===================================  ===========================
 1   rectangle (filled header band)        background layer
 2   rounded_rectangle (accent card)       background layer
 3   circle                                background layer / decoration
 4   rectangle with ``fill_color: null``   outline-only container: proves a
                                           shape can frame later content
                                           without hiding it
 5   text (bold, left)                     foreground
 6   text (right aligned)                  foreground
 7   text (multi-line, ``\\n`` separated)   foreground
 8   table (3x3, header row)               foreground
 9   text (center aligned)                 foreground
===  ===================================  ===========================
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .schemas import SlideSpec

__all__ = [
    "SAMPLE_SPEC_JSON",
    "sample_spec",
    "sample_llm_response_with_fences",
    "sample_spec_dict",
]


# --------------------------------------------------------------------------
# The spec, as the LLM would hand it to us: bboxes are [x, y, w, h] integers
# in the normalized 0..1000 coordinate space (see README).
# --------------------------------------------------------------------------
_SPEC_DICT: dict[str, Any] = {
    "background_color": "#FFFFFF",
    "elements": [
        # ---- background / container layer (painted first) ----------------
        {
            "type": "shape",
            "kind": "rectangle",
            "bbox": [0, 0, 1000, 150],
            "fill_color": "#1F3B73",
            "border_color": None,
            "border_width": None,
        },
        {
            "type": "shape",
            "kind": "rounded_rectangle",
            "bbox": [35, 200, 420, 330],
            "fill_color": "#F2F5FA",
            "border_color": "#1F3B73",
            "border_width": 1.5,
        },
        {
            "type": "shape",
            "kind": "circle",
            "bbox": [890, 30, 80, 90],
            "fill_color": "#FFB000",
            "border_color": "#FFFFFF",
            "border_width": 1.0,
        },
        {
            # No fill at all: the table drawn later must stay visible through
            # this frame.  This is the "fill_color is None -> fill.background()"
            # rendering path.
            "type": "shape",
            "kind": "rectangle",
            "bbox": [500, 190, 470, 350],
            "fill_color": None,
            "border_color": "#9AA9C2",
            "border_width": 1.0,
        },
        # ---- foreground layer (painted last, on top) ---------------------
        {
            "type": "text",
            "content": "Quarterly Platform Review",
            "bbox": [35, 40, 640, 70],
            "font_size": 32,
            "font_color": "#FFFFFF",
            "bold": True,
            "align": "left",
        },
        {
            "type": "text",
            "content": "FY24 - Confidential",
            "bbox": [660, 55, 210, 40],
            "font_size": 12,
            "font_color": "#DCE3F0",
            "bold": False,
            "align": "right",
        },
        {
            "type": "text",
            "content": (
                "Highlights\n"
                "- p95 latency down 38%\n"
                "- Two new regions live\n"
                "- Churn flat at 1.2%"
            ),
            "bbox": [65, 230, 365, 270],
            "font_size": 14,
            "font_color": "#10233F",
            "bold": False,
            "align": "left",
        },
        {
            "type": "table",
            "bbox": [525, 220, 420, 260],
            "data": [
                ["Region", "Users", "Growth"],
                ["EMEA", "18,400", "+12%"],
                ["APAC", "9,750", "+27%"],
            ],
            "header_bg_color": "#1F3B73",
            "header_font_bold": True,
            "font_size": 10,
        },
        {
            "type": "text",
            "content": "Source: internal telemetry, 2024-Q4",
            "bbox": [35, 880, 930, 60],
            "font_size": 10,
            "font_color": "#5A6B85",
            "bold": False,
            "align": "center",
        },
    ],
}


SAMPLE_SPEC_JSON: str = json.dumps(_SPEC_DICT, indent=2, ensure_ascii=False)
"""The sample spec as a raw JSON string (byte-for-byte the LLM contract)."""


def sample_spec_dict() -> dict[str, Any]:
    """Return a deep copy of the raw sample spec dictionary."""
    return copy.deepcopy(_SPEC_DICT)


def sample_spec() -> SlideSpec:
    """Return a freshly validated :class:`schemas.SlideSpec`.

    A new object is built on every call (from a deep copy of the source dict)
    so a caller mutating the result cannot poison other callers or tests.
    """
    return SlideSpec.model_validate(sample_spec_dict())


def sample_llm_response_with_fences() -> str:
    """:data:`SAMPLE_SPEC_JSON` as a chatty, markdown-fenced LLM reply.

    Used to exercise ``vision_analyzer.extract_json`` against the way models
    actually answer when they ignore the "pure JSON only" instruction.
    """
    return (
        "Sure! Here is the slide layout I extracted from the image.\n"
        "I kept the background shapes first so the text paints on top.\n\n"
        "```json\n"
        f"{SAMPLE_SPEC_JSON}\n"
        "```\n\n"
        "Let me know if you want the table transcribed differently."
    )


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    spec = sample_spec()
    print(SAMPLE_SPEC_JSON)
    print(f"\n{len(spec.elements)} elements validated OK", flush=True)
