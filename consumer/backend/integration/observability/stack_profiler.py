# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Request-scoped sampled Python stack profiler that writes Speedscope files."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import linecache
import os
from pathlib import Path
import re
import sys
import threading
import time
from types import FrameType
from typing import Any, Iterator, Optional

from utils.logging import get_logger
from .speedscope_io import resolve_speedscope_output_dir, speedscope_file_lock

logger = get_logger("observability.stack_profiler")

SPEEDSCOPE_SCHEMA = "https://www.speedscope.app/file-format-schema.json"
STACK_MAX_EVENTS_ENV = "SPEEDSCOPE_STACK_PROFILE_MAX_EVENTS"
STACK_PROCESS_MAX_EVENTS_ENV = "SPEEDSCOPE_STACK_PROFILE_PROCESS_MAX_EVENTS"
STACK_SAMPLE_INTERVAL_MS_ENV = "SPEEDSCOPE_STACK_SAMPLE_INTERVAL_MS"
DEFAULT_MAX_SAMPLES = 20000
DEFAULT_PROCESS_MAX_SAMPLES = 50000
DEFAULT_SAMPLE_INTERVAL_MS = 10.0

_RUNS: dict[str, "_RunStackProfile"] = {}
_LOCK = threading.RLock()
_CONTEXT_BINDINGS: ContextVar[tuple["_ThreadBinding", ...]] = ContextVar(
    "speedscope_stack_profile_bindings",
    default=(),
)
_THREAD_RUN_COUNTS: dict[int, dict[str, int]] = {}
_THREAD_RUN_COUNTS_LOCK = threading.RLock()
_PROCESS_SAMPLE_COUNT = 0
_PROCESS_SAMPLE_BUDGET_LOCK = threading.RLock()


@dataclass
class _ThreadStackProfile:
    thread_id: int
    thread_name: str
    samples: list[list[int]] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)
    active_count: int = 0
    dropped_samples: int = 0
    skipped_shared_thread_samples: int = 0


@dataclass(frozen=True)
class _ThreadBinding:
    run_id: str
    profile: "_RunStackProfile"
    thread_profile: _ThreadStackProfile


@dataclass
class _RunStackProfile:
    run_id: str
    session_id: str
    turn_id: Optional[int]
    start_ns: int
    output_dir: Path
    max_samples: int
    sample_interval_ms: float
    process_max_samples: int = DEFAULT_PROCESS_MAX_SAMPLES
    total_samples: int = 0
    frames: list[dict[str, Any]] = field(default_factory=list)
    frame_indexes: dict[tuple[str, str], int] = field(default_factory=dict)
    threads: dict[int, _ThreadStackProfile] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.RLock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    sampler_thread: Optional[threading.Thread] = None
    closed: bool = False
    end_ns: Optional[int] = None

    def start_sampler(self) -> None:
        if self.sampler_thread is not None:
            return
        self.sampler_thread = threading.Thread(
            target=_sample_loop,
            args=(self,),
            name="speedscope-stack-sampler",
            daemon=True,
        )
        self.sampler_thread.start()

    def stop_sampler(self) -> None:
        self.stop_event.set()
        sampler = self.sampler_thread
        if sampler is not None and sampler.is_alive():
            sampler.join(timeout=max(0.2, self.sample_interval_ms / 1000.0 * 4))

    def thread_profile(self) -> _ThreadStackProfile:
        thread_id = threading.get_ident()
        with self.lock:
            profile = self.threads.get(thread_id)
            if profile is None:
                profile = _ThreadStackProfile(
                    thread_id=thread_id,
                    thread_name=threading.current_thread().name or str(thread_id),
                )
                self.threads[thread_id] = profile
            return profile

    def synthetic_frame_index(self, name: str, file_name: str) -> int:
        return self._frame_index(name=name, file_name=file_name)

    def frame_index(self, frame: FrameType) -> Optional[int]:
        if not _include_frame(frame):
            return None
        code = frame.f_code
        lineno = int(frame.f_lineno or code.co_firstlineno or 0)
        file_name = _normalize_file_name(code.co_filename, lineno)
        module_name = str(frame.f_globals.get("__name__") or "").strip()
        function_name = code.co_name or "<unknown>"
        qualified = "{}.{}".format(module_name, function_name) if module_name else function_name
        name = "{} L{}".format(qualified, lineno)
        return self._frame_index(name=name, file_name=file_name)

    def _frame_index(self, *, name: str, file_name: str) -> int:
        key = (name, file_name)
        index = self.frame_indexes.get(key)
        if index is not None:
            return index
        index = len(self.frames)
        self.frames.append({"name": name, "file": file_name})
        self.frame_indexes[key] = index
        return index

    def timestamp_ms(self, now_ns: Optional[int] = None) -> float:
        current = now_ns if now_ns is not None else time.perf_counter_ns()
        return max(0.0, (current - self.start_ns) / 1_000_000.0)


