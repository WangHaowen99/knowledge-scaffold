"""Evaluation report, answers and context artifacts of a stopped run, kept in the legacy report format.

onto-modeling copies these files into a project workspace for /ontology-evolution, which parses
``reports/eval-*.md`` (per-case details and tool-failure lines) and check_eval_generalization.py, which
reads the ``- **问题**：`` lines. The layout therefore mirrors the legacy modeling report.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from .core import canonical, digest

ACTIVE = ('queued', 'preparing', 'running', 'cancelling')
# Stems sort by time like the legacy eval-<YYYYMMDD-HHMMSS> names; the run prefix avoids same-second clashes.
STEM = re.compile(r'^eval-\d{8}-\d{6}-[a-f0-9]{6}$')
CHINA = timezone(timedelta(hours=8))
LIMITS = {'report.md': 8 * 1024 * 1024, 'answers.json': 8 * 1024 * 1024,
          'context.json': 2 * 1024 * 1024, 'suggestions.md': 256 * 1024}
MARKS = {'pass': '✅', 'partial': '⚠️', 'fail': '❌', 'manual': '📝'}
QUESTION_CHARS, REFERENCE_CHARS, ANSWER_CHARS, REASON_CHARS = 1000, 1500, 600, 1000
FULL_ANSWER_CHARS = 8000
STATUS_REASONS = {
    'pending': '本用例未执行（轮次已结束）',
    'unknown': '本用例未取得可核验结果：远端提交尚未对账',
    'executing': '本用例未取得可核验结果：远端执行尚未结束',
    'cancelled': '本用例已随评测停止而取消',
    'answer_ready': '已获得回答但尚未判分',
}
STOP_REASONS = {'cancelled': '评测已停止', 'failed': '评测失败', 'interrupted': '评测已中断', 'waiting_auth': '评测等待重新授权'}

# 「工具失败」摘要与旧链路一致：逐用例限量，抽取错误码、问题码与字段路径，并脱敏。
TOOL_FAILURES_PER_CASE = 5
TOOL_FAILURE_DETAIL_CHARS = 240
TOOL_FAILURE_CASE_CHARS = 1500
_JSON_CODE_RE = re.compile(r'"(?:code|error_code)"\s*:\s*"([A-Z][A-Z0-9_]{2,79})"')
_ERROR_CODE_RE = re.compile(
    r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:FAILED|ERROR|DENIED|TIMEOUT|STALLED|INVALID"
    r"|NOT_FOUND|UNAVAILABLE|EXCEEDED|REJECTED|MISSING)\b")
_ISSUE_RE = re.compile(r'"code"\s*:\s*"([a-z][a-z0-9_]*)"\s*,\s*"path"\s*:\s*"([^"]{1,240})"')
_JSON_MESSAGE_RE = re.compile(r'"message"\s*:\s*"([^"]{1,500})"')
_EXIT_CODE_RE = re.compile(r'exited with code (-?\d+)', re.IGNORECASE)
_SECRET_PATTERNS = (
    (re.compile(r'(?i)\bbearer\s+[\w.~+/=-]+'), 'Bearer ***'),
    (re.compile(r'\beyJ[\w-]+\.[\w-]+\.[\w-]+'), '***'),
    (re.compile(r'(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)'
                r'("?\s*[=:]\s*"?)[^\s"\',;}]+'), r'\1\2***'),
)


def redact(text):
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def clip(text, limit):
    text = str(text or '')
    return text if len(text) <= limit else text[:limit] + '…（已截断）'


def failed_first(calls, limit):
    """At most ``limit`` recorded tool calls in call order; when some must go, failed calls are kept first."""
    if len(calls) <= limit:
        return calls
    kept = sorted(range(len(calls)), key=lambda i: (calls[i].get('status') != 'failed', i))[:limit]
    return [calls[i] for i in sorted(kept)]


def tool_failure_line(failure):
    """One tool failure: tool · error code · issue codes and field paths · exit code · bounded detail."""
    tool = str(failure.get('tool_name') or '未知工具').replace('`', '').strip()[:64] or '未知工具'
    raw = str(failure.get('error') or '').strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    parsed = parsed if isinstance(parsed, dict) else {}
    error = parsed.get('error') if isinstance(parsed.get('error'), dict) else parsed
    code = str(error.get('code') or error.get('error_code') or '').strip()
    issues = [f"{issue.get('code')} {issue.get('path') or ''}".strip()
              for issue in error.get('issues') or [] if isinstance(issue, dict) and issue.get('code')] \
        if isinstance(error.get('issues'), list) else []
    if parsed:
        top = parsed.get('error')
        pieces = (error.get('message'), top if isinstance(top, str) else '', error.get('stderr') or parsed.get('stderr'))
        message = ' '.join(str(v) for v in pieces if v)
    else:
        match = _JSON_MESSAGE_RE.search(raw)
        message = match.group(1) if match else raw
    if not code:
        match = _JSON_CODE_RE.search(raw) or _ERROR_CODE_RE.search(raw)
        code = (match.group(1) if match and match.re is _JSON_CODE_RE else match.group(0)) if match else ''
    if not issues:
        issues = [f'{issue} {path}' for issue, path in _ISSUE_RE.findall(raw)]
    parts = [f'`{tool}`']
    if code:
        parts.append(f'`{code[:80]}`')
    if issues:
        parts.append('问题：' + '、'.join(f"`{redact(issue).replace('`', '')}`" for issue in list(dict.fromkeys(issues))[:6]))
    exit_code = _EXIT_CODE_RE.search(raw)
    if exit_code:
        parts.append(f'退出码 {exit_code.group(1)}')
    detail = ' '.join(redact(message).split())
    if detail:
        parts.append('详情：' + (detail[:TOOL_FAILURE_DETAIL_CHARS] + '…' if len(detail) > TOOL_FAILURE_DETAIL_CHARS else detail))
    return ' · '.join(parts)


def tool_failure_lines(trace):
    """Nested list lines of one case's 「工具失败」 section; empty when there is nothing to report."""
    if not isinstance(trace, dict):
        return []
    failures = [item for item in trace.get('tool_failures') or [] if isinstance(item, dict)]
    lines = []
    if failures:
        counted = {}
        for failure in failures:
            line = tool_failure_line(failure)
            counted[line] = counted.get(line, 0) + 1
        for line, times in list(counted.items())[:TOOL_FAILURES_PER_CASE]:
            lines.append(f'  - {line}' + (f'（×{times}）' if times > 1 else ''))
        if len(counted) > TOOL_FAILURES_PER_CASE:
            lines.append(f'  - …另有 {len(counted) - TOOL_FAILURES_PER_CASE} 类失败未列出')
        if trace.get('tool_calls'):
            lines.append(f"  - 本用例共调用工具 {trace['tool_calls']} 次，失败 {len(failures)} 次")
    if trace.get('status') == 'unavailable':
        reason = ' '.join(redact(str(trace.get('error') or '原因未知')).split())[:160]
        lines.append(f'  - 执行轨迹未取到（{reason}），工具失败情况未知')
    kept, used = [], 0
    for line in lines:
        if used + len(line) > TOOL_FAILURE_CASE_CHARS:
            kept.append('  - …（已截断）')
            break
        kept.append(line)
        used += len(line)
    return kept


