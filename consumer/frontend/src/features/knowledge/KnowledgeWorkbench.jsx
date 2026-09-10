import { useEffect, useMemo, useRef, useState } from 'react';

import { authFetch } from '../../shared/api/auth';
import { friendlyRequestError } from '../../shared/api/errors';
import EvolutionWorkbench from '../evolution';
import { useAuthContext } from '../auth';
import ImportFlowDialog from './import/ImportFlowDialog';
import KnowledgeDataSources from './KnowledgeDataSources';
import OntologyGraphPanel from './graph/OntologyGraphPanel';
import {
  MARKDOWN_UPLOAD_CANCEL_NOTICE,
  resolveKnowledgeWorkbenchNotices,
  useNativeFilePicker,
} from './lib/browserInterop';
import { DIR_ICONS, DIR_LABELS, findDirEntries } from './hooks/useKbBrowser';
import { packageDisplayLabel } from './lib/packagePresentation';
import Markdown from '../../shared/ui/Markdown';
import { ToastNotice } from '../../shared/ui/feedback';
import { EmptyState, Modal, ScopeTabs, StatusTag } from '../../shared/ui/primitives';
import businessCheckpointGuide from './content/businessCheckpointGuide.js';
import './knowledge.css';

const EXT_LANG = { '.md': 'markdown', '.yaml': 'yaml', '.yml': 'yaml', '.json': 'json', '.py': 'python', '.sql': 'sql', '.csv': 'csv', '.txt': 'text' };
const KB_TABS = [
  { id: 'files', label: '文件与版本' },
  { id: 'data', label: '数据源' },
  { id: 'graph', label: '本体图' },
  { id: 'evolution', label: '记忆进化' },
];

