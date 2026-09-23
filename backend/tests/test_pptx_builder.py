"""Builder tests: geometry, styling, z-order, and the anti-flattening proof.

The most important test in this file is
:meth:`TestBuildPptx.test_reopened_deck_contains_no_picture_shapes` - it is the
one that proves the tool produced an *editable* deck instead of pasting the
input image onto a slide.
"""

from __future__ import annotations

import re
import zipfile

import pytest
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Inches, Pt

from app import config
from app import mock_data
from app import pptx_builder
from app.pptx_builder import (
    PLAIN_TABLE_STYLE_ID,
    SHAPE_MAP,
    TABLE_FONT_DARK,
    TABLE_FONT_LIGHT,
    PptxBuildError,
    add_shape,
    add_table,
    add_textbox,
    build_pptx,
    build_presentation,
    scale_bbox,
    to_rgb,
)
from app.schemas import BBox, ShapeElement, SlideSpec, TableElement, TextElement

# 0.0001 inch: guards against float-ordering noise without letting a real
# geometry bug through.
EMU_TOL = 100
INCH = 914400


def _force(model, field: str, value) -> None:
    """Write a field straight into the model dict, bypassing validation.

    Used to hand the builder the kind of garbage the schema would normally
    have filtered out, so we can prove the builder itself is defensive.
    """
    model.__dict__[field] = value


def _blank_deck():
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    return prs, prs.slides.add_slide(prs.slide_layouts[6])


def _category(shape) -> str:
    """Classify a rendered shape the same way the spec classifies elements."""
    if shape.has_table:
        return "table"
    stype = shape.shape_type
    if stype == MSO_SHAPE_TYPE.TEXT_BOX:
        return "text"
    if stype == MSO_SHAPE_TYPE.AUTO_SHAPE:
        return "shape"
    return f"unexpected({stype})"


def _expected_categories(spec: SlideSpec) -> list[str]:
    return [el.type for el in spec.elements]


def _effective_font_sizes(cell) -> list:
    """Every non-None font size visible on a table cell (runs + paragraphs)."""
    sizes = []
    for para in cell.text_frame.paragraphs:
        if para.font.size is not None:
            sizes.append(para.font.size)
        for run in para.runs:
            if run.font.size is not None:
                sizes.append(run.font.size)
    return sizes


def _bold_flags(cell) -> list:
    flags = []
    for para in cell.text_frame.paragraphs:
        flags.append(para.font.bold)
        flags.extend(run.font.bold for run in para.runs)
    return [f for f in flags if f is not None]


# ---------------------------------------------------------------------------
# module surface
# ---------------------------------------------------------------------------
def test_build_error_is_an_exception():
    assert issubclass(PptxBuildError, Exception)


def test_shape_map_covers_every_shape_kind():
    assert set(SHAPE_MAP) == {"rectangle", "rounded_rectangle", "circle"}
    assert SHAPE_MAP["rectangle"] == MSO_SHAPE.RECTANGLE
    assert SHAPE_MAP["rounded_rectangle"] == MSO_SHAPE.ROUNDED_RECTANGLE
    assert SHAPE_MAP["circle"] == MSO_SHAPE.OVAL


# ---------------------------------------------------------------------------
# to_rgb
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#FF0000", RGBColor(0xFF, 0x00, 0x00)),
        ("#1f3b73", RGBColor(0x1F, 0x3B, 0x73)),
        ("abc", RGBColor(0xAA, 0xBB, 0xCC)),
        ("FFFFFF", RGBColor(0xFF, 0xFF, 0xFF)),
        ("#12345678", RGBColor(0x12, 0x34, 0x56)),
    ],
)
def test_to_rgb_parses_and_normalizes(raw, expected):
    assert to_rgb(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "banana", "#GGG", "rgb(0,0,0)"])
def test_to_rgb_degrades_to_none_instead_of_raising(raw):
    assert to_rgb(raw) is None


# ---------------------------------------------------------------------------
# scale_bbox
# ---------------------------------------------------------------------------
def test_scale_bbox_maps_the_full_coordinate_space_onto_the_slide():
    left, top, width, height = scale_bbox(BBox(x=0, y=0, w=1000, h=1000))
    assert left == 0 and top == 0
    assert abs(width - Inches(13.333)) <= EMU_TOL
    assert abs(height - Inches(7.5)) <= EMU_TOL


def test_scale_bbox_is_proportional():
    left, top, width, height = scale_bbox(BBox(x=500, y=200, w=250, h=400))
    assert abs(left - Inches(0.5 * 13.333)) <= EMU_TOL
    assert abs(top - Inches(0.2 * 7.5)) <= EMU_TOL
    assert abs(width - Inches(0.25 * 13.333)) <= EMU_TOL
    assert abs(height - Inches(0.4 * 7.5)) <= EMU_TOL


