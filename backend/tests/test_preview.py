"""Tests for the preview payload and ``POST /api/preview`` - fully offline.

The preview's whole value is that it describes the deck that was ACTUALLY
built, so almost every assertion here goes through ``pptx_builder`` for real and
then checks the payload against the deck's own shapes rather than against the
spec that was asked for. Where the builder deliberately overrides the spec - the
min-dimension floor, the off-slide clipping - the test asserts that the preview
follows the BUILDER, because a preview that echoed the request would be showing
the user a slide that does not exist.

Nothing here touches the network: a preview needs no model and no key.
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient
from pptx import Presentation
from pptx.enum.dml import MSO_THEME_COLOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches

from app import config, mock_data, pptx_builder
from app.main import app
from app.schemas import SlideSpec
from app.services import converter, preview as preview_module

#: The slide every deck is built on, in EMU - what the percentages divide by.
SLIDE_W_EMU = int(Inches(config.SLIDE_WIDTH_IN))
SLIDE_H_EMU = int(Inches(config.SLIDE_HEIGHT_IN))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def client() -> TestClient:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """A preview needs no key, and must not be able to use one by accident."""
    monkeypatch.setattr(config, "LITELLM_API_KEY", "", raising=False)


def preview_of(spec_payload) -> dict:
    """Build ``spec_payload`` for real and return the preview of the deck.

    Mirrors the order the product uses: bytes first, preview second (see
    ``converter.preview_for`` - reading the slide background mutates the XML).
    """
    spec = SlideSpec.model_validate(spec_payload)
    presentation, _bytes = converter.build_deck(spec)
    return preview_module.build_preview(presentation)


def built(spec_payload):
    """``(preview, slide)`` so a test can compare the payload with the deck."""
    spec = SlideSpec.model_validate(spec_payload)
    presentation, _bytes = converter.build_deck(spec)
    return preview_module.build_preview(presentation), presentation.slides[0]


def spec_dict() -> dict:
    return copy.deepcopy(mock_data.sample_spec_dict())


def one(element: dict, *, background=None) -> dict:
    """A spec holding exactly one element, so indices are unambiguous."""
    payload: dict = {"elements": [element]}
    if background is not None:
        payload["background_color"] = background
    return payload


def kinds(preview: dict) -> list[str]:
    return [shape["kind"] for shape in preview["shapes"]]


# =========================================================================== #
# the slide block
# =========================================================================== #
class TestSlideBlock:
    def test_it_reports_the_real_16_by_9_slide(self):
        preview = preview_of(one({"type": "text", "bbox": [0, 0, 100, 100], "content": "x"}))
        slide = preview["slide"]

        assert set(slide) == {"width_in", "height_in", "width_pt", "height_pt"}
        assert slide["width_in"] == 13.333
        assert slide["height_in"] == 7.5
        # 7.5in x 72 = exactly 540pt, which is what the UI sizes fonts against.
        assert slide["height_pt"] == 540.0
        # The width is 13.333in, not a round 960pt - the real value is reported
        # rather than a tidied one, because the geometry has to agree with it.
        assert slide["width_pt"] == pytest.approx(959.98, abs=0.01)

    def test_every_number_is_a_json_number_never_a_string(self):
        preview = preview_of(spec_dict())
        blob = json.dumps(preview)  # must be serializable at all
        assert blob

        for value in preview["slide"].values():
            assert isinstance(value, (int, float)) and not isinstance(value, bool)
        for shape in preview["shapes"]:
            for key in ("left_pct", "top_pct", "width_pct", "height_pct"):
                assert isinstance(shape[key], (int, float)), (key, shape[key])
                assert not isinstance(shape[key], bool)


# =========================================================================== #
# geometry
# =========================================================================== #
class TestGeometryPercentages:
    def test_a_full_bleed_box_is_zero_zero_one_hundred_one_hundred(self):
        preview = preview_of(
            one({"type": "shape", "kind": "rectangle", "bbox": [0, 0, 1000, 1000]})
        )
        shape = preview["shapes"][0]

        assert shape["left_pct"] == 0.0
        assert shape["top_pct"] == 0.0
        assert shape["width_pct"] == 100.0
        assert shape["height_pct"] == 100.0

    def test_x_and_width_scale_by_the_slide_width_not_its_height(self):
        """The load-bearing test for the axis pairing.

        The box is deliberately not square: 500 units across is 50% of the
        slide's WIDTH, and 250 units down is 25% of its HEIGHT. If the
        denominators were swapped, 500/1000 of 7.5in would still read as 50% -
        so the two axes carry different fractions here, and a swap shows up as
        a wrong number rather than as a coincidence.
        """
        preview = preview_of(
            one({"type": "shape", "kind": "rectangle", "bbox": [200, 500, 500, 250]})
        )
        shape = preview["shapes"][0]

        assert shape["left_pct"] == pytest.approx(20.0, abs=0.01)
        assert shape["top_pct"] == pytest.approx(50.0, abs=0.01)
        assert shape["width_pct"] == pytest.approx(50.0, abs=0.01)
        assert shape["height_pct"] == pytest.approx(25.0, abs=0.01)

    def test_the_percentages_match_the_decks_own_emu(self):
        """Computed from the shape, not re-derived from the bbox."""
        preview, slide = built(spec_dict())

        for payload, shape in zip(preview["shapes"], slide.shapes):
            assert payload["left_pct"] == pytest.approx(
                shape.left / SLIDE_W_EMU * 100, abs=0.001
            )
            assert payload["top_pct"] == pytest.approx(
                shape.top / SLIDE_H_EMU * 100, abs=0.001
            )
            assert payload["width_pct"] == pytest.approx(
                shape.width / SLIDE_W_EMU * 100, abs=0.001
            )
            assert payload["height_pct"] == pytest.approx(
                shape.height / SLIDE_H_EMU * 100, abs=0.001
            )

    def test_percentages_are_not_clamped_for_the_ui(self):
        """A shape may legitimately extend past 100%; clipping is the UI's job.

        ``_fit_span`` keeps the origin the model gave and trims the overflow, so
        a box starting at 95% cannot be moved back to fit - and the preview must
        report where it really is rather than a tidied 100.
        """
        preview = preview_of(
            one({"type": "shape", "kind": "rectangle", "bbox": [950, 0, 200, 100]})
        )
        shape = preview["shapes"][0]
        assert shape["left_pct"] == pytest.approx(95.0, abs=0.01)
        assert shape["left_pct"] + shape["width_pct"] <= 100.5


class TestGeometryFollowsTheBuilderNotTheRequest:
    def test_a_floored_height_is_previewed_at_the_height_really_rendered(self):
        """A 5-unit-tall band is below ``MIN_DIMENSION_IN`` and the builder
        enlarges it. 5/1000 of 7.5in is 0.0375in; the floor is 0.05in. The
        preview has to show 0.05in, or it shows a band the deck does not have.
        """
        requested_pct = 5 / 1000 * 100  # 0.5% if the bbox were taken at face value
        floored_pct = pptx_builder.MIN_DIMENSION_IN / config.SLIDE_HEIGHT_IN * 100

        preview, slide = built(
            one({"type": "shape", "kind": "rectangle", "bbox": [0, 100, 1000, 5]})
        )
        shape = preview["shapes"][0]

        assert shape["height_pct"] == pytest.approx(floored_pct, abs=0.01)
        assert shape["height_pct"] > requested_pct
        # ...and it is the deck's own height, not a recomputed one.
        assert shape["height_pct"] == pytest.approx(
            slide.shapes[0].height / SLIDE_H_EMU * 100, abs=0.001
        )

    def test_a_clipped_box_is_previewed_where_it_really_landed(self):
        """``bbox`` clamps each field on its own, so ``x + w`` can reach 2000 -
        the corner-coordinate mistake. The builder trims the overflow; the
        preview must report the trimmed box."""
        preview, slide = built(
            one({"type": "shape", "kind": "rectangle", "bbox": [800, 800, 1000, 1000]})
        )
        shape = preview["shapes"][0]

        # Requested: 100% wide starting at 80%. Rendered: trimmed to the edge.
        assert shape["width_pct"] == pytest.approx(20.0, abs=0.01)
        assert shape["height_pct"] == pytest.approx(20.0, abs=0.01)
        assert shape["width_pct"] == pytest.approx(
            slide.shapes[0].width / SLIDE_W_EMU * 100, abs=0.001
        )

    def test_cleaned_text_is_previewed_as_stored_not_as_requested(self):
        """The builder drops control characters and splits CRLF into real
        paragraphs. The preview reads the runs back out of the deck, so it shows
        what a reader will see."""
        preview = preview_of(
            one(
                {
                    "type": "text",
                    "bbox": [0, 0, 500, 200],
                    "content": "first\r\nsec\x07ond",
                }
            )
        )
        paragraphs = preview["shapes"][0]["paragraphs"]

        assert [p["text"] for p in paragraphs] == ["first", "second"]
        assert "\x07" not in json.dumps(preview)
        assert "\r" not in json.dumps(preview)


# =========================================================================== #
# z-order
# =========================================================================== #
class TestZOrder:
    def test_the_shapes_are_in_element_order_back_to_front(self):
        payload = {
            "elements": [
                {"type": "shape", "kind": "rectangle", "bbox": [0, 0, 1000, 200]},
                {"type": "text", "bbox": [0, 0, 500, 100], "content": "over the band"},
                {
                    "type": "table",
                    "bbox": [0, 300, 500, 300],
                    "data": [["a", "b"], ["c", "d"]],
                },
                {"type": "shape", "kind": "circle", "bbox": [900, 0, 80, 80]},
            ]
        }
        preview, slide = built(payload)

        assert kinds(preview) == ["shape", "text", "table", "shape"]
        # Not merely plausible - the same order the deck itself holds.
        assert len(preview["shapes"]) == len(slide.shapes)
        assert preview["shapes"][1]["paragraphs"][0]["text"] == "over the band"
        assert preview["shapes"][3]["autoshape"] == "oval"

    def test_the_order_is_not_sorted_by_position(self):
        """A later element that sits ABOVE an earlier one must still come
        second, because order is z-order and sorting would invert the stack."""
        payload = {
            "elements": [
                {"type": "shape", "kind": "rectangle", "bbox": [0, 800, 200, 100]},
                {"type": "shape", "kind": "circle", "bbox": [0, 100, 200, 100]},
            ]
        }
        preview = preview_of(payload)
        assert [s["autoshape"] for s in preview["shapes"]] == ["rectangle", "oval"]
        assert preview["shapes"][0]["top_pct"] > preview["shapes"][1]["top_pct"]


# =========================================================================== #
# colours
# =========================================================================== #
class TestColours:
    def test_a_filled_shape_reports_its_hex(self):
        preview = preview_of(
            one(
                {
                    "type": "shape",
                    "kind": "rectangle",
                    "bbox": [0, 0, 500, 200],
                    "fill_color": "#1E3A6E",
                    "border_color": "#FFFFFF",
                    "border_width": 2.5,
                }
            )
        )
        shape = preview["shapes"][0]
        assert shape["fill_color"] == "#1E3A6E"
        assert shape["line_color"] == "#FFFFFF"
        assert shape["line_width_pt"] == 2.5

    def test_a_no_fill_shape_reports_null_not_white(self):
        """``fill_color: null`` means see-through so later content shows; a
        preview that guessed white would hide whatever is underneath."""
        preview = preview_of(
            one({"type": "shape", "kind": "rectangle", "bbox": [0, 0, 500, 200]})
        )
        shape = preview["shapes"][0]
        assert shape["fill_color"] is None
        assert shape["line_color"] is None
        assert shape["line_width_pt"] is None

    def test_a_shape_with_an_outline_but_no_fill_keeps_the_outline(self):
        preview = preview_of(
            one(
                {
                    "type": "shape",
                    "kind": "rectangle",
                    "bbox": [0, 0, 500, 200],
                    "border_color": "#9AA9C2",
                }
            )
        )
        shape = preview["shapes"][0]
        assert shape["fill_color"] is None
        assert shape["line_color"] == "#9AA9C2"
        assert shape["line_width_pt"] == pytest.approx(
            float(config.DEFAULT_BORDER_WIDTH_PT), abs=0.01
        )

    def test_a_theme_colour_reads_as_null_rather_than_raising(self):
        """``fore_color.rgb`` RAISES on a scheme colour in python-pptx 1.0.2
        (``AttributeError``), so an unguarded read would turn one themed shape
        into a failed preview for the whole slide."""
        presentation = Presentation()
        presentation.slide_width = Inches(config.SLIDE_WIDTH_IN)
        presentation.slide_height = Inches(config.SLIDE_HEIGHT_IN)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        shape = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(1), Inches(1), Inches(2), Inches(1)
        )
        shape.fill.solid()
        shape.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_1
        shape.line.color.theme_color = MSO_THEME_COLOR.ACCENT_2

        # The raw accessor really does raise - the guard is not hypothetical.
        with pytest.raises(AttributeError):
            _ = shape.fill.fore_color.rgb

        preview = preview_module.build_preview(presentation)
        assert preview["shapes"][0]["fill_color"] is None
        assert preview["shapes"][0]["line_color"] is None

    def test_an_inherited_font_colour_reads_as_null(self):
        presentation = Presentation()
        presentation.slide_width = Inches(config.SLIDE_WIDTH_IN)
        presentation.slide_height = Inches(config.SLIDE_HEIGHT_IN)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
        box.text_frame.paragraphs[0].add_run().text = "unstyled"

        paragraph = preview_module.build_preview(presentation)["shapes"][0][
            "paragraphs"
        ][0]
        assert paragraph["text"] == "unstyled"
        assert paragraph["font_color"] is None
        assert paragraph["bold"] is False

    def test_the_background_is_reported_and_null_when_inherited(self):
        assert preview_of(
            one(
                {"type": "text", "bbox": [0, 0, 100, 100], "content": "x"},
                background="#102040",
            )
        )["background_color"] == "#102040"

        assert (
            preview_of(one({"type": "text", "bbox": [0, 0, 100, 100], "content": "x"}))[
                "background_color"
            ]
            is None
        )


# =========================================================================== #
# text
# =========================================================================== #
class TestTextShapes:
    def test_each_line_is_its_own_paragraph_with_its_styling(self):
        preview = preview_of(
            one(
                {
                    "type": "text",
                    "bbox": [100, 200, 400, 150],
                    "content": "Title\nSubtitle",
                    "font_size": 28,
                    "font_color": "#FFFFFF",
                    "bold": True,
                    "align": "center",
                }
            )
        )
        shape = preview["shapes"][0]

        assert shape["kind"] == "text"
        assert shape["align"] == "center"
        assert [p["text"] for p in shape["paragraphs"]] == ["Title", "Subtitle"]
        for paragraph in shape["paragraphs"]:
            assert paragraph["font_size_pt"] == 28.0
            assert paragraph["font_color"] == "#FFFFFF"
            assert paragraph["bold"] is True
            assert paragraph["align"] == "center"

    @pytest.mark.parametrize("align", ["left", "center", "right"])
    def test_every_alignment_survives_the_round_trip(self, align):
        preview = preview_of(
            one(
                {
                    "type": "text",
                    "bbox": [0, 0, 500, 100],
                    "content": "aligned",
                    "align": align,
                }
            )
        )
        assert preview["shapes"][0]["align"] == align
        assert preview["shapes"][0]["paragraphs"][0]["align"] == align

    def test_a_missing_font_size_falls_back_to_the_configured_default(self):
        """The UI must never have to guess a size, so the fallback is the same
        value the builder itself passes to python-pptx."""
        preview = preview_of(
            one({"type": "text", "bbox": [0, 0, 500, 100], "content": "no size given"})
        )
        paragraph = preview["shapes"][0]["paragraphs"][0]
        assert paragraph["font_size_pt"] == float(config.DEFAULT_TEXT_FONT_SIZE)

    def test_the_fallback_is_used_when_the_run_carries_no_size_at_all(self):
        """Built by hand, with nothing styled: the run's ``size`` really is
        ``None``, which is the case the fallback exists for."""
        presentation = Presentation()
        presentation.slide_width = Inches(config.SLIDE_WIDTH_IN)
        presentation.slide_height = Inches(config.SLIDE_HEIGHT_IN)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
        run = box.text_frame.paragraphs[0].add_run()
        run.text = "bare"
        assert run.font.size is None

        paragraph = preview_module.build_preview(presentation)["shapes"][0][
            "paragraphs"
        ][0]
        assert paragraph["font_size_pt"] == float(config.DEFAULT_TEXT_FONT_SIZE)

    def test_an_explicit_size_wins_over_the_default(self):
        preview = preview_of(
            one(
                {
                    "type": "text",
                    "bbox": [0, 0, 500, 100],
                    "content": "sized",
                    "font_size": 9,
                }
            )
        )
        assert preview["shapes"][0]["paragraphs"][0]["font_size_pt"] == 9.0
        assert config.DEFAULT_TEXT_FONT_SIZE != 9, "pick a size that is not the default"


# =========================================================================== #
# tables
# =========================================================================== #
class TestTables:
    TABLE = {
        "type": "table",
        "bbox": [500, 200, 460, 325],
        "data": [["Region", "Users", "Growth"], ["EMEA", "18,400", "+12%"]],
        "header_bg_color": "#1E3A6E",
        "header_font_bold": True,
        "font_size": 11,
    }

    def test_the_track_percentages_sum_to_about_one_hundred(self):
        preview = preview_of(one(copy.deepcopy(self.TABLE)))
        table = preview["shapes"][0]

        assert table["kind"] == "table"
        assert len(table["col_widths_pct"]) == 3
        assert len(table["row_heights_pct"]) == 2
        assert sum(table["col_widths_pct"]) == pytest.approx(100.0, abs=0.01)
        assert sum(table["row_heights_pct"]) == pytest.approx(100.0, abs=0.01)
        # Distributed evenly by the builder.
        assert table["col_widths_pct"][0] == pytest.approx(100 / 3, abs=0.01)
        assert table["row_heights_pct"][0] == pytest.approx(50.0, abs=0.01)

    def test_the_tracks_are_the_decks_own_widths(self):
        preview, slide = built(one(copy.deepcopy(self.TABLE)))
        table = slide.shapes[0].table
        total = sum(column.width for column in table.columns)

        assert preview["shapes"][0]["col_widths_pct"] == [
            pytest.approx(column.width / total * 100, abs=0.001)
            for column in table.columns
        ]

    def test_every_cell_carries_its_text_fill_and_font(self):
        preview = preview_of(one(copy.deepcopy(self.TABLE)))
        rows = preview["shapes"][0]["rows"]

        assert [[cell["text"] for cell in row] for row in rows] == [
            ["Region", "Users", "Growth"],
            ["EMEA", "18,400", "+12%"],
        ]
        for cell in rows[0]:
            assert cell["fill_color"] == "#1E3A6E"
            assert cell["bold"] is True
            # White on a dark header - chosen by the builder, read back here.
            assert cell["font_color"] == "#FFFFFF"
            assert cell["font_size_pt"] == 11.0
        for cell in rows[1]:
            assert cell["fill_color"] is None, "only the header row is filled"
            assert cell["bold"] is False
            assert cell["font_size_pt"] == 11.0

    def test_a_cell_font_size_falls_back_to_the_table_default(self):
        payload = copy.deepcopy(self.TABLE)
        payload.pop("font_size")
        preview = preview_of(one(payload))

        for row in preview["shapes"][0]["rows"]:
            for cell in row:
                assert cell["font_size_pt"] == float(config.DEFAULT_TABLE_FONT_SIZE)

    def test_a_ragged_row_is_padded_to_the_widest(self):
        """The builder squares the grid; the preview reports the real one."""
        preview = preview_of(
            one({"type": "table", "bbox": [0, 0, 500, 300], "data": [["a", "b", "c"], ["d"]]})
        )
        rows = preview["shapes"][0]["rows"]
        assert [len(row) for row in rows] == [3, 3]
        assert [cell["text"] for cell in rows[1]] == ["d", "", ""]


# =========================================================================== #
# shapes the product never emits
# =========================================================================== #
class TestUnsupportedShapes:
    def test_a_real_deck_holds_no_picture_and_no_unsupported_entry(self):
        preview = preview_of(spec_dict())
        assert "unsupported" not in kinds(preview)
        assert set(kinds(preview)) <= {"shape", "text", "table"}

    def test_a_picture_would_be_reported_as_unsupported_not_crash(self, tmp_path):
        """There are never any - nothing is ever flattened - but if one
        appeared, the preview must degrade rather than take the response down
        with it."""
        from make_sample_image import make_sample_image

        image = make_sample_image(tmp_path / "pic.png", size=(320, 180))

        presentation = Presentation()
        presentation.slide_width = Inches(config.SLIDE_WIDTH_IN)
        presentation.slide_height = Inches(config.SLIDE_HEIGHT_IN)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_picture(str(image), Inches(1), Inches(1), Inches(2), Inches(1))

        preview = preview_module.build_preview(presentation)
        shape = preview["shapes"][0]
        assert shape["kind"] == "unsupported"
        # Its geometry is still reported, so the UI can show a placeholder.
        assert shape["width_pct"] == pytest.approx(2 / config.SLIDE_WIDTH_IN * 100, abs=0.1)
        assert "fill_color" not in shape

    def test_an_empty_deck_previews_as_no_shapes(self):
        presentation = Presentation()
        presentation.slide_width = Inches(config.SLIDE_WIDTH_IN)
        presentation.slide_height = Inches(config.SLIDE_HEIGHT_IN)
        presentation.slides.add_slide(presentation.slide_layouts[6])

        preview = preview_module.build_preview(presentation)
        assert preview["shapes"] == []
        assert preview["slide"]["height_pt"] == 540.0


# =========================================================================== #
# a preview may never fail a conversion
# =========================================================================== #
class TestDerivationNeverFailsTheBuild:
    def test_preview_for_returns_a_warning_instead_of_raising(self, monkeypatch):
        def _explode(presentation):
            raise RuntimeError("walking the shapes went wrong")

        monkeypatch.setattr(preview_module, "build_preview", _explode)
        payload, warning = converter.preview_for(object())

        assert payload is None
        assert warning and "could not be derived" in warning
        assert "RuntimeError" in warning

    def test_build_from_payload_still_returns_the_deck(self, monkeypatch):
        monkeypatch.setattr(
            preview_module,
            "build_preview",
            lambda presentation: (_ for _ in ()).throw(RuntimeError("nope")),
        )
        result = converter.build_from_payload(spec_dict(), with_preview=True)

        assert result.preview is None
        assert result.pptx_bytes[:2] == b"PK", "the deck must survive intact"
        assert any("could not be derived" in line for line in result.warnings)

    def test_the_endpoint_answers_200_with_a_null_preview(self, client, monkeypatch):
        monkeypatch.setattr(
            preview_module,
            "build_preview",
            lambda presentation: (_ for _ in ()).throw(RuntimeError("nope")),
        )
        response = client.post("/api/preview", json=spec_dict())

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["preview"] is None
        assert any("could not be derived" in line for line in body["warnings"])
        # The rest of the contract is untouched by the failure.
        assert body["counts"] == {"shape": 4, "table": 1, "text": 4}

    def test_a_preview_failure_does_not_break_convert(self, client, monkeypatch):
        """The same guarantee on the path that matters most: the user still gets
        a download id even when the preview could not be derived."""
        from app import vision_analyzer

        monkeypatch.setattr(config, "LITELLM_API_KEY", "key-not-real", raising=False)
        monkeypatch.setattr(
            vision_analyzer,
            "analyze_image",
            lambda *a, **k: mock_data.sample_spec(),
        )
        monkeypatch.setattr(
            preview_module,
            "build_preview",
            lambda presentation: (_ for _ in ()).throw(RuntimeError("nope")),
        )

        from make_sample_image import make_sample_image
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as directory:
            image = make_sample_image(
                pathlib.Path(directory) / "s.png", size=(320, 180)
            )
            data = image.read_bytes()

        response = client.post(
            "/api/convert", files={"file": ("s.png", data, "image/png")}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["preview"] is None
        assert converter.is_download_id(body["download_id"])
        assert client.get(f"/api/download/{body['download_id']}").status_code == 200
        assert any("could not be derived" in line for line in body["warnings"])

    def test_the_warning_is_scrubbed_like_every_other_relayed_text(self, monkeypatch):
        leaky = "sk-abcdefghijklmnopqrstuvwxyz012345"

        def _explode(presentation):
            raise RuntimeError(f"Received API Key = {leaky}")

        monkeypatch.setattr(preview_module, "build_preview", _explode)
        _payload, warning = converter.preview_for(object())
        assert leaky not in warning


# =========================================================================== #
# the deck must not be changed by previewing it
# =========================================================================== #
class TestPreviewingDoesNotAlterTheDeck:
    """Deriving a preview reads ``slide.background.fill``, and that read is NOT
    free of side effects: python-pptx materializes a ``<p:bg>`` element on a
    slide that had none. Measured, on python-pptx 1.0.2::

        prs.save(a)                       # 28328 bytes
        prs.slides[0].background.fill     # <- creates <p:bg>
        prs.save(b)                       # 28342 bytes

    So the pipeline saves the bytes FIRST and previews second. The deck the user
    downloads must therefore carry no ``<p:bg>`` when the spec asked for no
    background - which is what these assert, against the shipped bytes rather
    than against a second build (comparing two builds proves nothing, because a
    misordered ``build_deck`` would grow both of them equally).
    """

    NO_BACKGROUND = {"type": "text", "bbox": [0, 0, 500, 100], "content": "x"}

    @staticmethod
    def slide_xml(data: bytes) -> str:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.read("ppt/slides/slide1.xml").decode("utf-8")

    def test_the_shipped_bytes_carry_no_background_element(self):
        result = converter.build_from_payload(
            one(copy.deepcopy(self.NO_BACKGROUND)), with_preview=True
        )

        assert result.preview is not None
        assert result.preview["background_color"] is None
        assert "p:bg" not in self.slide_xml(result.pptx_bytes), (
            "the .pptx now carries a <p:bg> the spec never asked for: the "
            "slide background was read BEFORE the bytes were saved"
        )

    def test_an_explicit_background_is_still_written(self):
        """The guard above must not be satisfiable by never writing one."""
        result = converter.build_from_payload(
            one(copy.deepcopy(self.NO_BACKGROUND), background="#102040"),
            with_preview=True,
        )
        assert result.preview["background_color"] == "#102040"
        assert "p:bg" in self.slide_xml(result.pptx_bytes)

    def test_convert_ships_no_background_element_either(self, monkeypatch):
        from app import vision_analyzer

        spec = SlideSpec.model_validate(one(copy.deepcopy(self.NO_BACKGROUND)))
        monkeypatch.setattr(config, "LITELLM_API_KEY", "key-not-real", raising=False)
        monkeypatch.setattr(vision_analyzer, "analyze_image", lambda *a, **k: spec)

        from make_sample_image import make_sample_image
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            image = make_sample_image(pathlib.Path(directory) / "s.png", size=(320, 180))
            data = image.read_bytes()

        result = converter.convert_upload(data, "s.png")
        assert result.preview is not None
        assert "p:bg" not in self.slide_xml(result.pptx_bytes)

    def test_build_from_json_is_unchanged_and_carries_no_preview_cost(self):
        result = converter.build_from_payload(spec_dict())
        assert result.preview is None
        assert result.pptx_bytes[:2] == b"PK"


# =========================================================================== #
# POST /api/preview
# =========================================================================== #
class TestPreviewEndpoint:
    def test_the_response_is_convert_minus_the_download_fields(self, client):
        response = client.post("/api/preview", json=spec_dict())
        assert response.status_code == 200
        body = response.json()

        assert set(body) == {
            "status",
            "counts",
            "total",
            "dropped",
            "warnings",
            "spec",
            "preview",
        }
        assert "download_id" not in body
        assert "filename" not in body
        assert body["status"] == "success"
        assert body["counts"] == {"shape": 4, "table": 1, "text": 4}
        assert body["total"] == 9
        assert body["dropped"] == []
        assert body["warnings"] == []
        assert set(body["preview"]) == {"slide", "background_color", "shapes"}
        assert len(body["preview"]["shapes"]) == 9

    def test_it_stores_nothing(self, client):
        before = converter.downloads.ids()
        client.post("/api/preview", json=spec_dict())
        assert converter.downloads.ids() == before

    def test_it_needs_no_key(self, client):
        assert config.LITELLM_API_KEY == ""
        assert client.post("/api/preview", json=spec_dict()).status_code == 200

    def test_a_spec_wrapper_is_accepted(self, client):
        response = client.post("/api/preview", json={"spec": spec_dict()})
        assert response.status_code == 200
        assert response.json()["total"] == 9
        assert len(response.json()["preview"]["shapes"]) == 9

    def test_a_bare_element_array_is_accepted(self, client):
        elements = spec_dict()["elements"]
        response = client.post("/api/preview", json=elements)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == len(elements)
        assert body["preview"]["shapes"]
        # No background key in a bare array, so the slide inherits.
        assert body["preview"]["background_color"] is None

    def test_a_partial_drop_is_reported_and_the_rest_is_previewed(self, client):
        payload = spec_dict()
        payload["elements"].append({"type": "table", "bbox": [10, 10, 100, 100]})

        response = client.post("/api/preview", json=payload)
        assert response.status_code == 200
        body = response.json()

        assert len(body["dropped"]) == 1
        assert "data - Field required" in body["dropped"][0]
        assert body["total"] == 9, "the dropped element is not in the spec"
        assert len(body["preview"]["shapes"]) == 9
        assert "dropped" not in body["spec"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"elements": [{"type": "nope", "bbox": [0, 0, 10, 10]}]},
            {"elements": [{"type": "table", "bbox": [0, 0, 1, 1]}]},
        ],
    )
    def test_an_unusable_spec_is_invalid_spec(self, client, payload):
        """An all-dropped payload is a loud failure here too - a preview of a
        blank slide would look like the model simply saw nothing."""
        response = client.post("/api/preview", json=payload)
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "invalid_spec"
        assert set(detail) == {"code", "message", "hint"}

    @pytest.mark.parametrize("payload", [{"elements": []}, {}])
    def test_a_deliberately_blank_spec_previews_as_a_blank_slide(self, client, payload):
        """Distinct from all-dropped: nothing was rejected, there was simply
        nothing to draw. ``/api/build-from-json`` answers 200 for this, so the
        preview must agree rather than invent a 400."""
        assert client.post("/api/build-from-json", json=payload).status_code == 200

        response = client.post("/api/preview", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["preview"]["shapes"] == []
        assert body["preview"]["slide"]["height_pt"] == 540.0

    def test_a_malformed_body_is_invalid_spec(self, client):
        response = client.post(
            "/api/preview",
            content="{not json at all",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_spec"

    def test_a_build_failure_is_build_failed(self, client, monkeypatch):
        def _explode(spec):
            raise pptx_builder.PptxBuildError("python-pptx said no")

        monkeypatch.setattr(pptx_builder, "build_presentation", _explode)
        response = client.post("/api/preview", json=spec_dict())
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["code"] == "build_failed"
        assert "python-pptx said no" in detail["message"]

    def test_an_oversize_body_is_refused_before_it_is_decoded(self, client):
        """The same cap and the same code ``/api/build-from-json`` uses: a body
        this size IS an unusable spec, and the endpoint pins only
        ``invalid_spec`` / ``build_failed``."""
        oversize = json.dumps(
            {"elements": [{"type": "text", "bbox": [0, 0, 10, 10], "content": "x"}]}
        ) + " " * (converter.MAX_JSON_BODY_BYTES + 1)
        response = client.post(
            "/api/preview",
            content=oversize,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "invalid_spec"
        assert "larger than the 2 MB limit" in detail["message"]

    def test_it_agrees_with_what_build_from_json_would_produce(self, client):
        """The two endpoints must describe the same deck, or the user downloads
        something other than what was previewed."""
        payload = spec_dict()
        previewed = client.post("/api/preview", json=payload).json()["preview"]
        binary = client.post("/api/build-from-json", json=payload)

        assert binary.status_code == 200
        import io

        presentation = Presentation(io.BytesIO(binary.content))
        shapes = presentation.slides[0].shapes
        assert len(previewed["shapes"]) == len(shapes)
        for payload_shape, real in zip(previewed["shapes"], shapes):
            assert payload_shape["left_pct"] == pytest.approx(
                real.left / SLIDE_W_EMU * 100, abs=0.001
            )
            assert payload_shape["height_pct"] == pytest.approx(
                real.height / SLIDE_H_EMU * 100, abs=0.001
            )


# =========================================================================== #
# the whole payload is JSON-clean
# =========================================================================== #
class TestThePayloadIsSerializable:
    def test_the_sample_spec_round_trips_through_json_unchanged(self):
        preview = preview_of(spec_dict())
        assert json.loads(json.dumps(preview)) == preview

    def test_no_enum_or_emu_object_leaks_into_the_payload(self):
        """Every value has to be a plain JSON type: a python-pptx ``Emu`` or an
        ``MSO_*`` enum would serialize as a number or an int by accident today
        and break the moment one of them stops being int-like."""
        preview = preview_of(spec_dict())

        def walk(node, path="preview"):
            if isinstance(node, dict):
                for key, value in node.items():
                    assert isinstance(key, str), f"{path}: non-string key {key!r}"
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
            else:
                assert type(node) in (str, bool, int, float, type(None)), (
                    f"{path} is a {type(node).__name__}: {node!r}"
                )

        walk(preview)

    def test_a_shape_entry_has_exactly_the_pinned_keys(self):
        preview = preview_of(spec_dict())
        by_kind = {shape["kind"]: shape for shape in preview["shapes"]}
        geometry = {"left_pct", "top_pct", "width_pct", "height_pct"}

        assert set(by_kind["shape"]) == geometry | {
            "kind",
            "autoshape",
            "fill_color",
            "line_color",
            "line_width_pt",
        }
        assert set(by_kind["text"]) == geometry | {"kind", "align", "paragraphs"}
        assert set(by_kind["table"]) == geometry | {
            "kind",
            "col_widths_pct",
            "row_heights_pct",
            "rows",
        }
        assert set(by_kind["text"]["paragraphs"][0]) == {
            "text",
            "font_size_pt",
            "font_color",
            "bold",
            "align",
        }
        assert set(by_kind["table"]["rows"][0][0]) == {
            "text",
            "fill_color",
            "font_size_pt",
            "font_color",
            "bold",
            "align",
        }

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("rectangle", "rectangle"),
            ("rounded_rectangle", "rounded_rectangle"),
            ("circle", "oval"),
        ],
    )
    def test_the_autoshape_name_is_the_preset_really_used(self, kind, expected):
        """The schema calls it ``circle``; python-pptx draws an ``OVAL``. The
        preview reports what is in the file, which is what the UI has to draw."""
        preview = preview_of(
            one({"type": "shape", "kind": kind, "bbox": [0, 0, 300, 300]})
        )
        assert preview["shapes"][0]["autoshape"] == expected

    def test_an_unmapped_preset_reads_as_other_rather_than_failing(self):
        presentation = Presentation()
        presentation.slide_width = Inches(config.SLIDE_WIDTH_IN)
        presentation.slide_height = Inches(config.SLIDE_HEIGHT_IN)
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_shape(
            MSO_SHAPE.RIGHT_ARROW, Inches(1), Inches(1), Inches(2), Inches(1)
        )

        shape = preview_module.build_preview(presentation)["shapes"][0]
        assert shape["kind"] == "shape"
        assert shape["autoshape"] == "other"


# =========================================================================== #
# the module stays framework-free
# =========================================================================== #
def test_the_preview_module_imports_nothing_from_the_web_layer():
    """``preview.py`` sits in ``app/services`` and may not reach for fastapi,
    the request, or the spec - it only reads a built deck.

    Asserted against the IMPORT statements rather than the whole file, so the
    module can still explain in prose why it does not use the SlideSpec.
    """
    import pathlib

    source = pathlib.Path(preview_module.__file__).read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert imports, "found no imports to check - re-point this guard"

    joined = "\n".join(imports)
    for forbidden in ("fastapi", "starlette", "schemas", "SlideSpec", "pptx_builder"):
        assert forbidden not in joined, (
            f"preview.py imports {forbidden}; it must derive the preview from "
            f"the built deck alone.\nimports were:\n{joined}"
        )


def test_build_preview_takes_a_presentation_not_a_spec():
    import inspect

    parameters = list(inspect.signature(preview_module.build_preview).parameters)
    assert parameters == ["presentation"]

    # Pointing it at a spec is a programming error, not a silent wrong answer.
    with pytest.raises(Exception):
        preview_module.build_preview(mock_data.sample_spec())
