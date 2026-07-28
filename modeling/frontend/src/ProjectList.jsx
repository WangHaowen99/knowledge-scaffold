import React, { useEffect, useState } from 'react';
import { listProjects, createProject, deleteProject } from './api.js';

const STAGE_LABEL = {
  project_setup: '项目创建', ingest: '资料解析', analyze: '初步分析', interview: '知识萃取访谈',
  competency: '胜任力问题', generate: '包生成', review: '包评审', data_source: '数据源配置',
  testcases: '用例录入', evaluate: '评测运行', optimize: '优化迭代',
};
const STAGE_KEYS = Object.keys(STAGE_LABEL);

export function ProjectCard({ project: p, onOpen, onRequestDelete }) {
  const stageIdx = STAGE_KEYS.indexOf(p.stage);
  const pct = stageIdx >= 0 ? Math.round(((stageIdx + 1) / STAGE_KEYS.length) * 100) : 0;
  return (
    <div className="pj-card" onClick={() => onOpen(p.id)}>
      <button type="button" className="pj-delete" title="删除项目" aria-label={`删除项目 ${p.name}`}
        onClick={(event) => { event.stopPropagation(); onRequestDelete(p); }}>×</button>
      <h3 title={p.name}>{p.name}</h3>
      <div className={`pd ${p.background ? '' : 'pd-empty'}`}>{p.background || '暂无场景背景描述'}</div>
      <div className="pj-prog"
        title={stageIdx >= 0 ? `建模进度：${STAGE_LABEL[p.stage]}（${stageIdx + 1}/${STAGE_KEYS.length}）` : undefined}>
        <div className="pj-prog-in" style={{ width: `${pct}%` }} />
      </div>
      <div className="pm">
        <span className="tag tag-lang">{STAGE_LABEL[p.stage] || p.stage}</span>
        {p.domain && <span className="tag tag-ok" title={p.domain}>{p.domain}</span>}
        <span className="pj-date">{(p.updatedAt || '').slice(0, 10)}</span>
      </div>
    </div>
  );
}

export function ProjectDeleteDialog({ project, busy, error, onCancel, onConfirm }) {
  return (
    <div className="modal-ov" onClick={(event) => {
      if (!busy && event.target === event.currentTarget) onCancel();
    }}>
      <div className="modal pj-delete-modal" role="dialog" aria-modal="true" aria-labelledby="project-delete-title">
        <h3 id="project-delete-title">删除项目</h3>
        <p className="pj-delete-question">确认删除项目「{project.name}」吗？</p>
        <p className="pj-delete-warning">将永久删除 modeling 中的资料、访谈、知识包版本、评测记录和优化建议，此操作不可恢复。</p>
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
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: '', domain: '', background: '' });
  const [err, setErr] = useState('');
  const [deleting, setDeleting] = useState(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteErr, setDeleteErr] = useState('');

  const load = () => listProjects().then((d) => setProjects(d.projects)).finally(() => setLoading(false));
  useEffect(() => { load(); }, []);

  const submit = async () => {
    if (!form.name.trim()) { setErr('项目名必填'); return; }
    try {
      const { project } = await createProject(form);
      setCreating(false);
      onOpen(project.id);
    } catch (e) { setErr(e.message); }
  };

  const requestDelete = (project) => {
    setDeleting(project);
    setDeleteErr('');
  };

  const confirmDelete = async () => {
    if (!deleting || deleteBusy) return;
    setDeleteBusy(true); setDeleteErr('');
    try {
      await deleteProject(deleting.id);
      setProjects((current) => current.filter((project) => project.id !== deleting.id));
      setDeleting(null);
    } catch (e) { setDeleteErr(e.message); }
    finally { setDeleteBusy(false); }
  };

  return (
    <div>
      {loading && <div className="empty">加载中…</div>}
      <div className="pj-grid" data-tour="pj-grid">
        {projects.map((project) => (
          <ProjectCard key={project.id} project={project} onOpen={onOpen} onRequestDelete={requestDelete} />
        ))}
        <button className="pj-card pj-new" data-tour="pj-new" onClick={() => { setCreating(true); setErr(''); }}>＋ 新建建模项目</button>
      </div>

      {deleting && (
        <ProjectDeleteDialog project={deleting} busy={deleteBusy} error={deleteErr}
          onCancel={() => { if (!deleteBusy) setDeleting(null); }} onConfirm={confirmDelete} />
      )}

      {creating && (
        <div className="modal-ov" onClick={(e) => { if (e.target === e.currentTarget) setCreating(false); }}>
          <div className="modal">
            <h3>新建建模项目</h3>
            <div className="field"><label>项目名 *</label>
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoFocus /></div>
            <div className="field"><label>业务领域</label>
              <input value={form.domain} placeholder="如：工程变更影响分析"
                onChange={(e) => setForm({ ...form, domain: e.target.value })} /></div>
            <div className="field"><label>场景背景</label>
              <textarea rows={4} placeholder="这个知识包要解决什么问题、给谁用、有什么术语偏好…"
                value={form.background} onChange={(e) => setForm({ ...form, background: e.target.value })} /></div>
            {err && <div className="lerr">{err}</div>}
            <div className="foot">
              <button className="btn" onClick={() => setCreating(false)}>取消</button>
              <button className="btn btn-p" onClick={submit}>创建并进入</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
