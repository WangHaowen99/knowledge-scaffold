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
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .. import config
from . import eval_data_sources, eval_import, package_mapping

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

# 一轮编排从打包到收报告都占着一个线程（含每 6 秒一次的轮询等待，最长 30 分钟），
# 放进评测专用线程池：不占事件循环的默认线程池——后者还承担上传、git、项目导入删除
# 等短任务，被评测占满时这些操作要排队几十秒到几分钟。专用池容量即同时执行的评测轮数，
# 由同容量的信号量把关：排队发生在事件循环里，排队中的轮次可以直接取消出队
_executor: ThreadPoolExecutor | None = None
_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # 事件循环 → 槽位信号量
_queue_lock = threading.Lock()
# 等待评测槽位的 project_id → (入队时间, 所在事件循环, 编排任务)，按入队顺序
_queued: dict[str, tuple[int, asyncio.AbstractEventLoop, asyncio.Task]] = {}

STOPPED_MESSAGE = "评测已停止（modeling 侧轮次已取消，已完成用例的结果仍保留在 modeling 侧）"
INTERRUPTED_MESSAGE = (
    "上一轮评测被服务重启中断：modeling 侧轮次可能仍在执行或已完成，"
    "可「续接上一轮」收取结果"
)


class EvalStopped(Exception):
    """用户主动停止评测——与编排失败区分开：状态落 stopped，且允许重跑。"""


class EvaluationPackageInvalid(ValueError):
    error_code = "EVALUATION_PACKAGE_INVALID"

    def __init__(self, problems: list[str]):
        self.error_details = {"problems": problems}
        super().__init__("对象关系未通过评测前校验：" + "；".join(problems[:3]))


def evaluation_object_snapshot(root: Path) -> dict[str, str]:
    """评测检查与导出共用同一份对象内容，不受数据源模式或降级绑定选项影响。"""
    try:
        texts = package_mapping.read_object_texts(root, strict=True)
    except ValueError as exc:
        raise EvaluationPackageInvalid([str(exc)]) from exc
    _, problems = package_mapping.package_relations(
        texts, reject_duplicate_relations=True,
    )
    if problems:
        raise EvaluationPackageInvalid(problems)
    return texts


def object_snapshot_fingerprint(texts: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(texts, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


QUEUED_STOP_MESSAGE = "评测已停止（排队中，尚未执行）"


def _eval_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=config.EVAL_MAX_CONCURRENT_ROUNDS, thread_name_prefix="onto-eval"
        )
    return _executor


def _eval_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _slots.get(loop)
    if slots is None:
        slots = _slots[loop] = asyncio.Semaphore(config.EVAL_MAX_CONCURRENT_ROUNDS)
    return slots


def queue_position(project_id: str) -> int | None:
    """排队中返回 1 起的位次，未排队返回 None。"""
    with _queue_lock:
        for position, queued_id in enumerate(_queued, start=1):
            if queued_id == project_id:
                return position
    return None


def _queued_since(project_id: str) -> int | None:
    with _queue_lock:
        entry = _queued.get(project_id)
    return entry[0] if entry else None


def _cancel_queued(project_id: str) -> bool:
    """排队中的轮次直接出队并取消等待；已开始执行的返回 False（走协作式取消）。

    可从路由的工作线程调用：取消经 call_soon_threadsafe 投递回编排所在的事件循环。
    """
    with _queue_lock:
        entry = _queued.pop(project_id, None)
    if entry is None:
        return False
    _since, loop, task = entry
    loop.call_soon_threadsafe(task.cancel)
    return True


async def _run_in_eval_pool(project_id: str, func: Callable[..., None], *args: Any) -> None:
    """在评测专用线程池里执行一轮编排；槽位满时排队，排队中可被停止。"""
    loop = asyncio.get_running_loop()
    with _queue_lock:
        _queued[project_id] = (int(time.time()), loop, asyncio.current_task())
    try:
        async with _eval_slots():
            with _queue_lock:
                # 出队与停止方的出队互斥：先拿到的一方说了算
                still_queued = _queued.pop(project_id, None) is not None
            if not still_queued:
                return  # 拿到槽位前已被停止：request_stop 已落 stopped 状态
            await loop.run_in_executor(_eval_executor(), func, *args)
    finally:
        with _queue_lock:
            _queued.pop(project_id, None)


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


def model_profiles(token: str) -> dict:
    """Return the enabled global Consumer model pool through Modeling."""
    return _http("GET", "/api/evaluation/model-profiles", token, timeout=15)


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
            exc.error_code = str(payload.get("code") or "").strip()
            nested_details = payload.get("details")
            if isinstance(nested_details, dict):
                exc.error_details = nested_details
            else:
                # modeling 的 HTTP 错误响应为保持兼容，会把安全 details
                # 展平到响应顶层（例如 conflicts/truncated），不能只读取
                # payload.details，否则错误码会透传但冲突字段会丢失。
                exc.error_details = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"code", "error", "message", "reason", "display", "next"}
                }
        except Exception:
            exc.detail = ""
            exc.error_code = ""
            exc.error_details = {}
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


