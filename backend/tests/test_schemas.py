"""Schema-level tests: colour normalization, bbox coercion, union parsing.

These are the guard rails that keep a sloppy LLM answer from taking the whole
conversion down: every field that the model can get wrong must either be
coerced into something drawable or degrade to ``None``.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app import mock_data
from app.schemas import (
    BBox,
    ElementsAllDroppedError,
    ShapeElement,
    SlideSpec,
    TableElement,
    TextElement,
    all_dropped_reasons,
    llm_json_schema,
    normalize_hex_color,
    normalize_spec_payload,
    validate_elements,
)


# ---------------------------------------------------------------------------
# normalize_spec_payload: shapes that would otherwise validate into a BLANK spec
# ---------------------------------------------------------------------------
_ELEMENT = {"type": "text", "bbox": [0, 0, 500, 100], "content": "Title"}
_TABLE_EL = {"type": "table", "bbox": [0, 200, 500, 300], "data": [["a", "b"]]}


class TestNormalizeSpecPayload:
    """``extra="ignore"`` + ``elements=[]`` means ANY object validates.

    So a payload that merely *contains* the slide must be recovered here, or the
    user silently gets a blank deck.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            {"elements": [_ELEMENT]},
            {"background_color": "#FFFFFF", "elements": []},
            {"background_color": "#FFFFFF"},
        ],
    )
    def test_a_spec_shaped_payload_is_returned_untouched(self, payload):
        assert normalize_spec_payload(payload) is payload

    def test_a_bare_element_array_becomes_a_spec(self):
        assert normalize_spec_payload([_ELEMENT, _TABLE_EL]) == {
            "elements": [_ELEMENT, _TABLE_EL]
        }

    @pytest.mark.parametrize("key", ["slide", "result", "response", "anything"])
    def test_an_envelope_key_is_unwrapped(self, key):
        inner = {"background_color": "#FFFFFF", "elements": [_ELEMENT]}
        assert normalize_spec_payload({key: inner}) == inner

    def test_a_doubly_wrapped_spec_is_unwrapped(self):
        inner = {"elements": [_ELEMENT]}
        assert normalize_spec_payload({"data": {"slide": inner}}) == inner

    def test_an_envelope_holding_an_element_array_is_unwrapped(self):
        assert normalize_spec_payload({"result": [_ELEMENT]}) == {"elements": [_ELEMENT]}

    @pytest.mark.parametrize(
        "payload",
        [
            {"a": 1},
            {"note": "dense slide", "confidence": 0.9},
            [1, 2, 3],
            ["a", "b"],
            [],
            "just a string",
            None,
            42,
        ],
    )
    def test_unrecognizable_payloads_are_returned_unchanged(self, payload):
        assert normalize_spec_payload(payload) == payload

    def test_the_recovered_payload_actually_validates(self):
        spec = SlideSpec.model_validate(normalize_spec_payload({"slide": {"elements": [_ELEMENT]}}))
        assert len(spec.elements) == 1
        spec = SlideSpec.model_validate(normalize_spec_payload([_ELEMENT, _TABLE_EL]))
        assert [el.type for el in spec.elements] == ["text", "table"]


class TestABareArrayIsRecoveredEvenWithOneBadEntry:
    """Recognition ran BEFORE the per-element isolation and was all-or-nothing.

    So one unrecognized entry in a bare array made the recovery fail,
    :class:`SlideSpec` got a ``list``, died with ``model_type``, and the user
    lost every good element AND every per-element reason - while the identical
    content wrapped in ``{"elements": [...]}`` built fine. One element-looking
    entry is now enough; isolation drops the rest with reasons.
    """

    _BAD = {"type": "hologram", "bbox": [1, 1, 9, 9]}

    def test_one_unknown_type_no_longer_sinks_the_whole_array(self):
        payload = normalize_spec_payload([_ELEMENT, self._BAD, _TABLE_EL])
        assert payload == {"elements": [_ELEMENT, self._BAD, _TABLE_EL]}

    def test_it_then_validates_into_the_good_elements_plus_a_reason(self):
        spec = SlideSpec.model_validate(
            normalize_spec_payload([_ELEMENT, self._BAD, _TABLE_EL])
        )
        assert [el.type for el in spec.elements] == ["text", "table"]
        assert len(spec.dropped) == 1 and "hologram" in spec.dropped[0]

    def test_a_bare_array_matches_the_wrapped_form_exactly(self):
        """The two spellings of the same content may not disagree."""
        items = [_ELEMENT, self._BAD, _TABLE_EL]
        bare = SlideSpec.model_validate(normalize_spec_payload(items))
        wrapped = SlideSpec.model_validate({"elements": items})
        assert bare.model_dump() == wrapped.model_dump()
        assert bare.dropped == wrapped.dropped

    @pytest.mark.parametrize(
        "junk",
        [
            {"type": "hologram", "bbox": [1, 1, 9, 9]},
            {"note": "the model chatting"},
            "a stray string",
            None,
            42,
            [],
        ],
    )
    def test_any_single_junk_neighbour_is_tolerated(self, junk):
        spec = SlideSpec.model_validate(normalize_spec_payload([_ELEMENT, junk]))
        assert [el.type for el in spec.elements] == ["text"]
        assert len(spec.dropped) == 1

    def test_a_leading_junk_entry_is_tolerated_too(self):
        spec = SlideSpec.model_validate(normalize_spec_payload([self._BAD, _ELEMENT]))
        assert [el.type for el in spec.elements] == ["text"]

    @pytest.mark.parametrize(
        "payload", [[1, 2, 3], ["a", "b"], [], [None], [{"note": "hi"}], [[1], [2]]]
    )
    def test_a_list_that_is_clearly_not_elements_is_still_a_bad_spec(self, payload):
        """Nothing element-looking in there: it must NOT be swallowed."""
        assert normalize_spec_payload(payload) == payload
        with pytest.raises(ValidationError) as excinfo:
            SlideSpec.model_validate(normalize_spec_payload(payload))
        assert excinfo.value.errors()[0]["type"] == "model_type"


