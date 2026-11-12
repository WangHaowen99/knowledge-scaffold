"""Solver contract validation: a run type that checks a released package's solver operations in consumer.

Each case asks the consumer agent to solve one operation with given arguments, then reads the run's public
events: which solver tool was called with what arguments, its public result, and the matching
``solver.*`` events. The verdict comes from deterministic assertions, never from a Judge. The rules are
ported from the earlier modeling-side validation; see docs/decisions/solver-validation-unified-entry.md.
"""
import json
import re
from datetime import datetime

import yaml

from .artifacts import CHINA, clip, redact
from .core import Failure, canonical, digest
from .packages import unpack

DEFAULT_CONCURRENCY = 1
MAX_CONCURRENCY = 4
MAX_CASES = 20
MAX_CASE_NAME = 80
MAX_QUESTION = 4000
MAX_ARGUMENTS_BYTES = 64 * 1024
MAX_EXPECTED_CANDIDATES = 100_000
MAX_OPERATIONS = 64
MAX_SCHEMA_CHARS = 16 * 1024
CASE_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
SOLVER_KEY = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
CASE_FIELDS = {'id', 'name', 'solver_key', 'arguments', 'question', 'expected_candidate_count'}
SOLVER_MODE = 'solver_workflow'
ROUTE_PROFILE = 'ordered_binary_route.v1'
MAX_ROUTE_ITEMS = 64
# Events are read once per case; 4 MiB as in the earlier pipeline, since solver outputs are verbose.
EVENT_LIMIT = 4 * 1024 * 1024
MAX_ARGUMENT_DIFFS = 20
MAX_TEXT = 300
REDACTED = '[REDACTED]'
NO_ANSWER = '（Agent 未返回文字回答）'
SOLVER_EVENTS = frozenset({'solver.input.awaiting_confirmation', 'solver.progress', 'solver.verification.completed',
                           'solver.verification.rejected', 'solver.result.authoritative'})
# Tool cards are solve_<operation key slug>_<first 8 hex of the contract digest>; the compatible card "solve"
# names the operation in its input.
CARD = re.compile(r'^solve_([a-z0-9_]+)_[0-9a-f]{8}$')
PUBLIC_RESULT_FIELDS = ('run_id', 'job_id', 'feasibility', 'optimality', 'proof_kind', 'result_artifact_ref',
                        'verification_evidence_ref', 'knowledge_revision_id', 'fact_snapshot_digest', 'problem_digest',
                        'candidate_results_artifact_ref', 'candidate_results_artifact_digest')
# When both the tool result and the authoritative event carry one of these, they must agree.
AUTHORITATIVE_EVENT_FIELDS = ('job_id', 'result_artifact_ref', 'verification_evidence_ref', 'knowledge_revision_id',
                              'fact_snapshot_digest', 'problem_digest', 'candidate_results_artifact_ref',
                              'candidate_results_artifact_digest')
REQUIRED_EVIDENCE = ('feasibility', 'job_id', 'result_artifact_ref', 'verification_evidence_ref',
                     'knowledge_revision_id', 'fact_snapshot_digest', 'problem_digest')
CANDIDATE_EVIDENCE = ('candidate_results_artifact_ref', 'candidate_results_artifact_digest')
EVIDENCE_FIELDS = ('status', 'authoritative', 'code', 'phase', 'retryable', 'detail', 'missing', 'solver_tool_called',
                   'solver_tool', 'arguments_match', 'argument_diff', 'arguments_unverifiable', 'feasibility',
                   'optimality', 'proof_kind', 'run_id', 'job_id', 'result_artifact_ref', 'verification_evidence_ref',
                   'knowledge_revision_id', 'fact_snapshot_digest', 'problem_digest', 'candidate_results_artifact_ref',
                   'candidate_results_artifact_digest', 'candidate_count', 'candidate_results_error')
