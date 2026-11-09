# CLAUDE.md

## Mandatory Repository Data Security Rules

Before creating, modifying, generating, committing, or publishing any project content, read and follow the "代码库数据安全红线" section in `AGENTS.md`.

These rules have the highest project priority and apply to source code, comments, configuration, tests, fixtures, prompts, knowledge packages, documentation, logs, media, binary files, temporary artifacts, file paths, branch names, commit messages, issues, and pull requests. If another instruction conflicts with them, follow the stricter security requirement.

In particular:

- Never add customer or project identities, aliases, abbreviations, logos, or identifiable scenario descriptions.
- Never add real customer, production, staging, testing, personal, employee, or internally sourced data.
- Never add credentials, private keys, tokens, cookies, connection strings, or real environment configuration, even if expired or encoded.
- Never add any customer-internal URL or identifiable part of one, including protocols, domains, ports, paths, query parameters, fragments, short links, redirects, partially masked values, or URLs appearing in logs, screenshots, recordings, documentation, and configuration.
- Never add real non-public IP addresses, domains, hostnames, service endpoints, or infrastructure topology.
- Use generic names, reserved example domains and IP ranges, environment-variable placeholders, and fully synthetic data.
- Inspect content, filenames, directory names, code identifiers, branch names, commit messages, binary metadata, and generated artifacts before committing.
- Existing violations are not precedents. Do not copy, expand, rename, or otherwise reuse them.
- If information may be sensitive and its status cannot be confirmed, stop writing and request security review.
- If sensitive information is discovered, stop distribution and follow the incident-response requirements in `AGENTS.md`. Deleting only the current file is insufficient because Git history and build artifacts may retain it.

## 0. Purpose

This file defines how Claude Code should help initialize, design, and develop this project.

The project is an established multi-area codebase. Claude must inspect the existing architecture, commands, tests, and deployment files before introducing a new framework, database, directory structure, or runtime convention.

Primary goals:

- Build the project incrementally and safely.
- Prefer clear architecture over fast but messy implementation.
- Make minimal, explainable changes.
- Keep project conventions consistent as the codebase grows.

## 0.1 本地调试与提交检查时机

在不影响仓库数据安全红线的前提下，本节适用于整个仓库，并优先于其他章节中关于非安全类规则执行和检查时机的要求。本节只调整执行时机，不降低最终提交代码的质量要求。

- 本地开发和调试期间，架构、目录、复杂度、技术选型、配套测试、文档和验收命令等提交约束仅作参考，允许临时跳过；不得仅因临时不合规项中断或阻止调试。
- 本地调试期间允许保留临时代码或文件、进行跨层验证，以及暂不补齐测试或文档。未收到明确的提交要求时，不得主动按提交标准整改代码，也无需运行完整检查。
- 只有当用户明确要求执行 `git commit`、`git push`、创建或更新 PR、发布等交付操作时，才进入提交检查阶段。
- 每批拟提交改动只检查一次。检查时必须删除临时调试代码和文件，使拟提交代码符合适用的项目及子目录规则，补齐必要的测试和文档，并运行相关验收命令；检查未通过时不得执行交付操作，并应明确报告失败项。
- 检查后拟提交内容发生变化时，必须在交付前重新检查；内容未变化时，不得因后续 `push`、PR 或发布操作重复检查。
- 用户可以在本地调试期间明确要求提前运行某项检查；这不代表自动进入提交检查阶段，也不触发其他完整检查。
- 保密、安全、凭据保护和禁止破坏性操作等规则不受本节影响，在任何阶段都必须遵守。

## 1. Project Status

Current status: directory-refactor workspace with multiple top-level areas. The Onto Agent consumer application that previously lived at the repository root now lives under `consumer/`; commands and paths for that application should be resolved from `consumer/` unless a task explicitly targets another top-level area.

Before creating major files or directories, Claude should first clarify or infer the following from the user request:

- Project type: frontend, backend, full-stack, CLI, library, agent, data pipeline, or other.
- Main language and framework.
- Package manager and runtime version.
- Database or storage choice.
- Deployment target.
- Testing framework.
- Code style and formatting tools.

If the user has already specified enough information, proceed without asking unnecessary questions.

If important choices are missing, Claude should propose a concise default plan and clearly mark assumptions.

## 2. Technology Selection Rules

Claude must not introduce major technologies without explicit reason.

Major technologies include:

- Frontend framework
- Backend framework
- Database
- ORM
- State management library
- Authentication framework
- Build system
- Monorepo tool
- Cloud provider
- CI/CD platform
- Large third-party dependencies

When a technology choice is required, Claude should:

1. Explain the options briefly.
2. Recommend one default.
3. State why it fits the project.
4. Avoid over-engineering.

