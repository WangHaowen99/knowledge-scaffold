"""数据评测编排：onto-modeling 作为 modeling 评测 API 的编排方（方案 P2）。

链路（全部走 modeling 现成 API，见 docs/eval-optimization-integration.md §2.3）：
  发布知识包 zip → POST /api/package-workspaces/import（自动建 kind=package 项目
  并导入包版本）→ POST testcases/batch（trace 场景转换的用例）→ POST evaluate
  → 轮询 GET rounds/:roundId → 报告落 reports/eval-<时间戳>.md（git 版本化）。

- 鉴权：透传当前用户 token（共享账号中心/方案 A 下 modeling 直接接受）；
  无共享 auth 的部署用 ONTO_WEB_MODELING_TOKEN 兜底；
- 数据源：none（纯知识包问答，显式 PUT mode=none）/ dataset（quick-import 建评测库，
  需 modeling 侧 EVAL_DB_URL）/ connection（NL2SQL Template/HTTP、Neo4j 外部数据源，
  PUT data-source 由 modeling 测试连接并同步到 consumer）；配置见 eval_data_sources；
- 一键导入：dataset 与带导入数据的 Neo4j 都先由 eval_import 生成规范化工作簿，发布包
  mapping 按同一份表结构导出（Neo4j 用图模式），导出后逐项核对再导入；
- 进度：状态落 .ontology-state/eval.json（前端轮询），评测项目保留在
  modeling 侧可追溯。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .. import config
from . import eval_data_sources, eval_import

STATE_REL = ".ontology-state/eval.json"
POLL_INTERVAL_SECONDS = 6
ROUND_TIMEOUT_SECONDS = 1800
AUTH_GRACE_SECONDS = 600
DATA_IMPORT_TIMEOUT_SECONDS = 600  # 凭据过期后等待前端接力新 token 的宽限窗
# running 状态由编排线程每轮刷 ts；内存里已无编排且 ts 停滞超过此窗即判为中断
STALE_AFTER_SECONDS = POLL_INTERVAL_SECONDS * 5
GRAPH_IMPORT_TIMEOUT_SECONDS = 1200
# 得分低于此线、或出现异常用例时，提示进入 /ontology-evolution 评测优化
EVOLUTION_SCORE_THRESHOLD = 90

_running: set[str] = set()  # project_id 去重（单进程内防并发重复触发）
_live_tokens: dict[str, str] = {}  # project_id → 前端轮询接力的最新 token
_stop_requested: set[str] = set()  # 用户请求停止的 project_id（协作式取消）

STOPPED_MESSAGE = "评测已停止（modeling 侧轮次已取消，已完成用例的结果仍保留在 modeling 侧）"
INTERRUPTED_MESSAGE = (
    "上一轮评测被服务重启中断：modeling 侧轮次可能仍在执行或已完成，"
    "可「续接上一轮」收取结果"
)


class EvalStopped(Exception):
    """用户主动停止评测——与编排失败区分开：状态落 stopped，且允许重跑。"""


def _check_stop(project_id: str) -> None:
    """协作式取消点：编排的每个可中断处调用，命中停止请求即退出编排。"""
    if project_id in _stop_requested:
        raise EvalStopped(STOPPED_MESSAGE)


def update_token(project_id: str, token: str) -> None:
    """评测轮常超过 access token 有效期（约 15 分钟）。前端本就带着自动刷新
    的 token 轮询 /eval/status——顺手把新鲜 token 接力给运行中的编排。"""
    if token and project_id in _running:
        _live_tokens[project_id] = token


def enabled() -> bool:
    return bool(config.MODELING_URL)


def reachable(timeout: float = 3.0) -> bool:
    if not enabled():
        return False
    try:
        req = urllib.request.Request(config.MODELING_URL + "/", method="GET")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # 有 HTTP 响应即视为在线（401/404 都算活着）
    except Exception:
        return False


def _http(
    method: str,
    path: str,
    token: str,
    body: dict | None = None,
    data: bytes | None = None,
    content_type: str = "application/json",
    timeout: float = 60.0,
) -> dict:
    payload = data if data is not None else (
        json.dumps(body).encode() if body is not None else None
    )
    req = urllib.request.Request(
        config.MODELING_URL + path,
        method=method,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        # body 只能读一次：把 modeling 的业务错误话术挂到异常上供状态呈现
        try:
            payload = json.loads(exc.read().decode() or "{}")
            exc.detail = str(payload.get("error") or payload.get("message") or "").strip()
        except Exception:
            exc.detail = ""
        raise


def _multipart_zip(field_name: str, filename: str, content: bytes, extra: dict) -> tuple[bytes, str]:
    boundary = f"----ontoeval{uuid.uuid4().hex[:12]}"
    parts = []
    for key, value in extra.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/zip\r\n\r\n"
        ).encode()
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _multipart_files(files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    """quick-import 的多 Excel multipart（服务端按 part 类型收文件，字段名不限）。"""
    boundary = f"----ontoeval{uuid.uuid4().hex[:12]}"
    parts = []
    for filename, content in files:
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
                f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ---- 数据导入：MySQL 评测库（quick-import）与 Neo4j 图库（graph-import）----


def needs_import(data_source: dict) -> bool:
    """dataset 模式、或声明了导入数据的 Neo4j 连接，发起前要整理导入工作簿。"""
    if str(data_source.get("mode") or "none") == "dataset":
        return True
    has_selection = bool(data_source.get("files") or data_source.get("include_mock"))
    return is_graph(data_source) and has_selection


def is_graph(data_source: dict) -> bool:
    return data_source.get("mode") == "connection" and data_source.get("provider") == "neo4j"


def _graph_import(
    project_id: str, root: Path, tok, mp_project: str, data_source: dict, plan: dict
) -> None:
    """Neo4j 一键导入：上传工作簿 → 推断关系关联 → 校验 → 执行（清空本隔离范围旧数据）→ 轮询。"""
    rows = sum(len(t.rows) for t in plan["tables"])
    _write_state(
        root, phase="graph_import",
        message=f"图数据导入：上传 {len(plan['tables'])} 张表（{rows} 行）",
    )
    body, ctype = _multipart_files([(eval_import.WORKBOOK_NAME, plan["workbook"])])
    try:
        upload = _http(
            "POST", f"/api/projects/{mp_project}/graph-import/uploads", tok(),
            data=body, content_type=ctype, timeout=180,
        )["upload"]
        relations, skipped = eval_import.graph_relation_mapping(upload)
        mapping = {"relations": relations}
        validation = _http(
            "POST", f"/api/projects/{mp_project}/graph-import/uploads/{upload['id']}/validate",
            tok(), body={"mapping": mapping},
        )["validation"]
        _check_stop(project_id)
        run = _http(
            "POST", f"/api/projects/{mp_project}/graph-import/uploads/{upload['id']}/execute",
            tok(),
            body={
                "mapping": mapping,
                "password": (data_source.get("config") or {}).get("password", ""),
                # 隔离范围 = 评测工作空间 + 本知识包 ID，只属于本项目的评测数据，每轮重灌
                "replaceExisting": True,
            },
        )["run"]
    except urllib.error.HTTPError as exc:
        detail = str(getattr(exc, "detail", "") or "") or f"HTTP {exc.code}"
        raise RuntimeError(f"图数据导入失败：{detail}") from exc
    finished = _poll_graph_import(project_id, root, tok, mp_project, run["id"])
    notes = skipped + list(validation.get("warnings") or [])
    _write_state(
        root,
        graph_import={
            "nodes": finished.get("nodeCount", 0),
            "relationships": finished.get("relationshipCount", 0),
            "relations_mapped": len(relations),
            "notes": notes[:20],
        },
    )


def _poll_graph_import(project_id: str, root: Path, tok, mp_project: str, run_id: str) -> dict:
    deadline = time.time() + GRAPH_IMPORT_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        _check_stop(project_id)
        runs = _http("GET", f"/api/projects/{mp_project}/graph-imports", tok()).get("imports") or []
        run = next((r for r in runs if r.get("id") == run_id), {})
        status = run.get("status")
        if status == "done":
            return run
        if status in ("failed", "cancelled"):
            raise RuntimeError(f"图数据导入失败：{str(run.get('error') or status)[:200]}")
        _write_state(root, message="图数据导入中（写入节点与关系）…")
    raise TimeoutError("图数据导入超时")


def _poll_data_import(project_id: str, root: Path, tok, mp_project: str) -> None:
    deadline = time.time() + DATA_IMPORT_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        _check_stop(project_id)
        data = _http(
            "GET", f"/api/projects/{mp_project}/evaluation-database/imports", tok()
        )
        runs = data.get("imports") or []
        run = runs[0] if runs else {}
        status = run.get("status")
        if status == "done":
            return
        if status == "failed":
            raise RuntimeError(f"评测库导入失败：{str(run.get('error') or '原因未知')[:200]}")
        _write_state(root, message="评测库导入中（建表并写入数据）…")
    raise TimeoutError("评测库导入超时")


# ---- trace 场景 → 评测用例 ----


def trace_cases(root: Path) -> list[dict]:
    """ontology/testcase/*.md 的 TC 段落 → (question, groundTruth) 用例。

    TC 段落是 K-V 表：`输入-*` 行拼为问题条件，`预期-*` 行拼为标准答案。
    """
    cases: list[dict] = []
    tc_dir = root / "ontology" / "testcase"
    if not tc_dir.is_dir():
        return cases
    for doc in sorted(tc_dir.glob("*.md")):
        if doc.name == "index.md":
            continue
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:
            continue
        subject = doc.stem
        for match in re.finditer(r"##\s*(TC-\d+[：:][^\n]*)\n(.*?)(?=\n##\s|\Z)", text, re.S):
            title, body = match.group(1).strip(), match.group(2)
            inputs, expected = [], []
            for line in body.splitlines():
                if not line.strip().startswith("|"):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 2 or set(cells[0]) <= set("-: "):
                    continue
                key, value = cells[0], cells[1]
                if key.startswith("输入"):
                    inputs.append(f"{key.split('-', 1)[-1]}：{value}")
                elif key.startswith("预期"):
                    expected.append(f"{key.split('-', 1)[-1]}：{value}")
            if not inputs or not expected:
                continue
            cases.append(
                {
                    "question": f"关于「{subject}」（{title}）：{'；'.join(inputs)}。按业务规则应得到什么结果？",
                    "groundTruth": "；".join(expected),
                    "source": f"ontology/testcase/{doc.name}",
                }
            )
    return cases


MAX_IMPORT_CASES = 500


def parse_case_json(raw: str) -> list[dict]:
    """用例 JSON → [{question, groundTruth}]（与 modeling 的 testcase-import 契约一致）。

    顶层数组或 {cases: [...]}；question/prompt 必填；
    groundTruth/ground_truth/answer/referenceAnswer 可选；重复问题拒绝。
    """
    try:
        value = json.loads(raw or "")
    except (ValueError, TypeError) as exc:
        raise ValueError("用例文件不是有效的 JSON") from exc
    rows = value if isinstance(value, list) else (value or {}).get("cases")
    if not isinstance(rows, list):
        raise ValueError("用例文件必须是数组或包含 cases 数组")
    if not rows:
        raise ValueError("用例文件中没有可导入内容")
    if len(rows) > MAX_IMPORT_CASES:
        raise ValueError(f"一次最多导入 {MAX_IMPORT_CASES} 条真实用例")

    def text(v):
        return "" if v is None else str(v).strip()

    seen = set()
    cases = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条用例格式不正确")
        question = text(row.get("question") or row.get("prompt"))
        if not question:
            raise ValueError(f"第 {index} 条缺少问题")
        key = " ".join(question.split()).lower()
        if key in seen:
            raise ValueError(f"用例文件包含重复问题：第 {index} 条")
        seen.add(key)
        cases.append({
            "question": question,
            "groundTruth": text(
                row.get("groundTruth") or row.get("ground_truth")
                or row.get("answer") or row.get("referenceAnswer")
            ),
        })
    return cases


def case_template_json() -> str:
    """导入模板（与 modeling 的下载模板同构）。"""
    return json.dumps({
        "cases": [
            {"question": "示例业务问题", "groundTruth": "可选：填写标准答案"},
            {"question": "仅用于真实执行的问题", "groundTruth": ""},
        ],
    }, ensure_ascii=False, indent=2) + "\n"


# ---- 状态与报告 ----


def _state_path(root: Path) -> Path:
    return root / STATE_REL


def read_state(root: Path) -> dict:
    path = _state_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(root: Path, **fields: Any) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = read_state(root)
    state.update(fields, ts=int(time.time()))
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _resumable(state: dict) -> bool:
    """能否续接：modeling 侧有轮次坐标才谈得上收取已有结果；该轮报告已收取
    （异常终止轮的部分结果已落报告）则无可续接。"""
    if not (state.get("modeling_project") and state.get("round_id")):
        return False
    return state.get("report_round") != state.get("round_id")


def status_view(project_id: str, root: Path) -> dict:
    """前端可见的评测状态：把"内存里已无编排、状态却仍是 running"的孤儿纠正为中断。

    running 只由本进程的编排线程维护（每轮刷 ts）。进程重启或线程意外退出后
    内存态消失，而 eval.json 会永远停在 running，把运行按钮永久锁死；此处按
    "无内存态 + ts 停滞"双条件收敛，并标出是否可续接。
    """
    state = read_state(root)
    if (
        state.get("status") == "running"
        and project_id not in _running
        and int(time.time()) - int(state.get("ts") or 0) >= STALE_AFTER_SECONDS
    ):
        _write_state(root, status="interrupted", message=INTERRUPTED_MESSAGE)
        state = read_state(root)
    if state.get("status") in ("interrupted", "failed"):
        state = {**state, "resumable": _resumable(state)}
    return state


def sweep_interrupted(data_dir: Path | None = None) -> list[str]:
    """进程启动时收敛孤儿状态：新进程里不存在任何编排，仍标 running 的工作区
    一律改写为 interrupted（保留轮次坐标以便续接）。返回被改写的项目 ID。"""
    base = Path(data_dir if data_dir is not None else config.DATA_DIR) / "workspaces"
    touched: list[str] = []
    if not base.is_dir():
        return touched
    for state_path in sorted(base.glob(f"*/{STATE_REL}")):
        root = state_path.parent.parent
        if read_state(root).get("status") != "running":
            continue
        _write_state(root, status="interrupted", message=INTERRUPTED_MESSAGE)
        touched.append(root.name)
    return touched


