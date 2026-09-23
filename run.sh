#!/usr/bin/env bash
#
# run.sh - bring up the whole image-to-pptx stack with one command.
#
#   ./run.sh              start the FastAPI backend (:8000) and the Vite dev server (:5173)
#   ./run.sh backend      start only the backend
#   ./run.sh frontend     start only the frontend
#   ./run.sh test         run the python suite (pytest) and the TS type-check (tsc --noEmit)
#   ./run.sh help         this text
#
# It is safe to run from any directory: the script resolves its own location.
# Ctrl-C stops every child it started - no orphaned uvicorn or vite is left
# holding a port.
#
# Ports can be moved when something else already owns the defaults:
#   API_PORT=8010 WEB_PORT=5174 ./run.sh
# Moving API_PORT only makes sense together with the frontend's dev proxy
# target (frontend/vite.config.ts), so prefer freeing the default port.
#
# Env read (never written to a file - see README.md):
#   LITELLM_BASE_URL  LITELLM_API_KEY  VISION_MODEL
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# Paths - resolved from this script, so the cwd is irrelevant
# --------------------------------------------------------------------------- #
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
VENV_DIR="${VENV_DIR:-$ROOT/.venv}"
VENV_PY="$VENV_DIR/bin/python"
REQUIREMENTS="$BACKEND_DIR/requirements.txt"
REQ_STAMP="$VENV_DIR/.requirements.stamp"

API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-5173}"

# How long to wait for each service to answer before saying so (seconds).
READY_TIMEOUT="${READY_TIMEOUT:-60}"

# --------------------------------------------------------------------------- #
# Output helpers - colour only when stdout is a terminal
# --------------------------------------------------------------------------- #
if [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'
    C_RUN=$'\033[38;5;245m'; C_API=$'\033[36m'; C_WEB=$'\033[35m'
    C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OK=$'\033[32m'
else
    C_RESET=""; C_BOLD=""; C_RUN=""; C_API=""; C_WEB=""
    C_WARN=""; C_ERR=""; C_OK=""
fi

log()  { printf '%s[run.sh]%s %s\n' "$C_RUN" "$C_RESET" "$*"; }
ok()   { printf '%s[run.sh]%s %s%s%s\n' "$C_RUN" "$C_RESET" "$C_OK" "$*" "$C_RESET"; }
warn() { printf '%s[run.sh]%s %sWARNING:%s %s\n' "$C_RUN" "$C_RESET" "$C_WARN" "$C_RESET" "$*" >&2; }
err()  { printf '%s[run.sh]%s %sERROR:%s %s\n' "$C_RUN" "$C_RESET" "$C_ERR" "$C_RESET" "$*" >&2; }
die()  { err "$*"; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# Read a stream and stamp every line with a service tag. The trailing read
# catches a final line that was not newline-terminated (a crash traceback, say).
prefix_lines() {
    local tag="$1" line
    while IFS= read -r line; do
        printf '%s %s\n' "$tag" "$line"
    done
    if [ -n "${line:-}" ]; then
        printf '%s %s\n' "$tag" "$line"
    fi
}

# --------------------------------------------------------------------------- #
# Preflight: tools and ports
# --------------------------------------------------------------------------- #
need_python() {
    [ -x "$VENV_PY" ] && return 0
    have python3 || die "python3 was not found on PATH.
  Install Python 3.10+ (macOS: brew install python@3.12) and retry.
  Already have one elsewhere? Point this script at it:
    VENV_DIR=/path/to/existing/.venv ./run.sh"
}

need_node() {
    have node || die "node was not found on PATH.
  Install Node 18+ (macOS: brew install node), then retry."
    have npm || die "npm was not found on PATH.
  It ships with Node - reinstall Node (macOS: brew install node), then retry."
}

# PIDs listening on a TCP port, one per line (empty when the port is free).
port_pids() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null || true
}

require_free_port() {
    local port="$1" label="$2" pids
    pids="$(port_pids "$port" | tr '\n' ' ' | sed 's/ *$//')"
    [ -z "$pids" ] && return 0
    err "port $port ($label) is already in use by PID(s): $pids"
    printf '\n  Who has it:\n    lsof -nP -iTCP:%s -sTCP:LISTEN\n' "$port" >&2
    printf '  Free it:\n    kill %s          # then retry; kill -9 if it ignores that\n' "$pids" >&2
    printf '  Or move this service to another port:\n' >&2
    if [ "$port" = "$API_PORT" ]; then
        printf '    API_PORT=%s ./run.sh      # keep frontend/vite.config.ts proxy in step\n\n' "$((port + 10))" >&2
    else
        printf '    WEB_PORT=%s ./run.sh\n\n' "$((port + 1))" >&2
    fi
    exit 1
}

# Load $ROOT/.env into the environment, WITHOUT overriding anything already
# set, so an inline `LITELLM_API_KEY=... ./run.sh` or a prior export still wins.
# `set -a` would export every name in the file but would also clobber existing
# values, so each name is checked first. Values are never printed.
load_env_file() {
    local file="$ROOT/.env" name value applied=()
    [ -f "$file" ] || { log "no .env at $file - using the environment only."; return 0; }

    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"          # ltrim
        case "$line" in ''|'#'*) continue ;; esac
        line="${line#export }"
        name="${line%%=*}"
        value="${line#*=}"
        [ "$name" = "$line" ] && continue                # no '=' on the line
        name="$(printf '%s' "$name" | tr -d '[:space:]')"
        case "$name" in ''|*[!A-Za-z0-9_]*) continue ;; esac
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"       # rtrim
        case "$value" in
            \"*\") value="${value%\"}"; value="${value#\"}" ;;
            \'*\') value="${value%\'}"; value="${value#\'}" ;;
        esac
        # Only fill in what is not already set to something non-empty.
        if [ -z "${!name:-}" ]; then
            export "$name=$value"
            applied+=("$name")
        fi
    done < "$file"

    if [ "${#applied[@]}" -gt 0 ]; then
        log "loaded .env -> set ${applied[*]} (values not shown)."
    else
        log "found .env but set nothing from it (already in the environment)."
    fi
}