def outcome(case):
    """Legacy verdict of one case: pass/partial/fail, manual (no reference) or error."""
    judgement = case.get('override') or case.get('judgement')
    if judgement and case.get('answer'):
        reason = judgement.get('reason') or ''
        return judgement['verdict'], reason + ('（人工改判）' if case.get('override') else '')
    if case.get('status') == 'manual' and case.get('answer'):
        return 'manual', '无参考答案，待人工判定'
    error = case.get('error') if isinstance(case.get('error'), dict) else {}
    message = str(error.get('message') or '').strip()
    if case.get('status') == 'judge_error':
        return 'error', '判分失败：' + (message or '判分模型未返回结果')
    return 'error', message or STATUS_REASONS.get(case.get('status'), '本用例未取得有效回答或判分')


def summarize(run):
    verdicts = [outcome(case)[0] for case in run['cases']]
    counts = {key: verdicts.count(key) for key in ('pass', 'partial', 'fail', 'error', 'manual')}
    valid = counts['pass'] + counts['partial'] + counts['fail']
    score = round((counts['pass'] + counts['partial'] * 0.5) * 100 / valid) if valid else None
    technical = run['status'] != 'completed' or bool(counts['error']) or not verdicts
    result_status = 'unavailable' if not valid else 'partial' if technical else 'complete'
    if run.get('origin') == 'consumer_feedback':
        # Feedback is what users reported, not a sample of questions: it has no score.
        score, result_status = None, 'complete' if run['status'] == 'completed' else 'unavailable'
    return dict(total=len(verdicts), **counts, valid=valid, score=score, result_status=result_status)


