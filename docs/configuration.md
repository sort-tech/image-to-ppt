# Environment variables

[← README](../README.md)

| variable | default | meaning |
| --- | --- | --- |
| `LITELLM_BASE_URL` | `http://localhost:4000/` | base URL of the OpenAI-compatible proxy. Needs the scheme |
| `LITELLM_API_KEY` | *(empty - no default)* | key the proxy expects. **Required for a real conversion**; any value your proxy accepts will do, including a dummy one for a local proxy that does not check |
| `VISION_MODEL` | `gemini-flash` | model / alias to request. Overridable per request (`model` field) or per run (`-m`) |

### No proxy: call Gemini, OpenAI or Claude directly

Set that provider's own key - one key is enough. `PROVIDER` picks one
explicitly; without it the first key present wins, in this order:
`LITELLM_API_KEY` (so an existing proxy setup is never redirected by a
provider key exported for another tool), `GEMINI_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`. In direct mode the three variables above are ignored
(works for `run.sh`, `uvicorn`, `cli.py` and Docker alike):

| variable | default | meaning |
| --- | --- | --- |
| `PROVIDER` | *(unset = picked by key)* | `gemini`, `openai` or `anthropic`; `litellm` forces the proxy. A typo logs a warning and falls back to picking by key |
| `GEMINI_API_KEY` | *(empty)* | Gemini's own key |
| `OPENAI_API_KEY` | *(empty)* | OpenAI's own key |
| `ANTHROPIC_API_KEY` | *(empty)* | Anthropic's own key. Claude is called through the official `anthropic` SDK (Messages API - `backend/app/anthropic_client.py`), not a compatibility endpoint; `ANTHROPIC_BASE_URL` is honoured as the SDK does |
| `PROVIDER_MODEL` | `gemini-3.8-flash` / `gpt-4o-mini` / `claude-opus-5-5` | model to request; overridable per request or with `-m`. `VISION_MODEL` is the proxy's alias and is not read in direct mode, so a stale `VISION_MODEL=gemini-flash` is never sent to Google |

```bash
PROVIDER=gemini GEMINI_API_KEY='<your key>' ./run.sh
```

See [providers.md](providers.md) for the endpoints and model examples.

A variable that is **present but blank** (`VISION_MODEL=` on its own line in
`.env`) counts as unset and falls back to the default above. It used to be
copied through as `""`, and nothing downstream could recover it: the proxy
answered `Invalid model name passed in model=` and `/api/health` reported no
model at all, which the UI's status pill - the only place the model is named
since the picker went - rendered as a dangling ` · 키 설정됨`.

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
