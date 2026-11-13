"""Evidence gates for query-required data cases; never trust the answer's process narrative."""
import json
import re

from .artifacts import redact
from .core import Failure

RULE_VERSION = 'query-evidence-v1'
REQUIREMENTS = ('auto', 'business_query', 'reasoning_only')
MAX_RECEIPTS = 20
PREVIEW_CHARS = 2000
QUERY = re.compile(r'(?:查询|查阅|检索|读取|获取|查找|查数|查库).{0,16}(?:业务数据|配套.{0,6}数据|数据库|数据表|记录)|'
                   r'(?:query|search|retrieve|fetch|read|look up).{0,32}(?:business data|database|records|dataset)', re.I | re.S)
EXEMPT = re.compile(r'无需(?:查询|查库)|不(?:需要|要)(?:查询|查库)|独立假设|纯规则|仅(?:依据|根据)题干|'
                    r'no (?:database|query)|hypothetical scenario|reasoning.only', re.I)


def requirement(question, configured='auto'):
    if configured not in REQUIREMENTS:
        raise Failure('execution_requirement_invalid', '执行要求须为 auto、business_query 或 reasoning_only', 422)
    if configured != 'auto':
        return {'mode': configured, 'source': 'explicit', 'rule_version': RULE_VERSION}
    if EXEMPT.search(question):
        # A conflict is not permission to guess which part the user meant.
        mode = 'ambiguous' if QUERY.search(EXEMPT.sub('', question)) else 'reasoning_only'
    else:
        mode = 'business_query' if QUERY.search(question) else 'reasoning_only'
    return {'mode': mode, 'source': 'auto', 'rule_version': RULE_VERSION}