def _graph_plan_mismatches(validation: dict, plan: dict) -> list[str]:
    """导入预检（modeling 的写入计划）↔ 预览给出的实例数与关系条数逐项对账。

    预览按 modeling 的建点/配对规则在本地算出这两个数（eval_import.graph_plan）；这里与
    modeling 真正的写入计划比一次，对不上就在写库之前拦下——复刻的规则万一与 modeling
    漂移，评测会停下报数，而不是留一个与图里内容不符的数字。
    """
    counts = validation.get("counts") or {}
    out = []
    for field, key, unit in (
        ("nodeCount", "expected_instances", "个实例"),
        ("relationshipCount", "expected_edges", "条关系"),
    ):
        expected, planned = plan.get(key), counts.get(field)
        if expected is not None and planned is not None and planned != expected:
            out.append(f"预览给出 {expected} {unit}，导入预检会写入 {planned} {unit}")
    return out


def _graph_import(
    project_id: str, root: Path, tok, mp_project: str, data_source: dict, plan: dict
) -> None:
    """Neo4j 一键导入：上传工作簿 → 推断关系关联 → 校验 → 执行（清空本隔离范围旧数据）→ 轮询。"""
    rows = sum(len(t.rows) for t in plan["tables"])
    _write_state(
        root, phase="graph_import",
        message=f"图数据导入：上传 {len(plan['tables'])} 张表（{rows} 行）",
    )
    entities = len(package_mapping.object_data_mappings(
        plan.get("binding") or {"objects": {}}, graph=True,
    )["objects"])
    body, ctype = _multipart_files([(eval_import.WORKBOOK_NAME, plan["workbook"])])
    try:
        upload = _http(
            "POST", f"/api/projects/{mp_project}/graph-import/uploads", tok(),
            data=body, content_type=ctype, timeout=180,
        )["upload"]
        relations, skipped = eval_import.graph_relation_mapping(upload)
        exported_count = (plan.get("relation_report") or {}).get("relations_exported")
        mismatched = exported_count is not None and exported_count != len(
            (upload.get("schema") or {}).get("relations") or []
        )
        # 关系转换不出来只跳过那一条；导出与读取对不上是转换本身的缺陷，才停下
        skipped = (plan.get("relation_report") or {}).get("notes", []) + skipped
        report = {
            **(plan.get("relation_report") or {}),
            "nodes": 0, "relationships": 0,
            "entities": entities,
            "expected_instances": plan.get("expected_instances"),
            "expected_edges": plan.get("expected_edges"),
            "relations_mapped": len(relations),
            "relations_skipped": len(skipped), "notes": skipped,
        }
        _write_state(root, graph_import=report)
        if mismatched:
            raise RuntimeError("知识包导出与图导入读取的关系定义数不一致，请检查知识包转换")
        mapping = {"relations": relations}
        validation = _http(
            "POST", f"/api/projects/{mp_project}/graph-import/uploads/{upload['id']}/validate",
            tok(), body={"mapping": mapping},
        )["validation"]
        mismatches = _graph_plan_mismatches(validation, plan)
        if mismatches:
            _write_state(root, graph_import={**report, "notes": skipped + mismatches})
            raise RuntimeError("导入预检与预览不一致，已在写入图库前停下：" + "；".join(mismatches))
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
    counts = validation.get("counts") or {}
    notes += [
        f"实体「{item.get('objectKey')}」{item.get('message') or item.get('code')}"
        for item in validation.get("objectDiagnostics") or []
    ]
    if not relations:
        notes.append("知识包未声明可执行关系，本次仅导入节点")
    if counts.get("skippedBlankCount"):
        notes.append(f"{counts['skippedBlankCount']} 行关联字段为空，未建立关系")
    if counts.get("skippedUnmatchedCount"):
        notes.append(f"{counts['skippedUnmatchedCount']} 行关联值未匹配到目标实例，未建立关系")
    _write_state(
        root,
        graph_import={
            **report,
            "nodes": finished.get("nodeCount", 0),
            "relationships": finished.get("relationshipCount", 0),
            "relations_mapped": len(relations),
            "skipped_blank_rows": counts.get("skippedBlankCount", 0),
            "skipped_unmatched_rows": counts.get("skippedUnmatchedCount", 0),
            "notes": notes,
        },
    )
    # 预检计划即实际写入量（modeling 侧同样断言），不一致说明写入过程丢了东西
    for field, label in (("nodeCount", "节点"), ("relationshipCount", "关系")):
        if counts.get(field) is not None and counts[field] != finished.get(field, 0):
            raise RuntimeError(f"图{label}实际写入数量与导入预检不一致，请检查图导入结果")


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


_SCENE_INPUT_RE = re.compile(r"^\s*[-*]\s*`([^`]+)`\s*=\s*(.+?)\s*$")
_SCENE_EXPECTED_RE = re.compile(r"^\s*[-*]\s*\*\*(预期[^*]*)\*\*\s*[：:]\s*(.+?)\s*$")