GUIDANCE = {
    'solver_tool_not_called': 'Agent 链路问题：检查求解 Operation 的描述与触发说明，确认问题能让 Agent 选中该求解能力。',
    'solver_arguments_mismatch': 'Agent 链路问题：检查求解 Operation 对入参的说明；自填提问时确认问题包含全部入参。',
    'solver_arguments_unverifiable': '入参在运行事件中被截断或脱敏，无法比对：缩短过长的字符串入参，或调整与凭据同名的字段名后重试。',
    'solver_result_not_authoritative': '结果不是 consumer 的权威求解结果：检查求解服务与 Verifier。',
    'solver_authoritative_event_missing': '没有收到该求解调用的权威结果事件：检查 consumer 求解事件。',
    'solver_authoritative_event_mismatch': '求解工具结果与权威结果事件的证据不一致：检查 consumer 求解事件。',
    'solver_evidence_incomplete': '权威结果缺少证据字段：检查求解服务版本。',
    'solver_candidate_count_mismatch': '候选数与期望不符：检查 Decision Model、评估器与数据。',
    'solver_timeout': '求解未在时限内结束：检查求解服务负载与数据规模后重试。',
    'solver_contract_invalid': '求解契约未通过 consumer 准入：在建模侧修复契约后发布新版本。',
    'capability_not_registered': 'consumer 未启用求解插件：联系 consumer 运维启用后重试。',
    'capability_admission_rejected': 'consumer 准入拒绝了该求解操作：在建模侧修复契约后发布新版本。',
}
DEFAULT_GUIDANCE = '未得到求解结果：按失败码与阶段定位数据、模型编译、事实快照或评估器问题。'


def _text(value):
    return clip(' '.join(redact(str(value)).split()), MAX_TEXT) if isinstance(value, str) and value.strip() else ''


# ---- 求解验证集 ----


def definition(body):
    """A validated solver validation set: at most 20 cases, each naming one solver operation."""
    if set(body) - {'name', 'cases', 'revision'}:
        raise Failure('definition_invalid', '未知求解验证集字段', 422)
    cases = body.get('cases')
    if not isinstance(cases, list) or not 0 < len(cases) <= MAX_CASES:
        raise Failure('case_limit', f'用例数须为 1–{MAX_CASES}', 422)
    normalized, seen = [], set()
    for number, case in enumerate(cases, 1):
        if not isinstance(case, dict) or set(case) - CASE_FIELDS:
            raise Failure('case_invalid', f'第 {number} 条用例含未知字段', 422)
        case_id, name, key = case.get('id'), case.get('name'), case.get('solver_key')
        if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id) or case_id in seen:
            raise Failure('case_invalid', f'第 {number} 条用例的 id 无效或重复', 422)
        seen.add(case_id)
        if not isinstance(name, str) or not 0 < len(name.strip()) <= MAX_CASE_NAME:
            raise Failure('case_invalid', f'用例 {case_id} 的名称须为 1–{MAX_CASE_NAME} 字', 422)
        if not isinstance(key, str) or not SOLVER_KEY.fullmatch(key):
            raise Failure('case_invalid', f'用例 {case_id} 的求解操作 key 无效', 422)
        arguments = case.get('arguments')
        if not isinstance(arguments, dict) or len(canonical(arguments).encode()) > MAX_ARGUMENTS_BYTES:
            raise Failure('case_invalid', f'用例 {case_id} 的入参须为不超过 64 KiB 的 JSON 对象', 422)
        item = dict(id=case_id, name=name.strip(), solver_key=key, arguments=arguments)
        question = case.get('question')
        if question not in (None, ''):
            if not isinstance(question, str) or len(question) > MAX_QUESTION:
                raise Failure('case_invalid', f'用例 {case_id} 的提问不超过 {MAX_QUESTION} 字', 422)
            item['question'] = question
        count = case.get('expected_candidate_count')
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_EXPECTED_CANDIDATES:
                raise Failure('case_invalid', f'用例 {case_id} 的期望候选数须为 0–{MAX_EXPECTED_CANDIDATES} 的整数', 422)
            item['expected_candidate_count'] = count
        normalized.append(item)
    return dict(name=body['name'], cases=normalized)


# ---- 包内求解声明 ----


