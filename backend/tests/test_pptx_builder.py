"""Builder tests: geometry, styling, z-order, and the anti-flattening proof.

The most important tests in this file are the anti-flattening ones -
:class:`TestFlatteningReport` and
:meth:`TestBuildPptx.test_a_spec_without_image_elements_embeds_no_picture_at_all`.
They are what proves the tool produced an *editable* deck instead of pasting the
input image onto a slide. "Zero picture shapes" is no longer the rule, because a
photo/chart region is now a legitimate cropped sub-image; the rule is
:func:`pptx_builder.flattening_report`, and it is shared with the API so there is
exactly one definition of "flattened".
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import get_args

import pytest
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_FILL, MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Inches, Pt

from app import config
from app import mock_data
from app import pptx_builder
from app.pptx_builder import (
    ARROW_HEAD_TYPE,
    CONNECTOR_MAP,
    FULL_BLEED_PICTURE_PCT,
    MAX_PICTURE_AREA_PCT,
    PLAIN_TABLE_STYLE_ID,
    SHAPE_MAP,
    TABLE_FONT_DARK,
    TABLE_FONT_LIGHT,
    PictureSourceError,
    PptxBuildError,
    add_connector,
    add_picture,
    add_shape,
    add_table,
    add_textbox,
    build_pptx,
    build_presentation,
    flattening_report,
    scale_bbox,
    scale_point,
    to_rgb,
)
from app.schemas import (
    BBox,
    ConnectorElement,
    ConnectorKind,
    ImageElement,
    Point,
    ShapeElement,
    ShapeKind,
    SlideSpec,
    TableElement,
    TextElement,
)

# 0.0001 inch: guards against float-ordering noise without letting a real
# geometry bug through.
EMU_TOL = 100
INCH = 914400

#: A source image with four flat, unmistakable quadrants. Deliberately NOT
#: square and not a multiple of the 0..1000 grid, so a crop that mixes the two
#: axes up - or rounds sloppily - lands on the wrong colour and fails loudly.
SOURCE_PX = (400, 200)
QUADRANTS = {
    "top_left": ((0, 0, 200, 100), (255, 0, 0)),
    "top_right": ((200, 0, 400, 100), (0, 255, 0)),
    "bottom_left": ((0, 100, 200, 200), (0, 0, 255)),
    "bottom_right": ((200, 100, 400, 200), (255, 255, 0)),
}
#: The same four quadrants in the normalized 0..1000 bbox system.
QUADRANT_BBOX = {
    "top_left": [0, 0, 500, 500],
    "top_right": [500, 0, 500, 500],
    "bottom_left": [0, 500, 500, 500],
    "bottom_right": [500, 500, 500, 500],
}


def _source_image() -> Image.Image:
    """The four-quadrant source image (see :data:`QUADRANTS`)."""
    image = Image.new("RGB", SOURCE_PX, (0, 0, 0))
    for box, color in QUADRANTS.values():
        image.paste(Image.new("RGB", (box[2] - box[0], box[3] - box[1]), color), box[:2])
    return image


def _source_bytes(image: Image.Image | None = None, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    (image or _source_image()).save(buffer, format=fmt)
    return buffer.getvalue()


def _source_path(tmp_path, image: Image.Image | None = None):
    path = tmp_path / "source.png"
    path.write_bytes(_source_bytes(image))
    return path


def _pictures(slide) -> list:
    return [s for s in slide.shapes if s.shape_type in
            (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE)]


def _connectors(slide) -> list:
    return [s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.LINE]


def _embedded_crop(picture) -> Image.Image:
    """The image bytes that really went INTO the deck, decoded again."""
    return Image.open(io.BytesIO(picture.image.blob)).convert("RGB")


def _colors(image: Image.Image) -> set:
    """Every distinct colour in *image* (the crop is flat, so this is tiny)."""
    counted = image.getcolors(maxcolors=1 << 24)
    assert counted is not None, "the crop has an implausible number of colours"
    return {color for _count, color in counted}


def _ln_children(shape) -> list[str]:
    """The local tag names of the shape's ``a:ln`` children, in document order."""
    ln = shape.line._get_or_add_ln()
    return [child.tag.split("}")[-1] for child in ln]


def _category(shape) -> str:
    """Classify a rendered shape the same way the spec classifies elements."""
    if shape.has_table:
        return "table"
    stype = shape.shape_type
    if stype == MSO_SHAPE_TYPE.TEXT_BOX:
        return "text"
    if stype == MSO_SHAPE_TYPE.AUTO_SHAPE:
        return "shape"
    # A connector reports as LINE; there is no MSO_SHAPE_TYPE.CONNECTOR member.
    if stype == MSO_SHAPE_TYPE.LINE:
        return "connector"
    if stype in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE):
        return "image"
    return f"unexpected({stype})"


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
    # Derived from the schema literal, so a kind added there without a preset
    # here (which would silently render as a rectangle) fails this test.
    assert set(SHAPE_MAP) == set(get_args(ShapeKind))
    assert SHAPE_MAP["rectangle"] == MSO_SHAPE.RECTANGLE
    assert SHAPE_MAP["rounded_rectangle"] == MSO_SHAPE.ROUNDED_RECTANGLE
    assert SHAPE_MAP["circle"] == MSO_SHAPE.OVAL
    assert SHAPE_MAP["arrow_right"] == MSO_SHAPE.RIGHT_ARROW
    assert SHAPE_MAP["arrow_left"] == MSO_SHAPE.LEFT_ARROW
    assert SHAPE_MAP["arrow_up"] == MSO_SHAPE.UP_ARROW
    assert SHAPE_MAP["arrow_down"] == MSO_SHAPE.DOWN_ARROW