check_api_key() {
    if [ -z "${LITELLM_API_KEY:-}" ]; then
        warn "LITELLM_API_KEY is unset (or blank) - starting anyway.
           Mock mode needs no key: tick \"Mock mode\" in the UI, POST /api/convert with
           is_mock=true, or run 'python cli.py --mock'. Real conversions will come back
           503 missing_api_key until you set a key. Either export it in THIS shell:
             export LITELLM_API_KEY='<your proxy key>'
           or put it in $ROOT/.env (git-ignored, loaded automatically):
             LITELLM_API_KEY=<your proxy key>"
    else
        log "LITELLM_API_KEY is set (value not shown)."
    fi
    log "proxy: ${LITELLM_BASE_URL:-http://localhost:4000/}   model: ${VISION_MODEL:-gemini-flash}"
}

# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
ensure_venv() {
    need_python
    if [ -x "$VENV_PY" ]; then
        log "reusing venv at $VENV_DIR"
    else
        log "creating venv at $VENV_DIR ..."
        python3 -m venv "$VENV_DIR"
        [ -x "$VENV_PY" ] || die "venv creation did not produce $VENV_PY"
        "$VENV_PY" -m pip install --quiet --upgrade pip
    fi
}

requirements_fingerprint() {
    if have shasum; then
        shasum -a 256 "$REQUIREMENTS" | awk '{print $1}'
    else
        cksum "$REQUIREMENTS" | awk '{print $1"-"$2}'
    fi
}

# True when the stamp matches AND every top-level import really resolves, so a
# half-installed venv is repaired instead of skipped.
python_deps_satisfied() {
    [ -f "$REQ_STAMP" ] || return 1
    [ "$(cat "$REQ_STAMP")" = "$(requirements_fingerprint)" ] || return 1
    "$VENV_PY" - <<'PY' >/dev/null 2>&1
import importlib
for mod in ("fastapi", "uvicorn", "multipart", "pptx", "pydantic", "openai", "PIL", "pytest", "httpx"):
    importlib.import_module(mod)
PY
}