def start_run_stack_profile(
    *,
    run_id: str,
    session_id: Optional[str],
    turn_id: Optional[int],
    enabled: bool = False,
) -> None:
    if not enabled:
        return
    normalized_run_id = str(run_id or "").strip()
    normalized_session_id = str(session_id or "").strip()
    if not normalized_run_id or not normalized_session_id:
        return
    with _LOCK:
        profile = _RUNS.get(normalized_run_id)
        if profile is None:
            max_samples = _max_samples()
            profile = _RunStackProfile(
                run_id=normalized_run_id,
                session_id=normalized_session_id,
                turn_id=turn_id,
                start_ns=time.perf_counter_ns(),
                output_dir=resolve_speedscope_output_dir(),
                max_samples=max_samples,
                process_max_samples=_process_max_samples(),
                sample_interval_ms=_sample_interval_ms(),
            )
            _RUNS[normalized_run_id] = profile
            profile.start_sampler()
    attach_current_thread_stack_profile(normalized_run_id)


def stop_run_stack_profile(run_id: str, *, status: str = "success") -> None:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return
    with _LOCK:
        profile = _RUNS.pop(normalized_run_id, None)
        if profile is not None:
            profile.closed = True
            profile.end_ns = time.perf_counter_ns()
    if profile is None:
        return
    detach_current_thread_stack_profile(normalized_run_id)
    profile.stop_sampler()
    try:
        _write_stack_profile(profile, status=status)
    except Exception:
        logger.warning("Failed to write Speedscope code stack profile", exc_info=True)
    finally:
        _release_process_samples(profile.total_samples)


@contextmanager
def profile_current_thread(run_id: Optional[str]) -> Iterator[None]:
    attached = attach_current_thread_stack_profile(run_id)
    try:
        yield
    finally:
        if attached:
            detach_current_thread_stack_profile(run_id)


def attach_current_thread_stack_profile(run_id: Optional[str]) -> bool:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return False
    with _LOCK:
        profile = _RUNS.get(normalized_run_id)
    if profile is None or profile.closed:
        return False
    thread_profile = profile.thread_profile()
    _increment_thread_binding(profile, thread_profile)
    bindings = _CONTEXT_BINDINGS.get()
    _CONTEXT_BINDINGS.set(
        (
            *bindings,
            _ThreadBinding(
                run_id=normalized_run_id,
                profile=profile,
                thread_profile=thread_profile,
            ),
        )
    )
    return True


def detach_current_thread_stack_profile(run_id: Optional[str] = None) -> None:
    bindings = list(_CONTEXT_BINDINGS.get())
    if not bindings:
        return
    normalized_run_id = str(run_id or "").strip()
    index = len(bindings) - 1
    if normalized_run_id:
        for candidate_index in range(len(bindings) - 1, -1, -1):
            if bindings[candidate_index].run_id == normalized_run_id:
                index = candidate_index
                break
        else:
            return
    binding = bindings.pop(index)
    _CONTEXT_BINDINGS.set(tuple(bindings))
    _decrement_thread_binding(binding.profile, binding.thread_profile)


def _increment_thread_binding(
    profile: "_RunStackProfile",
    thread_profile: _ThreadStackProfile,
) -> None:
    with profile.lock:
        thread_profile.active_count += 1
    with _THREAD_RUN_COUNTS_LOCK:
        counts = _THREAD_RUN_COUNTS.setdefault(thread_profile.thread_id, {})
        counts[profile.run_id] = int(counts.get(profile.run_id, 0)) + 1


def _decrement_thread_binding(
    profile: "_RunStackProfile",
    thread_profile: _ThreadStackProfile,
) -> None:
    with profile.lock:
        thread_profile.active_count = max(0, thread_profile.active_count - 1)
    with _THREAD_RUN_COUNTS_LOCK:
        counts = _THREAD_RUN_COUNTS.get(thread_profile.thread_id)
        if not counts:
            return
        next_count = int(counts.get(profile.run_id, 0)) - 1
        if next_count > 0:
            counts[profile.run_id] = next_count
        else:
            counts.pop(profile.run_id, None)
        if not counts:
            _THREAD_RUN_COUNTS.pop(thread_profile.thread_id, None)