def user_suggestions(run):
    """Suggestions consumer users made about the package, kept with a feedback run."""
    return list((run.get('feedback') or {}).get('suggestions') or [])


def has_results(run):
    if run.get('origin') == 'consumer_feedback':
        return run['status'] == 'completed' and bool(run['cases'] or user_suggestions(run))
    return any(case.get('answer') or case.get('status') in ('execution_error', 'judge_error') for case in run['cases'])


def fingerprint(run):
    """Changes whenever the report would change: run status, answers, judgements and overrides."""
    return digest([run['status'], [[c.get('status'), bool(c.get('answer')), c.get('judgement'), c.get('override'),
                                    (c.get('error') or {}).get('message') if isinstance(c.get('error'), dict) else None]
                                   for c in run['cases']]])


def new_stem(run_id, now):
    return 'eval-' + datetime.fromtimestamp(now, CHINA).strftime('%Y%m%d-%H%M%S') + '-' + run_id[:6]


def nested_quote(text):
    return '\n'.join(f'  > {line}' if line else '  >' for line in (str(text).splitlines() or ['（无）']))


def render_report(run, stem, now):
    summary = summarize(run)
    base = run.get('base_ref') or {}
    ran = run.get('package_ref') or {}
    version = base.get('version') or ran.get('version') or ''
    lines = [f"# 数据评测报告 — {datetime.fromtimestamp(now, CHINA).strftime('%Y-%m-%d %H:%M')}", '',
             '> 评测方式：独立评测服务从 Hub 固定版本取包，由 consumer 智能体逐用例问答，LLM Judge 判分',
             f"> 轮次：{run['id']}（评测服务的评测轮次，可在评测页「评测任务」中追溯）"]
    if version:
        lines.append(f'> 知识包版本：{version}' + (f"；实际运行评测快照 {ran['version']}（mapping 由评测数据源生成）"
                                                if ran.get('version') and ran['version'] != version else ''))
    if run.get('data_source_name'):
        lines.append(f"> 数据源：{run['data_source_name']}")
    if run.get('eval_set_name'):
        lines.append(f"> 评测集：{run['eval_set_name']}")
    if run['status'] != 'completed':
        error = run.get('error') if isinstance(run.get('error'), dict) else {}
        reason = str(error.get('message') or run.get('message') or STOP_REASONS.get(run['status'], run['status']))
        executed = sum(1 for case in run['cases'] if case.get('answer'))
        lines.append(f"> ⚠️ 轮次异常终止：{' '.join(reason.split())[:300]}（已执行 {executed}/{summary['total']} 条，"
                     '未执行的用例计为异常）')
    score = summary['score']
    technical = summary['result_status'] != 'complete'
    outcome_text = {'unavailable': '未形成有效评测', 'partial': '部分有效，存在技术异常', 'complete': '评测完成'}[summary['result_status']]
    counts = f"- 通过 {summary['pass']} / 部分 {summary['partial']} / 失败 {summary['fail']} / 异常 {summary['error']}"
    lines += ['', '## 总览', '', f"- 用例：{summary['total']}",
              counts + (f" / 待人工判定 {summary['manual']}" if summary['manual'] else ''),
              f'- 结果状态：{outcome_text}',
              f"- 有效判分：{summary['valid']}/{summary['total']}；技术异常不计为业务答错，不进入得分分母。"
              + ('待人工判定的用例没有参考答案，同样不计入。' if summary['manual'] else ''),
              (f"- {'有效用例得分' if technical else '综合得分'}：**{score}**（仅 {summary['valid']} 条有效用例，pass=1、partial=0.5 加权）"
               if score is not None else '- 得分：未形成有效评测（不是 0 分）'),
              '', '## 逐用例明细', '']
    for index, case in enumerate(run['cases'], 1):
        verdict, reason = outcome(case)
        answer = str(case.get('answer') or '')
        display = answer[:ANSWER_CHARS]
        # Outside the answer quote: these describe the answer, they are not part of it.
        meta = ['', f"  - **原始回答长度**：{len(answer)} 字符；是否截断：{'是' if len(answer) > len(display) else '否'}"]
        if len(answer) > len(display):
            meta.append(f'  - **完整回答**：[按结果 ID 查看附件](reports/{stem}.answers.json#case-{index})')
        lines += [f"### {index}. {MARKS.get(verdict, '🛑')} {verdict}", '',
                  # One line: check_eval_generalization.py collects the corpus from these lines.
                  f"- **问题**：{clip(' '.join(str(case.get('question') or '').split()), QUESTION_CHARS)}", '',
                  '- **标准答案**：', '', nested_quote(clip(case.get('reference'), REFERENCE_CHARS) or '（无）'), '',
                  '- **智能体回答**：', '', nested_quote(display or '（无）'), '\n'.join(meta), '',
                  '- **判分理由**：', '', nested_quote(clip(reason, REASON_CHARS) or '（无）'), '']
        failures = [] if verdict == 'pass' else tool_failure_lines(case.get('trace'))
        if failures:
            lines += ['- **工具失败**：', '', *failures, '']
    return '\n'.join(lines)


