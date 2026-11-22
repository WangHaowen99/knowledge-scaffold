import { useCallback } from 'react';
import { useApp } from '../../app/context.js';
import { Icon, Select, Tabs } from '../../ui/index.jsx';
import {
  FILTERS, RECORD_PAGE, auditKey, canIgnore, canRollback, evaluationLine, formatAt, isFeedback, mode, rate, reasonTitle,
  recordsSignature, runKey, version,
} from './flywheelModel.js';
import { StatusChip, TextLink } from './FlywheelCards.jsx';
import { useLoadMore, useRecordPages } from './useRecordPages.js';

function RunActions({ run, state, onAction }) {
  const suggestions = run.status === 'waiting_optimize'
    || (run.status === 'blocked' && ['suggestions_failed', 'version_mismatch'].includes(run.reason))
    || run.status === 'expired';
  return (
    <div className="fw-acts">
      <TextLink onClick={() => onAction('detail', run)}>详情</TextLink>
      {state.can_write && canIgnore(run) ? <TextLink gray onClick={() => onAction('ignore', run)}>忽略</TextLink> : null}
      {state.can_write && canRollback(run) ? (
        <TextLink gray onClick={() => onAction('rollback', run)}><Icon name="undo" size={12} />回退</TextLink>
      ) : suggestions && run.eval_run_id ? (
        <TextLink onClick={() => onAction('suggestions', run)}>查看建议</TextLink>
      ) : run.status === 'rolledback' ? <span className="muted">历史保留</span> : null}
    </div>
  );
}

function RunRow({ run, state, onAction }) {
  const evaluation = ['blocked', 'expired'].includes(run.status) ? reasonTitle(run) : evaluationLine(run);
  return (
    <tr>
      <td>
        <span className="mono b">{run.id}</span>
        <span className="fw-sub mono">{formatAt(run.received_at)}</span>
      </td>
      <td>{mode(run)}</td>
      <td className="mono">
        {version(run.content_version || run.version)}
        {run.target_version ? <> <span className="muted">→</span> {version(run.target_version)}</> : null}
        {run.kind === 'rollback' && run.status === 'restored' ? <span className="fw-sub">仅恢复建模内容</span> : null}
      </td>
      <td>
        {run.score == null ? (isFeedback(run) ? <span className="muted">用户反馈</span> : '—') : (
          <span className="mono">
            {rate(run.score)}
            {run.after_score != null ? <> <span className="muted">→</span> <span className="fw-up">{rate(run.after_score)}</span></> : null}
          </span>
        )}
      </td>
      <td>
        <StatusChip run={run} />
        {evaluation ? <span className="fw-sub">{evaluation}</span> : null}
      </td>
      <td><RunActions run={run} state={state} onAction={onAction} /></td>
    </tr>
  );
}

// 记录列表的末行：加载中、加载失败可重试、或已全部显示；sentinel 滚到附近时取下一批
function ListEnd({ pages, colSpan, empty, sentinel }) {
  let text = null;
  if (pages.error) {
    text = <>加载失败：{pages.error} <TextLink onClick={pages.retry}>重试</TextLink></>;
  } else if (pages.loading) {
    text = <><span className="spin" aria-hidden="true" />正在加载…</>;
  } else if (!pages.items.length) {
    return <tr><td colSpan={colSpan}><div className="fw-empty">{empty}</div></td></tr>;
  } else if (pages.done && pages.items.length > RECORD_PAGE) {
    text = `已显示全部 ${pages.total} 条`;
  } else if (pages.done) {
    return null;
  }
  return (
    <tr className="fw-end">
      <td colSpan={colSpan}><div ref={sentinel} className="fw-end-t">{text}</div></td>
    </tr>
  );
}

function RunsTable({ state, filter, onAction }) {
  const { api } = useApp();
  const projectId = state.project.id;
  const fetchPage = useCallback((offset, limit) => api.flywheelRuns(projectId, { offset, limit, filter }), [api, projectId, filter]);
  const pages = useRecordPages(fetchPage, runKey, recordsSignature(state, 'runs'));
  const sentinel = useLoadMore(pages.more, !pages.done && !pages.error, pages.items.length);
  return (
    <table className="tbl compact fw-tbl">
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
  const { api } = useApp();
  const projectId = state.project.id;
  const fetchPage = useCallback((offset, limit) => api.flywheelAudit(projectId, { offset, limit }), [api, projectId]);
  const pages = useRecordPages(fetchPage, auditKey, recordsSignature(state, 'settings'));
  const sentinel = useLoadMore(pages.more, !pages.done && !pages.error, pages.items.length);
  return (
    <table className="tbl compact fw-tbl">
      <thead><tr><th>变更时间</th><th>设置项</th><th>变更内容</th><th>操作来源</th></tr></thead>
      <tbody>
        {pages.items.map((item) => (
          <tr key={auditKey(item)}>
            <td className="mono">{formatAt(item.at)}</td><td>{item.action}</td><td>{item.description}</td><td>{item.actor}</td>
          </tr>
        ))}
        <ListEnd pages={pages} colSpan={4} sentinel={sentinel} empty="暂无设置变更" />
      </tbody>
    </table>
  );
}

// 运行记录与设置变更：从最新一条开始分批读取，滚动到列表底部时自动取下一批
export default function FlywheelRecords({ state, tab, filter, onTab, onFilter, onAction, recordsRef }) {
  return (
    <section ref={recordsRef} aria-label="数据飞轮运行记录">
      <div className="fw-records-h">
        <Tabs items={[['runs', '运行记录', state.total], ['settings', '设置变更']]} value={tab} onChange={onTab} />
        {tab === 'runs' ? <Select w={160} value={filter} options={FILTERS} onChange={onFilter} /> : null}
      </div>
      <div className="tbl-wrap">
        {tab === 'settings' ? <AuditTable state={state} /> : <RunsTable state={state} filter={filter} onAction={onAction} />}
      </div>
    </section>
  );
}
