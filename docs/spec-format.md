# The 0..1000 coordinate system

[← README](../README.md)

Every `bbox` is `[x, y, w, h]` in **integers from 0 to 1000**, where `x`/`w` are
relative to the image *width* and `y`/`h` to the image *height*.
`[0, 0, 1000, 1000]` is the whole slide; `[500, 0, 500, 1000]` is the right
half. All four components are required.

`pptx_builder.scale_bbox` maps that onto the 16:9 canvas:

```python
left   = Inches(bbox.x / 1000 * 13.333)
top    = Inches(bbox.y / 1000 * 7.5)
width  = Inches(bbox.w / 1000 * 13.333)
height = Inches(bbox.h / 1000 * 7.5)
```

**Why integers instead of 0.0-1.0 floats?**

- **Resolution independence.** The model never sees pixel dimensions, so the
  same spec renders identically whether the input was 800x450 or 4K.
- **LLMs are bad at floats.** Models emit `0.3333333333333333`, `.33`, `33%`,
  `"0,33"` and occasionally `1.0000001`. Integers on a familiar 0-1000 ruler
  are a token-cheap, low-ambiguity target, and per-mille granularity is ~13
  thousandths of an inch on a 13.3in slide - far finer than anyone can see.
- **Cheap, safe validation.** Clamping to `0..1000` and flooring `w`/`h` at 1
  is trivial and total: no NaN, no infinity, no negative-size shapes, no
  silently off-slide geometry.
- **Hand-editable.** A spec full of round numbers is pleasant to fix by hand;
  a dump full of 17-digit floats is not.

Element **order is z-order**: earlier elements are painted first, later ones on
top. The prompt tells the model to emit background and container shapes early
and tables/text late, and the builder preserves that order exactly.

Slides are always **13.333 x 7.5 in** with `sldSz type="screen16x9"`.

### Why the table font is small by default

Tables default to **10pt** (`DEFAULT_TABLE_FONT_SIZE`), smaller than the 12pt
text default. This is not a style preference, it is a python-pptx constraint:
PowerPoint auto-grows a table row to fit its content and **python-pptx cannot
shrink a row below the height its text needs**. Ask for 18pt in a table you
placed 2 inches tall and PowerPoint will push it off the bottom of the slide,
wrecking the reproduction. So cells are rendered compactly. If your source
table is genuinely large-print, set `font_size` on the table element and rebuild
via `POST /api/build-from-json` or `--from-json`.

---

## The JSON spec format

```jsonc
{
  "background_color": "#FFFFFF",        // optional, #RRGGBB
  "elements": [                          // order == z-order (back to front)
    { "type": "shape", "kind": "rectangle",
      "bbox": [0, 0, 1000, 150],
      "fill_color": "#1F3B73",           // null/omitted -> no fill at all
      "border_color": null,              // null/omitted -> no outline
      "border_width": 1.5 },             // points

    { "type": "shape", "kind": "rounded_rectangle",
      "bbox": [35, 200, 420, 330], "fill_color": "#F2F5FA",
      "border_color": "#1F3B73", "border_width": 1.5 },

    { "type": "shape", "kind": "circle",
      "bbox": [890, 30, 80, 90], "fill_color": "#FFB000" },

    { "type": "text",
      "bbox": [35, 40, 640, 70],
      "content": "Quarterly Platform Review",
      "font_size": 32,                   // points; defaults to 12
      "font_color": "#FFFFFF",
      "bold": true,
      "align": "left" },                 // left | center | right

    { "type": "text",
      "bbox": [65, 230, 365, 270],
      "content": "Highlights\n- p95 latency down 38%\n- Two new regions live",
      "font_size": 14, "align": "left" },   // "\n" -> separate paragraphs

    { "type": "table",
      "bbox": [525, 220, 420, 260],
      "data": [                          // row-major, first row = header
        ["Region", "Users", "Growth"],
        ["EMEA", "18,400", "+12%"],
        ["APAC", "9,750", "+27%"]
      ],
      "header_bg_color": "#1F3B73",
      "header_font_bold": true,
      "font_size": 10 },

    { "type": "connector",              // NO bbox: two points instead
      "kind": "elbow",                  // straight (default) | elbow
      "start_point": { "x": 120, "y": 360 },
      "end_point": [420, 520],          // [x, y] is accepted too
      "arrow_start": false,             // default false
      "arrow_end": true,                // default true
      "dash_style": "dashed",           // solid (default) | dashed
      "color": "#1F3B73",
      "width_pt": 1.5 },

    { "type": "image",                  // a region CROPPED from the input image
      "bbox": [600, 200, 300, 400],
      "description": "team photo" }     // -> the picture's alt text
  ]
}
```

Element types and fields:

- **`shape`** - `kind` is `rectangle` (default), `rounded_rectangle`, `circle`
  or one of the block arrows `arrow_right` / `arrow_left` / `arrow_up` /
  `arrow_down`; `fill_color`, `border_color`, `border_width`. A shape with no
  `fill_color` is drawn *unfilled*, so it can frame content painted later.
  Shapes never carry text.