def test_scale_bbox_enforces_a_minimum_size_for_degenerate_boxes():
    _, _, width, height = scale_bbox(BBox(x=0, y=0, w=1, h=1))
    assert width >= Inches(0.049), "sliver width must be floored so it still renders"
    assert height >= Inches(0.049), "sliver height must be floored so it still renders"


def test_scale_bbox_does_not_inflate_normal_boxes():
    _, _, width, height = scale_bbox(BBox(x=0, y=0, w=500, h=500))
    assert abs(width - Inches(0.5 * 13.333)) <= EMU_TOL
    assert abs(height - Inches(0.5 * 7.5)) <= EMU_TOL


def test_scale_bbox_returns_four_integral_emu_values():
    values = scale_bbox(BBox(x=10, y=10, w=10, h=10))
    assert len(values) == 4
    assert all(isinstance(v, int) for v in values)  # Emu subclasses int


# ---------------------------------------------------------------------------
# scale_bbox: out-of-frame boxes (x + w > 1000) must stay on the slide
# ---------------------------------------------------------------------------
class TestScaleBboxStaysOnTheSlide:
    """BBox clamps x/y/w/h independently, so x + w can reach 2000.

    A corner-style ``[x0, y0, x1, y1]`` answer overshoots the right/bottom edge
    and would otherwise be drawn completely off the slide, invisibly.
    """

    SLIDE_W = Inches(13.333)
    SLIDE_H = Inches(7.5)

    @pytest.mark.parametrize(
        "bbox",
        [
            [990, 950, 200, 200],  # slight overshoot on both axes
            [900, 900, 1000, 1000],  # corner coordinates read as w/h
            [1000, 1000, 1000, 1000],  # entirely off the slide
            [0, 0, 1000, 1000],  # exactly the full slide (must not move)
        ],
    )
    def test_the_box_never_reaches_past_the_slide(self, bbox):
        left, top, width, height = scale_bbox(BBox.model_validate(bbox))
        assert left >= 0 and top >= 0
        assert left + width <= self.SLIDE_W + EMU_TOL, "box reaches past the right edge"
        assert top + height <= self.SLIDE_H + EMU_TOL, "box reaches past the bottom edge"
        assert width >= Inches(0.049) and height >= Inches(0.049)

    def test_clipping_is_reported_on_stderr(self, capsys):
        scale_bbox(BBox.model_validate([900, 900, 1000, 1000]))
        err = capsys.readouterr().err
        assert "900" in err and "slide" in err.lower(), err
        assert "x0" in err, "the warning should name the corner-coordinate mistake"

    @pytest.mark.parametrize(
        "bbox",
        [
            [0, 0, 1000, 1000],
            [500, 200, 250, 400],
            [800, 0, 200, 1000],
            # the smallest box that needs no adjustment at all: w=4 -> 0.053in and
            # h=7 -> 0.0525in both clear MIN_DIMENSION_IN, so nothing is floored
            [0, 0, 4, 7],
        ],
    )
    def test_boxes_that_fit_produce_no_warning(self, bbox, capsys):
        scale_bbox(BBox.model_validate(bbox))
        assert capsys.readouterr().err == "", "an unadjusted bbox must stay silent"

    def test_the_origin_is_preserved_when_only_the_size_overflows(self):
        left, top, width, _ = scale_bbox(BBox.model_validate([900, 100, 1000, 100]))
        assert abs(left - Inches(0.9 * 13.333)) <= EMU_TOL, "the origin must not move"
        assert abs((left + width) - self.SLIDE_W) <= EMU_TOL, "the overflow must be trimmed"

    def test_a_floored_box_is_reported_too(self, capsys):
        """``size = max(size_in, minimum)`` ran BEFORE ``clipped`` was computed.

        Comparing the floored size against itself is equal by construction, so
        the minimum-dimension floor could never report itself and a sliver was
        silently enlarged - the only adjustment in this function that did not
        warn. It must warn like the out-of-slide case does.
        """
        scale_bbox(BBox.model_validate([0, 0, 1, 1]))
        err = capsys.readouterr().err
        assert err.strip(), "a box floored up to the minimum must not be silent"
        assert "0.05" in err, err
        assert "width and height" in err, err
        assert "reaches past" not in err, "a floor is not an out-of-slide clip"

    @pytest.mark.parametrize(
        ("bbox", "phrase"),
        [
            ([0, 0, 1, 500], "width is below"),  # 0.013in wide, tall enough
            ([0, 0, 500, 1], "height is below"),  # 0.0075in tall, wide enough
            ([0, 0, 3, 6], "width and height are below"),  # both under the floor
        ],
    )
    def test_the_warning_names_the_axis_that_was_floored(self, bbox, phrase, capsys):
        scale_bbox(BBox.model_validate(bbox))
        err = capsys.readouterr().err
        assert f": {phrase} " in err, err
        assert err.count("[pptx_builder] warning") == 1, "one floor, one warning"

    def test_a_floored_box_is_still_rendered_at_the_minimum_size(self, capsys):
        _, _, width, height = scale_bbox(BBox.model_validate([0, 0, 1, 1]))
        assert width >= Inches(0.049) and height >= Inches(0.049)
        assert capsys.readouterr().err.strip(), "and it said so"

    def test_a_box_that_is_both_floored_and_off_slide_reports_both(self, capsys):
        scale_bbox(BBox.model_validate([1000, 0, 1, 1000]))
        err = capsys.readouterr().err
        assert "reaches past" in err, err
        assert "0.05in" in err, err
        assert err.count("[pptx_builder] warning") == 2, err

    def test_a_clipped_element_is_still_rendered(self, capsys):
        spec = SlideSpec.model_validate(
            {
                "elements": [
                    {"type": "text", "bbox": [990, 950, 200, 200], "content": "edge"},
                    {"type": "shape", "bbox": [900, 900, 1000, 1000], "fill_color": "#FF0000"},
                    {"type": "table", "bbox": [950, 950, 400, 400], "data": [["a", "b"]]},
                ]
            }
        )
        prs = build_presentation(spec)
        shapes = list(prs.slides[0].shapes)
        assert len(shapes) == 3, "clipping must not drop elements"
        for shape in shapes:
            assert shape.left + shape.width <= prs.slide_width + EMU_TOL
        assert capsys.readouterr().err.count("reaches past") == 3


