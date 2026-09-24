import { Button, Modal, Tooltip } from 'antd';
import { useEffect, useRef, useState } from 'react';

// 数据评测 · 执行详情：编排阶段 + 逐用例进度。
// 配色只用主题色、错误色与中性灰；判分靠图形区分（实心勾 / 半圆 / 叉 / 感叹号），
// 不靠色相堆叠，暗色模式与主题色切换自动适配

const PHASES = [
  ['package', '打包'],
  ['import', '导入'],
  ['ds', '数据源'],
  ['cases', '用例'],
  ['evaluate', '执行'],
];
const DS_PHASES = ['data_import', 'data_source', 'graph_import'];
const VERDICTS = [
  ['pass', '通过'],
  ['partial', '部分'],
  ['fail', '失败'],
  ['error', '异常'],
];
const VERDICT_TEXT = {
  ...Object.fromEntries(VERDICTS),
  running: '执行中',
  pending: '待执行',
};
const TERMINAL = new Set(VERDICTS.map(([key]) => key));
const STATUS_TEXT = {
  running: '运行中',
  done: '已完成',
  failed: '异常终止',
  stopped: '已停止',
  interrupted: '已中断',
};
const INTRO_MS = 900;

const prefersReducedMotion = () =>
  typeof window !== 'undefined' &&
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