def _thread_exclusive_to_run(thread_id: int, run_id: str) -> bool:
    with _THREAD_RUN_COUNTS_LOCK:
        counts = {
            active_run_id: count
            for active_run_id, count in (_THREAD_RUN_COUNTS.get(thread_id) or {}).items()
            if count > 0
        }
    if not counts:
        return False
    return len(counts) == 1 and int(counts.get(run_id, 0)) > 0


def _sample_loop(profile: _RunStackProfile) -> None:
    interval_seconds = max(0.001, profile.sample_interval_ms / 1000.0)
    while not profile.stop_event.wait(interval_seconds):
        if profile.closed:
            return
        try:
            _sample_profile(profile)
        except Exception:
            logger.warning("Speedscope stack sample failed", exc_info=True)


def _sample_profile(profile: _RunStackProfile) -> None:
    now_ns = time.perf_counter_ns()
    current_frames = getattr(sys, "_current_frames", None)
    frames_by_thread = current_frames() if callable(current_frames) else {}
    with profile.lock:
        thread_profiles = [
            thread_profile
            for thread_profile in profile.threads.values()
            if thread_profile.active_count > 0
        ]
        for thread_profile in thread_profiles:
            frame = frames_by_thread.get(thread_profile.thread_id)
            if frame is None:
                continue
            if not _thread_exclusive_to_run(thread_profile.thread_id, profile.run_id):
                thread_profile.skipped_shared_thread_samples += 1
                continue
            sample = _sample_stack(profile, thread_profile, frame)
            if not sample:
                continue
            if not _reserve_sample_budget(profile):
                thread_profile.dropped_samples += 1
                continue
            thread_profile.samples.append(sample)
            thread_profile.weights.append(profile.sample_interval_ms)
    del frames_by_thread
    del now_ns


def _sample_stack(
    profile: _RunStackProfile,
    thread_profile: _ThreadStackProfile,
    frame: FrameType,
) -> list[int]:
    state = _classify_thread_state(frame)
    thread_frame = profile.synthetic_frame_index(
        "THREAD / {}".format(thread_profile.thread_name),
        "meta-wise/thread",
    )
    state_frame = profile.synthetic_frame_index(
        "STATE / {}".format(state),
        "meta-wise/execution-state",
    )
    stack: list[int] = []
    current: Optional[FrameType] = frame
    while current is not None:
        index = profile.frame_index(current)
        if index is not None:
            stack.append(index)
        current = current.f_back
    stack.reverse()
    if not stack:
        return []
    return [thread_frame, state_frame, *stack[-80:]]


def _reserve_sample_budget(profile: _RunStackProfile) -> bool:
    global _PROCESS_SAMPLE_COUNT

    if profile.total_samples >= profile.max_samples:
        return False
    with _PROCESS_SAMPLE_BUDGET_LOCK:
        if _PROCESS_SAMPLE_COUNT >= profile.process_max_samples:
            return False
        _PROCESS_SAMPLE_COUNT += 1
    profile.total_samples += 1
    return True


def _release_process_samples(count: int) -> None:
    global _PROCESS_SAMPLE_COUNT

    if count <= 0:
        return
    with _PROCESS_SAMPLE_BUDGET_LOCK:
        _PROCESS_SAMPLE_COUNT = max(0, _PROCESS_SAMPLE_COUNT - count)


def _classify_thread_state(frame: FrameType) -> str:
    current: Optional[FrameType] = frame
    while current is not None:
        module_name = str(current.f_globals.get("__name__") or "")
        function_name = str(current.f_code.co_name or "")
        source = _source_line(current.f_code.co_filename, int(current.f_lineno or 0))
        if "event_queue.get()" in source:
            return "WAIT_UPSTREAM"
        if "sleep(" in source:
            return "WAIT_SLEEP"
        if source.startswith("await ") or source.startswith("async for ") or " await " in source:
            return "WAIT_ASYNC"
        if module_name.startswith(("socket", "ssl", "select", "selectors")):
            return "WAIT_IO"
        if module_name.startswith(("httpx", "httpcore", "openai", "requests", "urllib3", "anyio")):
            return "WAIT_IO"
        if module_name.startswith("asyncio") and function_name in {"select", "_run_once", "run_forever"}:
            return "WAIT_ASYNC"
        if module_name.startswith(("threading", "queue", "concurrent.futures")):
            if function_name in {"wait", "get", "result", "join", "_wait_for_tstate_lock"}:
                return "WAIT_LOCK"
        if module_name == "time" and function_name == "sleep":
            return "WAIT_SLEEP"
        current = current.f_back
    return "CPU_RUN"