def _unexecuted_as_errors(
    results: list[dict], cases: list[dict] | None, failure: str
) -> list[dict]:
    """轮次异常终止时，没跑到的用例补记为异常。

    报告与得分覆盖整个用例集，/ontology-evolution 按 E 类（运行异常）给出指引，
    而不是只看跑完的那几条。
    """
    executed = [str(r.get("question", "")).strip() for r in results]
    missing = []
    for case in cases or []:
        question = str(case.get("question", "")).strip()
        # 续接场景的 cases 取自状态里截断过的问题，前缀匹配同时覆盖完全相等
        if not question or any(q.startswith(question) for q in executed):
            continue
        missing.append({
            "question": question,
            "groundTruth": case.get("groundTruth", ""),
            "finalAnswer": "",
            "verdict": "error",
            "reason": f"轮次异常终止，本用例未执行：{failure}",
        })
    return list(results) + missing


def _write_report(
    root: Path,
    round_info: dict,
    results: list[dict],
    cases: list[dict] | None = None,
    failure: str = "",
) -> str:
    """落评测报告并把状态收敛到终态。

    failure 非空 = modeling 侧轮次异常终止但已有部分结果：照样出报告（状态 failed），
    让失分与异常用例能进入 /ontology-evolution，而不是随失败状态一起丢掉。
    """
    executed = len(results)
    if failure:
        results = _unexecuted_as_errors(results, cases, failure)
    counts = {"pass": 0, "partial": 0, "fail": 0, "error": 0}
    for item in results:
        counts[item.get("verdict", "error")] = counts.get(item.get("verdict", "error"), 0) + 1
    total = len(results) or 1
    score = round((counts["pass"] + counts["partial"] * 0.5) * 100 / total)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rel = f"reports/eval-{stamp}.md"
    state = read_state(root)
    lines = [
        f"# 数据评测报告 — {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"> 评测方式：知识包发布至 modeling 影子工作区，由 consumer 智能体逐用例问答，LLM Judge 判分",
        f"> 轮次：{round_info.get('id', '')}（modeling 侧可追溯）",
    ]
    if state.get("data_source_name"):
        lines.append(f"> 数据源：{state['data_source_name']}")
    if state.get("eval_set_name"):
        lines.append(f"> 评测集：{state['eval_set_name']}")
    if failure:
        lines.append(
            f"> ⚠️ 轮次异常终止：{failure}（已执行 {executed}/{len(results)} 条，"
            "未执行的用例计为异常）"
        )
    lines += [
        "",
        "## 总览",
        "",
        f"- 用例：{len(results)}",
        f"- 通过 {counts['pass']} / 部分 {counts['partial']} / 失败 {counts['fail']} / 异常 {counts['error']}",
        f"- 综合得分：**{score}**（pass=1、partial=0.5 加权）",
        "",
        "## 逐用例明细",
        "",
    ]
    for index, item in enumerate(results, 1):
        mark = {"pass": "✅", "partial": "⚠️", "fail": "❌"}.get(item.get("verdict"), "🛑")
        lines += [
            f"### {index}. {mark} {item.get('verdict', 'error')}",
            "",
            f"- **问题**：{item.get('question', '')}",
            f"- **标准答案**：{item.get('groundTruth', '')}",
            f"- **智能体回答**：{(item.get('finalAnswer') or '').strip()[:600] or '（无）'}",
            f"- **判分理由**：{item.get('reason', '')}",
            "",
        ]
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("\n".join(lines), encoding="utf-8")
    terminal = {"status": "failed", "message": (
        f"{failure}（已执行 {executed}/{len(results)} 条，报告已生成）"[:300]
    )} if failure else {"status": "done"}
    _write_state(
        root,
        **terminal,
        report=rel,
        # 报告出自哪一轮：新一轮发起时清空，用来判断报告是否属于最近一轮
        report_round=round_info.get("id") or state.get("round_id") or rel,
        summary={"total": len(results), **counts, "score": score},
        cases=_case_progress(
            [{"question": i.get("question", "")} for i in results], results, False
        ),
    )
    return rel


