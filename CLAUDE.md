# CLAUDE.md

## 0. Purpose

This file defines how Claude Code should help initialize, design, and develop this project.

The project is an established multi-area codebase. Claude must inspect the existing architecture, commands, tests, and deployment files before introducing a new framework, database, directory structure, or runtime convention.

Primary goals:

- Build the project incrementally and safely.
- Prefer clear architecture over fast but messy implementation.
- Make minimal, explainable changes.
- Keep project conventions consistent as the codebase grows.

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

## 10. Environment and Secrets

Never commit secrets.

Rules:

- Use `.env.example` to document required variables.
- Do not create real `.env` files with secrets.
- Do not print secrets, tokens, private keys, passwords, or credentials.
- Do not hardcode environment-specific URLs unless they are clearly examples.
- Keep local, staging, and production configuration clearly separated.

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
