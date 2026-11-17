// 旧版评测记录（ES-09～ES-12）：照 develop 左栏「旧版评测记录（历史链路）」和 EvalRunDetail「评测执行详情」。
// 旧运行器已随 ADR-0020 删除：没有「停止旧任务」「对账旧任务」；完整回答读报告附带的 answers.json；
// 用法：<LegacyEvaluations pid={pid} open={…} onClose={…} />
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { Button, Modal } from '../../ui/index.jsx';
import { Doc, useAnswerPreview, useLoad } from './EvaluationDoc.jsx';
import {
  POLL_MS, STATUS_TEXT, TERMINAL, VERDICTS, VERDICT_TEXT, formatDuration, phaseIndex, verdictCounts, PHASES,
} from './evaluationText.js';
import '../../styles/evaluation.css';

function PhaseTrack({ st }) {
  const idx = phaseIndex(st);
  const halted = ['failed', 'stopped', 'interrupted'].includes(st.status);
  return (
    <div className="erd-phases">
      {PHASES.map(([key, label], i) => {
        let state = 'todo';
        if (i < idx) state = 'done';
        else if (i === idx && st.status === 'running') state = 'current';
        else if (i === idx && halted) state = st.status === 'failed' ? 'failed' : 'halted';
        return (
          <div key={key} className={`erd-phase is-${state}`}>
            <span className="erd-phase-node">{state === 'done' ? '✓' : state === 'failed' ? '✕' : ''}</span>
            <span className="erd-phase-label">{label}</span>
          </div>
        );
      })}
    </div>
  );
}

function Headline({ st, finished, total }) {
  const score = st.summary?.score;
  if (score != null && st.status !== 'running') {
    return (
      <div className="erd-headline">
        <span className="erd-headline-value">{score}<small>分</small></span>
        <span className="erd-headline-caption">
          {st.summary?.result_status === 'partial'
            ? `有效用例得分 · 仅 ${st.summary.valid}/${total} 条有效，非全量结果`
            : `综合得分 · 共 ${total} 条用例`}
        </span>
      </div>
    );
  }
  if (st.summary?.result_status === 'unavailable') {
    return (
      <div className="erd-headline">
        <span className="erd-headline-value">—</span>
        <span className="erd-headline-caption">未形成有效评测 · 技术异常不代表业务答错</span>
      </div>
    );
  }
  const percent = total ? Math.round((finished * 100) / total) : 0;
  return (
    <div className="erd-headline">
      <span className="erd-headline-value">{finished}<small>/ {total}</small></span>
      <span className="erd-headline-caption">已完成用例 · {percent}%</span>
    </div>
  );
}

function MetaLine({ st }) {
  const elapsed = st.started_at && st.status !== 'interrupted' ? formatDuration((st.ts || st.started_at) - st.started_at) : '';
  const items = [
    st.data_source_name && ['数据源', st.data_source_name],
    st.eval_set_name && ['评测集', st.eval_set_name],
    elapsed && ['用时', elapsed],
  ].filter(Boolean);
  const graph = st.graph_import;
  const unbound = st.unbound_objects || [];
  if (!items.length && !graph && !unbound.length) return null;
  return (
    <div className="erd-meta">
      {items.map(([label, value]) => (
        <span key={label} className="erd-meta-item"><span className="erd-meta-label">{label}</span><span>{value}</span></span>
      ))}
      {unbound.length > 0 ? (
        <span className="erd-meta-item" data-pop={`数据绑定未过检、本轮按未绑定处理：${unbound.join('、')}`}>
          <span className="erd-meta-label">未查库对象</span><span>{unbound.length} 个</span>
        </span>
      ) : null}
      {graph ? (
        <span className="erd-meta-item" data-pop={(graph.notes || []).join('\n') || undefined}>
          <span className="erd-meta-label">图数据</span>
          <span>
            {graph.entities != null && `实体 ${graph.entities} · `}
            实例 {graph.nodes} · 关系 {graph.relationships}
            {graph.relations_skipped ? ` · 关系未转换 ${graph.relations_skipped}` : ''}
            {graph.notes?.length ? `（${graph.notes.length} 条提示）` : ''}
          </span>
        </span>
      ) : null}
      {graph?.notes?.length > 0 ? (
        <details className="erd-meta-item">
          <summary>查看图关系提示（{graph.notes.length} 条）</summary>
          <ul>{graph.notes.map((note, index) => <li key={index}>{note}</li>)}</ul>
        </details>
      ) : null}
    </div>
  );
}

