# Tests

[← README](../README.md)

```bash
./run.sh test           # pytest + tsc --noEmit
```

or each half on its own:

```bash
cd image-to-ppt/backend && ../.venv/bin/python -m pytest -q
cd image-to-ppt/frontend && npm run typecheck
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
  alignment, connector endpoints / arrowheads (written as raw `a:headEnd` /
  `a:tailEnd`, since python-pptx has no arrowhead API) / dash styles, cropping
  an `image` element out of the source, graceful degradation of bad colours, one
  broken element not aborting the build, and **the reopened deck passing
  `pptx_builder.flattening_report()`** - the three anti-flattening rules quoted
  in [spec-format.md](spec-format.md#anti-flattening-rules), plus the check that every embedded picture's bytes
  really resolve inside the package.
- `tests/test_cli.py` - `--from-json`, `--save-json`, `--mock` being gone, flag
  forwarding, exit codes (0/1/2), friendly errors without tracebacks, the
  Pillow sample-image generator, and a scan of **the README's and docs' own bash blocks**
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
cd image-to-ppt/backend \
  && ../.venv/bin/python -c "from app import mock_data; print(mock_data.SAMPLE_SPEC_JSON)" > /tmp/spec.json \
  && ../.venv/bin/python cli.py --from-json /tmp/spec.json -o /tmp/demo.pptx \
  && open /tmp/demo.pptx
```

To smoke-test the **model** path instead, `cli.py -i sample.png -o /tmp/demo.pptx`
does the same thing end to end - but it needs `LITELLM_API_KEY` and a reachable
LiteLLM proxy, and it writes no file at all if the analysis fails, so the
`&& open` half never runs.
