# Agentic Frontend

React + Vite client for the refactored agent runtime. It renders authenticated chat sessions, streams `agent.v2` workflow events, and provides workspace configuration, ontology management, operation evolution, local observability, and task-process views.

## Current UI areas

- Chat workspace with incremental assistant/tool streaming
- Session sidebar with background-stream indicators
- Configuration center for system settings, workspaces, and workspace runtime config, plus ontology-scoped data-source editing in the knowledge workbench
- Ontology management and operation evolution review
- Task process sidebar built from streamed run/tool/sub-agent events
- Session performance and admin local-observability dashboards
- Admin management UI for users and skills
- Login flow backed by JWT auth

## Project layout

```text
src/
├── App.jsx
├── main.jsx
├── index.css
├── components/
│   ├── AgenticChat.jsx
│   ├── ChatInput.jsx
│   ├── ChatMessage.jsx
│   ├── ChatSidebar.jsx
│   ├── ConfigurationCenter.jsx
│   ├── DataConnectionManagement.jsx
│   ├── LocalObservabilityDashboard.jsx
│   ├── OntologyManagement.jsx
│   ├── OntologySelector.jsx
│   ├── OperationEvolution.jsx
│   ├── PerformanceRunDetail.jsx
│   ├── RunFeedback.jsx
│   ├── SessionPerformanceAnalysis.jsx
│   ├── SystemSettings.jsx
│   ├── TopBar.jsx
│   ├── UserManagement.jsx
│   ├── WorkspaceManagement.jsx
│   ├── WorkspaceRuntimeConfig.jsx
│   ├── LoginPage.jsx
│   └── TaskDAGSidebar.jsx
├── hooks/
│   ├── useAuth.js
│   ├── useAgenticStream.js
│   ├── useStreamManager.js
│   └── useChatSessions.js
└── utils/
    ├── auth.js
    ├── streamHelpers.js
    ├── messageBuilder.js
    ├── conversationTurns.js
    ├── segmentGroups.js
    ├── toolCallTracker.js
    ├── observabilityStatus.js
    ├── ontologyCommand.js
    ├── performanceMetrics.js
    ├── reasoningDetailGroups.js
    ├── nodeNames.js
    └── logger.js
```

## Run locally

Run from the `frontend/` directory:

```bash
npm install
npm run dev
```

Dev server address: `http://localhost:6600`

No frontend `.env` file is required for local development. The dev server uses Vite proxying and forwards same-origin paths such as `/auth`, `/agentic`, `/api`, and `/health` to `http://127.0.0.1:5055`.

## Environment

[`frontend/.env.example`](.env.example) is optional and only needed for extra client-side settings such as log level:

```bash
# Optional
# VITE_LOG_LEVEL=info
```

## Verification commands

```bash
npm run lint
npm run build
```

Current verification status:

- `npm run build` passes
- `npm run lint` passes
- Build still emits a Vite chunk-size warning for the main bundle (~650 kB minified JS)

## Runtime flow

### Authentication

`useAuth` calls:

- `POST /auth/login`
- `GET /auth/me`

The JWT is stored in local storage and added to all authenticated fetch/SSE requests.

### Sessions

`useChatSessions` loads and manages:

- `GET /agentic/sessions`
- `POST /agentic/sessions`
- `GET /agentic/sessions/:id`
- `DELETE /agentic/sessions/:id`
- `POST /agentic/sessions/:id/generate-title`

Message state is cached per session so switching sessions does not interrupt background streams.

### Workflow streaming

`useAgenticStream` opens `POST /agentic/runs/stream` and feeds `agent.v2` envelopes through `applyAgentV2Event`.

`useStreamManager`:

- keeps one active stream per session
- supports parallel streams across different sessions
- tracks `runId` per session
- calls `POST /agentic/runs/:run_id/cancel` on stop/disconnect/error when possible

### Configuration and workspace resources

The configuration center and related panels use:

- `GET/PATCH/POST /api/system-config...`
- `GET/POST/PATCH/DELETE /agentic/workspaces...`
- `GET/PATCH /agentic/workspaces/:id/runtime-config`
- `GET/PUT/DELETE /agentic/ontologies/:id/data-source`
- `POST /agentic/ontologies/:id/data-source/test`
- `GET /agentic/business-data/provider-schemas`

### Tool activity

- `TaskDAGSidebar` visualizes run progress from streamed turn, tool, sub-agent, and summary events
- Production nginx proxies `/auth`, `/agentic`, `/api`, and `/health`

## Docker

- Frontend-only compose: [`frontend/docker-compose.yml`](docker-compose.yml)
- Full stack compose: [`docker-compose.yml`](../docker-compose.yml)

The production image serves static assets with nginx on port `80` inside the container, mapped to `8888` by default in Compose.
