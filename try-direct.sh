#!/usr/bin/env bash
#
# try-direct.sh - convert an image with a provider's OWN key, no LiteLLM proxy.
#
#   GEMINI_API_KEY=...  ./try-direct.sh gemini [IMAGE] [MODEL]
#   OPENAI_API_KEY=...  ./try-direct.sh openai [IMAGE] [MODEL]   (or ANTHROPIC_API_KEY ... anthropic)
#   ./try-direct.sh gemini --dry-run        # show what would run; calls nothing
#
# Why this works without a proxy: both Google and OpenAI serve an
# OpenAI-compatible /chat/completions endpoint, and the app only ever speaks
# that protocol. This script just sets PROVIDER (see backend/app/config.py for
# the endpoints and default models); your provider key is read from YOUR shell,
# never written to a file and never printed.
# Output goes to ./direct-<provider>.pptx and ./direct-<provider>.json.
#
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PY="${PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

usage() { sed -n '3,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

PROVIDER="${1:-}"; [ -n "$PROVIDER" ] || usage
shift || true

DRY=0; ARGS=()
for a in "$@"; do
    if [ "$a" = "--dry-run" ]; then DRY=1; else ARGS+=("$a"); fi
done
IMAGE="${ARGS[0]:-$ROOT/backend/sample.png}"
case "$IMAGE" in /*) ;; *) IMAGE="$PWD/$IMAGE" ;; esac

case "$PROVIDER" in
    gemini) KEY_VAR=GEMINI_API_KEY ;;
    openai) KEY_VAR=OPENAI_API_KEY ;;
    anthropic) KEY_VAR=ANTHROPIC_API_KEY ;;
    -h|--help|help) usage 0 ;;
    *) echo "unknown provider '$PROVIDER' (use: gemini | openai | anthropic)" >&2; exit 2 ;;
esac

KEY="${!KEY_VAR:-}"
OUT="$ROOT/direct-$PROVIDER"
[ -z "${ARGS[1]:-}" ] || export PROVIDER_MODEL="${ARGS[1]}"
export PROVIDER

# The endpoint and default model live in backend/app/config.py (PROVIDERS); ask
# it rather than keeping a second copy here. config.py needs no dependencies.
RESOLVED="$(cd "$ROOT/backend" && "$PY" -c 'from app import config; print(config.LITELLM_BASE_URL, config.DEFAULT_MODEL)')"
BASE_URL="${RESOLVED% *}"; MODEL="${RESOLVED#* }"

echo "[try-direct] provider=$PROVIDER model=$MODEL"
echo "[try-direct] base_url=$BASE_URL"
echo "[try-direct] image=$IMAGE"
if [ -n "$KEY" ]; then echo "[try-direct] $KEY_VAR is set"; else echo "[try-direct] $KEY_VAR is NOT set"; fi

if [ "$DRY" = 1 ]; then
    echo "[try-direct] dry run: nothing was sent"
    exit 0
fi
if [ -z "$KEY" ]; then
    echo "[try-direct] export $KEY_VAR first, e.g.  export $KEY_VAR='<your key>'" >&2
    exit 2
fi

cd "$ROOT/backend"
"$PY" cli.py -i "$IMAGE" -o "$OUT.pptx" --save-json "$OUT.json" -v
echo "[try-direct] wrote $OUT.pptx and $OUT.json"