# ---------------------------------------------------------------------------
# add_shape
# ---------------------------------------------------------------------------
class TestAddShape:
    def test_geometry_and_preset_match_the_element(self):
        _, slide = _blank_deck()
        el = ShapeElement(type="shape", kind="rounded_rectangle", bbox=[100, 200, 300, 400])
        shape = add_shape(slide, el)
        left, top, width, height = scale_bbox(el.bbox)
        assert shape.auto_shape_type == MSO_SHAPE.ROUNDED_RECTANGLE
        assert abs(shape.left - left) <= EMU_TOL
        assert abs(shape.top - top) <= EMU_TOL
        assert abs(shape.width - width) <= EMU_TOL
        assert abs(shape.height - height) <= EMU_TOL

    def test_circle_maps_to_an_oval(self):
        _, slide = _blank_deck()
        shape = add_shape(slide, ShapeElement(type="shape", kind="circle", bbox=[0, 0, 100, 100]))
        assert shape.auto_shape_type == MSO_SHAPE.OVAL

    def test_shapes_carry_no_placeholder_text(self):
        _, slide = _blank_deck()
        shape = add_shape(slide, ShapeElement(type="shape", bbox=[0, 0, 100, 100]))
        assert shape.text_frame.text == ""

    def test_solid_fill_when_a_colour_is_given(self):
        from pptx.enum.dml import MSO_FILL

        _, slide = _blank_deck()
        shape = add_shape(
            slide, ShapeElement(type="shape", bbox=[0, 0, 100, 100], fill_color="#1F3B73")
        )
        assert shape.fill.type == MSO_FILL.SOLID
        assert shape.fill.fore_color.rgb == RGBColor(0x1F, 0x3B, 0x73)

    def test_no_fill_keeps_underlying_content_visible(self):
        from pptx.enum.dml import MSO_FILL

        _, slide = _blank_deck()
        shape = add_shape(slide, ShapeElement(type="shape", bbox=[0, 0, 100, 100]))
        assert shape.fill.type == MSO_FILL.BACKGROUND

    def test_border_colour_and_width_are_applied(self):
        _, slide = _blank_deck()
        shape = add_shape(
            slide,
            ShapeElement(
                type="shape", bbox=[0, 0, 100, 100], border_color="#FF0000", border_width=2.5
            ),
        )
        assert shape.line.color.rgb == RGBColor(0xFF, 0x00, 0x00)
        assert shape.line.width == Pt(2.5)

    def test_missing_border_colour_means_no_outline(self):
        from pptx.enum.dml import MSO_FILL

        _, slide = _blank_deck()
        shape = add_shape(slide, ShapeElement(type="shape", bbox=[0, 0, 100, 100]))
        assert shape.line.fill.type == MSO_FILL.BACKGROUND