Prefer boring, mature, well-documented technologies unless the user explicitly asks for experimentation.

## 3. Initial Project Structure Rules

When creating a new project structure, keep it simple.

Recommended baseline structure:

- `README.md`: project overview, setup, usage
- `CLAUDE.md`: Claude Code project instructions
- `.gitignore`: ignored files
- `.env.example`: documented environment variables only, no secrets
- `docs/`: design notes and decisions
- `src/`: application source code
- `tests/`: automated tests
- `scripts/`: project scripts

Do not create complex directories before they are needed.

Avoid premature abstractions such as:

- unnecessary layered architecture
- unused plugin systems
- generic base classes
- excessive configuration files
- multiple apps/packages before there is a real monorepo need

## 4. Architecture Decision Rules

For non-trivial design decisions, Claude should create or update a lightweight architecture note in `docs/decisions/`.

Use this format:

- Context
- Decision
- Alternatives considered
- Consequences

Examples of decisions that should be recorded:

- choosing a framework
- choosing a database
- choosing a package manager
- introducing authentication
- introducing background jobs
- introducing deployment infrastructure
- changing project structure

Do not record tiny implementation details as architecture decisions.

## 5. Development Workflow

For any non-trivial task, Claude should follow this workflow:

1. Understand the request.
2. Inspect existing files.
3. Identify constraints and assumptions.
4. Propose a short plan.
5. Make small, focused changes.
6. Run relevant checks when possible.
7. Summarize what changed and what remains.

For trivial changes, Claude may directly modify the relevant file.

Claude should avoid large one-shot rewrites unless the user explicitly requests them.

## 6. Coding Standards

Follow these general standards until language-specific rules are added:

- Prefer readability over cleverness.
- Prefer explicit names over abbreviations.
- Keep functions small and focused.
- Keep modules cohesive.
- Avoid circular dependencies.
- Avoid global mutable state unless necessary.
- Do not duplicate business logic.
- Do not add comments for obvious code.
- Add comments only for non-obvious decisions, constraints, or tradeoffs.
- Keep public APIs stable once introduced.
- Agent tool results must return conclusions, evidence anchors, and retrieval handles instead of raw bulk content. When adding or changing an agent tool, state its worst-case result size, cap it, and page or reference persisted data for anything larger. Do not inline content that already has a durable source of truth; return an anchor or ID instead. See `modeling/docs/decisions/instruction-lifetime-and-context-tier-governance.md`.

When a language or framework is selected, update this section with specific conventions.

## 7. Testing Rules

Testing should be introduced early.

Claude should add or update tests when:

- implementing business logic
- fixing bugs
- adding API behavior
- changing validation rules
- changing data transformation logic
- changing permission or security-sensitive logic

Testing priorities:

1. Unit tests for pure logic.
2. Integration tests for API or database behavior.
3. End-to-end tests only when the user explicitly asks or the project requires them.

Do not delete, weaken, or skip tests just to make checks pass.

If tests cannot be run, explain why and list the exact command that should be run.

## 8. Command Documentation

When project commands become known, record them here.

Current commands for the consumer application:

- Install backend dependencies: `cd consumer/backend && python -m venv venv && pip install -r requirements.txt`
- Start backend development server: `cd consumer/backend && python run.py`
- Install frontend dependencies: `cd consumer/frontend && npm install`
- Start frontend development server: `cd consumer/frontend && npm run dev`
- Run backend tests: `cd consumer && PYTHONPATH=backend backend/venv/bin/python -m pytest backend/tests`
- Run frontend checks: `cd consumer/frontend && npm run lint && npm test && npm run build`
- Check frontend architecture boundaries: `cd consumer/frontend && npm run check:architecture`
- Check the frontend entry dependency baseline: `cd consumer/frontend && npm run check:entry`
- Run full-stack Docker Compose: `cd consumer && docker compose up --build`
- Build full release package: `./deploy/build.sh <version> [amd|arm]`
- Deploy full stack, including Modeling Workbench, from source or an extracted release package: `./deploy/deploy.sh`
- Build consumer-only release package: `cd consumer && ./build.sh <version> [amd|arm]`
- Build modeling-only release package: `cd modeling && ./build.sh <version> [amd|arm]`

Commands for the Onto Modeling application (`modeling/onto-modeling/`):