- **`table`** - `data` is a 2-D array of strings; ragged rows are padded with
  `""`; `header_bg_color`, `header_font_bold`, `font_size`.
- **`text`** - `content` (**required**, with `\n` for line breaks),
  `font_size`, `font_color`, `bold`, `align`.
- **`connector`** - a diagram connector, the one element type with **no
  `bbox`**: it is defined by `start_point` and `end_point` (each a
  `{"x":…,"y":…}` dict or an `[x, y]` pair, on the same 0..1000 grid, clamped
  the same way). `kind` is `straight` (default) or `elbow`; `arrow_start`
  (default `false`) and `arrow_end` (default `true`) say which end carries the
  arrowhead; `dash_style` is `solid` (default) or `dashed`; `color` and
  `width_pt` follow the usual colour/size rules. A missing or unreadable
  `x`/`y` invalidates the element exactly as a broken `bbox` does -
  `connector 3: start_point - 'y' is missing or unreadable`.
- **`image`** - a region cropped out of the **source image** and embedded as a
  picture: `bbox` plus an optional `description` (written into the picture's alt
  text, and read back from there by the preview). This is the flattening
  exception fenced by the [three anti-flattening rules](#anti-flattening-rules), and it is meant
  for a photo, a rendered chart, a logo or a complex illustration - not for
  ordinary boxes, text or tables, and never for one big region covering the
  slide. Only a conversion has the source image: `POST /api/preview`,
  `POST /api/build-from-json` and `cli.py --from-json` have nothing to crop
  from, so they skip the element, warn that the conversion has to be re-run, and
  draw a dashed grey placeholder outline at the bbox so the gap is visible in
  the deck. An EXIF-rotated source (an ordinary portrait phone photo) is
  normalized to its **displayed** orientation before the crop, which is the
  frame the bbox is measured in.

Robustness rules baked into the schema: colours accept `#FFF`, `fff`,
`FFFFFF`, `#ffffff` and `#RRGGBBAA` (alpha dropped) and fall back to `None` if
unparseable; `bbox` accepts a list, a tuple or a dict (including `x1`/`y1`
corner keys) and coerces strings and floats before clamping; unknown extra keys
are ignored.

Tolerance stops at *absent*, though: all four of `x`/`y`/`w`/`h` must be
**present and parseable**. A missing or unreadable component makes that element
invalid - `element 0 (text): bbox - 'h' is missing or unreadable` - because
defaulting it would render an invisible 0.05in sliver and report success over a
deck the user cannot read.

The same rule covers a `text` element's `content`: absent gives
`element 0 (text): content - Field required`, `null` gives
`element 0 (text): content - must not be null ...`. `"content": ""` stays valid
on purpose - an empty text box you can click and type into is a real, editable
object.

Text that PowerPoint cannot store at all - a lone UTF-16 surrogate, the kind
`json.loads('"\ud800"')` produces - is replaced with `U+FFFD` so the rest of the
string still renders, and the substitution is reported truthfully (on `stderr`
for the CLI, in `warnings` for the API). The element is rendered, not skipped.

Elements are validated **one at a time**: an element that cannot be repaired (a
`table` with no `data`, an unknown `type`, an incomplete `bbox`) is dropped with
a reason and the rest of the slide is still built, so the run still succeeds.
That holds for a bare `[element, ...]` array too: one unrecognized entry costs
only that entry. The reasons surface as `dropped` in the API response and on
`stderr` in the CLI; on the model they live on `SlideSpec.dropped`, which is
**output-only** - a `dropped` key in the *input* is stripped, so neither a spec
file nor the model can fabricate a diagnostic, and it is excluded from
serialization so a saved spec stays clean.

Losing *every* element is a hard failure instead (`400 invalid_spec` /
`ElementsAllDroppedError`): a silently blank deck is worse than an error.

A box that has to be moved, trimmed or enlarged to render also **warns**, so
the geometry you see is never quietly different from the geometry the spec
asked for.

Live copies of this example live in the test fixture
`backend/app/mock_data.py`: `SAMPLE_SPEC_JSON` (string) and `sample_spec()`
(validated object). That module is a **fixture the suite shares**, not a
product feature - nothing a user can reach imports it.

## Anti-flattening rules

The one exception is an **`image` element**: a region the analyzer genuinely
cannot express as shape, table or text - a photo, a rendered chart, a logo -
is cropped out of the source image and embedded as a picture. That exception is
fenced, because pasting the input onto a slide would make the whole exercise
pointless. `pptx_builder.flattening_report()` is the single definition of
"flattened" that the build, the API and the suite all read, and it fails a deck
when any of three rules breaks:

1. **no single picture covers the slide** - not `>= 95%` of the slide width
   *and* `>= 95%` of its height;
2. **all pictures together cover at most 80% of the slide** (the union of their
   boxes, so overlap counts once) - four 50%x50% pictures pass rule 1 while
   tiling exactly the screenshot rule 1 forbids;
3. **a picture-only slide is a flattened slide** - a slide carrying a picture
   must also carry at least one native shape, connector, table or text box.

When the analyzer's own output would break a rule, the offending `image`
element is skipped with a warning naming it; a flattened deck is never written.