# ---------------------------------------------------------------------------
# add_table
# ---------------------------------------------------------------------------
class TestAddTable:
    DATA = [["Region", "Users", "Growth"], ["EMEA", "18,400", "+12%"], ["APAC", "9,750", "+27%"]]

    def _table(self, **kwargs):
        _, slide = _blank_deck()
        el = TableElement(type="table", bbox=[100, 100, 600, 400], data=self.DATA, **kwargs)
        frame = add_table(slide, el)
        assert frame.has_table
        return el, frame.table

    def test_dimensions_and_cell_text_match_the_data(self):
        el, table = self._table()
        assert len(table.rows) == 3
        assert len(table.columns) == 3
        for r, row in enumerate(el.data):
            for c, expected in enumerate(row):
                assert table.cell(r, c).text == expected

    def test_geometry_matches_scale_bbox(self):
        _, slide = _blank_deck()
        el = TableElement(type="table", bbox=[100, 100, 600, 400], data=self.DATA)
        frame = add_table(slide, el)
        left, top, width, height = scale_bbox(el.bbox)
        assert abs(frame.left - left) <= EMU_TOL
        assert abs(frame.top - top) <= EMU_TOL
        assert abs(frame.width - width) <= EMU_TOL

    def test_first_row_banding_is_enabled_for_a_header(self):
        _, table = self._table(header_bg_color="#1F3B73", header_font_bold=True)
        assert table.first_row is True

    def test_first_row_flag_is_false_without_any_header_styling(self):
        _, table = self._table(header_bg_color=None, header_font_bold=False)
        assert table.first_row is False

    def test_header_cells_get_the_requested_background(self):
        from pptx.enum.dml import MSO_FILL

        _, table = self._table(header_bg_color="#1F3B73")
        for c in range(3):
            cell = table.cell(0, c)
            assert cell.fill.type == MSO_FILL.SOLID
            assert cell.fill.fore_color.rgb == RGBColor(0x1F, 0x3B, 0x73)

    def test_default_font_size_is_the_compact_table_size(self):
        from app import config

        _, table = self._table()
        expected = Pt(config.DEFAULT_TABLE_FONT_SIZE)
        for r in range(3):
            for c in range(3):
                sizes = _effective_font_sizes(table.cell(r, c))
                assert sizes, f"cell ({r},{c}) has no explicit font size"
                assert all(s == expected for s in sizes), f"cell ({r},{c}) -> {sizes}"

    def test_explicit_font_size_wins(self):
        _, table = self._table(font_size=14)
        for r in range(3):
            for c in range(3):
                sizes = _effective_font_sizes(table.cell(r, c))
                assert sizes and all(s == Pt(14) for s in sizes)

    def test_header_row_is_bold_when_requested(self):
        _, table = self._table(header_font_bold=True)
        assert True in _bold_flags(table.cell(0, 0))
        assert False not in _bold_flags(table.cell(0, 0))

    def test_cell_margins_are_kept_small(self):
        _, table = self._table()
        cell = table.cell(0, 0)
        assert cell.margin_left <= Inches(0.05)
        assert cell.margin_top <= Inches(0.05)

    # -- inherited-table-style neutralisation ------------------------------ #
    def test_row_banding_is_turned_off(self):
        """python-pptx emits bandRow="1"; nothing in a SlideSpec asks for that."""
        _, table = self._table(header_bg_color="#1F3B73")
        assert table.horz_banding is False, "banded body rows were never requested"
        assert table.vert_banding is False

    def test_the_default_accent_table_style_is_replaced(self):
        _, table = self._table(header_bg_color="#1F3B73")
        xml = table._tbl.xml
        assert PLAIN_TABLE_STYLE_ID in xml
        assert "{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}" not in xml, (
            "'Medium Style 2 - Accent 1' would decide the table's colours instead "
            "of the analyzed image"
        )
        assert 'bandRow="1"' not in xml

    def test_header_bold_false_is_written_as_off_not_inherited(self):
        """`header_font_bold=False` plus a header fill must land in the file."""
        _, table = self._table(header_bg_color="#EEEEEE", header_font_bold=False)
        assert table.first_row is True  # a fill still marks row 0 as the header
        flags = _bold_flags(table.cell(0, 0))
        assert flags and all(flag is False for flag in flags), flags

    def test_every_cell_gets_an_explicit_font_colour(self):
        _, table = self._table(header_bg_color="#1F3B73")
        for r in range(3):
            for c in range(3):
                cell = table.cell(r, c)
                for para in cell.text_frame.paragraphs:
                    assert para.font.color.rgb is not None
                    for run in para.runs:
                        assert run.font.color.rgb is not None, f"cell ({r},{c}) run"

    @pytest.mark.parametrize(
        ("header_bg", "expected"),
        [
            ("#1F3B73", TABLE_FONT_LIGHT),  # dark header -> white text
            ("#F2F2F2", TABLE_FONT_DARK),  # light header -> black text
            (None, TABLE_FONT_DARK),  # no header fill -> black text
        ],
    )
    def test_header_text_colour_stays_readable_on_its_fill(self, header_bg, expected):
        _, table = self._table(header_bg_color=header_bg)
        run = table.cell(0, 0).text_frame.paragraphs[0].runs[0]
        assert run.font.color.rgb == expected

    def test_body_cells_keep_a_dark_font_colour(self):
        _, table = self._table(header_bg_color="#1F3B73")
        run = table.cell(1, 0).text_frame.paragraphs[0].runs[0]
        assert run.font.color.rgb == TABLE_FONT_DARK

    def test_ragged_and_empty_data_are_survivable(self):
        _, slide = _blank_deck()
        el = TableElement(type="table", bbox=[0, 0, 500, 500], data=[["a", "b"], ["c"]])
        table = add_table(slide, el).table
        assert len(table.rows) == 2 and len(table.columns) == 2
        assert table.cell(1, 1).text == ""


