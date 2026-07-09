import { useEffect, useState } from 'react';
import { listProjects, createProject } from './api.js';

const STAGE_LABEL = {
  project_setup: '项目创建', ingest: '资料解析', analyze: '初步分析', interview: '建模访谈',
  competency: '胜任力问题', generate: '包生成', review: '包评审', testcases: '用例录入',
  evaluate: '评测运行', optimize: '优化迭代',
};

export default function ProjectList({ onOpen }) {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: '', domain: '', background: '' });
  const [err, setErr] = useState('');

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

  return (
    <div>
      {loading && <div className="empty">加载中…</div>}
      <div className="pj-grid">
        {projects.map((p) => (
          <div key={p.id} className="pj-card" onClick={() => onOpen(p.id)}>
            <h3>{p.name}</h3>
            <div className="pd">{p.background || p.domain || '（无描述）'}</div>
            <div className="pm">
              <span className="tag tag-lang">{STAGE_LABEL[p.stage] || p.stage}</span>
              {p.domain && <span className="tag tag-ok">{p.domain}</span>}
              <span style={{ marginLeft: 'auto', color: 'var(--muted)', fontSize: 11.5 }}>{(p.updatedAt || '').slice(0, 10)}</span>
            </div>
          </div>
        ))}
        <button className="pj-card pj-new" onClick={() => { setCreating(true); setErr(''); }}>＋ 新建建模项目</button>
      </div>

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