def test_connector_map_covers_every_connector_kind():
    assert set(CONNECTOR_MAP) == set(get_args(ConnectorKind))
    assert CONNECTOR_MAP["straight"] == MSO_CONNECTOR.STRAIGHT
    assert CONNECTOR_MAP["elbow"] == MSO_CONNECTOR.ELBOW


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
# scale_point: connector endpoints
# ---------------------------------------------------------------------------
class TestScalePoint:
    def test_maps_the_full_coordinate_space_onto_the_slide(self):
        assert scale_point(Point(x=0, y=0)) == (0, 0)
        x, y = scale_point(Point(x=1000, y=1000))
        assert abs(x - Inches(13.333)) <= EMU_TOL
        assert abs(y - Inches(7.5)) <= EMU_TOL

    def test_x_scales_by_width_and_y_by_height(self):
        """The two axes are scaled independently - like scale_bbox, not squared."""
        x, y = scale_point(Point(x=500, y=500))
        assert abs(x - Inches(0.5 * 13.333)) <= EMU_TOL
        assert abs(y - Inches(0.5 * 7.5)) <= EMU_TOL
        assert x != y, "a 1:1 point must NOT land on the same EMU on both axes"

    def test_returns_two_integral_emu_values(self):
        values = scale_point(Point(x=123, y=456))
        assert len(values) == 2
        assert all(isinstance(value, int) for value in values)  # Emu subclasses int

    def test_a_point_that_fits_is_silent(self, capsys):
        scale_point(Point(x=250, y=750))
        assert capsys.readouterr().err == ""

    def test_an_out_of_range_point_is_clamped_and_reported(self, capsys):
        """Point already clamps 0..1000; a forced value may still not move silently."""
        point = Point(x=1, y=1)
        _force(point, "x", 2000)
        x, y = scale_point(point)
        assert abs(x - Inches(13.333)) <= EMU_TOL
        err = capsys.readouterr().err
        assert err.strip(), "a moved endpoint must not be silent"
        assert "outside the" in err and "slide" in err, err

    def test_an_unreadable_component_raises_so_the_element_is_skipped(self):
        point = Point(x=1, y=1)
        _force(point, "y", "banana")
        with pytest.raises((TypeError, ValueError)):
            scale_point(point)


# ---------------------------------------------------------------------------
# add_connector
# ---------------------------------------------------------------------------
def _connector(**kwargs) -> ConnectorElement:
    payload = {
        "type": "connector",
        "start_point": [100, 200],
        "end_point": [800, 600],
    }
    payload.update(kwargs)
    return ConnectorElement.model_validate(payload)


class TestAddConnector:
    def test_geometry_matches_the_two_points(self):
        _, slide = _blank_deck()
        el = _connector()
        shape = add_connector(slide, el)
        assert (shape.begin_x, shape.begin_y) == scale_point(el.start_point)
        assert (shape.end_x, shape.end_y) == scale_point(el.end_point)

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [("straight", MSO_CONNECTOR.STRAIGHT), ("elbow", MSO_CONNECTOR.ELBOW)],
    )
    def test_the_kind_selects_the_connector_preset(self, kind, expected):
        _, slide = _blank_deck()
        shape = add_connector(slide, _connector(kind=kind))
        prst = shape._element.spPr.find(
            "{http://schemas.openxmlformats.org/drawingml/2006/main}prstGeom"
        )
        assert prst is not None
        assert prst.get("prst") == ("bentConnector3" if kind == "elbow" else "line")

    def test_a_connector_reports_as_a_line_shape(self):
        """Verified: MSO_SHAPE_TYPE.CONNECTOR does not exist; a connector is LINE.

        The preview detects connectors by this value, so it is asserted here.
        """
        _, slide = _blank_deck()
        shape = add_connector(slide, _connector())
        assert shape.shape_type == MSO_SHAPE_TYPE.LINE
        assert _category(shape) == "connector"

    def test_colour_and_width_are_applied(self):
        _, slide = _blank_deck()
        shape = add_connector(slide, _connector(color="#1E3A6E", width_pt=1.5))
        assert shape.line.color.rgb == RGBColor(0x1E, 0x3A, 0x6E)
        assert shape.line.width == Pt(1.5)

    def test_a_missing_width_falls_back_to_the_config_default(self):
        _, slide = _blank_deck()
        shape = add_connector(slide, _connector(color="#000000"))
        assert shape.line.width == Pt(config.DEFAULT_BORDER_WIDTH_PT)

    def test_a_garbage_width_degrades_to_the_config_default(self):
        _, slide = _blank_deck()
        el = _connector()
        _force(el, "width_pt", "wide-ish")
        shape = add_connector(slide, el)
        assert shape.line.width == Pt(config.DEFAULT_BORDER_WIDTH_PT)

    def test_a_garbage_colour_degrades_to_no_explicit_colour(self):
        """No colour must mean "theme line", NOT "invisible" and NOT a crash.

        A connector is a line: making it invisible (what a shape's missing
        border colour means) would delete the arrow the diagram is about.
        """
        _, slide = _blank_deck()
        el = _connector()
        _force(el, "color", "banana")
        shape = add_connector(slide, el)  # must not raise
        assert "solidFill" not in _ln_children(shape)
        assert "noFill" not in _ln_children(shape), "a connector may not be invisible"
        assert shape.shape_type == MSO_SHAPE_TYPE.LINE

    @pytest.mark.parametrize(
        ("dash_style", "expected"),
        [("solid", MSO_LINE_DASH_STYLE.SOLID), ("dashed", MSO_LINE_DASH_STYLE.DASH)],
    )
    def test_the_dash_style_is_written_explicitly(self, dash_style, expected):
        _, slide = _blank_deck()
        shape = add_connector(slide, _connector(dash_style=dash_style))
        assert shape.line.dash_style == expected, "the dash style may not be inherited"

    @pytest.mark.parametrize(
        ("arrow_start", "arrow_end", "expected"),
        [
            (False, False, []),
            (True, False, ["headEnd"]),
            (False, True, ["tailEnd"]),
            (True, True, ["headEnd", "tailEnd"]),
        ],
    )
    def test_arrowheads_are_written_on_the_requested_ends(
        self, arrow_start, arrow_end, expected
    ):
        """``a:headEnd`` is the BEGIN point's arrowhead, ``a:tailEnd`` the END's."""
        _, slide = _blank_deck()
        shape = add_connector(
            slide, _connector(arrow_start=arrow_start, arrow_end=arrow_end)
        )
        children = _ln_children(shape)
        assert [tag for tag in children if tag.endswith("End")] == expected
        for tag in ("a:headEnd", "a:tailEnd"):
            local = tag.split(":")[1]
            element = shape.line._get_or_add_ln().find(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}" + local
            )
            if local in expected:
                assert element is not None
                assert element.get("type") == ARROW_HEAD_TYPE
            else:
                assert element is None

    def test_arrowheads_come_after_the_dash_style_in_the_xml(self):
        """CT_LineProperties order is [fill, prstDash, ..., headEnd, tailEnd].

        Colour/width/dash are set first and the arrowheads appended last for
        exactly this reason; a wrong order is an invalid package.
        """
        _, slide = _blank_deck()
        shape = add_connector(
            slide,
            _connector(color="#FF0000", width_pt=2, dash_style="dashed",
                       arrow_start=True, arrow_end=True),
        )
        assert _ln_children(shape) == ["solidFill", "prstDash", "headEnd", "tailEnd"]

    def test_a_right_to_left_connector_keeps_its_direction(self):
        """python-pptx flips the frame; begin/end must still be begin/end.

        The arrowheads are tied to the line's ends, so a connector drawn
        backwards would otherwise point the wrong way.
        """
        _, slide = _blank_deck()
        el = _connector(start_point=[900, 800], end_point=[100, 200])
        shape = add_connector(slide, el)
        assert (shape.begin_x, shape.begin_y) == scale_point(el.start_point)
        assert (shape.end_x, shape.end_y) == scale_point(el.end_point)

    def test_two_identical_points_do_not_break_the_build(self):
        _, slide = _blank_deck()
        el = _connector(start_point=[500, 500], end_point=[500, 500])
        shape = add_connector(slide, el)
        assert (shape.begin_x, shape.begin_y) == (shape.end_x, shape.end_y)

    def test_setting_the_arrowheads_twice_does_not_duplicate_them(self):
        _, slide = _blank_deck()
        shape = add_connector(slide, _connector(arrow_start=True, arrow_end=True))
        pptx_builder._set_arrow_heads(shape, start=True, end=False)
        assert [tag for tag in _ln_children(shape) if tag.endswith("End")] == ["headEnd"]