ensure_python_deps() {
    ensure_venv
    [ -f "$REQUIREMENTS" ] || die "$REQUIREMENTS is missing - is the checkout complete?"
    if python_deps_satisfied; then
        log "python deps already satisfied (backend/requirements.txt unchanged) - skipping pip install"
        return 0
    fi
    log "installing python deps from backend/requirements.txt ..."
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install -r "$REQUIREMENTS"
    requirements_fingerprint > "$REQ_STAMP"
    ok "python deps installed."
}

ensure_frontend_deps() {
    need_node
    [ -d "$FRONTEND_DIR" ] || die "$FRONTEND_DIR does not exist.
  The web UI lives in frontend/. Use './run.sh backend' for the API alone."
    [ -f "$FRONTEND_DIR/package.json" ] || die "$FRONTEND_DIR/package.json is missing."
    if [ -d "$FRONTEND_DIR/node_modules" ] &&
       [ ! "$FRONTEND_DIR/package.json" -nt "$FRONTEND_DIR/node_modules" ]; then
        log "node deps already satisfied (node_modules newer than package.json) - skipping npm install"
        return 0
    fi
    if [ -d "$FRONTEND_DIR/node_modules" ]; then
        log "package.json changed since the last install - running npm install ..."
    else
        log "frontend/node_modules is missing - running npm install ..."
    fi
    ( cd "$FRONTEND_DIR" && npm install )
    # Make node_modules newer than package.json even if npm did nothing.
    touch "$FRONTEND_DIR/node_modules"
    ok "node deps installed."
}

# --------------------------------------------------------------------------- #
# Process supervision
# --------------------------------------------------------------------------- #
SERVICE_PIDS=()
SERVICE_NAMES=()
# Only the ports THIS invocation bound. `require_free_port` proved each of them
# was free beforehand, so a listener still on one afterwards really is ours -
# whereas checking both ports in single-service mode would blame us for whatever
# else happens to own the port we never touched.
STARTED_PORTS=()
CLEANED=0

# SIGTERM/SIGKILL a process and every descendant, deepest first. uvicorn
# --reload forks a worker and `npm run dev` forks node, so killing only the
# PID we started would leave the real listener behind.
kill_tree() {
    local pid="$1" sig="${2:-TERM}" kid
    for kid in $(pgrep -P "$pid" 2>/dev/null || true); do
        kill_tree "$kid" "$sig"
    done
    kill -"$sig" "$pid" 2>/dev/null || true
}

cleanup() {
    [ "$CLEANED" = 1 ] && return 0
    CLEANED=1
    trap - EXIT INT TERM
    local i pid waited
    [ "${#SERVICE_PIDS[@]}" -eq 0 ] && return 0
    printf '\n'
    log "shutting down ..."
    i=0
    while [ "$i" -lt "${#SERVICE_PIDS[@]}" ]; do
        pid="${SERVICE_PIDS[$i]}"
        if kill -0 "$pid" 2>/dev/null; then
            log "stopping ${SERVICE_NAMES[$i]} (pid $pid)"
            kill_tree "$pid" TERM
        fi
        i=$((i + 1))
    done
    # Give them a moment to exit cleanly, then insist.
    waited=0
    while [ "$waited" -lt 50 ]; do
        local alive=0
        for pid in ${SERVICE_PIDS[@]+"${SERVICE_PIDS[@]}"}; do
            kill -0 "$pid" 2>/dev/null && alive=1
        done
        [ "$alive" = 0 ] && break
        sleep 0.1
        waited=$((waited + 1))
    done
    for pid in ${SERVICE_PIDS[@]+"${SERVICE_PIDS[@]}"}; do
        if kill -0 "$pid" 2>/dev/null; then
            log "forcing pid $pid"
            kill_tree "$pid" KILL
        fi
    done
    wait 2>/dev/null || true
    # Last line of defence: anything still holding a port WE bound.
    local port leftover stuck="" listed=""
    for port in ${STARTED_PORTS[@]+"${STARTED_PORTS[@]}"}; do
        listed="$listed $port"
        leftover="$(port_pids "$port" | tr '\n' ' ' | sed 's/ *$//')"
        [ -n "$leftover" ] && stuck="$stuck $port (pid $leftover)"
    done
    if [ -n "$stuck" ]; then
        warn "these ports are still listening after shutdown:$stuck
           Check and clear by hand:  lsof -nP -iTCP:${STARTED_PORTS[0]} -sTCP:LISTEN"
    elif [ -n "$listed" ]; then
        ok "all children stopped; port(s)$listed free."
    fi
}