function phaseIndex(st) {
  if (st.status === 'done') return PHASES.length;
  const key = DS_PHASES.includes(st.phase) ? 'ds' : st.phase;
  return Math.max(0, PHASES.findIndex(([k]) => k === key));
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${m}:${s}` : `${m}:${s}`;
}

// 数字滚动到目标值（ease-out），首次挂载从 0 起跳
function useCountUp(target, duration = 700) {
  const [value, setValue] = useState(0);
  const current = useRef(0);
  useEffect(() => {
    const goal = Number(target) || 0;
    if (prefersReducedMotion()) {
      current.current = goal;
      setValue(goal);
      return undefined;
    }
    const from = current.current;
    const start = performance.now();
    let raf = 0;
    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration);
      current.current = from + (goal - from) * (1 - (1 - t) ** 3);
      setValue(current.current);
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration]);
  return Math.round(value);
}

// 运行中逐秒走表；终态用最后一次状态写入时间收尾
function useElapsed(st) {
  const running = st.status === 'running';
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [running]);
  if (!st.started_at || st.status === 'interrupted') return '';
  return formatDuration((running ? now : st.ts || now) - st.started_at);
}

function VerdictGlyph({ verdict }) {
  const cls = `erd-glyph is-${verdict}`;
  if (verdict === 'pass') {
    return (
      <svg className={cls} viewBox="0 0 16 16" aria-hidden>
        <circle className="erd-glyph-disc" cx="8" cy="8" r="7" />
        <path className="erd-glyph-mark" d="M4.9 8.3l2 2 4.2-4.4" />
      </svg>
    );
  }
  if (verdict === 'partial') {
    return (
      <svg className={cls} viewBox="0 0 16 16" aria-hidden>
        <circle className="erd-glyph-ring" cx="8" cy="8" r="6.25" />
        <path className="erd-glyph-half" d="M8 1.75a6.25 6.25 0 0 1 0 12.5z" />
      </svg>
    );
  }
  if (verdict === 'fail') {
    return (
      <svg className={cls} viewBox="0 0 16 16" aria-hidden>
        <circle className="erd-glyph-disc" cx="8" cy="8" r="7" />
        <path className="erd-glyph-mark" d="M5.7 5.7l4.6 4.6M10.3 5.7l-4.6 4.6" />
      </svg>
    );
  }
  if (verdict === 'error') {
    return (
      <svg className={cls} viewBox="0 0 16 16" aria-hidden>
        <circle className="erd-glyph-ring" cx="8" cy="8" r="6.25" />
        <path className="erd-glyph-mark" d="M8 4.7v4" />
        <circle className="erd-glyph-dot" cx="8" cy="11.1" r="0.9" />
      </svg>
    );
  }
  if (verdict === 'running') {
    return (
      <svg className={cls} viewBox="0 0 16 16" aria-hidden>
        <circle className="erd-glyph-track" cx="8" cy="8" r="6.25" />
        <path className="erd-glyph-arc" d="M8 1.75a6.25 6.25 0 0 1 6.25 6.25" />
      </svg>
    );
  }
  return (
    <svg className={cls} viewBox="0 0 16 16" aria-hidden>
      <circle className="erd-glyph-ring" cx="8" cy="8" r="6.25" />
    </svg>
  );
}

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
          <div
            key={key}
            className={`erd-phase is-${state}`}
            style={{ '--i': i }}
          >
            <span className="erd-phase-node">
              {state === 'done' && (
                <svg viewBox="0 0 12 12" aria-hidden>
                  <path d="M3 6.2l2 2 4-4.2" />
                </svg>
              )}
              {state === 'failed' && (
                <svg viewBox="0 0 12 12" aria-hidden>
                  <path d="M4 4l4 4M8 4l-4 4" />
                </svg>
              )}
            </span>
            <span className="erd-phase-label">{label}</span>
            {i < PHASES.length - 1 && <span className="erd-phase-line" />}
          </div>
        );
      })}
    </div>
  );
}

function StatCell({ verdict, label, count, active, onClick }) {
  const value = useCountUp(count);
  return (
    <button
      type="button"
      className={`erd-stat${active ? ' is-active' : ''}`}
      onClick={onClick}
      aria-pressed={active}
    >
      <span className="erd-stat-label">
        <VerdictGlyph verdict={verdict} />
        {label}
      </span>
      <span className="erd-stat-value">{value}</span>
    </button>
  );
}

function Headline({ st, finished, total }) {
  const score = st.summary?.score;
  const hasScore = score != null && st.status !== 'running';
  const big = useCountUp(hasScore ? score : finished);
  const percent = total ? Math.round((finished * 100) / total) : 0;
  if (hasScore) {
    return (
      <div className="erd-headline">
        <span className="erd-headline-value">
          {big}
          <small>分</small>
        </span>
        <span className="erd-headline-caption">综合得分 · 共 {total} 条用例</span>
      </div>
    );
  }
  return (
    <div className="erd-headline">
      <span className="erd-headline-value">
        {big}
        <small>/ {total}</small>
      </span>
      <span className="erd-headline-caption">已完成用例 · {percent}%</span>
    </div>
  );
}

function ProgressBar({ counts, total, running }) {
  const finished = VERDICTS.reduce((sum, [key]) => sum + counts[key], 0);
  const width = (n) => `${finished ? (n * 100) / finished : 0}%`;
  return (
    <div className={`erd-bar${running ? ' is-running' : ''}`}>
      <div
        className="erd-bar-fill"
        style={{ width: `${total ? (finished * 100) / total : 0}%` }}
      >
        {VERDICTS.map(([key]) => (
          <span key={key} className={`erd-bar-seg is-${key}`} style={{ width: width(counts[key]) }} />
        ))}
      </div>
    </div>
  );
}

function CaseRow({ item, index, open, onToggle, delay }) {
  const verdict = item.verdict || 'pending';
  const hasDetail = Boolean(item.answer || item.reason);
  const expandable = TERMINAL.has(verdict) && hasDetail;
  const onKeyDown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onToggle();
    }
  };
  return (
    <div
      className={`erd-row is-${verdict}${open ? ' is-open' : ''}${
        expandable ? ' is-expandable' : ''
      }`}
      style={{ '--d': `${delay}ms` }}
      role={expandable ? 'button' : undefined}
      tabIndex={expandable ? 0 : undefined}
      aria-expanded={expandable ? open : undefined}
      onClick={expandable ? onToggle : undefined}
      onKeyDown={expandable ? onKeyDown : undefined}
    >
      <div className="erd-row-head">
        <span className="erd-row-index">{String(index + 1).padStart(2, '0')}</span>
        <VerdictGlyph key={verdict} verdict={verdict} />
        <span className="erd-row-question" title={item.question}>
          {item.question}
        </span>
        <span className="erd-row-state">{VERDICT_TEXT[verdict] || verdict}</span>
        <span className="erd-row-caret" aria-hidden />
      </div>
      {expandable && (
        <div className="erd-row-detail">
          <div className="erd-row-detail-inner">
            <div className="erd-row-fields">
              {item.answer && (
                <>
                  <span className="erd-row-label">回答</span>
                  <span className="erd-row-answer">{item.answer}</span>
                </>
              )}
              {item.reason && (
                <>
                  <span className="erd-row-label">判分</span>
                  <span>{item.reason}</span>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function MetaLine({ st }) {
  const elapsed = useElapsed(st);
  const items = [
    st.data_source_name && ['数据源', st.data_source_name],
    st.eval_set_name && ['评测集', st.eval_set_name],
    elapsed && ['用时', elapsed],
  ].filter(Boolean);
  const graph = st.graph_import;
  if (!items.length && !graph) return null;
  return (
    <div className="erd-meta">
      {items.map(([label, value]) => (
        <span key={label} className="erd-meta-item">
          <span className="erd-meta-label">{label}</span>
          <span className="erd-meta-value">{value}</span>
        </span>
      ))}
      {graph && (
        <Tooltip title={(graph.notes || []).join('\n') || undefined}>
          <span className="erd-meta-item">
            <span className="erd-meta-label">图数据</span>
            <span className="erd-meta-value">
              节点 {graph.nodes} · 关系 {graph.relationships}
              {graph.notes?.length ? `（${graph.notes.length} 条提示）` : ''}
            </span>
          </span>
        </Tooltip>
      )}
    </div>
  );
}

function Overview({ st, counts, total, filter, onFilter }) {
  const finished = VERDICTS.reduce((sum, [key]) => sum + counts[key], 0);
  return (
    <section className="erd-panel erd-overview">
      <Headline st={st} finished={finished} total={total} />
      <div className="erd-stats">
        {VERDICTS.map(([key, label]) => (
          <StatCell
            key={key}
            verdict={key}
            label={label}
            count={counts[key]}
            active={filter === key}
            onClick={() => onFilter(filter === key ? null : key)}
          />
        ))}
      </div>
      <ProgressBar counts={counts} total={total} running={st.status === 'running'} />
    </section>
  );
}

// 用例列表：开场逐行错峰浮入；执行中的用例自动滚入视野（按判分筛选时不打扰）
function CaseList({ cases, filter, onClearFilter }) {
  const [openIndex, setOpenIndex] = useState(null);
  const [intro, setIntro] = useState(true);
  useEffect(() => {
    const timer = setTimeout(() => setIntro(false), INTRO_MS);
    return () => clearTimeout(timer);
  }, []);

  const listRef = useRef(null);
  const runningIndex = cases.findIndex((c) => c.verdict === 'running');
  useEffect(() => {
    if (runningIndex < 0 || filter) return;
    const row = listRef.current?.querySelector('.erd-row.is-running');
    row?.scrollIntoView({
      block: 'nearest',
      behavior: prefersReducedMotion() ? 'auto' : 'smooth',
    });
  }, [runningIndex, filter]);

  const visible = cases
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => !filter || item.verdict === filter);
  const filteredEmpty = cases.length > 0 && visible.length === 0;
  return (
    <>
      <div className="erd-list-head">
        <span>
          用例
          {filter ? ` · ${VERDICT_TEXT[filter]} ${visible.length}` : ` ${cases.length}`}
        </span>
        {filter && (
          <button type="button" className="erd-link" onClick={onClearFilter}>
            显示全部
          </button>
        )}
      </div>
      <div className="erd-list" ref={listRef}>
        {visible.map(({ item, index }, order) => (
          <CaseRow
            key={index}
            item={item}
            index={index}
            open={openIndex === index}
            delay={intro ? Math.min(order * 35, 420) : 0}
            onToggle={() => setOpenIndex(openIndex === index ? null : index)}
          />
        ))}
        {!cases.length && (
          <div className="erd-empty">启动评测后，这里会实时展示每条用例的执行状态</div>
        )}
        {filteredEmpty && <div className="erd-empty">没有「{VERDICT_TEXT[filter]}」的用例</div>}
      </div>
    </>
  );
}

function DetailBody({ st, evolution, onOpenReport, onEvolution }) {
  const [filter, setFilter] = useState(null);
  const cases = st.cases || [];
  const counts = { pass: 0, partial: 0, fail: 0, error: 0 };
  cases.forEach((c) => {
    if (TERMINAL.has(c.verdict)) counts[c.verdict] += 1;
  });
  const running = st.status === 'running';
  const notice = ['failed', 'stopped', 'interrupted'].includes(st.status) && st.message;
  const hasFooterAction = Boolean(st.report || evolution);
  return (
    <div className="erd-body">
      <MetaLine st={st} />
      <section className="erd-panel">
        <PhaseTrack st={st} />
        {running && st.message && (
          <div className="erd-live">
            <span className="erd-live-dot" />
            {st.message}
          </div>
        )}
      </section>
      {notice && <div className={`erd-notice is-${st.status}`}>{st.message}</div>}
      <Overview st={st} counts={counts} total={cases.length} filter={filter} onFilter={setFilter} />
      <CaseList cases={cases} filter={filter} onClearFilter={() => setFilter(null)} />
      {hasFooterAction && !running && (
        <div className="erd-footer">
          {st.report && <Button onClick={() => onOpenReport(st.report)}>查看完整报告</Button>}
          {evolution && (
            <Tooltip title="根据最近一轮评测的失分与异常用例归因并优化知识包">
              <Button type="primary" onClick={onEvolution}>
                评测优化
              </Button>
            </Tooltip>
          )}
        </div>
      )}
    </div>
  );
}

export default function EvalRunDetail({
  open,
  state,
  evolution,
  onClose,
  onOpenReport,
  onEvolution,
}) {
  const st = state || {};
  const status = st.status || 'idle';
  return (
    <Modal
      className="erd-modal"
      rootClassName="erd-root"
      open={open}
      onCancel={onClose}
      footer={null}
      width={880}
      centered
      destroyOnHidden
      title={
        <div className="erd-title">
          <span>评测执行详情</span>
          {STATUS_TEXT[status] && (
            <span className={`erd-status is-${status}`}>
              <i />
              {STATUS_TEXT[status]}
            </span>
          )}
        </div>
      }
    >
      <DetailBody
        st={st}
        evolution={evolution}
        onOpenReport={onOpenReport}
        onEvolution={onEvolution}
      />
    </Modal>
  );
}