export default function KnowledgeWorkbench({ kb, focusRequest, traceMessages = [], traceMetadata = {} }) {
  const { workspaceId } = useAuthContext();
  const {
    packages, pkgsLoading, packagesError, retryPackages, pkg, pkgId, selectPkg,
    tree, treeLoading, treeError, retryTree, sel, selectDir, selectFile,
    canActivate, canManage, viewVersion, viewingActive, versionOverride, selectVersion,
    setPkgEnabled, setBusinessCheckpointEnabled, activateVersion, deleteVersion, deletePackage, downloadPkg,
    clearOperationNotice, opBusy, opNotice,
    importPreview, clearImportPreview, reloadTree, knowledgeRevision, refreshKnowledge,
    importFlow, setImportAcknowledged, confirmImportSafety, setImportNewVersion,
    resolveImportConflict, retryImport, reopenImportConflict, cancelImport,
  } = kb;
  const [activeTab, setActiveTab] = useState('files');
  const [content, setContent] = useState('');
  const [contentErr, setContentErr] = useState('');
  const [contentLoading, setContentLoading] = useState(false);
  const [view, setView] = useState('render');
  const [filter, setFilter] = useState('');
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [fileBusy, setFileBusy] = useState('');
  const [fileNotice, setFileNotice] = useState('');
  const [checkpointDialog, setCheckpointDialog] = useState('');
  const fileAbortRef = useRef(null);
  const fileScopeRef = useRef('');
  fileScopeRef.current = `${workspaceId || ''}:${pkgId || ''}:${sel.file || ''}:${versionOverride || ''}`;

  const entries = useMemo(() => findDirEntries(tree, sel.dir), [tree, sel.dir]);
  const traceInput = useMemo(
    () => ({ messages: traceMessages, metadata: traceMetadata }),
    [traceMessages, traceMetadata],
  );
  const fileEntry = useMemo(() => (
    sel.file
      ? findDirEntries(tree, sel.file.includes('/') ? sel.file.slice(0, sel.file.lastIndexOf('/')) : '')
        .find((entry) => entry.path === sel.file)
      : null
  ), [sel.file, tree]);

  useEffect(() => {
    if (focusRequest?.tab && KB_TABS.some((item) => item.id === focusRequest.tab)) {
      setActiveTab(focusRequest.tab);
    }
    if (!focusRequest?.path) return;
    if (focusRequest.ontologyId && focusRequest.ontologyId !== pkgId) return;
    if (!treeContainsPath(tree, focusRequest.path)) return;
    setActiveTab('files');
    selectFile(focusRequest.path);
  }, [focusRequest, pkgId, selectFile, tree]);

  useEffect(() => {
    if (sel.file || !entries.length) return undefined;
    const first = entries.find((entry) => entry.type === 'file' && entry.viewable);
    if (!first) return undefined;
    const timer = setTimeout(() => selectFile(first.path), 0);
    return () => clearTimeout(timer);
  }, [sel.dir, sel.file, entries, selectFile]);

  useEffect(() => {
    fileAbortRef.current?.abort();
    fileAbortRef.current = null;
    setEditing(false);
    setFileBusy('');
    if (!pkgId || !sel.file) {
      setContent('');
      setEditContent('');
      setContentErr('');
      setContentLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    fileAbortRef.current = controller;
    const requestScope = fileScopeRef.current;
    const timer = setTimeout(async () => {
      setContentLoading(true);
      setContent('');
      setEditContent('');
      setContentErr('');
      try {
        const params = new URLSearchParams({ path: sel.file });
        if (versionOverride !== null) params.set('version', versionOverride);
        const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(pkgId)}/raw-file?${params.toString()}`, {
          signal: controller.signal,
        });
        const payload = await response.json().catch(() => ({}));
        if (controller.signal.aborted || requestScope !== fileScopeRef.current) return;
        if (response.ok) {
          setContent(payload.content || '');
          setEditContent(payload.content || '');
        } else {
          setContent('');
          setContentErr(payload.error || `读取失败（${response.status}）`);
        }
      } catch (error) {
        if (!controller.signal.aborted && requestScope === fileScopeRef.current && error?.name !== 'AbortError') {
          setContent('');
          setContentErr(friendlyRequestError(error, '文件读取失败'));
        }
      } finally {
        if (!controller.signal.aborted && requestScope === fileScopeRef.current) setContentLoading(false);
        if (fileAbortRef.current === controller) fileAbortRef.current = null;
      }
    }, 0);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [knowledgeRevision, pkgId, sel.file, versionOverride, workspaceId]);

  useEffect(() => {
    setFileNotice('');
    setCheckpointDialog('');
  }, [pkgId, versionOverride, workspaceId]);

  useEffect(() => {
    setFilter('');
  }, [pkgId, workspaceId]);

  const importDialog = (
    <ImportFlowDialog state={importFlow} onAcknowledge={setImportAcknowledged}
      onConfirmSafety={confirmImportSafety} onNewVersionChange={setImportNewVersion}
      onResolveConflict={resolveImportConflict} onRetry={retryImport}
      onReopenConflict={reopenImportConflict} onCancel={cancelImport} />
  );

  const shown = entries.filter((entry) => !filter || entry.name.toLowerCase().includes(filter.toLowerCase()));
  const ext = sel.file ? sel.file.slice(sel.file.lastIndexOf('.')).toLowerCase() : '';
  const isMd = ext === '.md';
  const crumbs = sel.dir ? sel.dir.split('/') : [];
  const versions = [...(pkg?.versions || [])].sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
  const activeVersion = pkg?.active_version || pkg?.version || '';
  const parts = sel.file?.split('/') || [];
  const [folderName] = parts;
  const editableFolder = parts.length === 2 && ['objects', 'operations'].includes(folderName)
    ? folderName
    : '';
  const canEditFile = canManage && viewingActive && isMd && Boolean(editableFolder);
  const uploadFolder = ['objects', 'operations'].includes(sel.dir) ? sel.dir : '';

  const onToggleEnabled = () => {
    if (pkg.enabled && !window.confirm(`禁用后 Agent 将无法使用「${pkg.name || pkgId}」，确认禁用？`)) return;
    setPkgEnabled(!pkg.enabled);
  };
  const checkpointEnabled = pkg?.business_checkpoint?.enabled === true;
  const checkpointTemplateAvailable = pkg?.business_checkpoint?.template_available === true;
  const onToggleBusinessCheckpoint = () => {
    setFileNotice('');
    if (checkpointEnabled) {
      setBusinessCheckpointEnabled(false);
      return;
    }
    setCheckpointDialog('confirm');
  };
  const confirmBusinessCheckpoint = () => {
    setCheckpointDialog('');
    setBusinessCheckpointEnabled(true);
  };
  const onDeleteVersion = () => {
    if (!window.confirm(`确认删除「${pkg.name || pkgId}」的版本 ${viewVersion}？该版本文件将被永久删除。`)) return;
    deleteVersion();
  };
  const onDeletePackage = () => {
    if (!window.confirm(`确认删除整个知识包「${pkg.name || pkgId}」及其所有版本？该操作不可撤销。`)) return;
    deletePackage();
  };

  const saveFile = async () => {
    if (!canEditFile) return;
    fileAbortRef.current?.abort();
    const controller = new AbortController();
    fileAbortRef.current = controller;
    const requestScope = fileScopeRef.current;
    setFileBusy('save');
    setFileNotice('');
    try {
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(pkgId)}/file`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: editableFolder, name: parts.at(-1), content: editContent }),
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (requestScope !== fileScopeRef.current) return;
      if (response.ok) {
        setContent(editContent);
        setEditing(false);
        setFileNotice('文件已保存到当前激活版本');
        reloadTree();
      } else setFileNotice(`保存失败：${payload.error || payload.detail || response.status}`);
    } catch (error) {
      if (!controller.signal.aborted && requestScope === fileScopeRef.current && error?.name !== 'AbortError') {
        setFileNotice(friendlyRequestError(error, '文件保存失败'));
      }
    } finally {
      if (!controller.signal.aborted && requestScope === fileScopeRef.current) setFileBusy('');
      if (fileAbortRef.current === controller) fileAbortRef.current = null;
    }
  };

  const uploadFile = async (file) => {
    if (!uploadFolder || !file) return;
    fileAbortRef.current?.abort();
    const controller = new AbortController();
    fileAbortRef.current = controller;
    const requestScope = fileScopeRef.current;
    setFileBusy('upload');
    setFileNotice('');
    try {
      const form = new FormData();
      form.append('folder', uploadFolder);
      form.append('file', file);
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(pkgId)}/upload`, {
        method: 'POST',
        body: form,
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (requestScope !== fileScopeRef.current) return;
      if (response.ok) {
        const uploadedPath = `${uploadFolder}/${payload.name || file.name}`;
        setFileNotice(`${payload.existed ? '已覆盖' : '已上传'} ${payload.name || file.name}`);
        const refreshed = await refreshKnowledge({ ontologyId: pkgId });
        if (refreshed && requestScope === fileScopeRef.current) selectFile(uploadedPath);
      } else setFileNotice(`上传失败：${payload.error || payload.detail || response.status}`);
    } catch (error) {
      if (!controller.signal.aborted && requestScope === fileScopeRef.current && error?.name !== 'AbortError') {
        setFileNotice(friendlyRequestError(error, '文件上传失败'));
      }
    } finally {
      if (!controller.signal.aborted && requestScope === fileScopeRef.current) setFileBusy('');
      if (fileAbortRef.current === controller) fileAbortRef.current = null;
    }
  };

  const markdownUploadPicker = useNativeFilePicker({
    onSelect: uploadFile,
    onCancel: () => setFileNotice(MARKDOWN_UPLOAD_CANCEL_NOTICE),
  });
  const workbenchNotices = resolveKnowledgeWorkbenchNotices(fileNotice, opNotice);
  const openMarkdownUploadPicker = () => {
    setFileNotice('');
    clearOperationNotice();
    markdownUploadPicker.open();
  };

  if (!pkg) {
    const title = pkgsLoading
      ? '正在加载知识包…'
      : packagesError
        ? '知识包加载失败'
        : packages.length
          ? '请选择知识包'
          : '暂无知识包';
    const description = packagesError
      || (pkgsLoading
        ? '正在读取当前工作空间的知识包列表。'
        : packages.length
          ? '选择知识包后可浏览版本、目录与文件内容。'
          : '导入知识包后，可在这里统一浏览和管理内容。');
    return (
      <>
        <div className="kbx kb-empty-shell"><EmptyState title={title} description={description}
          action={packagesError ? <button className="btn" type="button" onClick={() => retryPackages()}>重新加载</button> : undefined} /></div>
        {importDialog}
      </>
    );
  }

  return (
    <div className="kbwrap">
      <div className="kb-toolbar">
        <label className="kb-tb-package">切换知识包
          <select aria-label="切换知识包" value={pkgId || ''} disabled={pkgsLoading}
            title={packageDisplayLabel(pkg)} onChange={(event) => selectPkg(event.target.value)}>
            {packages.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>{packageDisplayLabel(candidate)}</option>
            ))}
          </select>
        </label>
        <StatusTag status={pkg.enabled ? 'enabled' : 'disabled'}>{pkg.enabled ? '已启用' : '已禁用'}</StatusTag>
        <StatusTag status={checkpointEnabled ? 'enabled' : 'disabled'}>
          CheckPoint {checkpointEnabled ? '已开启' : '已关闭'}
        </StatusTag>
        {activeVersion && <StatusTag status="active">激活版本 {activeVersion}</StatusTag>}
        {versions.length > 0 && (
          <label className="kb-tb-ver">查看版本
            <select value={viewVersion} onChange={(event) => selectVersion(event.target.value)}>
              {versions.map((version) => <option key={version} value={version}>{version}{version === activeVersion ? '（激活）' : ''}</option>)}
            </select>
          </label>
        )}
        <span className="kb-tb-sp" />
        <button className="btn" disabled={Boolean(opBusy)} onClick={() => { setFileNotice(''); downloadPkg(); }}>{opBusy === 'download' ? '打包中…' : '下载 ZIP'}</button>
        {canManage && <button className="btn" disabled={Boolean(opBusy)} onClick={onToggleEnabled}>{opBusy === 'status' ? '处理中…' : pkg.enabled ? '禁用' : '启用'}</button>}
        {canManage && (
          <button
            className="btn"
            disabled={Boolean(opBusy) || (!checkpointEnabled && (!pkg.enabled || !checkpointTemplateAvailable))}
            title={checkpointTemplateAvailable ? '控制 LLM 是否获得 CheckPoint 工具' : '知识包缺少 assets/business-checkpoint-template.md'}
            onClick={onToggleBusinessCheckpoint}
          >
            {opBusy === 'businessCheckpoint' ? '处理中…' : checkpointEnabled ? '关闭 CheckPoint' : '开启 CheckPoint'}
          </button>
        )}
        {canActivate && !viewingActive && <button className="btn btn-p" disabled={Boolean(opBusy)} onClick={activateVersion}>{opBusy === 'activate' ? '激活中…' : '设为激活版本'}</button>}
        {canManage && <button className="btn btn-danger" disabled={Boolean(opBusy) || viewingActive || versions.length <= 1} onClick={onDeleteVersion}>{opBusy === 'deleteVersion' ? '删除中…' : '删除版本'}</button>}
        {canManage && <button className="btn btn-danger" disabled={Boolean(opBusy)} onClick={onDeletePackage}>{opBusy === 'deletePackage' ? '删除中…' : '删除知识包'}</button>}
      </div>
      {workbenchNotices.inlineMessage && <div className="kb-opnotice">{workbenchNotices.inlineMessage}</div>}
      <div className="kb-tabs"><ScopeTabs items={KB_TABS} value={activeTab} onChange={setActiveTab} ariaLabel="知识包工作台页签" /></div>

      {activeTab === 'files' && (
        <div className="kbx">
          <div className="col2">
            <div className="kb-crumbs">
              <button onClick={() => selectDir('')} className={!sel.dir ? 'cur' : ''}>{pkg.name || pkgId}</button>
              {crumbs.map((crumb, index) => {
                const path = crumbs.slice(0, index + 1).join('/');
                return <span key={path}> / <button onClick={() => selectDir(path)} className={index === crumbs.length - 1 ? 'cur' : ''}>{crumb}</button></span>;
              })}
            </div>
            <div className="kb-file-tools">
              <input className="kb-search" placeholder="筛选文件…" value={filter} onChange={(event) => setFilter(event.target.value)} />
              {canManage && viewingActive && uploadFolder && <button className="btn" disabled={Boolean(fileBusy)} onClick={openMarkdownUploadPicker}>＋ 上传 Markdown</button>}
              <input ref={markdownUploadPicker.inputRef} hidden type="file" accept=".md,text/markdown"
                onChange={markdownUploadPicker.handleChange} />
            </div>
            {!viewingActive && <div className="ro-banner">历史版本只读。需要修改时先设为激活版本。</div>}
            {sel.dir === 'assets' && <div className="ro-banner">Assets 支持浏览和下载，在线编辑与上传已关闭。</div>}
            {treeLoading && <div className="empty">加载中…</div>}
            {!treeLoading && treeError && <EmptyState compact title="目录加载失败" description={treeError}
              action={<button className="btn" type="button" onClick={retryTree}>重新加载</button>} />}
            {!treeLoading && !treeError && !shown.length && <EmptyState compact title="该目录暂无内容" />}
            {!treeLoading && !treeError && shown.map((entry) => entry.type === 'dir' ? (
              <button type="button" key={entry.path} className="lrow" onClick={() => selectDir(entry.path)}>
                <span className="ln">{DIR_ICONS[entry.name] || '▸'} {entry.name}/ <small>{DIR_LABELS[entry.name] || ''}</small></span>
                <span className="ld">{(entry.children || []).length} 项</span>
              </button>
            ) : (
              <button type="button" key={entry.path} className={`lrow ${entry.path === sel.file ? 'on' : ''}`}
                onClick={() => entry.viewable !== false && selectFile(entry.path)} disabled={entry.viewable === false}>
                <span className="ln">≡ {entry.name}</span>
                <span className="ld">{((entry.size || 0) / 1024).toFixed(1)} KB{entry.viewable === false ? ' · 不支持在线查看' : ''}</span>
              </button>
            ))}
          </div>
          <div className="col3">
            {!sel.file && <EmptyState title="选择文件查看内容" description="Objects 与 Operations 的 Markdown 文件可在激活版本中编辑。" />}
            {sel.file && (
              <>
                <div className="doc-h">
                  <span>≡ {fileEntry?.name || parts.at(-1)}</span>
                  <span className="viewtog">
                    {isMd && !editing && <><button type="button" aria-pressed={view !== 'source'} className={view !== 'source' ? 'on' : ''} onClick={() => setView('render')}>渲染</button><button type="button" aria-pressed={view === 'source'} className={view === 'source' ? 'on' : ''} onClick={() => setView('source')}>源码</button></>}
                    {canEditFile && !editing && <button className="btn" onClick={() => { setEditContent(content); setEditing(true); }}>编辑</button>}
                    {editing && <><button className="btn" onClick={() => { setEditing(false); setEditContent(content); }}>取消</button><button className="btn btn-p" disabled={fileBusy === 'save'} onClick={saveFile}>{fileBusy === 'save' ? '保存中…' : '保存'}</button></>}
                  </span>
                </div>
                <div className="doc-sub">{pkgId}/{sel.file}</div>
                {contentLoading && <div className="empty">加载中…</div>}
                {!contentLoading && contentErr && <EmptyState title="文件读取失败" description={contentErr} />}
                {!contentLoading && !contentErr && editing && <textarea className="kb-file-editor" value={editContent} spellCheck="false" onChange={(event) => setEditContent(event.target.value)} />}
                {!contentLoading && !contentErr && !editing && (isMd && view !== 'source'
                  ? <div className="kb-markdown"><Markdown>{stripFrontmatter(content)}</Markdown></div>
                  : <pre className="srcview" data-lang={EXT_LANG[ext] || ''}>{content}</pre>)}
              </>
            )}
          </div>
        </div>
      )}
      {activeTab === 'data' && <div className="kb-scroll"><KnowledgeDataSources key={`${workspaceId}:${pkgId}`}
        ontologyId={pkgId} onDataSourceChanged={retryPackages} /></div>}
      {activeTab === 'graph' && <div className="kb-scroll"><OntologyGraphPanel ontologyId={pkgId} version={viewVersion}
        traceMessages={traceInput} onOpenSource={(path) => { setActiveTab('files'); selectFile(path); }} /></div>}
      {activeTab === 'evolution' && <div className="kb-scroll"><EvolutionWorkbench ontologyId={pkgId}
        onKnowledgeChanged={refreshKnowledge} readOnly={!viewingActive}
        viewVersion={viewVersion} activeVersion={activeVersion} /></div>}

      {importPreview && (
        <Modal title="导入结果预览" description="导入已完成。请核对识别到的内容与服务端校验结果。" onClose={clearImportPreview}
          actions={<button className="btn btn-p" onClick={clearImportPreview}>完成</button>} wide>
          <ImportPreview preview={importPreview} />
        </Modal>
      )}
      {checkpointDialog === 'confirm' && (
        <Modal title="开启 CheckPoint" onClose={() => setCheckpointDialog('')}
          actions={<><button className="btn" onClick={() => setCheckpointDialog('')}>取消</button><button className="btn btn-p" onClick={confirmBusinessCheckpoint}>确认开启</button></>}>
          <p className="kb-checkpoint-confirm-copy">
            CheckPoint 需要本体包配套配置才能完整生效，本体包配置规范参考：
            <a href="#business-checkpoint-guide" onClick={(event) => { event.preventDefault(); setCheckpointDialog('guide'); }}>本体包 Checkpoint 通用规范</a>
          </p>
        </Modal>
      )}
      {checkpointDialog === 'guide' && (
        <Modal title="本体包 Checkpoint 通用规范" onClose={() => setCheckpointDialog('confirm')}
          actions={<button className="btn btn-p" onClick={() => setCheckpointDialog('confirm')}>返回</button>} wide>
          <div className="kb-checkpoint-template-download">
            <span>通用模板：</span>
            <a href="/business-checkpoint-template.md" download="business-checkpoint-template.md">下载 business-checkpoint-template.md</a>
          </div>
          <Markdown>{businessCheckpointGuide}</Markdown>
        </Modal>
      )}
      <ToastNotice message={workbenchNotices.toastMessage} tone={workbenchNotices.toastTone} duration={5200} />
      {importDialog}
    </div>
  );
}

function ImportPreview({ preview }) {
  if (typeof preview === 'string') return <pre className="import-preview-content">{preview}</pre>;
  const entries = Array.isArray(preview) ? preview : Object.entries(preview || {});
  if (!entries.length) return <EmptyState compact title="服务端未返回额外预览信息" />;
  return <div className="import-preview">{Array.isArray(preview)
    ? preview.map((item, index) => <pre className="import-preview-content" key={item.path || index}>{typeof item === 'string' ? item : JSON.stringify(item, null, 2)}</pre>)
    : entries.map(([key, value]) => <div key={key}><strong>{key}</strong><pre className="import-preview-content">{typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</pre></div>)}</div>;
}

function stripFrontmatter(markdown) {
  const match = /^---\n[\s\S]*?\n---\n?/.exec(markdown || '');
  return match ? markdown.slice(match[0].length) : markdown;
}

function treeContainsPath(entries, path) {
  return (entries || []).some((entry) => entry.path === path || treeContainsPath(entry.children, path));
}
