#!/usr/bin/env bash
#
# One-command bring-up for the STM32 AI assistant.
#
#   ./run.sh                 first run or every run -- it is idempotent
#   ./run.sh --no-kb         skip the PageVault knowledge base
#   ./run.sh --down          stop this stack
#   ./run.sh --status        show what is running and where
#
# It does the four things `docker compose up` alone does not, and which the
# README leaves you to discover by failure:
#
#   1. creates the external `rag-net` network (compose hard-fails without it)
#   2. builds the ARM toolchain image first, because backend `depends_on` it
#      being *healthy* -- ~1 GB, the only step that needs internet
#   3. imports the vendor pin tables (`build_devices`), which the README omits
#      entirely and without which pin/AF validation has no data
#   4. checks the two settings that silently break the UI when ports move:
#      NEXT_PUBLIC_API_URL and CORS_ORIGINS
#
set -Eeuo pipefail

cd "$(dirname "$(readlink -f "$0")")"

ENV_FILE=.env
WITH_KB=1
ACTION=up

RED=''; YLW=''; GRN=''; DIM=''; BLD=''; OFF=''
if [[ -t 1 ]]; then
  RED=$'\033[31m'; YLW=$'\033[33m'; GRN=$'\033[32m'
  DIM=$'\033[2m'; BLD=$'\033[1m'; OFF=$'\033[0m'
fi

step() { printf '\n%s==>%s %s%s\n' "$BLD" "$OFF" "$BLD" "$1$OFF"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '%s[warn]%s %s\n' "$YLW" "$OFF" "$1" >&2; }
ok()   { printf '    %s%s%s\n' "$GRN" "$1" "$OFF"; }
die()  { printf '\n%s[error]%s %s\n' "$RED" "$OFF" "$1" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case $1 in
    --no-kb)   WITH_KB=0 ;;
    --with-kb) WITH_KB=1 ;;
    --down)    ACTION=down ;;
    --status)  ACTION=status ;;
    -h|--help) sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------- preflight
command -v docker >/dev/null 2>&1 || die "docker is not installed."
docker compose version >/dev/null 2>&1 ||
  die "the docker compose v2 plugin is missing (\`docker compose version\` failed)."
docker info >/dev/null 2>&1 ||
  die "cannot talk to the docker daemon. Is it running, and is your user in the \`docker\` group?"

# ------------------------------------------------------------------- config
# Reads one key out of .env. Last occurrence wins, matching compose. Tolerates
# CRLF and surrounding quotes; deliberately does NOT source the file, so a
# stray shell metacharacter in an API key cannot execute anything.
env_get() {
  local key=$1 default=${2-} val=''
  if [[ -f $ENV_FILE ]]; then
    val=$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=//p" "$ENV_FILE" | tail -n1)
    val=${val%$'\r'}
    val=${val#[\"\']}
    val=${val%[\"\']}
  fi
  printf '%s' "${val:-$default}"
}

if [[ ! -f $ENV_FILE ]]; then
  [[ -f .env.example ]] || die "neither .env nor .env.example is present -- wrong directory?"
  cp .env.example "$ENV_FILE"
  printf '\n%s created .env from .env.example.%s\n\n' "$BLD" "$OFF"
  info "Set LLM_API_KEY (and EMBEDDING_API_KEY) in it, then run ./run.sh again."
  exit 1
fi

FRONTEND_PORT=$(env_get FRONTEND_PORT 19300)
BACKEND_PORT=$(env_get BACKEND_PORT 19800)
PAGEVAULT_PORT=$(env_get PAGEVAULT_PORT 19100)
POSTGRES_PORT=$(env_get POSTGRES_PORT 19432)
REDIS_PORT=$(env_get REDIS_PORT 19379)
QDRANT_PORT=$(env_get QDRANT_PORT 19333)
QDRANT_GRPC_PORT=$(env_get QDRANT_GRPC_PORT 19334)
PAGEVAULT_DIR=$(env_get PAGEVAULT_DIR ../pagevault)

# A second STM32 checkout needs a second PageVault Compose project too. Without
# this explicit name, Compose derives `pagevault` from the checkout directory
# and the test run silently reuses the original PageVault containers. Preserve
# the historic name for the normal installation, but namespace PageVault when
# COMPOSE_PROJECT_NAME was supplied for an isolated test installation.
MAIN_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-$(env_get COMPOSE_PROJECT_NAME)}
if [[ -n ${PAGEVAULT_PROJECT_NAME:-} ]]; then
  PAGEVAULT_PROJECT_NAME=$PAGEVAULT_PROJECT_NAME
elif [[ -n $MAIN_PROJECT_NAME ]]; then
  PAGEVAULT_PROJECT_NAME="${MAIN_PROJECT_NAME}-pagevault"
else
  PAGEVAULT_PROJECT_NAME=pagevault
fi

# The PageVault stack is invoked with `-f ../pagevault/...`, which makes that
# directory compose's project directory -- so it reads PageVault's .env, not
# ours. Exporting is the only way our value reaches it.
export PAGEVAULT_PORT
export PAGEVAULT_PROJECT_NAME

case $ACTION in
  down)
    step "Stopping this stack"
    docker compose down
    info "PageVault, if running, is untouched. Stop it with: make kb-down"
    exit 0
    ;;
  status)
    docker compose ps
    printf '\nDashboard  http://localhost:%s\nAPI        http://localhost:%s\n' \
      "$FRONTEND_PORT" "$BACKEND_PORT"
    exit 0
    ;;
