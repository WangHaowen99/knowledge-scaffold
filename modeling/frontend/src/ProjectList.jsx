import React, { useEffect, useState } from 'react';
import { listProjects, createProject, deleteProject } from './api.js';

const DEFAULT_FORM = {
  name: '',
  domain: '',
  background: '',
  targetUsers: '',
  glossaryPrefs: '',
  sensitivityLevel: 'standard',
  workflowProfile: {
    enabledSteps: [],
    requiredSteps: [],
    releaseRequested: true,
    externalResearch: false,
  },
};

export function projectConstructionSummary(project) {
  if (project.kind === 'package') {
    return { label: '知识包工作区', percent: null, title: '打开知识包工作区' };
  }
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

export function ProjectCard({ project: p, onOpen, onRequestDelete, deleting = false }) {
  const summary = projectConstructionSummary(p);
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
      <button
        type="button"
        className="pj-delete"
        aria-label={`删除项目：${p.name}`}
        title="删除项目"
        disabled={deleting}
        onClick={(event) => {
          event.stopPropagation();
          onRequestDelete(p);
        }}
      >{deleting ? '…' : '×'}</button>
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
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState(DEFAULT_FORM);
  const [err, setErr] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [deleteErr, setDeleteErr] = useState('');
  const [deleting, setDeleting] = useState(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const load = () => listProjects()
    .then((d) => { setProjects(d.projects); setLoadErr(''); })
    .catch((e) => setLoadErr(e.message || '项目列表加载失败'))
    .finally(() => setLoading(false));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!creating) return undefined;
    const onKeyDown = (e) => { if (e.key === 'Escape' && !submitting) setCreating(false); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [creating, submitting]);

  const submit = async () => {
    if (submitting) return;
    if (!form.name.trim()) { setErr('项目名必填'); return; }
    setSubmitting(true);
    try {
      const { project } = await createProject(form);
      setCreating(false);
      onOpen(project);
    } catch (e) { setErr(e.message); } finally { setSubmitting(false); }
  };

  const setProfile = (patch) => setForm({
    ...form,
    workflowProfile: { ...form.workflowProfile, ...patch },
  });

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
            deleting={deleteBusy && deleting?.id === p.id}
          />
        ))}
        <button className="pj-card pj-new" data-tour="pj-new" onClick={() => { setCreating(true); setErr(''); }}>
          <span className="pj-new-ic">＋</span>
          <span>新建项目</span>
        </button>
      </div>
      {deleting && (
        <ProjectDeleteDialog project={deleting} busy={deleteBusy} error={deleteErr}
          onCancel={() => { if (!deleteBusy) setDeleting(null); }} onConfirm={confirmDelete} />
      )}

      {creating && (
        <div className="modal-ov" onClick={(e) => { if (e.target === e.currentTarget) setCreating(false); }}>
          <div className="modal">
            <h3>新建项目</h3>
            <form onSubmit={(e) => { e.preventDefault(); void submit(); }}>
            <div className="field"><label>项目名 *</label>
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoFocus /></div>
            <div className="field"><label>业务领域</label>
              <input value={form.domain} placeholder="如：对公营销"
                onChange={(e) => setForm({ ...form, domain: e.target.value })} /></div>
            <div className="field"><label>目标用户</label>
              <input value={form.targetUsers} placeholder="如：客户经理"
                onChange={(e) => setForm({ ...form, targetUsers: e.target.value })} /></div>
            <div className="field"><label>项目背景（可选）</label>
              <textarea rows={3} placeholder="可填写项目来源或背景；建模目标将在 AI 辅助构建中单独确认"
                value={form.background} onChange={(e) => setForm({ ...form, background: e.target.value })} /></div>
            <div className="field"><label>资料级别</label>
              <select value={form.sensitivityLevel} onChange={(e) => {
                const sensitivityLevel = e.target.value;
                setForm({
                  ...form,
                  sensitivityLevel,
                  workflowProfile: {
                    ...form.workflowProfile,
                    externalResearch: sensitivityLevel === 'restricted'
                      ? false
                      : form.workflowProfile.externalResearch,
                  },
                });
              }}>
                <option value="standard">普通</option>
                <option value="restricted">敏感</option>
              </select>
            </div>
            <fieldset className="pj-options">
              <legend>流程</legend>
              <p className="pj-options-note">
                建模阶段统一使用 AI 辅助构建；验证阶段固定包含场景推演、数据源绑定和评测。
              </p>
              <label><input type="checkbox"
                checked={form.workflowProfile.releaseRequested}
                onChange={(e) => setProfile({ releaseRequested: e.target.checked })} />正式交付</label>
              <label><input type="checkbox"
                disabled={form.sensitivityLevel === 'restricted'}
                checked={form.workflowProfile.externalResearch}
                onChange={(e) => setProfile({ externalResearch: e.target.checked })} />联网调研</label>
            </fieldset>
            <details className="pj-more">
              <summary>更多设置</summary>
              <div className="field"><label>术语偏好</label>
                <input value={form.glossaryPrefs} placeholder="如：统一称为客户经理"
                  onChange={(e) => setForm({ ...form, glossaryPrefs: e.target.value })} /></div>
            </details>
            {err && <div className="lerr">{err}</div>}
            <div className="foot">
              <button type="button" className="btn" disabled={submitting} onClick={() => setCreating(false)}>取消</button>
              <button type="submit" className="btn btn-p" disabled={submitting}>{submitting ? '创建中…' : '创建'}</button>
            </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