class TestAJunkSiblingListMayNotHijackTheRealSpec:
    """The permissive ``any()`` recognition belongs to a BARE array only.

    Consulted during the envelope-key descent it made any nested list holding
    ONE element-shaped dict "the element array", so the sibling key carrying the
    actual slide was never examined: the real elements vanished and the
    diagnostics described the stray entry instead. The wrapper-key descent now
    runs first and a nested list is only a fallback.
    """

    #: The real slide, under a sibling key of the junk list.
    SLIDE = {
        "background_color": "#FFFFFF",
        "elements": [
            {"type": "text", "bbox": [100, 100, 800, 120], "content": "QUARTERLY REVENUE"},
            {
                "type": "shape",
                "bbox": [100, 300, 800, 200],
                "kind": "rectangle",
                "fill_color": "#0044AA",
            },
        ],
    }

    #: Variant A: the stray entry is a VALID element, so it used to become the
    #: whole slide (exit 0, one 0.12x0.07in text box, wrong diagnostics).
    STRAY_VALID = {"type": "text", "bbox": [1, 1, 9, 9], "content": "a stray note"}

    #: Variant B: the stray entry is INVALID, so every entry of the junk list
    #: dropped and ElementsAllDroppedError fired - exit 1, no deck at all.
    STRAY_BROKEN = {"type": "text", "bbox": [1, 1], "content": "stray"}

    def _payload(self, stray):
        return {"notes": [1, 2, stray], "slide": dict(self.SLIDE)}

    @pytest.mark.parametrize("stray_name", ["STRAY_VALID", "STRAY_BROKEN"])
    def test_the_sibling_key_holding_the_slide_wins(self, stray_name):
        payload = self._payload(getattr(self, stray_name))
        assert normalize_spec_payload(payload) == self.SLIDE

    @pytest.mark.parametrize("stray_name", ["STRAY_VALID", "STRAY_BROKEN"])
    def test_both_real_elements_survive_and_the_stray_is_not_in_the_spec(self, stray_name):
        spec = SlideSpec.model_validate(
            normalize_spec_payload(self._payload(getattr(self, stray_name)))
        )
        assert [el.type for el in spec.elements] == ["text", "shape"]
        assert spec.elements[0].content == "QUARTERLY REVENUE"
        assert spec.background_color == "#FFFFFF"
        assert spec.dropped == [], "the junk list may not invent drops either"

    def test_it_holds_when_the_junk_list_is_nested_deeper(self):
        payload = {"meta": {"notes": [1, 2, self.STRAY_VALID]}, "slide": dict(self.SLIDE)}
        assert normalize_spec_payload(payload) == self.SLIDE

    def test_a_junk_list_beside_a_clean_nested_array_does_not_win_either(self):
        payload = {"notes": [1, 2, self.STRAY_VALID], "result": [_ELEMENT, _TABLE_EL]}
        recovered = normalize_spec_payload(payload)
        assert recovered in (
            {"elements": [_ELEMENT, _TABLE_EL]},
            {"elements": [1, 2, self.STRAY_VALID]},
        )
        # The clean, fully element-shaped array is the one that must be picked.
        assert recovered == {"elements": [_ELEMENT, _TABLE_EL]}

    def test_a_nested_clean_element_array_still_works(self):
        """The other direction: this recovery may not be lost to the fix."""
        assert normalize_spec_payload({"result": [_ELEMENT]}) == {"elements": [_ELEMENT]}
        assert normalize_spec_payload({"data": {"slide": {"elements": [_ELEMENT]}}}) == {
            "elements": [_ELEMENT]
        }

    def test_a_bare_array_with_one_bad_entry_still_degrades_per_element(self):
        """And the direction the ``any()`` rule was introduced for."""
        bad = {"type": "hologram", "bbox": [1, 1, 9, 9]}
        spec = SlideSpec.model_validate(normalize_spec_payload([_ELEMENT, bad, _TABLE_EL]))
        assert [el.type for el in spec.elements] == ["text", "table"]
        assert len(spec.dropped) == 1 and "hologram" in spec.dropped[0]