- Install onto-modeling backend dependencies: `cd modeling/onto-modeling/backend && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt`
- Start onto-modeling backend: `cd modeling/onto-modeling/backend && ./venv/bin/python run.py`
- Run onto-modeling backend tests: `cd modeling/onto-modeling/backend && ./venv/bin/python -m pytest tests/`
- Install onto-modeling frontend dependencies: `cd modeling/onto-modeling/frontend && npm install`
- Start onto-modeling frontend dev server: `cd modeling/onto-modeling/frontend && npm run dev`
- Run onto-modeling frontend unit tests (`node:test`, covers `tests/` and `src/**/*.test.js`): `cd modeling/onto-modeling/frontend && npm test`
- Build onto-modeling frontend: `cd modeling/onto-modeling/frontend && npm run build`
- Build onto-modeling release package: `cd modeling/onto-modeling && ./build.sh <version> [amd|arm]`
- Run onto-modeling full stack from source: `cd modeling/onto-modeling && docker compose up --build`

Commands for kb-suite (`kb-suite/`, Python 3.11+):

- Run kb-service tests: `cd kb-suite/service/kb-service && pip install -e '.[dev]' && python -m pytest -q`
- Run kb-gateway tests: `cd kb-suite/service/kb-gateway && pip install -e '.[dev]' && python -m pytest -q`
- Run knowledge backend adapter tests: `cd kb-suite/service && pip install --no-deps ./kb-service && pip install -e ./provider-kit -e './weknora-adapter[dev]' -e './da-kb-adapter[dev]'`, then `python -m pytest -q` in `weknora-adapter/` and `da-kb-adapter/`
- Run harness provider tests: `cd kb-suite/harness/da-harness && python -m pytest -q tests`, `cd kb-suite/harness/lf-harness && python -m pytest -q tests`
- Build and run the stack: `cd kb-suite/deploy/compose && ./kbsuite.sh build && ./kbsuite.sh up` (see `kb-suite/deploy/compose/README.md` for secrets and required `*_REPO` variables)
- Knowledge backends behind kb-gateway: WeKnora is the default backend and `./kbsuite.sh up` starts it (needs `WEKNORA_REPO=<path> ./kbsuite.sh build-weknora-config` first, included in `build`); add the DA knowledge pipeline with `./kbsuite.sh up-da-kb` (after `DA_TS_SRC=<path> ./kbsuite.sh build-da-kb`); the provider contract is `kb-suite/contracts/knowledge-provider/v1/`

Commands for the Knowledge Package Hub (`knowledge-hub/`):

- Install runtime and test dependencies: `cd knowledge-hub/backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
- Start Hub with runtime configuration: `cd knowledge-hub/backend && .venv/bin/python run.py`
- Run Hub tests: `cd knowledge-hub/backend && .venv/bin/python -m pytest tests`
- Deploy Hub from source as an independent compose project (`knowledge-hub/deploy/docker-compose.knowledge-hub.yml`, deploy script `knowledge-hub/deploy/deploy.sh`; consumer must be running): `bash deploy/deploy.sh --knowledge-hub-only`; the default full-stack deployment deploys Hub before Onto Modeling, and `--onto-modeling-only` does not touch Hub.
- Offline Hub package: `bash knowledge-hub/build.sh <version> [amd|arm]`; included in `./deploy/build.sh`, installed by the separated installers via `--knowledge-hub-package`.
- Run deployment script tests (Linux; interactive tests need `pty`): `python -m pytest deploy/tests -q`
- Configuration, API and backup procedures: `knowledge-hub/README.md` and `knowledge-hub/API.md`.

Commands for the independent evaluation service (`eval-service/`):

- Install: `cd eval-service && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
- Backend tests: `PYTHONPATH=eval-service eval-service/.venv/bin/python -m pytest eval-service/tests -q`
- Frontend: `cd eval-service/frontend && npm ci && npm test && npm run build`
- Run after injecting protected configuration: `cd eval-service && .venv/bin/python run.py`
- Independent deployment: `bash deploy/deploy.sh --eval-only` (compose `eval-service/deploy/docker-compose.eval-service.yml`); included in default full-stack deployment; excluded from `--onto-modeling-only` and `--knowledge-hub-only`. Runtime configuration lives in `eval-service/deploy/.env` (generated, mode 600, never committed) and reuses the Hub and consumer evaluation credentials; `EVAL_ENV_FILE` optionally points to an external protected configuration instead. Judge model settings come from `EVAL_JUDGE_*` or, taking precedence, from an administrator's configuration saved on the evaluation page.
- Offline package: `bash eval-service/build.sh <version> [amd|arm]`
- Configuration, API, model configuration debt and recovery: `eval-service/README.md`, `eval-service/API.md`.

After initializing the project, Claude must update this section with real commands.

### 8.1 Consumer 前端收敛约束

