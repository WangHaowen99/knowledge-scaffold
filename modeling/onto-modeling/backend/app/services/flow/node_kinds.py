"""四类节点里不经助手执行的三类（详设 6.5；方案 D18；ADR-0066）。

- agent：助手执行入口命令（FL-05 等价于 S-20），不在这里；
- script：内核把包里的脚本（本项目装配目录里哈希核对过的那份）连同节点声明的输入文件打包，经脚本验证网关放进
  jiuwenbox 一次性沙箱运行，标准输出写成节点声明的产出、经写入事务合入。网关或沙箱不可用时不在本机回退：
  脚本来自包作者，不能在主容器里跑；
- human：只出一张决策卡（对话流 question.asked，也进待处理的「决策」，S-11 或 IN-03 回答），结论写进节点声明的
  记录文件；
- external：要经连接器网关调外部系统，需要对应的证据源提供方。本期证据源只留接口（ADR-0056），没有提供方，
  启用检查与开始节点都如实报「需要的提供方不可用」。
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
# 外部节点：需要的证据源提供方
# ---------------------------------------------------------------------------


def required_providers(nodes: list[dict]) -> list[str]:
    out = []
    for node in nodes:
        provider = node.get("provider") if isinstance(node.get("provider"), dict) else {}
        kind = str(provider.get("kind") or "")
        if kind_of(node) == "external" and kind and kind not in out:
            out.append(kind)
    return out


def missing_providers(nodes: list[dict]) -> list[str]:
    from ...evidence import available_providers

    installed = available_providers()
    return [kind for kind in required_providers(nodes) if kind not in installed]


def pack_nodes(repository, name: str, version: str) -> list[dict]:
    """包仓库里某个版本声明的节点（启用检查用）。"""
    from ..packs.assembler import flow_declaration

    declared = flow_declaration(name, repository.manifest(name, version), repository.version_dir(name, version))
    return [node for node in (declared or {}).get("nodes") or [] if isinstance(node, dict)]


# ---------------------------------------------------------------------------
# 脚本节点
# ---------------------------------------------------------------------------


def _inputs(root: Path, patterns: list[str]) -> list[str]:
    found: list[str] = []
    for pattern in patterns:
        for path in sorted(Path(root).glob(str(pattern))):
            rel = path.relative_to(root).as_posix()
            if not path.is_file() or path.is_symlink() or any(part.startswith(".") for part in Path(rel).parts):
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


def run_script(manager, project_id: str, node: dict, *, actor: str) -> dict:
    """在沙箱里跑脚本节点，标准输出写成 `script.output`；返回 {output, commit, version_label, duration_ms}。"""
    from ...kernel.writepath import ChangeTx, Origin
    from ..packs.assembler import verified_file

    spec = node.get("script") if isinstance(node.get("script"), dict) else {}
    name, output = str(spec.get("name") or ""), str(spec.get("output") or "")
    root = manager.project_root(project_id)
    script = verified_file(root, f"scripts/{name}") if name else None
    if script is None:
        raise NodeRunError("node_run_failed", "脚本不在本项目的装配目录里，或内容被改过", 409, {"script": name})
    if not output:
        raise NodeRunError("node_run_failed", "脚本节点没有声明输出", 409)
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
    with ChangeTx(manager, project_id, Origin("plugin", actor, node=node_trailer(node)),
                  summary=f"运行 {node['title']}") as tx:
        tx.write(output, text if text.endswith("\n") else text + "\n")
        committed = tx.commit(f"plugin: 运行「{node['title']}」")
    from .. import mainline

    commit = committed.commit or ""
    return {"state": "passed", "output": output, "commit": commit or None,
            "version_label": mainline.state(root)["version_label"], "duration_ms": duration, "runtime": "jiuwenbox"}


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
        question_id = f"node-{uuid.uuid4().hex[:10]}"
        question = _question(question_id, node, conversation_id)
        data[question_id] = {"node_id": node["id"], "state": "open", "question": question, "asked_at": time.time(),
                             "record": str((node.get("decision") or {}).get("record") or ""),
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
        with ChangeTx(manager, project_id, Origin("web", actor, node=item.get("trailer")),
                      summary=f"记录「{item['title']}」") as tx:
            tx.write(item["record"], text)
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
