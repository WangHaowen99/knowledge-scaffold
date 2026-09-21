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

STATE_REL = ".ontology-state/eval.json"
POLL_INTERVAL_SECONDS = 6
ROUND_TIMEOUT_SECONDS = 1800

_running: set[str] = set()  # project_id 去重（单进程内防并发重复触发）


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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


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
    )
    return rel


def _git_commit(root: Path, message: str) -> None:
    import subprocess

    for args in (["add", "-A"], ["commit", "-q", "-m", message]):
        subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=root, capture_output=True, text=True, timeout=30,
        )


# ---- 编排主体 ----


async def run(project_id: str, root: Path, token: str, cases: list[dict]) -> None:
    """后台评测编排；进度经 .ontology-state/eval.json 供前端轮询。"""
    if project_id in _running:
        return
    _running.add(project_id)
    try:
        await asyncio.to_thread(_run_sync, project_id, root, token, cases)
    finally:
        _running.discard(project_id)


def _run_sync(project_id: str, root: Path, token: str, cases: list[dict]) -> None:
    from .workspace import WorkspaceManager

    try:
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
            imported = _http("POST", "/api/package-workspaces/import", token,
                             data=body, content_type=ctype, timeout=120)
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise
            # 重名（此前评测轮留下的同名包）：追加时间戳重试
            body, ctype = _multipart_zip(
                "file", "package.zip", content,
                {"package_name": f"{package_name}-{int(time.time())}"},
            )
            imported = _http("POST", "/api/package-workspaces/import", token,
                             data=body, content_type=ctype, timeout=120)
        mp_project = imported["project"]["id"]
        _write_state(root, modeling_project=mp_project)

        # 数据源：modeling 要求显式配置后才能评测；v1 走 mode=none（纯知识包问答）。
        # 接通评测数据库（EVAL_DB_URL/MySQL）后，此处按 eval_data 对账结果切
        # connection + nl2sql_template 并追加 evaluation-database/import 步骤。
        _write_state(root, phase="data_source", message="配置数据源（无数据库模式）")
        _http("PUT", f"/api/projects/{mp_project}/data-source", token,
              body={"mode": "none"})

        _write_state(root, phase="cases", message=f"注入评测用例（{len(cases)} 条）")
        _http("POST", f"/api/projects/{mp_project}/testcases/batch", token,
              body={"cases": [{"question": c["question"], "groundTruth": c["groundTruth"]}
                              for c in cases]})

        _write_state(root, phase="evaluate", message="评测执行中（consumer 逐用例问答）")
        started = _http("POST", f"/api/projects/{mp_project}/evaluate", token, body={})
        round_id = started["roundId"]
        _write_state(root, round_id=round_id)

        deadline = time.time() + ROUND_TIMEOUT_SECONDS
        details: dict = {}
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            details = _http("GET", f"/api/projects/{mp_project}/rounds/{round_id}", token)
            round_info = details.get("round") or {}
            _write_state(
                root,
                message=f"评测执行中 {round_info.get('progressDone', 0)}/{round_info.get('progressTotal', 0)}",
            )
            if round_info.get("status") in ("done", "failed"):
                break
        round_info = (details.get("round") or {})
        if round_info.get("status") not in ("done", "failed"):
            raise TimeoutError("评测超时（modeling 侧轮次未在时限内结束）")
        if round_info.get("status") == "failed":
            # 轮次在 modeling 侧失败：透出其日志，不生成报告
            log = (round_info.get("log") or "").strip().splitlines()
            raise RuntimeError(f"modeling 评测轮失败：{log[-1] if log else '原因未知'}")

        rel = _write_report(root, round_info, details.get("results") or [])
        _git_commit(root, f"eval: 数据评测报告 {rel.split('/')[-1]}")
    except Exception as exc:  # 编排失败落状态，不留悬挂的 running
        _write_state(root, status="failed", message=str(exc)[:300])
