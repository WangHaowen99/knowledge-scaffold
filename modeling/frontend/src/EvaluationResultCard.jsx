import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, ChevronsDown, ChevronsUp } from 'lucide-react';
import MarkdownContent from './MarkdownContent.jsx';

const VERDICT_META = {
  pass: { className: 'tag-ok', label: '通过' },
  partial: { className: 'tag-draft', label: '部分' },
  fail: { className: 'tag-err', label: '失败' },
  error: { className: 'tag-err', label: '出错' },
};

const TOOL_LABELS = {
  business_data_search: '业务数据查询',
  ontology_context: '知识包上下文',
  execute_script: '脚本执行',
};

export function shouldExpandEvaluationResult(verdict) {
  return ['partial', 'fail', 'error'].includes(verdict);
}

export function defaultExpandedEvaluationIds(results) {
  return new Set(results.filter((item) => shouldExpandEvaluationResult(item.verdict)).map((item) => item.id));
}

export function toggleEvaluationResultExpansion(expandedIds, resultId) {
  const next = new Set(expandedIds);
  if (next.has(resultId)) next.delete(resultId);
  else next.add(resultId);
  return next;
}

function EvaluationMarkdown({ children, fallback }) {
  const value = String(children || '').trim();
  if (!value) return <div className="ev-answer-empty">{fallback}</div>;
  return <div className="ev-markdown"><MarkdownContent>{value}</MarkdownContent></div>;
}

function ToolExecutionTrace({ trace }) {
  const calls = trace?.toolCalls ?? [];
  let emptyText = '本轮未记录工具调用';
  if (trace?.status === 'unavailable') emptyText = '工具执行轨迹暂不可用';

  return (
    <section className="ev-trace" aria-label="工具执行轨迹">
      <div className="ev-section-title">工具执行轨迹</div>
      <div className="ev-section-hint">仅展示调用过的工具，不展示模型内部思考内容。</div>
      {calls.length > 0 ? (
        <ol className="ev-trace-list">
          {calls.map((tool, index) => (
            <li key={`${tool}-${index}`}>{TOOL_LABELS[tool] || tool}</li>
          ))}
        </ol>
      ) : <div className="ev-trace-empty">{emptyText}</div>}
    </section>
  );
}

export default function EvaluationResultCard({
  result,
  trace,
  open,
  onToggle,
  onOverride,
  index,
  showTrace = true,
}) {
  const [overrideTarget, setOverrideTarget] = useState('');
  const [overrideReason, setOverrideReason] = useState('');
  const [overrideBusy, setOverrideBusy] = useState(false);
  const [overrideError, setOverrideError] = useState('');
  const meta = VERDICT_META[result.verdict] ?? { className: 'tag-lang', label: '—' };
  const bodyId = `evaluation-result-${result.id}`;

  return (
    <article className={`ev-result ev-result-${result.verdict || 'unknown'} ${open ? 'is-open' : ''}`}>
      <button
        type="button"
        className="ev-result-head"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
      >
        <span className={`tag ${meta.className}`}>{meta.label}{result.overridden ? '·改' : ''}</span>
        {Number.isInteger(index) && <span className="ev-result-index">{index + 1}.</span>}
        <span className="ev-question-summary">{result.question}</span>
        <span className="ev-result-chevron" aria-hidden="true">
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </span>
      </button>

      {open && (
        <div className="ev-result-body" id={bodyId}>
          <div className="ev-answer-grid">
            <section className="ev-answer-panel ev-answer-standard" aria-label="标准答案">
              <div className="ev-section-title">标准答案</div>
              <div className="ev-answer-content">
                <EvaluationMarkdown fallback="（未配置标准答案）">{result.groundTruth}</EvaluationMarkdown>
              </div>
            </section>
            <section className="ev-answer-panel ev-answer-evaluated" aria-label="评测回答">
              <div className="ev-section-title">评测回答</div>
              <div className="ev-answer-content">
                <EvaluationMarkdown fallback="（无回答）">{result.finalAnswer}</EvaluationMarkdown>
              </div>
            </section>
          </div>

          <section className="ev-reason" aria-label="判分说明">
            <div className="ev-section-title">判分说明</div>
            <div className="ev-reason-content">
              <EvaluationMarkdown fallback="（暂无判分说明）">{result.reason}</EvaluationMarkdown>
            </div>
          </section>

          {result.overridden && (
            <section className="ev-override-audit" aria-label="人工改判记录">
              <div className="ev-section-title">人工改判记录</div>
              <div>原判定：{VERDICT_META[result.originalVerdict]?.label ?? result.originalVerdict ?? '—'}</div>
              <div>理由：{result.overrideReason || '—'}</div>
              <div>操作人：{result.overrideActor || '—'}{result.overriddenAt ? ` · ${new Date(result.overriddenAt).toLocaleString('zh-CN')}` : ''}</div>
            </section>
          )}

          {showTrace && <ToolExecutionTrace trace={trace} />}

          {typeof onOverride === 'function' && (result.verdict === 'error' ? (
            <div className="ev-override-disabled">运行错误需要先修复环境并重试，不能人工改判。</div>
          ) : <>
            <div className="ev-override">
              <span>人工改判</span>
              {['pass', 'partial', 'fail'].map((verdict) => (
                <button
                  key={verdict}
                  type="button"
                  className={`btn ${overrideTarget === verdict ? 'on' : ''}`}
                  disabled={result.verdict === verdict || overrideBusy}
                  onClick={() => { setOverrideTarget(verdict); setOverrideError(''); }}
                >
                  {VERDICT_META[verdict].label}
                </button>
              ))}
            </div>
            {overrideTarget && <div className="ev-override-form">
              <label htmlFor={`override-reason-${result.id}`}>说明改判理由</label>
              <textarea id={`override-reason-${result.id}`} rows={2} value={overrideReason}
                placeholder="例如：标准答案允许该同义表达"
                onChange={(event) => setOverrideReason(event.target.value)} />
              {overrideError && <div className="lerr">{overrideError}</div>}
              <div><button type="button" className="btn btn-p" disabled={overrideBusy || !overrideReason.trim()}
                onClick={async () => {
                  setOverrideBusy(true); setOverrideError('');
                  try {
                    await onOverride(overrideTarget, overrideReason.trim());
                    setOverrideTarget(''); setOverrideReason('');
                  } catch (error) {
                    setOverrideError(error instanceof Error ? error.message : String(error));
                  } finally { setOverrideBusy(false); }
                }}>确认改判</button>
                <button type="button" className="btn" disabled={overrideBusy}
                  onClick={() => { setOverrideTarget(''); setOverrideReason(''); setOverrideError(''); }}>取消</button></div>
            </div>}
          </>)}
        </div>
      )}
    </article>
  );
}

