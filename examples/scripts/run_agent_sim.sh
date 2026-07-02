#!/usr/bin/env bash
set -euo pipefail

# Edit this file, then run:
#   bash examples/scripts/run_agent_sim.sh

QUESTION="你的测试问题写在这里"

# Runtime identity.
WORKSPACE_ID="default"
USER_ID="sim"
RUN_ID=""
SESSION_ID=""
OUTPUT_DIR=""

# Agent behavior.
AGENT_MODE="quick"          # quick | thinking | multi_agent
LLM_REQUEST_MODE="sync"    # sync | stream
SELECTED_ONTOLOGY_ID=""

# Leave empty to use the default saved model profile.
MODEL_PROFILE_ID=""

# Or use a transient model config for this run. Leave these empty when using MODEL_PROFILE_ID.
BASE_URL=""
MODEL=""
API_KEY_ENV="LLM_API_KEY"
PROVIDER="openai_compatible"
TEMPERATURE="0.2"
MAX_TOKENS=""
TIMEOUT="60"

# Extra context merged into user_context. Keep as a JSON object.
CONTEXT_JSON='{}'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/venv/bin/python"
elif [[ -x "/Users/example/vscodePo/onto-agent/venv/bin/python" ]]; then
  PYTHON_BIN="/Users/example/vscodePo/onto-agent/venv/bin/python"
else
  PYTHON_BIN="python3"
fi

ARGS=(start "$QUESTION")
ARGS+=(--workspace-id "$WORKSPACE_ID")
ARGS+=(--user-id "$USER_ID")
ARGS+=(--agent-mode "$AGENT_MODE")
ARGS+=(--llm-request-mode "$LLM_REQUEST_MODE")
ARGS+=(--context-json "$CONTEXT_JSON")

if [[ -n "$RUN_ID" ]]; then
  ARGS+=(--run-id "$RUN_ID")
fi
if [[ -n "$SESSION_ID" ]]; then
  ARGS+=(--session-id "$SESSION_ID")
fi
if [[ -n "$OUTPUT_DIR" ]]; then
  ARGS+=(--output-dir "$OUTPUT_DIR")
fi
if [[ -n "$SELECTED_ONTOLOGY_ID" ]]; then
  ARGS+=(--selected-ontology-id "$SELECTED_ONTOLOGY_ID")
fi

if [[ -n "$MODEL_PROFILE_ID" ]]; then
  ARGS+=(--model-profile-id "$MODEL_PROFILE_ID")
elif [[ -n "$BASE_URL" || -n "$MODEL" ]]; then
  ARGS+=(--base-url "$BASE_URL")
  ARGS+=(--model "$MODEL")
  ARGS+=(--provider "$PROVIDER")
  if [[ -n "$API_KEY_ENV" ]]; then
    ARGS+=(--api-key-env "$API_KEY_ENV")
  fi
  if [[ -n "$TEMPERATURE" ]]; then
    ARGS+=(--temperature "$TEMPERATURE")
  fi
  if [[ -n "$MAX_TOKENS" ]]; then
    ARGS+=(--max-tokens "$MAX_TOKENS")
  fi
  if [[ -n "$TIMEOUT" ]]; then
    ARGS+=(--timeout "$TIMEOUT")
  fi
fi

PYTHONPATH="$ROOT_DIR/platform/server:$ROOT_DIR/modeling/skills:$ROOT_DIR/examples/scripts${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON_BIN" examples/scripts/agent_sim.py "${ARGS[@]}"
