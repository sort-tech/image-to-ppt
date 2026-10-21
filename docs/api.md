# API reference

[← README](../README.md)

Base path `/api`. All JSON is **snake_case**. CORS allows
`http://localhost:5173` and `http://localhost:3000` (all methods, all headers).

Every error uses the **same envelope**, so one client-side handler covers all
of them - and every status carries the CORS headers, **including 500**: the
envelope is produced by `ErrorEnvelopeMiddleware` *inside* the CORS layer,
because starlette's own `Exception` handler runs outside it and its response
would reach the browser without `Access-Control-Allow-Origin` (i.e. as an
unreadable network error, exactly when the hint matters most):

```json
{ "detail": { "code": "unsupported_extension", "message": "...", "hint": "..." } }
```

### `GET /api/health` → 200

Configuration only. **This endpoint never fails because the proxy is down** -
it reports what is configured, not what is reachable.

```json
{
  "status": "ok",
  "proxy": {
    "base_url": "http://localhost:4000/",
    "model": "gemini-flash",
    "api_key_configured": true
  },
  "supported_extensions": [".jpeg", ".jpg", ".png", ".webp"],
  "slide": { "width_in": 13.333, "height_in": 7.5, "coord_scale": 1000 }
}
```

`api_key_configured` is a **bool**. It never echoes the key or any prefix of it.

```bash
curl -s http://127.0.0.1:8000/api/health
```

### `POST /api/convert` (`multipart/form-data`) → 200

| field | type | required | meaning |
| --- | --- | --- | --- |
| `file` | file | **yes** | the image (`.png`, `.jpg`, `.jpeg`, `.webp`), max 20 MB |
| `model` | string | no | overrides `VISION_MODEL` for this request |
| `extra_instructions` | string | no | appended to the user prompt sent to the model |

There is no `is_mock` field. An extra form field is ignored, so a stale client
that still posts `is_mock=true` gets an ordinary conversion - and still a
`503 missing_api_key` without a key.

```json
{
  "status": "success",
  "counts": { "shape": 4, "table": 1, "text": 9 },
  "total": 14,
  "dropped": ["element 1 (table): data - Field required"],
  "warnings": ["bbox (505, 601, 461, 6): height is below the 0.05in minimum; enlarged it so the element stays visible. ..."],
  "spec": { "background_color": "#FFFFFF", "elements": [] },
  "preview": { "slide": {}, "background_color": "#FFFFFF", "shapes": [] },
  "download_id": "0f3c9a1b2d4e4f6a8b0c1d2e3f405162",
  "filename": "sample.pptx"
}
```

(Illustrative: both `dropped` and `warnings` are shown non-empty so you can see
their shape. Converting `backend/sample.png` for real gives
`counts: {"shape": 4, "table": 1, "text": 9}`, `total: 14`, `dropped: []` and
one warning about the 6-unit-high band.)