class TestConnectorsInTheSavedPackage:
    """The arrowheads are raw XML, so prove they survive a real save/reload."""

    #: All four arrow_start/arrow_end combinations, plus both kinds, both dash
    #: styles, and an explicit colour/width - everything the writer can emit.
    ELEMENTS = [
        {"type": "connector", "start_point": [100, 300], "end_point": [480, 300],
         "arrow_start": False, "arrow_end": True, "color": "#1E3A6E", "width_pt": 1.5},
        {"type": "connector", "kind": "elbow", "start_point": [480, 300],
         "end_point": [900, 700], "arrow_start": True, "arrow_end": True,
         "dash_style": "dashed"},
        {"type": "connector", "start_point": [100, 800], "end_point": [900, 800],
         "arrow_start": False, "arrow_end": False},
        {"type": "connector", "start_point": [100, 900], "end_point": [900, 900],
         "arrow_start": True, "arrow_end": False},
    ]
    #: The ``a:ln`` arrowhead children each of the above must end up with.
    EXPECTED_ENDS = [["tailEnd"], ["headEnd", "tailEnd"], [], ["headEnd"]]

    @pytest.fixture()
    def saved(self, tmp_path):
        spec = SlideSpec.model_validate({"elements": list(self.ELEMENTS)})
        out = build_pptx(spec, tmp_path / "connectors.pptx")
        return out, Presentation(str(out)).slides[0]

    def test_every_connector_reaches_the_package_with_its_endpoints(self, saved):
        _, slide = saved
        shapes = _connectors(slide)
        assert len(shapes) == len(self.ELEMENTS)
        for shape, payload in zip(shapes, self.ELEMENTS):
            expected_begin = scale_point(Point.model_validate(payload["start_point"]))
            expected_end = scale_point(Point.model_validate(payload["end_point"]))
            assert (shape.begin_x, shape.begin_y) == expected_begin
            assert (shape.end_x, shape.end_y) == expected_end

    def test_the_elbow_stays_an_elbow(self, saved):
        _, slide = saved
        elbow = _connectors(slide)[1]
        assert "bentConnector3" in elbow._element.xml

    def test_every_arrowhead_combination_survives_the_round_trip(self, saved):
        _, slide = saved
        shapes = _connectors(slide)
        assert len(shapes) == len(self.EXPECTED_ENDS)
        for shape, expected in zip(shapes, self.EXPECTED_ENDS):
            assert [t for t in _ln_children(shape) if t.endswith("End")] == expected

    def test_the_child_order_in_the_saved_file_is_valid(self, saved):
        _, slide = saved
        for shape, expected in zip(_connectors(slide), self.EXPECTED_ENDS):
            children = _ln_children(shape)
            # prstDash is always written, and every arrowhead must follow it.
            assert "prstDash" in children
            for tag in expected:
                assert children.index("prstDash") < children.index(tag), (
                    "an arrowhead before a:prstDash is an invalid package"
                )
            if expected == ["headEnd", "tailEnd"]:
                assert children.index("headEnd") < children.index("tailEnd")

    def test_the_dash_styles_survive_the_round_trip(self, saved):
        _, slide = saved
        solid, dashed, _bare, _back = _connectors(slide)
        assert solid.line.dash_style == MSO_LINE_DASH_STYLE.SOLID
        assert dashed.line.dash_style == MSO_LINE_DASH_STYLE.DASH

    def test_the_colour_and_width_survive_the_round_trip(self, saved):
        _, slide = saved
        styled = _connectors(slide)[0]
        assert styled.line.color.rgb == RGBColor(0x1E, 0x3A, 0x6E)
        assert styled.line.width == Pt(1.5)

    def test_the_arrowheads_are_in_the_written_slide_xml(self, saved):
        out, _ = saved
        with zipfile.ZipFile(out) as zf:
            xml = zf.read("ppt/slides/slide1.xml").decode("utf-8")
        assert f'<a:headEnd type="{ARROW_HEAD_TYPE}"' in xml
        assert f'<a:tailEnd type="{ARROW_HEAD_TYPE}"' in xml
        assert '<a:prstDash val="dash"/>' in xml

    def test_a_connector_is_not_a_picture(self, saved):
        out, slide = saved
        assert _pictures(slide) == []
        with zipfile.ZipFile(out) as zf:
            assert [n for n in zf.namelist() if n.startswith("ppt/media/")] == []