start_service() {
    local label="$1" color="$2" dir="$3"; shift 3
    local tag pid
    tag="$(printf '%s[%s]%s' "$color" "$label" "$C_RESET")"
    # `exec` inside the subshell means $! is the real process, not a wrapper,
    # and the process substitution keeps its output line-prefixed.
    ( cd "$dir" && exec "$@" ) > >(prefix_lines "$tag") 2>&1 &
    pid=$!
    SERVICE_PIDS+=("$pid")
    SERVICE_NAMES+=("$label")
    log "$label started (pid $pid)"
}

start_backend() {
    check_api_key
    STARTED_PORTS+=("$API_PORT")
    start_service api "$C_API" "$BACKEND_DIR" \
        "$VENV_PY" -m uvicorn app.main:app \
        --host "$API_HOST" --port "$API_PORT" --reload
}

start_frontend() {
    # --strictPort so a clash is an error instead of a silent hop to 5174,
    # which would leave the printed URL wrong. The host stays whatever
    # frontend/vite.config.ts chose.
    STARTED_PORTS+=("$WEB_PORT")
    start_service web "$C_WEB" "$FRONTEND_DIR" \
        npm run dev -- --port "$WEB_PORT" --strictPort
}

# Poll a URL until it answers (any HTTP status counts as "listening").
wait_for_url() {
    local url="$1" label="$2" i=0
    while [ "$i" -lt "$((READY_TIMEOUT * 2))" ]; do
        if curl -fsS -o /dev/null --max-time 2 "$url" 2>/dev/null ||
           curl -sS -o /dev/null --max-time 2 "$url" 2>/dev/null; then
            ok "$label is answering at $url"
            return 0
        fi
        # Bail out early if the service died on us.
        local pid alive=0
        for pid in ${SERVICE_PIDS[@]+"${SERVICE_PIDS[@]}"}; do
            kill -0 "$pid" 2>/dev/null && alive=1
        done
        [ "$alive" = 0 ] && { err "$label exited before it answered - see its log above."; return 1; }
        sleep 0.5
        i=$((i + 1))
    done
    warn "$label did not answer at $url within ${READY_TIMEOUT}s (still starting? see its log above)"
    return 0
}

# Block until any child dies; Ctrl-C lands in the trap instead.
#
# A signal that arrives while `sleep` is running is delivered to bash but only
# acted on once `sleep` returns, so the CLEANED check below keeps the trap's
# shutdown from being reported a second time as a crash.
supervise() {
    local pid
    while :; do
        [ "$CLEANED" = 1 ] && return 0
        for pid in ${SERVICE_PIDS[@]+"${SERVICE_PIDS[@]}"}; do
            if ! kill -0 "$pid" 2>/dev/null; then
                [ "$CLEANED" = 1 ] && return 0
                err "a service exited (pid $pid) - stopping the rest."
                return 1
            fi
        done
        sleep 1
    done
}

# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
mode_backend() {
    ensure_python_deps
    require_free_port "$API_PORT" "backend / uvicorn"
    trap cleanup EXIT INT TERM
    start_backend
    wait_for_url "http://$API_HOST:$API_PORT/api/health" "backend" || return 1
    printf '\n'
    log "${C_BOLD}API${C_RESET}    http://$API_HOST:$API_PORT/api/health   (docs: http://$API_HOST:$API_PORT/docs)"
    log "Ctrl-C to stop."
    printf '\n'
    supervise
}

mode_frontend() {
    ensure_frontend_deps
    require_free_port "$WEB_PORT" "frontend / vite"
    trap cleanup EXIT INT TERM
    start_frontend
    wait_for_url "http://localhost:$WEB_PORT/" "frontend" || return 1
    printf '\n'
    log "${C_BOLD}UI${C_RESET}     http://localhost:$WEB_PORT/"
    log "(the UI calls the API - start it too with './run.sh backend' or just './run.sh')"
    log "Ctrl-C to stop."
    printf '\n'
    supervise
}

mode_all() {
    ensure_python_deps
    ensure_frontend_deps
    require_free_port "$API_PORT" "backend / uvicorn"
    require_free_port "$WEB_PORT" "frontend / vite"
    trap cleanup EXIT INT TERM
    start_backend
    start_frontend
    wait_for_url "http://$API_HOST:$API_PORT/api/health" "backend" || return 1
    wait_for_url "http://localhost:$WEB_PORT/" "frontend" || return 1
    printf '\n'
    log "${C_BOLD}UI${C_RESET}     http://localhost:$WEB_PORT/"
    log "${C_BOLD}API${C_RESET}    http://$API_HOST:$API_PORT/api/health   (docs: http://$API_HOST:$API_PORT/docs)"
    log "Ctrl-C stops both."
    printf '\n'
    supervise
}

mode_test() {
    local rc=0 py_rc=0 ts_rc=0
    ensure_python_deps
    printf '\n'
    log "${C_BOLD}pytest${C_RESET} (backend)"
    if ( cd "$BACKEND_DIR" && "$VENV_PY" -m pytest -q ); then
        ok "pytest passed."
    else
        py_rc=$?
        err "pytest failed (exit $py_rc)."
    fi

    printf '\n'
    if [ -f "$FRONTEND_DIR/package.json" ]; then
        log "${C_BOLD}tsc --noEmit${C_RESET} (frontend)"
        ensure_frontend_deps
        # Prefer the frontend's own `typecheck` script so this stays in step
        # with whatever tsconfig layout it settles on; fall back to plain tsc.
        if grep -q '"typecheck"' "$FRONTEND_DIR/package.json"; then
            ( cd "$FRONTEND_DIR" && npm run --silent typecheck ) || ts_rc=$?
        else
            ( cd "$FRONTEND_DIR" && npx --no-install tsc --noEmit ) || ts_rc=$?
        fi
        if [ "$ts_rc" = 0 ]; then
            ok "tsc --noEmit passed - no type errors."
        else
            err "tsc --noEmit failed (exit $ts_rc)."
        fi
    else
        warn "no frontend/package.json - skipping the TypeScript check."
    fi

    printf '\n'
    [ "$py_rc" != 0 ] && rc=1
    [ "$ts_rc" != 0 ] && rc=1
    if [ "$rc" = 0 ]; then
        ok "all checks passed."
    else
        err "some checks failed (see above)."
    fi
    return "$rc"
}

usage() {
    sed -n '3,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

main() {
    # Before any mode, so the backend, the frontend and ./run.sh test all
    # inherit what .env provides. Harmless when there is no .env.
    case "${1:-all}" in
        help|-h|--help) : ;;
        *) load_env_file ;;
    esac
    case "${1:-all}" in
        all|both|"")   mode_all ;;
        backend|api)   mode_backend ;;
        frontend|web|ui) mode_frontend ;;
        test|tests|check) mode_test ;;
        help|-h|--help) usage ;;
        *) err "unknown argument: $1"; printf '\n' >&2; usage >&2; exit 2 ;;
    esac
}

main "$@"
