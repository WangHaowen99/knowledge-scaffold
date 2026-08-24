import React, { useRef, useState } from 'react';
import { Download } from 'lucide-react';

import {
  deleteDocument,
  downloadDocument,
  listMaterialRevisions,
  updateProject,
  uploadDocument,
} from './api.js';

function responseFilename(response, fallback) {
  const header = response.headers.get('Content-Disposition') ?? '';
  const encoded = header.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (!encoded) return fallback;
  try { return decodeURIComponent(encoded); } catch { return fallback; }
}

function projectEditForm(project) {
  return {
    name: project.name ?? '',
    domain: project.domain ?? '',
    background: project.background ?? '',
    targetUsers: project.targetUsers ?? '',
    glossaryPrefs: project.glossaryPrefs ?? '',
    sensitivityLevel: project.sensitivityLevel ?? 'standard',
    workflowProfile: {
      enabledSteps: [...(project.workflowProfile?.enabledSteps ?? [])],
      requiredSteps: [...(project.workflowProfile?.requiredSteps ?? [])],
      releaseRequested: project.workflowProfile?.releaseRequested ?? true,
      externalResearch: project.workflowProfile?.externalResearch ?? false,
    },
  };
}

export function ProjectSetupView({ project, onChanged }) {
  const [form, setForm] = useState(() => projectEditForm(project));
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState('');

  const setProfile = (patch) => setForm((current) => ({
    ...current,
    workflowProfile: { ...current.workflowProfile, ...patch },
  }));

  const save = async (event) => {
    event.preventDefault();
    if (!form.name.trim()) { setMsg('项目名必填'); return; }
    setSaving(true); setMsg('');
    try {
      await updateProject(project.id, form);
      setMsg('已保存');
      await onChanged();
    } catch (error) {
      setMsg(error instanceof Error ? error.message : '项目保存失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="project-setup" onSubmit={save}>
      <div className="project-setup-fields">
        <div className="field"><label>项目名 *</label>
          <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></div>
        <div className="field"><label>业务领域</label>
          <input value={form.domain} placeholder="如：对公营销"
            onChange={(event) => setForm({ ...form, domain: event.target.value })} /></div>
        <div className="field"><label>目标用户</label>
          <input value={form.targetUsers} placeholder="如：客户经理"
            onChange={(event) => setForm({ ...form, targetUsers: event.target.value })} /></div>
        <div className="field"><label>资料级别</label>
          <select value={form.sensitivityLevel} onChange={(event) => {
            const sensitivityLevel = event.target.value;
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
          </select></div>
      </div>
      <div className="field"><label>项目背景（可选）</label>
        <textarea rows={3} placeholder="项目来源或背景；不等同于已确认的建模目标" value={form.background}
          onChange={(event) => setForm({ ...form, background: event.target.value })} /></div>
      <fieldset className="pj-options">
        <legend>流程</legend>
        <label><input type="checkbox" checked={form.workflowProfile.releaseRequested}
          onChange={(event) => setProfile({ releaseRequested: event.target.checked })} />正式交付</label>
        <label><input type="checkbox" disabled={form.sensitivityLevel === 'restricted'}
          checked={form.workflowProfile.externalResearch}
          onChange={(event) => setProfile({ externalResearch: event.target.checked })} />联网调研</label>
        <p className="pj-options-note">验证阶段固定包含场景推演、数据源绑定和评测；确定性脚本由 AI 辅助构建阶段按需确认。</p>
      </fieldset>
      <details className="pj-more">
        <summary>更多设置</summary>
        <div className="field"><label>术语偏好</label>
          <input value={form.glossaryPrefs} placeholder="如：统一称为客户经理"
            onChange={(event) => setForm({ ...form, glossaryPrefs: event.target.value })} /></div>
      </details>
      <div className="project-setup-actions">
        {msg && <span className={msg === '已保存' ? 'saved' : 'lerr'}>{msg}</span>}
        <button className="btn btn-p" type="submit" disabled={saving}>{saving ? '保存中…' : '保存'}</button>
      </div>
    </form>
  );
}

export function IngestView({ project, documents, onChanged }) {
  const inputRef = useRef(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [history, setHistory] = useState(null);

  const upload = async (files) => {
    if (!files?.length) return;
    setBusy(true); setMsg('');
    try {
      const results = [];
      for (const file of files) results.push(await uploadDocument(project.id, file));
      const changed = results.filter((result) => result.materialImpact?.affectedResources?.length).length;
      const formatOnly = results.filter((result) => result.materialRevision?.changeKind === 'format_only').length;
      setMsg([
        `已上传 ${files.length} 个文件并保存为不可变资料修订`,
        ...(changed ? [`${changed} 份资料影响已确认知识，相关步骤已标记需复核`] : []),
        ...(formatOnly ? [`${formatOnly} 份仅格式变化，不影响现有知识`] : []),
      ].join('；'));
      await onChanged();
    } catch (error) {
      setMsg(`上传失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (document) => {
    if (!window.confirm(`删除「${document.filename}」？`)) return;
    setBusy(true); setMsg('');
    try {
      const result = await deleteDocument(project.id, document.id);
      const affected = result.materialImpact?.affectedResources?.length ?? 0;
      setMsg(`已停用 ${document.filename} 的当前修订并保留历史${affected ? `；${affected} 项知识需复核` : ''}`);
      setHistory(null);
      await onChanged();
    } catch (error) {
      setMsg(`删除失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const download = async (document) => {
    setBusy(true); setMsg('');
    try {
      const response = await downloadDocument(project.id, document.id);
      const url = URL.createObjectURL(await response.blob());
      const link = window.document.createElement('a');
      link.href = url;
      link.download = responseFilename(response, document.filename);
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
      setMsg(response.headers.get('X-Material-Export-Mode') === 'normalized'
        ? `已下载 ${document.filename} 的可复用副本；历史环境未保留原始字节`
        : `已下载原始资料 ${document.filename}`);
    } catch (error) {
      setMsg(`下载失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className={`dropzone ${drag ? 'drag' : ''}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(event) => { event.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(event) => {
          event.preventDefault(); setDrag(false); void upload([...event.dataTransfer.files]);
        }}>
        {busy ? '处理中…' : '拖入或选择资料'}
        <input ref={inputRef} type="file" multiple style={{ display: 'none' }}
          accept=".xlsx,.xls,.csv,.md,.markdown,.txt,.docx,.doc,.pdf"
          onChange={(event) => { void upload([...event.target.files]); event.target.value = ''; }} />
      </div>
      {msg && <div style={{ fontSize: 12.5, color: 'var(--ink2)', marginTop: 10 }}>{msg}</div>}
      {documents.length > 0 && (
        <div style={{ marginTop: 16 }}>
          {documents.map((document) => (
            <div key={document.id} className="doc-row">
              <span className="dn">{document.filename}</span>
              <span className="tag tag-lang">修订 {document.revisionNo ?? 1}</span>
              {document.parseStatus === 'parsed' && <span className="tag tag-ok">已解析{document.tables ? ` · ${document.tables.length} 表` : ''}</span>}
              {document.parseStatus === 'failed' && <span className="tag tag-err" title={document.parseError}>解析失败</span>}
              <button className="btn doc-download" title={`下载 ${document.filename}`} disabled={busy}
                onClick={() => { void download(document); }}>
                <Download size={13} aria-hidden="true" />下载
              </button>
              <button className="btn" style={{ padding: '2px 8px', fontSize: 11.5 }} disabled={busy}
                onClick={async () => {
                  try {
                    const result = await listMaterialRevisions(project.id, document.sourceKey);
                    setHistory({ sourceKey: document.sourceKey, filename: document.filename, ...result });
                  } catch (error) {
                    setMsg(`读取修订历史失败：${error instanceof Error ? error.message : String(error)}`);
                  }
                }}>历史</button>
              <button className="tc-del" title="删除该资料" disabled={busy}
                onClick={() => { void remove(document); }}>✕</button>
            </div>
          ))}
        </div>
      )}
      {history && (
        <section className="material-revision-history" aria-label={`${history.filename} 修订历史`}>
          <div className="material-revision-history-head">
            <strong>{history.filename} · 修订历史</strong>
            <button className="tc-del" title="关闭" onClick={() => setHistory(null)}>✕</button>
          </div>
          {(history.revisions ?? []).map((revision) => (
            <div key={revision.id} className="material-revision-row">
              <span>修订 {revision.revisionNo}</span>
              <span className={`tag ${revision.lifecycleStatus === 'active' ? 'tag-ok' : 'tag-lang'}`}>
                {revision.lifecycleStatus === 'active' ? '当前' : revision.lifecycleStatus === 'deleted' ? '已停用' : '历史'}
              </span>
              <time>{new Date(revision.createdAt).toLocaleString()}</time>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