def _frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
        value = yaml.safe_load('\n'.join(lines[1:end])) or {}
    except (StopIteration, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def _enabled(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or '').strip().lower() in {'true', '1', 'yes', 'on', 'enabled'}


def _sidecar(files, path, parse):
    raw = files.get(str(path or ''))
    if raw is None:
        return None
    try:
        return parse(raw.decode('utf-8'))
    except (ValueError, yaml.YAMLError):  # UnicodeDecodeError is a ValueError
        return None


def operations(raw):
    """Enabled solver operations declared by a package: key, title, model, profile and input schema."""
    files, manifest = unpack(raw)
    capabilities = manifest.get('capabilities') if isinstance(manifest.get('capabilities'), dict) else {}
    if str(manifest.get('schema_version')) != '0.2' or not _enabled(capabilities.get('enabled', True)):
        return []
    models = {item.get('id'): item for item in capabilities.get('decision_models') or [] if isinstance(item, dict)}
    found = []
    for name in sorted(files):
        if not re.fullmatch(r'operations/[^/]+\.md', name) or name == 'operations/index.md':
            continue
        try:
            front = _frontmatter(files[name].decode('utf-8'))
        except UnicodeDecodeError:
            continue
        execution = front.get('execution') if isinstance(front.get('execution'), dict) else {}
        if execution.get('mode') != SOLVER_MODE or not _enabled(execution.get('enabled', True)):
            continue
        key = str(front.get('key') or name[len('operations/'):-3])
        model = models.get(execution.get('decision_model')) or {}
        entry = _sidecar(files, model.get('entry'), yaml.safe_load)
        schema = _sidecar(files, model.get('input_schema'), json.loads)
        text = json.dumps(schema, ensure_ascii=False) if isinstance(schema, dict) else ''
        found.append(dict(solver_key=key, title=str(front.get('title') or key), decision_model=model.get('id'),
                          profile_id=entry.get('profile_id') if isinstance(entry, dict) else None,
                          input_schema=schema if text and len(text) <= MAX_SCHEMA_CHARS else None,
                          input_schema_truncated=len(text) > MAX_SCHEMA_CHARS))
        if len(found) >= MAX_OPERATIONS:
            break
    return found


def default_question(operation, arguments):
    """Name the solver operation and give its full arguments; only the contract chain is checked, not data lookup."""
    text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return (f"请调用求解能力「{operation['title']}」（operation_key：{operation['solver_key']}）求解。"
            '入参如下，请原样使用，不要增删或改写字段，也不要再向我确认：\n'
            f'```json\n{text}\n```')


def expected_candidates(operation, case):
    """An explicit count wins; an ordered binary route of N items has 2N canonical candidates."""
    if 'expected_candidate_count' in case:
        return case['expected_candidate_count']
    items = case['arguments'].get('items')
    if operation.get('profile_id') == ROUTE_PROFILE and isinstance(items, list) and 1 <= len(items) <= MAX_ROUTE_ITEMS:
        return 2 * len(items)
    return None


def plan_cases(cases, declared):
    """Run cases for a solver set against the package's operations; every enabled operation needs a case."""
    if not declared:
        raise Failure('solver_validation_not_required', '该知识包版本没有启用的求解操作，无需求解验证', 422)
    by_key = {operation['solver_key']: operation for operation in declared}
    unknown = sorted({case['solver_key'] for case in cases} - set(by_key))
    if unknown:
        raise Failure('solver_cases_invalid', '用例引用的求解操作不存在或未启用：' + '、'.join(unknown), 422)
    uncovered = sorted(set(by_key) - {case['solver_key'] for case in cases})
    if uncovered:
        raise Failure('solver_cases_incomplete', '以下求解操作还没有验证用例：' + '、'.join(uncovered), 422)
    planned = []
    for index, case in enumerate(cases):
        operation = by_key[case['solver_key']]
        # Run cases are addressed by position like data evaluation cases; the set's own ID is kept as case_id.
        planned.append(dict(case, id=str(index), case_id=case['id'],
                            question=case.get('question') or default_question(operation, case['arguments']),
                            question_generated=not case.get('question'),
                            expected_candidate_count=expected_candidates(operation, case),
                            input_digest=digest(case['arguments']), status='pending'))
    return planned


def unavailable(imported):
    """Solver operations consumer reported unusable when it installed the package, by key."""
    found = {}
    if not isinstance(imported, dict):
        return found
    warnings = imported.get('warnings') if isinstance(imported.get('warnings'), dict) else {}
    for line in warnings.get('operations') or []:
        match = re.match(r'^(\S+) \(([a-z_]{1,80})\): (.*)$', str(line))
        if match:
            found[match.group(1)] = {'code': match.group(2), 'message': _text(match.group(3))}
    status = imported.get('plugin_status') if isinstance(imported.get('plugin_status'), dict) else {}
    for entry in status.get('extensions') or []:
        if isinstance(entry, dict) and entry.get('execution_mode') == SOLVER_MODE and entry.get('registered') is False:
            found['*'] = {'code': str(entry.get('reason_code') or 'capability_not_registered'),
                          'message': _text(entry.get('reason')) or 'consumer 未启用求解插件'}
    return found


# ---- 运行事件取证 ----


def _plain(value):
    return value if isinstance(value, dict) else {}


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def observe(raw):
    """Solver tool calls of one consumer run, keyed by tool call ID, from its replayed public events."""
    calls, order = {}, 0
    for block in raw.decode('utf-8', 'replace').split('\n\n'):
        data = '\n'.join(line[5:].strip() for line in block.splitlines() if line.startswith('data:'))
        try:
            event = json.loads(data) if data else None
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind, payload = event.get('event_type'), _plain(event.get('payload'))
        key = str(payload.get('tool_call_id') or '')
        if kind == 'tool.started':
            name = str(payload.get('name') or payload.get('tool_name') or '')
            value = _json(payload.get('input_preview', payload.get('input')))
            call = None
            if name == 'solve':
                value = _plain(value)
                call = dict(name=name, operation_key=str(value.get('operation_key') or ''), arguments=value.get('arguments'))
            elif CARD.fullmatch(name):
                call = dict(name=name, slug=CARD.fullmatch(name).group(1), arguments=value)
            if call is not None:
                order += 1
                call.update(order=order, progress={}, unverifiable=bool(payload.get('truncated') or payload.get('omitted')))
                calls.setdefault(key or f'seq-{order}', call)
        elif key in calls and kind == 'tool.finished':
            calls[key].update(tool_status=str(payload.get('status') or ''), tool_error_code=str(payload.get('error_code') or ''),
                              output=_json(payload.get('output_preview')))
        elif key in calls and kind in SOLVER_EVENTS:
            calls[key]['progress'].update(payload)
            if kind == 'solver.result.authoritative':
                calls[key]['authoritative_event'] = payload
    return calls


def _slug(key):
    """The operation part of an exact card name, as consumer builds it: card names are at most 64 characters."""
    slug = re.sub(r'^_+|_+$', '', re.sub(r'[^A-Za-z0-9]+', '_', key)).lower()
    return slug[:64 - len('solve_') - len('_xxxxxxxx')].rstrip('_')


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same_scalar(expected, actual):
    if expected == actual or (expected is None and actual is None):
        return True
    # Agents may write a number as a numeric string, or the reverse.
    for number, other in ((expected, actual), (actual, expected)):
        if _is_number(number) and isinstance(other, str) and other.strip():
            try:
                return float(other) == number
            except ValueError:
                return False
    return False


def argument_diff(expected, actual, path='', out=None):
    """Paths where the agent's arguments differ from the case; key order is ignored and null equals missing."""
    out = [] if out is None else out
    label = path or '(全部入参)'
    if len(out) >= MAX_ARGUMENT_DIFFS or actual == REDACTED:
        return out
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            out.append(label)
        else:
            for index, value in enumerate(expected):
                argument_diff(value, actual[index], f'{path}[{index}]', out)
        return out
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            out.append(label)
            return out
        for key in sorted(set(expected) | set(actual)):
            argument_diff(expected.get(key), actual.get(key), f'{path}.{key}' if path else key, out)
        return out
    if not _same_scalar(expected, actual):
        out.append(label)
    return out


def _authoritative_problem(call):
    event = call.get('authoritative_event') or {}
    if event.get('authoritative') is not True or not str(event.get('job_id') or '').strip():
        return 'solver_authoritative_event_missing'
    output = _plain(call.get('output'))
    for key in AUTHORITATIVE_EVENT_FIELDS:
        left, right = str(event.get(key) or '').strip(), str(output.get(key) or '').strip()
        if left and right and left != right:
            return 'solver_authoritative_event_mismatch'
    return None


def _failure_detail(data):
    if _text(data.get('message')):
        return _text(data.get('message'))
    errors = data.get('errors') if isinstance(data.get('errors'), list) else []
    parts = [entry if isinstance(entry, str) else str(_plain(entry).get('message') or _plain(entry).get('code') or '')
             for entry in errors[:3]]
    return _text('；'.join(part for part in parts if part.strip())[:MAX_TEXT])


def call_result(case, call):
    """One case's result from the solver tool call it made; event fields fill in a truncated tool output."""
    diff = [] if call['unverifiable'] else argument_diff(case['arguments'], call.get('arguments'))
    tool = dict(solver_tool_called=True, solver_tool=call['name'], arguments_match=not diff and not call['unverifiable'])
    if call['unverifiable']:
        tool['arguments_unverifiable'] = True
    if diff:
        tool['argument_diff'] = diff
    data = {**call['progress'], **_plain(call.get('output'))}
    status = str(data.get('status') or '')
    if status == 'completed':
        problem = _authoritative_problem(call) if data.get('authoritative') is True else None
        fields = {key: str(data.get(key)).strip()[:512] for key in PUBLIC_RESULT_FIELDS if str(data.get(key) or '').strip()}
        result = dict(status='completed', authoritative=data.get('authoritative') is True and not problem,
                      candidate_count=None, **fields, **tool)
        if problem:
            result.update(code=problem)
        return result
    if status == 'awaiting_input':
        missing = [str(item)[:120] for item in data.get('missing') or [] if str(item).strip()][:20]
        return dict(status='awaiting_input', code='awaiting_input', phase=str(data.get('phase') or 'bind')[:64],
                    authoritative=False, missing=missing, **tool)
    if status == 'running' or (not status and not call.get('tool_status')):
        return dict(status='error', code='solver_timeout', phase=str(data.get('phase') or 'solve')[:64],
                    authoritative=False, retryable=True, **tool)
    fallback = call.get('tool_error_code') or ('solver_tool_failed' if call.get('tool_status') in ('failed', 'error')
                                               else 'solver_result_rejected')
    result = dict(status='error', code=str(data.get('code') or fallback)[:120], phase=str(data.get('phase') or 'unknown')[:64],
                  authoritative=False, retryable=data.get('retryable') is True, **tool)
    if _failure_detail(data):
        result['detail'] = _failure_detail(data)
    return result


def case_result(case, calls, remote):
    """The result of one case from its observed calls; the last call of its operation counts."""
    key = case['solver_key']
    matched = [call for call in calls.values()
               if (call.get('operation_key') == key if 'operation_key' in call else call.get('slug') == _slug(key))]
    if matched:
        return call_result(case, max(matched, key=lambda call: call['order']))
    error = _plain(remote.get('error'))
    if remote.get('status') in ('failed', 'cancelled') and not calls:
        return dict(status='error', code=str(error.get('error_code') or 'agent_run_failed').lower()[:120], phase='agent',
                    authoritative=False, detail=_text(error.get('message')) or 'Agent 运行未完成')
    others = sorted({call['name'] for call in calls.values()})[:5]
    answer = _text(str(_plain(remote.get('result')).get('content') or remote.get('result') or ''))
    detail = ('Agent 调用了其他求解工具：' + '、'.join(others)) if others else 'Agent 未调用求解工具' + (
        f'，回复：{answer[:200]}' if answer else '')
    return dict(status='error', code='solver_tool_not_called', phase='agent', authoritative=False,
                solver_tool_called=False, detail=detail)


# ---- 断言 ----


def assess(result, expected):
    """Assertions of one case and its first failure: agent chain first, then the solver result."""
    detail = result.get('detail') or ''
    suffix = f'：{detail}' if detail else ''
    called = result.get('solver_tool_called')
    if called is False:
        return ([{'name': 'solver_tool_called', 'passed': False}], 'solver_tool_not_called',
                'Agent 未调用该求解操作的求解工具（Agent 链路问题，非知识包求解失败）' + suffix)
    assertions = [{'name': 'solver_tool_called', 'passed': True}] if called is True else []
    if result.get('arguments_unverifiable'):
        assertions.append({'name': 'arguments_match', 'passed': False})
        return assertions, 'solver_arguments_unverifiable', '求解工具入参在运行事件中被截断或脱敏，无法核对是否与用例一致'
    if result.get('arguments_match') is False:
        diff = result.get('argument_diff') or []
        assertions.append({'name': 'arguments_match', 'diff': diff, 'passed': False})
        return (assertions, 'solver_arguments_mismatch', 'Agent 传给求解工具的入参与用例不一致（Agent 链路问题，非知识包求解失败）'
                + (f"：{'、'.join(diff[:5])}" if diff else ''))
    if result.get('arguments_match') is True:
        assertions.append({'name': 'arguments_match', 'passed': True})
    # A result without the call or comparison cannot be attributed to this case's solver call.
    if result.get('status') == 'completed' and called is not True:
        assertions.append({'name': 'solver_tool_called', 'passed': False})
        return assertions, 'solver_tool_not_called', '验证结果缺少求解工具调用记录，无法确认结果来自该求解操作'
    if result.get('status') == 'completed' and result.get('arguments_match') is not True:
        assertions.append({'name': 'arguments_match', 'passed': False})
        return assertions, 'solver_arguments_mismatch', '验证结果缺少入参比对结论，无法确认按用例入参求解'
    assessed, code, reason = _assess_result(result, expected, suffix)
    return assertions + assessed, code, reason


def _assess_result(result, expected, suffix):
    if result.get('status') != 'completed':
        code = str(result.get('code') or 'solver_result_rejected')
        phase = str(result.get('phase') or '')
        return ([{'name': 'completed', 'passed': False}], code,
                f"未得到求解结果（{code}{'，阶段 ' + phase if phase else ''}）{suffix}")
    if result.get('code') in ('solver_authoritative_event_missing', 'solver_authoritative_event_mismatch'):
        reason = {'solver_authoritative_event_missing': '没有收到该求解调用的权威结果事件',
                  'solver_authoritative_event_mismatch': '求解工具结果与权威结果事件的证据不一致'}[result['code']]
        return [{'name': 'authoritative', 'passed': False}], result['code'], reason
    authoritative = result.get('authoritative') is True
    assertions = [{'name': 'authoritative', 'passed': authoritative}]
    if not authoritative:
        return assertions, 'solver_result_not_authoritative', '结果不是 consumer 的权威求解结果' + suffix
    infeasible = result.get('feasibility') == 'infeasible'
    required = list(REQUIRED_EVIDENCE)
    if (expected and not infeasible) or any(str(result.get(key) or '').strip() for key in CANDIDATE_EVIDENCE):
        required += CANDIDATE_EVIDENCE
    missing = [key for key in required if not str(result.get(key) or '').strip()]
    assertions.append({'name': 'evidence', 'missing': missing, 'passed': not missing})
    if missing:
        return assertions, 'solver_evidence_incomplete', '权威结果缺少证据 ' + '、'.join(missing)
    if expected is None:
        return assertions, '', ''
    actual = result.get('candidate_count')
    passed = actual == expected if actual is not None else infeasible
    assertions.append({'name': 'candidate_count', 'expected': expected, 'actual': actual, 'passed': passed})
    if passed:
        return assertions, '', ''
    if actual is None:
        code = str(result.get('candidate_results_error') or 'solver_candidate_count_mismatch')
        return assertions, code, f'无法读取候选结果（{code}），预期 {expected} 个候选'
    return assertions, 'solver_candidate_count_mismatch', f'候选数 {actual}，预期 {expected}'


def has_candidate_results(result):
    """An authoritative result that names candidate results consumer can count."""
    authoritative = result['status'] == 'completed' and result['authoritative']
    return bool(authoritative and result.get('job_id') and result.get('candidate_results_artifact_ref'))


def settle(case, result, answer):
    """Write the verdict and public evidence into a case; the evidence keeps no tokens or connection details."""
    assertions, code, reason = assess(result, case.get('expected_candidate_count'))
    evidence = {key: result[key] for key in EVIDENCE_FIELDS if key in result}
    case.update(answer=answer or NO_ANSWER, status='settled',
                judgement={'verdict': 'fail' if code else 'pass', 'reason': reason or '全部断言通过',
                           'source': 'solver_assertions', 'code': code or None},
                solver={**evidence, 'assertions': assertions, 'input_digest': case.get('input_digest')})
    return case


def unavailable_result(entry):
    return dict(status='error', code=entry['code'], phase='admission', authoritative=False, detail=entry['message'])


# ---- 摘要与产物 ----


def summary(run):
    cases = run['cases']
    failed = [case for case in cases if (case.get('judgement') or {}).get('verdict') != 'pass']
    first = failed[0] if failed else None
    code = ((first.get('judgement') or {}).get('code') or (first.get('error') or {}).get('code')) if first else None
    message = (first.get('judgement') or {}).get('reason') or (first.get('error') or {}).get('message') if first else None
    return dict(total=len(cases), passed=len(cases) - len(failed), failed=len(failed),
                first_failure_code=code, first_failure_message=clip(message, MAX_TEXT) if message else None)


def _cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def render_report(run, stem, now):
    """Deterministic report of a solver validation run: per-case assertions and the guidance for each failure."""
    base, ran = run.get('base_ref') or {}, run.get('package_ref') or {}
    totals = summary(run)
    lines = [f"# 求解验证报告 — {datetime.fromtimestamp(now, CHINA).strftime('%Y-%m-%d %H:%M')}", '',
             '> 验证方式：独立评测服务从 Hub 固定版本取包，由 consumer 智能体逐用例调用求解能力，按确定性断言判定（不经 Judge）',
             f"> 轮次：{run['id']}",
             f"> 知识包版本：{base.get('version') or ran.get('version') or ''}",
             f"> 数据源：{run.get('data_source_name') or ''}", f"> 求解验证集：{run.get('solver_set_name') or ''}", '',
             '## 总览', '', f"- 用例：{totals['total']}", f"- 通过 {totals['passed']} / 未通过 {totals['failed']}"]
    if run['status'] != 'completed':
        lines.append(f"- ⚠️ 轮次未正常结束（{run['status']}），未执行的用例计为未通过")
    lines += ['', '## 逐用例明细', '']
    for index, case in enumerate(run['cases'], 1):
        judgement = case.get('judgement') or {}
        passed = judgement.get('verdict') == 'pass'
        error = case.get('error') if isinstance(case.get('error'), dict) else {}
        code = judgement.get('code') or error.get('code')
        reason = judgement.get('reason') or error.get('message') or '本用例未取得验证结果'
        lines += [f"### {index}. {'✅' if passed else '❌'} {case.get('name') or case.get('case_id')}（{case['solver_key']}）", '',
                  f"- **结论**：{'通过' if passed else '未通过'}" + (f'（{code}）' if code else ''),
                  f'- **说明**：{_cell(clip(reason, 1000))}', '']
        assertions = (case.get('solver') or {}).get('assertions') or []
        if assertions:
            lines += ['| 断言 | 结果 | 说明 |', '| --- | --- | --- |']
            for item in assertions:
                note = '、'.join(item.get('diff') or item.get('missing') or [])
                if item['name'] == 'candidate_count':
                    note = f"期望 {item.get('expected')}，实际 {item.get('actual')}"
                lines.append(f"| {item['name']} | {'✅' if item['passed'] else '❌'} | {_cell(note)} |")
            lines.append('')
        if not passed:
            lines += [f"- **修复指引**：{GUIDANCE.get(code, DEFAULT_GUIDANCE)}", '']
    return '\n'.join(lines)


def render_guidance(run, stem, now):
    """Fix guidance derived from the failure codes; no model is asked."""
    totals = summary(run)
    lines = [f"# 求解验证修复指引 — {datetime.fromtimestamp(now, CHINA).strftime('%Y-%m-%d %H:%M')}", '',
             f"> 依据求解验证报告：reports/{stem}.md（轮次 {run['id']}）", '']
    if not totals['failed']:
        return '\n'.join(lines + [f"全部 {totals['total']} 条用例通过，无需修复。", ''])
    codes = {}
    for index, case in enumerate(run['cases'], 1):
        judgement = case.get('judgement') or {}
        if judgement.get('verdict') != 'pass':
            code = judgement.get('code') or (case.get('error') or {}).get('code') or 'not_settled'
            codes.setdefault(code, []).append(index)
    for code, numbers in codes.items():
        lines += [f'## {code}', '', f"- 涉及用例：{'、'.join(str(n) for n in numbers)}",
                  f"- 指引：{GUIDANCE.get(code, DEFAULT_GUIDANCE)}", '']
    return '\n'.join(lines)
