"""三类节点里不经助手执行的两类（详设 6.5；方案 D18；ADR-0066）。

- agent：助手执行主命令（FL-05 等价于 S-20），不在这里；
- script：内核把包里的脚本（本项目装配目录里哈希核对过的那份）连同节点声明的输入文件打包，经脚本验证网关放进
  jiuwenbox 一次性沙箱运行，标准输出写成节点声明的产出、经写入事务合入。网关或沙箱不可用时不在本机回退：
  脚本来自包作者，不能在主容器里跑；
- human：只出一张决策卡（对话流 question.asked，也进待处理的「决策」，S-11 或 IN-03 回答），结论写进节点声明的
  记录文件。
"""

from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
import uuid
import zipfile
from pathlib import Path

INPUT_DIR = "inputs"
INPUT_FILES_MAX = 200
_lock = threading.RLock()


class NodeRunError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


def kind_of(node: dict) -> str:
    return str(node.get("kind") or "agent")


def node_trailer(node: dict) -> str:
    version = str(node.get("version") or "").rpartition("@")[2]
    return f"{node['id']}@{version}" if version else node["id"]


# ---------------------------------------------------------------------------
# 脚本节点
# ---------------------------------------------------------------------------


# 脚本节点的输入可以读资料，读不了内核与 git 的目录
_INPUT_FORBIDDEN = (".git", ".claude", ".onto", ".ontology-state")


def _target(node: dict, value: str, label: str) -> str:
    """节点要写的路径（脚本输出、决策记录）：规范化后不碰受保护目录、落在节点自己的写入范围里，否则 403
    （审查第 1 条：静态检查之外的第二道，绕过检查入库的包也写不了 `.git/config`、锁文件）。"""
    from ..packs import checks
    from ..packs import manifest as mf

    rel = mf.workspace_rel(value)
    if not value or rel is None or not checks.in_scope(rel, checks._scope_patterns(node)):
        raise NodeRunError("path_forbidden", f"{label} 不在节点声明的写入范围里", 403, {"path": value})
    return rel


def _sendable(root: Path, path: Path, rel: str) -> bool:
    """能送进沙箱的输入：普通文件、不是链接、不在隐藏目录里，中间段也不是目录链接。"""
    from ...kernel.artifacts import index as artifact_index

    if not path.is_file() or path.is_symlink():
        return False
    if any(part.startswith(".") for part in Path(rel).parts):
        return False
    return artifact_index.inside_no_links(root, rel) is not None


def _inputs(root: Path, patterns: list[str]) -> list[str]:
    from ..packs import manifest as mf

    found: list[str] = []
    for pattern in patterns:
        rel = mf.workspace_rel(str(pattern))
        if rel is None or rel.split("/", 1)[0] in _INPUT_FORBIDDEN:
            raise NodeRunError("path_forbidden", "脚本节点的输入不能读工作区外或内核目录", 403, {"path": str(pattern)})
        for path in sorted(Path(root).glob(rel)):
            rel = path.relative_to(root).as_posix()
            if not _sendable(root, path, rel):
                continue
            if rel not in found:
                found.append(rel)
            if len(found) > INPUT_FILES_MAX:
                raise NodeRunError("payload_too_large", f"脚本节点的输入超过 {INPUT_FILES_MAX} 个文件", 413,
                                   {"limit_files": INPUT_FILES_MAX})
    return found


