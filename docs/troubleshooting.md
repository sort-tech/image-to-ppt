# Troubleshooting

[← README](../README.md)

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
with `-m`. In the **browser** there is no model field any more, so the fix there
is `VISION_MODEL` in `.env` plus a backend restart - which is what the Korean
copy for `analysis_failed` now tells you, since the same code covers a model
that never answered and a model whose answer could not be used.

**CORS error in the browser console.** The dev UI should never hit this: Vite
serves on :5173 and proxies `/api` to :8000, so requests are **same-origin**.
If you see one, you are calling the backend cross-origin - the allow-list is
exactly `http://localhost:5173` and `http://localhost:3000`, so
`http://127.0.0.1:5173` (a *different* origin to a browser) or any other port
will be refused. Use `http://localhost:5173`, or go through the proxy.

**`400 unsupported_extension`** (CLI exit code 2). Only `.png`, `.jpg`,
`.jpeg` and `.webp` are accepted. Convert first:

```bash
cd image-to-ppt
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
cd image-to-ppt
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
cd image-to-ppt/frontend && rm -rf node_modules && npm install
```

**The venv is wrong or half-installed.** `run.sh` skips `pip install` when
`backend/requirements.txt` is unchanged *and* every top-level import resolves;
if it is beyond repair, delete it and let the script rebuild:

```bash
cd image-to-ppt && rm -rf .venv && ./run.sh test
```