def evolution_hint(state: dict) -> dict | None:
    """最近一轮评测是否该进入 /ontology-evolution（聊天区建议与左栏按钮共用）。

    得分低于阈值、或出现异常（error）用例即提示——异常用例会拉低得分，但少量异常
    可能仍在阈值之上；轮次异常终止但有部分结果时同样提示（报告已落盘）。
    """
    report = state.get("report")
    summary = state.get("summary") or {}
    if not (report and summary):
        return None
    if state.get("status") not in ("done", "failed"):
        return None
    # 报告须出自最近一轮；旧版状态没有 report_round 字段，done 即视为最近一轮
    fresh = state.get("report_round") if "report_round" in state else state.get("status") == "done"
    if not fresh:
        return None
    score = summary.get("score")
    errors = int(summary.get("error") or 0)
    low_score = score is not None and score < EVOLUTION_SCORE_THRESHOLD
    if not (errors or low_score):
        return None
    return {
        "report": report,
        "score": score,
        "errors": errors,
        "fails": int(summary.get("fail") or 0),
        "round_failed": state.get("status") == "failed",
    }


def _case_progress(cases: list[dict], results: list[dict], round_running: bool) -> list[dict]:
    """逐用例执行状态（前端进度视图）：已出结果的带判分与摘要；modeling 按序
    执行，第一个未出结果的用例标记为执行中，其余待执行。"""
    by_question: dict[str, dict] = {}
    for item in results:
        by_question.setdefault(str(item.get("question", "")).strip(), item)
    out = []
    waiting_marked = False
    for case in cases[:500]:
        question = str(case.get("question", "")).strip()
        item = by_question.get(question)
        if item is None and question:
            # 续接场景的 cases 取自状态里截断过的问题，退回前缀匹配
            item = next(
                (r for q, r in by_question.items() if q.startswith(question)), None
            )
        if item is not None:
            out.append({
                "question": question[:120],
                "verdict": item.get("verdict") or "error",
                "answer": str(item.get("finalAnswer") or "").strip()[:200],
                "reason": str(item.get("reason") or "").strip()[:160],
            })
        else:
            out.append({
                "question": question[:120],
                "verdict": "running" if (round_running and not waiting_marked) else "pending",
            })
            waiting_marked = True
    return out


