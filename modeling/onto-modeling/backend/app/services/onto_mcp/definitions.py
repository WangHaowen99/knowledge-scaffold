"""Public tool contract compatible with Modeling; persistence belongs to Onto."""
from .errors import McpError

SCOPES = (
    "ontology.read",
    "ontology.change.validate",
    "ontology.change.apply",
    "project.version.revert",
    "package.publish",
)
KEY_PATTERN = r"^[^/\\#?&|\r\n\t]{1,80}$"
TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
RECORDS = {"type": "array", "items": {"type": "object"}}
OPERATION = {"title": TEXT, "description": TEXT, "inputs": RECORDS, "outputs": RECORDS,
             "flow": STRINGS, "related_objects": STRINGS, "uses_rules": STRINGS,
             "uses_metrics": STRINGS, "notes": STRINGS}
RULE = {"title": TEXT, "description": TEXT, "applies_to_objects": STRINGS,
        "applies_to_operations": STRINGS, "content": TEXT, "conditions": RECORDS, "exceptions": STRINGS}


def definition(name, title, props, required=(), access="read"):
    return {"name": name, "title": title, "description": title + "（直接使用 Onto 工作区及本地 revision）",
            "scope": SCOPES[0 if access == "read" else 1 if access == "validate" else 2],
            "toolset": "read" if access == "read" else "change",
            "inputSchema": {"type": "object", "properties": props, "required": list(required),
                            "additionalProperties": False}}