def _scene_case(body: str) -> tuple[list[str], list[str]]:
    """/ontology-trace 的场景段（testcase-template.md）→ (输入条件, 预期结果)。

    输入是 `参数` = `值` 列表，预期取「预期路径 / 预期结果」等以「预期」开头的加粗项。
    """
    inputs, expected = [], []
    for line in body.splitlines():
        expected_match = _SCENE_EXPECTED_RE.match(line)
        if expected_match:
            expected.append(f"{expected_match.group(1)}：{expected_match.group(2)}")
            continue
        input_match = _SCENE_INPUT_RE.match(line)
        if input_match:
            inputs.append(f"{input_match.group(1)}：{input_match.group(2).strip('`')}")
    return inputs, expected


def trace_cases(root: Path) -> list[dict]:
    """ontology/testcase/*.md 的 TC 段落或场景段 → (question, groundTruth) 用例。

    TC 段落是 K-V 表：`输入-*` 行拼为问题条件，`预期-*` 行拼为标准答案；场景段是
    /ontology-trace 按 testcase-template.md 写出的格式，见 _scene_case。
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
        for match in re.finditer(r"##\s*(场景\s*\d+[：:][^\n]*)\n(.*?)(?=\n##\s|\Z)", text, re.S):
            inputs, expected = _scene_case(match.group(2))
            if inputs and expected:
                cases.append(_trace_case(subject, match.group(1).strip(), inputs, expected, doc.name))
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
            cases.append(_trace_case(subject, title, inputs, expected, doc.name))
    return cases


def _trace_case(subject: str, title: str, inputs: list[str], expected: list[str], name: str) -> dict:
    return {
        "question": f"关于「{subject}」（{title}）：{'；'.join(inputs)}。按业务规则应得到什么结果？",
        "groundTruth": "；".join(expected),
        "source": f"ontology/testcase/{name}",
    }


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
    # 工作区已不在（项目被删除）时不落状态：编排线程可能晚于删除才收尾，
    # 照常建目录会在原位置重建出只有状态文件的残壳
    if not root.is_dir():
        return
    path = _state_path(root)
    path.parent.mkdir(exist_ok=True)
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
    position = queue_position(project_id)
    if position is not None:
        # 排队态只叠加在视图上、不落盘：落盘的 running 会被「同题续接」误判为上一轮
        queued_at = _queued_since(project_id) or int(time.time())
        return {
            "status": "running",
            "phase": "queued",
            "message": (
                f"排队等待评测执行（第 {position} 位，"
                f"同时执行上限 {config.EVAL_MAX_CONCURRENT_ROUNDS} 轮）"
            ),
            "queue_position": position,
            "started_at": queued_at,
            "ts": int(time.time()),
        }
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
    """未取得可核验结果的用例补记为异常（不推断是否已执行）。

    报告与得分覆盖整个用例集，/ontology-evolution 按 E 类（运行异常）给出指引，
    而不是只看跑完的那几条。
    """
    executed = [str(r.get("question", "")).strip() for r in results]
    missing = []
    for case in cases or []:
        question = str(case.get("question", "")).strip()
        # 续接场景的 cases 取自状态里截断过的问题，前缀匹配同时覆盖完全相等
        if not question:
            continue
        exact = [i for i, q in enumerate(executed) if q == question]
        # Only old, explicitly truncated progress entries may use a unique prefix.
        candidates = exact or ([i for i, q in enumerate(executed) if q.startswith(question)]
                               if case.get('question_truncated') else [])
        if len(candidates) == 1:
            executed.pop(candidates[0])
            continue
        missing.append({
            "question": question,
            "groundTruth": case.get("groundTruth", ""),
            "finalAnswer": "",
            "verdict": "error",
            "reason": f"本用例未取得可核验结果：{failure}",
        })
    return list(results) + missing


# 报告「工具失败」摘要：/ontology-evolution 据此判断运行异常的真实根因（脚本参数/
# 数据映射/环境），而不是只看「运行失败」一句话去猜。逐用例限量，防报告膨胀。
TOOL_FAILURES_PER_CASE = 5
TOOL_FAILURE_DETAIL_CHARS = 240
TOOL_FAILURE_CASE_CHARS = 1500
_JSON_CODE_RE = re.compile(r'"(?:code|error_code)"\s*:\s*"([A-Z][A-Z0-9_]{2,79})"')
# 自由文本里只认错误码形态的大写标识，免得把表名/列名（如 CUSTOMER_CODE）误当错误码
_ERROR_CODE_RE = re.compile(
    r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:FAILED|ERROR|DENIED|TIMEOUT|STALLED|INVALID"
    r"|NOT_FOUND|UNAVAILABLE|EXCEEDED|REJECTED|MISSING)\b"
)
_ISSUE_RE = re.compile(r'"code"\s*:\s*"([a-z][a-z0-9_]*)"\s*,\s*"path"\s*:\s*"([^"]{1,240})"')
_JSON_MESSAGE_RE = re.compile(r'"message"\s*:\s*"([^"]{1,500})"')
_EXIT_CODE_RE = re.compile(r"exited with code (-?\d+)", re.IGNORECASE)
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer ***"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "***"),
    (re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
        r"(\"?\s*[=:]\s*\"?)[^\s\"',;}]+"
    ), r"\1\2***"),
)


def _redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _issue_labels(error: dict) -> list[str]:
    """结构化错误里的「问题码 字段路径」列表；issues 不是数组时为空。"""
    issues = error.get("issues")
    if not isinstance(issues, list):
        return []
    labels = []
    for issue in issues:
        if isinstance(issue, dict) and issue.get("code"):
            labels.append(f"{issue.get('code')} {issue.get('path') or ''}".strip())
    return labels


def _parsed_message(parsed: dict, error: dict) -> str:
    """解析成功的错误 JSON → 详情：message、顶层字符串 error、stderr 依次拼接。"""
    top_error = parsed.get("error")
    values = (
        error.get("message"),
        top_error if isinstance(top_error, str) else "",
        error.get("stderr") or parsed.get("stderr"),
    )
    return " ".join(str(value) for value in values if value)


def _text_error_code(raw: str) -> str:
    """从原文抽错误码：先认 JSON 的 code 字段（截断的 JSON 也行），再认错误码形态的大写标识。"""
    match = _JSON_CODE_RE.search(raw)
    if match:
        return match.group(1)
    match = _ERROR_CODE_RE.search(raw)
    return match.group(0) if match else ""


def _tool_failure_line(failure: dict) -> str:
    """一条工具失败 → 工具名 · 错误码 · 问题码与字段路径 · 退出码 · 截断详情。

    consumer 的 error 是截断到 1KB 的文本：参数校验失败是 JSON（可能被截断），
    脚本非零退出是「Error: Script exited with code N. …」——两种都按需抽取。
    """
    tool = str(failure.get("toolName") or failure.get("tool_name") or "未知工具")
    tool = tool.replace("`", "").strip()[:64] or "未知工具"
    raw = str(failure.get("error") or "").strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    parsed = parsed if isinstance(parsed, dict) else {}
    error = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
    code = str(error.get("code") or error.get("error_code") or "").strip()
    issues = _issue_labels(error)
    if parsed:
        message = _parsed_message(parsed, error)
    else:
        # 截断的 JSON 解析不了：有 message 字段就只取它，免得详情是半截 JSON
        match = _JSON_MESSAGE_RE.search(raw)
        message = match.group(1) if match else raw
    if not code:
        code = _text_error_code(raw)
    if not issues:
        issues = [f"{issue} {path}" for issue, path in _ISSUE_RE.findall(raw)]
    parts = [f"`{tool}`"]
    if code:
        parts.append(f"`{code[:80]}`")
    if issues:
        parts.append("问题：" + "、".join(
            f"`{_redact(issue).replace('`', '')}`" for issue in list(dict.fromkeys(issues))[:6]
        ))
    exit_code = _EXIT_CODE_RE.search(raw)
    if exit_code:
        parts.append(f"退出码 {exit_code.group(1)}")
    detail = " ".join(_redact(message).split())
    if detail:
        if len(detail) > TOOL_FAILURE_DETAIL_CHARS:
            detail = detail[:TOOL_FAILURE_DETAIL_CHARS] + "…"
        parts.append(f"详情：{detail}")
    return " · ".join(parts)


def _tool_failure_lines(trace: Any) -> list[str]:
    """单个用例的「工具失败」小节（Markdown 嵌套列表行）；无失败、无轨迹问题时为空。"""
    if not isinstance(trace, dict):
        return []
    failures = [item for item in trace.get("toolFailures") or [] if isinstance(item, dict)]
    lines: list[str] = []
    if failures:
        counted: dict[str, int] = {}
        for failure in failures:
            line = _tool_failure_line(failure)
            counted[line] = counted.get(line, 0) + 1
        for line, times in list(counted.items())[:TOOL_FAILURES_PER_CASE]:
            lines.append(f"  - {line}" + (f"（×{times}）" if times > 1 else ""))
        if len(counted) > TOOL_FAILURES_PER_CASE:
            lines.append(f"  - …另有 {len(counted) - TOOL_FAILURES_PER_CASE} 类失败未列出")
        calls = trace.get("toolCalls")
        if isinstance(calls, list) and calls:
            lines.append(f"  - 本用例共调用工具 {len(calls)} 次，失败 {len(failures)} 次")
    if trace.get("status") == "unavailable":
        reason = " ".join(_redact(str(trace.get("error") or "原因未知")).split())[:160]
        lines.append(f"  - 执行轨迹未取到（{reason}），工具失败情况未知")
    kept, used = [], 0
    for line in lines:
        if used + len(line) > TOOL_FAILURE_CASE_CHARS:
            kept.append("  - …（已截断）")
            break
        kept.append(line)
        used += len(line)
    return kept


def _write_report(
    root: Path,
    round_info: dict,
    results: list[dict],
    cases: list[dict] | None = None,
    failure: str = "",
    traces: dict | None = None,
) -> str:
    """落评测报告并把状态收敛到终态。

    failure 非空 = modeling 侧轮次异常终止但已有部分结果：照样出报告（状态 failed），
    让失分与异常用例能进入 /ontology-evolution，而不是随失败状态一起丢掉。
    traces = modeling 轮次详情里按结果 ID 索引的执行轨迹：失分用例附「工具失败」摘要。
    """
    executed = len(results)
    # A transport-level done is not proof that every question produced a usable answer.
    results = _unexecuted_as_errors(results, cases, failure or '服务未返回用例结果')
    normalized = []
    for original in results:
        item = dict(original)
        verdict = item.get('verdict')
        if verdict not in {'pass', 'partial', 'fail', 'error'} or (
            verdict != 'error' and not str(item.get('finalAnswer') or '').strip()
        ):
            item.update(verdict='error', reason=(
                f"无有效回答或有效判分（原判定：{verdict}）；" + str(item.get('reason') or '')
            ))
        normalized.append(item)
    results = normalized
    counts = {"pass": 0, "partial": 0, "fail": 0, "error": 0}
    for item in results:
        counts[item.get("verdict", "error")] = counts.get(item.get("verdict", "error"), 0) + 1
    valid = counts['pass'] + counts['partial'] + counts['fail']
    score = round((counts['pass'] + counts['partial'] * 0.5) * 100 / valid) if valid else None
    technical_failure = bool(failure or counts['error'] or not results)
    result_status = 'unavailable' if not valid else ('partial' if technical_failure else 'complete')
    outcome = {'unavailable': '未形成有效评测', 'partial': '部分有效，存在技术异常',
               'complete': '评测完成'}[result_status]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rel = f"reports/eval-{stamp}.md"
    answers_rel = f"reports/eval-{stamp}.answers.json"
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
        f"- 结果状态：{outcome}",
        f"- 有效判分：{valid}/{len(results)}；技术异常不计为业务答错，不进入得分分母。",
        (f"- {'有效用例得分' if technical_failure else '综合得分'}：**{score}**（仅 {valid} 条有效用例，pass=1、partial=0.5 加权）"
         if score is not None else '- 得分：未形成有效评测（不是 0 分）'),
        "",
        "## 逐用例明细",
        "",
    ]
    answer_records = []
    for index, item in enumerate(results, 1):
        mark = {"pass": "✅", "partial": "⚠️", "fail": "❌"}.get(item.get("verdict"), "🛑")
        # 报告附件保存原始 finalAnswer；展示摘要另行裁剪，不能用 strip/摘要覆盖原件。
        answer = str(item.get("finalAnswer") or "")
        result_id = str(item.get("id") or item.get("result_id") or f"case-{index}").strip()
        answer_records.append({
            "resultId": result_id,
            "question": item.get("question", ""),
            "finalAnswer": answer,
            "originalLength": len(answer),
            "sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            "verdict": item.get("verdict", "error"),
            "reason": item.get("reason", ""),
        })
        display = answer[:600]
        # 将回答逐行包进引用块：回答内部的标题、列表、表格仍按 Markdown 渲染，
        # 但不会改变报告外层的章节层级或把固定字段挤成同级小标题。
        def nested_quote(text: str) -> str:
            return "\n".join(
                f"  > {line}" if line else "  >"
                for line in (str(text).splitlines() or ["（无）"])
            )

        quoted_display = nested_quote(display)
        answer_meta_lines = [
            "  >",
            f"  > - **原始回答长度**：{len(answer)} 字符；是否截断：{'是' if len(answer) > len(display) else '否'}",
        ]
        if len(answer) > len(display):
            answer_meta_lines.append(
                f"  > - **完整回答**：[按结果 ID 查看附件]({answers_rel}#{result_id})"
            )
        answer_meta = "\n".join(answer_meta_lines)
        lines += [
            f"### {index}. {mark} {item.get('verdict', 'error')}",
            "",
            f"- **问题**：{item.get('question', '')}",
            "",
            "- **标准答案**：",
            "",
            nested_quote(item.get("groundTruth") or "（无）"),
            "",
            "- **智能体回答**：",
            "",
            quoted_display,
            answer_meta,
            "",
            "- **判分理由**：",
            "",
            nested_quote(item.get("reason") or "（无）"),
            "",
        ]
        failure_lines = (
            [] if item.get("verdict") == "pass"
            else _tool_failure_lines((traces or {}).get(str(item.get("id") or "").strip()))
        )
        if failure_lines:
            lines += ["- **工具失败**：", "", *failure_lines, ""]
    (root / "reports").mkdir(exist_ok=True)  # 不带 parents：项目已删除时不重建工作区
    (root / rel).write_text("\n".join(lines), encoding="utf-8")
    (root / answers_rel).write_text(
        json.dumps({"roundId": round_info.get("id", ""), "answers": answer_records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    terminal = {'status': 'failed' if technical_failure else 'done', 'message': (
        f"{failure or outcome}（有效 {valid}/{len(results)} 条，异常 {counts['error']} 条；报告已生成）"[:300]
    )}
    _write_state(
        root,
        **terminal,
        report=rel,
        # 报告出自哪一轮：新一轮发起时清空，用来判断报告是否属于最近一轮
        report_round=round_info.get("id") or state.get("round_id") or rel,
        summary={"total": len(results), **counts, "score": score, "valid": valid,
                 "result_status": result_status},
        cases=_case_progress(
            [{"question": i.get("question", "")} for i in results], results, False
        ),
    )
    return rel


def evolution_hint(state: dict) -> dict | None:
    """最近一轮评测是否该进入 /ontology-evolution（聊天区建议与左栏按钮共用）。

    得分低于阈值、或出现异常（error）用例即提示。异常不参与业务得分；
    轮次异常终止但有部分结果时同样提示（报告已落盘）。
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