`dropped` and `warnings` are **always arrays** - empty when nothing happened.
`spec` is the validated `SlideSpec` as JSON and **never contains a `dropped`
key** (diagnostics are output-only, so they cannot be forged; see
[the spec format](spec-format.md#the-json-spec-format)). `preview` is the render-ready
description of the built deck, or `null` with a line in `warnings` if deriving
it failed - see [`POST /api/preview`](#post-apipreview-applicationjson--200) for
its shape. Fetch the deck itself with the `download_id`.

```bash
curl -s -F 'file=@backend/sample.png' http://127.0.0.1:8000/api/convert
```

Errors:

| status | `code` | when |
| --- | --- | --- |
| 400 | `unsupported_extension` | extension not in the supported list (the message names the list). A name with no extension at all - including a dotfile name like `.png`, which POSIX reads as a name and not a suffix - is reported as `'<no extension>'`, never quoted back as if it were one |
| 400 | `empty_file` | zero bytes |
| 400 | `not_an_image` | the bytes are not a decodable image |
| 413 | `file_too_large` | over the 20 MB cap |
| 503 | `missing_api_key` | `LITELLM_API_KEY` unset/blank. The hint names the variable, the export, and the fact that `/api/preview` and `/api/build-from-json` need no key |
| 502 | `proxy_unreachable` | the proxy could not be reached (the hint names `LITELLM_BASE_URL`) |
| 502 | `analysis_failed` | the model replied but the reply could not be used |
| 500 | `build_failed` | the deck could not be built - and the code for any other unexpected failure of the pipeline, so this table is the whole error surface |

### `POST /api/refine-instructions` (`multipart/form-data`) → 200

Completes a rough note into ONE instruction written for the slide analyzer, in
the note's own language, ready to paste into `extra_instructions` on the next
`/api/convert`.

The flow it exists for: convert, look at the preview, spot something wrong, type
"표가 이상해", and get back an instruction that actually names the element. That
is why **context matters** - with only the bare note there is nothing to do but
tidy the grammar.

| field | type | required | meaning |
| --- | --- | --- | --- |
| `note` | string | **yes** | the rough text, in any language |
| `spec` | string | no | the current `SlideSpec` as JSON, when a conversion has already happened. Parsed leniently: an unusable value is **ignored, not rejected** - it is only context |
| `file` | file | no | the source image, used when there is no spec yet |
| `model` | string | no | overrides `VISION_MODEL` for this request |

Context is picked cheapest-first and **reported rather than assumed**: `spec`
when it parses and has elements (text-only, no vision call), otherwise `file`
(one vision call), otherwise neither. `context_used` says which it really was,
so the UI can name the context instead of implying the best case.

The spec is **summarized, never dumped** - a 14-element spec as raw JSON is
thousands of tokens of bbox integers the model cannot use to work out which
element the note means. What goes up is a capped digest of
`type - rough position in words - rough extent in words - short excerpt`, with
no coordinates and no sizes in figures at all (for `backend/sample.png`: under a
thousand characters against 2,614 of raw JSON).

The extent is words for the same reason the position is: a digest that carried
`460x5` got the numbers quoted straight back at the analyzer, which has its own
idea of the 0..1000 grid. But it has to be there - without it a 460x5 divider
and a full-slide card produced identical lines, so `구분선이 너무 두꺼워` had
nothing to attach to and mis-targeted the header band in 5 of 8 sampled runs.
`얇은 가로선`, `가로로 넓고 낮은 띠`, `큰 영역`, `작은 영역` and the rest carry
that much and cost a handful of characters.

An unusable `spec` is ignored rather than rejected, and that holds for one that
*parses* and then cannot be encoded: a lone surrogate (`"\ud800"`) survives
`json.loads` but no UTF-8 encoder, and it used to fail inside the HTTP client
rather than here. Strings that reach the digest are sanitized where they are
built, so the assist still works for that caller.

```json
{
  "status": "success",
  "instruction": "우측 중앙 영역은 3열 3행의 단일 표로 인식하세요. 첫 행(Region | Users | Growth)을 헤더로 지정하고, 셀 내부의 텍스트를 개별 텍스트 상자로 분리하지 마세요.",
  "note": "표가 이상해",
  "context_used": "spec",
  "fell_back": false
}
```

`instruction` is **never empty**, by construction: the UI writes it straight
into the box the user typed in, so an empty value would delete their own words.
If the model returns nothing usable the tidied note comes back instead,
`context_used` is downgraded to `"none"` - claiming the slide informed an answer
that is only the user's own sentence would be a lie - and `fell_back` is `true`.

`fell_back` is there because `context_used` alone cannot tell two different
events apart. Both answer `"none"`: a genuine no-context run, where the model
really does write an instruction from the note alone, and this fallback, where a
digest *was* built and sent and nothing came back. They need different sentences
on screen, and the UI prints one of them - so "there was no analysis or image"
must not be said about a request that carried one.

```bash
curl -s -F 'note=표가 이상해' -F 'spec=<spec.json' \
     http://127.0.0.1:8000/api/refine-instructions
```

Errors:

| status | `code` | when |
| --- | --- | --- |
| 400 | `empty_note` | `note` missing or blank after trimming. Checked before the optional `file`, so a caller who forgot the note gets the code that names what they forgot |
| 400 | `empty_file` / `not_an_image` | a `file` part with a filename but no bytes, or bytes Pillow cannot decode |
| 413 | `file_too_large` | over the same 20 MB cap as `/api/convert` |
| 503 | `missing_api_key` | `LITELLM_API_KEY` unset/blank |
| 502 | `proxy_unreachable` | the proxy could not be reached |
| 502 | `refine_failed` | the model was reached but gave nothing usable |

There is no `build_failed` here: nothing is built. A malformed `spec` is not an
error at all - it degrades to the next context down and says so.

### `POST /api/preview` (`application/json`) → 200

What a spec really builds, without storing anything: the deck is built so the
geometry reported is python-pptx's own (floors and clipping included), then the
bytes are thrown away. No model call, no key, no network. This is what the UI
calls to refresh the slide after you edit a spec.

The body is read exactly as `/api/build-from-json` reads it - a bare
`SlideSpec`, a wrapper `{"spec": {...}}` or a bare element array all work, via
the same `normalize_spec_payload`.

The response is the `POST /api/convert` response **without** `download_id` and
`filename`, because nothing is kept:

```json
{
  "status": "success",
  "counts": { "shape": 4, "table": 1, "text": 9 },
  "total": 14,
  "dropped": [],
  "warnings": [],
  "spec": { "background_color": "#FFFFFF", "elements": [] },
  "preview": {
    "slide": { "width_in": 13.333, "height_in": 7.5, "width_pt": 959.98, "height_pt": 540.0 },
    "background_color": "#FFFFFF",
    "shapes": [
      { "kind": "shape", "autoshape": "rectangle",
        "left_pct": 0.0, "top_pct": 0.0, "width_pct": 100.0, "height_pct": 15.6,
        "fill_color": "#1D356A", "line_color": null, "line_width_pt": null,
        "line_dash": "solid" },
      { "kind": "connector",
        "start_pct": { "x": 12.5, "y": 30.0 },
        "end_pct": { "x": 48.0, "y": 30.0 },
        "elbow": false, "arrow_start": false, "arrow_end": true,
        "dash": "solid", "color": "#1E3A6E", "width_pt": 1.5 },
      { "kind": "image",
        "left_pct": 60.0, "top_pct": 20.0, "width_pct": 30.0, "height_pct": 40.0,
        "data_uri": "data:image/png;base64,...", "description": "team photo" },
      { "kind": "text",
        "left_pct": 2.8, "top_pct": 5.0, "width_pct": 38.0, "height_pct": 5.0,
        "align": "left",
        "paragraphs": [
          { "text": "Quarterly Platform Review", "font_size_pt": 32.0,
            "font_color": "#FFFFFF", "bold": false, "align": "left" }
        ] },
      { "kind": "table",
        "left_pct": 50.5, "top_pct": 20.4, "width_pct": 46.0, "height_pct": 32.5,
        "col_widths_pct": [33.3333, 33.3333, 33.3333],
        "row_heights_pct": [33.3333, 33.3333, 33.3333],
        "rows": [
          [ { "text": "Region", "fill_color": "#1D356A", "font_size_pt": 10.0,
              "font_color": "#FFFFFF", "bold": true, "align": "center" } ]
        ] }
    ]
  }
}
```

Rules the payload keeps:

- `shapes` is already in **z-order**, back to front - it is `slide.shapes` in
  document order, and sorting it would be the bug.
- every numeric field is a **JSON number**, never a string; `left_pct`/`width_pct`
  are percentages of the slide's **width**, `top_pct`/`height_pct` of its
  **height**, and they are **not clamped**.
- `col_widths_pct` / `row_heights_pct` are percentages of the **table's own**
  box, each summing to ~100, so the UI's grid fills its frame exactly.
- a `null` colour means **no fill / no outline / inherited** - see-through, never
  a guess and never black. Only an explicit RGB is reported.
- `font_size_pt` falls back to the builder's own default
  (`DEFAULT_TEXT_FONT_SIZE` 12pt / `DEFAULT_TABLE_FONT_SIZE` 10pt) when a run
  carries no explicit size, so the UI never has to guess.
- a **connector** carries `start_pct`/`end_pct` and **no** `left_pct`/`width_pct`
  box, because it has no `bbox` in the spec either; a box would also lose which
  end the arrowhead is on. `dash`, `arrow_start`, `arrow_end`, `color` and
  `width_pt` are read back off the deck's `a:ln`, and `elbow` says the deck used
  `bentConnector3`.
- an **`image`**'s `data_uri` is the crop **that is really embedded in the
  deck** (`shape.image.blob`), never the source image: downscaled to a longest
  side of 480px and capped at 96KB, and `null` (with a `warnings` line) when it
  cannot be read or will not fit. A `null` is drawn as a labelled placeholder,
  not as an empty box.
- a shape kind the payload has no reader for comes back as
  `{"kind": "unsupported"}` rather than crashing. Pictures and connectors are
  **not** in that bucket any more - they have their own entries above.

```bash
curl -s -H 'Content-Type: application/json' \
     --data '{"elements":[{"type":"text","bbox":[100,100,500,100],"content":"hello"}]}' \
     http://127.0.0.1:8000/api/preview
```

Errors: **400 `invalid_spec`** and **500 `build_failed`**, the same envelope and
the same two codes as `/api/build-from-json`.

### `POST /api/build-from-json` (`application/json`) → 200, binary

Rebuild a deck from a spec you already have - no model call, no key, no
network. Free, and the way to iterate on geometry by hand. Unchanged by the
preview work: it still answers with the binary.

The body may be a bare `SlideSpec`, a wrapper `{"spec": {...}}`, or a bare
element array; `normalize_spec_payload` recovers all three. Optional
**query** parameter `filename` (default `slide.pptx`).

Response headers:

| header | value |
| --- | --- |
| `Content-Type` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` |
| `Content-Disposition` | `attachment; filename="<ascii-name>.pptx"`, plus `; filename*=UTF-8''<percent-encoded>` when the name is not ASCII |
| `X-Element-Counts` | the counts object as a compact JSON string, e.g. `{"shape":0,"table":0,"text":1}` |
| `X-Dropped-Count` | the number of dropped elements, as a string |

`X-Element-Counts` exists so the UI can report what it got without a second
round trip. Both custom headers - and `Content-Disposition` - are listed in the
CORS `expose_headers`, so browser JS can read them cross-origin as well as
through the proxy.

`filename=` is latin-1 only, and the sanitizer that makes a name safe to
interpolate there *removes* non-ASCII rather than escaping it - so on its own
it turned `분기보고서.png` into `slide.pptx` and `매출표_Q3.png` into `Q3.pptx`.
RFC 5987's `filename*` carries the user's own characters (UTF-8,
percent-encoded) and is what browsers prefer, so the download keeps its name;
a client that only understands `filename=` still gets the valid ASCII one. It
is emitted **only** when it would say something `filename=` cannot, so an
ASCII upload's header is exactly one parameter, as before.

The JSON `filename` field of `POST /api/convert` is the same **display** name
(the one the 변환 완료 panel shows and the browser falls back to), not the
latin-1 one: it travels as UTF-8 JSON and ends up in an `<a download>`, so it
has no latin-1 constraint, and having it disagree with the file the user
actually saves would be its own bug. Path parts, quotes, control characters
and the punctuation Windows forbids are still removed from it;
`pptx_headers` is the only place the ASCII-only form is produced, and it
derives both parameters itself rather than trusting a caller.

```bash
curl -s -D /tmp/h.txt -o /tmp/rebuilt.pptx \
     -H 'Content-Type: application/json' \
     --data '{"elements":[{"type":"text","bbox":[100,100,500,100],"content":"hello"}]}' \
     'http://127.0.0.1:8000/api/build-from-json?filename=rebuilt.pptx'
grep -i 'x-element-counts\|x-dropped-count' /tmp/h.txt
```

Errors: **400 `invalid_spec`** (when elements were dropped, the hint lists the
per-element reasons) and **500 `build_failed`** - those two and nothing else.

The body is capped at **2 MB** (`converter.MAX_JSON_BODY_BYTES`), counted as it
arrives and refused as `invalid_spec`: a spec is kilobytes, while decoding a
body costs several times its size in memory. A body that is malformed in any
way - unparseable, empty, or nested too deeply for the parser (which raises
`RecursionError`, not `ValueError`) - is that same 400, never a 500.

### `GET /api/download/{file_id}` → 200, binary

The deck produced by a `POST /api/convert`, with the same four headers as
above.

```bash
curl -s -o /tmp/out.pptx http://127.0.0.1:8000/api/download/<download_id>
```

Downloads live in an **in-process store** with a **30-minute TTL** and a cap of
**64 entries** (oldest evicted first). A download does **not** consume the
entry, so re-downloading the same id works for as long as it lives. Restarting
the backend - including an automatic `--reload` restart - empties the store.

**404 `unknown_download`** when the id is unknown or expired; the hint tells you
to re-run the conversion. `file_id` is validated as a uuid4 hex string *before*
any lookup and is never interpolated into a filesystem path - a malformed id
such as `..` is refused by the pattern, so it never reaches the store.

### Also served, outside the pinned contract

| route | what it is |
| --- | --- |
| `GET /docs`, `GET /openapi.json` | FastAPI's interactive docs and schema |
| `GET /` | a small JSON pointer to `/docs`, `/api/health` and the endpoint list, so the bare root is not a 404 |

And a few extra statuses you can hit from a client bug rather than a bad image:
**422 `invalid_request`** when the request itself cannot be read - `POST
/api/convert` with no `file` part, say, where the `hint` is
`file: Field required`. It uses the same envelope as everything else, because
FastAPI's own `{"detail": [ ... ]}` list would otherwise be the one error shape
a client had to special-case.

The same envelope also wraps the failures starlette raises before any route of
ours runs (`main._CODE_BY_STATUS`): **404 `not_found`** for a wrong URL - a
`VITE_API_BASE` missing its `/api`, typically - **405
`method_not_allowed`** for a wrong verb, and **400 `bad_request`** for a body
the multipart parser cannot read. Their `message` is starlette's own English
string and their `hint` is empty, so the UI supplies all the copy for them;
`tests/test_frontend_guards.py` asserts every code in that map has Korean copy
in `frontend/src/api/client.ts`.
