"""知识包发布适配器（方案 A8「发布适配器 SPI」，详设 ⑱ ㉒ ㉓）：ontology-package v0.1，
启用求解契约时升为 v0.2 operation-centric.v1；图模式（variant=graph）是它的变体
（方案第 6 章第 1 项：留作适配器变体，评测服务按需取）。

渲染只读 ReleaseSource 的只读树，输出逐字节确定（_ReproducibleZip）；同一提交、同一版本参数
渲染出的包逐字节相同（tests/test_publish_render_regression.py 守住）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ...kernel import metamodel
from .. import deterministic_contract, package_mapping, package_operations, solver_contract
from ..document_metadata import report_name
from ..solver.service import solver_check_at
from ..workspace.errors import WorkspaceError
from ..workspace.layout import _strip_frontmatter
from .layout import package_script_filename
from .spi import Manifest, ReleaseSource


def _used_objects(body: str, object_keys) -> list[str]:
    """条目正文链接到的对象 key（frontmatter uses.objects），按本项目锁定版本的元模型解析链接（render 定下）。"""
    return sorted(set(metamodel.current().link_names(body, object_keys)) & set(object_keys))


ADAPTER_ID = "ontology-package"
# 关系结构问题逐条带给发布检查的上限（错误消息仍只拼前 20 条）
RELATION_PROBLEM_LIMIT = 50
FORMAT_ONTOLOGY = "ontology"
FORMAT_SOLVER = "solver"
VARIANT_STANDARD = "standard"
VARIANT_GRAPH = "graph"

# 属性表"属性别名"列的占位写法（模板约定无别名写"无"）在下游校验器里
# 可能被当作真实别名，引发"同名指向多字段"冲突（modeling 新版已过滤，
# consumer 与存量部署未必）；打包时统一清洗为空，单一出口治本。
# 属性表"属性别名"列的占位写法（模板约定无别名写"无"）在下游校验器里
# 可能被当作真实别名，引发"同名指向多字段"冲突（modeling 新版已过滤，
# consumer 与存量部署未必）；打包时统一清洗为空，单一出口治本。
_ALIAS_HEADER_NAMES = {"属性别名", "别名", "aliases", "alias"}
_ALIAS_EMPTY_MARKERS = {"无", "-", "—", "–", "n/a"}

def _is_placeholder_alias_cell(cells: list[str], alias_col: int | None) -> bool:
    if alias_col is None or alias_col >= len(cells):
        return False
    if all(set(c) <= set("-: ") for c in cells):  # 跳过分隔行
        return False
    return cells[alias_col].lower() in _ALIAS_EMPTY_MARKERS

def sanitize_placeholder_aliases(markdown: str) -> str:
    lines = markdown.split("\n")
    alias_col: int | None = None
    prev_is_row = False
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            alias_col = None
            prev_is_row = False
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not prev_is_row:
            lowered = [c.lower() for c in cells]
            alias_col = next(
                (idx for idx, c in enumerate(lowered) if c in _ALIAS_HEADER_NAMES),
                None,
            )
        elif _is_placeholder_alias_cell(cells, alias_col):
            raw = line.split("|")
            if alias_col + 1 < len(raw):
                raw[alias_col + 1] = " "
                lines[i] = "|".join(raw)
        prev_is_row = True
    return "\n".join(lines)


class _ReproducibleZip:
    """知识包 ZIP 的确定性写出（REQ-BLD-010）：先收集成员，正常退出时按路径排序写入。

    成员统一 UTF-8 编码，时间戳固定为 ZIP 纪元、权限固定为 0644，同一内容重复导出逐字节相同；
    同一路径写两次说明导出规则冲突，直接报错而不是让后写的静默覆盖。
    """

    EPOCH = (1980, 1, 1, 0, 0, 0)

    def __init__(self, dest_path: Path):
        self.dest_path = dest_path
        self.entries: dict[str, bytes] = {}

    def __enter__(self) -> "_ReproducibleZip":
        return self

    def writestr(self, name: str, data: str | bytes) -> None:
        if name in self.entries:
            raise WorkspaceError(f"知识包内路径重复：{name}")
        self.entries[name] = data.encode("utf-8") if isinstance(data, str) else bytes(data)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            return
        import zipfile

        with zipfile.ZipFile(self.dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(self.entries):
                info = zipfile.ZipInfo(name, date_time=self.EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                zf.writestr(info, self.entries[name])


def _manifest(path: Path, contracts, binding: dict, allow_unbound: bool) -> Manifest:
    data = path.read_bytes()
    unbound = sorted({
        item["object"] for item in binding["issues"]
        if item["kind"] != "declared_unverifiable"
    }) if allow_unbound else []
    return Manifest(
        format=FORMAT_SOLVER if contracts else FORMAT_ONTOLOGY,
        schema_version="0.2" if contracts else "0.1",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        unbound_objects=unbound,
    )


def _contract_modules(src: ReleaseSource):
    """本项目锁定版本的脚本契约、描述符与入参形态实现；项目没有装配目录时用镜像版本。"""
    from ..packs import contracts

    project_root = Path(src.state_root)
    return (
        contracts.module_for(project_root, "script_contract"),
        contracts.module_for(project_root, "script_descriptor"),
        contracts.module_for(project_root, "script_argument_shape"),
    )


def _output_verification_problem(src: ReleaseSource, key: str) -> str:
    """对外脚本输出的运行验证（develop !616）：与 script_verify.verification_problem 同口径，源码哈希按本次渲染的树算。

    Hub 发布冻结时把验证记录复制进冻结树（hub_packages.freeze），树里有就用它；按提交导出的只读树不带记录，读工作区的。
    """
    from ..packs import contracts

    verify = contracts.module_for(Path(src.state_root), "script_verify")
    tree = Path(src.tree)
    script = tree / "ontology/scripts" / f"{key}.py"
    if (tree / verify.RECORD_FILE).is_file():
        return verify.verification_problem(tree, script)
    record = verify.load_records(Path(src.state_root)).get(script.relative_to(tree).as_posix())
    if not isinstance(record, dict) or record.get("output_contract_version") != verify.VERIFICATION_VERSION:
        return "请使用 script_verify.py run 完成包含输出契约检查的运行验证"
    if record.get("source_hash") != verify.sandbox.source_hash(script):
        return "验证后脚本或依赖已修改，请重新运行验证"
    return ""


def resolve_binding(src: ReleaseSource) -> dict:
    """对象 ↔ 真实数据表的绑定（来源Sheet、`# 字段` 段与 mappings 三处同一份结果）。"""
    return src.binding or package_mapping.resolve_object_tables(
        src.tree,
        tables=package_mapping.read_source_tables(src.tree),
        strict_declared=not src.allow_unbound,
    )


class OntologyPackageAdapter:
    """知识包适配器：check 给门禁结论，render 写包，materialize 反向导入已发布的包。"""

    id = ADAPTER_ID
    formats = (FORMAT_ONTOLOGY, FORMAT_SOLVER)
    variants = (VARIANT_STANDARD, VARIANT_GRAPH)

    def check(self, src: ReleaseSource) -> list[dict]:
        """渲染前能确定的阻断项（Finding）：数据绑定、求解契约静态检查与验证回执。"""
        from ..solver.service import release_status_at

        findings = []
        binding = resolve_binding(src)
        for issue in binding["issues"]:
            findings.append({
                "code": issue["kind"],
                "level": "error" if issue["blocking"] else "warning",
                "message": issue["message"],
                "path": f"ontology/object/{issue['object']}.md",
            })
        solver = release_status_at(src.tree, src.state_root)
        deterministic = deterministic_contract.release_status_at(src.tree)
        if solver.get("enabled"):
            for item in (solver.get("findings") or []) + (solver.get("gate") or []):
                findings.append({"level": "error", **item})
        for item in deterministic.get("findings") or []:
            findings.append({"level": "error", **item})
        if solver.get("enabled") and deterministic.get("enabled"):
            findings.append({
                "code": deterministic_contract.CODE_CONFLICT,
                "level": "error",
                "message": "第一版不能在同一知识包中混合 deterministic 与 solver_workflow",
            })
        return findings

    def render(self, src: ReleaseSource, out: Path) -> Manifest:
        # 链接解析、关系端点按本项目锁定版本的元模型（B3 合并遗留：原来一律用镜像版本）
        with metamodel.using(Path(src.state_root)):
            return self._render(src, out)

    def _render(self, src: ReleaseSource, out: Path) -> Manifest:
        """发布为知识包（consumer 可导入的 ontology-package v0.1）。

        对齐 consumer/backend/ontology/package_import.py 的校验契约：
        manifest.yaml 六必填字段、objects/operations 目录 + frontmatter、
        operation 必须声明 uses.objects、index 首列与详情 key 逐字相等。

        src.binding：调用方已算好的绑定（按同一只读树算），缺省现算；
        所有变体均将「关联对象」转为 `# 关系` 表；缺端点、重复声明、显式关联字段不在两端字段清单里的，导出前拒绝。
        variant=graph：图模式（mappings 与关系按 modeling 图导入契约，补齐物理关联字段）。
        src.allow_unbound：降级发布——声明对不上的对象不阻断，来源Sheet 写 UNBOUND、不进 mapping。

        树里有求解契约（ontology/solver/）时升级为 v0.2 operation-centric.v1：求解
        Operation 改名为 solver_key 并补 execution 声明，sidecar 原字节写到 operations/
        （受管模板的输入 Schema 例外：按评估器补装置与原油品种的可选值），
        manifest 与两级 index 带上求解声明。没有求解契约的包内容保持 v0.1。

        ZIP 按 _ReproducibleZip 确定性写出：同一只读树、同一版本参数重复渲染逐字节相同。
        src.commit 非空时写进 manifest（source_commit），标明包出自哪个提交。
        src.draft：求解验证候选包，manifest 标 status: draft，不是发布。
        """
        root = src.tree
        meta = src.meta
        version = src.version
        source_commit = src.commit
        # 契约实现按本项目锁定的版本（方案 A16、详设 8.1 第 5 步）：和 agent 在工作区里跑的是同一份
        _script_contract, _script_descriptor, _script_argument_shape = _contract_modules(src)
        graph = src.variant == VARIANT_GRAPH
        allow_unbound = src.allow_unbound
        draft = src.draft
        binding = src.binding
        dest_path = out
        pkg = src.package_id or meta["id"]

        report_names: dict[tuple[str, str], str] = {}

        def entry_items(*rel_dirs: str, pattern: str = "*.md") -> list[tuple[str, str]]:
            items = []
            seen = set()
            for rel_dir in rel_dirs:
                directory = root / rel_dir
                if not directory.is_dir():
                    continue
                for path in sorted(directory.glob(pattern)):
                    if path.is_symlink() or path.name == "index.md" or path.stem in seen:
                        continue
                    seen.add(path.stem)
                    raw = path.read_text(encoding="utf-8")
                    if path.suffix == ".md":
                        try:
                            original_name = report_name(raw)
                        except ValueError as exc:
                            raise WorkspaceError(f"{rel_dir}/{path.name}: {exc}") from exc
                        if original_name is not None:
                            resource = "operation" if directory.name == "logic" else directory.name
                            report_names[(resource, path.stem)] = original_name
                    items.append((path.stem, _strip_frontmatter(raw)))
            return items

        object_texts = package_mapping.read_object_texts(root, strict=True)
        objects = []
        for key, raw in sorted(object_texts.items()):
            try:
                original_name = report_name(raw)
            except ValueError as exc:
                raise WorkspaceError(f"ontology/object/{key}.md: {exc}") from exc
            if original_name is not None:
                report_names[("object", key)] = original_name
            objects.append((key, _strip_frontmatter(raw)))
        # ontology/logic 为旧目录兼容（范式升级 ADR-0004）
        operations = entry_items("ontology/operation", "ontology/logic")
        # 对象 ↔ 真实数据表：来源Sheet、`# 字段` 段与 mappings 三处同一份绑定结果
        binding = binding or package_mapping.resolve_object_tables(
            root,
            tables=package_mapping.read_source_tables(root),
            strict_declared=not allow_unbound,
        )
        if binding["problems"]:
            raise WorkspaceError(
                "数据绑定无法发布：" + "；".join(binding["problems"]),
                code="binding_problems",
            )
        bound = binding["objects"]
        mappings = package_mapping.object_data_mappings(binding, graph=graph)
        # 两种变体都保留语义声明（端点没绑定物理表的概念关系也留着）；结构错误在规范化前拒绝，
        # 不静默丢掉（develop !594）。同一条关系在两端对象里各写一次（对象模板的写法）归并成一条，
        # 不算错误：发布不做重复关系检查
        relation_details: list[dict] = []
        graph_edges, relation_problems = package_mapping.package_relations(
            object_texts, strict_fields=True, details=relation_details,
        )
        if relation_problems:
            raise WorkspaceError("对象关系结构无效：" + "；".join(relation_problems[:20]), 422,
                                 "relation_structure_invalid", {"problems": relation_details[:RELATION_PROBLEM_LIMIT]})
        rules = entry_items("ontology/rule")
        metrics = entry_items("ontology/metric")
        scripts = entry_items("ontology/scripts", pattern="*.py")
        object_keys = {key for key, _ in objects}
        solver_contracts, solver_problems = solver_contract.load_workspace_solver(
            root, [key for key, _ in operations]
        )
        if solver_problems:
            raise WorkspaceError(
                "求解契约无法发布：" + "；".join(f"{p}（{p.path}）" for p in solver_problems),
                code=solver_problems[0].code,
            )
        solver_evaluators = solver_check_at(root).evaluators if solver_contracts else {}
        solver = solver_contract.render_package_solver(
            solver_contracts, {key: root / rel for key, rel in solver_evaluators.items()}
        )
        deterministic = deterministic_contract.ensure_publishable(root, solver_enabled=bool(solver.contracts))
        deterministic_by_operation = {
            item.operation: item for item in deterministic.contracts if item.enabled
        }
        deterministic_by_handler = {
            item.handler: item for item in deterministic.contracts if item.enabled
        }
        solver_by_stem = {item.operation: item for item in solver.contracts}

        def package_operation_key(stem: str) -> str:
            item = solver_by_stem.get(stem)
            return item.solver_key if item else stem

        operation_bodies = dict(operations)
        relations, operation_parents = package_operations.relation_view(operations)
        # 脚本契约与对外绑定（每个 operation 只对外一个脚本、子操作与主操作脚本的覆盖关系）
        # 与规则检查 ontology_checker --include-scripts 同一实现；写包之前拦下，不留半成品
        script_contracts = {}
        for key, code in scripts:
            try:
                script_contracts[key] = _script_contract.parse_script(code)
            except _script_contract.ScriptContractError as exc:
                raise WorkspaceError(f"脚本 {key} 契约无效：{exc}") from exc
        script_bindings = _script_descriptor.resolve_script_bindings(
            script_contracts, relations, set(relations)
        )
        if script_bindings.errors:
            raise WorkspaceError(
                "脚本绑定不符合规则，无法发布：" + "；".join(script_bindings.messages()),
                code="script_binding_conflict",
            )
        # 对外脚本的输出静态确认不了时，要有与本次源码（连同同目录依赖）一致的运行验证记录；记录本身不进包
        for key in script_bindings.bindings:
            if getattr(script_contracts[key], "output_verified", True):  # 锁定的旧契约库没有这项检查
                continue
            problem = _output_verification_problem(src, key)
            if problem:
                raise WorkspaceError(f"脚本 {key} 输出尚未验证：{problem}", 422, "script_output_unverified",
                                     {"path": f"ontology/scripts/{key}.py"})
        # 入参形态：会被 consumer 调用前的参数证据校验拒绝的脚本（一拒就可能终止整轮对话）不发布
        object_documents = dict(objects)
        shape_errors = _script_descriptor.argument_shape_errors(
            script_contracts,
            script_bindings.bindings,
            _script_argument_shape.attributes_from_documents(object_documents),
        )
        object_enums = _script_argument_shape.enum_values_from_documents(object_documents)
        if shape_errors:
            raise WorkspaceError(
                "脚本入参会被 consumer 拒绝，无法发布：" + "；".join(message for _name, message in shape_errors),
                code="script_argument_shape",
            )

        def q(s):
            return json.dumps(s, ensure_ascii=False)  # JSON 字符串是合法 YAML 标量

        manifest = (
            f"kind: ontology-package\n"
            f"schema_version: '0.1'\n"
            f"id: {pkg}\n"
            f"name: {q(meta['name'])}\n"
            f"description: {q('由 Onto Web 建模项目发布：' + meta['name'])}\n"
            f"version: {version}\n"
            # modeling 包契约（package-contract MANIFEST_REQUIRED_FIELDS）必选；
            # consumer 按已知键读取，多余字段无害
            f"status: {'draft' if draft else 'released'}\n"
            f"language: zh-CN\n"
        )
        if source_commit:
            # 来源提交：consumer 与 modeling 的导入都只取已知键，多出的这一项被忽略
            manifest += f"source_commit: {q(source_commit)}\n"
        if solver.contracts:
            manifest = manifest.replace(
                "schema_version: '0.1'\n", "schema_version: '0.2'\n"
            ) + solver_contract.manifest_solver_section(solver)

        def front(resource: str, key: str, title: str, extra: str = "") -> str:
            original_name = report_names.get((resource, key))
            if original_name is not None:
                extra += f"report_name: {q(original_name)}\n"
            return f"---\nresource: {resource}\nkey: {q(key)}\ntitle: {q(title)}\n{extra}---\n\n"

        def index_md(
            resource: str,
            key: str,
            title: str,
            items: list[tuple[str, str]],
            headers: list[str],
            row: "callable",
        ) -> str:
            # 表头对齐 modeling 知识包契约（knowledge-markdown-contract INDEX_CONFIG）；
            # consumer 按表头名取列、容忍多列，两端同一份索引
            head = "| " + " | ".join(headers) + " |\n" + "|" + " --- |" * len(headers) + "\n"
            rows = "".join(row(k) for k, _ in items)
            return front(resource, key, title) + head + rows

        with _ReproducibleZip(dest_path) as zf:
            zf.writestr(f"{pkg}/manifest.yaml", manifest)
            zf.writestr(
                f"{pkg}/index.md",
                front("root", "root", meta["name"]) + f"# {meta['name']}\n"
                + solver_contract.index_block(solver),
            )
            zf.writestr(
                f"{pkg}/objects/index.md",
                index_md(
                    "object_index", "objects", "对象目录", objects,
                    ["object key", "标题", "说明", "来源Sheet"],
                    lambda k: f"| {k} | {k} | {k} | "
                              f"{bound.get(k, {}).get('table') or package_mapping.UNBOUND} |\n",
                ),
            )
            for key, body in objects:
                item = bound.get(key, {})
                table = item.get("table")
                # 绑定到真实表时补 mapping/sources frontmatter：mappings json 缺失或被裁剪时
                # modeling 仍能从 sources 还原 sourceRefs
                extra = (
                    f"mapping:\n  provider_alias: {package_mapping.PROVIDER_ALIAS}\n"
                    f"  resource: {q(key if graph else table)}\nsources: [{q('表格：' + table)}]\n"
                    if table else ""
                )
                body = package_mapping.rewrite_object_body(body, item.get("fields") or [])
                body = package_mapping.rewrite_relations_section(body, graph_edges.get(key, []))
                zf.writestr(
                    f"{pkg}/objects/{key}.md",
                    front("object", key, key, extra) + sanitize_placeholder_aliases(body),
                )
            if mappings["objects"]:
                zf.writestr(
                    f"{pkg}/mappings/object-data-mappings.json",
                    json.dumps(mappings, ensure_ascii=False, indent=2) + "\n",
                )
            if solver.contracts:
                # execution 列让 consumer 认出求解行；停用的求解 operation 不进求解索引
                def operation_row(k: str) -> str:
                    item = solver_by_stem.get(k)
                    execution = solver_contract.SOLVER_EXECUTION_MODE if item and item.enabled else ""
                    return package_operations.index_row(
                        k, operation_bodies[k], operation_parents.get(k, []),
                        package_key=package_operation_key(k), execution=execution,
                    )

                operation_headers = ["operation key", "标题", "输入", "输出", "execution"]
            elif deterministic_by_operation:
                def operation_row(k: str) -> str:
                    execution = "deterministic" if k in deterministic_by_operation else ""
                    return package_operations.index_row(
                        k, operation_bodies[k], operation_parents.get(k, []), execution=execution,
                    )

                operation_headers = ["operation key", "标题", "输入", "输出", "execution"]
            else:
                def operation_row(k: str) -> str:
                    return package_operations.index_row(k, operation_bodies[k], operation_parents.get(k, []))

                operation_headers = ["operation key", "标题", "输入", "输出"]
            zf.writestr(
                f"{pkg}/operations/index.md",
                index_md(
                    "operation_index", "operations", "操作目录", operations,
                    operation_headers, operation_row,
                ),
            )
            for key, body in operations:
                used = _used_objects(body, object_keys)
                item = solver_by_stem.get(key)
                if item is None:
                    extra = f"uses:\n  objects: {json.dumps(used, ensure_ascii=False)}\n"
                    callees = [
                        package_operation_key(callee) for callee in package_operations.uses_operations(key, relations)
                    ]
                    if callees:
                        # 主操作按顺序调用的子操作：consumer 据此识别主/子操作并路由脚本
                        extra += f"  operations: {json.dumps(callees, ensure_ascii=False)}\n"
                    deterministic_item = deterministic_by_operation.get(key)
                    if deterministic_item is not None:
                        execution = {
                            "type": "deterministic",
                            "parser": f"scripts/{deterministic_item.key}_parser.py",
                            "input_schema": f"schemas/{deterministic_item.key}.input.json",
                            "output_schema": f"schemas/{deterministic_item.key}.output.json",
                        }
                        extra += f"aliases: {json.dumps(list(deterministic_item.aliases), ensure_ascii=False)}\n"
                        extra += "execution:\n" + "".join(
                            f"  {field}: {q(value)}\n" for field, value in execution.items()
                        )
                        if deterministic_item.routing_all_terms or deterministic_item.routing_any_terms:
                            extra += "execution_policy:\n  routing:\n"
                            extra += (
                                "    all_terms: "
                                + json.dumps(list(deterministic_item.routing_all_terms), ensure_ascii=False)
                                + "\n"
                            )
                            extra += (
                                "    any_terms: "
                                + json.dumps(list(deterministic_item.routing_any_terms), ensure_ascii=False)
                                + "\n"
                            )
                    zf.writestr(f"{pkg}/operations/{key}.md", front("operation", key, key, extra) + body)
                    continue
                zf.writestr(
                    f"{pkg}/operations/{item.solver_key}.md",
                    solver_contract.operation_frontmatter(
                        item, key, used, object_keys, report_names.get(("operation", key))
                    ) + body,
                )
            for path, data in solver.files.items():
                zf.writestr(f"{pkg}/{path}", data)
            for item in deterministic_by_operation.values():
                zf.writestr(f"{pkg}/scripts/{item.key}_parser.py", item.parser)
                zf.writestr(f"{pkg}/schemas/{item.key}.input.json", item.input_schema)
                zf.writestr(f"{pkg}/schemas/{item.key}.output.json", item.output_schema)
            # 规则/指标/脚本：五元组一等实体（范式升级 ADR-0004），可选目录
            if rules:
                zf.writestr(
                    f"{pkg}/rules/index.md",
                    index_md(
                        "rule_index", "rules", "规则目录", rules,
                        ["rule key", "标题", "说明"],
                        lambda k: f"| {k} | {k} | {k} |\n",
                    ),
                )
                for key, body in rules:
                    used = _used_objects(body, object_keys)
                    extra = f"uses:\n  objects: {json.dumps(used, ensure_ascii=False)}\n"
                    zf.writestr(f"{pkg}/rules/{key}.md", front("rule", key, key, extra) + body)
            if metrics:
                zf.writestr(
                    f"{pkg}/metrics/index.md",
                    index_md(
                        "metric_index", "metrics", "指标目录", metrics,
                        ["metric key", "标题", "说明"],
                        lambda k: f"| {k} | {k} | {k} |\n",
                    ),
                )
                for key, body in metrics:
                    used = _used_objects(body, object_keys)
                    extra = f"uses:\n  objects: {json.dumps(used, ensure_ascii=False)}\n"
                    zf.writestr(f"{pkg}/metrics/{key}.md", front("metric", key, key, extra) + body)
            if scripts:
                exposed = [(key, code) for key, code in scripts if key in script_bindings.bindings]
                if exposed:
                    zf.writestr(
                        f"{pkg}/scripts/index.md",
                        index_md(
                            "script_index", "scripts", "脚本目录", exposed,
                            ["script key", "标题"],
                            lambda k: f"| {k} | {(script_contracts[k].title or k).replace('|', '，')} |\n",
                        ),
                    )
                for key, code in scripts:
                    operation = script_bindings.bindings.get(key)
                    # 执行契约：对外脚本的描述符与工作区 ontology/scripts/<脚本>.md 同一渲染器，
                    # 只把 entrypoint 换成包内 ASCII 文件名（modeling 按 scripts/<entrypoint> 取源码
                    # 且只收 ASCII 文件名），bound_operation 换成包内 operation key（求解 operation 为
                    # solver_key）。求解评估器只带源码，求解契约按包内路径引用它（见 _package_scripts），
                    # 文件名须与之一致。「关联operation」为「无」或不是现有操作的脚本只是被复用的
                    # 库模块，不对外调用、没有描述符，保留原文件名，对外脚本按原名 import 它，回导时也还原得了。
                    if operation or solver_contract.is_evaluator(code.encode("utf-8")):
                        filename = package_script_filename(key)
                    else:
                        filename = f"{key}.py"
                    zf.writestr(f"{pkg}/scripts/{filename}", code)
                    if operation:
                        deterministic_item = deterministic_by_handler.get(key)
                        zf.writestr(
                            f"{pkg}/scripts/{key}.md",
                            _script_descriptor.render_descriptor(
                                key, script_contracts[key], package_operation_key(operation), filename,
                                operation_parents.get(operation, []),
                                enums=object_enums,
                                data_access=(deterministic_item.data_access if deterministic_item else None),
                            ),
                        )
        return _manifest(dest_path, solver.contracts, binding, allow_unbound)

    def materialize(self, archive: Path, dest: Path) -> dict:
        """反向导入：已发布知识包按工作区布局物化到 dest，原包留底；返回包摘要。"""
        from .. import package_archive

        summary = package_archive.inspect_archive(archive)
        if summary["kind"] != package_archive.KIND_KNOWLEDGE_PACKAGE:
            raise WorkspaceError("这不是已发布知识包（缺少知识包清单或 objects 目录）")
        if summary["blockers"]:
            raise WorkspaceError("；".join(summary["blockers"]))
        package_archive.materialize(dest, archive, summary["package_root"])
        return summary


ADAPTER = OntologyPackageAdapter()
