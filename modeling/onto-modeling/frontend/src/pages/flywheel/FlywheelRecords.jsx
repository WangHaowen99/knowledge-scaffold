import { useCallback } from 'react';
import { api } from '../../lib/api.js';
import {
  ACTIVE, FILTERS, RECORD_PAGE, auditKey, canIgnore, canRollback, evaluationLine, formatAt, isFeedback, mode, rate,
  reasonTitle, recordsSignature, runKey, statusOf, version,
} from '../../lib/flywheel.js';
import { Link, Tag } from './FlywheelCards.jsx';
import Icon from './Icon.jsx';
import { useLoadMore, useRecordPages } from './useRecordPages.js';

function RunActions({ run, state, onAction }) {
  const suggestions = run.status === 'waiting_optimize'
    || (run.status === 'blocked' && ['suggestions_failed', 'version_mismatch'].includes(run.reason))
    || run.status === 'expired';
  return (
    <div className="fw-record-actions">
      <Link onClick={() => onAction('detail', run)}>详情</Link>
      {state.can_write && canIgnore(run) && <Link gray onClick={() => onAction('ignore', run)}>忽略</Link>}
      {state.can_write && canRollback(run) ? (
        <Link gray onClick={() => onAction('rollback', run)}><Icon name="rollback" size="small" />回退</Link>
      ) : suggestions && run.eval_run_id ? (
        <Link onClick={() => onAction('suggestions', run)}>查看建议</Link>
      ) : run.status === 'rolledback' ? <span className="fw-muted">历史保留</span> : null}
    </div>
  );
}

function RunRow({ run, state, onAction }) {
  const [label, color] = statusOf(run);
  const evaluation = ['blocked', 'expired'].includes(run.status) ? reasonTitle(run) : evaluationLine(run);
  return (
    <tr>
      <td>
        <span className="fw-record-id fw-mono">{run.id}</span>
        <span className="fw-subline fw-mono">{formatAt(run.received_at)}</span>
      </td>
      <td>{mode(run)}</td>
      <td className="fw-mono">
        {version(run.content_version || run.version)}
        {run.target_version && <> <span className="fw-muted">→</span> {version(run.target_version)}</>}
        {run.kind === 'rollback' && run.status === 'restored' && <span className="fw-subline">仅恢复建模内容</span>}
      </td>
      <td>
        {run.score == null ? (isFeedback(run) ? <span className="fw-muted">用户反馈</span> : '—') : (
          <span className="fw-mono">
            {rate(run.score)}
            {run.after_score != null && <> <span className="fw-muted">→</span> <span style={{ color: 'var(--fw-green)' }}>{rate(run.after_score)}</span></>}
          </span>
        )}
      </td>
      <td>
        <Tag color={color} live={ACTIVE.includes(run.status)}>{label}</Tag>
        {evaluation && <span className="fw-subline">{evaluation}</span>}
      </td>
      <td><RunActions run={run} state={state} onAction={onAction} /></td>
    </tr>
  );
}

// 记录列表的末行：加载中、加载失败可重试、或已全部显示；sentinel 滚到附近时取下一批
function ListEnd({ pages, colSpan, empty, sentinel }) {
  let text = null;
  if (pages.error) {
    text = <>加载失败：{pages.error} <Link onClick={pages.retry}>重试</Link></>;
  } else if (pages.loading) {
    text = <span className="fw-loading-inline"><span className="fw-spinner" aria-hidden="true" />正在加载…</span>;
  } else if (!pages.items.length) {
    return <tr><td colSpan={colSpan}><div className="fw-empty">{empty}</div></td></tr>;
  } else if (pages.done && pages.items.length > RECORD_PAGE) {
    text = `已显示全部 ${pages.total} 条`;
  } else if (pages.done) {
    return null;
  }
  return (
    <tr className="fw-list-end">
      <td colSpan={colSpan}><div ref={sentinel} className="fw-muted">{text}</div></td>
    </tr>
  );
}

function RunsTable({ state, filter, onAction }) {
  const projectId = state.project.id;
  const fetchPage = useCallback((offset, limit) => api.flywheelRuns(projectId, { offset, limit, filter }), [projectId, filter]);
  const pages = useRecordPages(fetchPage, runKey, recordsSignature(state, 'runs'));
  const sentinel = useLoadMore(pages.more, !pages.done && !pages.error, pages.items.length);
  return (
    <table className="fw-record-table">
      <thead><tr><th>运行 / 触发时间</th><th>处理方式</th><th>版本变化</th><th>严格通过率</th><th>处理结果</th><th>操作</th></tr></thead>
      <tbody>
        {pages.items.map((run) => <RunRow key={run.id} run={run} state={state} onAction={onAction} />)}
        <ListEnd pages={pages} colSpan={6} sentinel={sentinel}
          empty={state.total ? '当前筛选下暂无运行记录' : '暂无运行记录'} />
      </tbody>
    </table>
  );
}

function AuditTable({ state }) {
  const projectId = state.project.id;
  const fetchPage = useCallback((offset, limit) => api.flywheelAudit(projectId, { offset, limit }), [projectId]);
  const pages = useRecordPages(fetchPage, auditKey, recordsSignature(state, 'settings'));
  const sentinel = useLoadMore(pages.more, !pages.done && !pages.error, pages.items.length);
  return (
    <table className="fw-record-table">
      <thead><tr><th>变更时间</th><th>设置项</th><th>变更内容</th><th>操作来源</th></tr></thead>
      <tbody>
        {pages.items.map((item) => (
          <tr key={auditKey(item)}>
            <td className="fw-mono">{formatAt(item.at)}</td><td>{item.action}</td><td>{item.description}</td><td>{item.actor}</td>
          </tr>
        ))}
        <ListEnd pages={pages} colSpan={4} sentinel={sentinel} empty="暂无设置变更" />
      </tbody>
    </table>
  );
}

// 运行记录与设置变更：从最新一条开始分批读取，滚动到列表底部时自动取下一批
export default function FlywheelRecords({ state, tab, filter, onTab, onFilter, onAction, recordsRef }) {
  const switchTab = (event) => {
    if (['ArrowLeft', 'ArrowRight'].includes(event.key)) {
      event.preventDefault();
      onTab(tab === 'runs' ? 'settings' : 'runs');
    }
  };
  return (
    <section className="fw-records" ref={recordsRef} aria-label="数据飞轮运行记录">
      <div className="fw-records-heading">
        <div className="fw-record-tabs" role="tablist" aria-label="记录类型" onKeyDown={switchTab}>
          <button type="button" className="fw-record-tab" role="tab" aria-selected={tab === 'runs'} onClick={() => onTab('runs')}>
            运行记录<span className="fw-counter">{state.total}</span>
          </button>
          <button type="button" className="fw-record-tab" role="tab" aria-selected={tab === 'settings'} onClick={() => onTab('settings')}>
            设置变更
          </button>
        </div>
        {tab === 'runs' && (
          <select className="fw-record-filter" aria-label="筛选运行状态" value={filter} onChange={(event) => onFilter(event.target.value)}>
            {FILTERS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        )}
      </div>
      <div className="fw-table-scroll" role="tabpanel" tabIndex={0}>
        {tab === 'settings' ? <AuditTable state={state} /> : <RunsTable state={state} filter={filter} onAction={onAction} />}
      </div>
    </section>
  );
}