def _write_stack_profile(profile: _RunStackProfile, *, status: str) -> None:
    path = profile.output_dir / _session_file_name(profile.session_id)
    with speedscope_file_lock(path):
        document = _load_session_document(path, profile.session_id)
        code_prefix = _code_profile_prefix(profile)
        document["profiles"] = [
            row
            for row in document.get("profiles", [])
            if not str(row.get("name") or "").startswith(code_prefix)
        ]
        document = _compact_document_frames(document)
        frames = document["shared"]["frames"]
        frame_indexes = {
            (str(frame.get("name") or ""), str(frame.get("file") or "")): index
            for index, frame in enumerate(frames)
            if isinstance(frame, dict)
        }
        frame_map = _merge_frames(profile.frames, frames, frame_indexes)
        profiles = []
        end_value = max(0.001, profile.timestamp_ms(profile.end_ns))
        for thread_profile in sorted(profile.threads.values(), key=_thread_profile_sort_key):
            if not thread_profile.samples:
                continue
            dropped = (
                " [DROPPED {} SAMPLES]".format(thread_profile.dropped_samples)
                if thread_profile.dropped_samples
                else ""
            )
            skipped = (
                " [SKIPPED {} SHARED-THREAD SAMPLES]".format(
                    thread_profile.skipped_shared_thread_samples
                )
                if thread_profile.skipped_shared_thread_samples
                else ""
            )
            profiles.append(
                {
                    "type": "sampled",
                    "name": "{} / {} / Thread {}{}{}".format(
                        code_prefix,
                        status,
                        thread_profile.thread_name,
                        dropped,
                        skipped,
                    ),
                    "unit": "milliseconds",
                    "startValue": 0,
                    "endValue": end_value,
                    "samples": [
                        [frame_map[index] for index in sample if index in frame_map]
                        for sample in thread_profile.samples
                    ],
                    "weights": thread_profile.weights,
                }
            )
        first_new_profile = len(document["profiles"])
        document["profiles"].extend(profiles)
        if profiles:
            document["activeProfileIndex"] = first_new_profile
        _atomic_write_json(path, document)


def _thread_profile_sort_key(thread_profile: _ThreadStackProfile) -> tuple[int, int, str]:
    name = thread_profile.thread_name
    if "jiuwen-loop" in name:
        priority = 0
    elif "jiuwen-tool" in name:
        priority = 1
    elif "ThreadPoolExecutor" in name:
        priority = 3
    else:
        priority = 2
    return (priority, -len(thread_profile.samples), name)


def _include_frame(frame: FrameType) -> bool:
    filename = str(frame.f_code.co_filename or "").replace("\\", "/")
    if not filename or filename.startswith("<"):
        return False
    module_name = str(frame.f_globals.get("__name__") or "")
    if module_name == __name__:
        return False
    if module_name.startswith(
        (
            "agentic",
            "openjiuwen",
            "httpx",
            "httpcore",
            "openai",
            "requests",
            "urllib3",
            "anyio",
        )
    ):
        return True
    markers = (
        "/app/",
        "/consumer/backend/",
        "/openjiuwen/",
        "/site-packages/httpx/",
        "/site-packages/httpcore/",
        "/site-packages/openai/",
        "/site-packages/requests/",
        "/site-packages/urllib3/",
        "/site-packages/anyio/",
    )
    return any(marker in filename for marker in markers)


def _normalize_file_name(filename: str, line_number: int) -> str:
    normalized = str(filename or "").replace("\\", "/")
    for marker in ("/app/", "/consumer/backend/", "/site-packages/"):
        index = normalized.find(marker)
        if index >= 0:
            normalized = normalized[index + len(marker):]
            break
    return "{}:{}".format(normalized, int(line_number or 0))


def _source_line(filename: str, line_number: int) -> str:
    try:
        return linecache.getline(filename, int(line_number or 0)).strip()[:120]
    except Exception:
        return ""


def _code_profile_prefix(profile: _RunStackProfile) -> str:
    return "Code Lines / Turn {} / Run {}".format(
        profile.turn_id if profile.turn_id is not None else "?",
        profile.run_id,
    )


