# Agentic Backend

Flask backend for the refactored agent runtime. It exposes authenticated REST endpoints, streams `agent.v2` SSE events, persists chat/execution state per user, and drives a tool-using agent loop.

Runtime ontology and skill assets now live under the repository-root `.onto-runtime/` directory:

```text
<repo>/.onto-runtime/
├── config/
│   └── system_config.json
├── system/
│   └── skills/
├── workspaces/
│   └── <workspace_id>/
│       ├── metadata.json
│       ├── runtime_config.json
│       ├── ontologies/
│       ├── skills/
│       ├── files/
│       └── runtime/
├── users.json
└── workspace-settings.json
```

## Current capabilities

- Jiuwen-backed main ReAct loop through `agentic/loop/jiuwen_adapter.py`
- Registry-based tool execution with barriered parallel batches and phased `context -> search -> execute` ordering
- Stream-closed tool execution for fully assembled streamed tool calls
- Structured `read_documents` ledger backing ontology-search prerequisites
- Ontology spec loading via `ontology_context`, skill guidance loading via `skill`, business data lookup via `business_data_search`, optional public web lookup via `web_search`, and script-backed operation execution via `execute_script`
- Unified `memory` tool for explicit user-provided memory capture, with filesystem/OpenViking memory backends and operation-evolution routing
- Homogeneous sub-agents via `spawn_sub_agent`
- Skill CRUD, ontology import/file APIs, operation evolution review, and workspace runtime config APIs
- Workspace-scoped business data connections, provider schemas, and ontology data bindings
- Agent-to-Agent (A2A) and ACP protocol adapters over the same orchestrator/runtime
- Local performance analysis plus optional observability export through system config
- Skill ontology parser pipeline (`skill_parser/`)
- Runtime system config API
- Workspace-scoped resource management

## Project Layout

Strategy A keeps the Python package name as `agentic`, while the source is split by domain. `platform/server/agentic/__init__.py` extends the package search path across the runtime and platform modules.

```text
platform/server/
├── agentic/                  # Flask app factory and HTTP route package anchor
├── run.py                    # Local Flask entry point
├── wsgi.py                   # Gunicorn/production entry point
├── server_config.py          # Host, port, debug, request limits
├── requirements.txt
└── Dockerfile

runtime/
├── agent/agentic/            # Orchestrator, Jiuwen loop, prompts, services, sub-agents
├── tools/agentic/            # Tool registry, execution, tool events
├── data_search/agentic/      # Business data search, MindsDB, NL2SQL, graph providers
├── direct_execution/agentic/ # Script operation direct execution
├── memory/agentic/           # Memory and operation evolution runtime
└── web_search/agentic/       # External web search providers

platform/
├── config/agentic/           # System config and ops config
├── workspace/agentic/        # Workspace runtime path management
├── auth/agentic/             # Users, auth, secret handling
├── stores/agentic/           # ChatStore, RunStore, ExecutionStore
├── protocols/agentic/        # ACP and A2A adapters
└── observability/agentic/    # Runtime event bus, exporters, local perf analysis

modeling/skills/skill_parser/ # Ontology skill file parser
tests/backend/                # pytest regression suite
```

## Run locally

Run from the `platform/server/` directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest

cp .env.example .env
python run.py
```

Default server address: `http://localhost:5055`

Notes:

- `AUTH_USERS_FILE` defaults to `<repo>/.onto-runtime/users.json`. If the file does not exist, the backend bootstraps default users automatically.
- `PROJECT_ROOT` defaults to `<repo>/.onto-runtime`, which is now the runtime root containing `system/` and `workspaces/`.
- `system_config.json` defaults to `<repo>/.onto-runtime/config/system_config.json`; legacy `<repo>/backend/config/system_config.json` is migrated when the new file does not exist.
- Ontologies are resolved per workspace under `<repo>/.onto-runtime/workspaces/<workspace_id>/ontologies`.
- Skills are resolved per workspace under `<repo>/.onto-runtime/workspaces/<workspace_id>/skills`, then fall back to `<repo>/.onto-runtime/system/skills`.
- Server host/port/debug flags live in [`server_config.py`](server_config.py).

## Tests

```bash
PYTHONPATH=.:../../modeling/skills python -m pytest ../../tests/backend
```

Run focused test files for targeted backend changes, and run the full suite before release or merge.

## Configuration

Copy [`platform/server/.env.example`](.env.example) to `.env`.

Important variables:

