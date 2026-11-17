"""运维管控与可观测接口（契约 3.25；详设 4.10.2、4.10.3；方案 E6、E8）。

O-02～O-07 指标、归因、链路、日志；O-14 立即探测；O-16～O-19 会话池配置、强制断开、排空；
O-20～O-24 维护模式；O-29 熔断；O-42 版本清单；O-45～O-47 诊断包；O-49 导出审计；O-51 错误详情；
O-54 永久删除回收站项目。写操作只有 ops（除另有说明），读操作 ops、admin；写操作都记审计（类型 ops）。
O-08～O-10 告警规则（规则只登记、本期不评估，state 恒为 ok）；导出器（O-11、O-12）、恢复演练（O-38、O-39）、
任务中心（O-40、O-41）、数据保留（O-43、O-44）不在本阶段。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import config
from ..api import deps
from ..api.errors import ApiError
from ..governance import config_registry, ops_state, roles, telemetry
from ..services import audit
from . import ops

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])

AUDIT_EXPORT_LIMIT = 100_000
RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
BUCKETS = 12


def _read(request: Request) -> None:
    roles.require_ops_read(request)


def _write(request: Request) -> None:
    roles.require_ops_write(request)


def _ops_audit(request: Request, action: str, **detail) -> None:
    """运维写操作的专项审计（类型 ops）：动作名与要点，请求本身的默认记录由治理中间件写。"""
    audit.record(type="ops", action=action, actor=deps.actor(request) or "anonymous", method=request.method,
                 path=request.url.path, detail={k: v for k, v in detail.items() if v is not None} or None)


def _maintenance_error(exc: ops_state.MaintenanceError) -> ApiError:
    return ApiError(exc.code, str(exc), exc.status, exc.details)


def _maintenance_view(state: dict | None) -> dict:
    if not state:
        return {"active": False}
    return {**state, "started_at": deps.iso(state.get("started_at")), "until": deps.iso(state.get("until"))}


# ---- O-02 指标、O-03 归因 ----


def _labels_filter(raw: str) -> dict[str, str]:
    result = {}
    for part in (raw or "").split(","):
        key, _, value = part.partition("=")
        if key.strip() and value.strip():
            result[key.strip()] = value.strip()
    return result


def _matches(labels: dict, wanted: dict) -> bool:
    return all(str(labels.get(key)) == value for key, value in wanted.items())


def _series(name: str, span: int, wanted: dict, reduce: Callable[[list[float]], float]) -> list[float]:
    now = time.time()
    width = span / BUCKETS
    buckets: list[list[float]] = [[] for _ in range(BUCKETS)]
    for ts, value, labels in telemetry.samples(name, now - span):
        if not _matches(labels, wanted):
            continue
        index = min(BUCKETS - 1, int((ts - (now - span)) / width))
        buckets[index].append(value)
    return [round(reduce(bucket), 2) if bucket else 0 for bucket in buckets]


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else 0


def _card(card_id: str, label: str, value, unit: str, series: list[float], secondary: str = "",
          tone: str = "normal") -> dict:
    return {"id": card_id, "label": label, "value": value, "unit": unit, "secondary": secondary,
            "series": series, "tone": tone}


@router.get("/metrics")
def metrics(request: Request, range: str = "24h", labels: str = ""):  # noqa: A002  契约查询参数名
    """O-02：指标卡片。数值来自进程内指标（重启后从零开始）。"""
    _read(request)
    if range not in RANGES:
        raise ApiError("invalid_argument", "range 只能是 1h、24h、7d", 400, {"fields": {"range": "enum"}})
    span, wanted = RANGES[range], _labels_filter(labels)
    pool = ops.pool_view()
    total = sum

    def card_total(name: str) -> tuple[float, list[float]]:
        series = _series(name, span, wanted, total)
        return round(sum(series), 2), series

    turns = _series("onto_turn_seconds", span, wanted, _p95)
    latency = _series("onto_model_call_seconds", span, wanted, _p95)
    output, output_series = card_total("onto_output_tokens_total")
    turns_done, _ = card_total("onto_turns_total")
    failed, fail_series = card_total("onto_model_failures_total")
    guards, guard_series = card_total("onto_guard_denies_total")
    checks, check_series = card_total("onto_check_failures_total")
    merges, merge_series = card_total("onto_merges_total")
    reconnects, reconnect_series = card_total("onto_reconnects_total")
    fail_rate = round(failed / turns_done * 100, 1) if turns_done else 0
    return {"cards": [
        _card("pool", "会话池与排队", pool["active"], "个", [pool["active"]] * BUCKETS,
              f"上限 {pool['max_sessions']} · 排队 {pool['queued']}", tone="warn" if pool["queued"] else "normal"),
        _card("turn_duration", "轮次时长 p95", max(turns) if turns else 0, "秒", turns),
        _card("model_latency_p95", "模型耗时 p95", max(latency) if latency else 0, "秒", latency),
        _card("output_tokens", "输出 tokens", int(output), "tokens", output_series),
        _card("model_fail_rate", "模型失败", fail_rate, "%", fail_series, f"{int(failed)} 次",
              tone="error" if fail_rate >= 5 else "normal"),
        _card("guard_denies", "守卫拒绝", int(guards), "次", guard_series),
        _card("check_failures", "检查失败", int(checks), "次", check_series),
        _card("merges", "合入", int(merges), "次", merge_series),
        _card("reconnects", "热更新与重连", int(reconnects), "次", reconnect_series),
    ]}


@router.get("/attribution")
def attribution(request: Request, by: str = "pack", range: str = "24h", labels: str = ""):  # noqa: A002
    """O-03：按包、按节点归因。样本里带 pack / node 标签的才计入（插件运行时接入后才有数据）。"""
    _read(request)
    if by not in ("pack", "node"):
        raise ApiError("invalid_argument", "by 只能是 pack 或 node", 400, {"fields": {"by": "enum"}})
    if range not in RANGES:
        raise ApiError("invalid_argument", "range 只能是 1h、24h、7d", 400, {"fields": {"range": "enum"}})
    since, wanted = time.time() - RANGES[range], _labels_filter(labels)
    rows: dict[str, dict] = {}
    for ts, value, sample_labels in telemetry.samples("onto_turn_seconds", since):
        key = sample_labels.get(by)
        if not key or not _matches(sample_labels, wanted):
            continue
        row = rows.setdefault(key, {"calls": 0, "durations": [], "failures": 0, "guard_denies": 0})
        row["calls"] += 1
        row["durations"].append(value)
    for metric, field in (("onto_model_failures_total", "failures"), ("onto_guard_denies_total", "guard_denies")):
        for _ts, value, sample_labels in telemetry.samples(metric, since):
            key = sample_labels.get(by)
            if key in rows:
                rows[key][field] += value
    return {"items": [
        {"key": key, "version_or_source": key.partition("@")[2] or None, "calls": row["calls"],
         "fail_rate_pct": round(row["failures"] / row["calls"] * 100, 1) if row["calls"] else 0,
         "avg_duration_ms": int(sum(row["durations"]) / len(row["durations"]) * 1000) if row["durations"] else 0,
         "guard_denies": int(row["guard_denies"])}  # 重跑、一次通过率没有数据来源，不返回
        for key, row in sorted(rows.items())
    ]}


# ---- O-04、O-05 链路 ----


def _trace_view(trace: dict) -> dict:
    spans = sorted(trace.get("spans") or [], key=lambda span: span.get("started_at") or 0)
    start = spans[0]["started_at"] if spans else trace.get("started_at") or 0
    end = max((span["started_at"] + span["duration_ms"] / 1000 for span in spans), default=start)
    labels = trace.get("labels") or {}
    return {
        "trace_id": trace["trace_id"],
        "total_ms": int((end - start) * 1000),
        "labels": {key: labels.get(key) for key in ("project", "session", "exec", "changeset", "pack", "node")},
        "spans": [
            {"id": span["id"], "parent_id": span.get("parent_id"), "name": span["name"], "kind": span["kind"],
             "start_offset_ms": int((span["started_at"] - start) * 1000), "duration_ms": span["duration_ms"],
             "status": span["status"], "attrs": span.get("attrs") or {}}
            for span in spans
        ],
    }


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str, request: Request):
    """O-04：链路详情（进程内保留最近 2000 条）。"""
    _read(request)
    trace = telemetry.get_trace(trace_id)
    if not trace:
        raise ApiError("trace_not_found", "链路不存在或已过保留期", 404)
    return _trace_view(trace)


@router.get("/traces")
def traces(request: Request, labels: str = "", range: str = "24h", status: str = "", cursor: str = "",  # noqa: A002
           limit: int = 0):
    """O-05：链路检索。"""
    _read(request)
    if range not in RANGES:
        raise ApiError("invalid_argument", "range 只能是 1h、24h、7d", 400, {"fields": {"range": "enum"}})
    since, wanted = time.time() - RANGES[range], _labels_filter(labels)
    items = []
    for trace in telemetry.list_traces():
        if trace.get("started_at", 0) < since or not _matches(trace.get("labels") or {}, wanted):
            continue
        view = _trace_view(trace)
        trace_status = "error" if any(span["status"] == "error" for span in view["spans"]) else \
            "denied" if any(span["status"] == "denied" for span in view["spans"]) else "ok"
        if status and trace_status != status:
            continue
        root = view["spans"][0]["name"] if view["spans"] else ""
        items.append({"trace_id": view["trace_id"], "started_at": deps.iso(trace.get("started_at")),
                      "total_ms": view["total_ms"], "root_name": root, "labels": view["labels"], "status": trace_status})
    page = deps.page(items, cursor, limit, {"labels": labels, "range": range, "status": status})
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


# ---- O-06、O-07 日志 ----

_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def _log_filter(level: str, q: str, labels: str):
    levels = {item.strip().upper() for item in level.split(",") if item.strip()}
    if levels - set(_LEVELS):
        raise ApiError("invalid_argument", "level 只能是 INFO、WARN、ERROR", 400, {"fields": {"level": "enum"}})
    wanted = _labels_filter(labels)
    alternatives = [part.strip() for part in (q or "").split(" OR ") if part.strip()]

    def matches(entry: dict) -> bool:
        fields = entry.get("fields") or {}
        if levels and entry.get("level") not in levels:
            return False
        if not _matches(fields, wanted):
            return False
        if not alternatives:
            return True
        text = json.dumps(entry, ensure_ascii=False).lower()
        for alternative in alternatives:
            key, sep, value = alternative.partition("=")
            if sep and str(fields.get(key.strip())) == value.strip():
                return True
            if not sep and alternative.lower() in text:
                return True
        return False

    return matches


def _log_view(entry: dict) -> dict:
    fields = {key: "" if value is None else str(value) for key, value in (entry.get("fields") or {}).items()}
    if entry.get("message"):
        fields.setdefault("message", entry["message"])
    return {"ts": entry.get("ts"), "level": entry.get("level"), "event": entry.get("event"), "fields": fields}


@router.get("/logs")
def logs(request: Request, level: str = "", q: str = "", labels: str = "", range: str = "24h",  # noqa: A002
         cursor: str = "", limit: int = 0):
    """O-06：结构化日志，敏感字段已脱敏；默认每页 100 条。"""
    _read(request)
    if range not in RANGES:
        raise ApiError("invalid_argument", "range 只能是 1h、24h、7d", 400, {"fields": {"range": "enum"}})
    matches = _log_filter(level, q, labels)
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - RANGES[range]))
    items = [_log_view(entry) for entry in telemetry.read_logs() if (entry.get("ts") or "") >= since and matches(entry)]
    page = deps.page(items, cursor, limit, {"level": level, "q": q, "labels": labels, "range": range}, default=100)
    return {"items": page["items"], "next_cursor": page["next_cursor"]}


@router.get("/logs/stream")
async def logs_stream(request: Request, level: str = "", q: str = "", labels: str = ""):
    """O-07：日志跟随（SSE，事件 log，信封同 1.11）。"""
    _read(request)
    matches = _log_filter(level, q, labels)

    async def stream():
        seen = {entry.get("ts") for entry in telemetry.read_logs(200)}
        sequence = 0
        while not await request.is_disconnected():
            fresh = [entry for entry in reversed(telemetry.read_logs(200)) if entry.get("ts") not in seen]
            for entry in fresh:
                seen.add(entry.get("ts"))
                if matches(entry):
                    sequence += 1
                    envelope = {"type": "log", "seq": sequence, "data": _log_view(entry)}
                    yield f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"
            yield ": keep-alive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---- O-14 立即探测 ----


@router.post("/health/probe")
def probe(request: Request):
    """O-14：立即重新探测，响应同 O-13 的 dependencies（数组）。"""
    _write(request)
    return ops.dependencies(force=True)


# ---- O-16～O-19 会话池 ----


@router.put("/sessions/config")
async def save_pool_config(request: Request):
    """O-16：会话池配置，下一个会话起生效，覆盖环境变量默认值（配置注册表来源 data_dir）。"""
    _write(request)
    body = await deps.json_body(request)
    try:
        current = ops_state.save_pool_config(body.get("max_sessions"), body.get("idle_ttl_s"),
                                             body.get("overflow_policy") or "evict_idle_then_queue")
    except ValueError as exc:
        raise ApiError("invalid_argument", "会话池配置不合法", 400, {"fields": exc.args[0]}) from None
    _ops_audit(request, "ops.pool_config", **current)
    return {"config": current}


@router.post("/sessions/{project_id}/disconnect")
async def disconnect_session(project_id: str, request: Request):
    """O-18：强制断开；正在进行的一轮中断，历史标「上一轮未正常结束」。"""
    _write(request)
    body = await deps.json_body(request)
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise ApiError("invalid_argument", "请填写断开原因", 400, {"fields": {"reason": "required"}})
    if not await ops_state.force_disconnect(project_id, reason[:200]):
        raise ApiError("session_not_found", "该项目没有在线会话", 404)
    _ops_audit(request, "ops.session_disconnect", project=project_id, reason=reason[:200])
    return {"disconnected": True}


@router.post("/sessions/drain")
async def drain_sessions(request: Request):
    """O-19：排空。排空期间拒绝新会话；在线会话跑完当前这一轮后回收，超时的强制断开。"""
    _write(request)
    body = await deps.json_body(request)
    wait = body.get("max_wait_s", ops_state.DEFAULT_DRAIN_WAIT_S)
    if not isinstance(wait, int) or isinstance(wait, bool) or not 1 <= wait <= 86400:
        raise ApiError("invalid_argument", "max_wait_s 必须是 1–86400 的整数", 400, {"fields": {"max_wait_s": "int"}})
    result = ops_state.start_drain(wait)
    _ops_audit(request, "ops.drain", max_wait_s=wait, online=result["online"])
    return JSONResponse(status_code=202, content=result)


# ---- O-20～O-24 维护模式 ----


@router.get("/maintenance")
def maintenance(request: Request):
    _read(request)
    return _maintenance_view(ops_state.maintenance_view())


@router.post("/maintenance")
async def enter_maintenance(request: Request):
    _write(request)
    body = await deps.json_body(request)
    target = body.get("target")
    if target is not None and not (isinstance(target, dict) and target.get("kind") == "core_upgrade"):
        raise ApiError("invalid_argument", "target 只支持 core_upgrade", 400, {"fields": {"target": "kind"}})
    try:
        state = ops_state.enter_maintenance(body.get("mode"), str(body.get("message") or ""), deps.actor(request), target)
    except ops_state.MaintenanceError as exc:
        raise _maintenance_error(exc) from None
    _ops_audit(request, "ops.maintenance_enter", mode=state["mode"])
    if body.get("autobuild", "pause_at_safe_point") == "pause_at_safe_point":
        # 自主构建（O-21 autobuild）：在跑的做完这一段就暂停，排队的等维护结束
        from ..services.autobuild.runner import runner as autobuild_runner

        autobuild_runner.pause_all_at_safe_point()
    return JSONResponse(status_code=201, content=_maintenance_view(state))


@router.post("/maintenance/advance")
async def advance_maintenance(request: Request):
    _write(request)
    try:
        state = ops_state.advance_maintenance(deps.actor(request))
    except ops_state.MaintenanceError as exc:
        raise _maintenance_error(exc) from None
    _ops_audit(request, "ops.maintenance_advance", step=next(s["id"] for s in state["steps"] if s["state"] == "current"))
    return _maintenance_view(state)


@router.post("/maintenance/force-disconnect")
async def maintenance_force_disconnect(request: Request):
    _write(request)
    body = await deps.json_body(request)
    try:
        state = await ops_state.force_disconnect_remaining(str(body.get("reason") or "")[:200])
    except ops_state.MaintenanceError as exc:
        raise _maintenance_error(exc) from None
    _ops_audit(request, "ops.maintenance_force_disconnect")
    return _maintenance_view(state)


@router.delete("/maintenance")
async def exit_maintenance(request: Request):
    _write(request)
    try:
        ops_state.exit_maintenance(deps.actor(request))
    except ops_state.MaintenanceError as exc:
        raise _maintenance_error(exc) from None
    _ops_audit(request, "ops.maintenance_exit")
    from ..services.autobuild.runner import runner as autobuild_runner

    await autobuild_runner.admit()  # 维护期间排队的自主构建接着开始
    return {"active": False}


# ---- O-08～O-10 告警规则：运维新建的（source=kernel）与各包声明的（source=包版本，只能停用） ----

# 插件运行时登记：() → 包声明的规则（契约 7.16 AlertRule 去掉 state、enabled）
_alert_providers: list[Callable[[], list[dict]]] = []


def register_alert_provider(provider: Callable[[], list[dict]]) -> None:
    if provider not in _alert_providers:
        _alert_providers.append(provider)


def _alert_rules() -> list[dict]:
    stored = ops_state.alert_rules()
    disabled = set(stored["disabled"])
    rules = [{**rule, "state": "ok"} for rule in stored["rules"].values()]
    for provider in _alert_providers:
        rules += [{**rule, "state": "ok", "enabled": rule["id"] not in disabled} for rule in _safe(provider)]
    return sorted(rules, key=lambda rule: (rule.get("source") != "kernel", rule["id"]))


@router.get("/alerts/rules")
def list_alert_rules(request: Request):
    """O-08：告警规则。本期只登记、不评估（firing 恒为 0）。"""
    _read(request)
    return {"items": _alert_rules(), "firing": 0}


def _rule_body(body: dict, *, partial: bool = False) -> dict:
    out = {}
    for key in ("name", "condition", "window", "runbook_md"):
        if key in body or not partial:
            value = str(body.get(key) or "").strip()
            if key in ("name", "condition") and not value:
                raise ApiError("invalid_argument", f"{key} 必填", 400, {"fields": {key: "required"}})
            out[key] = value[:2000] if key == "runbook_md" else value[:200]
    if "level" in body or not partial:
        if body.get("level") not in ops_state.ALERT_LEVELS:
            raise ApiError("invalid_argument", "level 只能是 critical、warning、info", 400, {"fields": {"level": "enum"}})
        out["level"] = body["level"]
    if "auto_action" in body or not partial:
        if body.get("auto_action") not in (None, "breaker"):
            raise ApiError("invalid_argument", "auto_action 只能是 breaker", 400, {"fields": {"auto_action": "enum"}})
        out["auto_action"] = body.get("auto_action")
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    return out


@router.post("/alerts/rules")
async def create_alert_rule(request: Request):
    """O-09：新建告警规则（source=kernel）。"""
    _write(request)
    rule = {"id": f"rule-{uuid.uuid4().hex[:8]}", **_rule_body(await deps.json_body(request)), "enabled": True,
            "source": "kernel"}
    rule.setdefault("window", "5m")
    ops_state.save_alert_rule(rule)
    _ops_audit(request, "alert.create", rule=rule["id"])
    return JSONResponse(status_code=201, content={**rule, "state": "ok"})


@router.put("/alerts/rules/{rule_id}")
async def update_alert_rule(rule_id: str, request: Request):
    """O-10：修改规则；包声明的规则只能停用或重新启用。"""
    _write(request)
    body = await deps.json_body(request)
    current = next((rule for rule in _alert_rules() if rule["id"] == rule_id), None)
    if current is None:
        raise ApiError("not_found", "告警规则不存在", 404)
    if current.get("source") != "kernel":
        if set(body) - {"enabled"}:
            raise ApiError("forbidden", "包声明的规则只能停用或启用", 403)
        ops_state.set_pack_rule_enabled(rule_id, bool(body.get("enabled")))
    else:
        updated = {key: value for key, value in current.items() if key != "state"}
        updated.update(_rule_body(body, partial=True))
        ops_state.save_alert_rule(updated)
    _ops_audit(request, "alert.update", rule=rule_id)
    return next(rule for rule in _alert_rules() if rule["id"] == rule_id)


@router.delete("/alerts/rules/{rule_id}")
def delete_alert_rule(rule_id: str, request: Request):
    """O-10：删除运维新建的规则；包声明的不能删（forbidden）。"""
    _write(request)
    current = next((rule for rule in _alert_rules() if rule["id"] == rule_id), None)
    if current is None:
        raise ApiError("not_found", "告警规则不存在", 404)
    if current.get("source") != "kernel":
        raise ApiError("forbidden", "包声明的规则只能停用，不能删除", 403)
    ops_state.delete_alert_rule(rule_id)
    _ops_audit(request, "alert.delete", rule=rule_id)
    return Response(status_code=204)


# ---- O-29 熔断 ----

# 插件运行时（B3a）注册：(包名, 版本) → O-25 的一项；没注册或返回 None 视为包不存在
pack_lookup: Callable[[str, str], dict | None] | None = None


def register_pack_lookup(lookup: Callable[[str, str], dict | None]) -> None:
    global pack_lookup
    pack_lookup = lookup


@router.post("/packs/{name}/{version}/breaker")
async def breaker(name: str, version: str, request: Request):
    """O-29：熔断与恢复；熔断后节点暂时跳过（插件运行时读 ops_state.breaker_state），close 先半开。"""
    _write(request)
    item = pack_lookup(name, version) if pack_lookup else None
    if item is None:
        raise ApiError("pack_version_not_found", "包或版本不存在", 404)
    body = await deps.json_body(request)
    try:
        state = ops_state.set_breaker(name, version, str(body.get("action") or ""), deps.actor(request),
                                      str(body.get("reason") or ""))
    except ops_state.MaintenanceError as exc:
        raise _maintenance_error(exc) from None
    _ops_audit(request, "plugin.breaker", pack=f"{name}@{version}", state=state)
    return {**item, "breaker": state}


# ---- O-42 版本清单 ----


# ---- O-11、O-12 导出器 ----


@router.get("/exporters")
def exporters(request: Request):
    """O-11：本部署有的导出器（Prometheus 抓取地址、本地日志文件）。"""
    from ..services import ops_center

    _read(request)
    return {"items": ops_center.exporter_items()}


@router.put("/exporters")
async def save_exporters(request: Request):
    """O-12：改完马上生效（不用滚动重启）。"""
    from ..services import ops_center

    _write(request)
    body = await deps.json_body(request)
    try:
        items = ops_center.save_exporters(body.get("items"))
    except ops_center.OpsCenterError as exc:
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    _ops_audit(request, "ops.exporters", items=[{"id": item["id"], "enabled": item["enabled"]} for item in items])
    return {"items": items, "effective": "immediate"}


@router.get("/versions")
def versions(request: Request):
    _read(request)
    from ..kernel import metamodel

    mm = metamodel.stable()
    items = [
        {"category": "kernel", "component": "onto-modeling", "version": _app_version(), "note": None},
        {"category": "kernel", "component": "kernel_api", "version": "1", "note": "API v1"},
        {"category": "kernel", "component": "page_api", "version": "1.0", "note": None},
        {"category": "data_format", "component": "metamodel", "version": mm.METAMODEL_VERSION, "note": None},
        {"category": "data_format", "component": "ontology-package", "version": "0.1 / 0.2", "note": "启用求解契约时 0.2"},
        {"category": "engine", "component": "claude-agent-sdk", "version": ops._package_version("claude-agent-sdk") or None,
         "note": ops._cli_version() or None},
        {"category": "engine", "component": "openjiuwen", "version": ops._package_version("openjiuwen") or None, "note": None},
    ]
    items += [item for provider in _version_providers for item in _safe(provider)]
    return {"items": items}


_version_providers: list[Callable[[], list[dict]]] = []


def register_version_provider(provider: Callable[[], list[dict]]) -> None:
    """插件运行时登记包、锁文件格式等版本（B3a 接入）。"""
    _version_providers.append(provider)


def _safe(provider) -> list[dict]:
    try:
        return list(provider() or [])
    except Exception:
        return []


def _app_version() -> str:
    from ..main import app

    return app.version


# ---- O-45～O-47 诊断包 ----

DIAGNOSTIC_PARTS = ("logs_24h", "config_refs", "versions", "recent_errors", "startup_report", "pool_alert_snapshot")


def _diagnostics_keep_days() -> int:
    """诊断包保留天数：运维在数据保留里改过就用改过的（O-44），缺省 7 天。"""
    from ..services import ops_center

    return ops_center.retention_value("diagnostics")


def _diagnostics_dir() -> Path:
    return Path(config.DATA_DIR) / "ops" / "diagnostics"


def _purge_old_diagnostics() -> None:
    directory = _diagnostics_dir()
    if not directory.is_dir():
        return
    cutoff = time.time() - _diagnostics_keep_days() * 86400
    for path in directory.glob("*.zip"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def _diagnostic_item(path: Path) -> dict:
    meta_path = path.with_suffix(".json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    created = path.stat().st_mtime
    return {"id": path.stem, "name": path.name, "size": path.stat().st_size, "created_at": deps.iso(created),
            "created_by": meta.get("created_by"), "expires_at": deps.iso(created + _diagnostics_keep_days() * 86400)}


@router.post("/diagnostics")
async def create_diagnostics(request: Request):
    """O-45：诊断包。全部脱敏，不含密钥和项目内容，缺省保留 7 天（O-44 可改）。"""
    _write(request)
    body = await deps.json_body(request)
    include = body.get("include") or list(DIAGNOSTIC_PARTS)
    if not isinstance(include, list) or set(include) - set(DIAGNOSTIC_PARTS):
        raise ApiError("invalid_argument", "include 取值不合法", 400, {"fields": {"include": "enum"}})
    _purge_old_diagnostics()
    directory = _diagnostics_dir()
    directory.mkdir(parents=True, exist_ok=True)
    diag_id = f"diag-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"
    path = directory / f"{diag_id}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        if "logs_24h" in include:
            since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400))
            lines = [json.dumps(entry, ensure_ascii=False) for entry in reversed(telemetry.read_logs())
                     if (entry.get("ts") or "") >= since]
            zf.writestr("logs-24h.jsonl", telemetry.redact("\n".join(lines)))
        if "config_refs" in include:
            zf.writestr("config.json", json.dumps({"items": config_registry.items(),
                                                   "problems": config_registry.problems()}, ensure_ascii=False, indent=2))
        if "versions" in include:
            zf.writestr("versions.json", json.dumps(versions(request), ensure_ascii=False, indent=2))
        if "recent_errors" in include:
            errors = ops.errors(request, cursor="", limit=200)["items"]
            zf.writestr("errors.json", json.dumps(errors, ensure_ascii=False, indent=2))
        if "startup_report" in include:
            zf.writestr("startup-report.json", json.dumps(ops.startup_report_view(request), ensure_ascii=False, indent=2))
        if "pool_alert_snapshot" in include:
            zf.writestr("pool.json", json.dumps({"pool": ops.pool_view(), "maintenance": ops_state.maintenance_view(),
                                                 "draining": ops_state.draining()}, ensure_ascii=False, indent=2))
    path.with_suffix(".json").write_text(json.dumps({"created_by": deps.actor(request), "include": include}),
                                         encoding="utf-8")
    _ops_audit(request, "ops.diagnostics", id=diag_id)
    return JSONResponse(status_code=201, content=_diagnostic_item(path))


@router.get("/diagnostics")
def list_diagnostics(request: Request):
    _read(request)
    _purge_old_diagnostics()
    directory = _diagnostics_dir()
    paths = sorted(directory.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if directory.is_dir() else []
    return {"items": [_diagnostic_item(path) for path in paths]}


@router.get("/diagnostics/{diag_id}/download")
def download_diagnostics(diag_id: str, request: Request):
    _read(request)
    path = _diagnostics_dir() / f"{diag_id}.zip"
    if not diag_id.startswith("diag-") or "/" in diag_id or not path.is_file():
        raise ApiError("diagnostics_not_found", "诊断包不存在或已过期", 404)
    audit.record(type="ops", action="ops.diagnostics_download", actor=deps.actor(request) or "anonymous",
                 method="GET", path=request.url.path, status=200, detail={"id": diag_id})
    return Response(path.read_bytes(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{path.name}"'})


# ---- O-49 导出审计 ----


@router.get("/audit/export")
def export_audit(request: Request, type: str = "", q: str = "", format: str = "jsonl"):  # noqa: A002
    _read(request)
    if format not in ("jsonl", "csv"):
        raise ApiError("invalid_argument", "format 只能是 jsonl 或 csv", 400, {"fields": {"format": "enum"}})
    entries = [entry for _index, entry in ops.filtered_audit(request, type, q)[:AUDIT_EXPORT_LIMIT]]
    audit.record(type="ops", action="ops.audit_export", actor=deps.actor(request) or "anonymous", method="GET",
                 path=request.url.path, status=200, detail={"count": len(entries), "format": format})
    if format == "jsonl":
        content = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
        media = "application/x-ndjson"
    else:
        buffer = io.StringIO()
        columns = ("id", "ts", "actor", "actor_kind", "type", "action", "method", "path", "status", "project_id",
                   "summary", "ip", "trace_id")
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(entries)
        content, media = buffer.getvalue(), "text/csv"
    name = f"audit-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.{format}"
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- O-51 错误详情 ----


@router.get("/errors/{error_id}")
def error_detail(error_id: str, request: Request):
    _read(request)
    metas = {meta["id"]: meta for meta in deps.workspaces.list_projects()}
    from ..services import error_log

    for index, raw in ops._read_log(error_log._file()):
        record = ops.error_record(index, raw, metas)
        if record["id"] == error_id:
            stack = str(raw.get("stack") or raw.get("raw") or "")
            return {**record, "stack": telemetry.redact(stack)[:8000],
                    "context": {"project_id": record.get("project_id"), "changeset_id": None, "pack": record.get("pack"),
                                "node": record.get("node"), "exec_id": None, "session_id": raw.get("session_id")},
                    "auto_action": None}
    raise ApiError("error_record_not_found", "错误留档不存在", 404)


# ---- O-54 永久删除回收站项目 ----


@router.delete("/trash/{entry}")
def purge_trash(entry: str, request: Request):
    """O-54：先移出回收站再清除（中途重启由启动清扫接着删）。ops、admin。"""
    roles.require_ops_write(request, admin_too=True)
    directory = deps.workspaces.trash_dir()
    target = directory / entry
    if "/" in entry or entry.startswith(".") or not target.is_dir():
        raise ApiError("trash_entry_not_found", "回收站条目不存在", 404)
    staged = deps.workspaces.purge_dir() / f"trash-{entry}-{uuid.uuid4().hex[:6]}"
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(staged))
    deps.workspaces.purge_staged(staged)
    _ops_audit(request, "ops.trash_purge", entry=entry)
    return JSONResponse(status_code=202, content={"purged": entry})

