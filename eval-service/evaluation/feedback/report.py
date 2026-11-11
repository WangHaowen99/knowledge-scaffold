"""The report of a feedback run: which consumer feedback this optimization is based on.

Title, header and overview describe the feedback instead of an evaluation; the per-case section keeps the
evaluation report's layout and marks, so onto-modeling's evaluation optimization, /ontology-evolution and
check_eval_generalization.py read it unchanged. Submitters are never named, only counted.
"""
from datetime import datetime

from ..artifacts import ANSWER_CHARS, CHINA, MARKS, QUESTION_CHARS, REASON_CHARS, REFERENCE_CHARS, clip, nested_quote, \
    outcome, tool_failure_lines

TYPES = (('error_qa_user_vote', '用户投票'), ('error_qa_user_correction', '追问纠错'), ('error_qa_system_rule', '系统识别'))
REASONS = {'wrong_answer': '答案错误', 'missing_data': '数据缺失', 'wrong_process': '过程错误', 'other': '其他'}
EXCLUDED = {'runtime_error': '运行环境问题', 'insufficient_info': '信息不足（只有 👎，未达门槛）', 'misjudged': '用户误判',
            'resolved_by_newer_version': '新版本已修复'}


def day(seconds):
    return datetime.fromtimestamp(seconds, CHINA).strftime('%Y-%m-%d')


def render(run, stem, now):
    feedback = run.get('feedback') or {}
    stats = feedback.get('stats') or {}
    by_type = stats.get('by_type') or {}
    window = feedback.get('window') or {}
    ref = run.get('package_ref') or {}
    errors = sum(by_type.get(key, 0) for key, _ in TYPES) + by_type.get('vote_down', 0)
    detail = ' · '.join(f'{label} {by_type[key]}' for key, label in (('vote_down', '用户投票'), *TYPES[1:]) if by_type.get(key))
    reasons = ' · '.join(f"{REASONS.get(key, key)} {count}" for key, count in (stats.get('reasons') or {}).items() if count)
    suggestions = feedback.get('suggestions') or []
    lines = [f"# 消费反馈评测报告 — {datetime.fromtimestamp(now, CHINA).strftime('%Y-%m-%d %H:%M')}", '',
             '> 评测方式：本次没有执行评测用例。以下是用户在消费服务中实际问答时提交的反馈，作为本次优化的依据。',
             f"> 轮次：{run['id']}（消费反馈轮次，可在评测「用户反馈」页追溯）",
             f"> 知识包版本：{ref.get('version') or '（未注明）'}"]
    if window:
        lines.append(f"> 反馈时间：{day(window['from'])} 至 {day(window['to'])}")
    lines += [f"> 数据源：{run.get('data_source_name') or '消费服务真实问答'}", '', '## 总览', '',
              f"- 反馈：{stats.get('signals', 0)} 条，来自 {stats.get('users', 0)} 位用户；问题簇 {len(feedback.get('clusters') or [])} 个",
              f"- 错误问答 {errors}" + (f"（{detail}）" if detail else '') + f"；用户建议 {len(suggestions)}；"
              f"👍 {by_type.get('upvote', 0)}（不生成建议）"]
    if reasons:
        lines.append(f'- 👎 原因：{reasons}')
    lines += ['- 本报告不计算得分', '', '## 逐用例明细', '']
    if not run['cases']:
        lines += ['本次没有可分析的错误问答，优化依据见下方「用户建议」。', '']
    for index, case in enumerate(run['cases'], 1):
        verdict, reason = outcome(case)
        meta = case.get('feedback_meta') or {}
        note = [f"  > - **反馈**：{meta.get('kind', '用户反馈')} · {meta.get('users', 1)} 位用户"
                + (f" · 最近 {day(meta['latest'])}" if meta.get('latest') else '')]
        if meta.get('original_question'):
            note.insert(0, f"  > - **原始问题**：{clip(' '.join(meta['original_question'].split()), 500)}")
        if meta.get('mostly_upvoted'):
            note.append('  > - 👍 明显多于 👎，可能只是个别用户不满意')
        lines += [f"### {index}. {MARKS.get(verdict, '🛑')} {verdict}", '',
                  f"- **问题**：{clip(' '.join(str(case.get('question') or '').split()), QUESTION_CHARS)}", '',
                  '- **标准答案**：', '', nested_quote(clip(case.get('reference'), REFERENCE_CHARS) or '（未提供）'), '',
                  '- **智能体回答**：', '', nested_quote(clip(case.get('answer'), ANSWER_CHARS) or '（无）'), '  >', *note, '',
                  '- **判分理由**：', '', nested_quote(clip(reason, REASON_CHARS) or '（无）'), '']
        failures = [] if verdict == 'pass' else tool_failure_lines(case.get('trace'))
        if failures:
            lines += ['- **工具失败**：', '', *failures, '']
    if suggestions:
        lines += ['## 用户建议', '', '| # | 建议 | 关联 | 用户数 |', '|---|---|---|---|']
        for number, item in enumerate(suggestions, 1):
            scope = item.get('scope') or '—'
            scope = scope.replace('operation:', '操作：').replace('entity:', '对象：')
            text = ' '.join(str(item.get('content') or '').split()).replace('|', '\\|')
            lines.append(f"| {number} | {clip(text, 600)} | {scope} | {item.get('users', 1)} |")
        lines.append('')
    excluded = {key: count for key, count in (feedback.get('excluded') or {}).items() if count}
    if excluded:
        lines += ['## 未纳入本次优化', '']
        lines += [f'- {EXCLUDED.get(key, key)} {count} 条' for key, count in excluded.items()]
        lines.append('')
    return '\n'.join(lines)