# ---------------------------------------------------------------------------
# block arrows (shape.kind), which are auto shapes and not connectors
# ---------------------------------------------------------------------------
class TestBlockArrows:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("arrow_right", MSO_SHAPE.RIGHT_ARROW),
            ("arrow_left", MSO_SHAPE.LEFT_ARROW),
            ("arrow_up", MSO_SHAPE.UP_ARROW),
            ("arrow_down", MSO_SHAPE.DOWN_ARROW),
        ],
    )
    def test_each_direction_maps_to_its_preset(self, kind, expected):
        _, slide = _blank_deck()
        shape = add_shape(
            slide, ShapeElement(type="shape", kind=kind, bbox=[100, 100, 200, 100])
        )
        assert shape.auto_shape_type == expected
        assert shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE

    def test_a_block_arrow_is_styled_like_any_other_shape(self):
        _, slide = _blank_deck()
        shape = add_shape(
            slide,
            ShapeElement(
                type="shape",
                kind="arrow_right",
                bbox=[100, 100, 200, 100],
                fill_color="#1F3B73",
                border_color="#FF0000",
                border_width=2.0,
            ),
        )
        assert shape.fill.fore_color.rgb == RGBColor(0x1F, 0x3B, 0x73)
        assert shape.line.color.rgb == RGBColor(0xFF, 0x00, 0x00)
        assert shape.line.width == Pt(2.0)
        assert shape.text_frame.text == ""

    def test_the_arrows_survive_the_saved_package(self, tmp_path):
        spec = SlideSpec.model_validate(
            {
                "elements": [
                    {"type": "shape", "kind": kind, "bbox": [i * 200, 100, 150, 100]}
                    for i, kind in enumerate(
                        ("arrow_right", "arrow_left", "arrow_up", "arrow_down")
                    )
                ]
            }
        )
        out = build_pptx(spec, tmp_path / "arrows.pptx")
        slide = Presentation(str(out)).slides[0]
        assert [s.auto_shape_type for s in slide.shapes] == [
            MSO_SHAPE.RIGHT_ARROW,
            MSO_SHAPE.LEFT_ARROW,
            MSO_SHAPE.UP_ARROW,
            MSO_SHAPE.DOWN_ARROW,
        ]


