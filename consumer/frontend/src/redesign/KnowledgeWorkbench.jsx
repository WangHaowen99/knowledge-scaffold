import { useEffect, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, Code2, Database, Download, FileText, Folder, GitCompareArrows, MoreHorizontal, Power, RefreshCw, Trash2, Wrench } from 'lucide-react';
import { authFetch } from '../utils/auth';
import { friendlyRequestError } from '../utils/requestErrors';
import Markdown from './Markdown';
import { DIR_LABELS, findDirEntries } from './useKbBrowser';
import { ConfirmDialog, SkeletonState, ToastNotice } from './UiFeedback';

// 知识包工作台内容区：目录树在应用侧边栏（KbSidebarTree），这里是
// 「当前目录条目列表 + 文件内容」两栏。文件内容经 /raw-file 按相对路径读取，
// Markdown 渲染 / 源码切换；yaml/json/py 等按源码高亮块展示。
const EXT_LANG = { '.yaml': 'yaml', '.yml': 'yaml', '.json': 'json', '.py': 'python', '.sql': 'sql', '.csv': 'csv', '.txt': 'text' };
const DIR_ICON_COMPONENTS = { objects: Database, operations: Wrench, scripts: Code2, mappings: GitCompareArrows };

function DirectoryIcon({ name, size = 14 }) {
  const Icon = DIR_ICON_COMPONENTS[name] || Folder;
  return <Icon size={size} strokeWidth={1.9} />;
}