def _session_file_name(session_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "session")).strip("._-")
    normalized = (normalized or "session")[:64]
    digest = hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest()[:12]
    return "session-{}-{}.speedscope.json".format(normalized, digest)


def _load_session_document(path: Path, session_id: str) -> dict[str, Any]:
    if path.exists():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            has_valid_schema = isinstance(document, dict) and document.get("$schema") == SPEEDSCOPE_SCHEMA
            has_valid_profiles = isinstance(document, dict) and isinstance(document.get("profiles"), list)
            shared = document.get("shared") if isinstance(document, dict) else None
            has_valid_frames = isinstance(shared, dict) and isinstance(shared.get("frames"), list)
            if has_valid_schema and has_valid_profiles and has_valid_frames:
                return document
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable Speedscope session trace %s", path, exc_info=True)
    return {
        "$schema": SPEEDSCOPE_SCHEMA,
        "name": "Knowledge QA Session {}".format(session_id),
        "exporter": "meta-wise",
        "activeProfileIndex": 0,
        "shared": {"frames": []},
        "profiles": [],
    }


def _compact_document_frames(document: dict[str, Any]) -> dict[str, Any]:
    old_frames = list(document.get("shared", {}).get("frames") or [])
    new_frames: list[dict[str, Any]] = []
    frame_indexes: dict[tuple[str, str], int] = {}
    index_map: dict[int, int] = {}

    def remap_frame(old_index: Any) -> Optional[int]:
        try:
            numeric_index = int(old_index)
        except (TypeError, ValueError):
            return None
        if numeric_index in index_map:
            return index_map[numeric_index]
        if numeric_index < 0 or numeric_index >= len(old_frames):
            return None
        frame = old_frames[numeric_index]
        if not isinstance(frame, dict):
            return None
        normalized = {
            "name": str(frame.get("name") or ""),
            "file": str(frame.get("file") or ""),
        }
        key = (normalized["name"], normalized["file"])
        new_index = frame_indexes.get(key)
        if new_index is None:
            new_index = len(new_frames)
            new_frames.append(normalized)
            frame_indexes[key] = new_index
        index_map[numeric_index] = new_index
        return new_index

    for profile in document.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        if profile.get("type") == "sampled":
            remapped_samples = []
            for sample in profile.get("samples") or []:
                if not isinstance(sample, list):
                    continue
                remapped = [remap_frame(index) for index in sample]
                remapped_samples.append([index for index in remapped if index is not None])
            profile["samples"] = remapped_samples
            continue
        for event in profile.get("events") or []:
            if not isinstance(event, dict) or "frame" not in event:
                continue
            new_index = remap_frame(event.get("frame"))
            if new_index is not None:
                event["frame"] = new_index

    document["shared"] = {"frames": new_frames}
    return document


def _merge_frames(
    source_frames: list[dict[str, Any]],
    target_frames: list[dict[str, Any]],
    target_indexes: dict[tuple[str, str], int],
) -> dict[int, int]:
    frame_map: dict[int, int] = {}
    for source_index, source_frame in enumerate(source_frames):
        if not isinstance(source_frame, dict):
            continue
        normalized = {
            "name": str(source_frame.get("name") or ""),
            "file": str(source_frame.get("file") or ""),
        }
        key = (normalized["name"], normalized["file"])
        target_index = target_indexes.get(key)
        if target_index is None:
            target_index = len(target_frames)
            target_frames.append(normalized)
            target_indexes[key] = target_index
        frame_map[source_index] = target_index
    return frame_map


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name("{}.{}.{}.tmp".format(path.name, os.getpid(), threading.get_ident()))
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _max_samples() -> int:
    try:
        return max(1000, int(os.getenv(STACK_MAX_EVENTS_ENV) or DEFAULT_MAX_SAMPLES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SAMPLES


def _process_max_samples() -> int:
    try:
        return max(
            1000,
            int(os.getenv(STACK_PROCESS_MAX_EVENTS_ENV) or DEFAULT_PROCESS_MAX_SAMPLES),
        )
    except (TypeError, ValueError):
        return DEFAULT_PROCESS_MAX_SAMPLES


def _sample_interval_ms() -> float:
    try:
        return max(1.0, float(os.getenv(STACK_SAMPLE_INTERVAL_MS_ENV) or DEFAULT_SAMPLE_INTERVAL_MS))
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_INTERVAL_MS
