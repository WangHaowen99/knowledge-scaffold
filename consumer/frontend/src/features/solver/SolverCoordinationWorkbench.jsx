import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle, Clock3, FileCheck2, LoaderCircle, RefreshCw, ShieldCheck,
} from 'lucide-react';

import { EmptyState, PageHeader, StatusTag, Table } from '../../shared/ui/primitives.jsx';
import { getSolverEvidence, getSolverHistory } from './api.js';
import { createRequestEpochGuard } from './model.js';
import './solver.css';

const statusLabel = (status) => ({
  completed: '已完成',
  running: '运行中',
  queued: '排队中',
  failed: '失败',
  error: '失败',
  cancelled: '已取消',
  timed_out: '已超时',
  rejected: '校验拒绝',
}[status] || status || '未知');

const semanticLabel = (value) => ({
  feasible: '可行',
  infeasible: '已证明不可行',
  not_found: '未找到可行解',
  proven_optimal: '已证明最优',
  not_proven: '未证明最优',
  not_applicable: '不适用',
}[value] || value || '—');

const shortIdentity = (value, size = 24) => {
  const text = String(value || '').trim();
  return text.length > size ? `${text.slice(0, size)}…` : text || '—';
};

const formatTimestamp = (value) => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
};

function EvidenceDetails({ run }) {
  const [state, setState] = useState({ loading: false, loaded: false, error: '', data: null });
  const evidence = run?.verification_evidence_ref || run?.evidence_ref;
  const result = run?.result_artifact_ref;
  if (!run?.job_id) return <span className="solver-history-muted">—</span>;

  const loadEvidence = async (event) => {
    if (!event.currentTarget.open || state.loading || state.loaded) return;
    setState({ loading: true, loaded: false, error: '', data: null });
    try {
      const data = await getSolverEvidence(run.job_id);
      setState({ loading: false, loaded: true, error: '', data });
    } catch (error) {
      setState({
        loading: false,
        loaded: false,
        error: error?.message || '加载验证证据失败',
        data: null,
      });
    }
  };

  const verification = state.data?.verification_results?.at?.(-1)?.result;
  const integrityVerified = state.data?.artifacts?.length > 0
    && state.data.artifacts.every((artifact) => artifact.integrity_verified === true);
  return (
    <details className="solver-history-evidence" onToggle={loadEvidence}>
      <summary><FileCheck2 size={12} />验证证据</summary>
      <dl>
        {evidence && <div><dt>校验证据</dt><dd><code title={evidence}>{evidence}</code></dd></div>}
        {result && <div><dt>结果制品</dt><dd><code title={result}>{result}</code></dd></div>}
        {run.fact_snapshot_digest && <div><dt>事实快照</dt><dd><code title={run.fact_snapshot_digest}>{run.fact_snapshot_digest}</code></dd></div>}
        {verification?.proof_kind && <div><dt>校验方法</dt><dd>{verification.proof_kind}</dd></div>}
        {state.loaded && (
          <div><dt>制品完整性</dt><dd>{integrityVerified ? '已复核' : '无制品记录'}</dd></div>
        )}
      </dl>
      {state.loading && <span className="solver-history-evidence-state"><LoaderCircle className="spin" size={12} />加载中</span>}
      {state.error && <span className="solver-history-evidence-state is-error"><AlertTriangle size={12} />{state.error}</span>}
    </details>
  );
}

function HistoryTable({ items }) {
  return (
    <div className="solver-history-table-wrap">
      <Table className="solver-history-table" aria-label="求解历史">
        <thead>
          <tr><th>时间</th><th>Operation</th><th>状态</th><th>验证语义</th><th>Job</th><th>证据</th></tr>
        </thead>
        <tbody>
          {items.map((run) => (
            <tr key={run.job_id || run.run_id}>
              <td><span className="solver-history-time">{formatTimestamp(run.updated_at || run.created_at)}</span></td>
              <td>
                <strong className="solver-history-operation">{run.operation_key || '—'}</strong>
                <span className="solver-history-revision">{run.knowledge_revision_id || run.package_revision || '—'}</span>
              </td>
              <td><StatusTag status={run.status}>{statusLabel(run.status)}</StatusTag></td>
              <td>
                <span className="solver-history-semantic">{semanticLabel(run.feasibility)}</span>
                <span className="solver-history-muted">{semanticLabel(run.optimality)}</span>
              </td>
              <td><code className="solver-history-id" title={run.job_id}>{shortIdentity(run.job_id)}</code></td>
              <td><EvidenceDetails run={run} /></td>
            </tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}

export default function SolverCoordinationWorkbench({ workspaceId, resetToken = 0 }) {
  const [state, setState] = useState({ loading: true, error: '', items: [] });
  const [requestGuard] = useState(() => createRequestEpochGuard());

  const loadHistory = useCallback(async (signal) => {
    const requestEpoch = requestGuard.invalidate();
    if (!workspaceId) {
      setState({ loading: false, error: '', items: [] });
      return;
    }
    setState((current) => ({ ...current, loading: true, error: '' }));
    try {
      const payload = await getSolverHistory({ limit: 50 }, { signal });
      const items = Array.isArray(payload?.items)
        ? payload.items
        : (Array.isArray(payload?.runs) ? payload.runs : []);
      if (!requestGuard.accepts(requestEpoch)) return;
      setState({ loading: false, error: '', items });
    } catch (error) {
      if (error?.name === 'AbortError') return;
      if (!requestGuard.accepts(requestEpoch)) return;
      setState({ loading: false, error: error?.message || '加载求解历史失败', items: [] });
    }
  }, [requestGuard, workspaceId]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void loadHistory(controller.signal);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      requestGuard.invalidate();
      controller.abort();
    };
  }, [loadHistory, requestGuard, resetToken]);

  return (
    <main className="solver-workbench">
      <PageHeader
        eyebrow="Solver Coordination"
        title="求解历史"
        description="当前工作空间的求解运行与独立验证记录。"
        actions={(
          <button className="iconbtn" type="button" title="刷新求解历史" aria-label="刷新求解历史"
            disabled={state.loading} onClick={() => { void loadHistory(); }}>
            {state.loading ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}
          </button>
        )}
      />
      {state.error && (
        <div className="solver-history-error" role="alert">
          <AlertTriangle size={15} /><span>{state.error}</span>
        </div>
      )}
      <section className="solver-history-shell" aria-labelledby="solver-history-heading">
        <header className="solver-history-heading">
          <div><Clock3 size={17} /><h3 id="solver-history-heading">运行记录</h3></div>
          <span><ShieldCheck size={13} />仅权威结果可进入业务结论</span>
        </header>
        {state.loading && state.items.length === 0
          ? <div className="solver-history-loading"><LoaderCircle className="spin" size={18} />加载运行记录</div>
          : state.items.length > 0
            ? <HistoryTable items={state.items} />
            : <EmptyState compact title="暂无求解记录" description="当前工作空间尚无已记录的求解运行。" />}
      </section>
    </main>
  );
}
