# Personal Default Workspace Minimal Fix

## Status

Approved on 2026-09-01.

## Problem

An ordinary account can enter a legacy ownerless or global default workspace instead of an account-owned personal workspace. This can expose a legacy `test` workspace in the workspace selector, prevent the account from receiving owner capabilities such as knowledge-package import, and carry a previous account's browser selection into the new account.

The first implementation also changed global HTTP and MCP ACL semantics, migration auditing, deployment guidance, and several historical ADRs. Those changes are outside this fix.

## Goals

- On the first workspace-list request, create or reuse one workspace owned by the authenticated ordinary user.
- Name a newly managed personal workspace `默认工作空间` and create it without seeded knowledge packages or content. This is its initial state, not an invariant, and does not prevent later imports.
- Return that workspace as `preferred_workspace_id` without changing the platform-wide `default_workspace_id`.
- Show only explicitly owned or member workspaces to ordinary users in the workspace list, so legacy ownerless `test` workspaces are not selected by the UI.
- Persist browser workspace selection per username.
- Center the empty knowledge-package state in the available workbench area.
- Deliver equivalent minimal changes to both `develop` and `0.26.8.0` through separate clean branches.

## Non-Goals

- Do not change global HTTP, ACP, or MCP authorization for ownerless or global-default workspaces.
- Do not change solver behavior, ontology behavior, or modeling-platform code.
- Do not add ACL migration reports or visibility conversion logic.
- Do not revise deployment instructions or historical ACL ADRs.
- Do not relabel the global default badge as the personal preferred workspace.
- Do not delete existing workspace data from disk.

## Design

### Backend

`GET /agentic/workspaces` provisions a personal workspace for an ordinary authenticated user under the existing identity lifecycle lock. It reuses an existing owned workspace; when the user owns multiple workspaces, it chooses the earliest by `created_at`, using workspace ID as the deterministic tie-breaker. It safely handles concurrent first requests and deterministic ID collisions, and never changes the global default workspace.

The list response adds `preferred_workspace_id`. For ordinary users, list visibility is limited to workspaces where the user is owner or member. This is a presentation and selection boundary only; shared route-level and MCP ACL functions retain their existing compatibility behavior.

Personal workspace IDs use the reserved `home-<digest>` form. Public workspace creation cannot claim that namespace, while the internal provisioning path can.

### Frontend

Workspace selection follows this order:

1. A still-visible selection saved for the current username.
2. The server-provided `preferred_workspace_id`.
3. The global `default_workspace_id` when visible.
4. The first visible workspace.

Local storage keys are namespaced by username. The knowledge workbench uses a centered empty-state shell when no package exists.

### Branching

- `fix/personal-default-workspace-develop` starts from `origin/develop`.
- `fix/personal-default-workspace-0.26.8.0` starts from `origin/0.26.8.0`.
- Each branch contains only this design, the minimal implementation, direct tests, and the narrowly relevant API reference changes.
- The earlier `fix/personal-default-workspace` branch is not used for either PR.

## Error Handling

- Identity directory unavailable: preserve the existing stable service-unavailable mapping.
- User identity missing or being deleted: fail closed through the existing identity lifecycle errors.
- Personal ID allocation exhausted: return `PERSONAL_WORKSPACE_PROVISIONING_FAILED`.
- Workspace-list failure must not reuse a previous account's workspace selection.

## Verification

- Backend tests cover first creation, owner assignment, reuse, concurrency, ID collision, reserved IDs, admin behavior, preferred selection, and hiding legacy ownerless list entries.
- Frontend tests cover per-account persistence, selection priority, stale-selection rejection, and centered empty state.
- Existing focused workspace authorization tests confirm route-level and MCP compatibility remains unchanged.
- Run the affected backend suite, the full frontend test suite, lint, and production build on both target branches.
- Confirm each PR diff contains no modeling files, ACL migration scripts, deployment guide changes, MCP ACL changes, or historical ADR revisions.