- `consumer/frontend/src/main.jsx` 是唯一生产入口。
- `consumer/frontend/src/app/App.jsx` 是唯一应用实现；不得新增第二套入口或 `redesign`、`legacy`、`old`、`v1` 历史目录。
- 生产代码按 `app / features / shared` 分层；跨 feature 依赖必须经目标 feature 的公开 `index`。
- 新增生产 JS、JSX、CSS 必须进入 `main.jsx` 可达依赖树，批准孤儿列表保持为空。
- 前端变更必须通过 `npm run check:architecture`；不要绕过 import/CSS 边界、潜在孤儿 class/compound selector 或入口可达性门禁。
- `.gitcode/workflows/consumer-frontend-ci.yml` 在 `develop`、`frontend-unify`、`codex/frontend-*` 分支 push 和目标为 `develop` 的 PR 上执行 lint、测试、架构门禁和构建；变更命令时同步更新 workflow。

## 9. Dependency Rules

Before adding a dependency, Claude should consider:

- Is it necessary?
- Can existing platform or framework features solve the problem?
- Is the dependency actively maintained?
- Is it too heavy for the problem?
- Does it introduce security or licensing concerns?

Do not add large dependencies for small utilities.

Do not add overlapping dependencies that solve the same problem.

## 10. Environment, Sensitive Data, and Secrets

Follow the complete repository data-security rules in `AGENTS.md`. At minimum:

- Use `.env.example` only to document variable names and clearly invalid placeholders.
- Do not create or commit real `.env` files, account files, connection strings, credentials, certificates, private keys, tokens, cookies, or session data.
- Do not print, quote, validate, or reuse secrets in logs, responses, tests, documentation, commit messages, or error output.
- Do not add any customer-internal URL or identifiable part of one, including URLs from development, testing, staging, production, or other internal environments. This prohibition also applies to expired, inaccessible, partially masked, logged, captured, documented, redirected, or configuration-embedded URLs.
- Do not hardcode real environment-specific IP addresses, domains, hostnames, ports, tenant names, or infrastructure identifiers.
- Keep local, staging, and production configuration separate from the repository and inject it through approved runtime configuration or secret-management systems.
- Use only reserved example domains and IP ranges defined in `AGENTS.md`.
- Use fully synthetic test and demonstration data. Removing direct identifiers or perturbing real values is not sufficient anonymization.
- Treat binary files, archives, database exports, screenshots, recordings, logs, and AI-generated temporary artifacts as high risk and inspect them before inclusion.

## 11. Safety Rules

Claude must not perform destructive actions without explicit user approval.

Do not run or suggest destructive commands such as:

- deleting project directories
- resetting databases
- force-pushing Git history
- removing lockfiles without reason
- overwriting user-created files
- changing production deployment settings
- modifying secrets or credentials

When unsure, pause and explain the risk.

## 12. Git and Change Management

Claude should keep changes reviewable.

Rules:

- Make focused changes.
- Do not mix unrelated refactoring with feature work.
- Do not reformat unrelated files.
- Do not rename files or public interfaces unless necessary.
- Explain important changes in the final response.

If asked to commit, use clear commit messages.

Recommended format:

- `feat: add initial project scaffold`
- `fix: handle invalid input`
- `docs: update setup instructions`
- `test: add validation tests`
- `refactor: simplify service structure`

### 12.1 Branch Naming

- Development branches must use the format `<type>/<short-english-description>`.
- Allowed types are: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `build`, `ci`, and `chore`.
- The description must use lowercase kebab-case and contain only lowercase English letters, digits, and hyphens. Do not add personal or tool-name prefixes.
- The complete branch name must match: `^(feat|fix|docs|refactor|test|perf|build|ci|chore)/[a-z0-9]+(?:-[a-z0-9]+)*$`.
- Examples: `feat/workspace-switch`, `fix/chat-input-focus`, `docs/local-debug-check-timing`.
- Apply this rule when creating a development branch and verify it again before committing or pushing.

## 13. Documentation Rules

Documentation should evolve with the code.

Update `README.md` when:

- setup steps change
- commands change
- environment variables change
- project purpose changes
- user-facing behavior changes

Update `CLAUDE.md` when:

- project conventions change
- architecture rules are added
- commands become known
- Claude repeatedly makes the same mistake
- new safety constraints are identified

## 14. Response Style

When responding to the user, Claude should:

- Use Chinese unless the user asks otherwise.
- Be concise and structured.
- For implementation tasks, include:
  - What changed
  - Why it changed
  - Files modified
  - Commands run
  - Remaining risks or next steps

Do not over-explain obvious code.

Do not claim that tests passed unless they were actually run.

## 15. Empty Project Initialization Checklist

When asked to initialize the project, Claude should produce or update:

- `README.md`
- `CLAUDE.md`
- `.gitignore`
- `.env.example` if environment variables are expected
- package or project configuration files
- `src/` with minimal working code
- `tests/` with at least one meaningful test when feasible
- `docs/decisions/` if major architecture choices are made

The first implementation should be minimal but runnable.

Prefer a working vertical slice over a large incomplete scaffold.