def full_answer(project_id: str, root: Path, token: str, result_id: str) -> dict:
    """按结果 ID 从 Modeling Core 读取完整 finalAnswer；不使用 Onto 摘要。"""
    state = read_state(root)
    modeling_project = str(state.get("modeling_project") or "").strip()
    round_id = str(state.get("round_id") or "").strip()
    wanted = str(result_id or "").strip()
    if not modeling_project or not round_id or not wanted:
        raise ValueError("完整回答证据不可用")
    details = _http(
        "GET",
        f"/api/projects/{modeling_project}/rounds/{round_id}",
        token,
        timeout=30,
    )
    result = next(
        (item for item in details.get("results") or []
         if str(item.get("id") or item.get("result_id") or "").strip() == wanted),
        None,
    )
    if result is None:
        raise ValueError("完整回答证据不存在或不属于当前评测轮次")
    answer = str(result.get("finalAnswer") or "")
    return {
        "resultId": wanted,
        "question": result.get("question", ""),
        "groundTruth": result.get("groundTruth", ""),
        "fullAnswer": answer,
        "originalLength": len(answer),
        "sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "verdict": result.get("verdict"),
        "reason": result.get("reason", ""),
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
            answer = str(item.get("finalAnswer") or "").strip()
            display = answer[:200]
            out.append({
                "question": question[:120],
                "verdict": item.get("verdict") or "error",
                "answer": display,
                "result_id": str(item.get("id") or item.get("result_id") or "").strip(),
                "is_truncated": len(answer) > len(display),
                "original_length": len(answer),
                "display_length": len(display),
                "full_answer_available": bool(item.get("id") or item.get("result_id")),
                "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest() if answer else "",
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

    def check_stop() -> None:
        # 停止请求可能早于轮次坐标落盘（项目删除后更是再也写不进状态），停止方据状态
        # 取消不到这一轮：由手握坐标的编排自己取消。停止方已取消过的，modeling 回 409，无害
        if project_id in _stop_requested:
            cancel_round(mp_project, round_id, tok())
        _check_stop(project_id)

    deadline = time.time() + ROUND_TIMEOUT_SECONDS
    details: dict = {}
    auth_wait_until: float | None = None
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        check_stop()
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
        check_stop()  # 取消请求已落 stopped 状态，不再覆写进度
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
        _collect_failed_round(
            root, round_info, details.get("results") or [], cases, details.get("traces")
        )
        return
    rel = _write_report(
        root, round_info, details.get("results") or [], cases,
        traces=details.get("traces"),
    )
    _git_commit(root, f"eval: 数据评测报告 {rel.split('/')[-1]}")


def _round_failure(round_info: dict) -> str:
    log = (round_info.get("log") or "").strip().splitlines()
    return f"modeling 评测轮失败：{log[-1] if log else '原因未知'}"


def _collect_failed_round(
    root: Path, round_info: dict, results: list[dict], cases: list[dict],
    traces: dict | None = None,
) -> None:
    """轮次在 modeling 侧失败：透出其日志，有结果就落报告。

    已有用例结果（常见于连续环境异常熔断）时照样落报告，没有任何结果才按编排失败处理。
    """
    failure = _round_failure(round_info)
    if not results:
        raise RuntimeError(failure)
    rel = _write_report(root, round_info, results, cases, failure=failure, traces=traces)
    _git_commit(root, f"eval: 数据评测报告（轮次异常终止） {rel.split('/')[-1]}")


def _try_resume(
    project_id: str, root: Path, token: str, cases: list[dict],
    collect_failed: bool = False,
    object_fingerprint: str | None = None,
) -> bool:
    """续接 modeling 侧已有轮次，收取结果而不重跑。

    凭据过期、服务重启等中断后，modeling 侧轮次往往仍在执行或已完成。重跑且
    用例集未变时续接收取结果，不重复导入、不重复烧模型。

    collect_failed：显式「续接上一轮」时，已失败但有部分结果的轮次也收取成报告；
    重新运行评测时不收（用户要的是新一轮）。"""
    prior = read_state(root)
    if prior.get('report_round') and not _resumable(prior):
        # A collected round with technical errors must not trap reruns in the
        # same upstream done round forever. A rerun creates a new evaluation.
        return False
    if object_fingerprint is not None and prior.get("object_fingerprint") != object_fingerprint:
        return False
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
        rel = _write_report(
            root, details.get("round") or {}, details.get("results") or [], cases,
            traces=details.get("traces"),
        )
        _git_commit(root, f"eval: 数据评测报告 {rel.split('/')[-1]}")
        return True
    partial_results = collect_failed and bool(details.get("results"))
    if status == "failed" and partial_results:
        _write_state(root, status="running", phase="evaluate",
                     message="续接上一轮（收取异常终止前的结果）")
        _collect_failed_round(
            root, details.get("round") or {}, details["results"], cases, details.get("traces")
        )
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
    if _cancel_queued(project_id):
        # 还在排队：直接出队，不产生任何 modeling 侧开销，随即可重新运行
        _write_state(root, status="stopped", message=QUEUED_STOP_MESSAGE)
        return {"stopped": True, "cancelled": False, "message": QUEUED_STOP_MESSAGE}
    state = read_state(root)
    in_process = project_id in _running
    # 刚出队、编排还没写下 running 的瞬间：以内存态为准
    if state.get("status") not in ("running", "interrupted") and not in_process:
        raise ValueError("当前没有进行中的评测")
    if in_process:
        _stop_requested.add(project_id)

    mp_project, round_id = state.get("modeling_project"), state.get("round_id")
    cancelled, detail = False, ""
    if mp_project and round_id:
        cancelled, detail = cancel_round(
            mp_project, round_id, _live_tokens.get(project_id) or token
        )

    message = (
        STOPPED_MESSAGE if not detail
        else f"评测已停止（modeling 侧取消未确认：{detail[:120]}）"
    )
    _write_state(root, status="stopped", message=message)
    return {"stopped": True, "cancelled": cancelled, "message": message}


def cancel_round(mp_project: str, round_id: str, token: str) -> tuple[bool, str]:
    """取消 modeling 侧评测轮次，返回（是否取消、未确认的原因）。

    404/409（轮次不存在或已结束）视为目标状态已达成：未取消，但也不算失败。
    """
    try:
        _http(
            "POST",
            f"/api/projects/{mp_project}/evaluate/cancel",
            token,
            body={"roundId": round_id},
            timeout=30,
        )
        return True, ""
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 409):
            return False, ""
        return False, str(getattr(exc, "detail", "") or exc)
    except Exception as exc:  # 网络不通等：由调用方决定如何呈现
        return False, str(exc)


def abort_for_delete(project_id: str, root: Path, token: str) -> tuple[str, str, str] | None:
    """删除项目时终止评测：只改内存标记、读状态，不发网络请求，立即返回。

    进程内有编排就置协作取消标记——刚起步、状态还没写成 running 的也算，否则它会在
    项目删除后照常去 modeling 侧发起一轮没人收取的评测。状态里有未完成的轮次时，
    返回取消它所需的（modeling 项目、轮次、token），由调用方放到响应之后调用 cancel_round。
    失败、已停止的也算：轮询因凭据过期或超时失败、停止时取消未确认，modeling 侧都可能
    还在跑；其实已结束的，modeling 回 409，无害。
    """
    _cancel_queued(project_id)  # 还在排队的轮次直接出队，不再等槽位
    if project_id in _running:
        _stop_requested.add(project_id)
    state = read_state(root)
    mp_project, round_id = state.get("modeling_project"), state.get("round_id")
    unfinished = state.get("status") in ("running", "interrupted", "failed", "stopped")
    if unfinished and mp_project and round_id:
        return mp_project, round_id, _live_tokens.get(project_id) or token
    return None


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
        await _run_in_eval_pool(project_id, _resume_sync, project_id, root, token)
    finally:
        _running.discard(project_id)
        _live_tokens.pop(project_id, None)
        _stop_requested.discard(project_id)


def _resume_sync(project_id: str, root: Path, token: str) -> None:
    prior = read_state(root)
    # 状态里的问题是截断过的，只用于进度展示；报告内容取 modeling 侧结果
    cases = [
        {"question": str(c.get("question", "")), "groundTruth": "",
         "question_truncated": len(str(c.get('question', ''))) == 120}
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
        code = str(getattr(exc, "error_code", "") or "").strip()
        details = getattr(exc, "error_details", {})
        if code:
            error = RuntimeError(f"数据源配置失败（{label}）：{detail}")
            error.error_code = code
            error.error_details = details
            raise error from exc
        raise RuntimeError(f"数据源配置失败（{label}）：{detail}") from exc


async def run(
    project_id: str, root: Path, token: str, cases: list[dict],
    data_source: dict | None = None, eval_set_name: str = "", allow_unbound: bool = False,
    refresh_token: str = "", model_profile_id: str = "", case_concurrency: int | None = None,
    judge_model: dict | None = None,
) -> None:
    """后台评测编排；进度经 .ontology-state/eval.json 供前端轮询。"""
    if project_id in _running:
        return
    _running.add(project_id)
    _stop_requested.discard(project_id)  # 上一轮遗留的取消标记不影响新编排
    _live_tokens[project_id] = token
    try:
        await _run_in_eval_pool(
            project_id, _run_sync,
            project_id, root, token, cases, data_source, eval_set_name, allow_unbound,
            refresh_token, model_profile_id, case_concurrency, judge_model,
        )
    finally:
        _running.discard(project_id)
        _live_tokens.pop(project_id, None)
        _stop_requested.discard(project_id)


def _run_sync(
    project_id: str, root: Path, token: str, cases: list[dict],
    data_source: dict | None = None, eval_set_name: str = "", allow_unbound: bool = False,
    refresh_token: str = "", model_profile_id: str = "", case_concurrency: int | None = None,
    judge_model: dict | None = None,
) -> None:
    from .workspace import WorkspaceManager

    def tok() -> str:
        return _live_tokens.get(project_id) or token

    data_source = data_source or {"mode": "none"}
    mode = str(data_source.get("mode") or "none")
    dataset_mode = mode == "dataset"
    try:
        object_texts = evaluation_object_snapshot(root)
        object_fingerprint = object_snapshot_fingerprint(object_texts)
        # 导入数据先整理成工作簿：坏输入快速失败，不浪费一次包导入
        plan = (
            eval_import.prepare(root, data_source, graph=is_graph(data_source))
            if needs_import(data_source) else None
        )
        # Explicitly starting with a user's judge must not reuse a round launched
        # with another user's credentials. The separate resume action still collects it.
        if judge_model is None and _try_resume(
            project_id, root, token, cases, object_fingerprint=object_fingerprint
        ):
            return
        _check_stop(project_id)
        _write_state(
            root, status="running", phase="package", message="打包知识包",
            # 新一轮的逐轮字段清空：图导入结果等只属于产生它的那一轮
            report_round=None, graph_import=None, unbound_objects=None, started_at=int(time.time()),
            modeling_project=None, round_id=None,
            error_code=None, error_details=None,
            object_fingerprint=object_fingerprint,
            hub_snapshot_id=None, hub_snapshot_digest=None,
            data_source_name=data_source.get("name") or eval_data_sources.default_name(data_source),
            eval_set_name=eval_set_name,
            judge_source="onto-modeling" if judge_model else "modeling",
        )
        manager = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)
        import tempfile

        zip_path = Path(tempfile.mkdtemp(prefix="onto-eval-")) / "package.zip"
        # 导入模式的绑定按本轮导入工作簿算；其余模式按项目数据表算，allow_unbound 即用户已确认降级
        binding = plan["binding"] if plan else package_mapping.resolve_object_tables(
            root, strict_declared=not allow_unbound
        )
        relation_report: dict = {}
        try:
            from . import hub_packages, auth
            from .workspace import WorkspaceError
            claims = auth.verify_access_token(tok()) if auth.ENABLED else None
            if not claims or not claims.get('sub'):
                raise WorkspaceError('Hub 评测快照需要有效用户身份', 401)
            subject = json.dumps([claims.get('iss', auth.ISSUER), claims['sub']], ensure_ascii=False)
            snapshot = hub_packages.publish(
                manager, project_id, uuid.uuid4().hex, tok(), subject, allow_unbound,
                kind='evaluation_snapshot', binding=binding, graph=is_graph(data_source),
                relation_report=relation_report, object_texts=object_texts,
            )
            hub_packages.download(snapshot['ontology_id'], snapshot['release_id'], tok(), zip_path)
            _write_state(root, hub_snapshot_id=snapshot['release_id'],
                         ontology_id=snapshot['ontology_id'],
                         hub_snapshot_digest=snapshot['package_digest'])
        finally:
            if is_graph(data_source):
                _write_state(root, graph_import={
                    "nodes": 0, "relationships": 0, "relations_mapped": 0, **relation_report,
                })
        unbound = sorted({
            item["object"] for item in binding["issues"] if item["kind"] != "declared_unverifiable"
        })
        if unbound:
            _write_state(root, unbound_objects=unbound[:50])
        if plan:
            plan["relation_report"] = relation_report
            mismatched = eval_import.verify_mapping(zip_path, plan["tables"])
            if mismatched:
                raise RuntimeError("发布包 mapping 与导入数据不一致：" + "；".join(mismatched[:3]))

        _write_state(root, phase="import", message="导入 modeling 评测工作区")
        package_name = f"onto-eval-{project_id}"
        content = zip_path.read_bytes()
        zip_path.unlink(missing_ok=True)
        zip_path.parent.rmdir()
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
        # 轮次在 modeling 侧后台执行，常超 access token 有效期：带上 refresh token，
        # modeling 据此为本轮登记可续期的凭据会话。无鉴权部署没有 refresh token，照旧空 body。
        # refresh token 只经此请求体转交，不落状态、不写日志
        started = _http(
            "POST", f"/api/projects/{mp_project}/evaluate", tok(),
            body={
                **({"refresh_token": refresh_token} if refresh_token else {}),
                **({"model_profile_id": model_profile_id} if model_profile_id else {}),
                **({"case_concurrency": case_concurrency} if case_concurrency else {}),
                # Resolved server-side for the initiating user; never persisted.
                **({"judge_model": judge_model} if judge_model is not None else {}),
            },
        )
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
        state_fields = {"status": "failed", "message": text[:300]}
        code = str(getattr(exc, "error_code", "") or "").strip()
        details = getattr(exc, "error_details", None)
        if code:
            state_fields["error_code"] = code
        if isinstance(details, dict):
            state_fields["error_details"] = details
        _write_state(root, **state_fields)
