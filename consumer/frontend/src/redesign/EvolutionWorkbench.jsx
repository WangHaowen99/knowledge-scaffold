import { useCallback, useEffect, useMemo, useState } from 'react';

import { authFetch } from '../utils/auth';
import { useAuthContext } from './useAuthContext';
import { EmptyState, SectionCard, ScopeTabs, StatusTag, Table } from './ui';

const STATUS_LABELS = {
  pending: '待积累', promoted: '已晋级', applied: '已应用', rejected: '已拒绝',
  submitted: '已提交', running: '运行中', downloading: '下载中', completed: '待审批',
  approved: '已批准', no_change: '无变更', failed: '失败', interrupted: '已中断', cancelled: '已取消',
};
const fmt = (value) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString() : '—';
const pathFor = (ontologyId, operationKey) => `${encodeURIComponent(ontologyId)}/${encodeURIComponent(operationKey)}`;

export default function EvolutionWorkbench({ ontologyId }) {
  const [kind, setKind] = useState('operation');
  return (
    <div className="kb-tab-body evolution-workbench">
      <ScopeTabs value={kind} onChange={setKind} ariaLabel="记忆进化类型" items={[
        { id: 'operation', label: 'Operation 进化' },
        { id: 'skill', label: 'Skill 进化' },
      ]} />
      {kind === 'operation'
        ? <OperationEvolution ontologyId={ontologyId} />
        : <SkillEvolution ontologyId={ontologyId} />}
    </div>
  );
}