def _poll_round(
    project_id: str, root: Path, token: str, mp_project: str, round_id: str, cases: list[dict]
) -> None:
    """轮询 modeling 轮次直至终态并落报告。401 不判死：轮次仍在 modeling 侧
    执行，宽限窗内等前端接力新鲜 token（页面开着即到）。"""

    def tok() -> str:
        return _live_tokens.get(project_id) or token

    deadline = time.time() + ROUND_TIMEOUT_SECONDS
    details: dict = {}
    auth_wait_until: float | None = None
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        _check_stop(project_id)
        try:
            details = _http("GET", f"/api/projects/{mp_project}/rounds/{round_id}", tok())
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            if auth_wait_until is None:
                auth_wait_until = time.time() + AUTH_GRACE_SECONDS
                _write_state(root, message="评测执行中（登录凭据过期，等待页面接力新凭据…）")
            if time.time() < auth_wait_until:
                continue
            raise RuntimeError(
                "评测凭据过期且未获接力（页面已关闭？）。modeling 侧轮次仍在执行，"
                "重新打开项目页并重跑评测即可续接收取结果"
            ) from exc
        auth_wait_until = None
        _check_stop(project_id)  # 取消请求已落 stopped 状态，不再覆写进度
        round_info = details.get("round") or {}
        _write_state(
            root,
            message=(
                f"评测执行中 {round_info.get('progressDone', 0)}"
                f"/{round_info.get('progressTotal', 0)}"
            ),
            cases=_case_progress(
                cases, details.get("results") or [], round_info.get("status") == "running"
            ),
        )
        if round_info.get("status") in ("done", "failed"):
            break
    round_info = details.get("round") or {}
    if round_info.get("status") not in ("done", "failed"):
        raise TimeoutError("评测超时（modeling 侧轮次未在时限内结束）")
    if round_info.get("status") == "failed":
        _collect_failed_round(root, round_info, details.get("results") or [], cases)
        return
    rel = _write_report(root, round_info, details.get("results") or [])
    _git_commit(root, f"eval: 数据评测报告 {rel.split('/')[-1]}")