def _bundle(script: Path, root: Path, inputs: list[str]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(script.name, script.read_bytes())
        for rel in inputs:
            archive.writestr(f"{INPUT_DIR}/{rel}", (Path(root) / rel).read_bytes())
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _client():
    from ..packs import contracts

    return contracts.default().load("jiuwenbox_client")


RUNS_REL = Path(".onto") / "run" / "script-runs.json"


def _first_line(text: str) -> str:
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")[:120]


def last_runs(root: Path) -> dict[str, dict]:
    """脚本节点最近一次运行结果 {节点: {state, summary, at}}（FL-01 `last_run`；`.onto/run/` 不进版本）。"""
    try:
        data = json.loads((Path(root) / RUNS_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_run(root: Path, node_id: str, state: str, summary: str) -> None:
    from datetime import datetime, timezone

    with _lock:
        data = last_runs(root)
        data[node_id] = {"state": state, "summary": summary,
                         "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        path = Path(root) / RUNS_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def run_script(manager, project_id: str, node: dict, *, actor: str) -> dict:
    """跑脚本节点并记下最近一次结果：跑了的（通过、没通过）记，沙箱不可用这类没跑起来的不记。"""
    root = manager.project_root(project_id)
    try:
        run, stdout = _run_script(manager, project_id, node, actor=actor)
    except NodeRunError as exc:
        if exc.code == "node_run_failed" and exc.status == 422:
            details = exc.details or {}
            summary = ("超时" if details.get("timed_out") else _first_line(details.get("stderr"))
                       or (f"退出码 {details['exit_code']}" if details.get("exit_code") is not None else str(exc)))
            _record_run(root, node["id"], "failed", summary)
        raise
    _record_run(root, node["id"], "passed", _first_line(stdout) or run["output"])
    # 跑完按主线现状重记基线（同 agent 节点的变更集合入）：过期、做法变了的这一步重跑后放行。原来脚本节点
    # 一旦过期，重跑也一直是过期
    from . import model
    from . import state as node_state

    node_state.confirm(root, model.load_for(root), [node["id"]])
    return run


def _run_script(manager, project_id: str, node: dict, *, actor: str) -> tuple[dict, str]:
    """在沙箱里跑脚本节点，标准输出写成 `script.output`；返回 ({output, commit, version_label, duration_ms}, 标准输出)。"""
    from ...kernel.writepath import ChangeTx, Origin
    from ..packs.assembler import verified_file

    spec = node.get("script") if isinstance(node.get("script"), dict) else {}
    name, output = str(spec.get("name") or ""), str(spec.get("output") or "")
    root = manager.project_root(project_id)
    script = verified_file(root, f"scripts/{name}") if name else None
    if script is None:
        raise NodeRunError("node_run_failed", "脚本不在本项目的装配目录里，或内容被改过", 409, {"script": name})
    output = _target(node, output, "script.output")
    client = _client()
    args = [str(item) for item in spec.get("args") or []]
    try:
        bundle = _bundle(script, root, _inputs(root, list(spec.get("inputs") or [])))
        started = time.monotonic()
        result = client.run_in_gateway(bundle, script.name, [[INPUT_DIR, *args]])[0]
    except client.BundleError as exc:
        raise NodeRunError("payload_too_large", str(exc), 413, {"limit_bytes": client.MAX_ARGUMENT_BYTES}) from exc
    except client.SandboxUnavailable as exc:
        raise NodeRunError("service_unavailable", f"脚本沙箱不可用：{exc}", 503, {"dependency": "sandbox_gateway"}) from exc
    except client.SandboxError as exc:
        raise NodeRunError("upstream_unavailable", f"脚本沙箱出错：{exc}", 502, {"upstream": "sandbox_gateway"}) from exc
    duration = int((time.monotonic() - started) * 1000)
    if result.get("timed_out") or result.get("exit_code") != 0:
        raise NodeRunError("node_run_failed", "脚本运行没有通过", 422, {
            "exit_code": result.get("exit_code"), "timed_out": bool(result.get("timed_out")),
            "stderr": str(result.get("stderr") or "")[-500:]})
    text = str(result.get("stdout") or "")
    # 执行器只回传前 OUTPUT_LIMIT 个字符：截断了就不写产出（审查第 6 条）；旧网关不带标记时按长度到顶判
    truncated = result.get("stdout_truncated")
    if truncated or (truncated is None and len(text) >= client.OUTPUT_LIMIT):
        raise NodeRunError("node_run_failed", "脚本输出超过上限", 422,
                           {"truncated": True, "limit_chars": client.OUTPUT_LIMIT})
    with ChangeTx(manager, project_id, Origin("plugin", actor, node=node_trailer(node)),
                  summary=f"运行 {node['title']}") as tx:
        tx.write(output, text if text.endswith("\n") else text + "\n")
        committed = tx.commit(f"plugin: 运行「{node['title']}」")
    from .. import mainline

    commit = committed.commit or ""
    # 包声明的提交门禁只提示（F3b）：脚本的输出照常写进版本，没过的规则带在结果里
    warnings = [{"code": "pack_gate", "level": "warning", "message": str(item.get("message") or ""),
                 "gate": str(item.get("gate") or ""), "path": str(item.get("path") or "")}
                for item in committed.findings]
    return {"state": "passed", "output": output, "commit": commit or None,
            "version_label": mainline.state(root)["version_label"], "duration_ms": duration,
            "runtime": "jiuwenbox", "warnings": warnings}, text


# ---------------------------------------------------------------------------
# 人工节点：一张决策卡
# ---------------------------------------------------------------------------


def _store(root: Path) -> Path:
    from ...kernel.changesets.worktree import data_dir_of

    return data_dir_of(Path(root)) / "flow-human" / f"{Path(root).name}.json"


def _load(root: Path) -> dict:
    try:
        data = json.loads(_store(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _save(root: Path, data: dict) -> None:
    path = _store(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _question(question_id: str, node: dict, conversation_id: str) -> dict:
    decision = node.get("decision") or {}
    options = []
    for index, option in enumerate(item for item in decision.get("options") or [] if isinstance(item, dict)):
        options.append({"id": str(option["id"]), "key": chr(65 + index) if index < 26 else str(index + 1),
                        "label": str(option.get("label") or option["id"]), "consequence": option.get("consequence"),
                        "recommended": False, "preview": None, "custom": False})
    return {
        "question_id": question_id, "kind": "decision", "conversation_id": conversation_id, "exec_id": None,
        "step_label": str(node.get("title") or ""), "eyebrow": f"需要你决定 · {node.get('title') or ''}",
        "title": str(decision.get("title") or node.get("title") or ""),
        "background_md": str(decision.get("background") or ""), "anchors": [], "options": options,
        "multi": False, "allow_custom": False, "custom_placeholder": "", "state": "open", "answer": None,
        "inbox_item_id": None, "affected": [],
    }


def ask(project_id: str, root: Path, node: dict, conversation_id: str, append) -> dict:
    """出决策卡：这个节点已经有没回答的卡就沿用它（不重复出）。append(conversation_id, record) 往对话里追加。"""
    with _lock:
        data = _load(root)
        for question_id, item in data.items():
            if item.get("node_id") == node["id"] and item.get("state") == "open":
                return item["question"]
        record = _target(node, str((node.get("decision") or {}).get("record") or ""), "decision.record")
        question_id = f"node-{uuid.uuid4().hex[:10]}"
        question = _question(question_id, node, conversation_id)
        data[question_id] = {"node_id": node["id"], "state": "open", "question": question, "asked_at": time.time(),
                             "record": record,
                             "trailer": node_trailer(node), "title": str(node.get("title") or "")}
        _save(root, data)
    append(conversation_id, {"type": "question_asked", "payload": question})
    return question


def open_questions(root: Path) -> dict[str, dict]:
    """还没回答的决策卡 {question_id: {node_id, question, asked_at, ...}}（待处理的决策来源）。"""
    with _lock:
        return {question_id: item for question_id, item in _load(root).items() if item.get("state") == "open"}


def answer(manager, project_id: str, question_id: str, body: dict, *, actor: str) -> tuple[dict, dict] | None:
    """S-11 回答人工节点的决策卡：结论经写入事务写进记录文件。返回（回答后的 Question, 要追加进对话的
    question_answered 记录）；不是人工节点的卡返回 None。对话记录由调用方在事件循环里追加。"""
    from ...kernel.writepath import ChangeTx, Origin

    root = manager.project_root(project_id)
    with _lock:
        item = _load(root).get(question_id)
        if item is None:
            return None
        if item.get("state") != "open":
            raise NodeRunError("question_gone", "问题已回答、撤回或失效", 410, {"state": "gone"})
        question = item["question"]
        option_id = str(body.get("option_id") or "")
        chosen = next((option for option in question["options"] if option["id"] == option_id), None)
        if chosen is None:
            raise NodeRunError("answer_invalid", "要选一个选项", 400)
        now = time.time()
        note = str(body.get("custom_text") or "").strip()
        text = (f"# {item['title']}\n\n- 结论：{chosen['label']}\n- 记录人：{actor or 'local'}\n"
                f"- 时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}\n" + (f"\n{note}\n" if note else ""))
        from ..packs import manifest as mf

        record_rel = mf.workspace_rel(item["record"])
        if record_rel is None or mf.protected_path(record_rel):
            raise NodeRunError("path_forbidden", "decision.record 不在节点声明的写入范围里", 403, {"path": item["record"]})
        with ChangeTx(manager, project_id, Origin("web", actor, node=item.get("trailer")),
                      summary=f"记录「{item['title']}」") as tx:
            tx.write(record_rel, text)
            tx.commit(f"web: 记录「{item['title']}」：{chosen['label']}")
        data = _load(root)
        data[question_id] = {**item, "state": "answered", "answered_at": now, "answer": {"option_id": option_id}}
        _save(root, data)
    record = {"question_id": question_id, "answer": {"option_id": option_id}, "by": "user",
              "summary": f"选了 {chosen['key']}：{chosen['label']}", "answered_at": None}
    from ..session_host.views import iso

    record["answered_at"] = iso(now)
    return {**question, "state": "answered", "answer": {"option_id": option_id}}, \
        {"type": "question_answered", "payload": record}


def purge(root: Path) -> None:
    try:
        _store(root).unlink()
    except OSError:
        pass