function OperationEvolution({ ontologyId }) {
  const { isAdmin, workspaceId } = useAuthContext();
  const [items, setItems] = useState([]);
  const [selectedKey, setSelectedKey] = useState('');
  const [proposals, setProposals] = useState([]);
  const [records, setRecords] = useState([]);
  const [staged, setStaged] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');

  const selected = useMemo(
    () => items.find((item) => `${item.ontology_id}/${item.operation_key}` === selectedKey) || null,
    [items, selectedKey],
  );

  const loadIndex = useCallback(async () => {
    setLoading(true);
    try {
      const response = await authFetch('/agentic/operations/proposals?status=all');
      const payload = await response.json().catch(() => ({}));
      const nextItems = (payload.items || []).filter((item) => !ontologyId || item.ontology_id === ontologyId);
      setItems(nextItems);
      setSelectedKey((current) => (
        current && nextItems.some((item) => `${item.ontology_id}/${item.operation_key}` === current)
          ? current
          : nextItems[0] ? `${nextItems[0].ontology_id}/${nextItems[0].operation_key}` : ''
      ));
    } finally {
      setLoading(false);
    }
  }, [ontologyId]);

  const loadDetail = useCallback(async () => {
    if (!selected) { setProposals([]); setRecords([]); setStaged(null); return; }
    const base = `/agentic/operations/${pathFor(selected.ontology_id, selected.operation_key)}`;
    const [proposalResponse, stagedResponse] = await Promise.all([
      authFetch(`${base}/proposals?status=all&include_records=1`),
      authFetch(`${base}/staged`),
    ]);
    const proposalPayload = await proposalResponse.json().catch(() => ({}));
    const stagedPayload = await stagedResponse.json().catch(() => ({}));
    setProposals(proposalPayload.proposals || []);
    setRecords(proposalPayload.proposal_records || []);
    setStaged(stagedResponse.ok ? stagedPayload : null);
  }, [selected]);

  useEffect(() => { const timer = setTimeout(loadIndex, 0); return () => clearTimeout(timer); }, [loadIndex, workspaceId]);
  useEffect(() => { const timer = setTimeout(loadDetail, 0); return () => clearTimeout(timer); }, [loadDetail]);

  const proposalAction = async (fingerprint, action) => {
    if (!selected) return;
    setBusy(`${action}:${fingerprint}`);
    setNotice('');
    const response = await authFetch(
      `/agentic/operations/${pathFor(selected.ontology_id, selected.operation_key)}/proposals/${encodeURIComponent(fingerprint)}/${action}`,
      { method: 'POST' },
    );
    setNotice(response.ok ? (action === 'endorse' ? '已增加认可证据' : '已拒绝该建议') : '操作失败');
    if (response.ok) { await loadDetail(); await loadIndex(); }
    setBusy('');
  };

  const stagedAction = async (action) => {
    if (!selected) return;
    setBusy(`staged:${action}`);
    setNotice('');
    const response = await authFetch(
      `/agentic/operations/${pathFor(selected.ontology_id, selected.operation_key)}/staged/${action}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: action === 'reject' ? JSON.stringify({ reject_proposals: true }) : undefined,
      },
    );
    const payload = await response.json().catch(() => ({}));
    setNotice(response.ok ? (action === 'apply' ? '暂存变更已应用' : '暂存变更已拒绝') : `操作失败：${payload.error || response.status}`);
    if (response.ok) { await loadDetail(); await loadIndex(); }
    setBusy('');
  };

  return (
    <div className="evolution-grid">
      <SectionCard title="Operation 建议" description="按业务操作聚合主动反馈与运行证据。">
        {loading && <div className="empty">加载中…</div>}
        {!loading && !items.length && <EmptyState compact title="暂无进化建议" description="Agent 运行后，建议会按 Operation 汇总到这里。" />}
        <div className="evolution-list">
          {items.map((item) => {
            const key = `${item.ontology_id}/${item.operation_key}`;
            return (
              <button type="button" key={key} className={key === selectedKey ? 'on' : ''} onClick={() => setSelectedKey(key)}>
                <strong>{item.operation_key}</strong>
                <span>{item.aggregate_count} 条建议 · {item.occurrences} 次证据</span>
                <small>{item.top_content}</small>
              </button>
            );
          })}
        </div>
      </SectionCard>
      <div className="evolution-detail">
        {!selected && <SectionCard><EmptyState title="选择 Operation 查看建议" /></SectionCard>}
        {selected && (
          <>
            <SectionCard title={selected.operation_key} description={`${selected.ontology_id} · 最近更新 ${fmt(selected.last_seen)}`}
              actions={<StatusTag status={selected.has_staged ? 'warning' : 'active'}>{selected.has_staged ? '待审批补丁' : '建议积累中'}</StatusTag>}>
              {!proposals.length && <EmptyState compact title="当前筛选下暂无建议" />}
              <div className="proposal-cards">
                {proposals.map((proposal) => (
                  <article key={proposal.fingerprint}>
                    <header><StatusTag status={proposal.status}>{STATUS_LABELS[proposal.status] || proposal.status}</StatusTag><span>{proposal.kind} · {proposal.occurrences} 次</span></header>
                    <p>{proposal.canonical_content}</p>
                    <footer>
                      <span>{proposal.active_endorsements || 0} 次认可</span>
                      {proposal.status !== 'applied' && proposal.status !== 'rejected' && (
                        <><button className="btn" disabled={Boolean(busy)} onClick={() => proposalAction(proposal.fingerprint, 'endorse')}>认可</button>
                          <button className="btn btn-danger" disabled={Boolean(busy)} onClick={() => proposalAction(proposal.fingerprint, 'reject')}>拒绝</button></>
                      )}
                    </footer>
                  </article>
                ))}
              </div>
              {records.length > 0 && <details className="evidence-detail"><summary>查看 {records.length} 条原始证据</summary>{records.map((record, index) => <p key={`${record.fingerprint}-${index}`}>{record.content}<small>{record.source} · {record.run_id || '无 run id'}</small></p>)}</details>}
            </SectionCard>
            {staged && (
              <SectionCard title="暂存变更对比" description="审批前核对 Operation 原文与候选内容。"
                actions={isAdmin && <><button className="btn btn-danger" disabled={Boolean(busy)} onClick={() => stagedAction('reject')}>拒绝补丁</button><button className="btn btn-p" disabled={Boolean(busy)} onClick={() => stagedAction('apply')}>应用补丁</button></>}>
                {!isAdmin && <div className="ro-banner">🔒 只有平台管理员可以应用或拒绝暂存补丁。</div>}
                <div className="diff-columns"><DiffPane label="当前版本" value={staged.original} /><DiffPane label="候选版本" value={staged.staged} /></div>
              </SectionCard>
            )}
            {notice && <div className="kb-opnotice">{notice}</div>}
          </>
        )}
      </div>
    </div>
  );
}

function SkillEvolution({ ontologyId }) {
  const { isAdmin, user, workspaceId } = useAuthContext();
  const [tasks, setTasks] = useState([]);
  const [selectedId, setSelectedId] = useState('');
  const [diff, setDiff] = useState(null);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');

  const selected = useMemo(() => tasks.find((task) => task.task_id === selectedId) || null, [selectedId, tasks]);
  const load = useCallback(async () => {
    const query = ontologyId ? `?ontology_id=${encodeURIComponent(ontologyId)}` : '';
    const response = await authFetch(`/agentic/skill-evolution/tasks${query}`);
    const payload = await response.json().catch(() => ({}));
    const next = payload.tasks || [];
    setTasks(next);
    setSelectedId((current) => current && next.some((task) => task.task_id === current) ? current : next[0]?.task_id || '');
  }, [ontologyId]);

  useEffect(() => { const timer = setTimeout(load, 0); return () => clearTimeout(timer); }, [load, workspaceId]);
  useEffect(() => {
    if (!selected || !['completed', 'approved', 'rejected'].includes(selected.status)) {
      const clearTimer = setTimeout(() => setDiff(null), 0);
      return () => clearTimeout(clearTimer);
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      const response = await authFetch(`/agentic/skill-evolution/tasks/${encodeURIComponent(selected.task_id)}/diff`);
      const payload = await response.json().catch(() => ({}));
      if (!cancelled) setDiff(response.ok ? payload : null);
    }, 0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [selected]);

  const act = async (action) => {
    if (!selected) return;
    setBusy(action);
    setNotice('');
    const options = { method: 'POST' };
    if (action === 'approve') {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify({ mode: 'new_version' });
    }
    const response = await authFetch(`/agentic/skill-evolution/tasks/${encodeURIComponent(selected.task_id)}/${action}`, options);
    const payload = await response.json().catch(() => ({}));
    setNotice(response.ok ? `任务已${action === 'approve' ? '批准并生成新版本' : action === 'reject' ? '拒绝' : '取消'}` : `操作失败：${payload.error || response.status}`);
    if (response.ok) await load();
    setBusy('');
  };

  return (
    <div className="evolution-grid">
      <SectionCard title="Skill 进化任务" description="任务由会话 Trace 发起，在这里查看差异并审批。">
        {!tasks.length && <EmptyState compact title="暂无 Skill 进化任务" />}
        <div className="evolution-list">
          {tasks.map((task) => (
            <button type="button" key={task.task_id} className={task.task_id === selectedId ? 'on' : ''} onClick={() => setSelectedId(task.task_id)}>
              <strong>{task.ontology_name || task.ontology_id}</strong>
              <span><StatusTag status={task.status}>{STATUS_LABELS[task.status] || task.status}</StatusTag> · {fmt(task.updated_at)}</span>
              <small>{task.change_summary || task.stage || task.error_message || task.task_id}</small>
            </button>
          ))}
        </div>
      </SectionCard>
      <div className="evolution-detail">
        {!selected && <SectionCard><EmptyState title="选择任务查看详情" /></SectionCard>}
        {selected && (
          <SectionCard title={selected.ontology_name || selected.ontology_id} description={`任务 ${selected.task_id} · 发起人 ${selected.triggered_by}`}
            actions={<StatusTag status={selected.status}>{STATUS_LABELS[selected.status] || selected.status}</StatusTag>}>
            <Table><tbody>
              <tr><th>当前版本</th><td>{selected.active_version || '—'}</td><th>运行</th><td>{selected.run_id || '—'}</td></tr>
              <tr><th>阶段</th><td>{selected.stage || '—'}</td><th>更新时间</th><td>{fmt(selected.updated_at)}</td></tr>
            </tbody></Table>
            {selected.error_message && <div className="ro-banner error">{selected.error_message}</div>}
            {diff?.drift?.drifted && <div className="ro-banner">⚠ {diff.drift.message || '当前激活版本已发生漂移，请重新核对差异。'}</div>}
            {diff?.files?.length ? diff.files.map((file) => (
              <details className="skill-diff" key={file.path} open>
                <summary>{file.path} · {file.change_type} · +{file.added_lines || 0} / -{file.removed_lines || 0}</summary>
                <div className="diff-columns"><DiffPane label="原文件" value={file.original} /><DiffPane label="进化结果" value={file.evolved} /></div>
              </details>
            )) : <EmptyState compact title={selected.status === 'completed' ? '没有文件差异' : '任务完成后显示文件差异'} description={selected.change_summary} />}
            <div className="foot">
              {['submitted', 'running', 'downloading'].includes(selected.status) && (isAdmin || selected.triggered_by === user?.username) && <button className="btn btn-danger" disabled={Boolean(busy)} onClick={() => act('cancel')}>取消任务</button>}
              {selected.status === 'completed' && isAdmin && <><button className="btn btn-danger" disabled={Boolean(busy)} onClick={() => act('reject')}>拒绝</button><button className="btn btn-p" disabled={Boolean(busy)} onClick={() => act('approve')}>批准为新版本</button></>}
            </div>
            {notice && <div className="kb-opnotice">{notice}</div>}
          </SectionCard>
        )}
      </div>
    </div>
  );
}

function DiffPane({ label, value }) {
  return <div><strong>{label}</strong><pre>{value || '（空）'}</pre></div>;
}