function CaseRow({ item, index, open, onToggle, onLoadFullAnswer }) {
  const verdict = item.verdict || 'pending';
  const expandable = TERMINAL.has(verdict) && Boolean(item.answer || item.reason);
  return (
    <div className={`erd-row is-${verdict}${open ? ' is-open' : ''}`}>
      <div className="erd-row-head" role={expandable ? 'button' : undefined} tabIndex={expandable ? 0 : undefined}
        onClick={expandable ? onToggle : undefined} onKeyDown={expandable ? (e) => e.key === 'Enter' && onToggle() : undefined}>
        <span className="erd-row-index">{String(index + 1).padStart(2, '0')}</span>
        <span className={`erd-glyph is-${verdict}`} />
        <span className="erd-row-question" title={item.question}>{item.question}</span>
        <span className="erd-row-state">{VERDICT_TEXT[verdict] || verdict}</span>
      </div>
      {expandable && open ? (
        <div className="erd-row-fields">
          {item.answer ? (
            <>
              <span className="erd-row-label">回答摘要</span>
              <span>
                {item.answer}
                {item.is_truncated ? (
                  <span className="ev-link erd-answer-more" role="button" tabIndex={0} onClick={() => onLoadFullAnswer(item)}>
                    查看完整回答（{item.original_length} 字符）
                  </span>
                ) : null}
              </span>
            </>
          ) : null}
          {item.reason ? <><span className="erd-row-label">判分</span><span>{item.reason}</span></> : null}
        </div>
      ) : null}
    </div>
  );
}

