# Agentic Backend

Flask backend for the refactored agent runtime. It exposes authenticated REST endpoints, streams `agent.v2` SSE events, persists chat/execution state per user, and drives a tool-using agent loop.

Runtime ontology and skill assets now live under the `consumer/` application root `.onto-runtime/` directory:

```text
<consumer>/.onto-runtime/
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
- Provider schemas and one workspace-local private business data source per ontology
- Agent-to-Agent (A2A) and ACP protocol adapters over the same orchestrator/runtime
- Local performance analysis plus optional observability export through system config
- Skill ontology parser pipeline (`skill_parser/`)
- Runtime system config API
- Workspace-scoped resource management

## Project layout

```text
agentic/
├── app.py                    # Flask app factory, auth/login routes, health
├── orchestrator.py           # Run lifecycle, persistence, cancellation
├── exceptions.py             # Shared exception types
├── workspaces.py             # Workspace resolution helpers
├── loop/
│   ├── jiuwen_adapter.py     # Jiuwen-backed main agent loop
│   ├── jiuwen/               # Jiuwen support modules
│   └── shared/               # Loop helpers shared by Jiuwen runtime code
├── a2a/                      # Agent-to-Agent protocol
│   ├── mapper.py             # A2A ↔ internal format mapping
│   ├── models.py             # A2A data models
│   └── service.py            # A2A service layer
├── acp/                      # ACP protocol adapter and config
├── business_data/            # Ontology-scoped data sources and provider adapters
├── direct_execution/         # Script direct-execution guards and scoped runtime
├── evolution/                # Operation evolution proposal/review services
├── observability/            # Runtime event bus and exporters
├── perf_analysis/            # Local performance JSONL analysis
├── routes/
│   ├── workflows.py          # /agentic/runs/*
│   ├── sessions.py           # /agentic/sessions/*
│   ├── ontologies.py         # /agentic/ontologies/*
│   ├── skills.py             # /agentic/skills/*
│   ├── business_data.py      # ontology data-source API and legacy retirement responses
│   ├── operations.py         # /agentic/operations/*
│   ├── performance.py        # /agentic/*/performance and /api/local-observability/*
│   ├── workspaces.py         # /agentic/workspaces/*
│   ├── a2a.py                # /a2a/* (Agent-to-Agent)
│   └── system_config.py      # /api/system-config*
├── tools/
│   ├── registry.py           # Tool registry and schema lookup
│   ├── executor.py           # Tool batch execution
│   ├── base.py               # ToolSpec base definitions
│   ├── events.py             # Tool event emission helpers
│   ├── business_data_search.py
│   ├── ontology_context.py
│   ├── skill.py
│   ├── memory.py
│   ├── spawn_sub_agent.py
│   └── execute_script.py
├── sub_agents/
│   ├── base.py               # SubAgent class
│   └── manager.py            # SubAgentManager (fan-out/fan-in)
├── memory/
│   ├── factory.py            # Memory backend factory
│   ├── manager.py            # OpenViking memory manager
│   ├── filesystem_manager.py # Filesystem-backed memory manager
│   ├── models.py             # Memory data models
│   ├── protocol.py           # Memory manager protocol/interface
│   └── utils.py              # Memory utilities
├── models/
│   ├── state.py              # AgenticState TypedDict
│   └── events.py             # SSE event models
├── stores/
│   ├── chat_store.py         # Per-user chat session persistence
│   ├── execution_store.py    # Per-user turn-level LLM context
│   ├── run_store.py          # In-memory run lifecycle tracking
│   └── user_store_manager.py # Per-user store instantiation
├── services/
│   ├── auth.py               # JWT auth and role checking
│   ├── llm/                  # OpenAI-compatible LLM client and router
│   ├── data_provider_client.py
│   ├── mindsdb.py            # MindsDB HTTP client
│   └── nl2sql.py             # NL2SQL HTTP client
├── prompts/
│   └── universal.py          # System prompt builder
├── config/
│   ├── settings.py           # LLM, App, Agent, RAG configs
│   └── system_config.py      # Runtime system config API
└── utils/
    ├── logging.py
    ├── streaming.py
    ├── skills.py
    ├── ontologies.py
    ├── context_window.py     # Sliding context window logic
    ├── json_utils.py         # JSON serialization helpers
    ├── llm_request_mode.py   # LLM request mode selection
    ├── read_docs.py          # Document reading utilities
    └── reasoning_summary.py  # Reasoning summary extraction

skill_parser/                 # Ontology skill file parser (top-level module)
├── constants.py
├── fs_utils.py
├── mapping.py
├── markdown_utils.py
├── modeling_template.py
├── models.py
├── pipeline.py
└── skill_ontology_parser.py

tests/                        # pytest regression suite for routes, tools,
                              # protocol adapters, observability, business data,
                              # evolution, runtime config, and orchestration