esac

# ------------------------------------------------------------ config checks
step "Checking configuration"

key=$(env_get LLM_API_KEY)
case $key in
  ''|sk-REPLACE_ME) die "LLM_API_KEY is still unset/placeholder in .env. The stack will start but every agent call will fail." ;;
  *) ok "LLM_API_KEY is set" ;;
esac

[[ $(env_get EMBEDDING_API_KEY) == sk-REPLACE_ME ]] &&
  warn "EMBEDDING_API_KEY is still the placeholder; ingestion/embedding calls will fail."

# NEXT_PUBLIC_API_URL is compiled into the browser bundle. If it points at a
# port the backend no longer listens on, every page loads and every request
# fails with a bare "Failed to fetch" -- the single most confusing failure here.
api_url=$(env_get NEXT_PUBLIC_API_URL)
if [[ -n $api_url ]]; then
  url_port=${api_url##*:}
  url_port=${url_port%%/*}
  if [[ $url_port != "$BACKEND_PORT" ]]; then
    warn "NEXT_PUBLIC_API_URL is ${api_url} but the backend is published on ${BACKEND_PORT}."
    warn "  The UI will load and then fail every request. Fix in .env, either:"
    warn "    NEXT_PUBLIC_API_URL=http://localhost:${BACKEND_PORT}"
    warn "  or comment the line out to let it follow BACKEND_PORT automatically."
  else
    ok "NEXT_PUBLIC_API_URL matches BACKEND_PORT"
  fi
  case $api_url in
    *localhost*|*127.0.0.1*)
      info "${DIM}NEXT_PUBLIC_API_URL is localhost: the UI will only work in a browser"
      info "on this machine. For remote access set it to this host's address.${OFF}" ;;
  esac
else
  ok "NEXT_PUBLIC_API_URL unset -- follows BACKEND_PORT (${BACKEND_PORT})"
fi

# pydantic-settings parses this as JSON. A comma-separated list raises
# SettingsError and the backend never starts.
cors=$(env_get CORS_ORIGINS)
if [[ -n $cors ]]; then
  [[ $cors == \[* ]] ||
    die "CORS_ORIGINS must be a JSON array, e.g. [\"http://localhost:${FRONTEND_PORT}\"] -- a bare/comma-separated value makes the backend exit at startup."
  [[ $cors == *:"$FRONTEND_PORT"* ]] ||
    warn "CORS_ORIGINS does not mention port ${FRONTEND_PORT}; the browser will block API calls."
fi

# A stale bare-hostname DSN resolves to PageVault's database on rag-net.
[[ $(env_get DATABASE_URL) == *stm32-postgres* ]] ||
  warn "DATABASE_URL does not use the stm32-postgres alias. On rag-net the bare name \`postgres\` is PageVault's database."
[[ $(env_get REDIS_URL) == *stm32-redis* ]] ||
  warn "REDIS_URL does not use the stm32-redis alias. On rag-net the bare name \`redis\` is PageVault's, and the worker will steal its tasks."

# PageVault .env check (when knowledge base is enabled)
if ((WITH_KB == 1)) && [[ -d $PAGEVAULT_DIR && -f $PAGEVAULT_DIR/docker-compose.yml ]]; then
  pv_env="$PAGEVAULT_DIR/.env"
  if [[ ! -f $pv_env ]]; then
    [[ -f $PAGEVAULT_DIR/.env.example ]] || die "PageVault at ${PAGEVAULT_DIR} has no .env and no .env.example."
    cp "$PAGEVAULT_DIR/.env.example" "$pv_env"
    if [[ -f $PAGEVAULT_DIR/.env.textrag.example ]]; then
      cat "$PAGEVAULT_DIR/.env.textrag.example" >> "$pv_env"
    fi
    printf '\n%s created %s from .env.example and .env.textrag.example.%s\n\n' "$BLD" "$pv_env" "$OFF"
    info "Review PageVault settings in ${pv_env}, then run ./run.sh again."
    exit 1
  fi
  if [[ -f $PAGEVAULT_DIR/docker-compose.textrag.yml ]] && ! grep -q "TEXTRAG_" "$pv_env"; then
    if [[ -f $PAGEVAULT_DIR/.env.textrag.example ]]; then
      cat "$PAGEVAULT_DIR/.env.textrag.example" >> "$pv_env"
      printf '\n%s appended .env.textrag.example to %s.%s\n\n' "$BLD" "$pv_env" "$OFF"
      info "Review PageVault settings in ${pv_env}, then run ./run.sh again."
      exit 1
    else
      die "${pv_env} is missing TEXTRAG_* settings required by docker-compose.textrag.yml."
    fi
  fi
  ok "PageVault .env is present"
fi

# ---------------------------------------------------------------- port scan
# Only meaningful before the stack exists; once it is up these are held by our
# own containers.
if [[ -z $(docker compose ps -q 2>/dev/null) ]] && command -v ss >/dev/null 2>&1; then
  listening=$(ss -Hltn 2>/dev/null | awk '{print $4}' | sed 's/.*://' || true)
  clash=()
  for p in "$FRONTEND_PORT" "$BACKEND_PORT" "$POSTGRES_PORT" "$REDIS_PORT" \
           "$QDRANT_PORT" "$QDRANT_GRPC_PORT"; do
    grep -qx "$p" <<<"$listening" && clash+=("$p")
  done
  ((${#clash[@]})) &&
    die "these host ports are already in use: ${clash[*]}. Change them in .env (see the 'Host ports' block)."
  ok "host ports free: $FRONTEND_PORT $BACKEND_PORT $POSTGRES_PORT $REDIS_PORT $QDRANT_PORT $QDRANT_GRPC_PORT"
fi

# ------------------------------------------------------------------ network
step "Shared network"
if docker network inspect rag-net >/dev/null 2>&1; then
  ok "rag-net exists"
else
  docker network create rag-net >/dev/null
  ok "rag-net created"
fi

# --------------------------------------------------------------- PageVault
step "Knowledge base (PageVault)"
KB_UP=0
if ((WITH_KB == 0)); then
  info "skipped (--no-kb)"
elif [[ -d $PAGEVAULT_DIR && -f $PAGEVAULT_DIR/docker-compose.yml ]]; then
  if docker compose \
      -p "$PAGEVAULT_PROJECT_NAME" \
      -f "$PAGEVAULT_DIR/docker-compose.yml" \
      -f "$PAGEVAULT_DIR/docker-compose.textrag.yml" \
      -f "$PWD/deploy/pagevault-rag.override.yml" \
      up -d; then
    KB_UP=1
    ok "PageVault is up on port ${PAGEVAULT_PORT}"
  else
    die "PageVault failed to start. Fix the error above, or run with --no-kb to start without knowledge base."
  fi
else
  warn "no PageVault checkout at ${PAGEVAULT_DIR}, so there is no knowledge base."
  warn "  Chat and RAG still work, but answers are unverified and uncited. To add it:"
  warn "    git clone https://github.com/mjavadhm/pagevault ${PAGEVAULT_DIR}"
fi

# --------------------------------------------------------- toolchain image
step "Build sandbox image"
info "${DIM}arm-none-eabi + ST HAL/CMSIS, ~1 GB. First run needs internet and can"
info "take 10-25 minutes; afterwards this is a cached no-op.${OFF}"
docker compose build builder
ok "builder image ready"

# ------------------------------------------------------------------ the app
step "Starting services"
info "${DIM}backend waits for the builder to report healthy before it starts.${OFF}"
docker compose up -d --build

# ------------------------------------------------------------ health gate
step "Waiting for the backend"
health=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "http://localhost:${BACKEND_PORT}/health" >/dev/null 2>&1; then
    health=1; break
  fi
  sleep 2
done
if ((health)); then
  ok "backend is healthy on ${BACKEND_PORT}"
else
  warn "backend did not answer /health within 120s. Recent logs:"
  docker compose logs --tail 30 backend >&2 || true
  die "startup failed. Full logs: docker compose logs -f backend"
fi

# -------------------------------------------------------------- pin tables
# Not in the README; only docs/m4-plan.md mentions it. Without it the planner
# has no alternate-function data and pin validation silently has nothing to
# validate against.
step "Vendor pin tables"
if docker compose exec -T backend python -c '
import sys
from app.codegen import devicedata
from app.codegen.devices import DEVICES
sys.exit(0 if set(DEVICES) <= set(devicedata.available()) else 1)
' >/dev/null 2>&1; then
  ok "already imported"
else
  info "importing (one-off, offline)..."
  if docker compose exec -T backend python -m scripts.build_devices; then
    ok "pin tables imported"
  else
    warn "build_devices failed -- codegen will run without pin/AF validation."
    warn "  Usually means the cube_sdk volume was filled by an older image: make sdk-refresh"
  fi
fi

# Proves the key and base URL actually work, which /health does not.
#
# The budget has to follow LLM_TIMEOUT_SECONDS rather than be a fixed small
# number: on a reasoning model (glm, o1/o3, deepseek-r) most of the latency is
# spent before the first token, and the *first* call of a fresh container also
# pays client construction, DNS and TLS. A 25s ceiling here reported a healthy
# provider as broken.
#
# Non-fatal either way: a rate-limited free tier is not a misconfiguration.
step "LLM provider"
llm_budget=$(env_get LLM_TIMEOUT_SECONDS 120)
[[ $llm_budget =~ ^[0-9]+$ ]] || llm_budget=120
info "${DIM}first call is cold; allowing $((llm_budget + 15))s${OFF}"
if llm_body=$(curl -fsS --max-time "$((llm_budget + 15))" \
                "http://localhost:${BACKEND_PORT}/health/llm" 2>&1); then
  llm_model=$(printf '%s' "$llm_body" | sed -n 's/.*"model":"\([^"]*\)".*/\1/p')
  ok "provider reachable${llm_model:+ (${llm_model})}"
else
  warn "GET /health/llm did not succeed -- check LLM_BASE_URL / LLM_API_KEY / LLM_MODEL."
  warn "  This does not stop the stack; retry with:"
  warn "    curl http://localhost:${BACKEND_PORT}/health/llm"
fi

# ---------------------------------------------------------------- all done
printf '\n%s  Ready%s\n\n' "$BLD$GRN" "$OFF"
printf '  Dashboard      http://localhost:%s\n' "$FRONTEND_PORT"
printf '  API            http://localhost:%s\n' "$BACKEND_PORT"
printf '  API health     http://localhost:%s/health\n' "$BACKEND_PORT"
((KB_UP)) && printf '  PageVault API  http://localhost:%s\n' "$PAGEVAULT_PORT"
printf '\n%s  logs: docker compose logs -f backend   stop: ./run.sh --down%s\n\n' "$DIM" "$OFF"
