"""Conservative checks for the JSON written by a script's CLI; never executes source."""
from __future__ import annotations

import ast

VERIFICATION_VERSION = 1
_UNKNOWN = None
_MAX_PATHS = 32


def _is_json_dump(node, aliases):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id in aliases
            and node.func.attr == "dumps" and node.args)


def _output_arguments(node, aliases):
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return []
    call = node.value
    sink = isinstance(call.func, ast.Name) and call.func.id in {"print", "_emit"}
    sink |= (isinstance(call.func, ast.Attribute) and call.func.attr == "write"
             and ast.unparse(call.func.value) == "sys.stdout")
    if not sink or any(kw.arg in {"file", "stream"} for kw in call.keywords):
        return []
    return [argument for argument in call.args if _is_json_dump(argument, aliases)]


def _json_aliases(tree):
    aliases = set()
    for node in tree.body:
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.name == "json":
                aliases.add(alias.asname or alias.name)
    return aliases


def _called_functions(nodes, functions):
    for node in nodes:
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id in functions:
                yield call.func.id


def static_outputs(tree: ast.Module, outputs: tuple[dict, ...]) -> tuple[list[str], bool]:
    """Check known CLI result keys. Unknown Python expressions require runtime verification.

    Track simple assignments, local function returns and conditional return paths. Loops,
    mutation, decorators and external calls deliberately produce an unknown result.
    """
    if not outputs:
        return [], True  # Legacy free-text output declarations have no machine-readable schema.
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    aliases = _json_aliases(tree)

    def resolve(node, env, stack):
        if isinstance(node, ast.Dict):
            if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in node.keys):
                return [_UNKNOWN]
            # The documented parameter-error envelope is separate from business output.
            pairs = {key.value: value for key, value in zip(node.keys, node.values)}
            if (isinstance(pairs.get("ok"), ast.Constant) and pairs["ok"].value is False
                    and "error" in pairs):
                return []
            return [set(pairs)]
        if isinstance(node, ast.Name):
            return env.get(node.id, [_UNKNOWN])
        if isinstance(node, ast.IfExp):
            return resolve(node.body, env, stack) + resolve(node.orelse, env, stack)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = functions.get(node.func.id)
            if fn is None or fn.name in env or fn.decorator_list:
                return [_UNKNOWN]
            if fn.name in stack or len(stack) >= 8:
                return [_UNKNOWN]
            params = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
            if fn.args.vararg:
                params.append(fn.args.vararg)
            if fn.args.kwarg:
                params.append(fn.args.kwarg)
            return returns(fn.body, {param.arg: [_UNKNOWN] for param in params}, stack + (fn.name,))
        return [_UNKNOWN]

    def assignment(node, env, stack):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if all(isinstance(target, ast.Name) for target in targets):
            values = resolve(node.value, env, stack)
            for target in targets:
                env[target.id] = values
        else:
            env.clear()  # A subscript/attribute write can mutate an aliased result dictionary.

    def returns(body, env, stack):
        pending = [(list(body), env)]
        found = []
        while pending:
            statements, scope = pending.pop()
            if not statements:
                found.append(_UNKNOWN)  # Implicit return None.
                continue
            node, *rest = statements
            if isinstance(node, ast.Return):
                found.extend(resolve(node.value, scope, stack))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                assignment(node, scope, stack)
                pending.append((rest, scope))
            elif isinstance(node, ast.If):
                branches = [node.body, node.orelse]
                if isinstance(node.test, ast.Constant):
                    branches = [node.body if node.test.value else node.orelse]
                pending.extend((branch + rest, dict(scope)) for branch in branches)
            elif isinstance(node, ast.Raise):
                continue
            elif isinstance(node, (ast.Pass, ast.Import, ast.ImportFrom)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            ):
                pending.append((rest, scope))
            else:
                found.append(_UNKNOWN)
            if len(pending) + len(found) > _MAX_PATHS:
                return [_UNKNOWN]
        return found

    errors, complete, sinks = [], True, 0
    # Follow CLI calls; an unused helper printing its own JSON is not the public result.
    entries = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))]
    pending = list(_called_functions(entries, functions))
    if not pending:
        pending = [name for name in ("_cli", "main") if name in functions]
    reachable = set()
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(_called_functions([functions[name]], functions))
    # A dict passed to json.dumps for storage or logging is not a CLI output contract.
    for scope in [tree, *(functions[name] for name in sorted(reachable))]:
        env = {}
        for node in scope.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                assignment(node, env, ())
                continue
            if isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With, ast.Match)):
                if any(_is_json_dump(item, aliases) for item in ast.walk(node)):
                    complete = False
            for argument in _output_arguments(node, aliases):
                sinks += 1
                for keys in resolve(argument.args[0], env, ()):
                    if keys is _UNKNOWN:
                        complete = False
                        continue
                    expected = {item["name"].split(".")[0] for item in outputs}
                    if keys != expected:
                        errors.append(
                            f"第 {argument.lineno} 行输出字段与声明不一致："
                            f"缺少 {sorted(expected - keys)}，多余 {sorted(keys - expected)}"
                        )
            if not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
                env.clear()
    return list(dict.fromkeys(errors)), complete and sinks > 0


def output_error(payload, outputs: tuple[dict, ...]) -> str:
    """Validate actual successful JSON output, including declared field types, without echoing values."""
    if not outputs:
        return ""
    if not isinstance(payload, dict):
        return "输出契约要求 JSON 对象"
    expected = {item["name"].split(".")[0] for item in outputs}
    if set(payload) != expected:
        return f"输出字段与声明不一致：缺少 {sorted(expected - set(payload))}，多余 {sorted(set(payload) - expected)}"
    types = {
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: type(value) is int,
        "number": lambda value: type(value) in (int, float),
        "boolean": lambda value: isinstance(value, bool),
        "object": lambda value: isinstance(value, dict),
        "array": lambda value: isinstance(value, list),
    }
    for field in outputs:
        value = payload
        for part in field["name"].split("."):
            if not isinstance(value, dict) or part not in value:
                return f"输出缺少字段 {field['name']}"
            value = value[part]
        check = types.get(field["type"])
        if check and not check(value):
            return f"输出字段 {field['name']} 类型与声明 {field['type']} 不一致"
    return ""