def _round_failure(round_info: dict) -> str:
    log = (round_info.get("log") or "").strip().splitlines()
    return f"modeling 评测轮失败：{log[-1] if log else '原因未知'}"


def _collect_failed_round(
    root: Path, round_info: dict, results: list[dict], cases: list[dict]
) -> None:
    """轮次在 modeling 侧失败：透出其日志，有结果就落报告。

    已有用例结果（常见于连续环境异常熔断）时照样落报告，没有任何结果才按编排失败处理。
    """
    failure = _round_failure(round_info)
    if not results:
        raise RuntimeError(failure)
    rel = _write_report(root, round_info, results, cases, failure=failure)
    _git_commit(root, f"eval: 数据评测报告（轮次异常终止） {rel.split('/')[-1]}")


def _try_resume(
    project_id: str, root: Path, token: str, cases: list[dict],
    collect_failed: bool = False,
) -> bool:
    """续接 modeling 侧已有轮次，收取结果而不重跑。

    凭据过期、服务重启等中断后，modeling 侧轮次往往仍在执行或已完成。重跑且
    用例集未变时续接收取结果，不重复导入、不重复烧模型。

    collect_failed：显式「续接上一轮」时，已失败但有部分结果的轮次也收取成报告；
    重新运行评测时不收（用户要的是新一轮）。"""
    prior = read_state(root)
    mp_project, round_id = prior.get("modeling_project"), prior.get("round_id")
    if not mp_project or not round_id:
        return False
    if prior.get("status") not in ("failed", "running", "interrupted"):
        return False
    prior_questions = [c.get("question") for c in prior.get("cases") or []]
    now_questions = [str(c.get("question", "")).strip()[:120] for c in cases]
    if prior_questions and now_questions and prior_questions != now_questions:
        return False
    try:
        details = _http(
            "GET",
            f"/api/projects/{mp_project}/rounds/{round_id}",
            _live_tokens.get(project_id) or token,
        )
    except Exception:
        return False
    status = (details.get("round") or {}).get("status")
    if status == "running":
        _write_state(root, status="running", phase="evaluate",
                     message="续接上一轮（modeling 侧仍在执行）")
        _poll_round(project_id, root, token, mp_project, round_id, cases)
        return True
    if status == "done":
        _write_state(root, status="running", phase="evaluate",
                     message="续接上一轮（收取已完成结果）")
        rel = _write_report(root, details.get("round") or {}, details.get("results") or [])
        _git_commit(root, f"eval: 数据评测报告 {rel.split('/')[-1]}")
        return True
    partial_results = collect_failed and bool(details.get("results"))
    if status == "failed" and partial_results:
        _write_state(root, status="running", phase="evaluate",
                     message="续接上一轮（收取异常终止前的结果）")
        _collect_failed_round(root, details.get("round") or {}, details["results"], cases)
        return True
    return False