class TestTextContentIsRequired:
    """A text element with no ``content`` is an invisible element.

    ``llm_json_schema()`` has always declared ``content`` required for text (and
    a missing table ``data`` has always been dropped), but ``content`` defaulted
    to ``""``: the element validated, rendered as an empty text box and the CLI
    reported success with an empty stderr - a visually blank slide handed over as
    a good deck. Absent or ``null`` is now invalid; an explicit ``""`` is not.
    """

    GOOD = {"type": "text", "bbox": [0, 0, 500, 100], "content": "Title"}

    def test_missing_content_is_dropped_with_a_reason(self):
        kept, reasons = validate_elements([{"type": "text", "bbox": [100, 100, 400, 100]}])
        assert kept == []
        assert reasons == ["element 0 (text): content - Field required"]

    def test_null_content_is_dropped_with_a_reason(self):
        kept, reasons = validate_elements(
            [{"type": "text", "bbox": [100, 100, 400, 100], "content": None}]
        )
        assert kept == []
        assert len(reasons) == 1
        assert reasons[0].startswith("element 0 (text): content - ")
        assert "null" in reasons[0]

    def test_an_explicit_empty_string_is_still_a_valid_text_box(self):
        kept, reasons = validate_elements(
            [{"type": "text", "bbox": [100, 100, 400, 100], "content": ""}]
        )
        assert reasons == []
        assert [el.content for el in kept] == [""]
        assert TextElement(type="text", bbox=[0, 0, 10, 10], content="").content == ""

    def test_a_contentless_text_element_costs_only_itself(self):
        spec = SlideSpec.model_validate(
            {
                "elements": [
                    self.GOOD,
                    {"type": "text", "bbox": [100, 300, 400, 100]},
                    _TABLE_EL,
                ]
            }
        )
        assert [el.type for el in spec.elements] == ["text", "table"]
        assert spec.dropped == ["element 1 (text): content - Field required"]

    def test_a_slide_of_nothing_but_contentless_text_is_a_loud_total_failure(self):
        with pytest.raises(ValidationError) as excinfo:
            SlideSpec.model_validate(
                {
                    "elements": [
                        {"type": "text", "bbox": [100, 100, 400, 100]},
                        {"type": "text", "bbox": [100, 300, 400, 100], "content": None},
                    ]
                }
            )
        reasons = all_dropped_reasons(excinfo.value)
        assert reasons is not None and len(reasons) == 2
        assert all("content" in reason for reason in reasons)

    def test_the_llm_schema_and_the_model_agree(self):
        text_schema = next(
            entry
            for entry in llm_json_schema()["properties"]["elements"]["items"]["oneOf"]
            if entry["title"] == "text"
        )
        assert "content" in text_schema["required"]
        assert "content" in TextElement.model_fields
        assert TextElement.model_fields["content"].is_required()

    def test_the_mock_spec_is_unaffected(self):
        spec = mock_data.sample_spec()
        assert spec.dropped == []
        assert all(el.content for el in spec.elements if el.type == "text")


class TestDroppedIsNeverTakenFromTheInput:
    """``dropped`` is tool-generated state; a payload must not be able to forge it.

    It used to survive the before-validator's early-return paths, so a spec file
    - or the model's own reply - could hand over a ``dropped`` list that the CLI
    printed as its own parse diagnostic, inventing losses that never happened.
    """

    FORGED = ["element 0 (text): TOTALLY MADE UP"]

    def test_the_reproduction_no_longer_reports_a_fabricated_drop(self):
        spec = SlideSpec.model_validate(
            {"background_color": "#FFFFFF", "dropped": list(self.FORGED)}
        )
        assert spec.dropped == []
        assert spec.background_color == "#FFFFFF"

    @pytest.mark.parametrize(
        "payload",
        [
            # no "elements" key at all: the early-return path
            {"background_color": "#FFFFFF"},
            # "elements" present but not a list: the other early-return path
            {"elements": "nope"},
            # an empty list: no isolation work to do
            {"elements": []},
            # the success path
            {"elements": [_ELEMENT]},
        ],
    )
    def test_every_path_through_the_validator_strips_it(self, payload):
        data = {**payload, "dropped": list(self.FORGED)}
        try:
            spec = SlideSpec.model_validate(data)
        except ValidationError as exc:
            # {"elements": "nope"} must still fail exactly as before - and must
            # not have smuggled the forged list in on the way.
            assert exc.errors()[0]["type"] == "list_type"
            return
        assert spec.dropped == []

    def test_a_forged_drop_cannot_ride_along_with_a_real_one(self):
        spec = SlideSpec.model_validate(
            {"elements": [_ELEMENT, _TABLE_NO_DATA], "dropped": list(self.FORGED)}
        )
        assert spec.dropped == ["element 1 (table): data - Field required"]

    def test_the_constructor_cannot_seed_it_either(self):
        assert SlideSpec(dropped=list(self.FORGED)).dropped == []
        assert SlideSpec(elements=[_ELEMENT], dropped=list(self.FORGED)).dropped == []

    def test_it_cannot_arrive_through_a_recovered_bare_array_either(self):
        spec = SlideSpec.model_validate(normalize_spec_payload([_ELEMENT]))
        assert spec.dropped == []

    def test_json_input_cannot_seed_it(self):
        blob = json.dumps({"elements": [_ELEMENT], "dropped": list(self.FORGED)})
        assert SlideSpec.model_validate_json(blob).dropped == []

    def test_an_all_dropped_payload_still_reports_only_real_reasons(self):
        with pytest.raises(ValidationError) as excinfo:
            SlideSpec.model_validate(
                {"elements": [_TABLE_NO_DATA], "dropped": list(self.FORGED)}
            )
        assert all_dropped_reasons(excinfo.value) == [
            "element 0 (table): data - Field required"
        ]


