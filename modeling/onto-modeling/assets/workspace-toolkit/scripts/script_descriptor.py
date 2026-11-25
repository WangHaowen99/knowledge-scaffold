#!/usr/bin/env python3
"""脚本描述符：ontology/scripts/<脚本>.py 旁同名 <脚本>.md，即 consumer 调用这个脚本的契约。

- 描述符是 .py 的派生物：由头部注释与 argparse 声明渲染，不要手改。工作区与导出的知识包
  共用 render_descriptor，导出时只把 entrypoint 换成包内 ASCII 文件名，建模时看到的就是
  consumer 拿到的那份。
- 格式：一级标题
  「# 脚本用途 / # 输入参数 / # 输出 / # 执行边界」，参数表是正文第一张表（consumer 把第一张
  表当参数表），输出表三列（consumer 跳过列数不同的行）。
- 绑定规则 resolve_script_bindings：consumer 只按 operation 调用脚本，同一 operation 只能有一个
  脚本；主操作有脚本时，没有标「可单独调用 > 是」的子操作不能再有自己的脚本——consumer 选中
  这类子操作时会借用主操作的脚本跑完整流程。「关联operation：无」的库模块不对外调用，没有描述符。
- 入参形态（script_argument_shape.py）：会被 consumer 调用前参数证据校验拒绝的写法在规则检查与
  导出时拦下；记录参数的说明自动补上原样拷贝查询结果、已提供但没给取值的记录照传 {} 的要求。

用法：python3 .claude/scripts/script_descriptor.py render ontology
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from operation_relations import (
    OperationRelation,
    ancestors,
    load_operation_documents,
    parent_map,
    relations_from_documents,
    standalone_flag,
)
from script_argument_shape import (
    COPY_RULE,
    IDENTITY_NOTE,
    PRESENCE_RULE,
    argument_shape_issues,
    attributes_from_documents,
    enum_note,
    enum_values_from_documents,
    identity_keys,
    is_record_param,
    load_object_documents,
    yes_no_issues,
)
from script_contract import ScriptContract, parse_script, script_warnings

RENDER_COMMAND = "python3 .claude/scripts/script_descriptor.py render ontology"
PARAM_HEADER = "| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 说明 | 是否多值 | Action |"
PARAM_DIVIDER = "| --- | --- | --- | --- | --- | --- | --- | --- |"
OUTPUT_HEADER = "| 输出 key | 类型 | 说明 |"
OUTPUT_DIVIDER = "| --- | --- | --- |"
# consumer 调用脚本的口径（consumer/backend/agent/tools/execute_script.py）：启动前的参数证据校验与成败判定。
# 非 0 退出被判为终止性错误（_terminal_execute_script_error），本轮对话立即结束，智能体没有机会改参数重试。
EXECUTION_BOUNDARIES = (
    "传参：记录参数的每一项原样拷贝同一条查询结果的字段，不同对象的数据分别放进各自的参数，"
    "不要把不同记录或不同对象的字段拼进一项，也不要改写取值；否则 consumer 拒绝调用，脚本不会运行。"
    "没有的编号、外键直接省略，脚本会按调用范围关联，资料不全时给出资料不足；"
    "题面说明记录已提供、但没给取值时传 {}，不要整条不传，否则脚本只能判记录缺失。",
    "成功：退出码 0，stdout 只输出一个 JSON 对象。「不通过」「资料不足」「资料冲突」等业务结论也算成功，写在输出字段里；"
    "不要用 ok=false、error、status=failed 表达业务结论，consumer 会把它当成调用失败。",
    '参数错误：退出码 0，stdout 输出 {"ok":false,"error":{"code":"INVALID_INPUT","message":"原因"}}，'
    "按原因改正参数后重试；参数不变原样重试会被判为停滞。",
    '脚本异常：非 0 退出，stderr 输出一行 ONTO_PUBLIC_ERROR:{"code":"SCRIPT_FAILED","message":"出错步骤"}；'
    "consumer 遇到非 0 退出会直接结束本轮对话。",
)
_EXIT_ON_INVALID_INPUT_RE = re.compile(r"""_public_error\(\s*["']INVALID_INPUT["']""")


@dataclass
class BindingReport:
    bindings: dict[str, str] = field(default_factory=dict)  # 脚本名 → 对外绑定的 operation
    errors: list[tuple[str, str]] = field(default_factory=list)  # (相对 ontology 的文件, 说明)：导出拒绝
    warnings: list[tuple[str, str]] = field(default_factory=list)  # (相对 ontology 的文件, 说明)

    def messages(self) -> list[str]:
        return [message for _file, message in self.errors]


@dataclass
class ScriptsState:
    contracts: dict[str, ScriptContract]  # 契约可解析的脚本
    invalid: set[str]  # 契约无效的脚本名（规则检查另报原因）
    report: BindingReport
    sources: dict[str, str] = field(default_factory=dict)  # 脚本名 → 源码
    attributes: dict[str, set[str]] = field(default_factory=dict)  # 对象名 → 属性英文名称
    parents: dict[str, list[str]] = field(default_factory=dict)  # operation → 调用它的父 operation
    enums: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # 对象名 → {枚举字段: 允许取值}

    def expected_descriptors(self) -> dict[str, str]:
        """工作区应有的描述符：{脚本名: 内容}。绑定冲突照样渲染，由规则检查与导出拦截。"""
        return {
            name: render_descriptor(
                name, self.contracts[name], operation, f"{name}.py", self.parents.get(operation, []),
                enums=self.enums,
            )
            for name, operation in self.report.bindings.items()
        }


def argument_shape_errors(
    contracts: dict[str, ScriptContract],
    bindings: dict[str, str],
    attributes: dict[str, set[str]],
) -> list[tuple[str, str]]:
    """对外脚本里会被 consumer 参数证据校验拒绝的入参形态：(脚本名, 说明)。导出据此拒绝。"""
    errors: list[tuple[str, str]] = []
    for name in sorted(bindings):
        contract = contracts.get(name)
        if contract is None:
            continue
        for severity, message in argument_shape_issues(list(contract.params), attributes):
            if severity == "错误":
                errors.append((name, f"{name}.py：{message}"))
    return errors


def _param_desc(param: dict, enums: dict[str, dict[str, list[str]]]) -> str:
    """参数说明：consumer 的智能体调用时只看得到这一句，按需补上记录照传、枚举取值、原样拷贝与编号的要求。
    记录照传放在最前面：假设场景里智能体最容易整条不传。"""
    desc = param["desc"]
    record = is_record_param(param)
    leading = [PRESENCE_RULE] if record and "{}" not in desc else []
    notes = [enum_note(param, enums)] if record else []
    if record and "原样拷贝" not in desc:
        notes.append(COPY_RULE)
    identities = identity_keys(param)
    if identities and record:
        notes.append(
            f"其中 {'、'.join(identities)} 是编号，{IDENTITY_NOTE}；只省略这些字段，省略后不剩字段就传 {{}}"
        )
    elif identities:
        notes.append(f"这是编号，{IDENTITY_NOTE}")
    return _cell("；".join(part for part in (*leading, desc, *notes) if part))


def render_descriptor(
    name: str,
    contract: ScriptContract,
    bound_operation: str,
    entrypoint: str,
    stage_of: list[str] | tuple[str, ...] = (),
    *,
    enums: dict[str, dict[str, list[str]]] | None = None,
) -> str:
    """stage_of：调用 bound_operation 的父 operation。子操作自己的脚本只覆盖流程中的一步，
    标题与用途写明适用范围，consumer 的智能体选脚本时就能看到，不会拿它回答整个流程的问题。
    enums：对象的枚举字段与允许取值，写进记录参数的说明。"""
    title = contract.title or name
    purpose = contract.title or f"由 /ontology-codegen 生成的确定性脚本，实现见 `{entrypoint}`。"
    if stage_of:
        flows = "、".join(f"「{parent}」" for parent in stage_of)
        title = f"{title.rstrip('。')}（只覆盖{flows}中的这一步）"
        purpose = (
            f"{purpose} 适用范围：只覆盖{flows}流程中「{bound_operation}」这一步，不含{flows}在这一步之前的输入检查、"
            f"前置判断等步骤；问题涉及资料缺失、资料冲突，或要{flows}的完整结论时，请执行{flows}。"
        )
    lines = [
        "---",
        "resource: script",
        f"key: {_yaml(name)}",
        f"title: {_yaml(title)}",
        *([f"description: {_yaml(purpose)}"] if contract.title or stage_of else []),
        f"bound_operation: {_yaml(bound_operation)}",
        "runtime: python",
        f"entrypoint: {entrypoint}",
        "---",
        "",
        "# 脚本用途",
        "",
        _cell(purpose),
        "",
        "# 输入参数",
        "",
        PARAM_HEADER,
        PARAM_DIVIDER,
        *(
            f"| {p['name']} | {p['flag']} | {p['type']} | {p['required']} | {p['default']} | "
            f"{_param_desc(p, enums or {})} | {p['multiple']} | {p['action']} |"
            for p in contract.params
        ),
        "",
        "# 输出",
        "",
    ]
    if contract.outputs:
        lines += [OUTPUT_HEADER, OUTPUT_DIVIDER]
        lines += [f"| {o['name']} | {o['type']} | {_cell(o['desc'])} |" for o in contract.outputs]
    elif contract.output_text.strip():
        lines += [_cell(line) for line in contract.output_text.splitlines() if line.strip()]
    else:
        lines.append("（脚本头部「输出」未声明字段）")
    lines += ["", "# 执行边界", "", *(f"- {item}" for item in EXECUTION_BOUNDARIES), ""]
    return "\n".join(lines)


def resolve_script_bindings(
    contracts: dict[str, ScriptContract],
    relations: dict[str, OperationRelation],
    operation_keys: set[str],
) -> BindingReport:
    """按整个 scripts 目录与 operation 调用关系决定每个脚本对外绑定哪个 operation。

    关联operation 写了不存在的名字时按库模块处理（不对外、没有描述符），与导出一致；
    这类写法由 ontology_checker 的「脚本绑定」检查报错，这里不重复报。"""
    report = BindingReport()
    by_operation: dict[str, list[str]] = {}
    for name in sorted(contracts):
        contract = contracts[name]
        if not contract.bound:
            continue  # 库模块：只供其他脚本 import
        if contract.fragment:
            report.errors.append((f"scripts/{name}.py", (
                f"{name}.py 是片段脚本（关联operation：{contract.bound} / {contract.fragment}）：consumer 只按 operation "
                f"调用整段脚本，片段单独绑定会让「{contract.bound}」的其余步骤被跳过。请把这几步并入「{contract.bound}」的整段脚本；"
                "需要单独执行的步骤拆成子操作，并在它的「# 与operation的关联」写「- 可单独调用 > 是」"
            )))
            continue
        if contract.bound not in operation_keys:
            continue
        report.bindings[name] = contract.bound
        by_operation.setdefault(contract.bound, []).append(name)

    for operation, names in sorted(by_operation.items()):
        if len(names) > 1:
            files = "、".join(f"{name}.py" for name in names)
            report.errors.append((f"scripts/{names[0]}.py", (
                f"operation「{operation}」绑定了多个脚本（{files}）：consumer 只执行文件名排第一的那个，"
                "其余永远调不到。请合并为一个脚本，公共代码放进「关联operation：无」的库模块"
            )))

    parents = parent_map(relations)
    scripted = set(by_operation)
    for operation in sorted(set(relations) | scripted):
        covering = [ancestor for ancestor in ancestors(operation, parents) if ancestor in scripted]
        if not covering:
            continue
        standalone = standalone_flag(relations.get(operation, OperationRelation())) is True
        if operation in scripted and not standalone:
            for name in by_operation.get(operation, []):
                report.errors.append((f"scripts/{name}.py", (
                    f"「{operation}」是「{covering[0]}」流程中的一步，且没有标可单独调用："
                    f"「{covering[0]}」的脚本已经覆盖这一步，consumer 选中「{operation}」时会改为执行「{covering[0]}」的完整流程。"
                    f"请删除 {name}.py，把逻辑并入「{covering[0]}」的脚本；确实需要单独调用时，"
                    f"在「{operation}」的「# 与operation的关联」写「- 可单独调用 > 是」"
                )))
        elif operation not in scripted and standalone:
            report.warnings.append((f"operation/{operation}.md", (
                f"「{operation}」标了可单独调用，但还没有自己的脚本：consumer 选中它时会借用「{covering[0]}」的脚本跑完整流程。"
                f"需要单独调用时请为「{operation}」生成脚本"
            )))
    return report


def load_scripts_state(ontology_dir: Path) -> ScriptsState:
    contracts: dict[str, ScriptContract] = {}
    sources: dict[str, str] = {}
    invalid: set[str] = set()
    scripts_dir = Path(ontology_dir) / "scripts"
    if scripts_dir.is_dir():
        for path in sorted(scripts_dir.glob("*.py")):
            try:
                sources[path.stem] = path.read_text(encoding="utf-8")
                contracts[path.stem] = parse_script(sources[path.stem])
            except (OSError, ValueError):  # ValueError 含 ScriptContractError 与 UnicodeDecodeError
                invalid.add(path.stem)
    documents = load_operation_documents(ontology_dir)
    relations = relations_from_documents(documents)
    report = resolve_script_bindings(contracts, relations, set(documents))
    parents = {
        key: sorted(parent for parent in found if parent in documents)
        for key, found in parent_map(relations).items()
    }
    object_documents = load_object_documents(ontology_dir)
    return ScriptsState(
        contracts, invalid, report, sources, attributes_from_documents(object_documents),
        {key: value for key, value in parents.items() if value},
        enum_values_from_documents(object_documents),
    )


def sync_workspace_descriptors(ontology_dir: Path) -> list[Path]:
    """按 .py 补齐、刷新描述符，删掉没有对外脚本的描述符；契约无效的脚本保留原描述符。返回改动的文件。"""
    scripts_dir = Path(ontology_dir) / "scripts"
    if not scripts_dir.is_dir():
        return []
    state = load_scripts_state(ontology_dir)
    expected = state.expected_descriptors()
    changed: list[Path] = []
    for name, content in expected.items():
        path = scripts_dir / f"{name}.md"
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            changed.append(path)
    for path in sorted(scripts_dir.glob("*.md")):
        if path.name.lower() == "index.md" or path.stem in expected or path.stem in state.invalid:
            continue
        path.unlink()
        changed.append(path)
    return changed


def descriptor_issues(ontology_dir: Path) -> list[tuple[str, str, str]]:
    """规则检查用：(相对 ontology 的文件, 严重度, 说明)。绑定规则、契约提示与描述符同步。"""
    state = load_scripts_state(ontology_dir)
    expected = state.expected_descriptors()
    issues = [(file, "错误", message) for file, message in state.report.errors]
    issues += [(file, "警告", message) for file, message in state.report.warnings]
    for name in sorted(state.report.bindings):
        contract = state.contracts[name]
        issues += [(f"scripts/{name}.py", "警告", f"第 {warning['line']} 行：{warning['message']}")
                   for warning in script_warnings(state.sources[name], f"scripts/{name}.py", contract)]
        issues += [
            (f"scripts/{name}.py", severity, message)
            for severity, message in argument_shape_issues(list(contract.params), state.attributes)
        ]
        issues += [
            (f"scripts/{name}.py", severity, message)
            for severity, message in yes_no_issues(state.sources.get(name, ""))
        ]
        if not contract.outputs:
            issues.append((f"scripts/{name}.py", "警告", (
                "头部「输出」没有按「字段  类型  说明」逐行列出 stdout JSON 的字段，描述符的输出表是空的"
            )))
        if "__main__" in state.sources.get(name, "") and "ONTO_PUBLIC_ERROR" not in state.sources[name]:
            issues.append((f"scripts/{name}.py", "警告", (
                "脚本异常没有在 stderr 输出 ONTO_PUBLIC_ERROR：consumer 结束对话时只记下退出码，看不出是哪一步出错"
                "（写法见 .claude/templates/script-template.py 的 _cli）"
            )))
        if _EXIT_ON_INVALID_INPUT_RE.search(state.sources.get(name, "")):
            issues.append((f"scripts/{name}.py", "警告", (
                "参数错误以非 0 退出（_public_error(\"INVALID_INPUT\", ...)）：consumer 遇到非 0 退出会直接结束本轮对话，"
                "智能体没有机会改参数重试。参数错误改用 _invalid_input（退出码 0，输出 ok=false 与字段级原因）；"
                "操作明确要求补全的记录或字段也用 _invalid_input。允许缺失的资料按业务约定输出"
            )))
    scripts_dir = Path(ontology_dir) / "scripts"
    for name, content in sorted(expected.items()):
        path = scripts_dir / f"{name}.md"
        if not path.is_file():
            issues.append((f"scripts/{name}.md", "错误", f"缺少脚本描述符：运行 {RENDER_COMMAND} 生成"))
        elif path.read_text(encoding="utf-8") != content:
            issues.append((f"scripts/{name}.md", "错误", (
                f"描述符与 {name}.py 不一致：运行 {RENDER_COMMAND} 重新生成（描述符由脚本头部注释与 argparse 生成，不要手改）"
            )))
    if scripts_dir.is_dir():
        for path in sorted(scripts_dir.glob("*.md")):
            if path.name.lower() == "index.md" or path.stem in expected or path.stem in state.invalid:
                continue
            issues.append((f"scripts/{path.name}", "错误", (
                f"多余的脚本描述符：没有绑定 operation 的同名脚本，consumer 不会用到它。运行 {RENDER_COMMAND} 清理"
            )))
    return issues


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # JSON 字符串是合法 YAML 标量


def _cell(text: str) -> str:
    """表格单元与正文都不能出现竖线：consumer 会把以竖线开头的行当成参数表。"""
    return " ".join(str(text or "").split()).replace("|", "，")


def _emit(text: str, stream=None) -> None:
    """命令输出给 agent 读，不走 logging。"""
    target = sys.stdout if stream is None else stream
    target.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 ontology/scripts/*.py 生成同名描述符（consumer 调用契约）")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="补齐、刷新描述符，清理没有对外脚本的描述符")
    render.add_argument("ontology_dir", help="ontology 目录（含 operation/ 与 scripts/）")
    args = parser.parse_args(argv)

    ontology_dir = Path(args.ontology_dir)
    changed = sync_workspace_descriptors(ontology_dir)
    for path in changed:
        _emit(f"{'已更新' if path.exists() else '已删除'} {path.as_posix()}")
    _emit(f"描述符已同步：改动 {len(changed)} 个文件")
    state = load_scripts_state(ontology_dir)
    if state.invalid:
        names = "、".join(sorted(state.invalid))
        _emit(f"契约无效、未生成描述符的脚本：{names}（运行 ontology_checker.py --include-scripts 查看原因）", sys.stderr)
    for message in state.report.messages():
        _emit(f"绑定问题（导出会被拦下）：{message}", sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
