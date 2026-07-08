import { useEffect, useState } from 'react';
import { authFetch } from '../utils/auth';
import Markdown from './Markdown';
import { DIR_ICONS, DIR_LABELS, findDirEntries } from './useKbBrowser';

// 知识包工作台内容区：目录树在应用侧边栏（KbSidebarTree），这里是
// 「当前目录条目列表 + 文件内容」两栏。文件内容经 /raw-file 按相对路径读取，
// Markdown 渲染 / 源码切换；yaml/json/py 等按源码高亮块展示。
const EXT_LANG = { '.yaml': 'yaml', '.yml': 'yaml', '.json': 'json', '.py': 'python', '.sql': 'sql', '.csv': 'csv', '.txt': 'text' };

export default function KnowledgeWorkbench({ kb }) {
  const {
    pkg, pkgId, tree, treeLoading, sel, selectDir, selectFile,
    canManage, viewVersion, viewingActive, versionOverride, selectVersion,
    setPkgEnabled, activateVersion, deleteVersion, downloadPkg, opBusy, opNotice,
  } = kb;
  const [content, setContent] = useState('');
  const [contentErr, setContentErr] = useState('');
  const [contentLoading, setContentLoading] = useState(false);
  const [view, setView] = useState('render');
  const [filter, setFilter] = useState('');

  const entries = findDirEntries(tree, sel.dir);
  const fileEntry = sel.file
    ? findDirEntries(tree, sel.file.includes('/') ? sel.file.slice(0, sel.file.lastIndexOf('/')) : '')
      .find((e) => e.path === sel.file)
    : null;

  // 目录切换后自动选中第一个可查看文件
  useEffect(() => {
    if (sel.file || !entries.length) return undefined;
    const first = entries.find((e) => e.type === 'file' && e.viewable);
    if (!first) return undefined;
    const t = setTimeout(() => selectFile(first.path), 0);
    return () => clearTimeout(t);
  }, [sel.dir, sel.file, entries, selectFile]);

  useEffect(() => {
    if (!pkgId || !sel.file) { setContent(''); setContentErr(''); return undefined; }
    let cancelled = false;
    const t = setTimeout(async () => {
      setContentLoading(true);
      setContentErr('');
      try {
        const params = new URLSearchParams({ path: sel.file });
        if (versionOverride !== null) params.set('version', versionOverride);
        const res = await authFetch(`/agentic/ontologies/${encodeURIComponent(pkgId)}/raw-file?${params.toString()}`);
        const data = await res.json().catch(() => ({}));
        if (cancelled) return;
        if (res.ok) setContent(data.content || '');
        else { setContent(''); setContentErr(data.error || `读取失败（${res.status}）`); }
      } finally {
        if (!cancelled) setContentLoading(false);
      }
    }, 0);
    return () => { cancelled = true; clearTimeout(t); };
  }, [pkgId, sel.file, versionOverride]);

  const shown = entries.filter((e) => !filter || e.name.toLowerCase().includes(filter.toLowerCase()));
  const ext = sel.file ? sel.file.slice(sel.file.lastIndexOf('.')).toLowerCase() : '';
  const isMd = ext === '.md';
  const crumbs = sel.dir ? sel.dir.split('/') : [];

  if (!pkg) {
    return <div className="kbx"><div className="empty" style={{ flex: 1, alignSelf: 'center' }}>
      {treeLoading ? '加载中…' : '左侧选择或导入一个知识包'}</div></div>;
  }

  const versions = [...(pkg.versions || [])].sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
  const activeVersion = pkg.active_version || pkg.version || '';
  const onToggleEnabled = () => {
    if (pkg.enabled && !window.confirm(`禁用后 Agent 将无法使用「${pkg.name || pkgId}」，确认禁用？`)) return;
    setPkgEnabled(!pkg.enabled);
  };
  const onDeleteVersion = () => {
    if (!window.confirm(`确认删除「${pkg.name || pkgId}」的版本 ${viewVersion}？该版本文件将被永久删除。`)) return;
    deleteVersion();
  };

  return (
    <div className="kbwrap">
      <div className="kb-toolbar">
        <span className="kb-tb-name">{pkg.name || pkgId}</span>
        <span className={`tag ${pkg.enabled ? 'tag-ok' : 'tag-off'}`}>{pkg.enabled ? '已启用' : '已禁用'}</span>
        {activeVersion && <span className="tag tag-lang">激活版本 {activeVersion}</span>}
        {versions.length > 0 && (
          <label className="kb-tb-ver">查看版本
            <select value={viewVersion} onChange={(e) => selectVersion(e.target.value)}>
              {versions.map((v) => (
                <option key={v} value={v}>{v}{v === activeVersion ? '（激活）' : ''}</option>
              ))}
            </select>
          </label>
        )}
        <span className="kb-tb-sp" />
        <button className="btn" disabled={!!opBusy} onClick={downloadPkg}>
          {opBusy === 'download' ? '打包中…' : '下载知识包(zip)'}
        </button>
        {canManage && (
          <button className="btn" disabled={!!opBusy} onClick={onToggleEnabled}>
            {opBusy === 'status' ? '处理中…' : pkg.enabled ? '禁用知识包' : '启用知识包'}
          </button>
        )}
        {canManage && (viewingActive ? (
          <button className="btn" disabled>已是激活版本</button>
        ) : (
          <button className="btn btn-p" disabled={!!opBusy} onClick={activateVersion}>
            {opBusy === 'activate' ? '激活中…' : '设为激活版本'}
          </button>
        ))}
        {canManage && (
          <button className="btn btn-danger" disabled={!!opBusy || viewingActive || versions.length <= 1}
            title={viewingActive ? '不能删除激活版本，请先切换查看其他版本' : versions.length <= 1 ? '仅剩一个版本，不能删除' : ''}
            onClick={onDeleteVersion}>
            {opBusy === 'deleteVersion' ? '删除中…' : '删除版本'}
          </button>
        )}
      </div>
      {opNotice && <div className="kb-opnotice">{opNotice}</div>}
      <div className="kbx">
      <div className="col2" style={{ width: 320 }}>
        <div className="kb-crumbs">
          <a onClick={() => selectDir('')} className={!sel.dir ? 'cur' : ''}>{pkg.name || pkgId}</a>
          {crumbs.map((c, i) => {
            const path = crumbs.slice(0, i + 1).join('/');
            return <span key={path}> / <a onClick={() => selectDir(path)} className={i === crumbs.length - 1 ? 'cur' : ''}>{c}</a></span>;
          })}
        </div>
        <input className="kb-search" style={{ marginBottom: 10 }} placeholder="筛选…"
          value={filter} onChange={(e) => setFilter(e.target.value)} />
        {treeLoading && <div className="empty">加载中…</div>}
        {!treeLoading && !shown.length && <div className="empty">该目录暂无内容</div>}
        {shown.map((e) => (
          e.type === 'dir' ? (
            <div key={e.path} className="lrow" onClick={() => selectDir(e.path)}>
              <div className="ln">{DIR_ICONS[e.name] || '▸'} {e.name}/ <span style={{ color: 'var(--muted)', fontWeight: 400 }}>{DIR_LABELS[e.name] || ''}</span></div>
              <div className="ld">{(e.children || []).length} 项</div>
            </div>
          ) : (
            <div key={e.path} className={`lrow ${e.path === sel.file ? 'on' : ''}`}
              onClick={() => e.viewable !== false && selectFile(e.path)}
              style={e.viewable === false ? { opacity: .55, cursor: 'not-allowed' } : undefined}>
              <div className="ln">≡ {e.name}</div>
              <div className="ld">{(e.size / 1024).toFixed(1)} KB{e.viewable === false ? ' · 不支持在线查看' : ''}</div>
            </div>
          )
        ))}
      </div>
      <div className="col3">
        {!sel.file && (
          <div className="empty" style={{ paddingTop: 80 }}>
            选择左侧文件查看内容{sel.dir ? '' : '，或进入某个目录'}
          </div>
        )}
        {sel.file && (
          <>
            <div className="doc-h">
              ≡ {fileEntry?.name || sel.file.split('/').pop()}
              {isMd && (
                <span className="viewtog">
                  <button className={view !== 'source' ? 'on' : ''} onClick={() => setView('render')}>渲染</button>
                  <button className={view === 'source' ? 'on' : ''} onClick={() => setView('source')}>源码</button>
                </span>
              )}
            </div>
            <div className="doc-sub">{pkgId}/{sel.file}</div>
            {contentLoading && <div className="empty">加载中…</div>}
            {!contentLoading && contentErr && <div className="empty">{contentErr}</div>}
            {!contentLoading && !contentErr && (
              isMd && view !== 'source'
                ? <div style={{ maxWidth: 860 }}><Markdown>{stripFrontmatter(content)}</Markdown></div>
                : <pre className="srcview" data-lang={EXT_LANG[ext] || ''}>{content}</pre>
            )}
          </>
        )}
      </div>
      </div>
    </div>
  );
}

// objects/*.md 顶部有 YAML frontmatter（resource/key/title…），渲染视图里裁掉
function stripFrontmatter(md) {
  const m = /^---\n[\s\S]*?\n---\n?/.exec(md || '');
  return m ? md.slice(m[0].length) : md;
}