function DetailBody({ pid, st, onOpenReport }) {
  const { api } = useApp();
  const [filter, setFilter] = useState(null);
  const [openIndex, setOpenIndex] = useState(null);
  const cases = st.cases || [];
  const counts = verdictCounts(cases);
  const finished = VERDICTS.reduce((sum, [key]) => sum + counts[key], 0);
  const running = st.status === 'running';
  const notice = ['failed', 'stopped', 'interrupted'].includes(st.status) && st.message;
  const aliasConflict = st.error_code === 'DATA_SOURCE_PACKAGE_FIELD_ALIAS_CONFLICT' ? (st.error_details?.conflicts || st.conflicts || []) : [];
  const stem = (st.report || '').replace(/^reports\//, '').replace(/\.md$/, '');
  const preview = useAnswerPreview((resultId) => api.legacyEvaluationAnswer(pid, stem, resultId), { title: '原始最终答复', failure: '' });
  const loadFullAnswer = (item) => {
    if (!item.result_id || !stem) {
      toast('完整回答证据不可用', 'alert');
      return;
    }
    preview.open(item.result_id);
  };
  const visible = cases.map((item, index) => ({ item, index })).filter(({ item }) => !filter || item.verdict === filter);
  return (
    <div className="erd-body">
      <MetaLine st={st} />
      <section className="erd-panel">
        <PhaseTrack st={st} />
        {running && st.message ? <div className="erd-live">{st.message}</div> : null}
      </section>
      {notice ? (
        <div className={`erd-notice is-${st.status}`}>
          <div>{aliasConflict.length ? '知识包存在字段名称或别名冲突，请修正后重新评测。' : st.message}</div>
          {aliasConflict.length > 0 ? (
            <ul>
              {aliasConflict.map((item) => (
                <li key={`${item.path || item.object_key || ''}:${item.display_name || ''}`}>
                  对象“{item.object_title || item.object_key || '未命名对象'}”的名称“{item.display_name || '未命名'}”同时用于字段：
                  {Array.isArray(item.field_keys) && item.field_keys.length ? item.field_keys.join('、') : '（字段列表不可用）'}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      <section className="erd-panel erd-overview">
        <Headline st={st} finished={finished} total={cases.length} />
        <div className="erd-stats">
          {VERDICTS.map(([key, label]) => (
            <button key={key} type="button" className={`erd-stat${filter === key ? ' is-active' : ''}`} aria-pressed={filter === key}
              onClick={() => setFilter(filter === key ? null : key)}>
              <span className="erd-stat-label"><span className={`erd-glyph is-${key}`} />{label}</span>
              <span className="erd-stat-value">{counts[key]}</span>
            </button>
          ))}
        </div>
        <div className="erd-bar">
          <div className="erd-bar-fill" style={{ width: `${cases.length ? (finished * 100) / cases.length : 0}%` }}>
            {VERDICTS.map(([key]) => <span key={key} className={`erd-bar-seg is-${key}`} style={{ width: `${finished ? (counts[key] * 100) / finished : 0}%` }} />)}
          </div>
        </div>
      </section>
      <div className="erd-list-head">
        <span>用例{filter ? ` · ${VERDICT_TEXT[filter]} ${visible.length}` : ` ${cases.length}`}</span>
        {filter ? <span className="ev-link" role="button" tabIndex={0} onClick={() => setFilter(null)}>显示全部</span> : null}
      </div>
      <div className="erd-list">
        {visible.map(({ item, index }) => (
          <CaseRow key={index} item={item} index={index} open={openIndex === index}
            onToggle={() => setOpenIndex(openIndex === index ? null : index)} onLoadFullAnswer={loadFullAnswer} />
        ))}
        {!cases.length ? <div className="erd-empty">启动评测后，这里会实时展示每条用例的执行状态</div> : null}
        {cases.length > 0 && !visible.length ? <div className="erd-empty">没有「{VERDICT_TEXT[filter]}」的用例</div> : null}
      </div>
      {st.report && !running ? (
        <div className="erd-footer">
          <Button onClick={() => onOpenReport(stem)}>查看完整报告</Button>
        </div>
      ) : null}
      {preview.modal}
    </div>
  );
}

function LegacyReport({ pid, stem, onClose }) {
  const { api } = useApp();
  const state = useLoad(() => api.legacyEvaluation(pid, stem), stem);
  const preview = useAnswerPreview((resultId) => api.legacyEvaluationAnswer(pid, stem, resultId), { title: '原始最终答复', failure: '' });
  return (
    <Modal open={!!stem} onClose={onClose} w="min(1280px, calc(100vw - 96px))" title={state.data?.path || stem}>
      {state.loading ? <div className="ev-loading"><span className="spin" /></div> : null}
      {state.error ? <div className="sm">{state.error}</div> : null}
      {state.data ? <Doc content={state.data.content} onOpenAnswer={preview.open} /> : null}
      {preview.modal}
    </Modal>
  );
}

// open / onClose 由页面控制（发布页「⋯」菜单里的「旧版评测记录」）
export function LegacyEvaluations({ pid, open, onClose }) {
  const { api } = useApp();
  const [info, setInfo] = useState(null);
  const [report, setReport] = useState('');
  // 打开期间定时重读（develop 的执行详情一样轮询）
  useEffect(() => {
    if (!open || !pid) return undefined;
    let stop = false;
    const pull = () => api.legacyEvaluations(pid).then((d) => { if (!stop) setInfo(d); }).catch(() => {});
    pull();
    const timer = setInterval(pull, POLL_MS);
    return () => { stop = true; clearInterval(timer); };
  }, [api, pid, open]);
  const st = info?.state || {};
  const status = st.status || 'idle';
  return (
    <>
      <Modal open={open} onClose={onClose} w={880} className="erd-modal"
        title={<span className="row">评测执行详情{STATUS_TEXT[status] ? <span className={`erd-status is-${status}`}>{STATUS_TEXT[status]}</span> : null}</span>}>
        <DetailBody pid={pid} st={st} onOpenReport={(stem) => setReport(stem)} />
      </Modal>
      {report ? <LegacyReport pid={pid} stem={report} onClose={() => setReport('')} /> : null}
    </>
  );
}
