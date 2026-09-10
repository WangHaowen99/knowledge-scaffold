"""Operation patcher worker — turns buffered proposals into staged `.md` patches.

Lifecycle:
- Owned by the ASGI lifespan. Two supervised asyncio tasks provide an
  event-driven worker and a periodic sweep; blocking scans use the bounded
  runtime tool pool.
- Subscribes to `ProposalBuffer.change_listener` for the event-driven path.
- Manual `request_rescan()` is exposed for the admin "立刻重新评估此 op" button.

Three-gate filter:
- ① Buffer pre-screen (here, in `_on_change`): ignore events that can never
  cross the threshold (`reject` and bare `endorse`).
- ② Queue dedup (here, in `_enqueue`): one pending entry per op.
- ③ Scan early-return (here, in `_scan`): staged-exists / cooldown /
  human-edited `.md` / threshold-not-met — all bail before the LLM call.

Only ③'s final branch (LLM merge) costs money.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.evolution.buffer import ProposalBuffer
from agent.evolution.models import Aggregate
from agent.runtime.async_utils import run_sync_compat
from agent.runtime.supervisor import RunTaskSupervisor
from llm import LLMPurpose, get_llm_router
from ontology.mutation_lock import ontology_mutation_lock
from ontology.ontologies import get_ontology_version_dir
from ontology.operation_execution import is_deterministic_package_locked
from pydantic import BaseModel, Field
from utils.logging import get_logger
from workspace.workspaces import get_workspace_paths, get_workspaces_root

logger = get_logger("evolution.patcher")


# -----------------------------------------------------------------------------
# LLM contract
# -----------------------------------------------------------------------------


class StagedMergeOutput(BaseModel):
    """Structured output from the merge model for one staged patch.

    The model rewrites the full markdown; the service compares and replaces it
    at file level after human review.
    """

    new_markdown: str = Field(
        ...,
        description="The complete rewritten operation .md file, top to bottom.",
    )
    change_summary: str = Field(
        ...,
        description="One-paragraph summary of what changed and why, in Chinese.",
    )
    incorporated_fingerprints: List[str] = Field(
        default_factory=list,
        description=(
            "List of proposal fingerprints actually merged into the new "
            "markdown. May be a subset of the input if the model judged some "
            "as redundant or contradictory."
        ),
    )


PATCHER_SYSTEM_PROMPT = """\
You patch an ontology operation `.md` spec by merging buffered improvement
proposals into it. The output is a staged proposal — a human reviewer will
apply or reject it.

Hard rules:
- Never invent facts. Only incorporate what the proposals say.
- Preserve the file's existing section structure (`# 业务描述`, `# 输入`,
  `# 输出`, `# 逻辑描述`, `# 注意事项`, etc.). Add `# 注意事项` if it does
  not exist and you have pitfalls to record.
- Never rename input/output primary keys; you may correct sub-field schema
  facts when a `schema` proposal explicitly says so.
- `procedure` proposals refine `# 逻辑描述` (or merge into existing steps).
- `pitfall` proposals append concise bullets under `# 注意事项`. Deduplicate
  against existing items.
- `schema` proposals correct entity field names / formats in the input or
  output tables, scoped to their `entity_key`.
- If a proposal contradicts the existing spec, prefer the proposal (the user
  has signalled it; the buffer already gates noise). But also note the
  contradiction in `change_summary`.
- Keep the writing terse. No commentary about the patch process inside the
  markdown body itself.
- Output the FULL rewritten markdown. Do not output diffs or partial files.
- Return raw JSON only. Do not wrap the response in markdown fences.
- JSON shape:
  {
    "new_markdown": "完整改写后的 operation markdown",
    "change_summary": "一句话说明改动原因",
    "incorporated_fingerprints": ["完整 fingerprint"]
  }
"""


PATCHER_USER_TEMPLATE = """\
[Operation] {operation_key}  (ontology: {ontology_id})

[Current `.md` content]
```markdown
{current_md}
```

