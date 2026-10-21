# Docker: try it, or develop in it

[← README](../README.md)

No Python or Node on your machine - just Docker. Two modes share one
`docker-compose.yml`.

| | **Try** (default) | **Develop** |
| --- | --- | --- |
| for | users kicking the tyres, demos | changing the code |
| frontend | built once, served by nginx | Vite dev server, hot reload |
| backend | image copy of `backend/` | `backend/` mounted, `uvicorn --reload` |
| command | `docker compose up --build` | `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` |

Both open at **<http://localhost:5173>** (API on :8000).

## 1. Try it with a Gemini or OpenAI key (no proxy)

```bash
export GEMINI_API_KEY='<your key>'
PROVIDER=gemini docker compose up --build
```

```bash
export OPENAI_API_KEY='<your key>'
PROVIDER=openai docker compose up --build
```

`PROVIDER` points the app at the provider's OpenAI-compatible endpoint and
uses that provider's own key - no LiteLLM needed (see
[providers.md](providers.md)). The default model is `gemini-3.8-flash` /
`gpt-4o-mini`; change it with e.g. `PROVIDER_MODEL=gemini-2.5-pro` or `PROVIDER_MODEL=gpt-4o`. `GET /api/health` shows the
endpoint and model in use and whether a key is configured (never the key).

Instead of exporting, you can put the same lines in a git-ignored `.env` next
to `docker-compose.yml` - Compose reads it automatically:

```bash
cp .env.example .env     # then add: PROVIDER=gemini  and  GEMINI_API_KEY=...
```

## 2. Try it with a LiteLLM proxy

Leave `PROVIDER` unset. Inside a container `localhost` is the container itself,
so a proxy on your host is reached as `host.docker.internal`:

```bash
LITELLM_API_KEY='<proxy key>' docker compose up --build
# other proxy URL:  DOCKER_LITELLM_BASE_URL=http://my-proxy:4000/ ...
# other model:      VISION_MODEL=my-alias ...
```

## 3. No key at all

`docker compose up --build` with nothing set still runs: the editor,
`POST /api/preview` and `POST /api/build-from-json` need no model. Only
image conversion answers `503 missing_api_key`.

## Develop

```bash
PROVIDER=gemini docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Edit `backend/` or `frontend/src/` and the change shows up without a rebuild
(file watching uses polling because bind mounts on macOS/Windows emit no
events). Rebuild (`--build`) after changing `requirements.txt` or
`package.json`. Run the test suite on the host
(`./run.sh test`): the image holds `backend/` only, and some tests check
repo-root files (`.env.example`, `run.sh`).

## Housekeeping

```bash
WEB_PORT=5174 API_PORT=8010 docker compose up --build   # ports taken
docker compose logs -f backend                          # see requests/errors
docker compose down                                     # stop and remove
```

Keys are passed as environment variables at run time. They are never baked
into an image, and `.env` is excluded from the build context by
`.dockerignore`.