# ---------------------------------------------------------------------------
# add_textbox
# ---------------------------------------------------------------------------
class TestAddTextbox:
    def test_word_wrap_autosize_and_anchor(self):
        _, slide = _blank_deck()
        box = add_textbox(
            slide, TextElement(type="text", bbox=[0, 0, 400, 200], content="hello world")
        )
        tf = box.text_frame
        assert tf.word_wrap is True
        assert tf.auto_size == MSO_AUTO_SIZE.NONE
        assert tf.vertical_anchor == MSO_ANCHOR.TOP
        assert tf.margin_left <= Inches(0.05)
        assert tf.margin_top <= Inches(0.05)

    def test_newlines_become_separate_paragraphs(self):
        _, slide = _blank_deck()
        content = "Highlights\n- one\n- two"
        box = add_textbox(slide, TextElement(type="text", bbox=[0, 0, 400, 200], content=content))
        paras = box.text_frame.paragraphs
        assert len(paras) == 3
        assert [p.text for p in paras] == content.split("\n")

    def test_text_is_real_editable_text(self):
        _, slide = _blank_deck()
        box = add_textbox(
            slide, TextElement(type="text", bbox=[0, 0, 400, 200], content="Quarterly Review")
        )
        assert "Quarterly Review" in box.text_frame.text

    @pytest.mark.parametrize(
        ("align", "expected"),
        [("center", PP_ALIGN.CENTER), ("right", PP_ALIGN.RIGHT)],
    )
    def test_alignment_is_applied(self, align, expected):
        _, slide = _blank_deck()
        box = add_textbox(
            slide, TextElement(type="text", bbox=[0, 0, 400, 200], content="x", align=align)
        )
        assert all(p.alignment == expected for p in box.text_frame.paragraphs)

    def test_left_alignment_is_left_or_default(self):
        _, slide = _blank_deck()
        box = add_textbox(
            slide, TextElement(type="text", bbox=[0, 0, 400, 200], content="x", align="left")
        )
        assert box.text_frame.paragraphs[0].alignment in (PP_ALIGN.LEFT, None)

    def test_font_size_bold_and_colour_are_applied(self):
        _, slide = _blank_deck()
        box = add_textbox(
            slide,
            TextElement(
                type="text",
                bbox=[0, 0, 400, 200],
                content="styled",
                font_size=28,
                font_color="#FF0000",
                bold=True,
            ),
        )
        run = box.text_frame.paragraphs[0].runs[0]
        assert run.font.size == Pt(28)
        assert run.font.bold is True
        assert run.font.color.rgb == RGBColor(0xFF, 0x00, 0x00)

    def test_default_font_size_comes_from_config(self):
        from app import config

        _, slide = _blank_deck()
        box = add_textbox(slide, TextElement(type="text", bbox=[0, 0, 400, 200], content="plain"))
        run = box.text_frame.paragraphs[0].runs[0]
        assert run.font.size == Pt(config.DEFAULT_TEXT_FONT_SIZE)

    def test_empty_content_does_not_explode(self):
        _, slide = _blank_deck()
        # An explicitly empty text box is legitimate (a MISSING content is not,
        # and is now dropped by the schema - see tests/test_schemas.py).
        box = add_textbox(slide, TextElement(type="text", bbox=[0, 0, 400, 200], content=""))
        assert box.text_frame.text == ""