```

## Run locally

Run from the `backend/` directory:

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

- Authentication is provided solely by the standalone auth-server. `AUTH_SERVER_URL` is required (for example `http://auth-server:5056` in Docker, or `http://localhost:5056` for a local auth-server); the backend fails to start when it is unset.
- `PROJECT_ROOT` defaults to `<consumer>/.onto-runtime`, which is now the runtime root containing `system/` and `workspaces/`.
- `system_config.json` is read only from `<consumer>/.onto-runtime/config/system_config.json`; when absent, environment variables and built-in defaults apply until the first save creates it.
- Ontologies are resolved per workspace under `<consumer>/.onto-runtime/workspaces/<workspace_id>/ontologies`.
- Skills are resolved per workspace under `<consumer>/.onto-runtime/workspaces/<workspace_id>/skills`, then fall back to `<consumer>/.onto-runtime/system/skills`.
- Server host/port/debug flags live in [`server_config.py`](server_config.py).

### MCP server

The MCP service runs as a separate FastMCP process. Start it from `consumer/backend` after installing dependencies:

```bash
PYTHONPATH=. python -m api.protocols.mcp.server --transport both --host 0.0.0.0 --port 7488
```

Docker Compose deployment starts a separate `mcp` container that reuses the backend image and `.onto-runtime` mount. The Flask app exposes MCP management APIs under `/agentic/mcp/*`. Each workspace has one persistent random `mcp_endpoint_id`, and the standalone process exposes `/{mcp_endpoint_id}/search/mcp`, `/{mcp_endpoint_id}/chat/mcp`, `/{mcp_endpoint_id}/search/sse`, and `/{mcp_endpoint_id}/chat/sse`. Get the complete URLs from `GET /agentic/mcp/runtime/status`; the old fixed routes are not available. Search tools require an explicit `ontology_id` and execute graph tools without LLM calls. Chat revalidates the MCP token, workspace ACL, and the current request URL endpoint binding before entering the canonical Jiuwen workflow and admission queue. Each MCP transport session is pinned to its first endpoint and cannot be reused across workspace URLs; clients must initialize a new session after the MCP service restarts.

## Tests

```bash
python -m pytest tests
```

Run focused test files for targeted backend changes, and run the full suite before release or merge.

## Configuration

Copy [`.env.example`](.env.example) to `.env`.

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
| `SYSTEM_CONFIG_PATH` | Optional saved system config file path | `<consumer>/.onto-runtime/config/system_config.json` |
| `CHAT_STORAGE_DIR` | Session JSON storage; release defaults resolve to `<PROJECT_ROOT>/data/chats` | `<PROJECT_ROOT>/data/chats` |
| `EXECUTION_STORAGE_DIR` | Execution records; release defaults resolve to `<PROJECT_ROOT>/data/executions` | `<PROJECT_ROOT>/data/executions` |
| `PROJECT_ROOT` | Runtime root containing `system/` and `workspaces/` | `<consumer>/.onto-runtime` |
| `AUTH_SERVER_URL` | Standalone auth-server base URL (required; startup fails when unset), e.g. `http://auth-server:5056` | empty |
| `AUTH_SERVER_ADMIN_TOKEN` | Shared admin token for the auth-server Admin API (required for user management) | empty |
| `AUTH_SERVER_AUDIENCE` | Expected JWT audience for local token verification | `meta-wise` |
| `AUTH_SERVER_ISSUER` | Expected JWT issuer; empty means `AUTH_SERVER_URL` is used | empty |
| `AUTH_USERS_FILE` | User credential file | `<consumer>/.onto-runtime/users.json` |
| `AUTH_SECRET` | JWT signing secret | `dev-secret-change` fallback if unset |
| `AUTH_TOKEN_TTL` | JWT TTL in seconds | `43200` |
| `MCP_SERVER_HOST` | Standalone MCP server host; Docker Compose overrides this to `mcp` in the Flask container for internal probing and to `0.0.0.0` in the MCP container for binding | `0.0.0.0` |
| `MCP_SERVER_PORT` | Standalone MCP server port; Docker Compose maps host `${MCP_SERVER_PORT:-7488}` to container `7488` | `7488` |
| `MCP_PUBLIC_BASE_URL` | Externally reachable protocol, host, port, and optional gateway prefix. Workspace endpoint IDs and service paths are appended automatically | derived from host/port |
| `MCP_TRANSPORT` | `both` exposes Streamable HTTP and SSE; a single transport may be selected explicitly | `both` |
| `MCP_SERVICES_CONFIG_FILE` | Optional YAML service catalog path; relative paths resolve from `consumer/backend` | `config/mcp_services.yaml` when present |
| `MCP_TOKEN_SECRET` | MCP JWT signing secret (required, at least 32 characters; do not reuse another service secret) | no default |
| `MCP_TOKEN_ISSUER` | MCP JWT `iss` claim | `meta-wise` |
| `MCP_TOKEN_AUDIENCE` | MCP JWT `aud` claim | `meta-wise-mcp` |
| `MCP_TOKEN_TTL_SECONDS` | Default MCP token TTL | `86400` |
| `MCP_TOKEN_STORE_PATH` | MCP revocation registry file | `<PROJECT_ROOT>/mcp-tokens.json` |
| `AGENT_MAIN_LOOP_MAX_ITERATIONS` | Max main-loop iterations | `50` |
| `AGENT_SUB_AGENT_MAX_ITERATIONS` | Max sub-agent iterations | `20` |
| `AGENT_SUB_AGENT_MAX_CONCURRENCY` | Max concurrent sub-agents per run | `6` |
| `AGENT_SUB_AGENT_MAX_DEPTH` | Max nested sub-agent depth | `10` |
| `AGENT_PARALLEL_DEFAULT_MAX_WORKERS` | Parallel regular-tool workers | `10` |
| `AGENT_CONTEXT_MAX_ROUNDS` | Sliding context rounds kept | `10` |
| `AGENT_LLM_CONNECTION_MAX_RETRIES` | LLM HTTP retry count for connection-style failures | `3` |
| `LOG_LEVEL` | Process startup log level; overridden by `ops_config.json` `logging.level` | `INFO` |
| `LOG_CONSOLE` | Enable console logging when set | off |
| `SPEEDSCOPE_TRACE_ENABLED` | Generate developer-only, session-scoped Speedscope timing files | `false` |