# ---------------------------------------------------------------------------
# normalize_hex_color
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # short form expansion
        ("#FFF", "#FFFFFF"),
        ("fff", "#FFFFFF"),
        ("#abc", "#AABBCC"),
        ("0f8", "#00FF88"),
        # long form / case / missing hash
        ("FFFFFF", "#FFFFFF"),
        ("#ffffff", "#FFFFFF"),
        ("#1F3B73", "#1F3B73"),
        ("1f3b73", "#1F3B73"),
        ("#ff00ff", "#FF00FF"),
        ("#000000", "#000000"),
        # 8 digit RGBA -> alpha dropped
        ("#12345678", "#123456"),
        ("#ffffff00", "#FFFFFF"),
        # garbage / empty -> None, never an exception
        (None, None),
        ("", None),
        ("   ", None),
        ("garbage", None),
        ("#GGGGGG", None),
        ("#GGG", None),
        ("#12345", None),
        ("#1234567", None),
        ("#", None),
        ("rgb(1,2,3)", None),
        ("transparent", None),
    ],
)
def test_normalize_hex_color(raw, expected):
    assert normalize_hex_color(raw) == expected


@pytest.mark.parametrize("raw", [object(), [], {}, ["#fff"], 3.5, True])
def test_normalize_hex_color_never_raises_on_odd_types(raw):
    """Non-string junk must be swallowed, not propagated as an exception."""
    assert normalize_hex_color(raw) is None


def test_bad_colour_degrades_to_none_on_every_colour_field():
    shape = ShapeElement(
        type="shape", bbox=[0, 0, 100, 100], fill_color="not-a-colour", border_color="#GGG"
    )
    assert shape.fill_color is None
    assert shape.border_color is None

    text = TextElement(type="text", bbox=[0, 0, 100, 100], content="", font_color="nope")
    assert text.font_color is None

    table = TableElement(type="table", bbox=[0, 0, 100, 100], data=[["a"]], header_bg_color="???")
    assert table.header_bg_color is None


def test_good_colour_is_normalized_on_fields():
    shape = ShapeElement(type="shape", bbox=[0, 0, 10, 10], fill_color="abc", border_color="1f3b73")
    assert shape.fill_color == "#AABBCC"
    assert shape.border_color == "#1F3B73"
    assert SlideSpec(background_color="fff").background_color == "#FFFFFF"


# ---------------------------------------------------------------------------
# BBox
# ---------------------------------------------------------------------------
def test_bbox_from_keywords():
    box = BBox(x=1, y=2, w=3, h=4)
    assert (box.x, box.y, box.w, box.h) == (1, 2, 3, 4)


@pytest.mark.parametrize(
    "raw",
    [
        [10, 20, 30, 40],
        (10, 20, 30, 40),
        [10.0, 20.0, 30.0, 40.0],
        ["10", "20", "30", "40"],
        {"x": 10, "y": 20, "w": 30, "h": 40},
        {"x": "10", "y": 20.0, "w": 30, "h": "40"},
    ],
)
def test_bbox_accepts_lists_tuples_dicts_and_coerces(raw):
    box = BBox.model_validate(raw)
    assert (box.x, box.y, box.w, box.h) == (10, 20, 30, 40)
    assert all(isinstance(v, int) for v in (box.x, box.y, box.w, box.h))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # upper clamp
        ([2000, 5000, 4000, 9999], (1000, 1000, 1000, 1000)),
        # lower clamp; w/h floor at 1 so degenerate boxes still render
        ([-50, -1, 0, 0], (0, 0, 1, 1)),
        ([-10.5, -0.2, -3, -7], (0, 0, 1, 1)),
        # floats above the ceiling
        ([1500.0, 1000.0, 1200.0, 1001.0], (1000, 1000, 1000, 1000)),
        # in-range values are untouched
        ([0, 0, 1000, 1000], (0, 0, 1000, 1000)),
    ],
)
def test_bbox_clamps_to_coordinate_space(raw, expected):
    box = BBox.model_validate(raw)
    assert (box.x, box.y, box.w, box.h) == expected


def test_bbox_inside_element_accepts_raw_list():
    """The LLM emits ``"bbox": [x, y, w, h]`` - it must work nested, too."""
    el = TextElement(type="text", bbox=[100, 200, 300, 400], content="hi")
    assert isinstance(el.bbox, BBox)
    assert (el.bbox.x, el.bbox.w) == (100, 300)


