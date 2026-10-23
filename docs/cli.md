# The CLI

[← README](../README.md)

`backend/cli.py` is the original command-line tool, preserved unchanged in
behaviour. It shares the conversion core with the API, so a spec produced by
one is buildable by the other.

```bash
cd image-to-ppt/backend
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
cd image-to-ppt/backend
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