# ---------------------------------------------------------------------------
# control characters: python-pptx escapes them into literal "_xNNNN_" text
# ---------------------------------------------------------------------------
class TestControlCharacters:
    """A Windows-authored source gives the model CRLF; the deck must not show it.

    python-pptx escapes \\x00-\\x08 and \\x0B-\\x1F into a plain-text
    ``_xNNNN_`` token inside ``<a:t>``, and DrawingML has no such escape, so
    PowerPoint renders those seven characters verbatim.
    """

    @pytest.mark.parametrize("sep", ["\r\n", "\r", "\x0b", "\x0c", "\n"])
    def test_every_line_break_flavour_becomes_a_real_paragraph(self, sep):
        _, slide = _blank_deck()
        box = add_textbox(
            slide,
            TextElement(type="text", bbox=[0, 0, 400, 200], content=f"line one{sep}line two"),
        )
        paras = box.text_frame.paragraphs
        assert [p.text for p in paras] == ["line one", "line two"]
        assert "_x" not in box.text_frame.text

    @pytest.mark.parametrize("junk", ["\x00", "\x07", "\x1f", "\x7f"])
    def test_stray_control_characters_are_dropped(self, junk):
        _, slide = _blank_deck()
        box = add_textbox(
            slide, TextElement(type="text", bbox=[0, 0, 400, 200], content=f"alpha{junk}beta")
        )
        assert box.text_frame.text == "alphabeta"

    def test_tabs_are_preserved(self):
        _, slide = _blank_deck()
        box = add_textbox(
            slide, TextElement(type="text", bbox=[0, 0, 400, 200], content="a\tb")
        )
        assert box.text_frame.text == "a\tb"

    @pytest.mark.parametrize("sep", ["\r\n", "\r", "\x0b"])
    def test_table_cells_are_cleaned_too(self, sep):
        _, slide = _blank_deck()
        el = TableElement(
            type="table", bbox=[0, 300, 500, 200], data=[[f"Region{sep}(2024)", "Users"]]
        )
        cell = add_table(slide, el).table.cell(0, 0)
        assert cell.text == "Region\n(2024)"
        assert "_x000D_" not in cell.text

    def test_no_escape_token_survives_into_the_saved_package(self, tmp_path):
        spec = SlideSpec.model_validate(
            {
                "elements": [
                    {
                        "type": "text",
                        "bbox": [10, 10, 600, 300],
                        "content": "Revenue up 12%\r\nChurn flat\rNPS 44\x0bdone\x00",
                    },
                    {
                        "type": "table",
                        "bbox": [10, 400, 600, 300],
                        "data": [["Region\r\n(2024)", "Users"], ["EMEA", "18,400"]],
                    },
                ]
            }
        )
        out = build_pptx(spec, tmp_path / "crlf.pptx")
        with zipfile.ZipFile(out) as zf:
            xml = zf.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "_x000D_" not in xml
        assert "_x000B_" not in xml
        assert "_x0000_" not in xml

        slide = Presentation(str(out)).slides[0]
        box = next(s for s in slide.shapes if s.has_text_frame and not s.has_table)
        assert [p.text for p in box.text_frame.paragraphs] == [
            "Revenue up 12%",
            "Churn flat",
            "NPS 44",
            "done",
        ]
        assert next(s for s in slide.shapes if s.has_table).table.cell(0, 0).text == (
            "Region\n(2024)"
        )


