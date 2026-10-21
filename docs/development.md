# Development guide

[← README](../README.md)

For someone who is going to change the code. To just try the app, start from the
README.

## Setup

```bash
git clone https://github.com/sort-tech/image-to-ppt && cd image-to-ppt
./run.sh test            # creates .venv, installs deps, runs pytest + tsc
./run.sh                 # api :8000 (--reload) + web :5173 (HMR)
```

No Python/Node installed? Use the Docker dev mode in [docker.md](docker.md):
source is mounted, so edits show up without a rebuild.

- **Python 3.10+** (the Docker image uses 3.12). On Python 3.14 two
  `TestJsonBodyLimits` tests in `test_api.py` currently fail (deeply-nested JSON
  / recursion-limit behaviour); they are unrelated to model or provider
  settings. Use 3.10-3.12 for a fully green suite.
- **Node 18+** (the Docker image uses 20).
- You do not need a model key to work on the code: the whole test suite is
  offline and `--from-json` / `/api/preview` / `/api/build-from-json` need no
  key. A key is only for trying real conversions.

## How a conversion flows

```
image ─▶ vision_analyzer.analyze_image ─▶ SlideSpec (schemas.py, pydantic)
                                              │
                          services/converter.py (orchestration, shared by API + CLI)
                                              │
                     pptx_builder.build_presentation ◀─────┘            services/preview.py
                           │                                  (reads the BUILT deck,
                         .pptx ───────────────────────────▶    returns render-ready JSON)
                                                              │
                                              frontend/src/components/SlidePreview.tsx
```

| you want to change | look in |
| --- | --- |
| which model / endpoint / key is used | `backend/app/config.py` (`PROVIDERS`, env vars) |
| the prompt, how the reply is parsed | `backend/app/vision_analyzer.py` |
| what a valid spec is (fields, coercion, drop rules) | `backend/app/schemas.py` |
| how an element becomes a PowerPoint object | `backend/app/pptx_builder.py` (`add_shape`, `add_table`, `add_textbox`, `add_connector`, `add_picture`) |
| what the UI preview shows | `backend/app/services/preview.py` + `frontend/src/components/SlidePreview.tsx` |
| HTTP routes / error codes | `backend/app/api/routes.py`, `services/converter.py` |
| CLI flags and messages | `backend/cli.py` |

## Adding an element type (checklist)

An element type touches the same four places; miss one and a test or the UI
will tell you:

1. `schemas.py` - a new `...Element` class with a `type` literal, added to the
   discriminated union.
2. `pptx_builder.py` - an `add_...` function and its dispatch.
3. `services/preview.py` - emit it in the preview so the UI can draw it.
4. `frontend/src/types/slide.ts` (mirror the types) and `SlidePreview.tsx`
   (a `case` in the `switch (shape.kind)`).

Then add cases to `tests/test_schemas.py`, `tests/test_pptx_builder.py` and
`tests/test_preview.py`, and document the element in [spec-format.md](spec-format.md).
Remember the anti-flattening rules ([spec-format.md](spec-format.md)): a new
element must not become a way to paste the source image onto the slide.

## Testing

```bash
./run.sh test                                   # everything
cd backend && ../.venv/bin/python -m pytest -q -k preview     # a slice
cd frontend && npm run typecheck
```

Details and what each module covers: [testing.md](testing.md). Two rules the
suite enforces that surprise newcomers:

- **No key literal anywhere in the repo.** Put keys in your shell or the
  git-ignored `.env`; `.env.example` holds placeholders only.
- **The README's own bash blocks are scanned**: a command under a "no proxy /
  no key / no network" promise must not be a `-i` vision conversion. Keep that
  in mind when editing the docs.

## Configuration while developing

`.env` (git-ignored) is loaded by `run.sh`, `uvicorn` and `cli.py`; the real
environment wins. The test suite never reads `.env`. Variables:
[configuration.md](configuration.md).

## Conventions

- Configuration falls back rather than breaks (a blank `VISION_MODEL=` means
  the default).
- The API contract (codes like `invalid_spec`) is English and stable; the UI
  translates it. Do not localise error codes.
- `mock_data.py` is a test fixture, not a product feature.
