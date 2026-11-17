# deepanalyze-harness

openJiuwen HarnessProtocol 1.0 provider for a remote DeepAnalyze-Java (DA) engine. Deployment, configuration and the 知识求索 page: `deploy/compose/README.md`.

## Items

Besides `tool` and `step`, the provider emits three item types. Their `data` already has the shape of the JiuwenSwarm event that shows them, so an io_adapter only has to copy fields. The DA events they come from are still forwarded as `ProviderEvent("deepanalyze", ...)` (the 知识求索 page reads those).

| item_type | item_id | kind | DA source |
|---|---|---|---|
| `todo` | `{taskId}:todo` | UPDATED, whole list each time | `todo_update` |
| `subagent` | `{taskId}:sub:{subagent_id}` | STARTED → UPDATED (activity) → COMPLETED | `workflow_event` (workflow runs and delegated tasks) |
| `file` | `{tool item id}:file` | COMPLETED | `tool_result` of `push_file` |

### Proposed io_adapter mapping

`_project_item` in `openjiuwen/harness_providers/io_adapter.py` (today it keeps only `tool`):

| item | OutputSchema `type` | `payload` | Swarm event |
|---|---|---|---|
| `todo` | `todo.updated` | `{"todos": data["todos"]}` | `todo.updated` |
| `subagent` without `activity` | `subagent_updated` | `{"subagent_updated": {**data, "parent_session_id": S}}` | `chat.subtask_update` |
| `subagent` with `activity` | `subagent_activity` | `{"subagent_activity": {**data["activity"], "parent_session_id": S}}` | `chat.subagent_activity` |
| `file` | `chat.file` | `{"files": [{path, name, size, mime_type} + download_url, download_token}]` | `chat.file` |

- `S` is the host session id: only the host knows it.
- `download_url` and `download_token` are minted by the host (Swarm's `build_file_download_info(path, name, session_id, user_id)`). `title` is extra and can be dropped.

### Field shapes

**todo** has the same shape as Swarm's `format_todos_for_frontend`:

```json
{"todos": [{"id": "t1", "content": "收集数据", "activeForm": "收集数据", "status": "pending"}]}
```

- `status` is one of `pending`, `in_progress`, `completed` or `cancelled`.
- DA renders the list as text lines (`⬜ [id] 内容 (pending)`), which the provider parses. A list of objects is accepted too.
- DA leaves cancelled items out, so `cancelled` never comes from DA today.
- DA has no `activeForm`, so it is filled with `content`.

**subagent**, roster form (STARTED, COMPLETED), has the keys of agent-core's `build_subagent_updated_payload` except `parent_session_id`. One subagent per workflow member:

| field | value |
|---|---|
| `subagent_id`, `sub_session_id` | `da-{workflowId}-{agentId}`; characters other than `[A-Za-z0-9_-]` become `_` |
| `subagent_type` | the run's `mode` from `workflow_start` (`council`, `pipeline`, ...); `deepanalyze` if the start was not seen |
| `display_name` / `role` / `task_description` | `agentId` / `role` / `task` of `workflow_agent_start` |
| `status` | `running`, then `closed` |
| `turn_outcome` = `closed_reason` | `completed`, `failed` or `cancelled`: from `workflow_agent_complete.status`; for members still open, from `workflow_complete.status` |
| `error` | `{"code": "DA_MEMBER_FAILED", "message": ...}` when failed, else null |
| `created_at`, `updated_at`, `closed_at` | ms since epoch, taken when the event arrives |
| `revision` | +1 on every roster event |
| `lifecycle`, `can_send_input`, `needs_resume` | `live` → `closed`, false, false (a member runs once and takes no input) |

**subagent**, activity form (UPDATED), is `{"subagent_id": ..., "activity": {...}}`. `activity` has the keys of agent-core's `SubagentActivity.to_dict()` except `parent_session_id`:

| DA member event | `kind` | `summary` | other fields |
|---|---|---|---|
| `workflow_agent_tool_call` | `tool_call` | `tool({...args})` | `tool_name`, and `tool_call_id` (DA's `callId`, else `{subagent_id}-{seq}`) |
| `workflow_agent_tool_result` | `tool_result` | the output | `tool_name`, `ok` (false when status is error or failed), and `tool_call_id` matched by DA's `seq` |
| `workflow_agent_chunk` | `thinking` | the text | |

- Every activity also has `task_id` (equal to `subagent_id`), `seq` (counts 1, 2, ... per member and is assigned by the provider), `at_ms` and `phase_id` = 0.
- `summary` is at most 2000 characters.

When a delegated run continues into the next turn, DA replays `workflow_start` and every member's start. A replayed start of a finished member is ignored. A member that is still open gets STARTED again in the new turn, with the same `created_at`.

**file**: `{"path", "name", "size", "mime_type", "title"}`, one `chat.file` entry.

- The provider fetches `downloadUrl` from DA (`GET /api/sessions/{sessionId}/output/{fileName}`, served by DA patch `0004`) into the working directory and points `path` there.
  - The working directory is the start context's `cwd`, else the `cwd` provider config key.
  - Only URLs on the engine's own origin are fetched, because the client carries the engine secret.
  - Nothing is overwritten. A taken name becomes `name (1).ext`; a file with the same bytes is reused.
  - Files over `max_file_bytes` (default 100 MB) are not fetched.
- `mime_type` comes from DA's `mimeType`, else from the response, else from the extension.
- Fail soft: with no working directory, a 404 (a DA image without patch `0004`), another origin, a network error or an oversize file, the provider logs a warning and emits no `file` item. The `tool` item and the turn are unaffected.

## Tests

In the Swarm image, which has openjiuwen installed:

```bash
docker run --rm --entrypoint sh -v "$PWD/harness/da-harness:/src:ro" -e PYTHONPATH=/src kb-suite/swarm:dev -c \
  "pip install -q pytest pytest-asyncio && cd /tmp && python -m pytest -q -p no:cacheprovider -o asyncio_mode=auto /src/tests"
```

`e2e/run_contract.sh` runs agent-core's harness contract suite against a live DA.