# ---------------------------------------------------------------------------
# BBox: an UNREADABLE component is not a default, it invalidates the element
# ---------------------------------------------------------------------------
class TestIncompleteBboxIsRejected:
    """A defaulted-away w/h rendered as an invisible 0.05in sliver.

    The element validated "successfully", the builder drew a box 17 characters
    of 12pt text could never fit in, and the CLI reported success with an empty
    stderr - the one case where a broken deck was handed over as a good one. All
    four components must be PRESENT and parseable or the element is invalid, so
    the existing per-element isolation drops it with a reason the user sees.
    """

    #: Every shape of unreadable bbox, and the component(s) that are unreadable.
    UNREADABLE = {
        "missing h (dict)": ({"x": 100, "y": 200, "w": 600}, ["h"]),
        "empty list": ([], ["x", "y", "w", "h"]),
        "two-item list": ([5, 5], ["w", "h"]),
        "three-item list": ([100, 200, 600], ["h"]),
        "missing w and h (dict)": ({"x": 100, "y": 200}, ["w", "h"]),
        "no recognizable keys": ({"q": 1}, ["x", "y", "w", "h"]),
        "unrecognized key spelling": ({"left_pct": 10}, ["x", "y", "w", "h"]),
        "all four explicitly null": (
            {"x": None, "y": None, "w": None, "h": None},
            ["x", "y", "w", "h"],
        ),
        "unparseable strings": ({"x": "a", "y": "b", "w": "c", "h": "d"}, ["x", "y", "w", "h"]),
        "list with a junk entry": ([10, 20, "wide", 40], ["w"]),
    }

    @pytest.mark.parametrize("label", sorted(UNREADABLE))
    def test_the_bbox_itself_refuses_to_validate(self, label):
        raw, _components = self.UNREADABLE[label]
        with pytest.raises(ValidationError):
            BBox.model_validate(raw)

    @pytest.mark.parametrize("label", sorted(UNREADABLE))
    def test_the_element_is_dropped_and_the_reason_names_the_component(self, label):
        raw, components = self.UNREADABLE[label]
        kept, reasons = validate_elements(
            [{"type": "text", "bbox": raw, "content": "QUARTERLY REVENUE"}, _GOOD_SHAPE]
        )
        assert [el.type for el in kept] == ["shape"], "only the bad element may be lost"
        assert len(reasons) == 1
        reason = reasons[0]
        assert reason.startswith("element 0 (text): bbox - "), reason
        for component in components:
            assert repr(component) in reason, reason
        assert "missing or unreadable" in reason, reason

    def test_the_documented_reason_wording_is_exact(self):
        _kept, (reason,) = validate_elements(
            [{"type": "text", "bbox": {"x": 100, "y": 200, "w": 600}, "content": "hi"}]
        )
        assert reason == "element 0 (text): bbox - 'h' is missing or unreadable"

    def test_several_unreadable_components_are_all_named(self):
        _kept, (reason,) = validate_elements([{"type": "text", "bbox": [5, 5], "content": "hi"}])
        assert reason == "element 0 (text): bbox - 'w', 'h' are missing or unreadable"

    @pytest.mark.parametrize("raw", [None, "top left", 42, True])
    def test_a_bbox_that_is_not_a_container_still_fails_as_before(self, raw):
        """These were already dropped; the tightening must not change that."""
        _kept, (reason,) = validate_elements([{"type": "text", "bbox": raw, "content": "hi"}])
        assert reason.startswith("element 0 (text): bbox")

    def test_a_partial_bbox_no_longer_builds_a_silent_sliver(self):
        """End of the defect: the spec reports the loss instead of succeeding."""
        spec = SlideSpec.model_validate(
            {
                "elements": [
                    {"type": "text", "bbox": {"x": 100, "y": 200, "w": 600}, "content": "hi"},
                    _GOOD_SHAPE,
                ]
            }
        )
        assert [el.type for el in spec.elements] == ["shape"]
        assert spec.dropped == ["element 0 (text): bbox - 'h' is missing or unreadable"]

    def test_an_all_unreadable_slide_is_a_loud_total_failure(self):
        with pytest.raises(ValidationError) as excinfo:
            SlideSpec.model_validate(
                {"elements": [{"type": "text", "bbox": [5, 5], "content": "hi"}]}
            )
        assert all_dropped_reasons(excinfo.value) == [
            "element 0 (text): bbox - 'w', 'h' are missing or unreadable"
        ]