def render_answers(run):
    """Full answers by result ID; each answer and the whole file stay bounded."""
    answers, used = [], 0
    for index, case in enumerate(run['cases'], 1):
        verdict, reason = outcome(case)
        answer = str(case.get('answer') or '')
        kept = answer[:FULL_ANSWER_CHARS] if used < LIMITS['answers.json'] // 2 else answer[:ANSWER_CHARS]
        used += len(kept.encode()) + len(str(case.get('question') or '').encode())
        answers.append({'resultId': f'case-{index}', 'question': str(case.get('question') or ''), 'finalAnswer': kept,
                        'originalLength': len(answer), 'truncated': len(kept) < len(answer),
                        'sha256': hashlib.sha256(answer.encode('utf-8')).hexdigest(), 'verdict': verdict, 'reason': reason})
    return json.dumps({'roundId': run['id'], 'answers': answers}, ensure_ascii=False, indent=2)


def tables(store, run):
    """Data source table headers only, never row values; at most 100 tables of 256 columns."""
    snapshot = (run.get('source') or {}).get('tables_snapshot')
    if not snapshot:
        return {}
    value = store.decrypt(snapshot)
    return {name: list(table.get('columns') or [])[:256] for name, table in list(value.items())[:100] if isinstance(table, dict)}


def render_context(store, run, stem, revision, summary):
    base = run.get('base_ref') or {}
    ran = run.get('package_ref') or {}
    return canonical(dict(run_id=run['id'], revision=revision, stem=stem,
                          ontology_id=base.get('ontology_id') or ran.get('ontology_id'),
                          release_id=base.get('release_id') or ran.get('release_id'),
                          version=base.get('version') or ran.get('version'), package_version=ran.get('version'),
                          name=ran.get('name'), status=run['status'], finished_at=run.get('finished_at'),
                          summary=summary, tables=tables(store, run), origin=run.get('origin') or 'evaluation',
                          **({'feedback': {key: (run.get('feedback') or {}).get(key) for key in ('stats', 'window', 'excluded')}}
                             if run.get('origin') == 'consumer_feedback' else {})))


def fit(name, text):
    """Truncate an artifact at a line boundary so it never exceeds its documented size."""
    raw = text.encode('utf-8')
    if len(raw) <= LIMITS[name]:
        return text
    cut = raw[:LIMITS[name] - 64].decode('utf-8', 'ignore')
    return cut[:cut.rfind('\n')] + '\n\n> …（内容超过上限，已截断）\n'
