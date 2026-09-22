"""数据评测编排：onto-modeling 作为 modeling 评测 API 的编排方（方案 P2）。

链路（全部走 modeling 现成 API，见 docs/eval-optimization-integration.md §2.3）：
  发布知识包 zip → POST /api/package-workspaces/import（自动建 kind=package 项目
  并导入包版本）→ POST testcases/batch（trace 场景转换的用例）→ POST evaluate
  → 轮询 GET rounds/:roundId → 报告落 reports/eval-<时间戳>.md（git 版本化）。

- 鉴权：透传当前用户 token（共享账号中心/方案 A 下 modeling 直接接受）；
  无共享 auth 的部署用 ONTO_WEB_MODELING_TOKEN 兜底；
- 数据源：v1 走 mode=none（纯知识包问答）。评测数据库导入需要 modeling 侧
  配置 EVAL_DB_URL（MySQL），属部署级依赖，接通后在此追加 import 步骤；
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
from . import package_mapping

STATE_REL = ".ontology-state/eval.json"
POLL_INTERVAL_SECONDS = 6
ROUND_TIMEOUT_SECONDS = 1800
AUTH_GRACE_SECONDS = 600
DATA_IMPORT_TIMEOUT_SECONDS = 600  # 凭据过期后等待前端接力新 token 的宽限窗
# running 状态由编排线程每轮刷 ts；内存里已无编排且 ts 停滞超过此窗即判为中断
STALE_AFTER_SECONDS = POLL_INTERVAL_SECONDS * 5

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


# ---- 数据集模式：评测数据表收集与导入 ----


def collect_dataset_files(root: Path, data_source: dict) -> list[tuple[str, bytes]]:
    """数据集模式的导入文件：勾选的 eval_data 资料 + 可选 mock 种子。
    编排启动前调用（坏输入快速失败，不浪费一次包导入）。"""
    files: list[tuple[str, bytes]] = []
    for rel in (data_source.get("resources") or [])[:20]:
        rel = str(rel).strip()
        path = (root / rel).resolve()
        if not str(path).startswith(str(root.resolve())) or not path.is_file():
            raise ValueError(f"评测数据文件不存在：{rel}")
        if path.suffix.lower() not in (".xlsx", ".xlsm"):
            raise ValueError(f"评测数据仅支持 Excel 表：{rel}")
        files.append((path.name, path.read_bytes()))
    if data_source.get("include_mock"):
        files.append(_generate_mock_seed(root))
    if not files:
        raise ValueError("数据集模式需要至少一份数据表（评测数据资料或示例数据种子）")
    return files


def assert_data_binding(root: Path) -> None:
    """数据集模式前置门禁：对象必须绑定到真实数据表，否则这一轮问答根本不会查库。

    历史故障：来源Sheet 写成对象名、包里没有 mappings，评测照跑并给出一个看起来不错的
    分数，但全部用例都只读了知识包上下文、一次库都没查。在发起时拦下，让问题暴露在
    这里而不是藏进报告分数里。
    """
    binding = package_mapping.resolve_object_tables(root)
    if binding["problems"]:
        raise ValueError("对象与评测数据表未对齐：" + "；".join(binding["problems"][:3]))
    if not binding["bound"]:
        raise ValueError(
            "没有任何对象绑定到评测数据表，数据问答会全部落空："
            "请在对象文档「# 数据来源」声明真实表名，"
            "或确认「对象属性清单」的英文名称与数据表列名一致"
        )


def _generate_mock_seed(root: Path) -> tuple[str, bytes]:
    """ontology/data-mock 的示例数据 → 评测库导入 Excel（工具箱脚本，单一实现）。"""
    import subprocess
    import sys as _sys
    import tempfile

    script = (
        Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "mock_to_eval_seed.py"
    )
    out = Path(tempfile.mkdtemp(prefix="onto-eval-seed-")) / "mock-seed.xlsx"
    proc = subprocess.run(
        [_sys.executable, str(script), "--out", str(out)],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not out.is_file():
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise RuntimeError(f"示例数据种子生成失败：{detail or '无 mock 产物'}")
    return ("mock-seed.xlsx", out.read_bytes())


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
    """能否续接：modeling 侧有轮次坐标才谈得上收取已有结果。"""
    return bool(state.get("modeling_project") and state.get("round_id"))


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


def _write_report(root: Path, round_info: dict, results: list[dict]) -> str:
    counts = {"pass": 0, "partial": 0, "fail": 0, "error": 0}
    for item in results:
        counts[item.get("verdict", "error")] = counts.get(item.get("verdict", "error"), 0) + 1
    total = len(results) or 1
    score = round((counts["pass"] + counts["partial"] * 0.5) * 100 / total)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rel = f"reports/eval-{stamp}.md"
    lines = [
        f"# 数据评测报告 — {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"> 评测方式：知识包发布至 modeling 影子工作区，由 consumer 智能体逐用例问答，LLM Judge 判分",
        f"> 轮次：{round_info.get('id', '')}（modeling 侧可追溯）",
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
    _write_state(
        root,
        status="done",
        report=rel,
        summary={"total": len(results), **counts, "score": score},
        cases=_case_progress(
            [{"question": i.get("question", "")} for i in results], results, False
        ),
    )
    return rel


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
        # 轮次在 modeling 侧失败：透出其日志，不生成报告
        log = (round_info.get("log") or "").strip().splitlines()
        raise RuntimeError(f"modeling 评测轮失败：{log[-1] if log else '原因未知'}")
    rel = _write_report(root, round_info, details.get("results") or [])
    _git_commit(root, f"eval: 数据评测报告 {rel.split('/')[-1]}")


def _try_resume(project_id: str, root: Path, token: str, cases: list[dict]) -> bool:
    """凭据过期、服务重启等中断后，modeling 侧轮次往往仍在执行或已完成。重跑且
    用例集未变时续接收取结果，不重复导入、不重复烧模型。"""
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
        if not _try_resume(project_id, root, token, cases):
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


async def run(
    project_id: str, root: Path, token: str, cases: list[dict],
    data_source: dict | None = None,
) -> None:
    """后台评测编排；进度经 .ontology-state/eval.json 供前端轮询。"""
    if project_id in _running:
        return
    _running.add(project_id)
    _stop_requested.discard(project_id)  # 上一轮遗留的取消标记不影响新编排
    _live_tokens[project_id] = token
    try:
        await asyncio.to_thread(_run_sync, project_id, root, token, cases, data_source)
    finally:
        _running.discard(project_id)
        _live_tokens.pop(project_id, None)
        _stop_requested.discard(project_id)


def _run_sync(
    project_id: str, root: Path, token: str, cases: list[dict],
    data_source: dict | None = None,
) -> None:
    from .workspace import WorkspaceManager

    def tok() -> str:
        return _live_tokens.get(project_id) or token

    dataset_mode = str((data_source or {}).get("mode") or "none") == "dataset"
    try:
        # 数据集模式先收集文件：坏输入快速失败，不浪费一次包导入
        dataset_files = (
            collect_dataset_files(root, data_source or {}) if dataset_mode else []
        )
        if _try_resume(project_id, root, token, cases):
            return
        _check_stop(project_id)
        if dataset_mode:
            assert_data_binding(root)
        _write_state(root, status="running", phase="package", message="打包知识包")
        manager = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)
        import tempfile

        zip_path = Path(tempfile.mkdtemp(prefix="onto-eval-")) / "package.zip"
        manager.export_package_zip(project_id, zip_path)

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
                message=f"评测库导入（{len(dataset_files)} 份数据表）",
            )
            body, ctype = _multipart_files(dataset_files)
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