def _git_commit(root: Path, message: str) -> None:
    import subprocess

    for args in (["add", "-A"], ["commit", "-q", "-m", message]):
        subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=root, capture_output=True, text=True, timeout=30,
        )


# ---- 停止与续接 ----


def request_stop(project_id: str, root: Path, token: str) -> dict:
    """停止评测：取消 modeling 侧轮次，并把状态收敛到可重跑的终态。

    两种现场都要能停下来：
    - 进程内有编排 → 置协作取消标记，轮询下一拍退出（不再覆写状态）；
    - 进程内无编排（重启后的中断状态）→ 只需取消远端轮次并收敛状态。

    modeling 返回 404/409（轮次不存在或已结束）视为目标状态已达成，不算失败。
    """
    state = read_state(root)
    if state.get("status") not in ("running", "interrupted"):
        raise ValueError("当前没有进行中的评测")
    if project_id in _running:
        _stop_requested.add(project_id)

    mp_project, round_id = state.get("modeling_project"), state.get("round_id")
    cancelled, detail = False, ""
    if mp_project and round_id:
        try:
            _http(
                "POST",
                f"/api/projects/{mp_project}/evaluate/cancel",
                _live_tokens.get(project_id) or token,
                body={"roundId": round_id},
                timeout=30,
            )
            cancelled = True
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 409):
                detail = str(getattr(exc, "detail", "") or exc)
        except Exception as exc:  # 网络不通等：本地状态照样收敛
            detail = str(exc)

    message = (
        STOPPED_MESSAGE if not detail
        else f"评测已停止（modeling 侧取消未确认：{detail[:120]}）"
    )
    _write_state(root, status="stopped", message=message)
    return {"stopped": True, "cancelled": cancelled, "message": message}