class TestBboxToleranceIsUnchanged:
    """Tightening "absent" must not narrow what a PRESENT value may look like."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ([10, 20, 30, 40], (10, 20, 30, 40)),
            ((10, 20, 30, 40), (10, 20, 30, 40)),
            ([10.4, 20.5, 30.0, 40.0], (10, 21, 30, 40)),
            (["10", "20px", " 30 ", "40.2"], (10, 20, 30, 40)),
            ({"x": 10, "y": 20, "w": 30, "h": 40}, (10, 20, 30, 40)),
            ({"left": 10, "top": 20, "width": 30, "height": 40}, (10, 20, 30, 40)),
            # out of range but present: still clamped, never rejected
            ([2000, 5000, 4000, 9999], (1000, 1000, 1000, 1000)),
            ([-50, -1, 0, 0], (0, 0, 1, 1)),
            ([-10.5, -0.2, -3, -7], (0, 0, 1, 1)),
            # corner coordinates: w/h are DERIVED, which counts as present
            ({"x": 10, "y": 20, "x1": 40, "y1": 60}, (10, 20, 30, 40)),
            ({"left": 10, "top": 20, "right": 40, "bottom": 60}, (10, 20, 30, 40)),
        ],
    )
    def test_present_values_are_still_coerced_and_clamped(self, raw, expected):
        box = BBox.model_validate(raw)
        assert box.as_tuple() == expected
        assert all(isinstance(value, int) for value in box.as_tuple())

    def test_a_five_item_list_still_ignores_the_extra(self):
        assert BBox.model_validate([10, 20, 30, 40, 99]).as_tuple() == (10, 20, 30, 40)

    def test_a_dict_with_extra_keys_still_validates(self):
        raw = {"x": 10, "y": 20, "w": 30, "h": 40, "rotation": 12, "unit": "px"}
        assert BBox.model_validate(raw).as_tuple() == (10, 20, 30, 40)

    def test_an_already_built_bbox_passes_straight_through(self):
        box = BBox(x=1, y=2, w=3, h=4)
        assert BBox.model_validate(box) is box

    def test_a_validated_bbox_round_trips_through_its_own_dump(self):
        box = BBox.model_validate([10, 20, 30, 40])
        assert BBox.model_validate(box.model_dump()).as_tuple() == box.as_tuple()


# ---------------------------------------------------------------------------
# TableElement normalization
# ---------------------------------------------------------------------------
def test_ragged_rows_are_padded_with_empty_strings():
    table = TableElement(
        type="table",
        bbox=[0, 0, 100, 100],
        data=[["a", "b", "c"], ["d"], ["e", "f"]],
    )
    assert table.data == [["a", "b", "c"], ["d", "", ""], ["e", "f", ""]]
    assert len({len(row) for row in table.data}) == 1


def test_empty_table_data_still_yields_a_drawable_1x1():
    table = TableElement(type="table", bbox=[0, 0, 100, 100], data=[])
    assert len(table.data) >= 1
    assert len(table.data[0]) >= 1
    assert all(len(row) == len(table.data[0]) for row in table.data)


def test_table_defaults():
    table = TableElement(type="table", bbox=[0, 0, 100, 100], data=[["a"]])
    assert table.header_font_bold is True
    assert table.header_bg_color is None
    assert table.font_size is None


# ---------------------------------------------------------------------------
# element defaults + discriminated union
# ---------------------------------------------------------------------------
def test_element_defaults():
    shape = ShapeElement(type="shape", bbox=[0, 0, 10, 10])
    assert shape.kind == "rectangle"
    assert shape.fill_color is None and shape.border_color is None
    assert shape.border_width is None

    # `content` is REQUIRED (an absent one used to default to "" and render an
    # invisible text box, reported as a success); an explicit "" is still fine.
    text = TextElement(type="text", bbox=[0, 0, 10, 10], content="")
    assert text.content == ""
    assert text.align == "left"
    assert text.bold is False
    assert text.font_size is None

    spec = SlideSpec()
    assert spec.elements == []
    assert spec.background_color is None


def test_discriminated_union_dispatches_on_type():
    spec = SlideSpec.model_validate(
        {
            "elements": [
                {"type": "shape", "kind": "circle", "bbox": [0, 0, 10, 10]},
                {"type": "table", "bbox": [0, 0, 10, 10], "data": [["h"], ["v"]]},
                {"type": "text", "bbox": [0, 0, 10, 10], "content": "x"},
            ]
        }
    )
    assert [type(el) for el in spec.elements] == [ShapeElement, TableElement, TextElement]
    assert spec.elements[0].kind == "circle"


def test_unknown_element_type_is_rejected():
    with pytest.raises(ValidationError):
        SlideSpec.model_validate({"elements": [{"type": "hologram", "bbox": [0, 0, 1, 1]}]})


def test_unknown_shape_kind_never_survives_as_a_bogus_value():
    """Either rejected outright or degraded - never passed through to the builder.

    ``SHAPE_MAP`` only knows the three literals, so anything else must not
    reach it.  Which of the two behaviours the schema picks is its own call.
    """
    try:
        el = ShapeElement.model_validate(
            {"type": "shape", "kind": "trapezoid", "bbox": [0, 0, 1, 1]}
        )
    except ValidationError:
        return
    assert el.kind in {"rectangle", "rounded_rectangle", "circle"}


def test_unknown_align_never_survives_as_a_bogus_value():
    try:
        el = TextElement.model_validate(
            {"type": "text", "bbox": [0, 0, 1, 1], "align": "justify"}
        )
    except ValidationError:
        return
    assert el.align in {"left", "center", "right"}


def test_extra_keys_from_the_llm_are_ignored_not_fatal():
    spec = SlideSpec.model_validate(
        {
            "background_color": "#FFFFFF",
            "notes": "the model likes to chat",
            "confidence": 0.87,
            "elements": [
                {
                    "type": "text",
                    "bbox": [0, 0, 10, 10],
                    "content": "hi",
                    "font_family": "Calibri",  # not in our schema
                    "rotation": 12,
                }
            ],
        }
    )
    assert len(spec.elements) == 1
    assert not hasattr(spec, "notes")
    assert not hasattr(spec.elements[0], "font_family")


# ---------------------------------------------------------------------------
# per-element isolation: ONE malformed element may not sink the whole slide
# ---------------------------------------------------------------------------
_GOOD_TEXT = {"type": "text", "bbox": [1, 1, 100, 50], "content": "good one"}
_GOOD_SHAPE = {
    "type": "shape",
    "bbox": [2, 2, 100, 50],
    "kind": "circle",
    "fill_color": "#FF0000",
}
#: The real-world failure: a vision model omits one required key ("data").
_TABLE_NO_DATA = {"type": "table", "bbox": [1, 1, 900, 400], "header_bg_color": "#112233"}
_BOGUS_TYPE = {"type": "hologram", "bbox": [0, 0, 1, 1]}


class TestValidateElements:
    """``validate_elements`` is the whole fix: drop the entry, keep the slide."""

    def test_returns_the_good_elements_and_one_reason_per_drop(self):
        kept, reasons = validate_elements([_GOOD_TEXT, _TABLE_NO_DATA, _GOOD_SHAPE])
        assert [el.type for el in kept] == ["text", "shape"]
        assert reasons == ["element 1 (table): data - Field required"]

    def test_the_reason_names_the_index_the_declared_type_and_the_field(self):
        _kept, (reason,) = validate_elements([_TABLE_NO_DATA])
        assert "element 0" in reason
        assert "(table)" in reason
        assert "data" in reason and "required" in reason.lower()

    def test_a_clean_list_drops_nothing(self):
        kept, reasons = validate_elements([_GOOD_TEXT, _GOOD_SHAPE])
        assert len(kept) == 2 and reasons == []

    def test_none_and_empty_mean_nothing_to_do(self):
        assert validate_elements(None) == ([], [])
        assert validate_elements([]) == ([], [])

    @pytest.mark.parametrize(
        "junk", [None, "text", 42, [], {"bbox": [0, 0, 1, 1]}, _BOGUS_TYPE, object()]
    )
    def test_an_entry_that_is_not_an_element_is_dropped_never_raised(self, junk):
        kept, reasons = validate_elements([_GOOD_TEXT, junk])
        assert [el.type for el in kept] == ["text"]
        assert len(reasons) == 1 and reasons[0].startswith("element 1 (")

    def test_a_tuple_is_accepted_like_a_list(self):
        kept, reasons = validate_elements((_GOOD_TEXT, _TABLE_NO_DATA))
        assert len(kept) == 1 and len(reasons) == 1

    def test_already_validated_elements_pass_straight_through(self):
        element = TextElement(type="text", bbox=[0, 0, 10, 10], content="x")
        kept, reasons = validate_elements([element])
        assert kept == [element] and reasons == []


class TestPartialDropOnTheSpec:
    def test_one_bad_element_no_longer_takes_the_whole_spec_down(self):
        spec = SlideSpec.model_validate(
            {"elements": [_GOOD_TEXT, _TABLE_NO_DATA, _GOOD_SHAPE]}
        )
        assert [el.type for el in spec.elements] == ["text", "shape"]
        assert spec.dropped == ["element 1 (table): data - Field required"]

    def test_a_clean_spec_reports_nothing_dropped(self):
        spec = SlideSpec.model_validate({"elements": [_GOOD_TEXT, _GOOD_SHAPE]})
        assert spec.dropped == []
        assert mock_data.sample_spec().dropped == []
        assert SlideSpec().dropped == []

    def test_everything_dropped_raises_instead_of_blanking_the_deck(self):
        """Partial success continues; total failure must be loud."""
        with pytest.raises(ValidationError) as excinfo:
            SlideSpec.model_validate({"elements": [_TABLE_NO_DATA, _BOGUS_TYPE]})

        reasons = all_dropped_reasons(excinfo.value)
        assert reasons is not None, "the reasons must survive the pydantic wrapping"
        assert len(reasons) == 2
        assert reasons[0] == "element 0 (table): data - Field required"
        assert "hologram" in reasons[1]

    def test_an_explicitly_empty_element_list_is_not_an_all_dropped_error(self):
        spec = SlideSpec.model_validate({"background_color": "#FFF", "elements": []})
        assert spec.elements == [] and spec.dropped == []

    def test_a_non_list_elements_value_still_fails_exactly_as_before(self):
        with pytest.raises(ValidationError) as excinfo:
            SlideSpec.model_validate({"elements": {"type": "text", "bbox": [0, 0, 1, 1]}})
        assert excinfo.value.errors()[0]["type"] == "list_type"
        assert all_dropped_reasons(excinfo.value) is None


class TestAllDroppedReasons:
    def test_the_error_itself_carries_the_reasons(self):
        exc = ElementsAllDroppedError(["element 0 (table): data - Field required"])
        assert isinstance(exc, ValueError), "must wrap into a pydantic ValidationError"
        assert exc.reasons == ["element 0 (table): data - Field required"]
        assert "data" in str(exc)
        assert all_dropped_reasons(exc) == exc.reasons

    def test_unrelated_failures_are_not_mistaken_for_a_drop(self):
        assert all_dropped_reasons(None) is None
        assert all_dropped_reasons(RuntimeError("connection refused")) is None

    def test_a_wrapped_failure_is_still_recognized(self):
        with pytest.raises(RuntimeError) as excinfo:
            try:
                SlideSpec.model_validate({"elements": [_TABLE_NO_DATA]})
            except ValidationError as exc:
                raise RuntimeError("re-raised somewhere up the stack") from exc
        assert all_dropped_reasons(excinfo.value) == [
            "element 0 (table): data - Field required"
        ]


class TestDroppedStaysOutOfSerialization:
    """``--save-json`` must keep writing a clean spec that ``--from-json`` reads."""

    def _partial(self) -> SlideSpec:
        spec = SlideSpec.model_validate(
            {"elements": [_GOOD_TEXT, _TABLE_NO_DATA, _GOOD_SHAPE]}
        )
        assert spec.dropped, "precondition: this spec really did drop something"
        return spec

    def test_model_dump_never_mentions_dropped(self):
        assert "dropped" not in self._partial().model_dump()

    def test_model_dump_json_never_mentions_dropped(self):
        payload = json.loads(self._partial().model_dump_json())
        assert "dropped" not in payload
        assert set(payload) == {"background_color", "elements"}

    def test_the_saved_spec_round_trips_into_an_identical_clean_spec(self):
        spec = self._partial()
        again = SlideSpec.model_validate_json(spec.model_dump_json())
        assert again.model_dump() == spec.model_dump()
        assert again.dropped == [], "a re-read spec has nothing left to drop"


# ---------------------------------------------------------------------------
# llm_json_schema
# ---------------------------------------------------------------------------
def test_llm_json_schema_is_unchanged_by_the_dropped_field():
    """The hand-written LLM contract must not grow a 'dropped' key."""
    schema = llm_json_schema()
    assert "dropped" not in json.dumps(schema)
    assert set(schema["properties"]) == {"background_color", "elements"}
    assert schema["required"] == ["elements"]
    variants = schema["properties"]["elements"]["items"]["oneOf"]
    assert [variant["title"] for variant in variants] == ["shape", "table", "text"]
    assert variants[1]["required"] == ["type", "bbox", "data"]


def test_llm_json_schema_is_a_serializable_dict_describing_the_contract():
    schema = llm_json_schema()
    assert isinstance(schema, dict) and schema
    blob = json.dumps(schema).lower()
    for token in ("shape", "table", "text", "bbox", "elements"):
        assert token in blob, f"{token!r} missing from llm_json_schema()"


# ---------------------------------------------------------------------------
# mock_data: the end-to-end parse path
# ---------------------------------------------------------------------------
class TestMockData:
    def test_sample_spec_json_round_trips_into_the_same_spec(self):
        from_json = SlideSpec.model_validate_json(mock_data.SAMPLE_SPEC_JSON)
        from_obj = mock_data.sample_spec()
        assert from_json.model_dump() == from_obj.model_dump()

    def test_sample_spec_json_is_valid_json(self):
        payload = json.loads(mock_data.SAMPLE_SPEC_JSON)
        assert isinstance(payload["elements"], list) and payload["elements"]

    def test_sample_spec_exercises_every_element_type(self):
        els = mock_data.sample_spec().elements
        kinds = {type(el) for el in els}
        assert kinds == {ShapeElement, TableElement, TextElement}

    def test_sample_spec_exercises_every_shape_kind_and_alignment(self):
        els = mock_data.sample_spec().elements
        shape_kinds = {el.kind for el in els if isinstance(el, ShapeElement)}
        aligns = {el.align for el in els if isinstance(el, TextElement)}
        assert shape_kinds == {"rectangle", "rounded_rectangle", "circle"}
        assert aligns == {"left", "center", "right"}

    def test_sample_spec_has_a_multiline_textbox_and_an_unfilled_shape(self):
        els = mock_data.sample_spec().elements
        assert any(isinstance(el, TextElement) and "\n" in el.content for el in els)
        assert any(isinstance(el, ShapeElement) and el.fill_color is None for el in els)

    def test_sample_spec_table_is_3x3_with_a_header(self):
        table = next(el for el in mock_data.sample_spec().elements if isinstance(el, TableElement))
        assert len(table.data) == 3
        assert all(len(row) == 3 for row in table.data)
        assert table.header_font_bold is True
        assert table.header_bg_color is not None

    def test_sample_spec_respects_z_order_shapes_before_foreground(self):
        els = mock_data.sample_spec().elements
        last_shape = max(i for i, el in enumerate(els) if isinstance(el, ShapeElement))
        first_fg = min(
            i for i, el in enumerate(els) if isinstance(el, (TextElement, TableElement))
        )
        assert last_shape < first_fg, "background shapes must be emitted before foreground"

    def test_sample_spec_returns_independent_objects(self):
        a, b = mock_data.sample_spec(), mock_data.sample_spec()
        assert a is not b and a.elements[0] is not b.elements[0]
        a.elements.clear()
        assert b.elements, "mutating one sample must not affect the next"

    def test_every_bbox_stays_inside_the_coordinate_space(self):
        for el in mock_data.sample_spec().elements:
            assert 0 <= el.bbox.x <= 1000 and 0 <= el.bbox.y <= 1000
            assert el.bbox.x + el.bbox.w <= 1000, f"{el.type} overflows the slide width"
            assert el.bbox.y + el.bbox.h <= 1000, f"{el.type} overflows the slide height"