def object_value(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            pass
    return {}


def bounded(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = redact(text)
    return text[:PREVIEW_CHARS], len(text) > PREVIEW_CHARS


def collect(events):
    """All events contribute to counts; only 20 bounded receipts enter Judge context.

    A complete replay starts at seq 1, has no gaps, closes every tool call and has a terminal turn.
    Receipt previews are evidence only, never model instructions. A truncated receipt cannot prove sufficiency.
    """
    started, receipts = {}, []
    sequences, terminal = [], False
    uncertain, failed, successful, omitted = False, 0, 0, 0
    for event in events:
        seq = event.get('seq')
        if isinstance(seq, int):
            sequences.append(seq)
        else:
            uncertain = True
        kind = event.get('event_type')
        payload = object_value(event.get('payload'))
        key = str(payload.get('tool_call_id') or '')
        if kind in ('run.completed', 'run.failed', 'run.cancelled', 'turn.finished'):
            terminal = True
        if kind == 'tool.started':
            if not key or key in started:
                uncertain = True
            started[key] = payload
        if kind != 'tool.finished':
            continue
        start = started.pop(key, None)
        if start is None:
            uncertain = True
            start = {}
        name = str(payload.get('name') or payload.get('tool_name') or start.get('name') or start.get('tool_name') or '')
        audit = object_value(payload.get('business_query_audit'))
        # Nested script queries count only when the runtime emits the same structured audit.
        if name != 'business_data_search' and not audit:
            continue
        raw_args = start.get('input_preview', start.get('input'))
        args = object_value(raw_args)
        action = str(audit.get('action') or args.get('action') or 'query')
        if action not in ('query', 'search', 'lookup_values'):
            continue
        output = object_value(payload.get('output_preview'))
        status = str(payload.get('status') or '').lower()
        error = payload.get('error_code') or payload.get('error') or output.get('error_code') or audit.get('error_code')
        is_failed = status in ('error', 'failed') or bool(error) or audit.get('status') == 'error'
        is_success = status in ('completed', 'success') and not is_failed
        if is_failed:
            failed += 1
        elif is_success:
            successful += 1
        else:
            uncertain = True
        input_text, input_cut = bounded(args)
        output_text, output_cut = bounded(payload.get('output_preview', ''))
        sanitization = object_value(event.get('sanitization'))
        truncated = any((input_cut, output_cut, payload.get('truncated'), payload.get('output_truncated'),
                         start.get('truncated'), start.get('omitted'), payload.get('omitted'), not args,
                         sanitization.get('sensitive_fields_redacted')))
        receipt = {'id': key[:128] or f'event-{seq}', 'tool': name[:64], 'action': action,
                   'status': 'failed' if is_failed else 'completed' if is_success else 'unknown',
                   'input': input_text, 'output': output_text, 'truncated': truncated}
        if audit:
            receipt['audit'] = {k: redact(str(audit.get(k) or ''))[:256] for k in
                                ('status', 'entity', 'action', 'record_count', 'plan_digest', 'request_digest', 'error_code')}
        # Successful tool status alone does not establish that a query really executed.
        anchors = output.get('evidence')
        anchored = isinstance(anchors, list) and any(isinstance(a, dict) and a.get('query_digest') for a in anchors)
        receipt['query_confirmed'] = bool(audit.get('plan_digest')) or anchored
        if len(receipts) < MAX_RECEIPTS:
            receipts.append(receipt)
        else:
            omitted += 1
    continuous = sequences == list(range(1, len(sequences) + 1)) and bool(sequences)
    complete = continuous and terminal and not started and not uncertain
    return {'trace_complete': complete, 'query_successes': successful, 'query_failures': failed,
            'receipts': receipts, 'omitted_receipts': omitted, 'tool_availability': 'not_recorded',
            'rule_version': RULE_VERSION}


def check(requirement, evidence):
    mode = requirement['mode']
    if mode == 'reasoning_only':
        return {'status': 'not_required', 'reason': '本题不要求业务取数', 'rule_version': RULE_VERSION}
    if mode == 'ambiguous':
        return {'status': 'unverifiable', 'reason': '题目执行要求存在歧义，请确认查询要求', 'rule_version': RULE_VERSION}
    evidence = evidence or {}
    success = evidence.get('query_successes', 0)
    if not success and evidence.get('query_failures'):
        status, reason = 'query_failed', '业务查询调用失败；请先诊断工具或数据源，不能据此自动修改知识包'
    elif not evidence.get('trace_complete'):
        status, reason = 'unverifiable', '执行轨迹缺失或不完整，无法核实必要查询'
    elif not success:
        status, reason = 'missing_query', '完整执行轨迹未记录必要业务查询；工具是否提供尚无证据，不能归责于模型'
    else:
        usable = [r for r in evidence.get('receipts', []) if r['status'] == 'completed' and r['query_confirmed']]
        if not usable or evidence.get('omitted_receipts') or any(r['truncated'] for r in usable):
            status, reason = 'unverifiable', '查询回执缺失、截断或超出取证上限，无法确认关键事实依据'
        else:
            status, reason = 'review', '查询已执行，仍需核对取数与题目及关键判断的对应关系'
    return {'status': status, 'reason': reason, 'rule_version': RULE_VERSION}


def finalize(answer_judgement, verification, evidence, assessment=None):
    """A model pass never bypasses an execution gate; non-business outcomes use existing manual/error states."""
    gate = dict(verification)
    if gate['status'] == 'not_required':
        return answer_judgement, gate, 'settled'
    if gate['status'] == 'review':
        assessment = assessment or {}
        refs = assessment.get('receipt_ids') or []
        valid = {r['id'] for r in (evidence or {}).get('receipts', []) if r['status'] == 'completed' and r['query_confirmed']}
        valid_refs = isinstance(refs, list) and all(isinstance(ref, str) for ref in refs)
        cited = valid_refs and bool(refs) and set(refs) <= valid
        supported = assessment.get('status') == 'supported' and cited and bool(assessment.get('reason'))
        if supported:
            gate.update(status='verified', reason=assessment.get('reason') or '查询依据及必要判断已核对', receipt_ids=refs)
            return answer_judgement, gate, 'settled'
        if assessment.get('status') == 'unsupported':
            gate.update(status='unsupported', reason=assessment.get('reason') or '查询依据不支持关键事实或必要判断')
        else:
            gate.update(status='unverifiable', reason='判分模型未提供有效的执行依据核验结论及回执引用')
    reason = gate['reason'] + '；答案判定：' + answer_judgement['verdict'] + '。' + answer_judgement['reason']
    if gate['status'] in ('missing_query', 'unsupported'):
        return {'verdict': 'fail', 'reason': reason[:4000], 'source': 'execution_gate'}, gate, 'settled'
    return None, gate, 'execution_error' if gate['status'] == 'query_failed' else 'manual'