# ---------------------------------------------------------------------------
# build_presentation
# ---------------------------------------------------------------------------
class TestBuildPresentation:
    def test_slide_is_16_by_9_at_13_333_by_7_5_inches(self):
        prs = build_presentation(mock_data.sample_spec())
        assert abs(prs.slide_width - Inches(13.333)) <= INCH // 100
        assert abs(prs.slide_height - Inches(7.5)) <= INCH // 100
        ratio = prs.slide_width / prs.slide_height
        assert 1.77 < ratio < 1.79

    def test_the_slide_size_type_agrees_with_the_dimensions(self):
        """``cx``/``cy`` were 16:9 while ``type`` still said 4:3.

        python-pptx sets only cx/cy and leaves the default template's
        ``type="screen4x3"`` in place. PowerPoint lays out from cx/cy, so
        rendering was right, but the package advertised a size it did not have
        and some tools and linters read ``type``.
        """
        prs = build_presentation(mock_data.sample_spec())
        sld_sz = prs._element.sldSz
        assert sld_sz.get("type") == "screen16x9"
        assert sld_sz.get("cx") == str(int(prs.slide_width))
        assert sld_sz.get("cy") == str(int(prs.slide_height))

    def test_tagging_the_slide_size_never_breaks_a_build(self, monkeypatch):
        """A future python-pptx that hides the element must degrade to a no-op."""
        monkeypatch.setattr(
            pptx_builder, "_slide_size_element", lambda prs: None, raising=True
        )
        assert len(build_presentation(mock_data.sample_spec()).slides) == 1

        def boom(prs):
            raise RuntimeError("python-pptx moved the element")

        monkeypatch.setattr(pptx_builder, "_slide_size_element", boom, raising=True)
        assert len(build_presentation(mock_data.sample_spec()).slides) == 1

    def test_a_non_16x9_configuration_says_custom_rather_than_lying(self, monkeypatch):
        monkeypatch.setattr(config, "SLIDE_WIDTH_IN", 10.0, raising=False)
        monkeypatch.setattr(config, "SLIDE_HEIGHT_IN", 7.5, raising=False)
        prs = build_presentation(SlideSpec())
        assert prs._element.sldSz.get("type") == "custom"

    def test_exactly_one_slide(self):
        prs = build_presentation(mock_data.sample_spec())
        assert len(prs.slides) == 1

    def test_every_element_becomes_exactly_one_native_shape(self):
        spec = mock_data.sample_spec()
        spec.background_color = None  # keep the shape list to our elements only
        prs = build_presentation(spec)
        shapes = list(prs.slides[0].shapes)
        assert len(shapes) == len(spec.elements)

    def test_element_types_map_to_the_right_shape_types(self):
        spec = mock_data.sample_spec()
        spec.background_color = None
        prs = build_presentation(spec)
        rendered = [_category(s) for s in prs.slides[0].shapes]
        expected = _expected_categories(spec)
        assert rendered.count("shape") == expected.count("shape")
        assert rendered.count("table") == expected.count("table")
        assert rendered.count("text") == expected.count("text")
        assert not [c for c in rendered if c.startswith("unexpected")]

    def test_z_order_is_preserved_element_for_element(self):
        spec = mock_data.sample_spec()
        spec.background_color = None
        prs = build_presentation(spec)
        assert [_category(s) for s in prs.slides[0].shapes] == _expected_categories(spec)

    def test_no_placeholders_leak_in_from_the_layout(self):
        """The blank layout (index 6) must be used, so nothing is inherited."""
        spec = mock_data.sample_spec()
        spec.background_color = None
        prs = build_presentation(spec)
        assert [s for s in prs.slides[0].shapes if s.is_placeholder] == []

    def test_table_content_survives_the_round_trip(self):
        spec = mock_data.sample_spec()
        table_el = next(el for el in spec.elements if isinstance(el, TableElement))
        prs = build_presentation(spec)
        frame = next(s for s in prs.slides[0].shapes if s.has_table)
        table = frame.table
        assert len(table.rows) == len(table_el.data)
        assert len(table.columns) == len(table_el.data[0])
        assert table.cell(0, 0).text == table_el.data[0][0]

    def test_all_textboxes_word_wrap(self):
        prs = build_presentation(mock_data.sample_spec())
        boxes = [s for s in prs.slides[0].shapes if _category(s) == "text"]
        assert boxes
        assert all(s.text_frame.word_wrap is True for s in boxes)

    def test_background_colour_does_not_break_the_build(self):
        spec = mock_data.sample_spec()
        spec.background_color = "#102030"
        assert len(build_presentation(spec).slides) == 1

    def test_empty_spec_builds_an_empty_slide(self):
        prs = build_presentation(SlideSpec())
        assert len(prs.slides) == 1
        assert len(prs.slides[0].shapes) == 0

    def test_bad_colours_degrade_instead_of_aborting(self):
        """Colours that never went through the validator must not crash us."""
        spec = mock_data.sample_spec()
        spec.background_color = None
        shape_el = next(el for el in spec.elements if isinstance(el, ShapeElement))
        text_el = next(el for el in spec.elements if isinstance(el, TextElement))
        table_el = next(el for el in spec.elements if isinstance(el, TableElement))
        _force(shape_el, "fill_color", "banana")
        _force(shape_el, "border_color", "#ZZZZZZ")
        _force(text_el, "font_color", "chartreuse-ish")
        _force(table_el, "header_bg_color", "")
        prs = build_presentation(spec)
        assert len(prs.slides[0].shapes) == len(spec.elements)

    def test_one_broken_element_does_not_abort_the_whole_build(self, capsys):
        spec = mock_data.sample_spec()
        spec.background_color = None
        broken = spec.elements[1]
        _force(broken, "bbox", None)  # scale_bbox will blow up on this one
        prs = build_presentation(spec)
        shapes = list(prs.slides[0].shapes)
        assert len(shapes) == len(spec.elements) - 1, "only the broken element may be skipped"
        captured = capsys.readouterr()
        assert captured.err.strip(), "a skipped element must be reported on stderr"

    def test_shape_elements_render_without_text(self):
        spec = mock_data.sample_spec()
        spec.background_color = None
        prs = build_presentation(spec)
        for shape in prs.slides[0].shapes:
            if _category(shape) == "shape" and shape.has_text_frame:
                assert shape.text_frame.text == ""