def definitions():
    result = []
    # 版本回退（ADR-0094）：列版本在读工具集，预览与确认回退在改工具集，都只认 project.version.revert
    version_tools = (
        ("list_project_versions", "列出项目版本", {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": TEXT}, (), "read"),
        ("preview_project_version_revert", "预览项目版本回退", {
            "target_version": {"type": "integer", "minimum": 1}}, ("target_version",), "validate"),
        ("revert_project_version", "确认项目版本回退", {
            "preview_id": {"type": "string", "pattern": r"^[0-9a-f]{32}$"}, "confirm": {"type": "boolean"}},
         ("preview_id", "confirm"), "apply"),
    )
    for name, title, props, required, access in version_tools:
        result.append({**definition(name, title, props, required, access), "scope": "project.version.revert"})
    for kind, label in (("object", "对象"), ("operation", "操作"), ("rule", "规则")):
        identity = {"type": "string", "pattern": "^" + kind + ":" + KEY_PATTERN[1:]}
        listing = {"revision": TEXT, "query": TEXT, "cursor": TEXT,
                   "limit": {"type": "integer", "minimum": 1, "maximum": 200}}
        if kind == "object":
            listing.update(sort=TEXT, order={"type": "string", "enum": ["asc", "desc"]})
        result.append(definition(f"list_ontology_{kind}s", f"列出{label}", listing))
        result.append(definition(f"get_ontology_{kind}", f"读取{label}",
                                 {f"{kind}_id": identity, "revision": TEXT}, [f"{kind}_id"]))
        if kind == "object":
            operations = {"type": "object", "properties": {"add": RECORDS, "update": RECORDS,
                          "remove": {"type": "array", "items": {"oneOf": [TEXT, {"type": "object"}]}}},
                          "additionalProperties": False}
            props = {"object_id": identity, "expected_revision": TEXT, "metadata": {"type": "object"},
                     "field_ops": operations, "relation_ops": operations}
            for action in ("validate", "update"):
                name = "validate_ontology_object_change" if action == "validate" else "update_ontology_object"
                result.append(definition(name, f"{action} 对象修改", props,
                                         ["object_id", "expected_revision"], action))
        else:
            props = {"expected_revision": TEXT, "key": {"type": "string", "pattern": KEY_PATTERN},
                     **(OPERATION if kind == "operation" else RULE)}
            required = ["expected_revision", "key", "title"] + (
                ["description", "inputs", "outputs", "flow", "related_objects"] if kind == "operation" else ["content"])
            result.append(definition(f"create_ontology_{kind}", f"新增{label}", props, required, "apply"))
            props = {"expected_revision": TEXT, f"{kind}_id": identity}
            result.append(definition(f"delete_ontology_{kind}", f"删除{label}", props, props.keys(), "apply"))
            result.append(definition(f"update_ontology_{kind}", f"修改{label}",
                                     {**props, "changes": {"type": "object", "minProperties": 1}},
                                     [*props, "changes"], "apply"))
    # 插件产物只读工具（契约 M-08；详设 5.9.4）：结果有上限，要正文按键再取
    artifact_type = {"type": "string", "pattern": r"^[a-z][a-z0-9-]{1,39}\.[a-z0-9_.-]{1,40}$"}
    result.append(definition("artifact_list", "列出插件产物", {
        "type": artifact_type, "filters": {"type": "object"}, "query": TEXT, "stale": {"type": "boolean"},
        "cursor": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["type"]))
    result.append(definition("artifact_get", "读取插件产物", {
        "type": artifact_type, "key": TEXT}, ["type", "key"]))
    result.append(definition("validate_ontology_operation_rule_change", "校验操作或规则修改", {
        "expected_revision": TEXT, "resource_type": {"enum": ["operation", "rule"]},
        "action": {"enum": ["create", "update", "delete"]}, "resource_id": TEXT, "payload": {"type": "object"}},
        ["expected_revision", "resource_type", "action", "resource_id"], "validate"))
    result.extend(package_publish_definitions())
    return result


def package_publish_definitions():
    """发布知识包版本的三个工具（ADR-0091）：预览拿确认令牌 → 确认发布（后台）→ 查状态。"""
    request_id = {"type": "string", "pattern": r"^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,127}$"}
    package_tools = (
        ("preview_package_publish", "预览知识包版本发布", {
            "request_id": request_id, "allow_unbound": {"type": "boolean"},
        }, (), True),
        ("publish_package_version", "确认发布知识包新版本", {
            "request_id": request_id,
            "confirmation_token": {"type": "string", "pattern": r"^[A-Za-z0-9_-]+\.[a-f0-9]{64}$"},
            # 预览返回 solver.requires_acknowledgement 时必填 true：确认求解能力尚未经端到端验证
            "solver_unverified_acknowledged": {"type": "boolean"},
        }, ("request_id", "confirmation_token"), False),
        ("get_package_publish_status", "查询知识包版本发布状态", {
            "job_id": {"type": "string", "pattern": r"^[a-f0-9]{32}$"},
        }, ("job_id",), True),
    )
    return [{
        "name": name, "title": title, "description": title + "（发布当前 MCP 端点绑定的项目）",
        "scope": "package.publish", "toolset": "change", "readOnly": read_only,
        "inputSchema": {"type": "object", "properties": props, "required": list(required),
                        "additionalProperties": False},
    } for name, title, props, required, read_only in package_tools]


TOOLS = {tool["name"]: tool for tool in definitions()}


def validate_input(value, schema, path="arguments"):
    """Validate the small JSON Schema subset used by the published tool contract."""
    import re
    if "oneOf" in schema:
        for candidate in schema["oneOf"]:
            try:
                validate_input(value, candidate, path)
                return
            except McpError:
                pass
        raise McpError("INVALID_INPUT", f"{path} 类型不符合要求")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    kind = schema.get("type")
    if kind in types and not isinstance(value, types[kind]):
        raise McpError("INVALID_INPUT", f"{path} 必须是 {kind}")
    if kind == "integer" and isinstance(value, bool):
        raise McpError("INVALID_INPUT", f"{path} 必须是 {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise McpError("INVALID_INPUT", f"{path} 不在允许值中")
    if kind == "string":
        if len(value) > 100000:
            raise McpError("INVALID_INPUT", f"{path} 格式无效")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise McpError("INVALID_INPUT", f"{path} 格式无效")
    if kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise McpError("INVALID_INPUT", f"{path} 超出范围")
    if kind == "object":
        props = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])) or len(value) < schema.get("minProperties", 0):
            raise McpError("INVALID_INPUT", f"{path} 缺少必填字段")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise McpError("INVALID_INPUT", f"{path} 包含不支持的字段")
        for key, item in value.items():
            if key in props:
                validate_input(item, props[key], f"{path}.{key}")
    if kind == "array":
        if len(value) > 1000:
            raise McpError("INVALID_INPUT", f"{path} 条目过多")
        for item in value:
            validate_input(item, schema.get("items", {}), path)
