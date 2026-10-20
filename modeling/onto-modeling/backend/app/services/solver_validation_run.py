"""求解契约端到端验证编排（需求 PR5）。

onto-modeling 不直连 Consumer、Connector 或 Solver，也不接收它们的地址与凭据：
  导出当前修订的知识包 → 导入 modeling 建评测项目并配置数据源 →
  POST /api/projects/:id/solver-contract-validations（modeling 把原始 ZIP 导入 Consumer
  评测工作空间、激活精确版本，逐用例向 Consumer 的 Agent 提问，与用户在 Consumer 对话求解
  走同一条链路，并从运行事件取求解工具的调用入参与公开结果）→
  按「调用了对应求解工具 + 入参与用例一致 + 权威结果 + 候选数量」判定，
  结论写进 solver_validation 记录供发布门禁使用。

用例的 question 可选：留空时按求解操作名与入参生成提问，要求 Agent 原样传入参；
填写时原样提问，用来检验 Agent 能否从自然语言里取出与 arguments 一致的入参。

验证用例放在 .ontology-state/（不进 git、不进导出包）：月份和批次属于每次请求的数据，
换数据不需要复制知识包。记录与证据只存状态、摘要和诊断引用，不存令牌或连接信息。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .. import config
from . import eval_data_sources, modeling_eval, package_mapping, solver_contract, solver_validation
from .workspace import WorkspaceError, WorkspaceManager

logger = logging.getLogger(__name__)

CASES_FILE = ".ontology-state/solver-validation-cases.json"
MAX_CASES = 20
MAX_CASE_NAME = 80
MAX_QUESTION = 4000
MAX_ARGUMENTS_BYTES = 64 * 1024
# modeling 用 multipart 文本字段接收用例，单字段默认上限 1 MB，这里留足余量
MAX_CASES_BYTES = 512 * 1024
MAX_EXPECTED_CANDIDATES = 100_000
CASE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# modeling 侧同步执行到所有用例终态（其截止时间 25 分钟），这里多留导入与回包的时间
REQUEST_TIMEOUT_SECONDS = 30 * 60
ROUTE_PROFILE = "ordered_binary_route.v1"
MAX_ROUTE_ITEMS = 64
MAX_MESSAGE = 300

CODE_CASES_INVALID = "solver_validation_cases_invalid"
CODE_CASES_INCOMPLETE = "solver_validation_cases_incomplete"
CODE_NOT_REQUIRED = "solver_validation_not_required"
CODE_BUSY = "solver_validation_busy"
CODE_UNAVAILABLE = "solver_validation_unavailable"
CODE_DATA_SOURCE_UNSUPPORTED = "solver_validation_data_source_unsupported"
CODE_REVISION_CONFLICT = "solver_contract_revision_conflict"
CODE_REVISION_CHANGED = "solver_contract_revision_changed"
CODE_NOT_AUTHORITATIVE = "solver_result_not_authoritative"
CODE_EVIDENCE_INCOMPLETE = "solver_evidence_incomplete"
CODE_CANDIDATE_MISMATCH = "solver_candidate_count_mismatch"
# Agent 链路问题（提问或 Agent 行为），与知识包本身的求解失败分开
CODE_TOOL_NOT_CALLED = "solver_tool_not_called"
CODE_ARGUMENTS_MISMATCH = "solver_arguments_mismatch"
CODE_MODELING_UNAVAILABLE = "modeling_unavailable"
CODE_INTERRUPTED = "solver_validation_interrupted"
CODE_FAILED = "solver_validation_failed"

INTERRUPTED_MESSAGE = "上一次验证被服务重启中断，请对当前修订重新验证"
# 只把 modeling/Consumer 公开摘要里的这些字段写进证据
_CASE_EVIDENCE_FIELDS = (
    "status", "authoritative", "code", "phase", "retryable", "detail", "missing",
    "solver_tool_called", "solver_tool", "arguments_match", "argument_diff",
    "feasibility", "optimality", "proof_kind", "run_id", "job_id",
    "result_artifact_ref", "verification_evidence_ref", "knowledge_revision_id",
    "fact_snapshot_digest", "problem_digest",
    "candidate_results_artifact_ref", "candidate_results_artifact_digest",
    "candidate_count", "candidate_results_error",
)
_REQUIRED_EVIDENCE = ("result_artifact_ref", "verification_evidence_ref")

_running: set[str] = set()  # 单进程内防同一项目并发验证
_tasks: set[asyncio.Task] = set()  # 持有后台任务引用，避免被回收
_lock = threading.Lock()


def safe_message(message: object) -> str:
    """modeling/Consumer 的报错可能带地址；落记录前统一脱敏截断。"""
    text = re.sub(r"[a-z][a-z0-9+.-]*://\S+", "[地址已隐藏]", str(message or ""), flags=re.I)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "[地址已隐藏]", text)
    return text[:MAX_MESSAGE]


# ---- 验证用例 ----


def _cases_path(root: Path) -> Path:
    return Path(root) / CASES_FILE


def list_cases(root: Path) -> list[dict]:
    try:
        data = json.loads(_cases_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cases = data.get("cases") if isinstance(data, dict) else None
    return [item for item in cases or [] if isinstance(item, dict)]


def _invalid(message: str) -> WorkspaceError:
    return WorkspaceError(message, 400, CODE_CASES_INVALID)


def normalize_cases(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise _invalid("验证用例必须是列表")
    if len(raw) > MAX_CASES:
        raise _invalid(f"验证用例最多 {MAX_CASES} 条")
    seen: set[str] = set()
    cases: list[dict] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise _invalid(f"第 {index} 条验证用例不是对象")
        case_id = str(item.get("id") or "").strip()
        if not CASE_ID_RE.match(case_id):
            raise _invalid(f"第 {index} 条验证用例的 id 只能由字母、数字、下划线和短横线组成")
        if case_id in seen:
            raise _invalid(f"验证用例 id 重复：{case_id}")
        seen.add(case_id)
        name = str(item.get("name") or "").strip() or case_id
        if len(name) > MAX_CASE_NAME:
            raise _invalid(f"验证用例 {case_id} 的名称超过 {MAX_CASE_NAME} 个字符")
        solver_key = str(item.get("solver_key") or "").strip()
        if not solver_contract.SOLVER_KEY_RE.match(solver_key):
            raise _invalid(f"验证用例 {case_id} 的 solver_key 不合法")
        arguments = item.get("arguments")
        if not isinstance(arguments, dict):
            raise _invalid(f"验证用例 {case_id} 的 arguments 必须是对象")
        if len(json.dumps(arguments, ensure_ascii=False).encode()) > MAX_ARGUMENTS_BYTES:
            raise _invalid(f"验证用例 {case_id} 的 arguments 超过 64 KB")
        case = {"id": case_id, "name": name, "solver_key": solver_key, "arguments": arguments}
        question = item.get("question")
        if question is not None and not isinstance(question, str):
            raise _invalid(f"验证用例 {case_id} 的 question 必须是文本")
        question = (question or "").strip()
        if len(question) > MAX_QUESTION:
            raise _invalid(f"验证用例 {case_id} 的提问超过 {MAX_QUESTION} 个字符")
        if question:
            case["question"] = question
        expected = item.get("expected_candidate_count")
        if expected is not None:
            if isinstance(expected, bool) or not isinstance(expected, int) or not (
                0 <= expected <= MAX_EXPECTED_CANDIDATES
            ):
                raise _invalid(f"验证用例 {case_id} 的 expected_candidate_count 必须是非负整数")
            case["expected_candidate_count"] = expected
        cases.append(case)
    if len(json.dumps(cases, ensure_ascii=False).encode()) > MAX_CASES_BYTES:
        raise _invalid("验证用例合计不能超过 512 KB")
    return cases


def save_cases(root: Path, raw: object) -> list[dict]:
    cases = normalize_cases(raw)
    path = _cases_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".solver-validation-cases-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"cases": cases}, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return cases


def expected_candidate_count(
    contract: solver_contract.WorkspaceSolverContract, case: dict
) -> int | None:
    """用例显式声明的数量优先；有序二元路线按「N 个项目 → 2N 个规范候选」推算。"""
    if "expected_candidate_count" in case:
        return case["expected_candidate_count"]
    try:
        entry = yaml.safe_load(contract.sidecars.get("entry") or b"")
    except yaml.YAMLError:
        return None
    if not isinstance(entry, dict) or entry.get("profile_id") != ROUTE_PROFILE:
        return None
    items = (case.get("arguments") or {}).get("items")
    if isinstance(items, list) and 1 <= len(items) <= MAX_ROUTE_ITEMS:
        return 2 * len(items)
    return None


def default_question(contract: solver_contract.WorkspaceSolverContract, case: dict) -> str:
    """未填写提问时的默认提问：点名求解操作并给出完整入参，只检验契约链路，不考 Agent 的取数能力。"""
    # 与入参大小上限同一口径序列化，不缩进，避免超出 modeling 的提问上限
    arguments = json.dumps(case.get("arguments") or {}, ensure_ascii=False, sort_keys=True)
    return (
        f"请调用求解能力「{contract.operation}」（operation_key：{contract.solver_key}）求解。"
        "入参如下，请原样使用，不要增删或改写字段，也不要再向我确认：\n"
        f"```json\n{arguments}\n```"
    )


# ---- 启动 ----


@dataclass
class ValidationPlan:
    project_id: str
    root: Path
    run_id: str
    revision: str
    token: str
    cases: list[dict]  # 启动时固化的用例副本，运行中改用例不影响本轮
    decision_models: dict[str, str]
    data_source: dict = field(default_factory=lambda: {"mode": "none"})
    allow_unbound: bool = False


def start(
    manager: WorkspaceManager,
    project_id: str,
    *,
    revision: str,
    case_ids: list[str],
    token: str,
    data_source_id: str = "",
    allow_unbound: bool = False,
    by: str = "",
) -> tuple[dict, ValidationPlan]:
    """校验并登记一次验证；返回 queued 记录与后台执行计划。不满足条件时抛 WorkspaceError。"""
    root = manager.project_root(project_id)
    check = manager.solver_check(project_id)
    if not check.needs_validation:
        raise WorkspaceError("当前知识包没有启用的求解契约，无需端到端验证", 400, CODE_NOT_REQUIRED)
    if check.findings:
        raise WorkspaceError(
            "求解契约静态检查未通过，请先修复后再验证", 400, solver_contract.CODE_INVALID
        )
    if (revision or "") != check.revision:
        raise WorkspaceError(
            "求解契约已被修改，请刷新后对当前修订启动验证", 409, CODE_REVISION_CONFLICT
        )

    contracts = {item.solver_key: item for item in check.contracts if item.enabled}
    saved = {item["id"]: item for item in normalize_cases(list_cases(root))}
    ids = list(dict.fromkeys(str(item) for item in case_ids)) or list(saved)
    if not ids:
        raise WorkspaceError("请先添加验证用例", 400, CODE_CASES_INCOMPLETE)
    missing = [item for item in ids if item not in saved]
    if missing:
        raise _invalid("验证用例不存在：" + "、".join(missing[:5]))
    selected = [dict(saved[item]) for item in ids]
    unknown = sorted({item["solver_key"] for item in selected} - set(contracts))
    if unknown:
        raise _invalid("验证用例引用的求解操作不存在或未启用：" + "、".join(unknown))
    uncovered = sorted(set(contracts) - {item["solver_key"] for item in selected})
    if uncovered:
        raise WorkspaceError(
            "以下求解操作还没有验证用例：" + "、".join(uncovered), 400, CODE_CASES_INCOMPLETE
        )
    for item in selected:
        contract = contracts[item["solver_key"]]
        item["expected_candidate_count"] = expected_candidate_count(contract, item)
        item["question_generated"] = not item.get("question")
        item["question"] = item.get("question") or default_question(contract, item)

    data_source: dict = {"mode": "none"}
    if data_source_id:
        found = eval_data_sources.get_source(project_id, data_source_id, root)
        if not found:
            raise WorkspaceError("数据源配置不存在或已删除，请重新选择", 404, "data_source_not_found")
        if modeling_eval.needs_import(found):
            raise WorkspaceError(
                "求解验证暂不支持需要导入数据的数据源，请选择已保存的外部连接或不使用数据源",
                400, CODE_DATA_SOURCE_UNSUPPORTED,
            )
        data_source = found
    if not allow_unbound:
        binding = package_mapping.resolve_object_tables(root)
        if binding["problems"]:
            raise WorkspaceError(
                "数据绑定未通过检查：" + "；".join(binding["problems"][:3]), 400, "binding_problems"
            )
    if not modeling_eval.enabled():
        raise WorkspaceError(
            "未配置 modeling 评测服务（ONTO_WEB_MODELING_URL），无法做端到端验证", 400, CODE_UNAVAILABLE
        )
    if not token:
        raise WorkspaceError("缺少可用于 modeling 的令牌", 400, CODE_UNAVAILABLE)
    if not modeling_eval.reachable():
        raise WorkspaceError("modeling 评测服务不可达，请先启动该服务", 503, CODE_UNAVAILABLE)

    with _lock:
        current = solver_validation.latest(root)
        active = current is not None and current.get("status") in solver_validation.ACTIVE_STATUSES
        if project_id in _running or active:
            raise WorkspaceError("该项目已有求解契约验证在运行，请等待完成", 409, CODE_BUSY)
        _running.add(project_id)
    try:
        record = solver_validation.create(root, check.revision, sorted(contracts), by)
    except Exception:
        _running.discard(project_id)
        raise
    if data_source_id:
        eval_data_sources.mark_used(project_id, data_source_id)
    plan = ValidationPlan(
        project_id=project_id,
        root=root,
        run_id=record["id"],
        revision=check.revision,
        token=token,
        cases=selected,
        decision_models={key: item.decision_model for key, item in contracts.items()},
        data_source=data_source,
        allow_unbound=allow_unbound,
    )
    return record, plan


def launch(plan: ValidationPlan) -> None:
    """在当前事件循环里后台执行；状态经 latest 接口轮询。"""
    task = asyncio.get_running_loop().create_task(_run(plan))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run(plan: ValidationPlan) -> None:
    try:
        await asyncio.to_thread(execute, plan)
    finally:
        _running.discard(plan.project_id)


def sweep_interrupted(data_dir: Path | None = None) -> list[str]:
    """进程启动时收敛孤儿记录：新进程里没有任何验证在跑，仍是 queued/running 的改判失败。"""
    base = Path(data_dir if data_dir is not None else config.DATA_DIR) / "workspaces"
    touched: list[str] = []
    if not base.is_dir():
        return touched
    for path in sorted(base.glob(f"*/{solver_validation.VALIDATIONS_FILE}")):
        root = path.parent.parent
        for record in solver_validation.list_records(root):
            if record.get("status") in solver_validation.ACTIVE_STATUSES:
                solver_validation.update(
                    root, record["id"], status=solver_validation.STATUS_FAILED,
                    error_code=CODE_INTERRUPTED, message=INTERRUPTED_MESSAGE,
                )
                touched.append(root.name)
    return touched


# ---- 执行 ----


def execute(plan: ValidationPlan, manager: WorkspaceManager | None = None) -> dict | None:
    """同步执行一次验证并落最终记录；任何失败都收敛为 failed，不留悬挂的 running。"""
    manager = manager or WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)
    solver_validation.update(
        plan.root, plan.run_id, status=solver_validation.STATUS_RUNNING,
        message="导出知识包并导入 modeling 评测项目",
        request_digest=_digest([_case_request(item) for item in plan.cases]),
    )
    evidence: dict = {
        "data_source": safe_message(
            plan.data_source.get("name") or eval_data_sources.default_name(plan.data_source)
        ),
        "decision_models": plan.decision_models,
    }
    try:
        content = _export(manager, plan)
        if manager.solver_check(plan.project_id).revision != plan.revision:
            return _fail(plan, CODE_REVISION_CHANGED, "验证期间求解契约已被修改，请对当前修订重新验证", evidence)
        modeling_project = _import(plan, content)
        evidence["modeling_project"] = modeling_project
        _configure_data_source(plan, modeling_project)
        solver_validation.update(
            plan.root, plan.run_id, message=f"Consumer 端到端验证中（{len(plan.cases)} 条用例）",
        )
        report = _validate(plan, modeling_project, content)
        return _finish(plan, report, evidence)
    except urllib.error.HTTPError as exc:
        code = str(getattr(exc, "error_code", "") or "").strip().lower() or f"http_{exc.code}"
        detail = str(getattr(exc, "detail", "") or "") or f"modeling 返回 HTTP {exc.code}"
        return _fail(plan, code, detail, evidence)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        logger.info("solver validation modeling request failed: %s", type(exc).__name__)
        return _fail(plan, CODE_MODELING_UNAVAILABLE, "modeling 服务不可用或请求超时，请稍后重试", evidence)
    except WorkspaceError as exc:
        return _fail(plan, exc.code, str(exc), evidence)
    except Exception as exc:  # 编排失败落记录，不留悬挂的 running
        logger.exception("solver validation failed")
        return _fail(plan, CODE_FAILED, str(exc) or type(exc).__name__, evidence)


def _export(manager: WorkspaceManager, plan: ValidationPlan) -> bytes:
    workdir = Path(tempfile.mkdtemp(prefix="onto-solver-validation-"))
    try:
        zip_path = workdir / "package.zip"
        manager.export_package_zip(
            plan.project_id, zip_path,
            binding=package_mapping.resolve_object_tables(
                plan.root, strict_declared=not plan.allow_unbound
            ),
            allow_unbound=plan.allow_unbound,
        )
        return zip_path.read_bytes()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _import(plan: ValidationPlan, content: bytes) -> str:
    package_name = f"onto-solver-{plan.project_id}"
    try:
        body, ctype = modeling_eval._multipart_zip(
            "file", "package.zip", content, {"package_name": package_name}
        )
        imported = modeling_eval._http(
            "POST", "/api/package-workspaces/import", plan.token,
            data=body, content_type=ctype, timeout=120,
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        # 重名（此前验证留下的同名包）：追加时间戳重试
        body, ctype = modeling_eval._multipart_zip(
            "file", "package.zip", content,
            {"package_name": f"{package_name}-{int(time.time())}"},
        )
        imported = modeling_eval._http(
            "POST", "/api/package-workspaces/import", plan.token,
            data=body, content_type=ctype, timeout=120,
        )
    return str(imported["project"]["id"])


def _configure_data_source(plan: ValidationPlan, modeling_project: str) -> None:
    source = plan.data_source
    payload = (
        eval_data_sources.modeling_payload(source, plan.project_id)
        if source.get("mode") == "connection" else {"mode": "none"}
    )
    modeling_eval._http(
        "PUT", f"/api/projects/{modeling_project}/data-source", plan.token,
        body=payload, timeout=120,
    )


def _case_request(item: dict) -> dict:
    return {
        "id": item["id"], "operation_key": item["solver_key"],
        "question": item["question"], "arguments": item["arguments"],
    }


def _validate(plan: ValidationPlan, modeling_project: str, content: bytes) -> dict:
    cases = [_case_request(item) for item in plan.cases]
    # 文本字段必须在文件之前：modeling 读到文件时只看得到已解析的字段
    body, ctype = modeling_eval._multipart_zip(
        "file", "package.zip", content,
        {"run_id": plan.run_id, "cases": json.dumps(cases, ensure_ascii=False)},
    )
    result = modeling_eval._http(
        "POST", f"/api/projects/{modeling_project}/solver-contract-validations", plan.token,
        data=body, content_type=ctype, timeout=REQUEST_TIMEOUT_SECONDS,
    )
    report = result.get("validation") if isinstance(result, dict) else None
    if not isinstance(report, dict):
        raise RuntimeError("modeling 返回的验证结果格式不正确")
    return report


def _assess(result: dict, expected: int | None) -> tuple[list[dict], str, str]:
    """单条用例的断言、首个失败码与说明；全部通过时失败码为空串。

    先判 Agent 链路（是否调用对应求解工具、入参是否与用例一致），再判求解结果：
    前两类失败说明的是提问或 Agent 行为，不是知识包求解契约的问题。
    """
    detail = str(result.get("detail") or "").strip()
    if result.get("solver_tool_called") is False:
        return (
            [{"name": "solver_tool_called", "passed": False}],
            CODE_TOOL_NOT_CALLED,
            "Agent 未调用该求解操作的求解工具（Agent 链路问题，非知识包求解失败）"
            + (f"：{detail}" if detail else ""),
        )
    assertions: list[dict] = []
    if result.get("solver_tool_called") is True:
        assertions.append({"name": "solver_tool_called", "passed": True})
    if result.get("arguments_match") is False:
        diff = [str(path) for path in result.get("argument_diff") or []][:20]
        assertions.append({"name": "arguments_match", "diff": diff, "passed": False})
        return (
            assertions,
            CODE_ARGUMENTS_MISMATCH,
            "Agent 传给求解工具的入参与用例不一致（Agent 链路问题，非知识包求解失败）"
            + (f"：{'、'.join(diff[:5])}" if diff else ""),
        )
    if result.get("arguments_match") is True:
        assertions.append({"name": "arguments_match", "passed": True})
    assessed, code, reason = _assess_solver_result(result, expected, detail)
    return assertions + assessed, code, reason


def _assess_solver_result(
    result: dict, expected: int | None, detail: str
) -> tuple[list[dict], str, str]:
    if result.get("status") != "completed":
        code = str(result.get("code") or "solver_result_rejected")
        phase = str(result.get("phase") or "")
        # Consumer 为调用方写的失败原因（如取不到哪张表），有就带上，免得只剩错误码
        return (
            [{"name": "completed", "passed": False}],
            code,
            f"未得到求解结果（{code}{'，阶段 ' + phase if phase else ''}）{'：' + detail if detail else ''}",
        )
    authoritative = result.get("authoritative") is True
    assertions = [{"name": "authoritative", "passed": authoritative}]
    if not authoritative:
        return assertions, CODE_NOT_AUTHORITATIVE, "结果不是 Consumer 的权威求解结果"
    # 权威结果必须带求解结果与 Verifier 证据的引用，缺任一项不算联调通过（REQ-BLD-005）
    missing = [key for key in _REQUIRED_EVIDENCE if not str(result.get(key) or "").strip()]
    assertions.append({"name": "evidence", "missing": missing, "passed": not missing})
    if missing:
        return assertions, CODE_EVIDENCE_INCOMPLETE, f"权威结果缺少证据引用 {'、'.join(missing)}"
    if expected is None:
        return assertions, "", ""
    actual = result.get("candidate_count")
    infeasible = result.get("feasibility") == "infeasible"
    passed = actual == expected if actual is not None else infeasible
    assertions.append({
        "name": "candidate_count", "expected": expected, "actual": actual, "passed": passed,
    })
    if passed:
        return assertions, "", ""
    if actual is None:
        code = str(result.get("candidate_results_error") or CODE_CANDIDATE_MISMATCH)
        return assertions, code, f"无法读取候选结果（{code}），预期 {expected} 个候选"
    return assertions, CODE_CANDIDATE_MISMATCH, f"候选数 {actual}，预期 {expected}"


def _finish(plan: ValidationPlan, report: dict, evidence: dict) -> dict | None:
    results = {
        str(item.get("id")): item
        for item in report.get("cases") or [] if isinstance(item, dict)
    }
    cases: list[dict] = []
    failures: list[tuple[str, str]] = []
    total = 0
    counted = False
    for item in plan.cases:
        result = results.get(item["id"]) or {
            "status": "error", "code": "solver_result_missing", "authoritative": False,
        }
        if "detail" in result:
            result = {**result, "detail": safe_message(result["detail"])}
        expected = item.get("expected_candidate_count")
        assertions, code, reason = _assess(result, expected)
        if code:
            failures.append((code, f"用例 {item['name']}：{reason}"))
        count = result.get("candidate_count")
        if isinstance(count, int) and not isinstance(count, bool):
            total += count
            counted = True
        cases.append({
            "id": item["id"],
            "name": item["name"],
            "solver_key": item["solver_key"],
            # 自动生成的提问内嵌整份入参，可由用例重新生成，不重复落证据
            **({} if item.get("question_generated") else {"question": item["question"]}),
            "question_generated": bool(item.get("question_generated")),
            **{key: result[key] for key in _CASE_EVIDENCE_FIELDS if key in result},
            "expected_candidate_count": expected,
            "assertions": assertions,
            "passed": not code,
        })
    package = report.get("package") if isinstance(report.get("package"), dict) else {}
    evidence.update({
        "package": {"id": str(package.get("id") or ""), "version": str(package.get("version") or "")},
        "cases": cases,
    })
    authoritative = bool(cases) and all(item.get("authoritative") is True for item in cases)
    passed = not failures
    message = (
        f"{len(cases)} 条用例全部通过" if passed
        else f"{len(cases) - len(failures)}/{len(cases)} 条用例通过；{failures[0][1]}"
    )
    return solver_validation.update(
        plan.root, plan.run_id,
        status=solver_validation.STATUS_PASSED if passed else solver_validation.STATUS_FAILED,
        authoritative=authoritative,
        candidate_count=total if counted else None,
        error_code="" if passed else failures[0][0],
        message=safe_message(message),
        result_digest=_digest(cases),
        evidence=evidence,
    )


def _fail(plan: ValidationPlan, code: str, message: str, evidence: dict) -> dict | None:
    return solver_validation.update(
        plan.root, plan.run_id,
        status=solver_validation.STATUS_FAILED,
        authoritative=False,
        error_code=code,
        message=safe_message(message),
        evidence=evidence,
    )


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
