"""Contract jobs (section 9.2) without a job table: the id says what was started and when.

A job id is ``{action}.{kbId}.{startedMs}``; its state is read back from DA:

- queue actions (DA enqueues documents and returns at once): from the
  documents' statuses and ``updated_at`` relative to the start;
- ``preprocess`` (a DA agent run): from DA's preprocess status;
- request actions (DA answers only when done): from the adapter's own record
  of the running request, so after a restart their outcome is unknown.

Times are compared on DA's clock: the start is taken from DA's ``Date`` header.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from kb_provider_kit import Ids

# DA document statuses of a document that is being worked on
PROCESSING = frozenset({"parsing", "compiling", "indexing", "linking", "quality_audit"})
QUEUE_ACTIONS = frozenset({"process_pending", "reindex"})
REQUEST_ACTIONS = frozenset({"regenerate_summaries", "rebuild_embeddings"})
AGENT_ACTIONS = frozenset({"preprocess"})
JOB_ACTIONS = QUEUE_ACTIONS | REQUEST_ACTIONS | AGENT_ACTIONS
# a preprocess run DA reports as started this long after the job's start is a later run
SAME_RUN = timedelta(minutes=2)
# DA may not list a just-started preprocess run as running yet
START_GRACE = timedelta(seconds=20)


def parse_time(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def started_at(date_header: str | None) -> datetime:
    """The start of an action on DA's clock (its ``Date`` header, whole seconds, so one second earlier)."""

    try:
        return parsedate_to_datetime(date_header or "") - timedelta(seconds=1)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc) - timedelta(seconds=1)


def job_id(action: str, kb_id: str, started: datetime) -> str:
    return f"{action}.{kb_id}.{int(started.timestamp() * 1000)}"


def parse_job_id(raw: str) -> tuple[str, str, datetime] | None:
    """(action, raw kb id, start) of a raw job id; ``None`` for anything this adapter could not have issued."""

    action, _, rest = raw.partition(".")
    kb_id, _, millis = rest.rpartition(".")
    if action not in JOB_ACTIONS or not kb_id or not millis.isdigit():
        return None
    return action, kb_id, datetime.fromtimestamp(int(millis) / 1000, tz=timezone.utc)


def view(
    raw_id: str, action: str, kb_id: str, started: datetime, ids: Ids, *, status: str, progress: float | None = None,
    message: str | None = None, cancellable: bool = False, has_report: bool = False,
    finished: datetime | None = None,
) -> dict:
    return {
        "id": ids.out(raw_id), "kb_id": ids.out(kb_id), "doc_id": None, "action": action, "status": status,
        "progress": progress, "message": message, "cancellable": cancellable, "has_report": has_report,
        "started_at": iso(started), "finished_at": iso(finished),
    }


def _waiting(action: str, doc: dict, started: datetime) -> bool:
    """Enqueued by the action at ``started`` and not picked up yet (still in its old state)."""

    status = doc.get("status")
    updated = parse_time(doc.get("updated_at"))
    old = updated is None or updated < started
    if action == "reindex":
        # DA re-enqueues every ready document
        return status == "ready" and old
    # process-all enqueues uploaded and failed documents
    return status == "uploaded" or (status == "error" and old)


def queue_state(action: str, started: datetime, docs: list[dict]) -> dict:
    """Status of a queue action from the knowledge base's documents.

    Documents created after the start are not part of it; one that was picked
    up shows a processing status, one that is done was updated after the start.
    """

    waiting = active = finished = failed = 0
    last: datetime | None = None
    for doc in docs:
        created = parse_time(doc.get("created_at"))
        if created is not None and created >= started:
            continue
        status = doc.get("status")
        updated = parse_time(doc.get("updated_at"))
        if status in PROCESSING:
            active += 1
        elif _waiting(action, doc, started):
            waiting += 1
        elif status in ("ready", "error") and updated is not None and updated >= started:
            finished += 1
            failed += status == "error"
            last = max(last, updated) if last else updated
    total = waiting + active + finished
    if total == 0:
        return {"status": "completed", "progress": 1.0, "message": "没有需要处理的文档", "finished": started}
    if waiting or active:
        return {"status": "running" if active or finished else "queued", "progress": round(finished / total, 3),
                "message": f"{finished}/{total} 篇", "finished": None}
    message = f"已处理 {finished} 篇" + (f"，{failed} 篇失败" if failed else "")
    return {"status": "failed" if failed == total else "completed", "progress": 1.0, "message": message,
            "finished": last}


def preprocess_state(started: datetime, status: dict, now: datetime) -> dict:
    """Status of a preprocess run started at ``started`` from DA's ``preprocess/status``."""

    running_since = parse_time(status.get("startedAt"))
    if status.get("running") and (running_since is None or running_since <= started + SAME_RUN):
        activity = status.get("currentActivity")
        return {"status": "running", "message": activity or None, "cancellable": True, "finished": None,
                "has_report": False}
    done = parse_time(status.get("lastPreprocessedAt"))
    if done is not None and done >= started:
        return {"status": "completed", "message": None, "cancellable": False, "finished": done,
                "has_report": bool(status.get("lastReport"))}
    if status.get("lastStatus") == "failed":
        return {"status": "failed", "message": status.get("lastError") or "预处理失败", "cancellable": False,
                "finished": None, "has_report": False}
    if now - started < START_GRACE:
        return {"status": "queued", "message": None, "cancellable": False, "finished": None, "has_report": False}
    return {"status": "failed", "message": "预处理没有完成", "cancellable": False, "finished": None,
            "has_report": False}
