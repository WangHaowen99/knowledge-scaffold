"""可观测（详设 4.10.2、方案 E6）：结构化日志、指标、链路，都带同一组公共标签。

- **日志**：根 logger 挂 JSON 格式化器，每行 `{ts, level, event, logger, fields}`，fields 带当前上下文的
  公共标签（project、session、exec、changeset、pack、node、trace_id）；消息与字段里的令牌、密钥脱敏。
  同时写进数据目录 `logs/onto-modeling.jsonl`（按日轮转，保留 14 天），运维面 O-06/O-07 读它。
- **指标**：进程内计数器与直方图，按名称 + 标签累计；`/metrics` 输出 Prometheus 文本格式（运维监听上），
  O-02 的卡片从最近的事件样本按时间分桶算出。
- **链路**：每个请求一个 trace，内核各处用 `span()` 记子段（提交、检查、模型调用……），只记名称、耗时、
  状态和计数类属性，不记项目内容；保留最近 2000 条链路（O-04/O-05）。

导出器（OTel 等）是可替换实现（详设 5.7），这里只提供内置的 file 与 prometheus 两个出口。
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from contextlib import contextmanager
from pathlib import Path

from .. import config
from . import context

LOG_FILE = "onto-modeling.jsonl"
LOG_KEEP_DAYS = 14
_SAMPLE_LIMIT = 20000
_TRACE_LIMIT = 2000
_SPANS_PER_TRACE = 200

# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization|access_token|refresh_token)"
               r"\s*[=:]\s*[\"']?)[^\s,;'\"&]+"),
    re.compile(r"(?i)([?&](?:token|key|access_token)=)[^&\s]+"),
    re.compile(r"\b(?:sk|onto_pat|onto_svc)[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
)
_SECRET_KEYS = re.compile(r"(?i)(key|token|secret|password|passwd|authorization|cookie)")


def redact(text: str) -> str:
    """去掉文本里的令牌、密钥、连接串口令（日志、错误留档、诊断包共用）。"""
    text = str(text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.re.groups else "") + "***", text)
    return text


def redact_fields(fields: dict) -> dict:
    return {
        key: ("***" if _SECRET_KEYS.search(str(key)) and value not in (None, "") else redact(value)
              if isinstance(value, str) else value)
        for key, value in fields.items()
    }


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = {key: value for key, value in record.__dict__.items() if key not in _RESERVED}
        fields = {**context.labels(), **{k: v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
                                         for k, v in fields.items()}}
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": _LEVELS.get(record.levelno, record.levelname),
            "event": getattr(record, "event", None) or record.name,
            "message": redact(message),
            "logger": record.name,
            "fields": redact_fields(fields),
        }
        return json.dumps(entry, ensure_ascii=False)


_LEVELS = {logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARN", logging.ERROR: "ERROR",
           logging.CRITICAL: "ERROR"}
_configured = False
_file_handler: logging.Handler | None = None


def log_dir() -> Path:
    return Path(config.DATA_DIR) / "logs"


def log_file() -> Path:
    return log_dir() / LOG_FILE


def configure_logging(level: int = logging.INFO) -> None:
    """根 logger 输出 JSON（控制台 + 数据目录按日轮转的文件）；重复调用无副作用。"""
    global _configured, _file_handler
    if _configured:
        return
    formatter = JsonFormatter()
    root = logging.getLogger()
    root.setLevel(min(root.level or level, level) if root.handlers else level)
    if not any(type(handler) is logging.StreamHandler for handler in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        _file_handler = logging.handlers.TimedRotatingFileHandler(
            log_file(), when="midnight", backupCount=LOG_KEEP_DAYS, encoding="utf-8", utc=True,
        )
        _file_handler.setFormatter(formatter)
        root.addHandler(_file_handler)
    except OSError:
        _file_handler = None
    _configured = True


def read_logs(limit: int = 5000) -> list[dict]:
    """最近的结构化日志（新的在前），只读当前文件与前一天的轮转文件。"""
    files = sorted(log_dir().glob(f"{LOG_FILE}*"), key=lambda p: p.stat().st_mtime) if log_dir().is_dir() else []
    entries: list[dict] = []
    for path in reversed(files[-2:]):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
            if len(entries) >= limit:
                return entries
    return entries


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

_metrics_lock = threading.Lock()
_counters: dict[tuple[str, tuple], float] = defaultdict(float)
_histograms: dict[tuple[str, tuple], list[float]] = defaultdict(list)
_samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=_SAMPLE_LIMIT))
_gauges: dict = {}
HISTOGRAM_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)


def _key(name: str, labels: dict) -> tuple[str, tuple]:
    return name, tuple(sorted((k, str(v)) for k, v in labels.items() if v not in (None, "")))


def count(name: str, value: float = 1, **labels) -> None:
    """计数器加 value；同时记一条带时间的样本（O-02 按时间分桶）。"""
    merged = {**_common_labels(), **labels}
    with _metrics_lock:
        _counters[_key(name, merged)] += value
        _samples[name].append((time.time(), value, merged))


def observe(name: str, value: float, **labels) -> None:
    """直方图观测值（秒、token 数等）。"""
    merged = {**_common_labels(), **labels}
    with _metrics_lock:
        bucket = _histograms[_key(name, merged)]
        bucket.append(value)
        if len(bucket) > _SAMPLE_LIMIT:
            del bucket[: len(bucket) - _SAMPLE_LIMIT]
        _samples[name].append((time.time(), value, merged))


def register_gauge(name: str, read: callable) -> None:
    """即时值（会话池在用数、排队数等），抓取时现读。"""
    _gauges[name] = read


# 包声明的指标（详设 5.6，ADR-0068）：插件运行时登记 provider() → [{name, help, type}]，
# /metrics 给出 HELP 与 TYPE；取值经 count / observe 按同名记录
_declarations: list[callable] = []


def register_declarations(provider: callable) -> None:
    if provider not in _declarations:
        _declarations.append(provider)


def declared_metrics() -> list[dict]:
    out = []
    for provider in list(_declarations):
        try:
            out += list(provider() or [])
        except Exception:  # noqa: BLE001 - 声明取不到不影响内核指标
            continue
    return out


def _common_labels() -> dict:
    # 指标只带低基数的标签：项目、包、节点；会话与 trace 进日志和链路，不进指标
    current = context.labels()
    return {key: current[key] for key in ("project", "pack", "node") if key in current}


def samples(name: str, since: float) -> list[tuple[float, float, dict]]:
    with _metrics_lock:
        return [item for item in _samples.get(name, ()) if item[0] >= since]


def gauge(name: str):
    read = _gauges.get(name)
    try:
        return read() if read else None
    except Exception:
        return None


def _prom_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_:]", "_", name)


def _prom_labels(labels: tuple) -> str:
    if not labels:
        return ""
    body = ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for k, v in labels)
    return "{" + body + "}"


def prometheus_text() -> str:
    lines: list[str] = []
    for item in declared_metrics():
        name = _prom_name(str(item.get("name") or ""))
        if name:
            lines.append(f"# HELP {name} {str(item.get('help') or '').replace(chr(10), ' ')}")
            lines.append(f"# TYPE {name} {item.get('type') or 'gauge'}")
    with _metrics_lock:
        counters = dict(_counters)
        histograms = {key: list(values) for key, values in _histograms.items()}
    for name in sorted({key[0] for key in counters}):
        lines.append(f"# TYPE {_prom_name(name)} counter")
        for (metric, labels), value in sorted(counters.items()):
            if metric == name:
                lines.append(f"{_prom_name(name)}{_prom_labels(labels)} {value}")
    for name in sorted({key[0] for key in histograms}):
        prom = _prom_name(name)
        lines.append(f"# TYPE {prom} histogram")
        for (metric, labels), values in sorted(histograms.items()):
            if metric != name:
                continue
            for bound in HISTOGRAM_BUCKETS:
                le = _prom_labels(labels + (("le", str(bound)),))
                lines.append(f"{prom}_bucket{le} {sum(1 for v in values if v <= bound)}")
            lines.append(f"{prom}_bucket{_prom_labels(labels + (('le', '+Inf'),))} {len(values)}")
            lines.append(f"{prom}_sum{_prom_labels(labels)} {sum(values)}")
            lines.append(f"{prom}_count{_prom_labels(labels)} {len(values)}")
    for name, _read in sorted(_gauges.items()):
        value = gauge(name)
        if isinstance(value, (int, float)):
            lines.append(f"# TYPE {_prom_name(name)} gauge")
            lines.append(f"{_prom_name(name)} {value}")
    return "\n".join(lines) + "\n"


def reset_metrics() -> None:
    """测试用：清空进程内指标与链路。"""
    with _metrics_lock:
        _counters.clear()
        _histograms.clear()
        _samples.clear()
    with _traces_lock:
        _traces.clear()


# ---------------------------------------------------------------------------
# 链路
# ---------------------------------------------------------------------------

_traces_lock = threading.Lock()
_traces: "OrderedDict[str, dict]" = OrderedDict()
_current_span: contextvars.ContextVar[str] = contextvars.ContextVar("onto_current_span", default="")


def _trace(trace_id: str) -> dict:
    trace = _traces.get(trace_id)
    if trace is None:
        trace = {"trace_id": trace_id, "started_at": time.time(), "labels": {}, "spans": []}
        _traces[trace_id] = trace
        while len(_traces) > _TRACE_LIMIT:
            _traces.popitem(last=False)
    return trace


@contextmanager
def span(name: str, kind: str = "event", **attrs):
    """记一段链路：没有 trace id 时（后台任务）新开一个。attrs 只放路径、key、计数，不放内容。"""
    trace_id = context.get("trace_id")
    token = None
    if not trace_id:
        trace_id = context.new_trace_id()
        token = context.set_label("trace_id", trace_id)
    span_id = uuid.uuid4().hex[:12]
    parent = _current_span.get()
    span_token = _current_span.set(span_id)
    started = time.time()
    record = {"id": span_id, "parent_id": parent or None, "name": name, "kind": kind, "status": "ok",
              "attrs": {k: v for k, v in attrs.items() if isinstance(v, (str, int, float, bool))}}
    try:
        yield record
    except Exception:
        record["status"] = "error"
        raise
    finally:
        record["duration_ms"] = int((time.time() - started) * 1000)
        record["started_at"] = started
        with _traces_lock:
            trace = _trace(trace_id)
            trace["labels"].update({k: v for k, v in context.labels().items() if k != "trace_id"})
            if len(trace["spans"]) < _SPANS_PER_TRACE:
                trace["spans"].append(record)
        _current_span.reset(span_token)
        if token is not None:
            context.trace_id_var.reset(token)


def get_trace(trace_id: str) -> dict | None:
    with _traces_lock:
        trace = _traces.get(trace_id)
        return json.loads(json.dumps(trace)) if trace else None


def list_traces() -> list[dict]:
    with _traces_lock:
        return [json.loads(json.dumps(trace)) for trace in reversed(_traces.values())]


def record_turn(project_id: str, event: dict, engine: str = "") -> None:
    """一轮结束（result 事件）：轮次时长、模型耗时、输出 token、失败次数（O-02）。"""
    labels = {"project": project_id, "engine": engine}
    count("onto_turns_total", **labels)
    if event.get("duration_ms"):
        observe("onto_turn_seconds", float(event["duration_ms"]) / 1000, **labels)
    if event.get("duration_api_ms"):
        observe("onto_model_call_seconds", float(event["duration_api_ms"]) / 1000, **labels)
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    if usage.get("output_tokens"):
        count("onto_output_tokens_total", float(usage["output_tokens"]), **labels)
    if event.get("is_error") or event.get("subtype") not in (None, "success"):
        count("onto_model_failures_total", **labels)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