export default function KnowledgeWorkbench({ kb }) {
  const {
    pkg, pkgId, pkgsLoading, packagesError, retryPackages, tree, treeLoading, sel, selectDir, selectFile,
    canManage, viewVersion, viewingActive, versionOverride, selectVersion,
    setPkgEnabled, activateVersion, deleteVersion, downloadPkg, opBusy, opNotice,
  } = kb;
  const [content, setContent] = useState('');
  const [contentErr, setContentErr] = useState('');
  const [contentLoading, setContentLoading] = useState(false);
  const [contentReloadKey, setContentReloadKey] = useState(0);
  const [view, setView] = useState('render');
  const [filter, setFilter] = useState('');
  const [confirmAction, setConfirmAction] = useState(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef(null);

  useEffect(() => {
    if (!moreOpen) return undefined;
    const closeOnOutside = (event) => { if (!moreRef.current?.contains(event.target)) setMoreOpen(false); };
    const closeOnEscape = (event) => { if (event.key === 'Escape') setMoreOpen(false); };
    document.addEventListener('mousedown', closeOnOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [moreOpen]);

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
      } catch (error) {
        if (!cancelled) {
          setContent('');
          setContentErr(friendlyRequestError(error, '文件读取失败'));
        }
      } finally {
        if (!cancelled) setContentLoading(false);
      }
    }, 0);
    return () => { cancelled = true; clearTimeout(t); };
  }, [pkgId, sel.file, versionOverride, contentReloadKey]);

  const shown = entries.filter((e) => !filter || e.name.toLowerCase().includes(filter.toLowerCase()));
  const ext = sel.file ? sel.file.slice(sel.file.lastIndexOf('.')).toLowerCase() : '';
  const isMd = ext === '.md';
  const crumbs = sel.dir ? sel.dir.split('/') : [];

  if (!pkg) {
    return <div className="kbx"><div style={{ flex: 1, alignSelf: 'center', width: 'min(520px, 90%)' }}>
      {pkgsLoading ? <SkeletonState rows={4} label="正在加载知识包" /> : packagesError
        ? <div className="kb-load-error"><AlertCircle size={20} /><span>{packagesError}</span><button className="btn" type="button" onClick={retryPackages}><RefreshCw size={13} />重试</button></div>
        : <div className="empty">左侧选择或导入一个知识包</div>}</div></div>;
  }

  const versions = [...(pkg.versions || [])].sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
  const activeVersion = pkg.active_version || pkg.version || '';
  const onToggleEnabled = () => {
    if (pkg.enabled) {
      setConfirmAction({ type: 'disable', title: '禁用知识包', description: `禁用后 Agent 将无法使用「${pkg.name || pkgId}」，已有文件和版本不会被删除。`, label: '确认禁用' });
      return;
    }
    setPkgEnabled(!pkg.enabled);
  };
  const onDeleteVersion = () => {
    setConfirmAction({ type: 'delete', title: '删除知识包版本', description: `将永久删除「${pkg.name || pkgId}」的版本 ${viewVersion}，此操作无法撤销。`, label: '删除版本' });
  };
  const confirmOperation = () => {
    if (confirmAction?.type === 'disable') setPkgEnabled(false);
    if (confirmAction?.type === 'delete') deleteVersion();
    setConfirmAction(null);
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
        {canManage && (viewingActive ? (
          <span className="kb-active-label"><CheckCircle2 size={14} />当前激活版本</span>
        ) : (
          <button className="btn btn-p" disabled={!!opBusy} onClick={activateVersion}>
            <CheckCircle2 size={14} />{opBusy === 'activate' ? '激活中…' : '设为激活版本'}
          </button>
        ))}
        <div className="kb-more" ref={moreRef}>
          <button type="button" className="btn" aria-label="更多知识包操作" aria-haspopup="menu" aria-expanded={moreOpen}
            onClick={() => setMoreOpen((value) => !value)}><MoreHorizontal size={16} />更多</button>
          {moreOpen && <div className="kb-more-menu" role="menu">
            <button role="menuitem" disabled={!!opBusy} onClick={() => { setMoreOpen(false); downloadPkg(); }}><Download size={14} />{opBusy === 'download' ? '打包中…' : '下载知识包'}</button>
            {canManage && <button role="menuitem" disabled={!!opBusy} onClick={() => { setMoreOpen(false); onToggleEnabled(); }}><Power size={14} />{pkg.enabled ? '禁用知识包' : '启用知识包'}</button>}
            {canManage && <button className="danger" disabled={!!opBusy || viewingActive || versions.length <= 1}
              title={viewingActive ? '不能删除激活版本，请先切换查看其他版本' : versions.length <= 1 ? '仅剩一个版本，不能删除' : ''}
              role="menuitem" onClick={() => { setMoreOpen(false); onDeleteVersion(); }}><Trash2 size={14} />删除当前版本</button>}
          </div>}
        </div>
      </div>
      <ToastNotice message={opNotice} />
      <ConfirmDialog open={Boolean(confirmAction)} title={confirmAction?.title} description={confirmAction?.description}
        confirmLabel={confirmAction?.label} danger busy={!!opBusy} onConfirm={confirmOperation} onCancel={() => setConfirmAction(null)} />
      <div className="kbx">
      <div className="col2" style={{ width: 320 }}>
        <div className="kb-crumbs">
          <button type="button" onClick={() => selectDir('')} className={!sel.dir ? 'cur' : ''}>{pkg.name || pkgId}</button>
          {crumbs.map((c, i) => {
            const path = crumbs.slice(0, i + 1).join('/');
            return <span key={path}> / <button type="button" onClick={() => selectDir(path)} className={i === crumbs.length - 1 ? 'cur' : ''}>{c}</button></span>;
          })}
        </div>
        <input className="kb-search" style={{ marginBottom: 10 }} placeholder="筛选…"
          value={filter} onChange={(e) => setFilter(e.target.value)} />
        {treeLoading && <SkeletonState compact rows={5} label="正在读取目录" />}
        {!treeLoading && !shown.length && <div className="empty">该目录暂无内容</div>}
        {shown.map((e) => (
          e.type === 'dir' ? (
            <button type="button" key={e.path} className="lrow" onClick={() => selectDir(e.path)}>
              <div className="ln kb-entry-name"><DirectoryIcon name={e.name} />{e.name}/ <span style={{ color: 'var(--muted)', fontWeight: 400 }}>{DIR_LABELS[e.name] || ''}</span></div>
              <div className="ld">{(e.children || []).length} 项</div>
            </button>
          ) : (
            <button type="button" key={e.path} className={`lrow ${e.path === sel.file ? 'on' : ''}`}
              disabled={e.viewable === false} onClick={() => selectFile(e.path)}>
              <div className="ln kb-entry-name"><FileText size={14} />{e.name}</div>
              <div className="ld">{(e.size / 1024).toFixed(1)} KB{e.viewable === false ? ' · 不支持在线查看' : ''}</div>
            </button>
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
              <FileText size={15} /> {fileEntry?.name || sel.file.split('/').pop()}
              {isMd && (
                <span className="viewtog">
                  <button className={view !== 'source' ? 'on' : ''} onClick={() => setView('render')}>渲染</button>
                  <button className={view === 'source' ? 'on' : ''} onClick={() => setView('source')}>源码</button>
                </span>
              )}
            </div>
            <div className="doc-sub">{pkgId}/{sel.file}</div>
            {contentLoading && <SkeletonState rows={6} label="正在读取文件" />}
            {!contentLoading && contentErr && <div className="kb-load-error"><AlertCircle size={20} /><span>{contentErr}</span><button className="btn" type="button" onClick={() => setContentReloadKey((value) => value + 1)}><RefreshCw size={13} />重试</button></div>}
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