# ---------------------------------------------------------------------------
# build_pptx
# ---------------------------------------------------------------------------
class TestBuildPptx:
    def test_writes_a_file_and_returns_a_resolved_path(self, tmp_path):
        out = tmp_path / "deck.pptx"
        result = build_pptx(mock_data.sample_spec(), out)
        assert result.exists() and result.is_file()
        assert result.is_absolute()
        assert result == out.resolve()
        assert out.stat().st_size > 0

    def test_creates_missing_parent_directories(self, tmp_path):
        out = tmp_path / "nested" / "deeper" / "deck.pptx"
        result = build_pptx(mock_data.sample_spec(), out)
        assert result.exists()

    def test_accepts_a_string_path(self, tmp_path):
        result = build_pptx(mock_data.sample_spec(), str(tmp_path / "s.pptx"))
        assert result.exists()

    def test_python_pptx_can_reopen_the_file(self, tmp_path):
        out = build_pptx(mock_data.sample_spec(), tmp_path / "deck.pptx")
        prs = Presentation(str(out))
        assert len(prs.slides) == 1
        assert len(prs.slides[0].shapes) >= 1

    def test_the_saved_package_declares_a_16x9_slide_size(self, tmp_path):
        """The regression is in the written XML, so assert on the written XML."""
        out = build_pptx(mock_data.sample_spec(), tmp_path / "deck.pptx")
        with zipfile.ZipFile(out) as zf:
            xml = zf.read("ppt/presentation.xml").decode("utf-8")
        sld_sz = re.search(r"<p:sldSz[^>]*>", xml)
        assert sld_sz is not None, xml[:400]
        declared = sld_sz.group(0)
        assert 'type="screen16x9"' in declared, declared
        assert "screen4x3" not in xml, "the template's 4:3 label survived the save"
        assert f'cx="{int(Inches(13.333))}"' in declared, declared
        assert f'cy="{int(Inches(7.5))}"' in declared, declared

    def test_a_reopened_deck_keeps_the_16x9_slide_size_type(self, tmp_path):
        out = build_pptx(mock_data.sample_spec(), tmp_path / "deck.pptx")
        assert Presentation(str(out))._element.sldSz.get("type") == "screen16x9"

    def test_reopened_deck_contains_no_picture_shapes(self, tmp_path):
        """The whole point of the tool: native objects, not a flattened image."""
        out = build_pptx(mock_data.sample_spec(), tmp_path / "deck.pptx")
        prs = Presentation(str(out))
        slide = prs.slides[0]

        for shape in slide.shapes:
            assert shape.shape_type != MSO_SHAPE_TYPE.PICTURE, "found a flattened picture shape"

        xml = slide.shapes._spTree.xml
        assert "<p:pic" not in xml, "slide XML contains a picture element"
        assert "<a:blip" not in xml, "slide XML embeds an image blip"

        with zipfile.ZipFile(out) as zf:
            media = [n for n in zf.namelist() if n.startswith("ppt/media/")]
        assert media == [], f"package embeds image parts: {media}"

    def test_reopened_deck_still_has_native_editable_content(self, tmp_path):
        out = build_pptx(mock_data.sample_spec(), tmp_path / "deck.pptx")
        slide = Presentation(str(out)).slides[0]
        cats = [_category(s) for s in slide.shapes]
        assert "shape" in cats and "table" in cats and "text" in cats

        texts = [s.text_frame.text for s in slide.shapes if s.has_text_frame]
        assert any("Quarterly Platform Review" in t for t in texts), "title text is not editable"

        table = next(s for s in slide.shapes if s.has_table).table
        assert table.cell(0, 0).text == "Region"
        assert table.cell(1, 1).text == "18,400"

    def test_multiline_text_survives_as_separate_paragraphs(self, tmp_path):
        out = build_pptx(mock_data.sample_spec(), tmp_path / "deck.pptx")
        slide = Presentation(str(out)).slides[0]
        multi = [
            s
            for s in slide.shapes
            if s.has_text_frame and len(s.text_frame.paragraphs) > 1
        ]
        assert multi, "the multi-line textbox collapsed into one paragraph"
        assert any("latency" in s.text_frame.text for s in multi)
