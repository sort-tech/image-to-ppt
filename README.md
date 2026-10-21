# image-to-pptx

Turn a picture of a slide into a **real PowerPoint slide** - in the browser, or
from the command line.

Feed it a screenshot, a scanned page, a whiteboard photo or a diagram, and it
writes a `.pptx` whose shapes, connectors, tables and text boxes are **native,
editable PowerPoint objects**: you can click the table and retype a cell, drag
a box, restyle the header, re-point an arrow.

The one exception is an **`image` element** - a photo, a rendered chart, a logo
the analyzer cannot express as shape, table or text. It is cropped out of the
source and embedded, under three rules that stop the deck from degenerating
into the input pasted onto a slide ([the anti-flattening rules](docs/spec-format.md#anti-flattening-rules)).

```
  browser (React) ──┐
                    ├──▶ FastAPI ──▶ Vision LLM ──▶ SlideSpec (JSON) ──▶ python-pptx ──▶ .pptx
  backend/cli.py ───┘                (gemini / openai    validated                       native shapes,
                                      or via LiteLLM)  by pydantic                     tables, text
```

The web app and the CLI are two front doors onto **the same conversion core**
(`backend/app/`). Nothing about the validation, the coordinate system or the
builder changed when the UI was added.

The browser UI is **in Korean** and shows a [preview of the
slide](docs/preview.md) before you download it. The API and the CLI
stay English: codes like `invalid_spec` are a contract, and the UI translates
them on the way to the screen (`frontend/src/api/client.ts`).

---

## Try it in 2 minutes

Pick one. You need a model key only to convert an image; the editor, preview
and rebuild-from-spec work without one.

**Docker** (nothing else installed) - details in [docs/docker.md](docs/docker.md):

```bash
export GEMINI_API_KEY='<your key>'          # or OPENAI_API_KEY with PROVIDER=openai
PROVIDER=gemini docker compose up --build   # then open http://localhost:5173
```

**Command line, straight to Gemini/OpenAI** - no proxy, no server:

```bash
export GEMINI_API_KEY='<your key>'
./try-direct.sh gemini                      # converts backend/sample.png -> direct-gemini.pptx
```

**Local dev stack** (Python 3.10+ and Node 18+), hot reload on both halves:

```bash
PROVIDER=gemini GEMINI_API_KEY='<your key>' ./run.sh    # then open http://localhost:5173
```

Details: [Quick start](#quick-start). Working on the code? Read
[docs/development.md](docs/development.md) first.

A LiteLLM proxy is optional: both routes above talk to the provider directly
([docs/providers.md](docs/providers.md)).

## Documentation

| guide | what is in it |
| --- | --- |
| [docs/development.md](docs/development.md) | **start here to change code**: data flow, where things live, adding an element, tests |
| [docs/docker.md](docs/docker.md) | try / develop with Docker, with a provider key or a proxy |
| [docs/providers.md](docs/providers.md) | Gemini/OpenAI direct, or a LiteLLM proxy |
| [docs/configuration.md](docs/configuration.md) | every environment variable, `.env` handling, key safety |
| [docs/manual-setup.md](docs/manual-setup.md) | run backend and frontend by hand, production build |
| [docs/preview.md](docs/preview.md) | the preview shown before you download |
| [docs/api.md](docs/api.md) | HTTP API reference and error codes |
| [docs/spec-format.md](docs/spec-format.md) | the 0..1000 coordinate system and the JSON spec |
| [docs/cli.md](docs/cli.md) | `backend/cli.py` |
| [docs/testing.md](docs/testing.md) | running and reading the test suite |
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptom -> cause -> fix |

---

## Layout

```
image-to-ppt/
├── run.sh                      one command to bring the whole stack up
├── try-direct.sh               CLI conversion with a Gemini/OpenAI key, no proxy
├── docker-compose.yml          try it: docker compose up --build
├── docker-compose.dev.yml      develop: source mounted, hot reload
├── README.md                   this file (the rest lives in docs/)
├── docs/                       guides and reference, one topic per file
│                               (development.md is the contributor entry point)
├── .env.example                placeholder env (the real key stays in your shell)
├── .gitignore
├── .venv/                      python virtualenv (created by run.sh)
│
├── backend/
│   ├── app/
│   │   ├── config.py           endpoint, model, slide geometry, styling defaults
│   │   ├── schemas.py          the pydantic contract + tolerant validation
│   │   ├── vision_analyzer.py  prompt, request, JSON recovery
│   │   ├── pptx_builder.py     SlideSpec ➜ .pptx (native objects + fenced crops)
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
│   ├── Dockerfile  docker-entrypoint.sh
│   ├── requirements.txt
│   ├── pytest.ini
│   └── tests/                  test_schemas / test_pptx_builder / test_vision_analyzer
│                               / test_preview / test_cli / test_config / test_api
│                               / test_env_file / test_frontend_guards - fully offline
│
└── frontend/
    ├── Dockerfile  nginx.conf
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
git clone https://github.com/sort-tech/image-to-ppt
cd image-to-ppt
export PROVIDER=gemini GEMINI_API_KEY='<your key>'   # or PROVIDER=openai + OPENAI_API_KEY,
                                                     # or LITELLM_API_KEY for a proxy;
                                                     # or put them in .env (git-ignored)
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

It resolves its own directory, so `/path/to/image-to-ppt/run.sh` works
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


More run options (separate terminals, production build) are in
[docs/manual-setup.md](docs/manual-setup.md). Variables are listed in
[docs/configuration.md](docs/configuration.md): `PROVIDER` + `GEMINI_API_KEY` /
`OPENAI_API_KEY` (direct), or `LITELLM_BASE_URL` + `LITELLM_API_KEY` +
`VISION_MODEL` (proxy).
