#!/usr/bin/env python3
"""Standalone local web UI for the temporary agent simulation companion."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "platform" / "server"
SKILLS = ROOT / "modeling" / "skills"
SCRIPTS = ROOT / "examples" / "scripts"
import sys

for path in (SERVER, SKILLS, SCRIPTS):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

import agent_sim_runner as runner


RUN_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,120}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the standalone agent simulation UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), SimulationHandler)
    print(f"agent simulation UI: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


class SimulationHandler(BaseHTTPRequestHandler):
    server_version = "AgentSimUI/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            return self._send_html(INDEX_HTML)
        if parsed.path == "/api/simulations":
            return self._send_json({"simulations": list_simulations()})

        match = re.match(r"^/api/simulations/([^/]+)/(status|result|events|logs)$", parsed.path)
        if match:
            run_id, action = match.group(1), match.group(2)
            run_dir = resolve_run_dir(run_id)
            if run_dir is None:
                return self._send_json({"error": "simulation not found"}, status=404)
            query = parse_qs(parsed.query)
            if action == "status":
                return self._send_json(simulation_status(run_dir))
            if action == "result":
                try:
                    return self._send_json(runner.read_result(str(run_dir)))
                except ValueError as exc:
                    return self._send_json({"error": str(exc)}, status=404)
            if action == "events":
                limit = positive_int(first(query.get("limit")), default=300, maximum=1000)
                return self._send_json({"events": read_jsonl_tail(run_dir / runner.EVENTS_FILENAME, limit=limit)})
            if action == "logs":
                tail = positive_int(first(query.get("tail")), default=300, maximum=5000)
                return self._send_json({"text": read_logs(run_dir, tail=tail)})

        return self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/simulations":
            return self._send_json({"error": "not found"}, status=404)
        try:
            payload = self._read_json_body()
            question = str(payload.get("question") or "").strip()
            if not question:
                return self._send_json({"error": "question is required"}, status=400)
            run_id = str(payload.get("run_id") or "").strip()
            if run_id and not RUN_ID_RE.match(run_id):
                return self._send_json({"error": "invalid run_id"}, status=400)
            result = start_simulations(payload, question=question)
            return self._send_json(result, status=202)
        except Exception as exc:
            return self._send_json({"error": str(exc)}, status=400)

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _send_html(self, text: str, *, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_runner_args(payload: dict[str, Any], *, question: str) -> SimpleNamespace:
    context = payload.get("context")
    if not isinstance(context, dict):
        context = {}
    return SimpleNamespace(
        question=question,
        session_id=text_or_none(payload.get("session_id")),
        run_id=text_or_none(payload.get("run_id")),
        user_id=text_or_none(payload.get("user_id")) or runner.DEFAULT_USER_ID,
        workspace_id=text_or_none(payload.get("workspace_id")) or runner.DEFAULT_WORKSPACE_ID,
        output_dir=None,
        model_profile_id=text_or_none(payload.get("model_profile_id")),
        agent_mode=text_or_none(payload.get("agent_mode")),
        llm_request_mode=text_or_none(payload.get("llm_request_mode")),
        selected_ontology_id=text_or_none(payload.get("selected_ontology_id")),
        context_json=json.dumps(context, ensure_ascii=False),
        context=[],
        base_url=text_or_none(payload.get("base_url")),
        model=text_or_none(payload.get("model")),
        api_key=text_or_none(payload.get("api_key")),
        api_key_env=text_or_none(payload.get("api_key_env")),
        provider=text_or_none(payload.get("provider")) or "openai_compatible",
        temperature=payload.get("temperature"),
        max_tokens=payload.get("max_tokens"),
        timeout=payload.get("timeout"),
    )


def start_simulations(payload: dict[str, Any], *, question: str) -> dict[str, Any]:
    count = positive_int(payload.get("count"), default=1, maximum=50)
    base_run_id = text_or_none(payload.get("run_id"))
    base_session_id = text_or_none(payload.get("session_id"))
    results = []
    for index in range(count):
        item_payload = dict(payload)
        if count > 1:
            suffix = f"{index + 1:02d}"
            if base_run_id:
                item_payload["run_id"] = f"{base_run_id}_{suffix}"
            if base_session_id:
                item_payload["session_id"] = f"{base_session_id}_{suffix}"
            if text_or_none(item_payload.get("run_id")) and not RUN_ID_RE.match(str(item_payload["run_id"])):
                raise ValueError("generated run_id is invalid or too long")
        args = build_runner_args(item_payload, question=question)
        results.append(runner.start_run(args))
    return {
        "run_id": results[0]["run_id"],
        "runs": results,
        "count": len(results),
    }


def simulation_root() -> Path:
    from agentic.config.system_config import load_system_config

    return (Path(load_system_config()["storage"]["project_root"]) / runner.ARTIFACT_ROOT_NAME).resolve()


def resolve_run_dir(run_id: str) -> Path | None:
    normalized = str(run_id or "").strip()
    if not RUN_ID_RE.match(normalized):
        return None
    root = simulation_root()
    candidate = (root / normalized).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def list_simulations() -> list[dict[str, Any]]:
    root = simulation_root()
    if not root.exists():
        return []
    rows = []
    for run_dir in sorted(root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not run_dir.is_dir() or not RUN_ID_RE.match(run_dir.name):
            continue
        rows.append(simulation_summary(run_dir))
        if len(rows) >= 100:
            break
    return rows


def simulation_summary(run_dir: Path) -> dict[str, Any]:
    result = read_json(run_dir / runner.RESULT_FILENAME)
    launch = read_json(run_dir / runner.LAUNCH_FILENAME)
    spec = read_json(run_dir / runner.SPEC_FILENAME)
    status_payload = result or {
        "status": "running" if runner._pid_is_running((launch or {}).get("pid")) else "unknown",
        "pid": (launch or {}).get("pid"),
        "launched_at_ms": (launch or {}).get("launched_at_ms"),
    }
    context = (spec or {}).get("context") or {}
    return {
        "run_id": run_dir.name,
        "status": status_payload.get("status"),
        "session_id": (spec or {}).get("session_id") or status_payload.get("session_id"),
        "question": (spec or {}).get("question"),
        "agent_mode": context.get("agent_mode"),
        "llm_request_mode": context.get("llm_request_mode"),
        "created_at_ms": (spec or {}).get("created_at_ms"),
        "started_at_ms": status_payload.get("started_at_ms"),
        "finished_at_ms": status_payload.get("finished_at_ms"),
        "duration_ms": status_payload.get("duration_ms"),
        "event_count": status_payload.get("event_count"),
        "error": status_payload.get("error"),
    }


def simulation_status(run_dir: Path) -> dict[str, Any]:
    payload = runner.read_status(str(run_dir))
    spec = read_json(run_dir / runner.SPEC_FILENAME) or {}
    payload.setdefault("run_id", run_dir.name)
    payload.setdefault("session_id", spec.get("session_id"))
    payload.setdefault("question", spec.get("question"))
    return payload


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_jsonl_tail(path: Path, *, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-limit:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def read_logs(run_dir: Path, *, tail: int) -> str:
    paths = [run_dir / runner.WORKER_LOG_FILENAME]
    log_dir = run_dir / "logs"
    if log_dir.exists():
        paths.extend(sorted(log_dir.glob("*.log*")))
    lines = []
    for path in paths:
        if not path.exists():
            continue
        lines.append(f"===== {path.name} =====\n")
        try:
            lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines(True))
        except OSError as exc:
            lines.append(f"<failed to read: {exc}>\n")
    return "".join(lines[-tail:]) if tail > 0 else "".join(lines)


def positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))


def first(values: list[str] | None) -> str | None:
    return values[0] if values else None


def text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Agent Simulation UI</title>
  <style>
    :root { color-scheme: light; --accent:#4f6ef7; --border:#dde1f0; --muted:#6e7491; --text:#1a1f36; --bg:#f4f6fb; --panel:#fff; --ok:#047857; --bad:#b91c1c; --warn:#a16207; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Inter, "Microsoft YaHei", system-ui, sans-serif; color:var(--text); background:var(--bg); }
    .page { height:100vh; display:flex; flex-direction:column; }
    header { height:54px; display:flex; align-items:center; justify-content:space-between; padding:0 18px; border-bottom:1px solid var(--border); background:rgba(255,255,255,.9); }
    header h1 { margin:0; font-size:16px; }
    header button, .primary { display:inline-flex; align-items:center; gap:6px; min-height:34px; border:1px solid rgba(79,110,247,.25); border-radius:8px; background:var(--accent); color:white; padding:0 12px; font:inherit; font-weight:650; cursor:pointer; }
    main { flex:1; min-height:0; display:grid; grid-template-columns:minmax(320px,420px) minmax(260px,340px) minmax(0,1fr); gap:14px; padding:14px; }
    section { min-height:0; border:1px solid rgba(191,204,255,.65); border-radius:10px; background:var(--panel); box-shadow:0 8px 22px rgba(79,110,247,.055); overflow:hidden; }
    .panel { padding:14px; overflow:auto; }
    h2 { margin:0 0 12px; font-size:15px; }
    label { display:flex; flex-direction:column; gap:6px; margin-bottom:10px; color:var(--muted); font-size:12px; font-weight:650; }
    input, select, textarea { width:100%; min-height:34px; border:1px solid var(--border); border-radius:8px; padding:0 10px; font:inherit; font-size:13px; outline:none; background:white; }
    textarea { min-height:96px; padding:9px 10px; resize:vertical; line-height:1.45; }
    input:focus, select:focus, textarea:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(79,110,247,.11); }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .span { grid-column:1 / -1; }
    .seg { display:inline-flex; gap:4px; padding:4px; margin:0 0 12px; border:1px solid var(--border); border-radius:9px; background:#f8fafc; }
    .seg button { min-height:28px; border:0; border-radius:7px; padding:0 10px; background:transparent; color:var(--muted); cursor:pointer; }
    .seg button.active { background:white; color:var(--accent); box-shadow:0 1px 4px rgba(26,31,54,.08); }
    .runs { padding:12px; display:flex; flex-direction:column; gap:8px; overflow:auto; height:100%; }
    .run { width:100%; min-height:58px; display:grid; grid-template-columns:24px minmax(0,1fr) auto; gap:9px; align-items:center; border:1px solid var(--border); border-radius:8px; background:white; padding:8px; cursor:pointer; text-align:left; }
    .run.active { border-color:var(--accent); background:rgba(79,110,247,.07); }
    .run-title, .run-id { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .run-title { font-size:13px; line-height:1.25; }
    .run-id, .dur, .muted { color:var(--muted); font-size:12px; }
    .dot { width:12px; height:12px; border-radius:99px; background:#94a3b8; }
    .dot.success { background:var(--ok); } .dot.error { background:var(--bad); } .dot.running { background:var(--warn); }
    .detail { padding:14px; overflow:auto; }
    .top { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:12px; }
    .status { display:inline-flex; align-items:center; min-height:28px; border-radius:999px; padding:0 10px; font-size:12px; font-weight:700; background:#eef2ff; color:var(--accent); }
    .status.success { background:rgba(16,185,129,.12); color:var(--ok); } .status.error { background:rgba(239,68,68,.11); color:var(--bad); } .status.running { background:rgba(232,163,8,.14); color:var(--warn); }
    .metrics { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:12px; }
    .metric { min-height:58px; border:1px solid var(--border); border-radius:8px; padding:9px; background:#f8fafc; }
    .metric span { display:block; color:var(--muted); font-size:12px; } .metric strong { display:block; margin-top:3px; }
    .split { display:grid; grid-template-columns:minmax(0,1.1fr) minmax(260px,.9fr); gap:12px; margin-bottom:12px; }
    .box { border:1px solid var(--border); border-radius:8px; overflow:hidden; background:white; }
    .box h3 { margin:0; min-height:36px; display:flex; align-items:center; padding:0 10px; border-bottom:1px solid var(--border); font-size:13px; }
    pre { margin:0; min-height:220px; max-height:340px; overflow:auto; padding:12px; background:#f8fafc; font-size:12px; line-height:1.45; white-space:pre-wrap; overflow-wrap:anywhere; }
    .log pre { background:#111827; color:#d1d5db; max-height:280px; }
    .rounds { max-height:360px; overflow:auto; background:#f8fafc; }
    .round { border-bottom:1px solid #edf2f7; padding:10px; }
    .round-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:8px; font-size:12px; }
    .round-head strong { font-size:13px; }
    .tool { border:1px solid var(--border); border-left:4px solid var(--accent); border-radius:8px; background:white; padding:8px; margin:8px 0 0 18px; }
    .tool.kind-0 { border-left-color:#4f6ef7; background:#f8faff; }
    .tool.kind-1 { border-left-color:#0f766e; background:#f6fffb; }
    .tool.kind-2 { border-left-color:#b45309; background:#fffaf2; }
    .tool.kind-3 { border-left-color:#be123c; background:#fff7f9; }
    .tool-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:6px; font-size:12px; }
    .tool-name { display:flex; align-items:center; gap:6px; font-weight:700; }
    .tool-index { display:inline-flex; align-items:center; justify-content:center; min-width:22px; height:20px; border-radius:999px; background:rgba(26,31,54,.08); color:var(--muted); font-size:11px; }
    .snippet { margin-top:6px; }
    .tool .snippet { margin-left:14px; }
    .snippet label { margin:0 0 4px; color:var(--muted); font-size:11px; }
    .snippet pre { min-height:0; max-height:72px; padding:8px; border-radius:7px; border:1px solid #edf2f7; background:#f8fafc; }
    .snippet.expanded pre { max-height:none; }
    .mini { border:0; background:transparent; color:var(--accent); padding:0; font:inherit; font-size:12px; cursor:pointer; }
    .event { min-height:34px; display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; align-items:center; padding:0 10px; border-bottom:1px solid #edf2f7; font-size:12px; }
    .event span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .event small { color:var(--muted); }
    .error { display:none; margin:0 14px 10px; min-height:34px; align-items:center; padding:7px 10px; border:1px solid rgba(239,68,68,.24); border-radius:8px; background:rgba(239,68,68,.08); color:var(--bad); font-size:13px; }
    @media (max-width:1200px){ main{grid-template-columns:360px 1fr}.detail{grid-column:1/-1}.split{grid-template-columns:1fr} }
    @media (max-width:760px){ main,.grid,.metrics{grid-template-columns:1fr} }
  </style>
</head>
<body>
<div class="page">
  <header><h1>Agent Simulation UI</h1><button onclick="refreshAll()">刷新</button></header>
  <div id="error" class="error"></div>
  <main>
    <section class="panel">
      <h2>配置</h2>
      <label class="span">问题<textarea id="question">你的测试问题写在这里</textarea></label>
      <div class="grid">
        <label>Run ID<input id="runId" placeholder="自动生成"></label>
        <label>数量<input id="runCount" type="number" min="1" max="50" value="1"></label>
        <label>Workspace<input id="workspaceId" value="default"></label>
        <label>Agent Mode<select id="agentMode"><option>quick</option><option>thinking</option><option>multi_agent</option></select></label>
        <label>LLM Request<select id="llmRequestMode"><option>sync</option><option>stream</option></select></label>
        <label class="span">Ontology<input id="ontologyId" placeholder="可选"></label>
      </div>
      <div class="seg"><button id="profileBtn" class="active" onclick="setModelMode('profile')">Profile</button><button id="transientBtn" onclick="setModelMode('transient')">临时模型</button></div>
      <div id="profileFields"><label>Model Profile<input id="modelProfileId" placeholder="默认"></label></div>
      <div id="transientFields" style="display:none">
        <div class="grid">
          <label class="span">Base URL<input id="baseUrl"></label>
          <label>Model<input id="model"></label>
          <label>API Key Env<input id="apiKeyEnv" value="LLM_API_KEY"></label>
          <label>Temperature<input id="temperature" value="0.2"></label>
          <label>Timeout<input id="timeout" value="60"></label>
        </div>
      </div>
      <label>Context JSON<textarea id="contextJson">{}</textarea></label>
      <button class="primary" onclick="startRun()">启动</button>
    </section>
    <section><div id="runs" class="runs"></div></section>
    <section class="detail">
      <div class="top"><div><h2 id="activeTitle">未选择运行</h2><div id="activeSub" class="muted"></div></div><span id="activeStatus" class="status">unknown</span></div>
      <div class="metrics"><div class="metric"><span>Duration</span><strong id="duration">0 ms</strong></div><div class="metric"><span>Events</span><strong id="eventCount">0</strong></div><div class="metric"><span>Started</span><strong id="started">-</strong></div></div>
      <div class="box" style="margin-bottom:12px"><h3>执行轮次</h3><div id="rounds" class="rounds"></div></div>
      <div class="split"><div class="box"><h3>结果</h3><pre id="output">暂无结果</pre></div><div class="box"><h3>事件</h3><div id="events"></div></div></div>
      <div class="box log"><h3>日志</h3><pre id="logs">暂无日志</pre></div>
    </section>
  </main>
</div>
<script>
let modelMode = 'profile';
let activeRunId = '';
window.expandedSnippets = window.expandedSnippets || new Set();
let expandedSnippets = window.expandedSnippets;
function $(id){ return document.getElementById(id); }
function showError(text){ const el=$('error'); el.textContent=text||''; el.style.display=text?'flex':'none'; }
function setModelMode(mode){ modelMode=mode; $('profileBtn').classList.toggle('active', mode==='profile'); $('transientBtn').classList.toggle('active', mode==='transient'); $('profileFields').style.display=mode==='profile'?'block':'none'; $('transientFields').style.display=mode==='transient'?'block':'none'; }
async function api(path, options){ const res=await fetch(path, options); const body=await res.json().catch(()=>({})); if(!res.ok) throw new Error(body.error || 'request failed'); return body; }
async function refreshAll(){ showError(''); try { const body=await api('/api/simulations'); renderRuns(body.simulations || []); if(activeRunId) await loadDetails(activeRunId); } catch(e){ showError(e.message); } }
async function startRun(){ showError(''); try { const context=JSON.parse($('contextJson').value || '{}'); const count=Math.max(1, Math.min(50, Number($('runCount').value || 1) || 1)); const payload={question:$('question').value,count,run_id:$('runId').value||undefined,workspace_id:$('workspaceId').value||undefined,agent_mode:$('agentMode').value,llm_request_mode:$('llmRequestMode').value,selected_ontology_id:$('ontologyId').value||undefined,context}; if(modelMode==='profile'){ payload.model_profile_id=$('modelProfileId').value||undefined; } else { payload.base_url=$('baseUrl').value; payload.model=$('model').value; payload.api_key_env=$('apiKeyEnv').value||undefined; payload.temperature=$('temperature').value===''?undefined:Number($('temperature').value); payload.timeout=$('timeout').value===''?undefined:Number($('timeout').value); } const body=await api('/api/simulations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const started=body.runs || [body]; activeRunId=started[0]?.run_id || body.run_id; await refreshAll(); await loadDetails(activeRunId); } catch(e){ showError(e.message); } }
function renderRuns(runs){ const box=$('runs'); box.innerHTML=''; if(!runs.length){ box.innerHTML='<div class="muted">暂无记录</div>'; return; } if(!activeRunId) activeRunId=runs[0].run_id; for(const run of runs){ const btn=document.createElement('button'); btn.className='run'+(run.run_id===activeRunId?' active':''); btn.onclick=()=>{ activeRunId=run.run_id; renderRuns(runs); loadDetails(run.run_id); }; btn.innerHTML=`<span class="dot ${tone(run.status)}"></span><span><strong class="run-title">${esc(run.question || run.session_id || '未命名问题')}</strong><small class="run-id">${esc(run.run_id)}</small></span><span class="dur">${fmtDuration(run.duration_ms)}</span>`; box.appendChild(btn); } }
async function loadDetails(runId){ const [status, ev, logs]=await Promise.all([api(`/api/simulations/${encodeURIComponent(runId)}/status`), api(`/api/simulations/${encodeURIComponent(runId)}/events?limit=300`), api(`/api/simulations/${encodeURIComponent(runId)}/logs?tail=300`)]); $('activeTitle').textContent=status.question || status.session_id || runId; $('activeSub').textContent=status.run_id || runId; const st=$('activeStatus'); st.textContent=status.status || 'unknown'; st.className='status '+tone(status.status); $('duration').textContent=fmtDuration(status.duration_ms); $('eventCount').textContent=String((ev.events||[]).length || status.event_count || 0); $('started').textContent=fmtTime(status.started_at_ms); $('output').textContent=typeof status.final_output==='string' ? status.final_output : JSON.stringify(status.final_output || status.error || '暂无结果', null, 2); $('logs').textContent=logs.text || '暂无日志'; renderRounds(ev.events || []); const eventsBox=$('events'); eventsBox.innerHTML=''; for(const item of ev.events || []){ const data=item.data || item; const row=document.createElement('div'); row.className='event'; row.innerHTML=`<span>${esc(data.type || item.event || 'event')}</span><small>${fmtTime(data.ts)}</small>`; eventsBox.appendChild(row); } }
function renderRounds(events){ const box=$('rounds'); box.innerHTML=''; const rounds=buildRounds(events); if(!rounds.length){ box.innerHTML='<div class="event"><span>暂无轮次</span><small>-</small></div>'; return; } for(const round of rounds){ const wrap=document.createElement('div'); wrap.className='round'; const names=round.tools.map(t=>t.name).filter(Boolean).join(', ') || '无工具'; wrap.innerHTML=`<div class="round-head"><strong>第 ${esc(round.iteration)} 轮</strong><span class="muted">${esc(names)} · ${fmtDuration(round.elapsed_ms)}</span></div>`; if(round.text){ wrap.appendChild(snippet('模型输出', round.text, `round:${round.iteration}:text`)); } round.tools.forEach((tool, index)=>{ const key=tool.id || `${round.iteration}:${index}`; const node=document.createElement('div'); node.className=`tool kind-${index % 4}`; node.innerHTML=`<div class="tool-head"><span class="tool-name"><span class="tool-index">#${index + 1}</span>${esc(tool.name || 'tool')}</span><span class="muted">${esc(tool.status || 'running')} ${fmtDuration(tool.elapsed_ms)}</span></div>`; node.appendChild(snippet('输入', pretty(tool.input || tool.message || ''), `tool:${key}:input`)); node.appendChild(snippet('输出', pretty(tool.output || tool.error || ''), `tool:${key}:output`)); wrap.appendChild(node); }); box.appendChild(wrap); } }
function buildRounds(events){ const map=new Map(); const toolIndex=new Map(); for(const item of events){ const data=item.data || item; const p=data.payload || {}; const iteration=p.iteration || 0; if(!iteration) continue; if(!map.has(iteration)) map.set(iteration,{iteration,tools:[],text:'',elapsed_ms:0}); const round=map.get(iteration); if(data.type==='token.delta' && p.text) round.text += p.text; if(data.type==='iteration.completed') round.elapsed_ms=p.total_elapsed_ms || round.elapsed_ms; if(data.type==='tool.started'){ const tool={id:p.tool_call_id,name:p.tool_name,status:'running',input:p.input,message:p.message,output:'',error:'',elapsed_ms:0}; round.tools.push(tool); if(p.tool_call_id) toolIndex.set(p.tool_call_id, tool); } if(data.type==='tool.finished'){ const tool=toolIndex.get(p.tool_call_id) || {id:p.tool_call_id,name:p.tool_name,input:p.input}; tool.name=tool.name || p.tool_name; tool.status=p.status; tool.output=p.output_preview; tool.error=p.error; tool.elapsed_ms=p.elapsed_ms; if(!toolIndex.has(p.tool_call_id)) round.tools.push(tool); } } return Array.from(map.values()).sort((a,b)=>Number(a.iteration)-Number(b.iteration)); }
function snippet(label, text, key){ const wrap=document.createElement('div'); wrap.className='snippet'; const content=String(text ?? ''); wrap.innerHTML=`<label>${esc(label)}</label><pre></pre>`; wrap.querySelector('pre').textContent=content || '-'; if(key && expandedSnippets.has(key)) wrap.classList.add('expanded'); if(content.length>220){ const btn=document.createElement('button'); btn.className='mini'; btn.type='button'; btn.dataset.key=key || ''; btn.setAttribute('onclick',"const w=this.closest('.snippet'); const k=this.dataset.key; const o=w.classList.toggle('expanded'); if(k){ if(o) window.expandedSnippets.add(k); else window.expandedSnippets.delete(k); } this.textContent=o?'收起':'展开';"); btn.textContent=wrap.classList.contains('expanded')?'收起':'展开'; wrap.appendChild(btn); } return wrap; }
function pretty(value){ if(value==null || value==='') return ''; if(typeof value==='string') return value; try { return JSON.stringify(value, null, 2); } catch(e){ return String(value); } }
window.toggleSnippet = function(btn){ const wrap=btn.closest('.snippet'); if(!wrap) return; const open=wrap.classList.toggle('expanded'); const key=btn.dataset.key; if(key){ if(open) expandedSnippets.add(key); else expandedSnippets.delete(key); } btn.textContent=open?'收起':'展开'; };
function tone(status){ status=String(status||'').toLowerCase(); if(status==='success'||status==='completed') return 'success'; if(status==='error'||status==='failed') return 'error'; if(status==='running'||status==='launched') return 'running'; return ''; }
function fmtDuration(ms){ ms=Number(ms); if(!Number.isFinite(ms)||ms<=0) return '0 ms'; if(ms<1000) return Math.round(ms)+' ms'; const s=ms/1000; if(s<60) return s.toFixed(1)+' s'; return Math.floor(s/60)+'m '+Math.round(s%60)+'s'; }
function fmtTime(ms){ ms=Number(ms); return Number.isFinite(ms)&&ms>0 ? new Date(ms).toLocaleTimeString() : '-'; }
function esc(text){ return String(text ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
refreshAll(); setInterval(()=>{ if(activeRunId) loadDetails(activeRunId).catch(()=>{}); }, 2500);
</script>
</body>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