# ---------------------------------------------------------------------------
# add_picture: the cropped sub-image
# ---------------------------------------------------------------------------
class TestAddPicture:
    def test_the_crop_lands_at_the_bbox_position_and_size(self):
        _, slide = _blank_deck()
        el = ImageElement(type="image", bbox=[500, 0, 300, 400])
        picture = add_picture(slide, el, _source_bytes())
        left, top, width, height = scale_bbox(el.bbox)
        assert abs(picture.left - left) <= EMU_TOL
        assert abs(picture.top - top) <= EMU_TOL
        assert abs(picture.width - width) <= EMU_TOL
        assert abs(picture.height - height) <= EMU_TOL
        assert picture.shape_type == MSO_SHAPE_TYPE.PICTURE

    @pytest.mark.parametrize("quadrant", sorted(QUADRANT_BBOX))
    def test_the_embedded_pixels_really_are_the_requested_region(self, quadrant):
        """The test that proves the crop maths, so it is strict on purpose.

        x/w map onto the source's WIDTH and y/h onto its HEIGHT. The source is
        400x200 - deliberately not square - so swapping the axes, or scaling
        both by the same number, picks a different quadrant and fails here.
        """
        _, slide = _blank_deck()
        box, color = QUADRANTS[quadrant]
        picture = add_picture(
            slide,
            ImageElement(type="image", bbox=QUADRANT_BBOX[quadrant]),
            _source_bytes(),
        )
        crop = _embedded_crop(picture)
        assert crop.size == (box[2] - box[0], box[3] - box[1])
        assert _colors(crop) == {color}, f"{quadrant} crop is not the right region"

    def test_an_off_grid_region_is_cropped_by_the_same_maths(self):
        """A region that is not a whole quadrant: 1/8 of the width, 1/4 of it."""
        _, slide = _blank_deck()
        picture = add_picture(
            slide, ImageElement(type="image", bbox=[250, 500, 250, 250]), _source_bytes()
        )
        crop = _embedded_crop(picture)
        # x: 250..500 of 1000 -> 100..200px of 400; y: 500..750 -> 100..150px of 200.
        assert crop.size == (100, 50)
        assert _colors(crop) == {QUADRANTS["bottom_left"][1]}

    def test_the_crop_fills_the_bbox_even_when_the_aspect_ratios_differ(self):
        """Documented behaviour: the axes scale independently, so it stretches."""
        _, slide = _blank_deck()
        el = ImageElement(type="image", bbox=[0, 0, 500, 100])
        picture = add_picture(slide, el, _source_bytes())
        _, _, width, height = scale_bbox(el.bbox)
        assert abs(picture.width - width) <= EMU_TOL
        assert abs(picture.height - height) <= EMU_TOL
        crop = _embedded_crop(picture)
        assert crop.size == (200, 20), "the crop itself follows the bbox, not the box"

    def test_the_source_may_be_raw_bytes(self):
        _, slide = _blank_deck()
        picture = add_picture(
            slide, ImageElement(type="image", bbox=QUADRANT_BBOX["top_right"]),
            _source_bytes(),
        )
        assert _colors(_embedded_crop(picture)) == {QUADRANTS["top_right"][1]}

    def test_the_source_may_be_a_path(self, tmp_path):
        _, slide = _blank_deck()
        picture = add_picture(
            slide, ImageElement(type="image", bbox=QUADRANT_BBOX["top_right"]),
            _source_path(tmp_path),
        )
        assert _colors(_embedded_crop(picture)) == {QUADRANTS["top_right"][1]}

    def test_the_source_may_be_a_path_string(self, tmp_path):
        _, slide = _blank_deck()
        picture = add_picture(
            slide, ImageElement(type="image", bbox=QUADRANT_BBOX["bottom_right"]),
            str(_source_path(tmp_path)),
        )
        assert _colors(_embedded_crop(picture)) == {QUADRANTS["bottom_right"][1]}

    def test_a_jpeg_source_works_too(self):
        _, slide = _blank_deck()
        picture = add_picture(
            slide, ImageElement(type="image", bbox=QUADRANT_BBOX["top_left"]),
            _source_bytes(fmt="JPEG"),
        )
        assert picture.width > 0  # decoded, cropped and embedded

    @pytest.mark.parametrize(
        ("source", "phrase"),
        [
            (None, "no source image"),
            (b"", "empty"),
            (b"this is not an image", "could not be decoded"),
            ("/nonexistent/source.png", "could not be decoded"),
        ],
    )
    def test_an_unusable_source_raises_picture_source_error(self, source, phrase):
        _, slide = _blank_deck()
        el = ImageElement(type="image", bbox=[100, 100, 200, 200])
        with pytest.raises(PictureSourceError) as excinfo:
            add_picture(slide, el, source)
        assert phrase in str(excinfo.value)
        assert len(slide.shapes) == 0, "a failed crop may not leave a shape behind"

    def test_a_truncated_png_is_rejected_rather_than_half_drawn(self):
        _, slide = _blank_deck()
        truncated = _source_bytes()[: len(_source_bytes()) // 2]
        with pytest.raises(PictureSourceError):
            add_picture(slide, ImageElement(type="image", bbox=[0, 0, 200, 200]), truncated)
        assert len(slide.shapes) == 0

    def test_an_already_decoded_pil_image_is_accepted(self):
        """build_presentation decodes once and reuses the image per element."""
        _, slide = _blank_deck()
        picture = add_picture(
            slide, ImageElement(type="image", bbox=QUADRANT_BBOX["bottom_left"]),
            _source_image(),
        )
        assert _colors(_embedded_crop(picture)) == {QUADRANTS["bottom_left"][1]}

    def test_a_cmyk_source_is_converted_rather_than_failing(self):
        _, slide = _blank_deck()
        cmyk = Image.new("CMYK", SOURCE_PX, (0, 0, 0, 0))
        buffer = io.BytesIO()
        cmyk.save(buffer, format="TIFF")
        picture = add_picture(
            slide, ImageElement(type="image", bbox=[0, 0, 400, 400]), buffer.getvalue()
        )
        assert picture.width > 0


# ---------------------------------------------------------------------------
# a missing source image: skipped, reported, and marked in the deck
# ---------------------------------------------------------------------------
class TestImageWithoutASourceImage:
    """``/api/preview``, ``/api/build-from-json`` and ``--from-json`` have no image.

    The element must not be silently omitted (a mystery hole) and must not fail
    the build (the rest of the slide is fine): it is skipped through the normal
    per-element isolation, reported as NOT rendered, and outlined.
    """

    SPEC = {
        "elements": [
            {"type": "text", "bbox": [0, 0, 400, 100], "content": "Quarterly"},
            {"type": "image", "bbox": [500, 200, 300, 400], "description": "team photo"},
        ]
    }

    def _build(self, **kwargs):
        return build_presentation(SlideSpec.model_validate(self.SPEC), **kwargs)

    def test_the_rest_of_the_slide_is_intact(self, capsys):
        prs = self._build()
        capsys.readouterr()
        slide = prs.slides[0]
        assert _pictures(slide) == []
        texts = [s for s in slide.shapes if _category(s) == "text"]
        assert len(texts) == 1 and "Quarterly" in texts[0].text_frame.text

    def test_the_warning_says_it_was_not_rendered_and_why(self, capsys):
        self._build()
        err = capsys.readouterr().err
        assert "was NOT rendered" in err, err
        assert "image" in err
        assert "no source image" in err, err
        assert "re-run the conversion" in err, err

    def test_a_placeholder_outline_marks_the_gap(self, capsys):
        prs = self._build()
        assert "placeholder" in capsys.readouterr().err
        slide = prs.slides[0]
        shapes = [s for s in slide.shapes if _category(s) == "shape"]
        assert len(shapes) == 1, "exactly one placeholder outline"
        placeholder = shapes[0]
        left, top, width, height = scale_bbox(BBox.model_validate([500, 200, 300, 400]))
        assert abs(placeholder.left - left) <= EMU_TOL
        assert abs(placeholder.top - top) <= EMU_TOL
        assert abs(placeholder.width - width) <= EMU_TOL
        assert abs(placeholder.height - height) <= EMU_TOL
        assert placeholder.fill.type == MSO_FILL.BACKGROUND, "it must not look like content"
        assert placeholder.line.dash_style == MSO_LINE_DASH_STYLE.DASH
        assert placeholder.text_frame.text == ""

    def test_the_deck_still_saves_and_reopens(self, tmp_path):
        out = build_pptx(SlideSpec.model_validate(self.SPEC), tmp_path / "nosource.pptx")
        slide = Presentation(str(out)).slides[0]
        assert len(slide.shapes) == 2
        assert _pictures(slide) == []

    def test_an_undecodable_source_is_reported_the_same_way(self, capsys):
        prs = self._build(source_image=b"nonsense, not a PNG")
        err = capsys.readouterr().err
        assert "was NOT rendered" in err
        assert "could not be decoded" in err, err
        assert _pictures(prs.slides[0]) == []
        assert len([s for s in prs.slides[0].shapes if _category(s) == "shape"]) == 1

    def test_a_missing_source_file_is_reported_the_same_way(self, capsys, tmp_path):
        prs = self._build(source_image=tmp_path / "does-not-exist.png")
        err = capsys.readouterr().err
        assert "was NOT rendered" in err and "could not be decoded" in err
        assert _pictures(prs.slides[0]) == []

    def test_every_image_element_gets_its_own_reason(self, capsys):
        spec = SlideSpec.model_validate(
            {
                "elements": [
                    {"type": "text", "bbox": [0, 0, 400, 100], "content": "t"},
                    {"type": "image", "bbox": [0, 200, 200, 200]},
                    {"type": "image", "bbox": [400, 200, 200, 200]},
                ]
            }
        )
        build_presentation(spec, b"nonsense")
        err = capsys.readouterr().err
        assert err.count("was NOT rendered") == 2, err

    def test_an_unusable_source_is_ignored_when_no_element_needs_it(self, capsys):
        """Decoding is lazy: a spec with no image element never touches it."""
        prs = build_presentation(mock_data.sample_spec(), b"not an image at all")
        assert capsys.readouterr().err == ""
        assert len(prs.slides[0].shapes) == len(mock_data.sample_spec().elements)


# ---------------------------------------------------------------------------
# the anti-flattening rule (shared with the API via flattening_report)
# ---------------------------------------------------------------------------
class TestFlatteningReport:
    """Three rules. Rule 1 is the user's; rules 2 and 3 close its holes.

    1. no single picture covering the slide (>= 95% of both axes);
    2. all pictures together <= 80% of the slide area - four 50%x50% pictures
       each pass rule 1 while tiling the slide into the same flattened image;
    3. a picture-only slide is flattened, whatever the sizes say.
    """

    def _deck_with(self, *bboxes, text: bool = True):
        prs, slide = _blank_deck()
        if text:
            add_textbox(slide, TextElement(type="text", bbox=[0, 0, 100, 50], content="hi"))
        for bbox in bboxes:
            add_picture(slide, ImageElement(type="image", bbox=list(bbox)), _source_bytes())
        return prs, slide

    def test_the_report_shape_is_stable(self):
        report = flattening_report(build_presentation(mock_data.sample_spec()))
        assert set(report) == {
            "flattened",
            "violations",
            "slide_count",
            "picture_count",
            "slides",
        }
        assert set(report["slides"][0]) == {
            "index",
            "picture_count",
            "native_count",
            "picture_area_pct",
            "full_bleed_count",
            "flattened",
            "violations",
        }

    def test_a_deck_with_no_pictures_is_not_flattened(self):
        report = flattening_report(build_presentation(mock_data.sample_spec()))
        assert report["flattened"] is False
        assert report["violations"] == []
        assert report["picture_count"] == 0
        assert report["slide_count"] == 1

    def test_a_legitimate_small_sub_image_passes(self):
        prs, _ = self._deck_with([600, 200, 300, 400])
        report = flattening_report(prs)
        assert report["flattened"] is False, report["violations"]
        assert report["picture_count"] == 1
        assert report["slides"][0]["picture_area_pct"] == pytest.approx(12.0, abs=0.5)

    def test_rule_1_a_single_picture_covering_the_slide(self):
        prs, _ = self._deck_with([0, 0, 1000, 1000])
        report = flattening_report(prs)
        assert report["flattened"] is True
        assert report["slides"][0]["full_bleed_count"] == 1
        assert any("both slide axes" in reason for reason in report["violations"])
        # A full-bleed picture necessarily breaks the area rule as well; that is
        # the two rules agreeing, not a bug.
        assert any(f"{MAX_PICTURE_AREA_PCT:g}% limit" in r for r in report["violations"])

    def test_rule_1_uses_both_axes(self):
        """A full-width but short banner is not a flattened slide by itself."""
        prs, _ = self._deck_with([0, 0, 1000, 200])
        report = flattening_report(prs)
        assert report["slides"][0]["full_bleed_count"] == 0
        assert report["flattened"] is False, report["violations"]

    def test_rule_2_four_half_size_tiles_still_flatten_the_slide(self):
        """The hole in a per-picture 95% rule: each tile passes it, all four tile."""
        prs, _ = self._deck_with(
            [0, 0, 500, 500], [500, 0, 500, 500], [0, 500, 500, 500], [500, 500, 500, 500]
        )
        report = flattening_report(prs)
        assert report["slides"][0]["full_bleed_count"] == 0, "no single tile is full-bleed"
        assert report["flattened"] is True
        assert report["slides"][0]["picture_area_pct"] == pytest.approx(100.0, abs=0.5)
        assert any(f"{MAX_PICTURE_AREA_PCT:g}% limit" in r for r in report["violations"])

    def test_rule_2_allows_exactly_the_limit(self):
        prs, _ = self._deck_with([0, 0, 1000, 800])
        report = flattening_report(prs)
        assert report["slides"][0]["picture_area_pct"] == pytest.approx(80.0, abs=0.2)
        assert report["flattened"] is False, report["violations"]

    def test_rule_3_a_picture_only_slide_is_flattened(self):
        prs, _ = self._deck_with([100, 100, 200, 200], text=False)
        report = flattening_report(prs)
        assert report["flattened"] is True
        assert report["slides"][0]["native_count"] == 0
        assert any("picture-only" in r or "no native shape" in r
                   for r in report["violations"])

    def test_rule_3_is_satisfied_by_any_native_object(self):
        prs, _ = self._deck_with([100, 100, 200, 200])
        assert flattening_report(prs)["flattened"] is False

    def test_a_connector_counts_as_native_content(self):
        prs, slide = _blank_deck()
        add_picture(slide, ImageElement(type="image", bbox=[100, 100, 200, 200]),
                    _source_bytes())
        add_connector(slide, _connector())
        assert flattening_report(prs)["flattened"] is False

    def test_the_violations_name_the_slide(self):
        prs, _ = self._deck_with([100, 100, 200, 200], text=False)
        assert all(r.startswith("slide 1: ") for r in flattening_report(prs)["violations"])


class TestTheBuilderRefusesToFlatten:
    """The report is not advice: the build enforces it, loudly, per element."""

    def _build(self, elements):
        spec = SlideSpec.model_validate({"elements": elements})
        return build_presentation(spec, _source_bytes())

    def test_a_full_slide_image_element_is_skipped(self, capsys):
        prs = self._build(
            [
                {"type": "text", "bbox": [0, 0, 300, 100], "content": "Title"},
                {"type": "image", "bbox": [0, 0, 1000, 1000]},
            ]
        )
        err = capsys.readouterr().err
        assert "element #1 (image) was NOT rendered" in err, err
        assert f"{FULL_BLEED_PICTURE_PCT:g}%" in err
        assert _pictures(prs.slides[0]) == []
        assert flattening_report(prs)["flattened"] is False
        assert len(prs.slides[0].shapes) == 1, "the text box must survive"

    def test_tiling_image_elements_are_trimmed_back_under_the_area_limit(self, capsys):
        prs = self._build(
            [
                {"type": "text", "bbox": [0, 0, 300, 100], "content": "Title"},
                {"type": "image", "bbox": [0, 0, 500, 500]},
                {"type": "image", "bbox": [500, 0, 500, 500]},
                {"type": "image", "bbox": [0, 500, 500, 500]},
                {"type": "image", "bbox": [500, 500, 500, 500]},
            ]
        )
        err = capsys.readouterr().err
        assert "was NOT rendered" in err
        assert f"{MAX_PICTURE_AREA_PCT:g}% limit" in err, err
        report = flattening_report(prs)
        assert report["flattened"] is False, report["violations"]
        assert report["picture_count"] >= 1, "only the excess may be dropped"
        assert report["slides"][0]["picture_area_pct"] <= MAX_PICTURE_AREA_PCT

    def test_a_picture_only_slide_is_refused(self, capsys):
        prs = self._build([{"type": "image", "bbox": [100, 100, 300, 300]}])
        err = capsys.readouterr().err
        assert "was NOT rendered" in err
        assert "picture-only" in err or "nothing but picture" in err, err
        assert _pictures(prs.slides[0]) == []
        assert flattening_report(prs)["flattened"] is False

    def test_a_legitimate_sub_image_is_kept(self, capsys):
        prs = self._build(
            [
                {"type": "text", "bbox": [0, 0, 300, 100], "content": "Title"},
                {"type": "image", "bbox": [600, 200, 300, 400],
                 "description": "team photo"},
            ]
        )
        assert capsys.readouterr().err == "", "a legitimate crop must be silent"
        pictures = _pictures(prs.slides[0])
        assert len(pictures) == 1
        assert flattening_report(prs)["flattened"] is False

    def test_at_exactly_the_area_limit_the_build_and_the_report_agree(self, capsys):
        """The enforcer and the report evaluate ONE shared predicate each.

        If they computed the rule separately they could disagree by a rounding
        step at the boundary - and a deck would be written that its own report
        calls flattened.
        """
        prs = self._build(
            [
                {"type": "text", "bbox": [0, 900, 300, 100], "content": "Title"},
                {"type": "image", "bbox": [0, 0, 1000, 800]},
            ]
        )
        assert capsys.readouterr().err == "", "80% is allowed, so nothing is dropped"
        report = flattening_report(prs)
        assert report["slides"][0]["picture_area_pct"] == pytest.approx(80.0, abs=0.2)
        assert report["flattened"] is False
        assert len(_pictures(prs.slides[0])) == 1

    def test_just_over_the_area_limit_is_trimmed(self, capsys):
        prs = self._build(
            [
                {"type": "text", "bbox": [0, 900, 300, 100], "content": "Title"},
                {"type": "image", "bbox": [0, 0, 1000, 850]},
            ]
        )
        assert "was NOT rendered" in capsys.readouterr().err
        assert _pictures(prs.slides[0]) == []
        assert flattening_report(prs)["flattened"] is False

    def test_the_written_deck_is_never_flattened(self, tmp_path):
        spec = SlideSpec.model_validate(
            {
                "elements": [
                    {"type": "text", "bbox": [0, 0, 300, 100], "content": "Title"},
                    {"type": "image", "bbox": [0, 0, 1000, 1000]},
                ]
            }
        )
        out = build_pptx(spec, tmp_path / "flat.pptx", _source_bytes())
        reopened = Presentation(str(out))
        assert flattening_report(reopened)["flattened"] is False
        with zipfile.ZipFile(out) as zf:
            assert [n for n in zf.namelist() if n.startswith("ppt/media/")] == []


# ---------------------------------------------------------------------------
# source_image threading
# ---------------------------------------------------------------------------
class TestSourceImageParameter:
    """It is optional, positional-or-keyword, and takes bytes or a path."""

    SPEC = {
        "elements": [
            {"type": "text", "bbox": [0, 0, 300, 100], "content": "Title"},
            {"type": "image", "bbox": QUADRANT_BBOX["top_right"][:2] + [300, 300]},
        ]
    }

    def _spec(self):
        return SlideSpec.model_validate(self.SPEC)

    def test_build_presentation_takes_it_positionally(self):
        prs = build_presentation(self._spec(), _source_bytes())
        assert len(_pictures(prs.slides[0])) == 1

    def test_build_presentation_takes_it_as_a_keyword(self):
        prs = build_presentation(self._spec(), source_image=_source_bytes())
        assert len(_pictures(prs.slides[0])) == 1

    def test_build_presentation_still_works_without_it(self, capsys):
        prs = build_presentation(self._spec())
        capsys.readouterr()
        assert _pictures(prs.slides[0]) == []

    def test_build_pptx_takes_bytes(self, tmp_path):
        out = build_pptx(self._spec(), tmp_path / "bytes.pptx", _source_bytes())
        assert len(_pictures(Presentation(str(out)).slides[0])) == 1

    def test_build_pptx_takes_a_path_as_a_keyword(self, tmp_path):
        out = build_pptx(
            self._spec(), tmp_path / "path.pptx", source_image=_source_path(tmp_path)
        )
        assert len(_pictures(Presentation(str(out)).slides[0])) == 1

    def test_build_pptx_still_works_without_it(self, tmp_path, capsys):
        out = build_pptx(self._spec(), tmp_path / "none.pptx")
        capsys.readouterr()
        assert out.exists()


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

    def test_a_broken_connector_is_isolated_like_any_other_element(self, capsys):
        """A connector has NO bbox, so the isolation must not assume one."""
        spec = SlideSpec.model_validate(
            {
                "elements": [
                    {"type": "text", "bbox": [0, 0, 300, 100], "content": "keep me"},
                    {"type": "connector", "start_point": [100, 100],
                     "end_point": [500, 500]},
                    {"type": "shape", "bbox": [0, 300, 200, 200]},
                ]
            }
        )
        _force(spec.elements[1], "start_point", None)
        prs = build_presentation(spec)
        err = capsys.readouterr().err
        assert "skipped element #1 (connector)" in err, err
        assert [_category(s) for s in prs.slides[0].shapes] == ["text", "shape"]

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

    def test_a_spec_without_image_elements_embeds_no_picture_at_all(self, tmp_path):
        """The whole point of the tool: native objects, not a flattened image.

        A picture is only ever legitimate for an ``image`` element, and this spec
        has none - so for THIS deck the old, absolute "zero pictures" assertion
        is still exactly right, and it is kept in full. The general rule now
        lives in :func:`flattening_report` (see :class:`TestFlatteningReport`),
        because a genuine photo/chart region is a legitimate crop.
        """
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

    def test_the_reopened_deck_is_not_flattened(self, tmp_path):
        """The replacement for "zero pictures": the shared flattening rule."""
        out = build_pptx(mock_data.sample_spec(), tmp_path / "deck.pptx")
        report = flattening_report(Presentation(str(out)))
        assert report["flattened"] is False, report["violations"]
        assert report["picture_count"] == 0
        assert report["slides"][0]["native_count"] > 0

    def test_a_deck_of_every_element_type_keeps_its_z_order(self, tmp_path):
        """One of each: shape, connector, text, image, table - in spec order."""
        spec = SlideSpec.model_validate(
            {
                "elements": [
                    {"type": "shape", "kind": "arrow_right", "bbox": [0, 0, 200, 100]},
                    {"type": "connector", "start_point": [200, 50],
                     "end_point": [500, 50], "arrow_end": True},
                    {"type": "text", "bbox": [0, 200, 400, 100], "content": "Flow"},
                    {"type": "image", "bbox": [600, 100, 300, 300],
                     "description": "team photo"},
                    {"type": "table", "bbox": [0, 500, 500, 300],
                     "data": [["a", "b"], ["c", "d"]]},
                ]
            }
        )
        out = build_pptx(spec, tmp_path / "everything.pptx", _source_bytes())
        slide = Presentation(str(out)).slides[0]
        assert [_category(s) for s in slide.shapes] == [
            "shape",
            "connector",
            "text",
            "image",
            "table",
        ]
        assert flattening_report(Presentation(str(out)))["flattened"] is False

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
