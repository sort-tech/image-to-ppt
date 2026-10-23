# Preview before download

[← README](../README.md)

Every conversion comes back with a **`preview`**: a render-ready description of
the slide, which the UI draws as an SVG so you see the deck *before* you decide
to download it. `POST /api/preview` re-derives it from an edited spec, so the
loop is `convert` → look → edit the spec → `preview` → download.

The preview is read out of **the `Presentation` object that was actually
built** - `services/preview.py` walks `slide.shapes` and reports each shape's
real geometry and styling. It never looks at the `SlideSpec`. That is the whole
point: re-deriving it from the spec would be a second implementation of
`pptx_builder.scale_bbox`, and the moment the two disagreed you would be looking
at a picture of a slide that does not exist. Reading the built deck means the
preview shows, for free:

| shown by construction | because |
| --- | --- |
| the min-dimension floor | a 5-unit-high band is previewed at the 0.05in it was enlarged to, not at the 0.0375in the spec asked for |
| off-slide clipping | a bbox that overshot 1000 is previewed where it really landed |
| cleaned text | control characters dropped, CRLF split into paragraphs, lone surrogates replaced - the runs are read back out of the deck |
| z-order | `slide.shapes` is in document order, which *is* the z-order, back to front |

Geometry is emitted as **percentages of the slide** (so the UI is trivially
responsive) and font sizes in **points** (the slide is 960 x 540pt, so an SVG
with `viewBox="0 0 960 540"` maps one point to one user unit). Percentages are
deliberately **not** clamped - a shape may legitimately sit partly off-slide,
and clipping it is the renderer's job.

Deriving the preview can never fail a conversion: any error leaves
`"preview": null` and adds a line to `warnings`, and the deck is still built and
still downloadable.

```bash
# see what a spec really builds, without storing anything
curl -s -H 'Content-Type: application/json' \
     --data '{"elements":[{"type":"shape","shape":"rectangle","bbox":{"x":100,"y":100,"w":500,"h":5}}]}' \
     http://127.0.0.1:8000/api/preview
# -> height_pct 0.6667 (= 0.05in, the FLOOR) with the matching warning,
#    not the 0.5 the spec's h=5 would suggest
```

> **Mock mode has been removed** from the whole product - the UI toggle, the
> `is_mock` API field and the CLI's `--mock` flag are all gone.
> `backend/app/mock_data.py` stays as the test suite's shared fixture
> (`sample_spec()`); it is not reachable from any user-facing code path.
> `POST /api/preview` and `POST /api/build-from-json` are what now cover
> "prove the builder and the UI work without a live model".
