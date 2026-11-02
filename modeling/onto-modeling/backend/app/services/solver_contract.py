"""求解契约（operation-centric.v1）在建模工作区与已发布知识包之间的无损往返。

知识包侧的布局以 consumer/backend/ontology/solver_package_layout.py 与
solver_coordination/capability_catalog.py 为准：

- manifest 为 `schema_version: '0.2'`，声明 `solver_layout: operation-centric.v1`
  与 `capabilities.decision_models[]`（模型 ID、绑定 operation 与五份 sidecar 路径）；
- 求解 Operation 的 frontmatter 带 `execution.mode: solver_workflow` 与 decision_model，
  key 满足 ^[a-z][a-z0-9_]{0,31}$；
- 五份 sidecar 平铺在 operations/ 下、以 operation key 为前缀；
- 根 index.md 与 operations/index.md 都列出全部启用的求解 operation key。

工作区侧每个求解 Operation 一个受管目录（agent 与通用文件编辑器都不可写）：

    ontology/solver/<solver_key>/
        binding.yaml               建模侧元数据：工作区 Operation、模型 ID、声明扩展字段、单位
        model.yaml                 Decision Model
        input.schema.json          输入 Schema
        result.schema.json         结果 Schema
        fact-snapshot.schema.json  事实快照 Schema
        presentation.yaml          Presentation

sidecar 按原字节保存、原字节导出，导入再导出不会因重新序列化改变契约摘要。工作区
Operation 文件名常是中文，consumer 却要求求解 operation key 为小写标识符：导出时按
binding.yaml 的 solver_key 改名。binding.yaml 本身不随包发布。
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from . import package_mapping

SOLVER_DIR = "ontology/solver"
BINDING_FILE = "binding.yaml"
OPERATION_CENTRIC_LAYOUT = "operation-centric.v1"
SOLVER_EXECUTION_MODE = "solver_workflow"
SOLVER_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SUPPORTED_SCHEMA_VERSIONS = ("0.1", "0.2")
INDEX_BLOCK_START = "<!-- solver-capabilities:v1:start -->"
INDEX_BLOCK_END = "<!-- solver-capabilities:v1:end -->"

# 稳定错误码（需求规格 2.3）；solver_contract_invalid 覆盖语法与结构错误
CODE_VERSION_INCOMPATIBLE = "solver_contract_version_incompatible"
CODE_REFERENCE_MISSING = "solver_contract_reference_missing"
CODE_CONTEXT_MISSING = "solver_context_missing"
CODE_RUNTIME_BINDING_FORBIDDEN = "solver_runtime_binding_forbidden"
CODE_VALIDATION_STALE = "solver_contract_validation_stale"
CODE_INVALID = "solver_contract_invalid"
# 发布门禁：当前修订缺少通过的端到端验证 / 最近一次验证失败
CODE_VALIDATION_REQUIRED = "solver_contract_validation_required"
CODE_VALIDATION_FAILED = "solver_contract_validation_failed"

FINDING_HINTS = {
    CODE_VERSION_INCOMPATIBLE: "请改用平台支持的契约版本或 Profile。",
    CODE_REFERENCE_MISSING: "补齐被引用的文件，或修正引用路径与 Operation 绑定。",
    CODE_CONTEXT_MISSING: "在评估器模块顶层定义 SOLVER_CONTEXT。",
    CODE_RUNTIME_BINDING_FORBIDDEN: "Endpoint、Token、口令、服务地址等运行时配置属于消费平台，删除后再发布。",
    CODE_VALIDATION_STALE: "内容已修改，请对当前修订重新验证。",
    CODE_INVALID: "按提示修正文件格式或字段取值。",
    CODE_VALIDATION_REQUIRED: "求解知识包发布前须对当前修订完成一次 Consumer 端到端验证。",
    CODE_VALIDATION_FAILED: "查看验证诊断，修正后对当前修订重新验证。",
}

# consumer 运行时当前唯一支持的 Profile 及其事实来源约束（solver_fact_providers）
SUPPORTED_PROFILES = ("ordered_binary_route.v1",)
FACT_SOURCE_KEYS = frozenset({"kind", "fail_closed", "evaluator_path"})

# 平台注册的问题族目录：问题族 → 可声明的引擎能力。口径对齐 consumer：OR-Tools 连接器
# 只接受 candidate_selection.v1 与早期内部族名 binary_ordered_route.v1（两者映射到同一
# 公开问题类型，导入的旧包可保留），能力即 OR-Tools 服务能力里该问题类型的 features
PROBLEM_FAMILY_CATALOG = {
    "candidate_selection.v1": {
        # 界面上的名字（契约 SV-01 problem_families[].label）
        "label": "候选方案二选一",
        "capabilities": ("domain_candidate_evaluation", "lexicographic_objective"),
        "aliases": ("binary_ordered_route.v1",),
    },
}

# ordered_binary_route.v1 对 Decision Model 的锁定要求（consumer profiles/ordered_binary_route.py）：
# 事实提供方只产出这两项事实，目标须为固定的词典序基线，选项域固定为两个不透明标识
ORDERED_ROUTE_REQUIRED_FACTS = ("route_metrics", "refinery_route_context")
ORDERED_ROUTE_OBJECTIVES = (
    {"metric": "primary_score", "sense": "maximize"},
    {"metric": "transition_count", "sense": "minimize"},
    {"metric": "safety_margin", "sense": "maximize"},
    {"metric": "candidate_rank", "sense": "minimize"},
)
ORDERED_ROUTE_OPTIONS = ("OPTION_0", "OPTION_1")
MAX_OPTION_LABEL_LENGTH = 128
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# consumer 结果文字：声明后最终回答写成「<装置编号><process_name>的 N 月度生产规划」，并逐批列出
# 品种、批次量、天数与「<stream_name>→<选项显示名称>」；配置不合法时 consumer 拒收整个求解结果
TEXT_PRESENTATION_KIND = "ordered_route_plan_zh.v1"
TEXT_PRESENTATION_FIELDS = ("process_name", "stream_name")
# consumer 适配器与事实 worker 读取的输入字段；同组内任一别名即可（适配器归一到首个），
# input_weight_tons 会被适配器丢弃，不算批次量
ORDERED_ROUTE_INPUT_FIELDS = ("plan_month", "diesel_unit_no", "items")
ORDERED_ROUTE_ITEM_FIELDS = (
    ("item_order",), ("item_id",), ("crude_origin", "source"),
    ("quantity_wan_tons", "quantity"), ("process_days", "duration"),
)
# consumer SolverInputBinder 只认这组 JSON Schema 关键字与类型，其余在运行时拒绝
_BINDER_SCHEMA_KEYWORDS = frozenset({
    "$schema", "$id", "title", "description", "type", "properties", "required",
    "additionalProperties", "enum", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems", "items",
    "unit", "precision",
})
_BINDER_SCHEMA_TYPES = frozenset({"string", "integer", "number", "boolean", "object", "array", "null"})


def _load_evaluator_contract():
    """评估器规则以工具箱的 solver_evaluator_contract.py 为单一事实源，经共享契约库加载（方案 A16）。

    /ontology-solver-evaluator 的自检与这里的契约检查共用同一份实现，避免两处规则漂移。
    """
    from .packs import contracts

    return contracts.default().load("solver_evaluator_contract")


_evaluator_contract = _load_evaluator_contract()
EVALUATOR_DISCOVERY_SYMBOLS = _evaluator_contract.EVALUATOR_DISCOVERY_SYMBOLS
EVALUATOR_CONTEXT_SYMBOL = _evaluator_contract.EVALUATOR_CONTEXT_SYMBOL
EVALUATOR_MODULE_CONSTANTS = _evaluator_contract.EVALUATOR_MODULE_CONSTANTS
EVALUATOR_CONTEXT_KEYS = _evaluator_contract.EVALUATOR_CONTEXT_KEYS
EVALUATOR_INTERFACE_HINT = _evaluator_contract.EVALUATOR_INTERFACE_HINT
EVALUATOR_DECLARATIVE_HINT = _evaluator_contract.DECLARATIVE_HINT
MAX_EVALUATOR_BYTES = _evaluator_contract.MAX_EVALUATOR_BYTES
# 声明式评估器的问题类别 → 稳定错误码；警告（如未找到对象目录）只在助手自检里提示
_DECLARATIVE_KIND_CODES = {
    _evaluator_contract.KIND_INVALID: CODE_INVALID,
    _evaluator_contract.KIND_REFERENCE: CODE_REFERENCE_MISSING,
    _evaluator_contract.KIND_CONTEXT: CODE_CONTEXT_MISSING,
}

# 与 consumer validate_solver_package_layout 的布局标记识别口径一致
_MARKER_KEYS = ("solver_layout", "solver_layout_version", "layout_version", "layout")
_MARKER_CONTAINERS = ("capabilities", "solver", "solver_capabilities")
_KNOWN_MARKERS = {
    "operation-centric.v1", "operation-centric", "operation_centric.v1",
    "operation_centric", "legacy",
}
# 运行时绑定检测（REQ-PKG-005）比 consumer ontology.ontologies.validate_no_runtime_binding_data 更严：
# consumer 只认 endpoint、token、secret、platform 四个键名片段，建模侧另查凭据类键名与取值形态，
# 在保存与发布前拦下 consumer 漏判的内容。报错只给字段路径与类别，不回显取值。
# 键名去掉分隔符后按子串匹配：providerRevision、api_key、connectionString 都能识别
_FORBIDDEN_KEY_TERMS = (
    "endpoint", "token", "secret", "password", "passwd", "credential", "cookie", "authorization",
    "platform", "runtimebinding", "providerrevision", "apikey", "accesskey", "privatekey",
    "connectionstring",
)
# 短词按完整片段匹配，避免 purity、security 之类的业务词误判
_FORBIDDEN_KEY_PARTS = frozenset({"url", "urls", "uri", "uris", "dsn"})
_FORBIDDEN_VALUE_PATTERNS = (
    ("服务地址", re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)),
    ("主机端口", re.compile(r"\b(?:localhost|\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}\b", re.IGNORECASE)),
    ("证书或私钥", re.compile(r"-----BEGIN [A-Z0-9 ]+-----")),
    ("Bearer 凭据", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}")),
)
# JSON Schema 方言声明（$schema）是规范标识，不是服务地址
_SCHEMA_DIALECT_RE = re.compile(r"^https?://json-schema\.org/[A-Za-z0-9/._-]*#?$")
_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n(?P<body>.*?)^---[ \t]*(?:\r?\n|$)", re.MULTILINE | re.DOTALL
)


@dataclass(frozen=True)
class Sidecar:
    role: str  # manifest 声明里的规范字段名
    filename: str  # 工作区文件名
    suffix: str  # 知识包内 operations/<key><suffix>
    aliases: tuple[str, ...]  # consumer 认可的同义字段，按优先级
    label: str


SIDECARS = (
    Sidecar("entry", "model.yaml", ".model.yaml",
            ("entry", "decision_model_entry", "decision_model_path"), "Decision Model"),
    Sidecar("input_schema", "input.schema.json", ".input.schema.json",
            ("input_schema", "input"), "输入 Schema"),
    Sidecar("result_schema", "result.schema.json", ".result.schema.json",
            ("result_schema", "output_schema", "output"), "结果 Schema"),
    Sidecar("fact_snapshot_schema", "fact-snapshot.schema.json", ".fact-snapshot.schema.json",
            ("fact_snapshot_schema", "fact_schema", "fact_snapshot"), "事实快照 Schema"),
    Sidecar("presentation", "presentation.yaml", ".presentation.yaml",
            ("presentation", "presentation_path"), "Presentation"),
)
SIDECAR_BY_ROLE = {sidecar.role: sidecar for sidecar in SIDECARS}
# 声明里的身份与路径字段由 binding 与布局重新生成，不进 binding.declaration
_DECLARATION_MANAGED_KEYS = frozenset(
    {"id", "model_id", "operation_key", "operation", "bound_operation", "key",
     "decision_model", "decision_model_id"}
    | {alias for sidecar in SIDECARS for alias in sidecar.aliases}
)


class SolverContractError(ValueError):
    """单条求解契约问题：稳定 code + 定位到包内/工作区文件与字段。"""

    def __init__(self, code: str, message: str, *, path: str = "", field: str = "", hint: str = ""):
        super().__init__(message)
        self.code = code
        self.path = path
        self.field = field
        self.hint = hint  # 覆盖错误码的通用提示

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "path": self.path, "field": self.field}


@dataclass
class PackageSolver:
    """已发布知识包里的求解契约，按工作区布局展开后的结果。"""

    workspace_files: dict[str, bytes] = field(default_factory=dict)
    operation_keys: tuple[str, ...] = ()
    problems: tuple[SolverContractError, ...] = ()


@dataclass
class WorkspaceSolverContract:
    solver_key: str
    operation: str  # 工作区 operation 文件名（不含 .md）
    decision_model: str
    binding: dict
    sidecars: dict[str, bytes]  # role -> 原字节

    @property
    def enabled(self) -> bool:
        frontmatter = self.binding.get("operation_frontmatter") or {}
        execution = frontmatter.get("execution") or {}
        declaration = self.binding.get("declaration") or {}
        flags = (
            frontmatter.get("enabled", True),
            execution.get("enabled", True),
            declaration.get("enabled", True),
        )
        return all(_enabled(value) for value in flags)


@dataclass
class PackageSolverRender:
    """导出知识包时需要的求解投影。"""

    contracts: tuple[WorkspaceSolverContract, ...] = ()
    files: dict[str, bytes] = field(default_factory=dict)  # 包内相对路径 -> 字节
    capabilities: dict = field(default_factory=dict)

    @property
    def renames(self) -> dict[str, str]:
        return {item.operation: item.solver_key for item in self.contracts}

    @property
    def index_keys(self) -> list[str]:
        return sorted(item.solver_key for item in self.contracts if item.enabled)


# ---- 通用解析 ----


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().casefold() in {"true", "1", "yes", "on", "enabled"}


def read_frontmatter(text: str) -> Mapping | None:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    try:
        value = yaml.safe_load(match.group("body")) or {}
    except yaml.YAMLError:
        return None
    return value if isinstance(value, Mapping) else None


def _forbidden_key(key: str) -> bool:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")
    compact = normalized.replace("_", "")
    return any(term in compact for term in _FORBIDDEN_KEY_TERMS) or bool(
        set(normalized.split("_")) & _FORBIDDEN_KEY_PARTS
    )


def _forbidden_value(key: str, value: str) -> str:
    """取值形如服务地址或凭据时返回类别，否则返回空串。"""
    if key == "$schema" and _SCHEMA_DIALECT_RE.match(value.strip()):
        return ""
    return next((label for label, pattern in _FORBIDDEN_VALUE_PATTERNS if pattern.search(value)), "")


def find_runtime_binding(
    value: Any, prefix: str = "", *, check_values: bool = True, key: str = ""
) -> tuple[str, str] | None:
    """返回第一处运行时绑定的 (字段路径, 类别)；没有返回 None。check_values=False 只查键名。"""
    if isinstance(value, str):
        label = _forbidden_value(key, value) if check_values else ""
        return (prefix, label) if label else None
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            name = str(raw_key or "").strip()
            here = f"{prefix}.{name}" if prefix else name
            if _forbidden_key(name):
                return here, "键名"
            found = find_runtime_binding(item, here, check_values=check_values, key=name)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = find_runtime_binding(item, f"{prefix}[{index}]", check_values=check_values)
            if found:
                return found
    return None


def _runtime_binding_error(path: str, field: str, label: str) -> SolverContractError:
    if label == "键名":
        message = (
            f"{path} 含运行时绑定字段 {field}：Endpoint、Token、Secret、口令、平台名等由消费平台管理，"
            "禁止随知识包发布"
        )
    else:
        message = f"{path} 的 {field} 取值含{label}：服务地址与凭据由消费平台管理，禁止随知识包发布"
    return SolverContractError(CODE_RUNTIME_BINDING_FORBIDDEN, message, path=path, field=field)


def _reject_runtime_binding(value: Any, path: str, *, check_values: bool = True) -> None:
    found = find_runtime_binding(value, check_values=check_values)
    if found:
        raise _runtime_binding_error(path, *found)


def _evaluator_runtime_binding(source: bytes | None, rel: str) -> list[SolverContractError]:
    """评估器源码里的运行时绑定（只解析不执行）：顶层赋值名、字典字面量的键与全部字符串常量。

    评估器随包发布，写进源码的服务地址或凭据同样会进入消费平台。
    """
    try:
        module = ast.parse((source or b"").decode("utf-8"))
    except (SyntaxError, ValueError):  # UnicodeDecodeError 是 ValueError 的子类
        return []  # 无法解析由评估器检查另行报告
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names = [statement.target.id]
        else:
            names = []
        for name in names:
            if _forbidden_key(name):
                return [_runtime_binding_error(rel, name, "键名")]
        owner = names[0] if names else ""
        for node in ast.walk(statement):
            keys = node.keys if isinstance(node, ast.Dict) else ()
            for key in keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and _forbidden_key(key.value):
                    return [_runtime_binding_error(rel, owner or f"第 {key.lineno} 行", "键名")]
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                label = _forbidden_value("", node.value)
                if label:
                    return [_runtime_binding_error(rel, owner or f"第 {node.lineno} 行", label)]
    return []


def _reject_remote_refs(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in {"$ref", "$dynamicRef"} and (
                not isinstance(item, str) or not item.startswith("#/")
            ):
                raise ValueError("只允许包内（#/ 开头）的 JSON Schema 引用")
            if str(key) == "$id" and isinstance(item, str) and item.strip().casefold().startswith(
                ("http:", "https:", "//")
            ):
                raise ValueError("不允许远程 JSON Schema $id")
            _reject_remote_refs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_remote_refs(item)


def parse_sidecar(data: bytes, filename: str, path: str) -> Mapping:
    """sidecar 语法与声明式安全检查，对齐 consumer _validate_sidecar_document。"""
    try:
        text = data.decode("utf-8")
        value = json.loads(text) if filename.endswith(".json") else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:  # ValueError 已涵盖 UnicodeDecodeError 与 JSONDecodeError
        raise SolverContractError(CODE_INVALID, f"{path} 无法解析：{exc}", path=path) from exc
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise SolverContractError(CODE_INVALID, f"{path} 顶层必须是映射", path=path)
    if filename.endswith(".json"):
        try:
            _reject_remote_refs(value)
            Draft202012Validator.check_schema(dict(value))
        except Exception as exc:  # jsonschema 的 SchemaError 与上面的 ValueError
            message = getattr(exc, "message", None) or str(exc)
            raise SolverContractError(
                CODE_INVALID, f"{path} 不是合法的本地 JSON Schema 2020-12：{message}", path=path
            ) from exc
    _reject_runtime_binding(value, path)
    return value


def layout_marker(manifest: Mapping) -> str:
    for container in (manifest, *(
        manifest.get(key) for key in _MARKER_CONTAINERS if isinstance(manifest.get(key), Mapping)
    )):
        for key in _MARKER_KEYS:
            text = str(container.get(key) or "").strip()
            if text:
                return text
    return ""


def _safe_package_path(raw: Any) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return ""
    parts = PurePosixPath(text).parts
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    return text


def _declared(values: Mapping, sidecar: Sidecar) -> str | None:
    """声明的 sidecar 路径；未声明返回 None，声明了但不安全返回空串。"""
    for alias in sidecar.aliases:
        value = values.get(alias)
        if isinstance(value, str) and value.strip():
            return _safe_package_path(value)
    return None


def _dump_yaml(value: Any) -> bytes:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode("utf-8")


def _model_id(execution: Mapping, frontmatter: Mapping) -> str:
    return str(
        execution.get("decision_model")
        or execution.get("decision_model_id")
        or frontmatter.get("decision_model")
        or frontmatter.get("decision_model_id")
        or ""
    ).strip()


# ---- 知识包 → 工作区 ----


def _declarations(manifest: Mapping) -> tuple[dict[str, dict], list[Mapping]]:
    capabilities = manifest.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}

    def as_list(value: Any) -> list:
        return list(value) if isinstance(value, list) else []

    models: dict[str, dict] = {}
    for raw in as_list(capabilities.get("decision_models")) + as_list(manifest.get("decision_models")):
        if not isinstance(raw, Mapping):
            continue
        model_id = str(raw.get("id") or raw.get("model_id") or "").strip()
        if not model_id:
            continue
        if model_id in models:
            raise SolverContractError(
                CODE_INVALID, f"manifest 重复声明 Decision Model：{model_id}",
                path="manifest.yaml", field="capabilities.decision_models",
            )
        models[model_id] = dict(raw)
    operations = as_list(capabilities.get("solver_operations")) + as_list(manifest.get("solver_operations"))
    overrides = [raw for raw in operations if isinstance(raw, Mapping)]
    return models, overrides


def _merged_declaration(
    models: dict[str, dict], overrides: list[Mapping], operation_key: str, model_id: str
) -> dict | None:
    if model_id not in models:
        return None
    merged = dict(models[model_id])
    for raw in overrides:
        key = str(raw.get("operation_key") or raw.get("operation") or raw.get("key") or "").strip()
        model = str(
            raw.get("decision_model") or raw.get("decision_model_id") or raw.get("model_id") or ""
        ).strip()
        if key == operation_key or (not key and model == model_id):
            merged.update(raw)
    return merged


def extract_package_solver(files: Mapping[str, bytes]) -> PackageSolver:
    """把已发布知识包（包根相对路径 → 字节）里的求解契约展开为工作区文件。

    0.1 与没有求解 Operation 的 0.2 包返回空结果，普通包导入行为不变。
    问题逐个收集，供导入预览一次性列出；有问题时 workspace_files 不可用。
    """
    raw_manifest = files.get("manifest.yaml")
    if raw_manifest is None:
        return PackageSolver()
    try:
        manifest = yaml.safe_load(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return PackageSolver()  # 清单本身坏了由既有导入流程处理，不归求解契约
    if not isinstance(manifest, Mapping):
        return PackageSolver()
    version = str(manifest.get("schema_version") or "").strip()
    if version and version not in SUPPORTED_SCHEMA_VERSIONS:
        return PackageSolver(problems=(SolverContractError(
            CODE_VERSION_INCOMPATIBLE,
            f"知识包 schema_version {version} 不受支持，支持范围：{'、'.join(SUPPORTED_SCHEMA_VERSIONS)}",
            path="manifest.yaml", field="schema_version",
        ),))
    if version != "0.2":
        return PackageSolver()

    problems: list[SolverContractError] = []
    marker = layout_marker(manifest)
    if marker and marker.casefold().replace("_", "-") not in {
        item.replace("_", "-") for item in _KNOWN_MARKERS
    }:
        problems.append(SolverContractError(
            CODE_VERSION_INCOMPATIBLE,
            f"求解包布局 {marker} 不受支持，支持范围：{OPERATION_CENTRIC_LAYOUT}、legacy",
            path="manifest.yaml", field="solver_layout",
        ))
    try:
        # 清单的通用字段（名称、说明等）不归求解契约，取值形态只查求解声明所在的 capabilities
        _reject_runtime_binding(manifest, "manifest.yaml", check_values=False)
        _reject_runtime_binding(
            {"capabilities": manifest.get("capabilities")}, "manifest.yaml"
        )
        models, overrides = _declarations(manifest)
    except SolverContractError as exc:
        return PackageSolver(problems=(*problems, exc))

    workspace_files: dict[str, bytes] = {}
    keys: list[str] = []
    used_models: set[str] = set()
    for path in sorted(files):
        name = path[len("operations/"):] if path.startswith("operations/") else ""
        if not name.endswith(".md") or "/" in name or name.casefold() == "index.md":
            continue
        try:
            frontmatter = read_frontmatter(files[path].decode("utf-8"))
        except UnicodeDecodeError:
            continue
        execution = frontmatter.get("execution") if frontmatter else None
        if (
            not isinstance(execution, Mapping)
            or str(execution.get("mode") or "").strip() != SOLVER_EXECUTION_MODE
            or str(frontmatter.get("resource") or "").strip() != "operation"
        ):
            continue
        used_models.add(_model_id(execution, frontmatter))
        try:
            key, files_for_operation = _extract_operation(
                files, path, frontmatter, execution, models, overrides
            )
        except SolverContractError as exc:
            problems.append(exc)
            continue
        if key in keys:
            problems.append(SolverContractError(
                CODE_INVALID, f"求解 operation key 重复：{key}", path=path, field="key"
            ))
            continue
        keys.append(key)
        workspace_files.update(files_for_operation)
    # 没有 Operation 认领的声明在工作区无处安放，再导出时会静默消失
    problems.extend(
        SolverContractError(
            CODE_REFERENCE_MISSING,
            f"manifest 声明的 Decision Model {model_id} 没有对应的求解 Operation",
            path="manifest.yaml", field="capabilities.decision_models",
        )
        for model_id in sorted(set(models) - used_models)
    )
    return PackageSolver(workspace_files, tuple(keys), tuple(problems))


def _extract_operation(
    files: Mapping[str, bytes],
    path: str,
    frontmatter: Mapping,
    execution: Mapping,
    models: dict[str, dict],
    overrides: list[Mapping],
) -> tuple[str, dict[str, bytes]]:
    _reject_runtime_binding(frontmatter, path)
    key = str(frontmatter.get("key") or "").strip()
    if not SOLVER_KEY_RE.fullmatch(key):
        raise SolverContractError(
            CODE_INVALID, f"求解 operation key {key!r} 不合法，须满足 ^[a-z][a-z0-9_]{{0,31}}$",
            path=path, field="key",
        )
    # 工作区以文件名为 Operation 身份（materialize 按文件名落盘），key 记为 solver_key
    stem = PurePosixPath(path).stem
    model_id = _model_id(execution, frontmatter)
    declaration = _merged_declaration(models, overrides, key, model_id)
    if declaration is None:
        raise SolverContractError(
            CODE_REFERENCE_MISSING,
            f"operation {key} 引用的 Decision Model {model_id or '（未声明）'} 不在 "
            "manifest capabilities.decision_models 中",
            path=path, field="execution.decision_model",
        )
    workspace_dir = f"{SOLVER_DIR}/{key}"
    out: dict[str, bytes] = {}
    for sidecar in SIDECARS:
        declared = _declared(declaration, sidecar)
        if declared is None:
            if sidecar.role in {"entry", "input_schema", "result_schema"}:
                raise SolverContractError(
                    CODE_REFERENCE_MISSING, f"Decision Model {model_id} 未声明{sidecar.label}",
                    path="manifest.yaml", field=f"capabilities.decision_models[{model_id}].{sidecar.role}",
                )
            continue  # 旧 0.2 包可不带事实快照与 Presentation；导出前由静态检查补齐要求
        data = files.get(declared) if declared else None
        if data is None:
            raise SolverContractError(
                CODE_REFERENCE_MISSING,
                f"Decision Model {model_id} 的{sidecar.label}路径无效或文件不存在：{declared or '（越界路径）'}",
                path="manifest.yaml", field=f"capabilities.decision_models[{model_id}].{sidecar.role}",
            )
        document = parse_sidecar(data, sidecar.filename, declared)
        if sidecar.role == "entry":
            document_id = str(document.get("id") or "").strip()
            if document_id and document_id != model_id:
                raise SolverContractError(
                    CODE_INVALID, f"Decision Model 文档 id {document_id} 与声明 {model_id} 不一致",
                    path=declared, field="id",
                )
        if sidecar.role == "input_schema":
            data = restore_template_input_schema(data)
        out[f"{workspace_dir}/{sidecar.filename}"] = data
    binding = {
        "solver_key": key,
        "operation": stem,
        "decision_model": model_id,
        "declaration": {
            name: value for name, value in declaration.items()
            if name not in _DECLARATION_MANAGED_KEYS
        },
        "operation_frontmatter": {
            name: value for name, value in frontmatter.items() if name not in {"resource", "key"}
        },
        "units": [],
    }
    out[f"{workspace_dir}/{BINDING_FILE}"] = _dump_yaml(binding)
    return key, out


# ---- 工作区 → 知识包 ----


def load_workspace_solver(
    root: Path, operation_stems: Iterable[str]
) -> tuple[list[WorkspaceSolverContract], list[SolverContractError]]:
    """读取工作区全部求解契约；问题逐个收集，供导出与发布检查共用。"""
    base = Path(root) / SOLVER_DIR
    if not base.is_dir():
        return [], []
    stems = set(operation_stems)
    contracts: list[WorkspaceSolverContract] = []
    problems: list[SolverContractError] = []
    for directory in sorted(base.iterdir(), key=lambda item: item.name):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        try:
            contracts.append(_load_contract(directory, stems))
        except SolverContractError as exc:
            problems.append(exc)
    seen: dict[str, str] = {}
    for contract in contracts:
        rel = f"{SOLVER_DIR}/{contract.solver_key}/{BINDING_FILE}"
        if contract.operation in seen:
            problems.append(SolverContractError(
                CODE_INVALID,
                f"工作区 Operation {contract.operation} 同时绑定了 {seen[contract.operation]} 与 "
                f"{contract.solver_key}",
                path=rel, field="operation",
            ))
        seen[contract.operation] = contract.solver_key
        if contract.solver_key != contract.operation and contract.solver_key in stems:
            problems.append(SolverContractError(
                CODE_INVALID,
                f"solver_key {contract.solver_key} 与另一个工作区 Operation 同名，导出后会互相覆盖",
                path=rel, field="solver_key",
            ))
    return contracts, problems


def _load_contract(directory: Path, stems: set[str]) -> WorkspaceSolverContract:
    rel_dir = f"{SOLVER_DIR}/{directory.name}"
    binding_rel = f"{rel_dir}/{BINDING_FILE}"
    binding_path = directory / BINDING_FILE
    if not binding_path.is_file() or binding_path.is_symlink():
        raise SolverContractError(CODE_REFERENCE_MISSING, f"{binding_rel} 不存在", path=binding_rel)
    try:
        binding = yaml.safe_load(binding_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SolverContractError(CODE_INVALID, f"{binding_rel} 无法解析：{exc}", path=binding_rel) from exc
    if not isinstance(binding, dict):
        raise SolverContractError(CODE_INVALID, f"{binding_rel} 顶层必须是映射", path=binding_rel)
    _reject_runtime_binding(binding, binding_rel)
    solver_key = str(binding.get("solver_key") or "").strip()
    if not SOLVER_KEY_RE.fullmatch(solver_key) or solver_key != directory.name:
        raise SolverContractError(
            CODE_INVALID,
            f"solver_key {solver_key!r} 须满足 ^[a-z][a-z0-9_]{{0,31}}$ 且与目录名 {directory.name} 一致",
            path=binding_rel, field="solver_key",
        )
    operation = str(binding.get("operation") or "").strip()
    if operation not in stems:
        raise SolverContractError(
            CODE_REFERENCE_MISSING, f"绑定的工作区 Operation「{operation or '（未填写）'}」不存在",
            path=binding_rel, field="operation",
        )
    decision_model = str(binding.get("decision_model") or "").strip()
    if not decision_model:
        raise SolverContractError(
            CODE_INVALID, "未填写 Decision Model ID", path=binding_rel, field="decision_model"
        )
    for name in ("declaration", "operation_frontmatter"):
        if not isinstance(binding.get(name) or {}, Mapping):
            raise SolverContractError(CODE_INVALID, f"{name} 必须是映射", path=binding_rel, field=name)
    execution = (binding.get("operation_frontmatter") or {}).get("execution") or {}
    if not isinstance(execution, Mapping):
        raise SolverContractError(
            CODE_INVALID, "execution 必须是映射", path=binding_rel, field="operation_frontmatter.execution"
        )
    sidecars: dict[str, bytes] = {}
    for sidecar in SIDECARS:
        path = directory / sidecar.filename
        rel = f"{rel_dir}/{sidecar.filename}"
        if not path.is_file() or path.is_symlink():
            raise SolverContractError(
                CODE_REFERENCE_MISSING, f"缺少{sidecar.label}：{rel}", path=rel
            )
        data = path.read_bytes()
        document = parse_sidecar(data, sidecar.filename, rel)
        if sidecar.role == "entry":
            document_id = str(document.get("id") or "").strip()
            if document_id and document_id != decision_model:
                raise SolverContractError(
                    CODE_INVALID, f"Decision Model 文档 id {document_id} 与绑定 {decision_model} 不一致",
                    path=rel, field="id",
                )
        sidecars[sidecar.role] = data
    return WorkspaceSolverContract(solver_key, operation, decision_model, binding, sidecars)


def render_package_solver(
    contracts: Iterable[WorkspaceSolverContract], evaluators: Mapping[str, Path] | None = None
) -> PackageSolverRender:
    """求解契约 → 包内 sidecar 与 manifest capabilities（确定性：按 solver_key 排序）。

    evaluators 是 solver_key → 工作区评估器文件，受管模板的输入 Schema 据此补可选值。
    """
    ordered = tuple(sorted(contracts, key=lambda item: item.solver_key))
    evaluators = evaluators or {}
    files: dict[str, bytes] = {}
    models = []
    for contract in ordered:
        entry: dict[str, Any] = {"id": contract.decision_model, "operation_key": contract.solver_key}
        for sidecar in SIDECARS:
            path = f"operations/{contract.solver_key}{sidecar.suffix}"
            entry[sidecar.role] = path
            data = contract.sidecars[sidecar.role]
            if sidecar.role == "input_schema":
                data = project_input_schema(data, evaluators.get(contract.solver_key))
            files[path] = data
        entry.update(contract.binding.get("declaration") or {})
        models.append(entry)
    capabilities = {"decision_models": models} if models else {}
    return PackageSolverRender(ordered, files, capabilities)


def operation_frontmatter(
    contract: WorkspaceSolverContract, title: str, used_objects: list[str], object_keys: set[str],
    report_name: str | None = None,
) -> str:
    """求解 Operation 的包内 frontmatter：保留导入时的扩展字段，身份与执行模式以 binding 为准。

    report_name 是工作区文档记录的业务原名，与普通 Operation 一样随包带出，回导时据此还原。
    回导按 JSON 字符串解析该行（document_metadata.report_name），不能交给 YAML 序列化。
    """
    preserved = dict(contract.binding.get("operation_frontmatter") or {})
    kept_name = preserved.pop("report_name", None)
    if report_name is None and isinstance(kept_name, str):
        report_name = kept_name
    for alias in ("decision_model", "decision_model_id"):
        if alias in preserved:
            preserved[alias] = contract.decision_model
    execution = dict(preserved.pop("execution", None) or {})
    execution["mode"] = SOLVER_EXECUTION_MODE
    if "decision_model_id" in execution and "decision_model" not in execution:
        execution["decision_model_id"] = contract.decision_model
    else:
        execution["decision_model"] = contract.decision_model
    uses = dict(preserved.pop("uses", None) or {})
    kept = [item for item in uses.get("objects") or [] if item in object_keys]
    uses["objects"] = sorted(set(used_objects) | set(kept))
    document = {
        "resource": "operation",
        "key": contract.solver_key,
        "title": preserved.pop("title", None) or title,
        **preserved,
        "execution": execution,
        "uses": uses,
    }
    text = _dump_yaml(document).decode("utf-8")
    if report_name is not None:
        text += f"report_name: {json.dumps(report_name, ensure_ascii=False)}\n"
    return "---\n" + text + "---\n\n"


def manifest_solver_section(render: PackageSolverRender) -> str:
    """追加到 manifest.yaml 的求解声明（schema_version 由调用方改为 0.2）。"""
    return _dump_yaml({
        "solver_layout": OPERATION_CENTRIC_LAYOUT,
        "capabilities": render.capabilities,
    }).decode("utf-8")


def index_block(render: PackageSolverRender) -> str:
    """根 index.md 的求解能力投影块，与 consumer _upsert_solver_index_block 同形。"""
    keys = render.index_keys
    if not keys:
        return ""
    rows = "".join(f"| {key} | {key} | {SOLVER_EXECUTION_MODE} |\n" for key in keys)
    return (
        f"\n{INDEX_BLOCK_START}\n# Solver capabilities\n\n"
        "| capability key | operation key | execution |\n| --- | --- | --- |\n"
        f"{rows}{INDEX_BLOCK_END}\n"
    )


# ---- 静态检查（发布体检与发布门禁共用）----


@dataclass
class SolverCheck:
    contracts: list[WorkspaceSolverContract] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    revision: str = ""  # 求解契约修订：契约文件、绑定 Operation 与评估器源码的摘要
    evaluators: dict[str, str] = field(default_factory=dict)  # solver_key -> 工作区评估器路径
    digests: dict = field(default_factory=dict)  # 规范化契约摘要，见 contract_digests

    @property
    def enabled(self) -> bool:
        return bool(self.contracts or self.findings)

    @property
    def needs_validation(self) -> bool:
        return any(item.enabled for item in self.contracts)


def finding(problem: SolverContractError, solver_key: str = "") -> dict:
    return {
        **problem.as_dict(),
        "severity": "error",
        "solver_key": solver_key or _problem_solver_key(problem),
        "hint": problem.hint or FINDING_HINTS.get(problem.code, ""),
    }


def is_evaluator(source: bytes | None) -> bool:
    """按 consumer 的评估器发现规则判断脚本源码是否为求解评估器（旧式或声明式）。"""
    return _evaluator_contract.evaluator_kind(source) is not None


def is_evaluator_file(path: Path) -> bool:
    return is_evaluator(_read_evaluator(path))


def _read_evaluator(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _object_fields(root: Path) -> dict[str, list[dict]]:
    """本体对象 → 字段（与发布包导出同一来源），供声明式评估器核对对象与属性引用。"""
    return {
        key: package_mapping.parse_object_doc(text)["fields"]
        for key, text in package_mapping.read_object_texts(root).items()
    }


def _check_declarative_evaluator(source: bytes | None, root: Path, rel: str) -> list[SolverContractError]:
    """声明式评估器的全部错误；警告不阻断发布，留给助手自检。"""
    return [
        SolverContractError(
            _DECLARATIVE_KIND_CODES[problem.kind], problem.message,
            path=rel, field=problem.field, hint=EVALUATOR_DECLARATIVE_HINT,
        )
        for problem in _evaluator_contract.check_declarative(source, _object_fields(root))
        if problem.severity == _evaluator_contract.SEVERITY_ERROR
    ]


def _check_evaluator(
    fact_source: Mapping,
    scripts: Mapping[str, Path],
    root: Path,
    model_rel: str,
) -> tuple[list[SolverContractError], str]:
    """按 consumer 的评估器发现规则定位评估器：旧式检查兼容层符号，声明式做完整校验。"""
    explicit = fact_source.get("evaluator_path")
    if explicit is not None:
        package_path = _safe_package_path(explicit)
        path = scripts.get(package_path) if package_path else None
        if path is None:
            return [SolverContractError(
                CODE_REFERENCE_MISSING,
                f"fact_source.evaluator_path 指向的评估器不存在：{explicit}",
                path=model_rel, field="fact_source.evaluator_path",
            )], ""
        source = _read_evaluator(path)
        required = EVALUATOR_DISCOVERY_SYMBOLS
    else:
        # 旧式与声明式评估器合计必须恰好一个，两份并存时不静默挑一份
        matches = []
        for _package_path, candidate in sorted(scripts.items()):
            candidate_source = _read_evaluator(candidate)
            if _evaluator_contract.evaluator_kind(candidate_source):
                matches.append((candidate, candidate_source))
        if not matches:
            return [SolverContractError(
                CODE_REFERENCE_MISSING,
                "未找到评估器：ontology/scripts/ 下没有同时定义 "
                + "、".join(sorted(EVALUATOR_DISCOVERY_SYMBOLS))
                + " 的脚本，也没有声明 TABLE_SOURCES 的声明式评估器",
                path=model_rel, field="fact_source.evaluator_path", hint=EVALUATOR_INTERFACE_HINT,
            )], ""
        if len(matches) > 1:
            return [SolverContractError(
                CODE_INVALID,
                f"评估器不唯一（{'、'.join(_rel(root, m) for m, _source in matches)}），"
                "请用 fact_source.evaluator_path 指定",
                path=model_rel, field="fact_source.evaluator_path",
            )], ""
        path, source = matches[0]
        required = frozenset()
    rel = _rel(root, path)
    problems = _evaluator_runtime_binding(source, rel)
    if _evaluator_contract.evaluator_kind(source) == _evaluator_contract.DECLARATIVE_KIND:
        return problems + _check_declarative_evaluator(source, root, rel), rel
    symbols = _evaluator_contract.module_symbols(source)
    if symbols is None:
        return [SolverContractError(
            CODE_INVALID, _evaluator_contract.UNPARSEABLE_MESSAGE, path=rel
        )], rel
    defined, assigned = symbols.defined, symbols.assigned
    missing = sorted(required - defined - assigned)
    if missing:
        problems.append(SolverContractError(
            CODE_VERSION_INCOMPATIBLE,
            f"评估器缺少兼容层要求的符号：{'、'.join(missing)}",
            path=rel, field="、".join(missing), hint=EVALUATOR_INTERFACE_HINT,
        ))
    missing = sorted(EVALUATOR_MODULE_CONSTANTS - defined - assigned)
    if missing:
        problems.append(SolverContractError(
            CODE_VERSION_INCOMPATIBLE,
            f"评估器缺少兼容层要求的模块常量：{'、'.join(missing)}",
            path=rel, field="、".join(missing), hint=EVALUATOR_INTERFACE_HINT,
        ))
    if EVALUATOR_CONTEXT_SYMBOL not in assigned:
        problems.append(SolverContractError(
            CODE_CONTEXT_MISSING,
            f"评估器未在模块顶层导出 {EVALUATOR_CONTEXT_SYMBOL}",
            path=rel, field=EVALUATOR_CONTEXT_SYMBOL,
        ))
        return problems, rel
    missing = _evaluator_contract.missing_context_keys(symbols.context)
    if missing:
        problems.append(SolverContractError(
            CODE_CONTEXT_MISSING,
            f"{EVALUATOR_CONTEXT_SYMBOL} 缺少兼容层要求的键：{'、'.join(missing)}",
            path=rel, field=EVALUATOR_CONTEXT_SYMBOL,
            hint=f"按 Consumer 兼容层接口在 {EVALUATOR_CONTEXT_SYMBOL} 中补齐这些键。",
        ))
    return problems, rel


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def registered_problem_family(name: str) -> str:
    """问题族在注册目录中的规范名（别名归一）；未注册返回空串。"""
    for family, entry in PROBLEM_FAMILY_CATALOG.items():
        if name == family or name in entry["aliases"]:
            return family
    return ""


def problem_family_catalog() -> list[dict]:
    """注册目录的对外视图：求解模型面板据此提供可选项与即时提示。"""
    return [
        {"id": family, "label": entry.get("label") or family, "capabilities": list(entry["capabilities"]),
         "aliases": list(entry["aliases"])}
        for family, entry in PROBLEM_FAMILY_CATALOG.items()
    ]


def _check_engine_requirements(requirements: Any, binding_rel: str) -> list[SolverContractError]:
    """问题族与引擎能力须出自注册目录；不支持的能力逐个列出。"""
    prefix = "declaration.engine_requirements"
    name = str(requirements.get("problem_family") or "").strip() if isinstance(requirements, Mapping) else ""
    if not name:
        # consumer 能力目录会静默丢弃没有问题族的声明，导出后求解能力凭空消失
        return [SolverContractError(
            CODE_INVALID, "能力声明缺少 engine_requirements.problem_family",
            path=binding_rel, field=f"{prefix}.problem_family",
        )]
    family = registered_problem_family(name)
    if not family:
        return [SolverContractError(
            CODE_VERSION_INCOMPATIBLE,
            f"问题族 {name} 不在平台注册目录中，支持范围：" + "、".join(PROBLEM_FAMILY_CATALOG),
            path=binding_rel, field=f"{prefix}.problem_family",
        )]
    capabilities = requirements.get("capabilities")
    if capabilities is None:
        return []
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        return [SolverContractError(
            CODE_INVALID, "engine_requirements.capabilities 须是能力名列表",
            path=binding_rel, field=f"{prefix}.capabilities",
        )]
    available = PROBLEM_FAMILY_CATALOG[family]["capabilities"]
    missing = sorted({item.strip() for item in capabilities if item.strip()} - set(available))
    if not missing:
        return []
    return [SolverContractError(
        CODE_VERSION_INCOMPATIBLE,
        f"问题族 {family} 的注册目录缺少能力：{'、'.join(missing)}；可声明的能力："
        + "、".join(available),
        path=binding_rel, field=f"{prefix}.capabilities",
    )]


def _fact_names(facts: Any) -> list[str] | None:
    """required_facts 去首尾空白后的事实名；不是不重复的非空字符串列表时返回 None。"""
    if not isinstance(facts, list):
        return None
    names = [item.strip() for item in facts if isinstance(item, str)]
    if len(names) != len(facts) or not all(names):
        return None
    return names if len(set(names)) == len(names) else None


def _check_ordered_route_model(model: Mapping, model_rel: str) -> list[SolverContractError]:
    """ordered_binary_route.v1 的事实声明、词典序目标与选项显示名（consumer 运行时据此 fail closed）。"""
    profile = SUPPORTED_PROFILES[0]
    problems: list[SolverContractError] = []
    names = _fact_names(model.get("required_facts"))
    if names is None:
        problems.append(SolverContractError(
            CODE_INVALID, "required_facts 须是不重复的事实名列表",
            path=model_rel, field="required_facts",
        ))
    else:
        missing = [name for name in ORDERED_ROUTE_REQUIRED_FACTS if name not in names]
        extra = sorted(set(names) - set(ORDERED_ROUTE_REQUIRED_FACTS))
        if missing:
            problems.append(SolverContractError(
                CODE_INVALID, f"Profile {profile} 要求 required_facts 声明：{'、'.join(missing)}",
                path=model_rel, field="required_facts",
            ))
        if extra:
            problems.append(SolverContractError(
                CODE_VERSION_INCOMPATIBLE,
                f"Profile {profile} 的事实提供方不产出 {'、'.join(extra)}，支持范围："
                + "、".join(ORDERED_ROUTE_REQUIRED_FACTS),
                path=model_rel, field="required_facts",
            ))
    objectives = model.get("objectives")
    if not isinstance(objectives, list) or [
        dict(item) if isinstance(item, Mapping) else item for item in objectives
    ] != list(ORDERED_ROUTE_OBJECTIVES):
        problems.append(SolverContractError(
            CODE_INVALID,
            f"Profile {profile} 的 objectives 须按词典序基线声明，不可增删或调序："
            + " → ".join(f"{item['metric']} {item['sense']}" for item in ORDERED_ROUTE_OBJECTIVES),
            path=model_rel, field="objectives",
        ))
    if "text_presentation" in model:
        problems += _check_text_presentation(model["text_presentation"], model_rel)
    mapping = model.get("display_mapping")
    if not isinstance(mapping, Mapping) or set(mapping) != set(ORDERED_ROUTE_OPTIONS):
        problems.append(SolverContractError(
            CODE_INVALID,
            "display_mapping 须且只须为 " + "、".join(ORDERED_ROUTE_OPTIONS) + " 各给一个显示名称",
            path=model_rel, field="display_mapping",
        ))
        return problems
    labels = [mapping[option] for option in ORDERED_ROUTE_OPTIONS]
    if not all(isinstance(label, str) and label.strip() for label in labels):
        problems.append(SolverContractError(
            CODE_INVALID, "display_mapping 的显示名称不能为空",
            path=model_rel, field="display_mapping",
        ))
    elif len({label.strip() for label in labels}) != len(labels):
        problems.append(SolverContractError(
            CODE_INVALID, "display_mapping 中两个选项的显示名称相同，结果无法区分",
            path=model_rel, field="display_mapping",
        ))
    elif any(
        len(label.strip()) > MAX_OPTION_LABEL_LENGTH or _CONTROL_CHARS_RE.search(label)
        for label in labels
    ):
        problems.append(SolverContractError(
            CODE_INVALID,
            f"display_mapping 的显示名称不能超过 {MAX_OPTION_LABEL_LENGTH} 个字符，也不能含换行等控制字符",
            path=model_rel, field="display_mapping",
        ))
    return problems


def _check_text_presentation(value: Any, model_rel: str) -> list[SolverContractError]:
    """consumer 渲染结果文字前的同口径检查：它不合法时 consumer 拒收整个求解结果，不只是少一段文字。"""
    if (
        not isinstance(value, Mapping) or set(value) != {"kind", *TEXT_PRESENTATION_FIELDS}
        or value.get("kind") != TEXT_PRESENTATION_KIND
    ):
        return [SolverContractError(
            CODE_INVALID,
            f"text_presentation 须为 {{kind: {TEXT_PRESENTATION_KIND}, process_name, stream_name}}，不允许其他字段",
            path=model_rel, field="text_presentation",
        )]
    names = [value[name] for name in TEXT_PRESENTATION_FIELDS]
    if not all(isinstance(name, str) and name and name == name.strip() for name in names):
        return [SolverContractError(
            CODE_INVALID, "结果文字的工艺名称与物流名称须同时填写，首尾不能有空白",
            path=model_rel, field="text_presentation",
        )]
    if any(len(name) > MAX_OPTION_LABEL_LENGTH or _CONTROL_CHARS_RE.search(name) for name in names):
        return [SolverContractError(
            CODE_INVALID,
            f"结果文字的工艺名称与物流名称不能超过 {MAX_OPTION_LABEL_LENGTH} 个字符，也不能含换行等控制字符",
            path=model_rel, field="text_presentation",
        )]
    return []


def _binder_schema_problem(node: Mapping, path: str) -> str:
    """consumer SolverInputBinder._validate_schema_node 的同口径检查，返回首个问题（consumer 遇错即停）。"""
    unsupported = sorted(str(key) for key in set(node) - _BINDER_SCHEMA_KEYWORDS)
    if unsupported:
        return f"{path} 使用了消费平台不支持的关键字：{'、'.join(unsupported)}"
    declared = node.get("type")
    types = tuple(declared) if isinstance(declared, list) else (declared,)
    if (
        not declared or len(set(map(str, types))) != len(types)
        or not all(isinstance(item, str) and item in _BINDER_SCHEMA_TYPES for item in types)
    ):
        return f"{path} 须声明受支持的 type"
    if "object" in types:
        properties = node.get("properties")
        if not isinstance(properties, Mapping):
            return f"{path} 须声明 properties"
        if node.get("additionalProperties") is not False:
            return f"{path} 须声明 additionalProperties: false"
        required = node.get("required", [])
        if (
            not isinstance(required, list) or len(set(map(str, required))) != len(required)
            or not all(isinstance(name, str) and name for name in required)
        ):
            return f"{path} 的 required 须是不重复的字段名列表"
        unknown = sorted(set(required) - set(properties))
        if unknown:
            return f"{path} 的 required 引用了未声明的字段：{'、'.join(unknown)}"
        for name, child in properties.items():
            if not isinstance(child, Mapping):
                return f"{path}.{name} 须是映射"
            problem = _binder_schema_problem(child, f"{path}.{name}")
            if problem:
                return problem
    elif any(keyword in node for keyword in ("properties", "required", "additionalProperties")):
        return f"{path} 不是 object，不能使用 properties、required 或 additionalProperties"
    if "array" in types:
        items = node.get("items")
        if not isinstance(items, Mapping):
            return f"{path} 须用 items 声明元素结构"
        return _binder_schema_problem(items, f"{path}[]")
    if "items" in node:
        return f"{path} 不是 array，不能使用 items"
    return ""


def _check_ordered_route_input(schema: Mapping, input_rel: str) -> list[SolverContractError]:
    """输入 Schema 须能被 consumer 绑定器接受，并声明适配器与事实 worker 读取的字段。"""
    problem = (
        "顶层 type 须为 object" if schema.get("type") != "object" else _binder_schema_problem(schema, "$")
    )
    if problem:
        return [SolverContractError(CODE_INVALID, f"输入 Schema 不被消费平台接受：{problem}", path=input_rel)]
    properties = schema["properties"]
    missing = [name for name in ORDERED_ROUTE_INPUT_FIELDS if name not in properties]
    items = properties.get("items")
    element = items.get("items") if isinstance(items, Mapping) and items.get("type") == "array" else None
    if "items" in properties and not (isinstance(element, Mapping) and element.get("type") == "object"):
        return [SolverContractError(
            CODE_INVALID, "输入字段 items 须是对象数组（每个元素一个批次）", path=input_rel, field="items",
        )]
    if element is not None:
        fields = element["properties"]
        missing += [
            f"items[].{names[0]}" for names in ORDERED_ROUTE_ITEM_FIELDS
            if not any(name in fields for name in names)
        ]
    if not missing:
        return []
    return [SolverContractError(
        CODE_INVALID,
        f"Profile {SUPPORTED_PROFILES[0]} 的输入 Schema 缺少字段：{'、'.join(missing)}",
        path=input_rel, field="、".join(missing),
    )]


def _is_external_snapshot(fact_source: Any) -> bool:
    """fact_source 是否为 {kind: external_snapshot, fail_closed: true}，且只多出可选的 evaluator_path。"""
    if not isinstance(fact_source, Mapping) or not set(fact_source) <= FACT_SOURCE_KEYS:
        return False
    return fact_source.get("kind") == "external_snapshot" and fact_source.get("fail_closed") is True


def _check_contract(
    root: Path, contract: WorkspaceSolverContract, scripts: Mapping[str, Path]
) -> tuple[list[SolverContractError], str]:
    rel_dir = f"{SOLVER_DIR}/{contract.solver_key}"
    model_rel = f"{rel_dir}/{SIDECAR_BY_ROLE['entry'].filename}"
    binding_rel = f"{rel_dir}/{BINDING_FILE}"
    declaration = contract.binding.get("declaration") or {}
    problems = _check_engine_requirements(declaration.get("engine_requirements"), binding_rel)
    model = parse_sidecar(contract.sidecars["entry"], "model.yaml", model_rel)
    profile = str(model.get("profile_id") or "").strip()
    if profile not in SUPPORTED_PROFILES:
        problems.append(SolverContractError(
            CODE_VERSION_INCOMPATIBLE,
            f"Decision Model Profile {profile or '（未声明）'} 不受支持，支持范围："
            + "、".join(SUPPORTED_PROFILES),
            path=model_rel, field="profile_id",
        ))
        return problems, ""
    problems += _check_ordered_route_model(model, model_rel)
    input_rel = f"{rel_dir}/{SIDECAR_BY_ROLE['input_schema'].filename}"
    problems += _check_ordered_route_input(
        parse_sidecar(contract.sidecars["input_schema"], "input.schema.json", input_rel), input_rel
    )
    fact_source = model.get("fact_source")
    if not _is_external_snapshot(fact_source):
        problems.append(SolverContractError(
            CODE_INVALID,
            "fact_source 须为 {kind: external_snapshot, fail_closed: true}，"
            "可选 evaluator_path，不允许其他字段",
            path=model_rel, field="fact_source",
        ))
        return problems, ""
    evaluator_problems, evaluator = _check_evaluator(fact_source, scripts, root, model_rel)
    return problems + evaluator_problems, evaluator


def _check_contracts(
    root: Path, contracts: Iterable[WorkspaceSolverContract], scripts: Mapping[str, Path]
) -> tuple[list[dict], dict[str, str]]:
    """已读出契约的语义检查：(findings, solver_key -> 工作区评估器路径)。"""
    findings: list[dict] = []
    evaluators: dict[str, str] = {}
    for contract in contracts:
        try:
            contract_problems, evaluator = _check_contract(root, contract, scripts)
        except SolverContractError as exc:
            contract_problems, evaluator = [exc], ""
        findings.extend(finding(problem, contract.solver_key) for problem in contract_problems)
        if evaluator:
            evaluators[contract.solver_key] = evaluator
    return findings, evaluators


def check_workspace_solver(
    root: Path, operation_stems: Iterable[str], scripts: Mapping[str, Path]
) -> SolverCheck:
    """工作区求解契约的静态检查。

    scripts 是包内脚本路径（scripts/<文件名>）→ 工作区脚本文件，由调用方按导出时的
    文件名规则给出，评估器路径的解析口径因此与导出包一致。
    """
    root = Path(root)
    contracts, problems = load_workspace_solver(root, list(operation_stems))
    findings, evaluators = _check_contracts(root, contracts, scripts)
    findings = [finding(problem) for problem in problems] + findings
    check = SolverCheck(contracts, findings, evaluators=evaluators)
    if check.enabled:
        check.revision = _revision(root, contracts, evaluators.values())
        check.digests = contract_digests(root, contracts, evaluators)
    return check


def _problem_solver_key(problem: SolverContractError) -> str:
    parts = problem.path.split("/")
    prefix = SOLVER_DIR.split("/")
    return parts[len(prefix)] if parts[: len(prefix)] == prefix and len(parts) > len(prefix) else ""


def _operation_doc(root: Path, contract: WorkspaceSolverContract) -> Path | None:
    for directory in ("ontology/operation", "ontology/logic"):
        path = root / directory / f"{contract.operation}.md"
        if path.is_file():
            return path
    return None


def _revision(root: Path, contracts: list[WorkspaceSolverContract], evaluators: Iterable[str]) -> str:
    """求解契约修订摘要：契约目录全部文件、被绑定的 Operation 文档与评估器源码。

    只覆盖会改变求解行为的内容：改模型、Schema、绑定或评估器立即使已有验证失效；
    对象文档只有被声明式评估器的 TABLE_SOURCES 引用时才计入，旧式评估器下改对象
    文档不使验证失效。
    """
    paths: set[str] = set()
    base = root / SOLVER_DIR
    if base.is_dir():
        paths.update(
            _rel(root, path) for path in base.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    for contract in contracts:
        path = _operation_doc(root, contract)
        if path is not None:
            paths.add(_rel(root, path))
    evaluators = set(evaluators)
    paths.update(evaluators)
    declared: set[str] = set()
    for rel in evaluators:
        declared.update(_evaluator_contract.declared_objects(_read_evaluator(root / rel)))
    object_dir = root / "ontology" / "object"
    if declared and object_dir.is_dir():
        # 按目录枚举再比对文件名，对象 key 不拼进路径
        paths.update(
            _rel(root, doc) for doc in object_dir.glob("*.md")
            if doc.stem in declared and doc.name != "index.md" and doc.is_file()
        )
    digest = hashlib.sha256()
    for rel in sorted(paths):
        data = (root / rel).read_bytes()
        digest.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data)
    return "sha256:" + digest.hexdigest()


# 规范化契约摘要的逐项名称；sidecar 按角色映射，entry 即 Decision Model
_DIGEST_ITEM_BY_ROLE = {"entry": "decision_model"}


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def canonical_digest(value: Any) -> str:
    """键排序、紧凑分隔的 UTF-8 JSON 的 sha256：只改缩进、键序或 YAML/JSON 写法不改变摘要。"""
    text = json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized_text(text: str) -> str:
    """统一换行、去掉行尾空白与首尾空行，纯排版差异不进入摘要。"""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _sidecar_document(data: bytes, filename: str) -> Any:
    try:
        text = data.decode("utf-8")
        return json.loads(text) if filename.endswith(".json") else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError):  # ValueError 已涵盖 UnicodeDecodeError 与 JSONDecodeError
        return {"unparsed": _normalized_text(data.decode("utf-8", "replace"))}


def _operation_document(root: Path, contract: WorkspaceSolverContract) -> dict:
    """求解 Operation 的发布语义：身份、执行声明、工作区文档的 frontmatter 与正文。"""
    path = _operation_doc(root, contract)
    text = path.read_bytes().decode("utf-8", "replace") if path is not None else ""
    match = _FRONTMATTER_RE.match(text)
    return {
        "key": contract.solver_key,
        "decision_model": contract.decision_model,
        "frontmatter": contract.binding.get("operation_frontmatter") or {},
        "document": {
            "frontmatter": read_frontmatter(text) or {},
            "body": _normalized_text(text[match.end():] if match else text),
        },
    }


def contract_digests(
    root: Path, contracts: Iterable[WorkspaceSolverContract], evaluators: Mapping[str, str]
) -> dict:
    """求解契约的规范化摘要（REQ-PKG-006）：逐项、逐契约与总摘要。

    各项按发布形态取值（输入 Schema 含导出时的可选值投影），解析后规范化再计算，
    同一内容总得到同一摘要；任一项语义变化同时改变该项、所在契约与总摘要。
    package 项是知识包格式版本（schema_version 与布局），不含每次发布递增的包版本号。
    摘要只在建模侧展示与留证，不写进知识包。
    """
    package = canonical_digest({"schema_version": "0.2", "solver_layout": OPERATION_CENTRIC_LAYOUT})
    operations: dict[str, dict[str, str]] = {}
    for contract in sorted(contracts, key=lambda item: item.solver_key):
        evaluator_rel = evaluators.get(contract.solver_key, "")
        evaluator_path = root / evaluator_rel if evaluator_rel else None
        items = {"operation": canonical_digest(_operation_document(root, contract))}
        for sidecar in SIDECARS:
            data = contract.sidecars[sidecar.role]
            if sidecar.role == "input_schema":
                data = project_input_schema(data, evaluator_path)
            items[_DIGEST_ITEM_BY_ROLE.get(sidecar.role, sidecar.role)] = canonical_digest(
                _sidecar_document(data, sidecar.filename)
            )
        # Policy：随 manifest 声明发布的扩展字段（引擎要求、启用状态等）
        items["policy"] = canonical_digest(contract.binding.get("declaration") or {})
        source = _read_evaluator(evaluator_path) if evaluator_path is not None else None
        items["evaluator"] = canonical_digest(
            _normalized_text(source.decode("utf-8", "replace")) if source is not None else None
        )
        operations[contract.solver_key] = {**items, "total": canonical_digest(items)}
    return {
        "algorithm": "sha256:canonical-json",
        "package": package,
        "operations": operations,
        "total": canonical_digest({
            "package": package,
            "operations": {key: item["total"] for key, item in operations.items()},
        }),
    }


# ---- 结构化读写（求解模型面板）----

# Profile 模板：业务用户只填模板参数，五份 sidecar 由服务端按 Profile 确定性生成
# （同一参数总是同一字节，保存后可按字节判断契约是否仍是模板产物）
_JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _text_field(max_length: int, description: str, min_length: int = 1) -> dict:
    return {"type": "string", "minLength": min_length, "maxLength": max_length, "description": description}


ORDERED_ROUTE_INPUT_SCHEMA = {
    "$schema": _JSON_SCHEMA_DIALECT,
    "type": "object",
    "additionalProperties": False,
    "required": list(ORDERED_ROUTE_INPUT_FIELDS),
    "properties": {
        "plan_month": _text_field(7, "计划月份，格式 YYYY-MM", min_length=7),
        "diesel_unit_no": _text_field(32, "装置编号，须是评估器支持的装置"),
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "description": "按加工顺序排列的批次",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [names[0] for names in ORDERED_ROUTE_ITEM_FIELDS],
                "properties": {
                    "item_order": {
                        "type": "integer", "minimum": 1, "maximum": 64, "description": "加工顺序，从 1 开始连续编号",
                    },
                    "item_id": _text_field(64, "批次号"),
                    "crude_origin": _text_field(64, "原油品种"),
                    "quantity_wan_tons": _text_field(32, "批次量（万吨）"),
                    "process_days": _text_field(32, "加工天数"),
                },
            },
        },
    },
}
# 结果的每个批次字段取值为不透明选项标识，显示名称由 Decision Model 的 display_mapping 给出
ORDERED_ROUTE_RESULT_SCHEMA = {
    "$schema": _JSON_SCHEMA_DIALECT,
    "type": "object",
    "required": ["candidate_id"],
    "properties": {"candidate_id": {"type": "string", "minLength": 1}},
    "additionalProperties": {"type": "string", "enum": list(ORDERED_ROUTE_OPTIONS)},
}
ORDERED_ROUTE_FACT_SNAPSHOT_SCHEMA = {
    "$schema": _JSON_SCHEMA_DIALECT,
    "type": "object",
    "properties": {"facts": {"type": "array"}},
}
TEMPLATE_PARAM_KEYS = ("display_mapping", "title", "evaluator_path", *TEXT_PRESENTATION_FIELDS)
MAX_TEMPLATE_TITLE_LENGTH = 128

# 「添加求解契约」的起始参数：声明扩展字段固定，模板参数（选项显示名称等）由用户填写
NEW_CONTRACT_TEMPLATE = {
    "declaration": {"engine_requirements": {
        "problem_family": "candidate_selection.v1",
        "capabilities": ["domain_candidate_evaluation", "lexicographic_objective"],
    }},
    "params": {
        "display_mapping": {option: "" for option in ORDERED_ROUTE_OPTIONS},
        "title": "",
        "evaluator_path": "",
        **{name: "" for name in TEXT_PRESENTATION_FIELDS},
    },
}


def _dump_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def render_template(params: Mapping) -> dict[str, bytes]:
    """规范化后的模板参数 → 五份 sidecar 字节（role → 字节）。"""
    mapping = params.get("display_mapping") or {}
    fact_source: dict[str, Any] = {"kind": "external_snapshot", "fail_closed": True}
    if params.get("evaluator_path"):
        fact_source["evaluator_path"] = params["evaluator_path"]
    model = {
        "profile_id": SUPPORTED_PROFILES[0],
        "required_facts": list(ORDERED_ROUTE_REQUIRED_FACTS),
        "objectives": [dict(item) for item in ORDERED_ROUTE_OBJECTIVES],
        "display_mapping": {option: mapping.get(option, "") for option in ORDERED_ROUTE_OPTIONS},
        "fact_source": fact_source,
    }
    text = {name: params.get(name) or "" for name in TEXT_PRESENTATION_FIELDS}
    if any(text.values()):
        # 两项都留空时不写，产物与加入这两个参数之前逐字节一致，已保存的契约仍受管
        model["text_presentation"] = {"kind": TEXT_PRESENTATION_KIND, **text}
    title = params.get("title") or ""
    return {
        "entry": _dump_yaml(model),
        "input_schema": _dump_json(ORDERED_ROUTE_INPUT_SCHEMA),
        "result_schema": _dump_json(ORDERED_ROUTE_RESULT_SCHEMA),
        "fact_snapshot_schema": _dump_json(ORDERED_ROUTE_FACT_SNAPSHOT_SCHEMA),
        "presentation": _dump_yaml({"title": title} if title else {}),
    }


def normalize_template_params(raw: Any, rel_dir: str) -> tuple[dict, list[SolverContractError]]:
    """面板提交的模板参数 → 规范化参数（去首尾空白）；字段或类型不对时保存直接拒收。

    显示名称为空、重名等语义问题不在这里拒收，由静态检查给出，与手动编辑的口径一致。
    """
    model_rel = f"{rel_dir}/{SIDECAR_BY_ROLE['entry'].filename}"
    presentation_rel = f"{rel_dir}/{SIDECAR_BY_ROLE['presentation'].filename}"
    if not isinstance(raw, Mapping):
        return {}, [SolverContractError(CODE_INVALID, "模板参数必须是映射", path=model_rel, field="template")]
    problems: list[SolverContractError] = []
    unknown = sorted(str(key) for key in set(raw) - set(TEMPLATE_PARAM_KEYS))
    if unknown:
        problems.append(SolverContractError(
            CODE_INVALID, f"模板参数不支持字段：{'、'.join(unknown)}", path=model_rel, field="template",
        ))
    mapping = raw.get("display_mapping")
    mapping = {} if mapping is None else mapping
    params: dict[str, Any] = {"display_mapping": {}}
    if not isinstance(mapping, Mapping) or not set(mapping) <= set(ORDERED_ROUTE_OPTIONS):
        problems.append(SolverContractError(
            CODE_INVALID, "display_mapping 只能包含 " + "、".join(ORDERED_ROUTE_OPTIONS),
            path=model_rel, field="display_mapping",
        ))
    else:
        for option in ORDERED_ROUTE_OPTIONS:
            label = mapping.get(option, "")
            if not isinstance(label, str):
                problems.append(SolverContractError(
                    CODE_INVALID, f"{option} 的显示名称必须是文本", path=model_rel, field=f"display_mapping.{option}",
                ))
            else:
                params["display_mapping"][option] = label.strip()
    for name, rel in (
        ("title", presentation_rel), ("evaluator_path", model_rel),
        *((field, model_rel) for field in TEXT_PRESENTATION_FIELDS),
    ):
        value = raw.get(name, "")
        value = "" if value is None else value
        if not isinstance(value, str):
            problems.append(SolverContractError(CODE_INVALID, f"{name} 必须是文本", path=rel, field=name))
            continue
        params[name] = value.strip()
    title = params.get("title", "")
    if len(title) > MAX_TEMPLATE_TITLE_LENGTH or _CONTROL_CHARS_RE.search(title):
        problems.append(SolverContractError(
            CODE_INVALID, f"标题不能超过 {MAX_TEMPLATE_TITLE_LENGTH} 个字符，也不能含换行等控制字符",
            path=presentation_rel, field="title",
        ))
    path = params.get("evaluator_path", "")
    if path and _safe_package_path(path) != path:
        problems.append(SolverContractError(
            CODE_INVALID, f"评估器路径须是包内相对路径：{path}", path=model_rel, field="fact_source.evaluator_path",
        ))
    return params, problems


def extract_template_params(sidecars: Mapping[str, bytes]) -> dict:
    """从已保存的 sidecar 尽量还原模板参数：手动编辑过的契约按模板重新生成时沿用。"""
    params: dict[str, Any] = {
        "display_mapping": {option: "" for option in ORDERED_ROUTE_OPTIONS},
        "title": "",
        "evaluator_path": "",
        **{name: "" for name in TEXT_PRESENTATION_FIELDS},
    }

    def load(role: str) -> Mapping:
        try:
            value = yaml.safe_load(sidecars.get(role, b"").decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            return {}
        return value if isinstance(value, Mapping) else {}

    model = load("entry")
    mapping = model.get("display_mapping")
    if isinstance(mapping, Mapping):
        for option in ORDERED_ROUTE_OPTIONS:
            if isinstance(mapping.get(option), str):
                params["display_mapping"][option] = mapping[option].strip()
    fact_source = model.get("fact_source")
    if isinstance(fact_source, Mapping) and isinstance(fact_source.get("evaluator_path"), str):
        params["evaluator_path"] = fact_source["evaluator_path"].strip()
    text = model.get("text_presentation")
    if isinstance(text, Mapping):
        for name in TEXT_PRESENTATION_FIELDS:
            if isinstance(text.get(name), str):
                params[name] = text[name].strip()
    title = load("presentation").get("title")
    if isinstance(title, str):
        params["title"] = title.strip()
    return params


def _evaluator_dict_keys(path: Path | None, symbol: str) -> list[str]:
    """评估器顶层 symbol 字典字面量的字符串键（只解析不执行）；取不到返回空列表。"""
    if path is None:
        return []
    try:
        data = path.read_bytes()
        if not data or len(data) > MAX_EVALUATOR_BYTES:
            return []
        module = ast.parse(data.decode("utf-8"), filename=path.name)
    except (OSError, SyntaxError, ValueError):  # UnicodeDecodeError 是 ValueError 的子类
        return []
    options: list[str] = []
    for node in module.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(isinstance(target, ast.Name) and target.id == symbol for target in targets):
            value = node.value
            options = [
                key.value for key in value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ] if isinstance(value, ast.Dict) else []
    return list(dict.fromkeys(options))


def evaluator_unit_options(path: Path | None) -> list[str]:
    """评估器 DIESEL_UNIT_IDS 的键：consumer 事实 worker 只接受这些装置编号，面板据此给出下拉选项。"""
    return _evaluator_dict_keys(path, "DIESEL_UNIT_IDS")


def evaluator_source_options(path: Path | None) -> list[str]:
    """评估器 CORRECTION_FACTORS 的键：没有修正系数的原油品种，consumer 取数时拒绝。"""
    return _evaluator_dict_keys(path, "CORRECTION_FACTORS")


# 受管模板的输入 Schema 在导出时按评估器补上可选值，让 consumer 的 Agent 填参前就知道取值范围；
# 工作区文件保持模板原样（受管判定与修订摘要都按工作区字节），回导时再还原。
MAX_PROJECTED_OPTIONS = 64
_SOURCE_OPTIONS_PREFIX = "，须是评估器有修正系数的品种之一："
# enum 只限定取值，Agent 遇到可选值之外的装置会就近挑一个照样求解；描述里要求它先问用户
_UNIT_CONFIRM_SUFFIX = "；用户给出的装置与可选值对应不上时，不要改用其他装置，先向用户确认"
_SOURCE_CONFIRM_SUFFIX = "；用户给出的品种与这些品种对应不上时，不要改用其他品种，先向用户确认"
_TEMPLATE_INPUT_SCHEMA = _dump_json(ORDERED_ROUTE_INPUT_SCHEMA)
_PROJECTION_ID_PREFIX = "urn:onto-modeling:input-schema-projection:sha256:"


def _crude_field(schema: Mapping) -> dict:
    return schema["properties"]["items"]["items"]["properties"]["crude_origin"]


def _projected_input_schema(units: list[str], sources: list[str]) -> dict:
    schema = copy.deepcopy(ORDERED_ROUTE_INPUT_SCHEMA)
    if 0 < len(units) <= MAX_PROJECTED_OPTIONS:
        unit_field = schema["properties"]["diesel_unit_no"]
        unit_field["enum"] = list(units)
        unit_field["description"] += _UNIT_CONFIRM_SUFFIX
    if 0 < len(sources) <= MAX_PROJECTED_OPTIONS and not any("、" in source for source in sources):
        # 原油品种只写进描述不做 enum：consumer 按规范化名称（去空白、全半角统一）匹配，
        # enum 是逐字比较，会把等价写法拒在门外
        _crude_field(schema)["description"] += (
            _SOURCE_OPTIONS_PREFIX + "、".join(sources) + _SOURCE_CONFIRM_SUFFIX
        )
    # A projected schema can be edited into another plausible projection.  Bind
    # its generated content to an ID so imports never erase such edits.
    schema["$id"] = _PROJECTION_ID_PREFIX + hashlib.sha256(_dump_json(schema)).hexdigest()
    return schema


def project_input_schema(data: bytes, evaluator: Path | None) -> bytes:
    """导出用的输入 Schema：受管模板按评估器补可选值，手写的 Schema 原样返回。"""
    if data != _TEMPLATE_INPUT_SCHEMA:
        return data
    units = evaluator_unit_options(evaluator)
    sources = evaluator_source_options(evaluator)
    if not units and not sources:
        return data
    return _dump_json(_projected_input_schema(
        units, sources,
    ))


def restore_template_input_schema(data: bytes) -> bytes:
    """导入时把 project_input_schema 的产物还原成模板字节，其他内容原样返回。"""
    try:
        schema = json.loads(data.decode("utf-8"))
        projection_id = schema.get("$id")
        if not isinstance(projection_id, str) or not projection_id.startswith(_PROJECTION_ID_PREFIX):
            return data
        unsigned = dict(schema)
        unsigned.pop("$id")
        if projection_id != _PROJECTION_ID_PREFIX + hashlib.sha256(_dump_json(unsigned)).hexdigest():
            return data
        units = schema["properties"]["diesel_unit_no"].get("enum", [])
        description = _crude_field(schema)["description"]
        if not isinstance(units, list) or not all(isinstance(unit, str) for unit in units):
            return data
        prefix = _crude_field(ORDERED_ROUTE_INPUT_SCHEMA)["description"] + _SOURCE_OPTIONS_PREFIX
        projected = description.startswith(prefix) and description.endswith(_SOURCE_CONFIRM_SUFFIX)
        sources = description[len(prefix):-len(_SOURCE_CONFIRM_SUFFIX)].split("、") if projected else []
    except (ValueError, LookupError, TypeError, AttributeError):  # ValueError 已涵盖 UnicodeDecodeError 与 JSONDecodeError
        return data
    return _TEMPLATE_INPUT_SCHEMA if _dump_json(_projected_input_schema(units, sources)) == data else data


class SolverContractRejected(ValueError):
    """保存的求解契约未通过结构校验；problems 逐条定位到字段。"""

    def __init__(self, problems: list[SolverContractError]):
        super().__init__("；".join(str(problem) for problem in problems[:3]))
        self.problems = problems


def contract_view(contract: WorkspaceSolverContract) -> dict:
    """面板读取的契约：binding 全量（含未知扩展字段）+ sidecar 原文 + 还原的模板参数。

    managed 表示五份 sidecar 与按还原参数重新生成的结果逐字节一致，面板据此用表单展示。
    还原参数用 template_params 而不是提交用的 template 字段名，原样回传视图不会触发重新生成。
    """
    params = extract_template_params(contract.sidecars)
    return {
        "solver_key": contract.solver_key,
        "operation": contract.operation,
        "decision_model": contract.decision_model,
        "enabled": contract.enabled,
        "binding": contract.binding,
        "sidecars": {
            role: data.decode("utf-8", "replace") for role, data in contract.sidecars.items()
        },
        "managed": render_template(params) == contract.sidecars,
        "template_params": params,
    }


def same_content(filename: str, existing: bytes, staged: bytes) -> bool:
    """sidecar 按字节比较；binding 由服务端重新序列化，按解析结果比较。"""
    if existing == staged:
        return True
    if filename != BINDING_FILE:
        return False
    try:
        return yaml.safe_load(existing) == yaml.safe_load(staged)
    except yaml.YAMLError:
        return False


def _stage_files(
    items: Iterable[Mapping],
) -> tuple[dict[str, dict[str, bytes]], list[SolverContractError], list[dict[str, str] | None]]:
    """面板提交的契约 → (各 solver_key 目录下的文件字节, 问题, 每条模板契约生成的 sidecar 原文)。

    条目带 template 时按模板生成全部 sidecar（忽略同时提交的 sidecars），否则按原文保存；
    solver_key 或模板参数不可用的条目只记问题。
    """
    staged: dict[str, dict[str, bytes]] = {}
    problems: list[SolverContractError] = []
    rendered: list[dict[str, str] | None] = []
    for index, item in enumerate(items):
        rendered.append(None)
        item = item if isinstance(item, Mapping) else {}
        binding, template, sidecars = item.get("binding"), item.get("template"), item.get("sidecars")
        if not isinstance(binding, Mapping) or (template is None and not isinstance(sidecars, Mapping)):
            problems.append(SolverContractError(
                CODE_INVALID, f"第 {index + 1} 个求解契约缺少 binding 或 sidecars", field=f"operations[{index}]"
            ))
            continue
        solver_key = str(binding.get("solver_key") or "").strip()
        if not SOLVER_KEY_RE.fullmatch(solver_key):
            problems.append(SolverContractError(
                CODE_INVALID, f"solver_key {solver_key!r} 须满足 ^[a-z][a-z0-9_]{{0,31}}$",
                path=f"{SOLVER_DIR}/{solver_key}/{BINDING_FILE}", field="solver_key",
            ))
            continue
        if solver_key in staged:
            problems.append(SolverContractError(
                CODE_INVALID, f"solver_key 重复：{solver_key}",
                path=f"{SOLVER_DIR}/{solver_key}/{BINDING_FILE}", field="solver_key",
            ))
            continue
        binding = dict(binding)
        if template is not None:
            params, template_problems = normalize_template_params(template, f"{SOLVER_DIR}/{solver_key}")
            if template_problems:
                problems += template_problems
                continue
            data = render_template(params)
            rendered[index] = {role: value.decode("utf-8") for role, value in data.items()}
        else:
            data = {
                sidecar.role: sidecars[sidecar.role].encode("utf-8")
                for sidecar in SIDECARS if isinstance(sidecars.get(sidecar.role), str)
            }
        if not str(binding.get("decision_model") or "").strip():
            # 面板不再让用户填 Decision Model ID：取 Decision Model 文档里的 id，没有时按新建契约的默认命名补上（与前端一致）
            binding["decision_model"] = _model_document_id(data) or f"{solver_key}_model"
        files = {BINDING_FILE: _dump_yaml(dict(binding))}
        files.update({SIDECAR_BY_ROLE[role].filename: value for role, value in data.items()})
        staged[solver_key] = files
    return staged, problems, rendered


def _model_document_id(data: Mapping[str, bytes]) -> str:
    entry = data.get("entry")
    if entry is None:
        return ""
    try:
        document = parse_sidecar(entry, SIDECAR_BY_ROLE["entry"].filename, SIDECAR_BY_ROLE["entry"].filename)
    except SolverContractError:
        return ""
    return str(document.get("id") or "").strip() if isinstance(document, Mapping) else ""


def _load_staged(
    staged: Mapping[str, Mapping[str, bytes]], operation_stems: Iterable[str]
) -> tuple[list[WorkspaceSolverContract], list[SolverContractError]]:
    """在临时目录落一遍，用与导出相同的 load_workspace_solver 读回，保存与发布的口径不会分叉。"""
    with tempfile.TemporaryDirectory(prefix="solver-contract-") as tmp:
        for solver_key, files in staged.items():
            directory = Path(tmp) / SOLVER_DIR / solver_key
            directory.mkdir(parents=True)
            for filename, data in files.items():
                (directory / filename).write_bytes(data)
        return load_workspace_solver(Path(tmp), operation_stems)


def stage_contracts(items: Iterable[Mapping], operation_stems: Iterable[str]) -> dict[str, dict[str, bytes]]:
    """面板提交的契约 → 各 solver_key 目录下的文件字节；结构不合法抛 SolverContractRejected。"""
    staged, problems, _rendered = _stage_files(items)
    if problems:
        raise SolverContractRejected(problems)
    _contracts, problems = _load_staged(staged, operation_stems)
    if problems:
        raise SolverContractRejected(problems)
    return staged


def evaluator_summary(path: Path | None) -> dict | None:
    """评估器的种类与约束数（契约 SV-01 evaluator.kind、SV-03 evaluator）：只解析不执行。

    声明式评估器给 declarative，旧式（自带计算的脚本）给 script；约束数是声明的日处理能力上限
    （RULE_DAILY_CAPACITIES）条数。读不到或不是评估器返回 None。
    """
    source = _read_evaluator(path) if path is not None else None
    kind = _evaluator_contract.evaluator_kind(source)
    if kind is None:
        return None
    return {
        "kind": "declarative" if kind == _evaluator_contract.DECLARATIVE_KIND else "script",
        "constraints": len(_evaluator_dict_keys(path, "RULE_DAILY_CAPACITIES")),
    }


def draft_evaluators(
    items: Iterable[Mapping], operation_stems: Iterable[str], root: Path, scripts: Mapping[str, Path]
) -> dict[str, str]:
    """面板草稿里各求解契约按发现规则找到的评估器（solver_key → 工作区路径），不落盘。"""
    staged, _problems, _rendered = _stage_files(items)
    contracts, _load_problems = _load_staged(staged, operation_stems)
    return _check_contracts(Path(root), contracts, scripts)[1]


def check_draft(
    items: Iterable[Mapping], operation_stems: Iterable[str], root: Path, scripts: Mapping[str, Path]
) -> tuple[list[dict], list[dict], list[dict[str, str] | None]]:
    """面板草稿的即时检查，不落盘：(保存会被拒的结构问题, 发布会被拦的问题, 模板生成的 sidecar)。

    结构问题与 stage_contracts 同源，其余与 check_workspace_solver 同源；能读出的契约
    照常做语义检查，一份草稿里的结构错误不掩盖其他契约的问题。生成结果按提交顺序
    一一对应，非模板条目与参数不合法的条目为 None。
    """
    staged, problems, rendered = _stage_files(items)
    contracts, load_problems = _load_staged(staged, operation_stems)
    findings, _evaluators = _check_contracts(Path(root), contracts, scripts)
    return [finding(problem) for problem in problems + load_problems], findings, rendered
