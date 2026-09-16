# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Skill-evolution service — orchestrates calls to the external skill-evolver.

Pipeline:
  start()    : gate-check -> build baseline zip + trace -> POST /runs -> persist task
  poll_once(): worker calls this per tick -> GET /runs/{id} -> on done download +
               extract -> compute diff -> mark completed/no_change/failed
  diff()     : evolved artifact vs submit-time baseline (only changed files)
  drift()    : compare current active version vs submit-time snapshot
  approve()  : apply evolved artifact as new version or overwrite active
  reject()   : discard
  cancel()   : local cancel (worker stops polling; skill-evolver keeps running)

The skill-evolver HTTP client is constructor-injectable so tests can mock it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml
from agent.skill_evolution.models import (
    TERMINAL_STATUSES,
    DiffFileEntry,
    DiffResult,
    DriftStatus,
    TaskRecord,
)
from agent.skill_evolution.store import PendingTaskExists, SkillEvolutionStore
from config.system_config import load_system_config
from ontology.mutation_lock import ontology_mutation_lock
from ontology.ontologies import (
    OntologyManifest,
    ensure_ontology_versions,
    get_ontology_registry,
    get_ontology_version_dir,
    is_valid_ontology_version,
)
from persistence.execution_store import ExecutionStore
from utils.logging import get_logger
from workspace.workspaces import get_workspace_ontologies_root

logger = get_logger("skill_evolution.service")

RUNS_PATH = "/api/plugin/v1/trace2skill/runs"
MIN_TURNS = 2
_EVOLVED_OPS_RELPREFIX = "operations/"


