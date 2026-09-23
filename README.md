# image-to-pptx

Turn a picture of a slide into a **real PowerPoint slide** - in the browser, or
from the command line.

Feed it a screenshot, a scanned page, a whiteboard photo or a diagram, and it
writes a `.pptx` whose shapes, tables and text boxes are **native, editable
PowerPoint objects**: you can click the table and retype a cell, drag a box,
restyle the header. The input image is never embedded. If the output contained
a flattened picture the whole exercise would be pointless, so the test suite
asserts that every saved deck has **zero picture shapes and no `ppt/media`
parts**.

```
  browser (React) ──┐
                    ├──▶ FastAPI ──▶ Vision LLM ──▶ SlideSpec (JSON) ──▶ python-pptx ──▶ .pptx
  backend/cli.py ───┘                (gemini-flash       validated                       native shapes,
                                      via LiteLLM)       by pydantic                     tables, text
```

The web app and the CLI are two front doors onto **the same conversion core**
(`backend/app/`). Nothing about the validation, the coordinate system or the
builder changed when the UI was added.

The browser UI is **in Korean** and shows a [preview of the
slide](#preview-before-download) before you download it. The API and the CLI
stay English: codes like `invalid_spec` are a contract, and the UI translates
them on the way to the screen (`frontend/src/api/client.ts`).

---

## Contents

- [Layout](#layout)
- [Quick start](#quick-start)
- [Running it by hand](#running-it-by-hand)
- [Environment variables](#environment-variables)
- [Prerequisite: a LiteLLM proxy](#prerequisite-a-litellm-proxy)
- [Preview before download](#preview-before-download)
- [API reference](#api-reference)
- [The 0..1000 coordinate system](#the-01000-coordinate-system)
- [The JSON spec format](#the-json-spec-format)
- [The CLI](#the-cli)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)

---

## Layout

```
image-to-ppt/
├── run.sh                      one command to bring the whole stack up
├── README.md                   this file
├── .env.example                placeholder env (the real key stays in your shell)
├── .gitignore
├── .venv/                      python virtualenv (created by run.sh)
│
├── backend/
│   ├── app/
│   │   ├── config.py           endpoint, model, slide geometry, styling defaults
│   │   ├── schemas.py          the pydantic contract + tolerant validation
│   │   ├── vision_analyzer.py  prompt, request, JSON recovery
│   │   ├── pptx_builder.py     SlideSpec ➜ .pptx (native shapes only)
│   │   ├── mock_data.py        a hand-built sample spec - a TEST FIXTURE only,
│   │   │                       not a product feature (Mock mode is gone)
│   │   ├── main.py             the FastAPI app: CORS, router, one error envelope
│   │   ├── api/routes.py       the /api routes and nothing else of substance
│   │   └── services/
│   │       ├── converter.py    framework-free orchestration, shared with the CLI
│   │       └── preview.py      walks the BUILT deck -> the render-ready preview
│   ├── cli.py                  the original CLI, preserved
│   ├── make_sample_image.py    draws a synthetic 1600x900 test slide with Pillow
│   ├── sample.png              a ready-made input image
│   ├── requirements.txt
│   ├── pytest.ini
│   └── tests/                  test_schemas / test_pptx_builder / test_vision_analyzer
│                               / test_preview / test_cli / test_config / test_api
│                               / test_env_file / test_frontend_guards - fully offline
│
└── frontend/
    ├── index.html
    ├── package.json
    ├── vite.config.ts          dev server on 5173, /api proxied to 8000
    ├── tailwind.config.js
    ├── postcss.config.js
    ├── tsconfig.json
    └── src/
        ├── main.tsx  App.tsx
        ├── api/client.ts       one typed wrapper per endpoint
        ├── components/         FileUploader, OptionPanel, SlidePreview,
        │                       ResultView, SpecEditor
        └── types/slide.ts      the SlideSpec + preview types, mirrored from
                                schemas.py and services/preview.py
```

The five core modules - `config.py`, `schemas.py`, `vision_analyzer.py`,
`pptx_builder.py`, `mock_data.py` - were **moved, not rewritten**, when the
project grew a web tier. They import each other relatively (`from . import
config`), so the same code backs the CLI, the HTTP API and the tests with no
`sys.path` juggling.

---

## Quick start

Needs **Python 3.10+** and **Node 18+** on `PATH`. Everything else is installed
for you.

```bash
cd /Users/yan/works/image-to-ppt
export LITELLM_API_KEY='<your proxy key>'   # or put it in .env (git-ignored)
./run.sh
```

That single command:

1. creates (or reuses) the virtualenv at `.venv/` and installs
   `backend/requirements.txt` - skipping the install, and saying so, when
   nothing changed;
2. runs `npm install` in `frontend/` only when `node_modules` is missing or
   `package.json` is newer;
3. starts **uvicorn** (`app.main:app`) on **:8000** with `--reload` and the
   **Vite** dev server on **:5173**, both in the background, each log line
   tagged `[api]` or `[web]`;
4. waits until both answer, then prints the URLs;
5. on **Ctrl-C**, kills both children *and their descendants* - uvicorn's
   reload worker and Vite's `node` child included - then confirms the ports are
   free. No orphan is ever left holding :8000 or :5173.

Open **<http://localhost:5173/>**. The browser only ever talks to :5173; Vite
proxies `/api` through to the backend, so requests are same-origin.

### The other run.sh modes

```bash
./run.sh              # both services (same as ./run.sh all)
./run.sh backend      # just uvicorn on :8000
./run.sh frontend     # just Vite on :5173
./run.sh test         # pytest (backend) + tsc --noEmit (frontend)
./run.sh help         # usage
```

It resolves its own directory, so `/Users/yan/works/image-to-ppt/run.sh` works
from anywhere. It refuses to start - naming the port, the PID and the `kill`
command - if :8000 or :5173 is already taken, and it warns (but still starts)
when `LITELLM_API_KEY` is unset, because the spec-driven endpoints
(`/api/preview`, `/api/build-from-json`) work without one.

Both ports can be moved when something else owns the defaults:

```bash
WEB_PORT=5174 ./run.sh frontend          # safe: nothing points at 5173 but you
API_PORT=8010 ./run.sh backend           # also update the proxy target in
                                         # frontend/vite.config.ts to match
```

---

## Running it by hand

Useful when you want the two halves in separate terminals, or a production-ish
frontend build.

**Backend** (terminal 1):

```bash
cd /Users/yan/works/image-to-ppt
python3 -m venv .venv                       # first time only
.venv/bin/python -m pip install -r backend/requirements.txt
export LITELLM_API_KEY='<your proxy key>'   # skip for /api/preview + /api/build-from-json
cd backend
../.venv/bin/python -m uvicorn app.main:app --port 8000 --reload
```

Check it:

```bash
curl -s http://127.0.0.1:8000/api/health
```

FastAPI also serves interactive docs at <http://127.0.0.1:8000/docs>.

**Frontend** (terminal 2):

```bash
cd /Users/yan/works/image-to-ppt/frontend
npm install                                 # first time, or after package.json changes
npm run dev                                 # http://localhost:5173/
```

**Production build of the frontend** (static files, then serve them however you
like):

```bash
cd /Users/yan/works/image-to-ppt/frontend
npm run build                               # type-checks, then writes dist/
npm run preview                             # serves dist/ on :5173
```

`npm run preview` has no `/api` proxy. The client defaults to the relative base
`/api`, so for a previewed or separately hosted build, point it straight at the
backend at build time - **including the `/api` path**:

```bash
VITE_API_BASE=http://localhost:8000/api npm run build
npm run preview                             # http://localhost:5173/
```

Those calls are cross-origin, which is exactly what the backend's CORS
allow-list (`http://localhost:5173`, `http://localhost:3000`) exists for.

---

## Environment variables

| variable | default | meaning |
| --- | --- | --- |
| `LITELLM_BASE_URL` | `http://localhost:4000/` | base URL of the OpenAI-compatible proxy. Needs the scheme |
| `LITELLM_API_KEY` | *(empty - no default)* | key the proxy expects. **Required for a real conversion**; any value your proxy accepts will do, including a dummy one for a local proxy that does not check |
| `VISION_MODEL` | `gemini-flash` | model / alias to request. Overridable per request (`model` field) or per run (`-m`) |

There are two ways to set them. **A `.env` file** in the repository root is the
easy one - it is git-ignored and loaded automatically:

```bash
cp .env.example .env          # then edit .env and put your key in
./run.sh
```

Or **export them** in the shell you start the stack from:

```bash
export LITELLM_BASE_URL='http://localhost:4000/'
export LITELLM_API_KEY='<your proxy key>'
export VISION_MODEL='gemini-flash'
./run.sh                      # start the stack in the SAME shell
```

**Precedence: the real environment always wins over `.env`**, so you can
override one value for a single run without editing the file:

```bash
LITELLM_API_KEY='sk-other' ./run.sh
```

`.env` is read by `run.sh`, by `uvicorn app.main:app` and by `backend/cli.py` -
each of them loads it before anything reads the settings, so it works however
you start the app. `backend/app/config.py` itself only reads `os.environ`, on
purpose: the **test suite never sees your `.env`**, so a local file cannot
change a test result. The loader lives in `backend/app/env_file.py`; it accepts
comments, blank lines, an `export ` prefix, quoted values and CRLF, skips a
malformed line instead of giving up on the file, and logs only variable *names*.

The backend says which of the two it used at startup, so a file that was never
loaded is obvious:

```
[api] INFO  loaded /path/to/.env -> set LITELLM_API_KEY, LITELLM_BASE_URL
```

**The key never lives in the source.** There is no default in
`backend/app/config.py`, and a test (`backend/tests/test_config.py`) scans the
entire tree - python *and* frontend sources - and fails the build if a key
literal reappears anywhere. `.env` is excluded from that scan, because a
git-ignored `.env` is exactly where a real key belongs; `.env.example` is
scanned and holds placeholders only.

`GET /api/health` reports whether a key is configured as a plain boolean. It
never echoes the key, or any prefix of it.

Neither does anything else. A proxy that rejects the key answers with
`Received API Key = sk-...<last 4>, Key Hash (Token) =<sha256 of the whole
key>`, and the OpenAI SDK puts that body verbatim into the exception text, so
every string on its way to a client - the `analysis_failed` hint and the
`warnings` array alike - goes through `converter.scrub_secrets` first: anything
shaped like a key, a hex digest, or a labelled credential is replaced with
`[redacted]`, and the configured key's own value is removed literally. The
unredacted upstream error is written to the **server log** instead. Set
`I2P_DEBUG_UPSTREAM_ERRORS=1` on the server to keep the raw text in responses
while debugging the proxy.

With the variable unset or blank, converting an image fails immediately with
`503 missing_api_key` (the hint names the variable and the export) - it never
sends a keyless request. **`POST /api/preview`, `POST /api/build-from-json`,
`GET /api/health`, `--from-json` and the whole test suite need no key at all**,
because they build from a spec instead of calling the model.

Other knobs are plain constants in `backend/app/config.py`: `LLM_TIMEOUT`
(120s), `LLM_MAX_RETRIES` (2), `SLIDE_WIDTH_IN` / `SLIDE_HEIGHT_IN`
(13.333 x 7.5in, i.e. 16:9), `COORD_SCALE` (1000), `DEFAULT_TEXT_FONT_SIZE`
(12pt), `DEFAULT_TABLE_FONT_SIZE` (10pt), `DEFAULT_BORDER_WIDTH_PT` (1.0).
Upload limits live in `backend/app/services/converter.py`:
`MAX_UPLOAD_BYTES` (20 MB), `DOWNLOAD_TTL_SECONDS` (30 min),
`DOWNLOAD_MAX_ENTRIES` (64).

---

## Prerequisite: a LiteLLM proxy

The tool talks to an OpenAI-compatible `/chat/completions` endpoint. It does
not call Google directly - it expects a [LiteLLM](https://docs.litellm.ai/)
proxy in front of the vision model, so the same code works with any provider
you can route through it.

```bash
# in another terminal
pip install "litellm[proxy]"
export GEMINI_API_KEY=...             # whatever your upstream provider needs

cat > litellm.config.yaml <<'YAML'
model_list:
  - model_name: gemini-flash          # the name this tool asks for
    litellm_params:
      model: gemini/gemini-2.0-flash  # the upstream model it maps to
      api_key: os.environ/GEMINI_API_KEY
YAML

litellm --config litellm.config.yaml --port 4000
```

The `model_name` on the left is what you put in `VISION_MODEL`; the
`litellm_params.model` on the right is the real upstream model, so you can
repoint or swap providers without touching this code. Any vision-capable model
works - `gemini-flash` is just the default.

The proxy must be reachable **before** an image can be converted.
`POST /api/preview` and `POST /api/build-from-json` do not need it at all.

---

## Preview before download

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

---

## API reference

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
[the spec format](#the-json-spec-format)). `preview` is the render-ready
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
        "fill_color": "#1D356A", "line_color": null, "line_width_pt": null },
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
- a shape python-pptx reports as a **picture** would come back as
  `{"kind": "unsupported"}` rather than crashing. The builder emits none, ever -
  `X-Element-Counts` and the suite both pin that.

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

---

## The 0..1000 coordinate system

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
      "font_size": 10 }
  ]
}
```

Element types and fields:

- **`shape`** - `kind` is `rectangle` (default), `rounded_rectangle` or
  `circle`; `fill_color`, `border_color`, `border_width`. A shape with no
  `fill_color` is drawn *unfilled*, so it can frame content painted later.
  Shapes never carry text.
- **`table`** - `data` is a 2-D array of strings; ragged rows are padded with
  `""`; `header_bg_color`, `header_font_bold`, `font_size`.
- **`text`** - `content` (**required**, with `\n` for line breaks),
  `font_size`, `font_color`, `bold`, `align`.

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

---

## The CLI

`backend/cli.py` is the original command-line tool, preserved unchanged in
behaviour. It shares the conversion core with the API, so a spec produced by
one is buildable by the other.

```bash
cd /Users/yan/works/image-to-ppt/backend
../.venv/bin/python cli.py --input sample.png --output result.pptx
```

| flag | description |
| --- | --- |
| `-i`, `--input PATH` | image to analyze. Required unless `--from-json`. Supported: `.png`, `.jpg`, `.jpeg`, `.webp` |
| `-o`, `--output PATH` | output `.pptx`. Defaults to the input stem (`shot.png` ➜ `shot.pptx`), or `out.pptx` |
| `-m`, `--model NAME` | override `VISION_MODEL` / `DEFAULT_MODEL` for this run |
| `--from-json PATH` | skip the LLM entirely and build from a saved spec |
| `--save-json PATH` | also write the validated spec to `PATH` as JSON, for inspection or reuse with `--from-json` |
| `--extra TEXT` | extra instruction appended to the user prompt |
| `-v`, `--verbose` | tracebacks and payload diagnostics |

Exit codes: **0** success, **1** runtime / LLM / build failure, **2** bad usage
or unsupported input.

### Examples

All of these run from `backend/`; `PY=../.venv/bin/python`.

```bash
cd /Users/yan/works/image-to-ppt/backend
PY=../.venv/bin/python

# 0. make yourself an input image (no assets required)
$PY make_sample_image.py                       # writes ./sample.png

# 1. the happy path
$PY cli.py -i sample.png -o /tmp/result.pptx

# 2. keep the spec so you can tweak it by hand and rebuild for free
$PY cli.py -i sample.png -o /tmp/slide.pptx --save-json /tmp/slide.json
$EDITOR /tmp/slide.json
$PY cli.py --from-json /tmp/slide.json -o /tmp/slide_v2.pptx

# 3. nudge the model when it misreads something
$PY cli.py -i sample.png --extra "The right column is a 4-row table, not text."

# 4. try a different model through the same proxy (any name it serves - see
#    "Unknown model" under Troubleshooting for how to list them)
$PY cli.py -i sample.png -m <another-model-your-proxy-serves> -o /tmp/other.pptx

# 5. no proxy, no key, no network: prove the builder works from a saved spec.
#    `--from-json` is the one CLI path that calls no model, so it needs a spec
#    file that already exists: either /tmp/slide.json from example 2, or - with
#    no key at all, ever - seed one from the suite's spec fixture first.
#    (app/mock_data.py is a TEST fixture, not a product feature; printing it is
#    just the handiest keyless source of a valid spec.)
$PY -c "from app import mock_data; print(mock_data.SAMPLE_SPEC_JSON)" > /tmp/spec.json
$PY cli.py --from-json /tmp/spec.json -o /tmp/demo.pptx

# 6. diagnose a bad run
$PY cli.py -i sample.png -v -o /tmp/verbose.pptx
```

---

## Tests

```bash
./run.sh test           # pytest + tsc --noEmit
```

or each half on its own:

```bash
cd /Users/yan/works/image-to-ppt/backend && ../.venv/bin/python -m pytest -q
cd /Users/yan/works/image-to-ppt/frontend && npm run typecheck
```

`backend/pytest.ini` pins `testpaths = tests` and `pythonpath = .`, so a bare
`pytest -q` from `backend/` works too and `from app import ...` resolves
without any `sys.path` manipulation.

The python suite is **offline by design** - no socket is ever opened. LLM calls
go through a fake client whose `.chat.completions.create(...)` returns an object
shaped like an OpenAI response, which also lets the tests assert on the exact
request that was built.

Coverage highlights:

- `tests/test_schemas.py` - colour normalization (including garbage ➜ `None`),
  `BBox` coercion and clamping, ragged-row padding, discriminated-union
  dispatch, extra keys ignored, per-element drop isolation, the `dropped`
  strip-on-input rule, and the sample spec round-tripping through JSON.
- `tests/test_vision_analyzer.py` - `encode_image` payloads and MIME types,
  `extract_json` against fenced, prose-wrapped, deeply nested,
  braces-inside-strings and metadata-prefixed responses, the vision message
  payload, and `analyze_image` happy path / invalid JSON / transport failure /
  the `response_format`-rejected retry.
- `tests/test_pptx_builder.py` - slide geometry, per-element shape mapping,
  z-order preservation, table sizing and fonts, textbox wrapping and
  alignment, graceful degradation of bad colours, one broken element not
  aborting the build, and **the reopened deck containing no picture shapes and
  no `ppt/media/` parts**.
- `tests/test_cli.py` - `--from-json`, `--save-json`, `--mock` being gone, flag
  forwarding, exit codes (0/1/2), friendly errors without tracebacks, the
  Pillow sample-image generator, and a scan of **this README's own bash blocks**
  asserting that no command sitting under a "no proxy / no key / no network"
  promise is a `-i` vision conversion (which needs both).
- `tests/test_config.py` - the API-key contract, and a scan of the **whole
  repository** (python and frontend sources alike) proving no key literal is
  committed.
- `tests/test_api.py` - every endpoint and every error code against FastAPI's
  `TestClient`: the health payload's exact shape, `api_key_configured` never
  leaking the key, the one error envelope, `spec` never carrying `dropped`, the
  download store's TTL / cap / re-download behaviour, a `file_id` that is
  not a uuid4 hex never reaching the store or the filesystem, the
  `Content-Disposition` pair for a non-ASCII upload name, and a scan of
  **`backend/` as a whole** (`cli.py` included - that is where `--mock` lived)
  proving no module a user can reach imports the `mock_data` test fixture.
- `tests/test_frontend_guards.py` - source-level guards for the frontend, in
  the suite the project actually runs: the containment rules that keep an
  unbounded backend string inside its column, and the Korean-UI rules -
  `word-break: keep-all`, no particle detached from the word it binds to, no
  bracketed either/or particle, no `0..1000` in Korean copy, and Korean copy
  for **every** error code the backend can emit (the `ConversionFailure` codes
  plus `main._CODE_BY_STATUS`, read live).

Quick manual smoke test, no proxy and no key required. `--from-json` is the one
CLI path that calls no model, so seed it from the suite's spec fixture and the
whole build path - schema validation, native shapes/tables/text boxes, the 16:9
slide - runs with no network at all:

```bash
cd /Users/yan/works/image-to-ppt/backend \
  && ../.venv/bin/python -c "from app import mock_data; print(mock_data.SAMPLE_SPEC_JSON)" > /tmp/spec.json \
  && ../.venv/bin/python cli.py --from-json /tmp/spec.json -o /tmp/demo.pptx \
  && open /tmp/demo.pptx
```

To smoke-test the **model** path instead, `cli.py -i sample.png -o /tmp/demo.pptx`
does the same thing end to end - but it needs `LITELLM_API_KEY` and a reachable
LiteLLM proxy, and it writes no file at all if the analysis fails, so the
`&& open` half never runs.

---

## Troubleshooting

**`port 8000 (backend / uvicorn) is already in use`.** `run.sh` refuses to
start rather than fight for the port, and prints the PID and the `kill` line.
See for yourself and free it:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN        # or -iTCP:5173 for the frontend
kill <pid>                              # kill -9 <pid> if it ignores that
```

Or move the service: `WEB_PORT=5174 ./run.sh frontend`, or
`API_PORT=8010 ./run.sh backend` (then update the proxy `target` in
`frontend/vite.config.ts` so the UI follows).

**Ctrl-C left something running.** It should not - `run.sh` traps `EXIT`,
`INT` and `TERM` and kills each child *and its descendants*, then reports that
both ports are free. If you killed the script with `kill -9` (which no trap can
catch), clean up by hand with the `lsof` / `kill` pair above.

One shell gotcha if you script it: when `run.sh` is started as a *background*
job from a non-interactive shell (`./run.sh &` inside another script), the shell
enters with `SIGINT` already ignored, and POSIX says a signal ignored on entry
to a non-interactive shell cannot be trapped - so a later `kill -INT` does
nothing and the stack keeps running. Use `kill -TERM <pid>` there, which the
trap does receive. Interactive Ctrl-C is unaffected: the terminal delivers
`SIGINT` to the whole foreground process group, so the children stop too.

When you check the ports yourself, match **listeners only** - a plain
`lsof -ti :5173` also matches browser tabs holding a client socket to the dev
server, which looks like a leftover process but is not one:

```bash
lsof -nP -iTCP:5173 -sTCP:LISTEN        # empty output = nothing is serving
```

**`503 missing_api_key`** (or the CLI's `LITELLM_API_KEY is not set (or is
blank)`). No key is configured, so no request was sent. Put it in `.env` at the
repository root, or export it in the shell you start the server from, then
restart:

```bash
echo "LITELLM_API_KEY=<your proxy key>" >> .env    # git-ignored, auto-loaded
./run.sh
```

**Still `503` with a key sitting in `.env`?** Check, in this order:

1. **Is the file at the repository root and named exactly `.env`?** Not
   `backend/.env`, not `.env.txt`. `ls -l .env` should show it.
2. **Did you edit `.env.example` instead of `.env`?** The example is only a
   template; nothing loads it.
3. **What did the server say at startup?** It logs the path it loaded and the
   names it set. `found ... but set nothing from it` means those names were
   already in the environment, which wins - `unset LITELLM_API_KEY` if a stale
   blank export is shadowing the file.
4. **Is the name right?** `LITELLM_API_KEY`, not `LITELLM_APIKEY` or
   `OPENAI_API_KEY`. A misspelled line is skipped silently.
5. **Confirm what the app actually sees** (prints names, never values):

   ```bash
   cd backend && ../.venv/bin/python cli.py -i sample.png -o /tmp/probe.pptx -v 2>&1 | grep '.env'
   curl -s localhost:8000/api/health | grep -o '"api_key_configured":[a-z]*'
   ```

Or stay offline with the spec-driven paths: `POST /api/preview`,
`POST /api/build-from-json` or `--from-json`, none of which need a key.

**`502 proxy_unreachable`** / `Connection refused`. The LiteLLM proxy is not
running, or `LITELLM_BASE_URL` points somewhere else. Remember the URL needs
the scheme (`http://`).

```bash
curl -s http://localhost:4000/health/readiness
curl -s http://127.0.0.1:8000/api/health        # shows the base_url in use
```

Meanwhile `POST /api/preview` and `POST /api/build-from-json` still work.

**`401` / `403` from the proxy** (surfaces as `502 analysis_failed`).
`LITELLM_API_KEY` is set but does not match what the proxy expects
(`--master_key` / its `general_settings`).

**Unknown model - `404 model not found`, or "model does not exist"** (also
`502 analysis_failed`). `VISION_MODEL` must match a name your proxy actually
serves. List them (the proxy authenticates this route too, so pass the key -
from the environment, never typed inline):

```bash
BASE="${LITELLM_BASE_URL:-http://localhost:4000/}"
curl -s -H "Authorization: Bearer $LITELLM_API_KEY" "${BASE%/}/v1/models"
```

`${BASE%/}` strips the trailing slash the default carries; plain
`$LITELLM_BASE_URL/v1/models` would request `//v1/models` and get a 404 from the
proxy itself. Override the model per request with the `model` field, or per run
with `-m`.

**CORS error in the browser console.** The dev UI should never hit this: Vite
serves on :5173 and proxies `/api` to :8000, so requests are **same-origin**.
If you see one, you are calling the backend cross-origin - the allow-list is
exactly `http://localhost:5173` and `http://localhost:3000`, so
`http://127.0.0.1:5173` (a *different* origin to a browser) or any other port
will be refused. Use `http://localhost:5173`, or go through the proxy.

**`400 unsupported_extension`** (CLI exit code 2). Only `.png`, `.jpg`,
`.jpeg` and `.webp` are accepted. Convert first:

```bash
cd /Users/yan/works/image-to-ppt
.venv/bin/python -c "from PIL import Image; Image.open('in.tiff').convert('RGB').save('in.png')"
```

**`400 not_an_image`** (CLI: `has an image extension but is not a readable
image`, exit code 2). The extension is fine but the bytes are not - a
half-finished download, an HTML error page saved as `.png`, or a file that was
merely renamed. The decode happens locally *before* a request is spent, so this
fails in milliseconds rather than coming back as a puzzling 400 from the proxy.

**`413 file_too_large`.** Over the 20 MB cap. Downscale it; a vision model gains
nothing from 40 megapixels:

```bash
cd /Users/yan/works/image-to-ppt
.venv/bin/python -c "from PIL import Image; im=Image.open('big.png'); im.thumbnail((2000,2000)); im.save('small.png')"
```

**`404 unknown_download`.** The `download_id` expired (30 min), was evicted
(more than 64 conversions since), or the backend restarted - `--reload` does
that on every edit. Re-run the conversion. If you still have the `spec` from
the response, `POST /api/build-from-json` rebuilds the same deck for free.

**`400 invalid_spec`.** Every element was dropped, so the deck would have been
blank. The `hint` lists the per-element reasons; the usual causes are a
`bbox` missing one of its four components and a `text` element with no
`content`.

**The model answered with prose, or the JSON failed to parse**
(`502 analysis_failed`). `extract_json` already strips markdown fences and
surrounding chatter, is brace- and string-literal-aware, and when a reply
contains several JSON objects it picks the spec-shaped one - so this usually
means the model genuinely refused or was cut off. Retry, use
`extra_instructions` / `--extra` to be more specific, or point `model` / `-m`
at a stronger model. The CLI's `-v` prints the raw payload diagnostics.

**Everything landed in the wrong place / overlaps.** The model mixed up the
axes or the z-order. Take the `spec` from the response (or `--save-json`), fix
the offending `bbox` values (`x`/`w` against width, `y`/`h` against height,
0-1000), and rebuild with `POST /api/build-from-json` or `--from-json` - no
tokens spent.

**Shapes appear but the text is invisible.** A filled foreground shape was
emitted *after* the text, covering it. Fix the ordering in a saved spec
(background shapes first), or add `extra_instructions` / `--extra "emit
background shapes first"`.

**A table hangs off the bottom of the slide.** See *Why the table font is
small* above; lower the table's `font_size` in the spec, or give it a shorter
`bbox` with fewer rows.

**One element is missing and `dropped` mentioned it.** That is the intended
behaviour: a single unrenderable element is reported and skipped so you still
get the rest of the slide.

**`npm install` / `tsc` complaints after a `git pull`.** `run.sh` reinstalls
only when `package.json` is newer than `node_modules`. Force it:

```bash
cd /Users/yan/works/image-to-ppt/frontend && rm -rf node_modules && npm install
```

**The venv is wrong or half-installed.** `run.sh` skips `pip install` when
`backend/requirements.txt` is unchanged *and* every top-level import resolves;
if it is beyond repair, delete it and let the script rebuild:

```bash
cd /Users/yan/works/image-to-ppt && rm -rf .venv && ./run.sh test
```
