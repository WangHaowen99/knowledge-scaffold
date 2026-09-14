import React, { useEffect, useState } from 'react';
import { listProjects, deleteProject, listProjectMembers, setProjectMember } from './api.js';

const AGENT_PHASE_PROGRESS = {
  intake: 5,
  goal: 20,
  process: 40,
  knowledge: 60,
  increment: 60,
  validation: 75,
  evaluation: 90,
  release: 100,
};

function agentStateSummary(state) {
  if (!state) {
    return { label: '进度暂不可用', percent: 0, title: '暂时无法读取建模助手的项目进度' };
  }
  const phase = String(state.phase || String(state.state || '').split(':')[0] || 'intake');
  const execution = String(state.execution || String(state.state || '').split(':')[1] || 'idle');
  const phaseLabel = state.phaseLabel || ({
    intake: '资料与目标准备',
    goal: '目标确认',
    process: '流程建模',
    knowledge: '知识建模',
    increment: '增量建模',
    validation: '确定性验证',
    evaluation: '评测',
    release: '发布交付',
  }[phase] || '项目推进');
  const suffix = {
    queued: '待执行',
    running: '进行中',
    waiting: '待确认',
    suspended: '待继续',
    completed: '已完成',
    failed: '受阻',
    cancelled: '已暂停',
  }[execution] || '';
  const label = `${phaseLabel}${suffix}`;
  const percent = AGENT_PHASE_PROGRESS[phase] ?? 0;
  return { label, percent, title: `项目进度：${label}（${percent}%）` };
}

export function projectConstructionSummary(project) {
  if (project.kind === 'package') {
    return { label: '知识包工作区', percent: null, title: '打开知识包工作区' };
  }
  if (project.runtime === 'agent') return agentStateSummary(project.agentState);
  const construction = project.construction;
  if (!construction) {
    return { label: '待进入建模', percent: 0, title: '进入项目后初始化 AI 辅助构建' };
  }
  const rawPercent = Number(construction.progress?.percent ?? 0);
  const percent = Math.min(100, Math.max(0, Number.isFinite(rawPercent) ? rawPercent : 0));
  if (construction.status === 'completed') {
    return { label: '建模已完成', percent: 100, title: 'AI 辅助构建已完成' };
  }
  const label = construction.currentTask?.title || 'AI 辅助构建';
  return { label, percent, title: `建模进度：${label}（${percent}%）` };
}

export function ProjectCard({ project: p, onOpen, onRequestDelete, onRequestShare, deleting = false }) {
  const summary = projectConstructionSummary(p);
  const accessRole = p.accessRole || 'owner';
  return (
    <div
      className={`pj-card ${p.kind === 'package' ? 'package' : ''}`}
      role="button"
      tabIndex={0}
      aria-label={`打开项目：${p.name}`}
      onClick={() => onOpen(p)}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget || !['Enter', ' '].includes(event.key)) return;
        event.preventDefault();
        onOpen(p);
      }}
    >
      {accessRole === 'owner' && onRequestShare && <button
        type="button"
        className="pj-share"
        aria-label={`共享项目：${p.name}`}
        title="管理项目成员"
        onClick={(event) => { event.stopPropagation(); onRequestShare(p); }}
      >成员</button>}
      {accessRole === 'owner' && <button
        type="button"
        className="pj-delete"
        aria-label={`删除项目：${p.name}`}
        title="删除项目"
        disabled={deleting}
        onClick={(event) => {
          event.stopPropagation();
          onRequestDelete(p);
        }}
      >{deleting ? '…' : '×'}</button>}
      <h3 title={p.name}>{p.name}</h3>
      <div className={`pd ${p.background ? '' : 'pd-empty'}`}>{p.background || '暂无说明'}</div>
      {p.kind !== 'package' && (
        <div className="pj-prog"
          title={summary.title}>
          <div className="pj-prog-in" style={{ width: `${summary.percent}%` }} />
        </div>
      )}
      <div className="pm">
        <span className="tag tag-lang">{summary.label}</span>
        {accessRole === 'viewer' && <span className="tag">只读成员</span>}
        {accessRole === 'modeler' && <span className="tag tag-ok">建模成员</span>}
        {p.domain && <span className="tag tag-ok" title={p.domain}>{p.domain}</span>}
        <span className="pj-date">{(p.updatedAt || '').slice(0, 10)}</span>
      </div>
    </div>
  );
}

