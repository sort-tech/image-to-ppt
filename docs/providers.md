# Prerequisite: a LiteLLM proxy

[← README](../README.md)

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

### Quick test without a proxy (Gemini / OpenAI key)

LiteLLM is only a convenience. Google and OpenAI both serve an
OpenAI-compatible endpoint, and this tool only speaks that protocol, so a plain
provider key works directly:

```bash
export GEMINI_API_KEY='<your key>'     # or OPENAI_API_KEY
./try-direct.sh gemini                 # converts backend/sample.png
./try-direct.sh openai my-slide.png gpt-4o   # image and model are optional
./try-direct.sh gemini --dry-run       # show the resolved setup, call nothing
```

| provider | `LITELLM_BASE_URL` it sets | default model |
|---|---|---|
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.8-flash` |
| `openai` | `https://api.openai.com/v1/` | `gpt-4o-mini` |

Other models (any vision-capable one; names change often, so check the
provider's model list):

| provider | examples |
|---|---|
| `gemini` | `gemini-3.8-flash` (default), `gemini-2.5-pro` |
| `openai` | `gpt-4o-mini` (default), `gpt-4o` |

```bash
./try-direct.sh gemini backend/sample.png gemini-2.5-pro
```

The script sets `PROVIDER` for that one run (your key is read from the shell,
never written to a file or printed) and writes `direct-<provider>.pptx` and
`.json` next to it. The same thing works for every entry point, because the
provider is resolved in `backend/app/config.py`:

```bash
PROVIDER=gemini GEMINI_API_KEY='<your key>' ./run.sh        # web app
PROVIDER=openai OPENAI_API_KEY='<your key>' python cli.py -i slide.png
```

Variables: [configuration.md](configuration.md).

The proxy must be reachable **before** an image can be converted.
`POST /api/preview` and `POST /api/build-from-json` do not need it at all.