| Variable | Description | Default |
| --- | --- | --- |
| `LLM_BASE_URL` | Main OpenAI-compatible endpoint | empty |
| `LLM_MODEL` | Main model id | empty |
| `LLM_API_KEY` | Main model API key | none |
| `LLM_TIMEOUT` | Main model timeout in seconds | `600` |
| `LLM_MAX_TOKENS` | Main model max tokens | `4096` |
| `LLM_TEMPERATURE` | Main model temperature | `0.7` |
| `TASK_LLM_BASE_URL` | Optional lightweight endpoint for titles and reasoning summaries | falls back to `LLM_BASE_URL` |
| `TASK_LLM_MODEL` | Optional lightweight model id for titles and reasoning summaries | falls back to `LLM_MODEL` |
| `TASK_LLM_API_KEY` | Optional lightweight API key | falls back to `LLM_API_KEY` |
| `TASK_LLM_TIMEOUT` | Lightweight model timeout in seconds | `600` |
| `TASK_LLM_MAX_TOKENS` | Lightweight model max tokens | `4096` |
| `TASK_LLM_TEMPERATURE` | Lightweight model temperature | `0.7` |
| `SYSTEM_CONFIG_PATH` | Optional saved system config file path | `<repo>/.onto-runtime/config/system_config.json` |
| `CHAT_STORAGE_DIR` | Session JSON storage | `./data/chats` |
| `EXECUTION_STORAGE_DIR` | Execution records | `./data/executions` |
| `PROJECT_ROOT` | Runtime root containing `system/` and `workspaces/` | `<repo>/.onto-runtime` |
| `AUTH_USERS_FILE` | User credential file | `<repo>/.onto-runtime/users.json` |
| `AUTH_SECRET` | JWT signing secret | `dev-secret-change` fallback if unset |
| `AUTH_TOKEN_TTL` | JWT TTL in seconds | `43200` |
| `AGENT_MAIN_LOOP_MAX_ITERATIONS` | Max main-loop iterations | `50` |
| `AGENT_SUB_AGENT_MAX_ITERATIONS` | Max sub-agent iterations | `20` |
| `AGENT_SUB_AGENT_MAX_CONCURRENCY` | Max concurrent sub-agents per run | `6` |
| `AGENT_SUB_AGENT_MAX_DEPTH` | Max nested sub-agent depth | `10` |
| `AGENT_PARALLEL_DEFAULT_MAX_WORKERS` | Parallel regular-tool workers | `10` |
| `AGENT_CONTEXT_MAX_ROUNDS` | Sliding context rounds kept | `10` |
| `AGENT_CONTEXT_PRUNE_TOOLS` | Comma-separated tools to prune earlier | empty |
| `AGENT_LLM_CONNECTION_MAX_RETRIES` | LLM HTTP retry count for connection-style failures | `3` |
| `LOG_LEVEL` | Runtime log level | app default |
| `LOG_CONSOLE` | Enable console logging when set | off |

Notes:

- The backend no longer ships with a built-in default LLM endpoint or model id. You must configure `LLM_BASE_URL` and `LLM_MODEL`, or save an enabled model profile in system config before chat can run.
- The main ReAct loop runs on openJiuwen. Install the backend dependencies from `platform/server/requirements.txt`.
- `business_data_search` uses workspace-local data connections and explicit ontology bindings. Create a MindsDB or NL2SQL HTTP connection through `/agentic/data-connections`, then bind each ontology through `/agentic/ontologies/{ontology_id}/data-binding`.
- `web_search` is optional and disabled by default. Configure `web_search` through system settings or `WEB_SEARCH_*` environment variables before the tool is exposed to the model.
- Observability output is configured through `/api/system-config` or the system settings UI. `observability.enabled` controls whether events are exported and `observability.mode` selects local performance JSONL or Langfuse; the old `LANGFUSE_*` and `OBSERVABILITY_*` environment variables are not read.
- With `observability.mode=local`, the chat UI can analyze the current session and admins can open the global local-observability dashboard. Those views read only the current machine's retained new-schema local perf JSONL files; they do not read Langfuse data or old direct perf logger records.
- If you use GLM directly, the recommended endpoint example is `https://open.bigmodel.cn/api/paas/v4`.

## HTTP surface

Main route groups:

- `POST /auth/login`
- `GET /auth/me`
- `GET /health`
- `POST /agentic/runs/stream`
- `GET /agentic/runs/<run_id>/status`
- `POST /agentic/runs/<run_id>/cancel`
- `GET/POST/DELETE /agentic/sessions...`
- `GET /agentic/sessions/<session_id>/performance`
- `GET /agentic/performance/runs/<run_id>`
- `GET /api/local-observability/overview` (`admin`)
- `GET /api/local-observability/runs` (`admin`)
- `GET/POST/DELETE /agentic/skills...`
- `GET/PUT/POST /agentic/ontologies...`
- `GET/PUT/DELETE /agentic/ontologies/<ontology_id>/data-binding`
- `GET/POST/PATCH/DELETE /agentic/data-connections...`
- `GET/POST/PATCH /agentic/operations...`
- `GET/POST/DELETE /agentic/workspaces...`
- `GET/PATCH /agentic/workspaces/<workspace_id>/runtime-config`
- `GET/PATCH/POST /api/system-config...`
- `GET /.well-known/a2a/agent-card.json`
- `GET/POST/DELETE /a2a/tasks...` (Agent-to-Agent protocol)
- `GET /acp/agent` (ACP discovery)
- `GET/POST/DELETE /acp/runs...` (ACP protocol)

Full request/response examples live in [`docs/api.md`](../../docs/api.md).

## Stream protocol

Workflow streaming uses:

```text
event: agent.v2
data: { ... }
```

The JSON envelope uses protocol `agent-stream.v2`. Common event types:

- `turn.started`
- `token.delta`
- `reasoning.summary`
- `tool.started`
- `tool.progress`
- `tool.finished`
- `subagent.started`
- `subagent.delta`
- `subagent.progress`
- `subagent.finished`
- `subagent_group.started`
- `subagent_group.finished`
- `turn.finished`

Notes:

- `token.delta` uses `payload.channel = content | reasoning`; reasoning chunks include a stable `reasoning_block_id`.
- When upstream LLM streaming is enabled, a streamed tool call may execute as soon as it has a stable `tool_call.id`, valid JSON arguments, and the provider advances to a newer `tool_call.index` or emits `finish_reason`.
- After the assistant message is fully assembled, the loop only tail-executes tool calls that were not already executed during streaming.

## Docker

- Full stack: root [`docker-compose.yml`](../../docker-compose.yml)
- Backend only: [`platform/server/docker-compose.yml`](docker-compose.yml)

The backend container listens on port `5055`.