export function ProjectDeleteDialog({ project, busy, error, onCancel, onConfirm }) {
  return (
    <div className="modal-ov">
      <div className="modal pj-delete-modal" role="dialog" aria-modal="true"
        aria-labelledby="project-delete-title">
        <h3 id="project-delete-title">删除项目</h3>
        <p className="pj-delete-question">确认删除项目「{project.name}」吗？</p>
        <p className="pj-delete-warning">
          将永久删除 modeling 中的资料、访谈、知识包版本、评测记录和优化建议，此操作不可恢复。
        </p>
        <p className="pj-delete-external">已同步到 consumer、Neo4j 和评测数据库的数据不会被删除。</p>
        {error && <div className="lerr">{error}</div>}
        <div className="foot">
          <button className="btn" disabled={busy} onClick={onCancel}>取消</button>
          <button className="btn pj-delete-confirm" disabled={busy} onClick={onConfirm}>
            {busy ? '删除中…' : '确认删除'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function ProjectList({ onOpen }) {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadErr, setLoadErr] = useState('');
  const [deleteErr, setDeleteErr] = useState('');
  const [deleting, setDeleting] = useState(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [sharing, setSharing] = useState(null);

  const load = () => listProjects()
    .then((d) => { setProjects(d.projects); setLoadErr(''); })
    .catch((e) => setLoadErr(e.message || '项目列表加载失败'))
    .finally(() => setLoading(false));
  useEffect(() => { load(); }, []);

  const requestDelete = (project) => {
    setDeleting(project);
    setDeleteErr('');
  };

  const confirmDelete = async () => {
    if (!deleting || deleteBusy) return;
    setDeleteBusy(true);
    setDeleteErr('');
    try {
      await deleteProject(deleting.id);
      setProjects((current) => current.filter((item) => item.id !== deleting.id));
      setDeleting(null);
    } catch (error) {
      setDeleteErr(error.message || '删除项目失败');
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <div>
      <div className="pj-head">
        <div>
          <h2>项目与知识包</h2>
          <p>AI 建模、人工编辑、评测与交付</p>
        </div>
        {!loading && projects.length > 0 && <span className="pj-count">{projects.length} 个项目</span>}
      </div>
      {loading && <div className="empty">加载中…</div>}
      {!loading && loadErr && (
        <div className="empty">
          项目列表加载失败：{loadErr}{' '}
          <button type="button" className="btn" onClick={() => { setLoading(true); load(); }}>重试</button>
        </div>
      )}
      {!loading && !loadErr && projects.length === 0 && (
        <div className="pj-hero">
          <span className="pj-hero-ic">✦</span>
          <div>
            <div className="pj-hero-t">暂无项目</div>
            <div className="pj-hero-d">创建项目并说明建模目标，AI 将围绕目标分析资料。</div>
          </div>
        </div>
      )}
      <div className="pj-grid" data-tour="pj-grid">
        {projects.map((p) => (
          <ProjectCard
            key={p.id}
            project={p}
            onOpen={onOpen}
            onRequestDelete={requestDelete}
            onRequestShare={setSharing}
            deleting={deleteBusy && deleting?.id === p.id}
          />
        ))}
        <button className="pj-card pj-new" data-tour="pj-new"
          onClick={() => window.location.assign('/workbench.html#/create')}>
          <span className="pj-new-ic">＋</span>
          <span>新建项目</span>
        </button>
      </div>
      {deleting && (
        <ProjectDeleteDialog project={deleting} busy={deleteBusy} error={deleteErr}
          onCancel={() => { if (!deleteBusy) setDeleting(null); }} onConfirm={confirmDelete} />
      )}
      {sharing && <ProjectMemberDialog project={sharing} onClose={() => setSharing(null)} />}
    </div>
  );
}

export function ProjectMemberDialog({ project, onClose }) {
  const [members, setMembers] = useState([]);
  const [username, setUsername] = useState('');
  const [role, setRole] = useState('viewer');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void listProjectMembers(project.id)
      .then((result) => setMembers(result.members || []))
      .catch((cause) => setError(cause.message || '成员列表加载失败'));
  }, [project.id]);

  const save = async (event) => {
    event.preventDefault();
    if (!username.trim() || busy) return;
    setBusy(true); setError('');
    try {
      const result = await setProjectMember(project.id, username.trim(), role);
      setMembers(result.members || []);
      setUsername('');
    } catch (cause) {
      setError(cause.message || '成员保存失败');
    } finally {
      setBusy(false);
    }
  };

  return <div className="modal-ov">
    <form className="modal pj-member-modal" role="dialog" aria-modal="true" aria-labelledby="project-member-title" onSubmit={save}>
      <h3 id="project-member-title">项目成员</h3>
      <p>添加成员后，对方会在项目列表直接看到这个项目。</p>
      <div className="pj-member-list">{members.map((member) => <div key={member.username}>
        <b>{member.username}</b><span>{(() => { if (member.role === 'owner') return '项目负责人'; return member.role === 'modeler' ? '建模成员' : '只读成员'; })()}</span>
      </div>)}</div>
      <label><span>成员用户名</span><input className="input" value={username} onChange={(event) => setUsername(event.target.value)} /></label>
      <label><span>权限</span><select className="input" value={role} onChange={(event) => setRole(event.target.value)}>
        <option value="viewer">只读成员</option>
        <option value="modeler">建模成员</option>
      </select></label>
      {error && <div className="lerr" role="alert">{error}</div>}
      <div className="foot"><button type="button" className="btn" onClick={onClose}>关闭</button>
        <button className="btn btn-p" disabled={!username.trim() || busy}>{busy ? '保存中…' : '添加成员'}</button></div>
    </form>
  </div>;
}