def _utc_now() -> str:
    """Return the current UTC timestamp without sub-second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class GateError(ValueError):
    """Raised when a submit gate fails. message is user-facing."""


class SkillEvolverClient:
    """Thin HTTP client for the skill-evolver trace2skill plugin.

    Constructor-injectable so tests can substitute a fake. Production use hits
    the real service via ``requests``.
    """

    def __init__(self, base_url: str, timeout_seconds: int = 30) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = max(5, int(timeout_seconds))

    @staticmethod
    def _headers() -> Dict[str, str]:
        return {"Accept": "application/json"}

    def submit_run(
        self,
        trace_bytes: bytes,
        baseline_bytes: bytes,
        request_json: Dict[str, Any],
    ) -> str:
        """Submit trace and baseline bytes to the configured skill-evolver."""
        if not self._base_url:
            raise RuntimeError("skill-evolver base_url 未配置")
        files = {
            "trace_file": ("trace.json", trace_bytes, "application/json"),
            "baseline_file": ("baseline.zip", baseline_bytes, "application/zip"),
            "request": (
                "request.json",
                json.dumps(request_json, ensure_ascii=False).encode("utf-8"),
                "application/json",
            ),
        }
        response = requests.post(
            self._base_url + RUNS_PATH,
            files=files,
            headers=self._headers(),
            timeout=self._timeout,
        )
        if response.status_code not in (200, 201, 202):
            raise RuntimeError("skill-evolver 提交失败 HTTP {}: {}".format(response.status_code, response.text[:300]))
        data = response.json() or {}
        run_id = str(data.get("run_id") or "").strip()
        if not run_id:
            raise RuntimeError("skill-evolver 未返回 run_id: {}".format(data))
        return run_id

    def get_run_status(self, run_id: str) -> Dict[str, Any]:
        """Fetch the current status payload for a skill-evolver run."""
        response = requests.get(
            self._base_url + RUNS_PATH + "/" + run_id,
            headers=self._headers(),
            timeout=self._timeout,
        )
        if response.status_code == 404:
            return {"status": "failed", "error": "run not found"}
        if not response.ok:
            raise RuntimeError(
                "skill-evolver 状态查询失败 HTTP {}: {}".format(response.status_code, response.text[:300])
            )
        return response.json() or {}

    def download_artifact(self, run_id: str) -> bytes:
        """Download the evolved artifact for a completed run."""
        response = requests.get(
            self._base_url + RUNS_PATH + "/" + run_id + "/artifact",
            headers=self._headers(),
            timeout=self._timeout,
            stream=False,
        )
        if not response.ok:
            raise RuntimeError(
                "skill-evolver 产物下载失败 HTTP {}: {}".format(response.status_code, response.text[:300])
            )
        return response.content


# --------------------------------------------------------------------------- #
# Status interpretation
# --------------------------------------------------------------------------- #
_DONE_STATUSES = {"completed", "succeeded", "success", "done", "finished"}
_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}


def _interpret_status(status_payload: Dict[str, Any]) -> Tuple[str, str]:
    """Map skill-evolver status payload to (state, message).

    state ∈ {"running", "completed", "failed"}.
    """
    if not isinstance(status_payload, dict):
        return "failed", "skill-evolver 返回非 JSON 对象"
    status = str(status_payload.get("status") or "").strip().lower()
    if status_payload.get("error") and not status:
        return "failed", str(status_payload.get("error"))[:300]
    if status in _FAILED_STATUSES:
        return "failed", str(status_payload.get("error") or status_payload.get("message") or "进化失败")[:300]
    if status in _DONE_STATUSES:
        return "completed", ""
    return "running", ""


# --------------------------------------------------------------------------- #
# Diff helpers
# --------------------------------------------------------------------------- #
def _read_zip_members(zip_bytes: bytes) -> Dict[str, str]:
    """Read a zip into {relative_path: text_content} for text members."""
    from api.routes.skills import _read_skill_zip_text_members

    return _read_skill_zip_text_members(zip_bytes)


def _strip_archive_root(path: str) -> str:
    """Strip the leading directory component of a zip member path."""
    parts = path.split("/")
    if len(parts) > 1:
        return "/".join(parts[1:])
    return path


def _collect_ops_files(version_dir: Path) -> Dict[str, str]:
    """Read operations/index.md + operation documents under a version dir."""
    out: Dict[str, str] = {}
    ops_dir = version_dir / "operations"
    if ops_dir.is_dir():
        for f in sorted(ops_dir.glob("*.md")):
            rel = "operations/" + f.name
            try:
                out[rel] = f.read_text(encoding="utf-8")
            except OSError:
                continue
    return out


def _collect_version_files(version_dir: Path) -> Dict[str, bytes]:
    """Read every file under a version dir as {relative_path: bytes}.

    Mirrors `_build_ontology_zip_buffer`'s `version_dir.rglob("*")` packing
    scope, so the version sha covers exactly what the baseline zip contained
    and what approve will overwrite. skill-evolver may evolve operations today
    and scripts/objects tomorrow; hashing the whole version dir keeps
    sha-scope == baseline-scope == install-scope without maintaining a
    growing whitelist. Symlinks are skipped (same as the zip packer)."""
    out: Dict[str, bytes] = {}
    if not version_dir.is_dir():
        return out
    paths = [p for p in version_dir.rglob("*") if p.is_file() and not p.is_symlink()]
    for p in sorted(paths, key=lambda x: x.relative_to(version_dir).as_posix()):
        rel = p.relative_to(version_dir).as_posix()
        if rel == "manifest.yaml":
            continue
        try:
            out[rel] = p.read_bytes()
        except OSError:
            continue
    revision_manifest = _canonical_revision_manifest(version_dir)
    if revision_manifest is not None:
        out["manifest.yaml"] = revision_manifest
    return out


def _canonical_revision_manifest(version_dir: Path) -> Optional[bytes]:
    """Hash legacy backfill and its equivalent revision manifest identically."""

    manifest_path = version_dir / "manifest.yaml"
    manifest: Any = None
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            try:
                return manifest_path.read_bytes()
            except OSError:
                return None
    elif version_dir.parent.name == "versions":
        shared_manifest_path = version_dir.parent.parent / "manifest.yaml"
        if shared_manifest_path.is_file() and not shared_manifest_path.is_symlink():
            try:
                shared = yaml.safe_load(shared_manifest_path.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                return None
            if isinstance(shared, dict) and str(shared.get("schema_version") or "").strip() == "0.1":
                manifest = dict(shared)
                manifest["version"] = version_dir.name
                manifest.pop("active_version", None)
                manifest.pop("versions", None)
    if not isinstance(manifest, dict):
        return None
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _locate_evolved_root(extract_dir: Path) -> Path:
    """Find the ontology root inside the extracted artifact (has operations/)."""

    def has_ops(d: Path) -> bool:
        """Return whether a directory contains the expected operations index."""
        return (d / "operations" / "index.md").is_file()

    if has_ops(extract_dir):
        return extract_dir
    for child in extract_dir.iterdir():
        if child.is_dir() and has_ops(child):
            return child
    raise RuntimeError("进化产物结构异常：未找到 operations 目录")


def _normalize_eol(text: str) -> str:
    """Normalize line endings to ``\\n``.

    The baseline zip is built from on-disk files (Windows checkout → CRLF via
    git autocrlf), while the evolved artifact comes from skill-evolver (Linux
    → LF). Comparing raw strings makes CRLF/LF-only differences register as
    "modified" and makes jsdiff's diffLines (which keeps terminators in each
    line token) render the whole file as delete+add. Normalizing both sides
    before compare/count/store fixes both. Deliberately NOT applied to
    _compute_version_sha — that path reads both sides the same way, so it
    is already self-consistent; changing it would break drift detection for
    in-flight tasks whose stored sha was computed on raw bytes.
    """
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _line_diff_counts(original: str, evolved: str) -> Tuple[int, int]:
    """Count added and removed lines in a normalized text diff."""
    diff = list(
        difflib.unified_diff(
            original.splitlines(),
            evolved.splitlines(),
            lineterm="",
        )
    )
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return added, removed


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class SkillEvolutionService:
    def __init__(
        self,
        store: SkillEvolutionStore,
        *,
        client: Optional[SkillEvolverClient] = None,
    ) -> None:
        self._store = store
        self._client_override = client
        # operations-sha cache for detect_drift: keyed by (workspace, version_dir)
        # + max mtime. detect_drift runs on every GET /tasks/<id> and /diff, and
        # the frontend polls /tasks/<id> every 10s while a task awaits approval;
        # without caching each poll re-reads + hashes every operations file.
        self._sha_cache: Dict[Tuple[str, str], Tuple[float, str]] = {}

    def get_task(self, workspace_id: str, task_id: str) -> Optional[TaskRecord]:
        """Return one task through the service boundary."""
        return self._store.get_task(workspace_id, task_id)

    def list_tasks(self, workspace_id: str, **filters: Any) -> List[TaskRecord]:
        """List tasks through the service boundary."""
        return self._store.list_tasks(workspace_id, **filters)

    # ----------------------------------------------------------- config/client
    @staticmethod
    def _config() -> Dict[str, Any]:
        return load_system_config().get("skill_evolution") or {}

    def _client(self) -> SkillEvolverClient:
        if self._client_override is not None:
            return self._client_override
        cfg = self._config()
        return SkillEvolverClient(
            base_url=cfg.get("base_url", ""),
            timeout_seconds=cfg.get("timeout_seconds", 30),
        )

    def is_enabled(self) -> bool:
        """Return whether skill evolution is enabled in system configuration."""
        return bool(self._config().get("enabled", False))

    # ------------------------------------------------------------- ontology
    @staticmethod
    def _resolve_ontology(workspace_id: str, ontology_id: str) -> OntologyManifest:
        registry = get_ontology_registry(workspace_id=workspace_id)
        registry.refresh()
        ontology = registry.get_ontology(ontology_id)
        if ontology is None:
            raise GateError("本体 {} 不存在".format(ontology_id))
        if not ontology.enabled:
            raise GateError("本体 {} 已禁用".format(ontology_id))
        return ontology

    @staticmethod
    def _ontology_dir(workspace_id: str, ontology_id: str) -> Path:
        return get_workspace_ontologies_root(workspace_id) / ontology_id

    @staticmethod
    def _ontology_root(ontology: OntologyManifest) -> Path:
        """Return the installed package root for versioned and legacy layouts."""
        version_dir = Path(ontology.ontology_dir)
        if version_dir.parent.name == "versions":
            return version_dir.parent.parent
        return Path(ontology.manifest_path).parent

    def _active_version_dir(self, workspace_id: str, ontology: OntologyManifest) -> Path:
        version_dir = get_ontology_version_dir(self._ontology_root(ontology), create=False)
        if version_dir is None:
            raise GateError("本体 {} 的激活版本目录不存在".format(ontology.id))
        return version_dir

    @staticmethod
    def _assert_full_snapshot(evolved_root: Path, reference_dir: Path, skip_names: set) -> None:
        """Reject partial snapshots before the rmtree+copy install.

        install deletes the target version dir then copies evolved_root's
        contents, so any top-level component present in the active version but
        missing from evolved_root would be silently lost. Require evolved_root
        to contain every top-level entry (dir or file) the reference has,
        excluding manifest.yaml/versions (those are handled separately by
        install). Per-file deletions *inside* a component are still allowed —
        we can't distinguish an intentional deletion from an omission, and the
        component-level check guards the data-loss case (a whole tree gone)."""

        def top_entries(d: Path) -> set:
            """Return non-excluded top-level entries for a snapshot root."""
            if not d.is_dir():
                return set()
            return {p.name for p in d.iterdir() if p.name not in skip_names}

        missing = sorted(top_entries(reference_dir) - top_entries(evolved_root))
        if missing:
            raise ValueError("进化产物不是完整快照，缺少 {}，拒绝落盘以防数据丢失".format(missing))

    @staticmethod
    def _compute_version_sha(version_dir: Path) -> str:
        """Whole-version sha: hashes every file under the version dir (same
        scope as the baseline zip and approve's rmtree+copy install). Keeps
        sha-scope == baseline-scope == install-scope so drift catches any
        approval-time edit, including objects/scripts once skill-evolver
        evolves them."""
        files = _collect_version_files(version_dir)
        h = hashlib.sha256()
        for rel in sorted(files):
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(files[rel])
            h.update(b"\0")
        return h.hexdigest()

    @staticmethod
    def _version_dir_mtime(version_dir: Path) -> float:
        """Max mtime of the version dir + every file under it. Used as the
        sha-cache invalidation key: a content edit bumps mtime, so the cached
        sha is recomputed and drift is still detected. Walks all files (not
        just operations) to match the whole-version sha scope."""
        mtimes = [version_dir.stat().st_mtime]
        if version_dir.is_dir():
            for p in version_dir.rglob("*"):
                if p.is_file():
                    mtimes.append(p.stat().st_mtime)
        return max(mtimes)

    def _compute_version_sha_cached(self, workspace_id: str, version_dir: Path) -> str:
        """Version sha with mtime-based caching. start() uses the uncached
        _compute_version_sha for the submit-time snapshot (one-shot); the
        cached variant serves the hot detect_drift path."""
        key = (workspace_id, str(version_dir))
        mtime = self._version_dir_mtime(version_dir)
        cached = self._sha_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        sha = self._compute_version_sha(version_dir)
        self._sha_cache[key] = (mtime, sha)
        return sha

    # ------------------------------------------------------------------ gates
    def _validate_gates(
        self,
        workspace_id: str,
        ontology: OntologyManifest,
        history: Optional[Dict[str, Any]],
        session_id: str,
    ) -> None:
        # Gate 2: COMPLETED turns with user_request >= 2.
        # Exclude in-progress (status="running") turns — clicking mid-stream
        # would otherwise count a partial turn (user question, no answer yet).
        if history is None:
            raise GateError("当前会话无执行记录，无法发起进化")
        turns = [
            t
            for t in history.get("turns", [])
            if str(t.get("user_request") or "").strip() and t.get("status") != "running"
        ]
        if len(turns) < MIN_TURNS:
            raise GateError(
                "当前对话只有 {} 轮已完成提问，至少需要 {} 轮才能发起进化（请等待当前回答完成）".format(
                    len(turns), MIN_TURNS
                )
            )

        # Gate 3: version alignment (multi-version ontologies only).
        # Enforced STRICTLY only when turns carry version stamps (sessions that
        # had a selected ontology). For tool-based sessions (agent loaded the
        # ontology via ontology_context, no session-level selection) there are
        # no stamps to verify — skip alignment; the approval-time drift check
        # still guards against later changes.
        active_version = str(ontology.active_version or "").strip()
        versions = list(ontology.versions or [])
        has_multi_versions = len([v for v in versions if str(v or "").strip()]) > 1
        if has_multi_versions:
            turn_versions = [
                str(t.get("ontology_version") or "").strip()
                for t in turns
                if str(t.get("ontology_version") or "").strip()
            ]
            if turn_versions:
                mismatched = [v for v in turn_versions if v != active_version]
                if mismatched:
                    distinct = set(turn_versions)
                    if len(distinct) > 1:
                        # Session itself spanned multiple versions (active changed mid-session)
                        raise GateError(
                            "本会话跨多个本体版本（{}），请新开对话后再发起进化".format(" → ".join(sorted(distinct)))
                        )
                    else:
                        # Session's version ≠ current active (stale after approval/version switch)
                        raise GateError(
                            "本会话基于版本 {}，当前激活版本为 {}，请切回该版本或新开对话后再发起进化".format(
                                turn_versions[0], active_version
                            )
                        )

        # Gate 4: no pending for this ontology
        if self._store.has_pending(workspace_id, ontology.id):
            raise GateError("该本体已有正在进行的进化任务，请先完成审批或取消")

    # ----------------------------------------------------------- ontology infer
    @staticmethod
    def _infer_ontology_from_trace(history: Optional[Dict[str, Any]]) -> Optional[str]:
        """Infer the target ontology from the trace when the session had no
        session-level selected_ontology_id (tool-based flow: agent loaded the
        ontology via ontology_context). Returns the ontology_id if exactly one
        distinct ontology is referenced, else None."""
        if not isinstance(history, dict):
            return None
        ontos: set = set()
        # 1) read_documents ledger (structured, has ontology_id attribution)
        rd = history.get("read_documents") or {}
        by_path = rd.get("by_path") if isinstance(rd, dict) else None
        if isinstance(by_path, dict):
            for rec in by_path.values():
                if isinstance(rec, dict) and rec.get("ontology_id"):
                    ontos.add(str(rec["ontology_id"]))
        if len(ontos) == 1:
            return ontos.pop()
        # 2) fallback: ontology_context tool calls in session_history
        for m in history.get("session_history") or []:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name") or tc.get("name")
                if fn != "ontology_context":
                    continue
                raw = (tc.get("function") or {}).get("arguments") or tc.get("arguments") or ""
                try:
                    a = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except (json.JSONDecodeError, TypeError):
                    a = {}
                oid = a.get("ontology_id")
                if oid:
                    ontos.add(str(oid))
        if len(ontos) == 1:
            return ontos.pop()
        return None

    # ------------------------------------------------------------------ start
    def start(
        self,
        *,
        workspace_id: str,
        ontology_id: Optional[str],
        session_id: str,
        triggered_by: str,
        execution_store: ExecutionStore,
    ) -> TaskRecord:
        """Create a skill-evolution task after validating submit gates."""
        if not self.is_enabled():
            raise GateError("本体进化功能未启用")

        history = execution_store.get_session_history(session_id)

        # Determine the target ontology. ontology_id (from the chat UI's
        # session-level selection) takes precedence; otherwise infer from the
        # trace (tool-based flow where the agent loaded the ontology via
        # ontology_context).
        target_id = str(ontology_id or "").strip()
        if not target_id:
            target_id = self._infer_ontology_from_trace(history) or ""
            if not target_id:
                raise GateError("无法确定进化目标本体：该会话未选定本体，且 trace 中未明确引用单一本体")
        ontology = self._resolve_ontology(workspace_id, target_id)
        self._validate_gates(workspace_id, ontology, history, session_id)

        version_dir = self._active_version_dir(workspace_id, ontology)
        active_version = str(ontology.active_version or "").strip()
        version_sha = self._compute_version_sha(version_dir)

        # Build baseline zip (reuse ontology download packer). Lazy import to
        # avoid a circular import with routes/__init__ at module load time.
        from api.routes.ontologies import (
            ONTOLOGY_MAX_ZIP_BYTES,
            _build_ontology_zip_buffer,
        )

        zip_buffer = _build_ontology_zip_buffer(self._ontology_root(ontology), version_dir, version=active_version)
        baseline_bytes = zip_buffer.getvalue()
        if len(baseline_bytes) > ONTOLOGY_MAX_ZIP_BYTES:
            raise GateError("本体基线过大，超过上传上限")

        # Trace (the session execution.json, already loaded above).
        # Exclude any in-progress (status="running") turn so a mid-stream click
        # doesn't send a partial turn (user question with no assistant answer).
        trace_history = dict(history)
        trace_history["turns"] = [t for t in history.get("turns", []) if t.get("status") != "running"]
        trace_bytes = json.dumps(trace_history, ensure_ascii=False).encode("utf-8")

        # max_iterations controls the skill-evolver evolution loop (1-30, server
        # default 15). Read from config so admins can tune it without code changes.
        cfg = self._config()
        max_iterations = int(cfg.get("max_iterations") or 15)
        request_json = {
            "artifact_type": "ontology",
            "target_name": ontology.id,
            "trace_format": "auto",
            "evolution": {
                "batch": True,
                "language": "Chinese",
                "max_iterations": max_iterations,
            },
            "evaluation": {"enabled": False},
        }

        # Audit log for trace export (data leaves the system)
        logger.info(
            "skill-evolution trace export: user=%s workspace=%s session=%s ontology=%s "
            "skill_evolver_url=%s trace_bytes=%d",
            triggered_by,
            workspace_id,
            session_id,
            ontology.id,
            self._config().get("base_url", ""),
            len(trace_bytes),
        )

        # Submit (short-timeout sync; on failure do NOT occupy pending)
        client = self._client()
        try:
            run_id = client.submit_run(trace_bytes, baseline_bytes, request_json)
        except Exception as exc:
            logger.warning("skill-evolution submit failed: %s", exc)
            raise GateError("提交进化失败：{}".format(str(exc)[:200])) from exc

        # Deterministic task_id from run_id (Python's str hash is randomized per
        # process via PYTHONHASHSEED — unsuitable for an identifier).
        run_id_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
        task_id = "evo_{}_{}".format(int(time.time()), run_id_hash)
        now = _utc_now()
        record = TaskRecord(
            task_id=task_id,
            workspace_id=workspace_id,
            ontology_id=ontology.id,
            ontology_name=ontology.name,
            session_id=session_id,
            triggered_by=triggered_by,
            run_id=run_id,
            status="submitted",
            active_version=active_version,
            version_sha=version_sha,
            created_at=now,
            updated_at=now,
            stage="evolving",
            trace_size_bytes=len(trace_bytes),
        )
        self._store.save_baseline(workspace_id, task_id, baseline_bytes)
        # Authoritative gate-4 check: closes the check-then-act race between
        # has_pending (fast-fail in _validate_gates) and create_task. Two
        # concurrent /start calls can both pass gate4 then both reach here; the
        # later one abandons its skill-evolver run (same as a local cancel).
        try:
            self._store.create_task_if_no_pending(record)
        except PendingTaskExists as exc:
            raise GateError(str(exc)) from exc
        return record

    # ------------------------------------------------------------- worker poll
    def poll_once(self, record: TaskRecord) -> TaskRecord:
        """Advance one in-flight task. Called by the worker each tick."""
        if record.status not in ("submitted", "running", "downloading"):
            return record
        if record.status == "submitted":
            record.status = "running"
            record.stage = "evolving"
            self._store.save_task(record)

        try:
            status_payload = self._client().get_run_status(record.run_id)
        except Exception as exc:
            logger.warning("skill-evolution poll status failed for %s: %s", record.task_id, exc)
            return record  # transient; retry next tick (timeout handled by worker)

        state, message = _interpret_status(status_payload)
        if state == "running":
            return record
        if state == "failed":
            return self.fail_task(record, stage="evolution", message=message or "skill-evolver 进化失败")

        # completed -> download + extract + diff
        return self._complete(record)

    def _complete(self, record: TaskRecord) -> TaskRecord:
        record.status = "downloading"
        record.stage = "downloading"
        self._store.save_task(record)
        try:
            artifact_bytes = self._client().download_artifact(record.run_id)
        except Exception as exc:
            logger.warning("skill-evolution artifact download failed for %s: %s", record.task_id, exc)
            return self.fail_task(record, stage="download", message="下载进化产物失败：{}".format(str(exc)[:200]))

        temp_dir = self._store.ensure_temp_dir(record.workspace_id, record.task_id)
        # Clear any half-extracted residue before extracting.
        for child in list(temp_dir.iterdir()):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        from api.routes.ontologies import _safe_extract_zip  # lazy import

        error = _safe_extract_zip(artifact_bytes, temp_dir)
        if error:
            self._store.clean_temp(record.workspace_id, record.task_id)
            return self.fail_task(record, stage="extract", message="解压进化产物失败：{}".format(error))

        try:
            evolved_root = _locate_evolved_root(temp_dir)
        except Exception as exc:
            self._store.clean_temp(record.workspace_id, record.task_id)
            return self.fail_task(record, stage="extract", message=str(exc))

        try:
            diff = self.compute_diff(record)
        except Exception as exc:
            logger.exception("skill-evolution compute_diff failed for %s", record.task_id)
            self._store.clean_temp(record.workspace_id, record.task_id)
            return self.fail_task(record, stage="diff", message="计算 diff 失败：{}".format(str(exc)[:200]))

        if diff.is_empty:
            record.status = "no_change"
            record.stage = ""
            record.change_summary = "本次进化未产生任何变更"
            self._store.save_task(record)
            self._store.finalize_artifacts(record)
            return record

        record.status = "completed"
        record.stage = ""
        record.change_summary = diff.change_summary
        self._store.save_task(record)
        return record

    def fail_task(self, record: TaskRecord, *, stage: str, message: str) -> TaskRecord:
        """Mark an in-flight task failed and clean its temporary artifacts."""
        # Re-read the latest on-disk state so we never clobber a concurrent
        # terminal decision (approve/reject/cancel) made by a route.
        latest = self._store.get_task(record.workspace_id, record.task_id)
        if latest is not None:
            record = latest
        if record.status in TERMINAL_STATUSES:
            return record
        record.status = "failed"
        record.failed_stage = stage
        record.error_message = message
        record.stage = ""
        self._store.save_task(record)
        self._store.finalize_artifacts(record)
        return record

    # ------------------------------------------------------------------- diff
    def get_diff(self, record: TaskRecord) -> DiffResult:
        """Return the diff for a task. For terminal tasks (approved/rejected/."""
        # Details: Return the diff for a task. For terminal tasks (approved/rejected/ cancelled) temp+baseline are
        # cleaned, so serve the diff persisted in history; otherwise compute from temp vs baseline.
        if record.status in TERMINAL_STATUSES:
            hist = self._store.get_history(record.workspace_id, record.task_id)
            stored = (hist or {}).get("diff")
            if isinstance(stored, dict):
                return DiffResult(
                    task_id=stored.get("task_id", record.task_id),
                    ontology_id=stored.get("ontology_id", record.ontology_id),
                    change_summary=stored.get("change_summary", ""),
                    files=[
                        DiffFileEntry(
                            path=f.get("path", ""),
                            change_type=f.get("change_type", "modified"),
                            original=f.get("original", ""),
                            evolved=f.get("evolved", ""),
                            added_lines=int(f.get("added_lines", 0) or 0),
                            removed_lines=int(f.get("removed_lines", 0) or 0),
                        )
                        for f in (stored.get("files") or [])
                    ],
                    is_empty=bool(stored.get("is_empty", False)),
                )
        return self.compute_diff(record)

    def compute_diff(self, record: TaskRecord) -> DiffResult:
        """Compute the persisted baseline-to-evolved operation diff."""
        baseline_path = self._store.baseline_path(record.workspace_id, record.task_id)
        baseline_files: Dict[str, str] = {}
        if baseline_path.exists():
            for member_path, content in _read_zip_members(baseline_path.read_bytes()).items():
                rel = _strip_archive_root(member_path)
                if rel.startswith(_EVOLVED_OPS_RELPREFIX):
                    baseline_files[rel] = content

        temp_dir = self._store.temp_path(record.workspace_id, record.task_id)
        evolved_files: Dict[str, str] = {}
        if temp_dir.exists():
            try:
                evolved_root = _locate_evolved_root(temp_dir)
                evolved_files = _collect_ops_files(evolved_root)
            except Exception:
                evolved_files = {}

        changed: List[DiffFileEntry] = []
        all_paths = sorted(set(baseline_files) | set(evolved_files))
        for rel in all_paths:
            original = _normalize_eol(baseline_files.get(rel, ""))
            evolved = _normalize_eol(evolved_files.get(rel, ""))
            if original == evolved:
                continue
            if not original and evolved:
                change_type = "added"
            elif original and not evolved:
                change_type = "deleted"
            else:
                change_type = "modified"
            added, removed = _line_diff_counts(original, evolved)
            changed.append(
                DiffFileEntry(
                    path=rel,
                    change_type=change_type,
                    original=original,
                    evolved=evolved,
                    added_lines=added,
                    removed_lines=removed,
                )
            )

        added_n = sum(1 for c in changed if c.change_type == "added")
        modified_n = sum(1 for c in changed if c.change_type == "modified")
        deleted_n = sum(1 for c in changed if c.change_type == "deleted")
        if changed:
            summary = "本次进化改动 {} 个文件：新增 {}，修改 {}，删除 {}".format(
                len(changed), added_n, modified_n, deleted_n
            )
        else:
            summary = "本次进化未产生任何变更"

        return DiffResult(
            task_id=record.task_id,
            ontology_id=record.ontology_id,
            change_summary=summary,
            files=changed,
            is_empty=not changed,
        )

    # ------------------------------------------------------------------ drift
    def detect_drift(self, record: TaskRecord) -> DriftStatus:
        """Check whether the submitted ontology changed before approval."""
        try:
            ontology = self._resolve_ontology(record.workspace_id, record.ontology_id)
        except GateError:
            return DriftStatus(
                drifted=True,
                reason="ontology_missing",
                message="本体已不存在",
                submit_active_version=record.active_version,
                current_active_version="",
            )
        current_active = str(ontology.active_version or "").strip()
        if current_active != record.active_version:
            return DriftStatus(
                drifted=True,
                reason="active_version_switched",
                message="激活版本已切换（提交时为 {}，现为 {}），请重新发起或切回旧版本".format(
                    record.active_version, current_active
                ),
                submit_active_version=record.active_version,
                current_active_version=current_active,
            )
        version_dir = self._active_version_dir(record.workspace_id, ontology)
        current_sha = self._compute_version_sha_cached(record.workspace_id, version_dir)
        if current_sha != record.version_sha:
            return DriftStatus(
                drifted=True,
                reason="content_modified",
                message="本体在审批期间被修改（可能由手动编辑或运算演进 apply 引起），建议重新发起进化",
                submit_active_version=record.active_version,
                current_active_version=current_active,
            )
        return DriftStatus(
            drifted=False,
            reason="clean",
            submit_active_version=record.active_version,
            current_active_version=current_active,
        )

    # ---------------------------------------------------------------- approve
    def approve(
        self,
        record: TaskRecord,
        *,
        mode: str,
        version_name: Optional[str],
        decided_by: str,
    ) -> TaskRecord:
        """Approve and install an evolved ontology snapshot."""
        if record.status != "completed":
            raise ValueError("仅待审批任务可执行审批")
        if mode not in ("overwrite", "new_version"):
            raise ValueError("mode 必须是 overwrite 或 new_version")

        drift = self.detect_drift(record)
        if drift.drifted:
            raise ValueError(drift.message)

        ontology = self._resolve_ontology(record.workspace_id, record.ontology_id)
        ontology_dir = self._ontology_root(ontology)
        temp_dir = self._store.temp_path(record.workspace_id, record.task_id)
        evolved_root = _locate_evolved_root(temp_dir)

        decision = "approve_overwrite" if mode == "overwrite" else "approve_new_version"
        new_version = ""

        # Lazy import to avoid circular import with routes/__init__.
        from api.routes.ontologies import (
            ONTOLOGY_VERSIONED_SKIP_NAMES,
            _install_ontology_version_package,
            _overwrite_ontology_active_version_package,
        )

        with ontology_mutation_lock(ontology_dir):
            # Re-check drift after acquiring the same lock used by every other
            # ontology writer. This closes the validation-to-install race.
            locked_drift = self.detect_drift(record)
            if locked_drift.drifted:
                raise ValueError(locked_drift.message)
            ontology = self._resolve_ontology(record.workspace_id, record.ontology_id)
            ontology_dir = self._ontology_root(ontology)

            # Full-snapshot guard: install rmtree's the target version dir then
            # copies evolved_root's contents, so a partial snapshot (e.g. missing
            # objects/ because skill-evolver returned only operations) would
            # silently delete the missing component. Require evolved_root to
            # contain every top-level component the active version has.
            self._assert_full_snapshot(
                evolved_root,
                self._active_version_dir(record.workspace_id, ontology),
                ONTOLOGY_VERSIONED_SKIP_NAMES,
            )

            if mode == "new_version":
                default_name = "evol-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                target_version = str(version_name or default_name).strip()
                if not is_valid_ontology_version(target_version):
                    raise ValueError("版本号不合法，仅支持 [A-Za-z0-9][A-Za-z0-9._-]*")
                manifest = ensure_ontology_versions(ontology_dir)
                existing_versions = [str(v).strip() for v in (manifest.get("versions") or []) if str(v or "").strip()]
                if target_version in existing_versions:
                    raise ValueError("版本 {} 已存在，请换一个版本号".format(target_version))
                _install_ontology_version_package(
                    source_root=evolved_root,
                    ontology_dir=ontology_dir,
                    ontology_id=ontology.id,
                    ontology_name=ontology.name,
                    ontology_description=ontology.description,
                    target_version=target_version,
                    existed=True,
                    workspace_id=record.workspace_id,
                    mutation_audit={
                        "operator": {"type": "user", "id": decided_by[:128]} if decided_by else None,
                        "request_id": (record.run_id or record.task_id)[:128],
                    },
                    mutation_trigger="admin",
                )
                new_version = target_version
            else:
                _overwrite_ontology_active_version_package(
                    source_root=evolved_root,
                    ontology_dir=ontology_dir,
                    ontology_id=ontology.id,
                    workspace_id=record.workspace_id,
                    mutation_audit={
                        "operator": {"type": "user", "id": decided_by[:128]} if decided_by else None,
                        "request_id": (record.run_id or record.task_id)[:128],
                    },
                    mutation_trigger="admin",
                )

        record.status = "approved"
        record.decision = decision
        record.decided_by = decided_by
        record.decided_at = _utc_now()
        record.new_version = new_version
        record.stage = ""
        self._store.save_task(record)

        # Persist approval history with the FULL diff (original/evolved text)
        # so the diff view still works after temp+baseline are finalized away.
        diff = self.compute_diff(record)
        self._store.save_history(
            record.workspace_id,
            record.task_id,
            {
                "task_id": record.task_id,
                "ontology_id": record.ontology_id,
                "session_id": record.session_id,
                "run_id": record.run_id,
                "triggered_by": record.triggered_by,
                "decided_by": decided_by,
                "decided_at": record.decided_at,
                "decision": decision,
                "new_version": new_version,
                "active_version": record.active_version,
                "evolve_created_at": record.created_at,
                "change_summary": diff.change_summary,
                "diff": diff.to_dict(),
            },
        )
        self._store.finalize_artifacts(record)
        return record

    # ----------------------------------------------------------------- reject
    def reject(self, record: TaskRecord, *, decided_by: str) -> TaskRecord:
        """Reject a completed evolution task and retain its decision history."""
        if record.status != "completed":
            raise ValueError("仅待审批任务可执行拒绝")
        record.status = "rejected"
        record.decision = "reject"
        record.decided_by = decided_by
        record.decided_at = _utc_now()
        record.stage = ""
        self._store.save_task(record)
        # Persist the full diff before finalize cleans temp+baseline, so the
        # diff view still works for the rejected task.
        diff = self.compute_diff(record)
        self._store.save_history(
            record.workspace_id,
            record.task_id,
            {
                "task_id": record.task_id,
                "ontology_id": record.ontology_id,
                "session_id": record.session_id,
                "run_id": record.run_id,
                "triggered_by": record.triggered_by,
                "decided_by": decided_by,
                "decided_at": record.decided_at,
                "decision": "reject",
                "active_version": record.active_version,
                "evolve_created_at": record.created_at,
                "change_summary": diff.change_summary,
                "diff": diff.to_dict(),
            },
        )
        self._store.finalize_artifacts(record)
        return record

    # ----------------------------------------------------------------- cancel
    def cancel(self, record: TaskRecord, *, decided_by: str) -> TaskRecord:
        """Cancel an in-flight evolution task and clean temporary artifacts."""
        if record.status not in ("submitted", "running", "downloading"):
            raise ValueError("仅进行中的任务可取消")
        record.status = "cancelled"
        record.decided_by = decided_by
        record.decided_at = _utc_now()
        record.stage = ""
        self._store.save_task(record)
        self._store.finalize_artifacts(record)
        return record

    # ----------------------------------------------------------------- delete
    def delete_task(self, record: TaskRecord) -> None:
        """Delete a terminal task's record + history. Does not revert applied."""
        # Details: Delete a terminal task's record + history. Does not revert applied ontology changes (an approved
        # task's version stays on the ontology).
        if record.status not in TERMINAL_STATUSES:
            raise ValueError("仅终态任务可删除；进行中的任务请先取消")
        self._store.delete_task(record.workspace_id, record.task_id)

    # --------------------------------------------------------- restart recover
    def mark_inflight_interrupted(self, workspace_id: str) -> int:
        """On worker startup, mark in-flight tasks as interrupted (user re-initiates)."""
        count = 0
        for record in self._store.list_tasks(workspace_id):
            if record.status in ("submitted", "running", "downloading"):
                record.status = "interrupted"
                record.stage = ""
                record.error_message = "服务重启，任务中断，请重新发起"
                self._store.save_task(record)
                self._store.finalize_artifacts(record)
                count += 1
        return count