def can_resume(root: Path) -> bool:
    """是否具备续接条件（状态为中断/失败且有轮次坐标）。"""
    state = read_state(root)
    return state.get("status") in ("interrupted", "failed") and _resumable(state)


async def resume(project_id: str, root: Path, token: str) -> None:
    """续接上一轮：只收取 modeling 侧已有轮次的结果，绝不重新发起评测。"""
    if project_id in _running:
        return
    _running.add(project_id)
    _stop_requested.discard(project_id)
    _live_tokens[project_id] = token
    try:
        await asyncio.to_thread(_resume_sync, project_id, root, token)
    finally:
        _running.discard(project_id)
        _live_tokens.pop(project_id, None)
        _stop_requested.discard(project_id)


def _resume_sync(project_id: str, root: Path, token: str) -> None:
    prior = read_state(root)
    # 状态里的问题是截断过的，只用于进度展示；报告内容取 modeling 侧结果
    cases = [
        {"question": str(c.get("question", "")), "groundTruth": ""}
        for c in prior.get("cases") or []
    ]
    try:
        if not _try_resume(project_id, root, token, cases, collect_failed=True):
            # 轮次已失败、或这次读不到：保持原状态，轮次坐标留着供再试
            _write_state(
                root, status=prior.get("status") or "interrupted",
                message="上一轮无法续接（modeling 侧轮次已失败或暂时读不到）"
                        "——可稍后重试，或重新运行评测",
            )
    except EvalStopped:
        if read_state(root).get("status") != "stopped":
            _write_state(root, status="stopped", message=STOPPED_MESSAGE)
    except Exception as exc:
        detail = str(getattr(exc, "detail", "") or "")
        text = f"{exc}：{detail}" if detail else str(exc)
        _write_state(root, status="failed", message=f"续接失败：{text}"[:300])


# ---- 编排主体 ----


def _configure_connection(
    root: Path, tok, mp_project: str, data_source: dict, ontology_id: str
) -> None:
    """外部数据源：modeling 侧测试连接并同步到 consumer 评测工作空间。

    Neo4j 的 Ontology 隔离值必须等于知识包 ID，这里按本次发布的包 ID 填写。
    """
    provider = str(data_source.get("provider") or "")
    label = eval_data_sources.PROVIDER_LABELS.get(provider, provider)
    _write_state(root, phase="data_source", message=f"配置数据源（{label}：测试连接并同步）")
    try:
        _http(
            "PUT", f"/api/projects/{mp_project}/data-source", tok(),
            body=eval_data_sources.modeling_payload(data_source, ontology_id),
            timeout=120,
        )
    except urllib.error.HTTPError as exc:
        detail = str(getattr(exc, "detail", "") or "") or f"HTTP {exc.code}"
        raise RuntimeError(f"数据源配置失败（{label}）：{detail}") from exc


async def run(
    project_id: str, root: Path, token: str, cases: list[dict],
    data_source: dict | None = None, eval_set_name: str = "",
) -> None:
    """后台评测编排；进度经 .ontology-state/eval.json 供前端轮询。"""
    if project_id in _running:
        return
    _running.add(project_id)
    _stop_requested.discard(project_id)  # 上一轮遗留的取消标记不影响新编排
    _live_tokens[project_id] = token
    try:
        await asyncio.to_thread(
            _run_sync, project_id, root, token, cases, data_source, eval_set_name
        )
    finally:
        _running.discard(project_id)
        _live_tokens.pop(project_id, None)
        _stop_requested.discard(project_id)