[Buffered proposals to incorporate ({proposal_count})]
{proposals_block}

[Instructions]
Rewrite the operation `.md` integrating the proposals according to the rules
in the system prompt. Return the full new markdown plus a brief change_summary
and the list of fingerprints you actually merged.

Respond with raw JSON only. `new_markdown` must be a JSON string containing the
complete markdown content. Do not use ``` fences outside the JSON response.
"""


def _format_proposals_block(actionable: List[Aggregate]) -> str:
    if not actionable:
        return "(none)"
    lines: List[str] = []
    for idx, a in enumerate(actionable, 1):
        sources = ", ".join("{}:{}".format(k, v) for k, v in sorted((a.sources or {}).items()))
        lines.append(
            "{idx}. fingerprint={fp} kind={kind} entity={entity}\n"
            "   occurrences={occ} applied={app} 👍={end} sources=[{src}]\n"
            "   content: {content}".format(
                idx=idx,
                fp=a.fingerprint,
                kind=a.kind,
                entity=a.entity_key or "-",
                occ=a.occurrences,
                app=a.applied_count,
                end=a.active_endorsements,
                src=sources,
                content=a.canonical_content,
            )
        )
    return "\n\n".join(lines)


# -----------------------------------------------------------------------------
# Patcher worker
# -----------------------------------------------------------------------------


@dataclass
class PatcherConfig:
    """Thresholds and scheduling options for the proposal patcher worker."""

    min_occurrences: int = 3
    min_applied: int = 2
    max_rejection: int = 0
    cooldown_sec: int = 3600
    sweep_interval_sec: int = 1800
    auto_apply: bool = False  # advisory; current path only writes staged


_TRIGGERING_EVENTS = {"append", "outcome", "vote", "endorse", "edit", "applied"}
# `reject` never moves a proposal closer to actionable; skip those.


class OperationPatcher:
    """Supervised worker that scans the proposal buffer and emits staged patches."""

    def __init__(
        self,
        *,
        buffer: ProposalBuffer,
        config: PatcherConfig,
    ) -> None:
        self._buffer = buffer
        self._cfg = replace(config)
        self._cfg_lock = threading.Lock()
        self._queue: asyncio.Queue[Tuple[Optional[str], str, str]] | None = None
        self._enqueued: set[Tuple[Optional[str], str, str]] = set()
        self._pending_before_start: set[Tuple[Optional[str], str, str]] = set()
        self._enqueued_lock = threading.Lock()
        self._sweep_wakeup: asyncio.Event | None = None
        self._supervisor: RunTaskSupervisor | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._started = False
        self._closed = False

    # -------------------------------------------------------------- lifecycle
    def bind_supervisor(self, supervisor: RunTaskSupervisor) -> None:
        if self._started:
            raise RuntimeError("cannot rebind a started operation patcher")
        self._supervisor = supervisor

    async def start(self) -> bool:
        if self._started:
            return False
        if self._closed:
            raise RuntimeError("operation patcher is closed")
        if self._supervisor is None:
            raise RuntimeError("operation patcher requires a runtime supervisor")
        self._owner_loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._sweep_wakeup = asyncio.Event()
        self._started = True
        self._tasks = {
            self._supervisor.start_derived(
                self._worker_loop(),
                name="operation-patcher-worker",
            ),
            self._supervisor.start_derived(
                self._sweep_loop(),
                name="operation-patcher-sweep",
            ),
        }
        initial_keys = await run_sync_compat(
            lambda: list(self._buffer.iter_op_dirs()),
            task_name="operation-patcher-bootstrap",
            offload_kind="tool",
        )
        with self._enqueued_lock:
            initial_keys.extend(self._pending_before_start)
            self._pending_before_start.clear()
        for key in initial_keys:
            self._enqueue_on_loop(tuple(key))
        logger.info(
            "OperationPatcher started (sweep=%ss)",
            self._current_config().sweep_interval_sec,
        )
        return True

    async def close(self, *, deadline: float) -> bool:
        self._closed = True
        if not self._started:
            return True
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if not task.done() and task is not current]
        for task in tasks:
            task.cancel()
        # Cancelling before waking is intentional.  Waking the Event first can
        # race asyncio.wait_for's completed waiter with Task.cancel(), leaving
        # the sweep task stuck in "cancelling" until a second cancellation.
        wakeup = self._sweep_wakeup
        if wakeup is not None:
            wakeup.set()
        if not tasks:
            self._started = False
            return True
        timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        self._started = False
        return not pending

    def cancel_nowait(self) -> None:
        """Best-effort test cleanup; application shutdown uses ``close``."""

        loop = self._owner_loop
        if loop is None or loop.is_closed():
            return

        def cancel_tasks() -> None:
            for task in tuple(self._tasks):
                if not task.done():
                    task.cancel()

        loop.call_soon_threadsafe(cancel_tasks)

    def update_config(self, config: PatcherConfig) -> None:
        """Swap runtime thresholds without rebuilding the worker threads."""
        if config is None:
            return
        with self._cfg_lock:
            self._cfg = replace(config)
        self._wake_sweeper()
        logger.info(
            "OperationPatcher config updated min_occurrences=%s min_applied=%s max_rejection=%s cooldown=%ss sweep=%ss auto_apply=%s",
            config.min_occurrences,
            config.min_applied,
            config.max_rejection,
            config.cooldown_sec,
            config.sweep_interval_sec,
            config.auto_apply,
        )

    # ----------------------------------------------------- buffer-side hook
    def on_buffer_change(
        self,
        event: str,
        workspace_id: Optional[str],
        ontology_id: str,
        operation_key: str,
        fingerprint: str,
    ) -> None:
        """Buffer change_listener target. Gate ① — pre-screen by event type."""
        del fingerprint
        if event not in _TRIGGERING_EVENTS:
            return
        self._enqueue(workspace_id, ontology_id, operation_key)

    def request_rescan(
        self,
        ontology_id: str,
        operation_key: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> None:
        """Manual trigger. Same path as the event-driven enqueue."""
        self._enqueue(workspace_id, ontology_id, operation_key)

    # ------------------------------------------------------------ internals
    def _enqueue(
        self,
        workspace_id: Optional[str],
        ontology_id: str,
        operation_key: str,
    ) -> None:
        if not ontology_id or not operation_key:
            return
        key = (workspace_id, ontology_id, operation_key)
        with self._enqueued_lock:
            if not self._started or self._owner_loop is None:
                self._pending_before_start.add(key)
                return
            if key in self._enqueued:
                return  # Gate ② — queue dedup
            self._enqueued.add(key)
        self._owner_loop.call_soon_threadsafe(self._enqueue_on_loop, key)

    def _enqueue_on_loop(
        self,
        key: Tuple[Optional[str], str, str],
    ) -> None:
        queue = self._queue
        if queue is None or self._closed:
            with self._enqueued_lock:
                self._enqueued.discard(key)
            return
        with self._enqueued_lock:
            if key not in self._enqueued:
                self._enqueued.add(key)
        queue.put_nowait(key)

    async def _worker_loop(self) -> None:
        queue = self._queue
        if queue is None:
            raise RuntimeError("patcher worker started without a queue")
        while True:
            key = await queue.get()
            try:
                await run_sync_compat(
                    self._scan,
                    key[1],
                    key[2],
                    workspace_id=key[0],
                    task_name="operation-patcher-scan",
                    offload_kind="tool",
                )
            except Exception:
                logger.exception("patcher scan failed for %s/%s", key[1], key[2])
            finally:
                with self._enqueued_lock:
                    self._enqueued.discard(key)
                queue.task_done()

    async def _sweep_loop(self) -> None:
        wakeup = self._sweep_wakeup
        if wakeup is None:
            raise RuntimeError("patcher sweeper started without a wakeup event")
        while True:
            interval = max(60, int(self._current_config().sweep_interval_sec))
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=interval)
            except TimeoutError:
                pass
            wakeup.clear()
            try:
                keys = await run_sync_compat(
                    lambda: list(self._buffer.iter_op_dirs()),
                    task_name="operation-patcher-sweep",
                    offload_kind="tool",
                )
                for key in keys:
                    self._enqueue(key[0], key[1], key[2])
            except Exception:
                logger.exception("patcher sweep enumeration failed")

    def _wake_sweeper(self) -> None:
        loop = self._owner_loop
        wakeup = self._sweep_wakeup
        if loop is None or wakeup is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(wakeup.set)

    def _current_config(self) -> PatcherConfig:
        with self._cfg_lock:
            return replace(self._cfg)

    # ---------------------------------------------------------------- scan
    def _scan(
        self,
        ontology_id: str,
        operation_key: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> None:
        op_path = resolve_operation_md(ontology_id, operation_key, workspace_id=workspace_id)
        if op_path is None:
            logger.debug(
                "patcher: skip %s/%s — operation .md not found in any workspace",
                ontology_id,
                operation_key,
            )
            return
        staged_path = op_path.with_suffix(op_path.suffix + ".staged")
        meta_path = staged_path.with_suffix(staged_path.suffix + ".meta.json")
        ontology_version_dir = op_path.parent.parent
        ontology_dir = (
            ontology_version_dir.parent.parent
            if ontology_version_dir.parent.name == "versions"
            else ontology_version_dir
        )

        # Staged sidecars live inside the version payload and would invalidate
        # a deterministic package lock even when they are never applied.
        if is_deterministic_package_locked(ontology_version_dir):
            return

        # Gate ③.a — never overwrite a pending staged file
        if staged_path.exists():
            return

        # Gate ③.b — cooldown since last patch (recorded in meta sidecar of the
        # most recent applied snapshot, falling back to op_path mtime).
        if self._in_cooldown(ontology_id, operation_key, op_path, workspace_id=workspace_id):
            return

        # Gate ③.c — collect actionable proposals
        actionable = self._collect_actionable(ontology_id, operation_key, workspace_id=workspace_id)
        if not actionable:
            return

        # Gate ③.d — confirm the .md still exists at the resolved path
        if not op_path.exists():
            return

        # Build LLM call
        try:
            current_md = op_path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("patcher: unable to read %s", op_path, exc_info=True)
            return

        result = self._llm_merge(
            ontology_id=ontology_id,
            operation_key=operation_key,
            current_md=current_md,
            actionable=actionable,
        )
        if result is None:
            return

        new_md = (result.new_markdown or "").strip()
        if not new_md or new_md == current_md.strip():
            logger.info(
                "patcher: LLM produced no-op for %s/%s — skipping",
                ontology_id,
                operation_key,
            )
            return

        # Atomic-ish write of staged + meta sidecar
        merged_fps = self._normalize_merged_fps(result.incorporated_fingerprints, actionable)
        if not merged_fps:
            logger.info(
                "patcher: LLM declared no proposals incorporated for %s/%s",
                ontology_id,
                operation_key,
            )
            return

        try:
            with ontology_mutation_lock(ontology_dir):
                locked_op_path = resolve_operation_md(
                    ontology_id,
                    operation_key,
                    workspace_id=workspace_id,
                )
                if locked_op_path is None or locked_op_path != op_path:
                    return
                locked_version_dir = locked_op_path.parent.parent
                if is_deterministic_package_locked(locked_version_dir):
                    return
                staged_path = locked_op_path.with_suffix(locked_op_path.suffix + ".staged")
                meta_path = staged_path.with_suffix(staged_path.suffix + ".meta.json")
                if staged_path.exists():
                    return
                locked_current_md = locked_op_path.read_text(encoding="utf-8")
                if _sha256(locked_current_md) != _sha256(current_md):
                    logger.info(
                        "patcher: operation changed during merge for %s/%s — skipping",
                        ontology_id,
                        operation_key,
                    )
                    return
                staged_path.write_text(new_md, encoding="utf-8")
                meta_path.write_text(
                    json.dumps(
                        {
                            "ontology_id": ontology_id,
                            "workspace_id": workspace_id,
                            "operation_key": operation_key,
                            "fingerprints": merged_fps,
                            "created_at": time.time(),
                            "original_sha": _sha256(locked_current_md),
                            "change_summary": result.change_summary or "",
                            "auto_apply": bool(self._current_config().auto_apply),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        except Exception:
            logger.exception("patcher: failed to write staged for %s/%s", ontology_id, operation_key)
            return

        self._buffer.mark_promoted(
            ontology_id,
            operation_key,
            merged_fps,
            workspace_id=workspace_id,
        )

    # --------------------------------------------------------- gating utils
    def _in_cooldown(
        self,
        ontology_id: str,
        operation_key: str,
        op_path: Path,
        *,
        workspace_id: Optional[str] = None,
    ) -> bool:
        cooldown = max(0, int(self._current_config().cooldown_sec))
        if cooldown <= 0:
            return False
        del op_path
        applied_dir = self._buffer._applied_dir(  # noqa: SLF001
            ontology_id,
            operation_key,
            workspace_id=workspace_id,
        )
        if not applied_dir.exists():
            return False
        latest = 0.0
        for entry in applied_dir.glob("*.json"):
            try:
                latest = max(latest, entry.stat().st_mtime)
            except OSError:
                continue
        if latest <= 0:
            return False
        return (time.time() - latest) < cooldown

    def _collect_actionable(
        self,
        ontology_id: str,
        operation_key: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> List[Aggregate]:
        cfg = self._current_config()
        out: List[Aggregate] = []
        for agg in self._buffer.list_aggregates(
            ontology_id,
            operation_key,
            workspace_id=workspace_id,
            statuses={"pending"},
        ):
            if agg.rejection_count > cfg.max_rejection:
                continue
            sources = agg.sources or {}
            is_active = sources.get("active_chat", 0) > 0
            if is_active:
                # Active chat fast-track: any non-rejected proposal qualifies.
                out.append(agg)
                continue
            if agg.occurrences >= cfg.min_occurrences and agg.applied_count >= cfg.min_applied:
                out.append(agg)
        return out

    @staticmethod
    def _normalize_merged_fps(
        declared: List[str],
        actionable: List[Aggregate],
    ) -> List[str]:
        actionable_set = {a.fingerprint for a in actionable}
        declared_set = {fp for fp in (declared or []) if fp}
        # Only count fps the LLM both saw and confirmed. Fall back to all
        # actionable if the model returned nothing — assumes "merged everything".
        if not declared_set:
            return [a.fingerprint for a in actionable]
        resolved: List[str] = []
        for declared_fp in declared_set:
            if declared_fp in actionable_set:
                resolved.append(declared_fp)
                continue
            matches = [full_fp for full_fp in actionable_set if full_fp.startswith(declared_fp)]
            if len(matches) == 1:
                resolved.append(matches[0])
        return resolved

    # ------------------------------------------------------------------ LLM
    def _llm_merge(
        self,
        *,
        ontology_id: str,
        operation_key: str,
        current_md: str,
        actionable: List[Aggregate],
    ) -> Optional[StagedMergeOutput]:
        try:
            task_llm = get_llm_router().get_service(LLMPurpose.TASK)
        except Exception:
            logger.warning("patcher: task_llm unavailable; skipping merge", exc_info=True)
            return None
        if not task_llm or not getattr(task_llm, "is_configured", True):
            logger.info("patcher: task_llm not configured; skipping merge")
            return None
        messages = [
            {"role": "system", "content": PATCHER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": PATCHER_USER_TEMPLATE.format(
                    operation_key=operation_key,
                    ontology_id=ontology_id,
                    current_md=current_md,
                    proposal_count=len(actionable),
                    proposals_block=_format_proposals_block(actionable),
                ),
            },
        ]
        try:
            structured = task_llm.call(
                messages=messages,
                response_model=StagedMergeOutput,
                enable_reasoning=False,
                temperature=0.2,
                max_tokens=4096,
                request_label="patcher.merge",
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            logger.warning(
                "patcher: LLM merge failed for %s/%s: %s",
                ontology_id,
                operation_key,
                exc,
            )
            return None
        output = getattr(structured, "data", None)
        if not isinstance(output, StagedMergeOutput):
            logger.info("patcher: LLM returned no structured output for %s/%s", ontology_id, operation_key)
            return None
        return output


# -----------------------------------------------------------------------------
# Path resolution & staged metadata helpers (used by HTTP layer too)
# -----------------------------------------------------------------------------


def resolve_operation_md(
    ontology_id: str,
    operation_key: str,
    *,
    workspace_id: Optional[str] = None,
) -> Optional[Path]:
    """Walk all workspace roots and return the first matching op .md.

    The buffer is workspace-agnostic but operation files live under the active
    ontology version. We accept the first match because ontology ids are
    slugified and globally unique in practice.
    """
    if not ontology_id or not operation_key:
        return None
    if workspace_id:
        ontology_dir = get_workspace_paths(workspace_id).ontologies_dir / ontology_id
        try:
            version_dir = get_ontology_version_dir(ontology_dir, create=False) or ontology_dir
        except Exception:
            version_dir = ontology_dir
        candidate = version_dir / "operations" / "{}.md".format(operation_key)
        return candidate.resolve() if candidate.exists() else None
    root = get_workspaces_root()
    if not root.exists():
        return None
    for ws_dir in root.iterdir():
        if not ws_dir.is_dir():
            continue
        ontology_dir = ws_dir / "ontologies" / ontology_id
        try:
            version_dir = get_ontology_version_dir(ontology_dir, create=False) or ontology_dir
        except Exception:
            version_dir = ontology_dir
        candidate = version_dir / "operations" / "{}.md".format(operation_key)
        if candidate.exists():
            return candidate.resolve()
    return None


def staged_paths_for(
    ontology_id: str,
    operation_key: str,
    *,
    workspace_id: Optional[str] = None,
) -> Optional[Tuple[Path, Path]]:
    """Return (staged_path, meta_path) if the operation .md exists, else None."""
    op_path = resolve_operation_md(ontology_id, operation_key, workspace_id=workspace_id)
    if op_path is None:
        return None
    staged = op_path.with_suffix(op_path.suffix + ".staged")
    meta = staged.with_suffix(staged.suffix + ".meta.json")
    return staged, meta


def list_pending_staged(*, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Enumerate all staged patches awaiting review across workspaces.

    Used by the pending-badge endpoint and the cross-op review list.
    """
    out: List[Dict[str, Any]] = []
    root = get_workspaces_root()
    if not root.exists():
        return out
    if workspace_id:
        workspace_dirs = [get_workspace_paths(workspace_id).workspace_dir]
    else:
        workspace_dirs = [entry for entry in root.iterdir() if entry.is_dir()]
    for ws_dir in workspace_dirs:
        if not ws_dir.is_dir():
            continue
        ontologies_dir = ws_dir / "ontologies"
        if not ontologies_dir.exists():
            continue
        for onto_dir in ontologies_dir.iterdir():
            ops_dir = onto_dir / "operations"
            if not ops_dir.exists():
                continue
            for staged in ops_dir.glob("*.md.staged"):
                meta = staged.with_suffix(staged.suffix + ".meta.json")
                meta_data: Dict[str, Any] = {}
                if meta.exists():
                    try:
                        meta_data = json.loads(meta.read_text(encoding="utf-8") or "{}")
                    except Exception:
                        meta_data = {}
                out.append(
                    {
                        "ontology_id": onto_dir.name,
                        "operation_key": staged.name[: -len(".md.staged")],
                        "workspace_id": ws_dir.name,
                        "staged_path": str(staged),
                        "created_at": meta_data.get("created_at"),
                        "fingerprints": meta_data.get("fingerprints") or [],
                        "change_summary": meta_data.get("change_summary") or "",
                    }
                )
    return out


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