Notes:

- Runtime log-level changes use `<consumer>/.onto-runtime/config/ops_config.json` (or `OPS_CONFIG_PATH`): `{"logging":{"level":"DEBUG"}}`. Each backend and MCP process checks the file metadata on the next logging call and reloads it only after a change; no restart is required. Invalid JSON or levels preserve the last valid value. Use an atomic file replacement when possible.
- The backend no longer ships with a built-in default LLM endpoint or model id. You must configure `LLM_BASE_URL` and `LLM_MODEL`, or save an enabled model profile in system config before chat can run.
- The main ReAct loop runs on openJiuwen. Install the backend dependencies from `backend/requirements.txt`.
- `business_data_search` resolves one private source by `workspace_id + ontology_id`. Save `provider + config` through `/agentic/ontologies/{ontology_id}/data-source`; credentials remain in workspace runtime storage, outside the ontology package.
- `web_search` is optional and disabled by default. Configure `web_search` through system settings or `WEB_SEARCH_*` environment variables before the tool is exposed to the model.
- Observability output is configured per workspace through `/agentic/workspaces/<workspace_id>/observability-config` or the configuration center's workspace observability section. Each workspace can independently select local performance JSONL or its own Langfuse project. Global system config no longer exposes an `observability` section, and the old `LANGFUSE_*` and `OBSERVABILITY_*` environment variables are not read.
- When a workspace uses `mode=local`, the chat UI can analyze that workspace's current session. Admins can also aggregate and filter local observations across workspaces, but that dashboard is an analysis view rather than global observability configuration. Neither view reads Langfuse data or old direct perf logger records.
- `SPEEDSCOPE_TRACE_ENABLED=true` is a developer diagnostic switch independent of workspace observability configuration. The knowledge-QA backend writes standard session-scoped `.speedscope.json` files under `<PROJECT_ROOT>/logs/speedscope/`, retained for 30 days. Runs and parallel execution lanes become evented profiles that can be opened directly in [Speedscope](https://www.speedscope.app/); these files are not consumed by the local-observability UI. Normal `agent.log` output remains enabled, while `agent_perf` is produced only for workspaces configured with `mode=local` and follows each workspace's retention setting.
- Speedscope traces contain stage names, status, and timing only. They exclude user questions, prompts, reasoning, answers, tool arguments, SQL parameters, and tool output. The standalone MCP container explicitly disables this switch.
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
- `GET/PUT/DELETE /agentic/ontologies/<ontology_id>/data-source`
- `POST /agentic/ontologies/<ontology_id>/data-source/test`
- `GET /agentic/business-data/provider-schemas`
- `GET/POST/PATCH /agentic/operations...`
- `GET/POST/DELETE /agentic/workspaces...`
- `GET/PATCH /agentic/workspaces/<workspace_id>/runtime-config`
- `GET/PATCH/POST /agentic/workspaces/<workspace_id>/observability-config...`
- `GET/PATCH/POST /api/system-config...`
- `GET /.well-known/a2a/agent-card.json`
- `GET/POST/DELETE /a2a/tasks...` (Agent-to-Agent protocol)
- `GET /acp/agent` (ACP discovery)
- `GET/POST/DELETE /acp/runs...` (ACP protocol)

Full request/response examples live in [`docs/api.md`](../docs/api.md).

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

- Full stack: root [`docker-compose.yml`](../docker-compose.yml)
- Backend only: [`backend/docker-compose.yml`](docker-compose.yml)

The backend container listens on port `5055`.