def _run_sync(
    project_id: str, root: Path, token: str, cases: list[dict],
    data_source: dict | None = None, eval_set_name: str = "",
) -> None:
    from .workspace import WorkspaceManager

    def tok() -> str:
        return _live_tokens.get(project_id) or token

    data_source = data_source or {"mode": "none"}
    mode = str(data_source.get("mode") or "none")
    dataset_mode = mode == "dataset"
    try:
        # 导入数据先整理成工作簿：坏输入快速失败，不浪费一次包导入
        plan = (
            eval_import.prepare(root, data_source, graph=is_graph(data_source))
            if needs_import(data_source) else None
        )
        if _try_resume(project_id, root, token, cases):
            return
        _check_stop(project_id)
        _write_state(
            root, status="running", phase="package", message="打包知识包",
            # 新一轮的逐轮字段清空：图导入结果等只属于产生它的那一轮
            report_round=None, graph_import=None, started_at=int(time.time()),
            data_source_name=data_source.get("name") or eval_data_sources.default_name(data_source),
            eval_set_name=eval_set_name,
        )
        manager = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)
        import tempfile

        zip_path = Path(tempfile.mkdtemp(prefix="onto-eval-")) / "package.zip"
        manager.export_package_zip(
            project_id, zip_path,
            binding=plan["binding"] if plan else None, graph=is_graph(data_source),
        )
        if plan:
            mismatched = eval_import.verify_mapping(zip_path, plan["tables"])
            if mismatched:
                raise RuntimeError("发布包 mapping 与导入数据不一致：" + "；".join(mismatched[:3]))

        _write_state(root, phase="import", message="导入 modeling 评测工作区")
        package_name = f"onto-eval-{project_id}"
        content = zip_path.read_bytes()
        try:
            body, ctype = _multipart_zip(
                "file", "package.zip", content, {"package_name": package_name}
            )
            imported = _http("POST", "/api/package-workspaces/import", tok(),
                             data=body, content_type=ctype, timeout=120)
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise
            # 重名（此前评测轮留下的同名包）：追加时间戳重试
            body, ctype = _multipart_zip(
                "file", "package.zip", content,
                {"package_name": f"{package_name}-{int(time.time())}"},
            )
            imported = _http("POST", "/api/package-workspaces/import", tok(),
                             data=body, content_type=ctype, timeout=120)
        mp_project = imported["project"]["id"]
        _write_state(root, modeling_project=mp_project)

        # 数据源：数据集模式走 quick-import（建评测库 + Excel 导入 + 自动绑定
        # NL2SQL 数据源）；否则显式配置 mode=none（纯知识包问答）
        if dataset_mode:
            _write_state(
                root, phase="data_import",
                message=f"评测库导入（{len(plan['tables'])} 张表）",
            )
            body, ctype = _multipart_files([(eval_import.WORKBOOK_NAME, plan["workbook"])])
            try:
                _http(
                    "POST",
                    f"/api/projects/{mp_project}/evaluation-database/quick-import",
                    tok(), data=body, content_type=ctype, timeout=180,
                )
            except urllib.error.HTTPError as exc:
                if exc.code == 503:
                    raise RuntimeError(
                        "评测库未就绪：需在 modeling 部署启用 eval profile"
                        "（评测 MySQL + NL2SQL 服务）并配置 EVAL_DB_URL"
                    ) from exc
                raise
            _poll_data_import(project_id, root, tok, mp_project)
        elif mode == "connection":
            _configure_connection(root, tok, mp_project, data_source, project_id)
            if plan:
                _graph_import(project_id, root, tok, mp_project, data_source, plan)
        else:
            _write_state(root, phase="data_source", message="配置数据源（无数据库模式）")
            _http("PUT", f"/api/projects/{mp_project}/data-source", tok(),
                  body={"mode": "none"})

        _write_state(root, phase="cases", message=f"注入评测用例（{len(cases)} 条）")
        _http("POST", f"/api/projects/{mp_project}/testcases/batch", tok(),
              body={"cases": [{"question": c["question"], "groundTruth": c["groundTruth"]}
                              for c in cases]})

        _check_stop(project_id)  # 发起前最后一个取消点：停在此处不产生模型开销
        _write_state(
            root, phase="evaluate", message="评测执行中（consumer 逐用例问答）",
            cases=_case_progress(cases, [], True),
        )
        started = _http("POST", f"/api/projects/{mp_project}/evaluate", tok(), body={})
        round_id = started["roundId"]
        _write_state(root, round_id=round_id)
        _poll_round(project_id, root, token, mp_project, round_id, cases)
    except EvalStopped:
        # 停止请求已在 request_stop 里落状态；此处只兜底，不覆盖其话术
        if read_state(root).get("status") != "stopped":
            _write_state(root, status="stopped", message=STOPPED_MESSAGE)
    except Exception as exc:  # 编排失败落状态，不留悬挂的 running
        detail = str(getattr(exc, "detail", "") or "")
        text = f"{exc}：{detail}" if detail else str(exc)
        _write_state(root, status="failed", message=text[:300])