export function EvaluationResultsList({ results, traces = {}, onOverride, showTrace = true }) {
  const [expandedIds, setExpandedIds] = useState(() => defaultExpandedEvaluationIds(results));
  const seenIdsRef = useRef(new Set(results.map((item) => item.id)));
  const bulkModeRef = useRef('default');

  useEffect(() => {
    const nextIds = new Set(results.map((item) => item.id));
    const newlyExpanded = results
      .filter((item) => !seenIdsRef.current.has(item.id) && shouldExpandEvaluationResult(item.verdict))
      .map((item) => item.id);
    seenIdsRef.current = nextIds;
    setExpandedIds((current) => {
      if (bulkModeRef.current === 'expanded') return nextIds;
      if (bulkModeRef.current === 'collapsed') return new Set();
      const next = new Set([...current].filter((id) => nextIds.has(id)));
      newlyExpanded.forEach((id) => next.add(id));
      return next;
    });
  }, [results]);

  if (!results.length) return null;
  const allExpanded = results.every((item) => expandedIds.has(item.id));
  const allCollapsed = results.every((item) => !expandedIds.has(item.id));

  return (
    <div className="ev-results">
      <div className="ev-results-toolbar" aria-label="评测结果展开操作">
        <span>{results.length} 条评测结果</span>
        <div>
          <button type="button" disabled={allExpanded} title="展开全部评测回答"
            onClick={() => { bulkModeRef.current = 'expanded'; setExpandedIds(new Set(results.map((item) => item.id))); }}>
            <ChevronsDown size={14} />展开全部
          </button>
          <button type="button" disabled={allCollapsed} title="收起全部评测回答"
            onClick={() => { bulkModeRef.current = 'collapsed'; setExpandedIds(new Set()); }}>
            <ChevronsUp size={14} />收起全部
          </button>
        </div>
      </div>
      {results.map((item, index) => (
        <EvaluationResultCard
          key={item.id}
          result={item}
          index={index}
          trace={traces[item.id]}
          showTrace={showTrace}
          open={expandedIds.has(item.id)}
          onToggle={() => setExpandedIds((current) => {
            bulkModeRef.current = 'custom';
            return toggleEvaluationResultExpansion(current, item.id);
          })}
          onOverride={typeof onOverride === 'function'
            ? (verdict, reason) => onOverride(item, verdict, reason)
            : undefined}
        />
      ))}
    </div>
  );
}
