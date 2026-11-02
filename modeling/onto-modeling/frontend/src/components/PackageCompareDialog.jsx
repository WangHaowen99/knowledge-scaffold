// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Input, Modal, Segmented, Select, Spin, Table, Tabs, Tag, Upload, message } from 'antd';
import { ArrowLeft, ArrowLeftRight, ChevronLeft, ChevronRight, FileDiff, PanelLeftClose, PanelLeftOpen, Search, UploadCloud, X } from 'lucide-react';
import { api } from '../lib/api.js';
import { createAbortableRequest, createGenerationGuard, createLatestRequest, createSerialPoller } from '../lib/latestRequest.js';
import { canApplyReleaseRefresh, chooseInitialReleases, debounceValue, mergeReleaseRefresh, packageIdentity, representativeChangesLabel, shouldClearSelectedFile, validPageForTotal } from '../lib/packageCompareView.js';
import ReadonlyPackageDiff from './ReadonlyPackageDiff.jsx';
import './PackageCompareDialog.css';

const STATUS = { added: '新增', deleted: '删除', modified: '修改', unchanged: '未变化' };
const CATEGORY = { indexes: '索引', metadata: '包元数据', mappings: '数据映射', metrics: '指标', objects: '对象', operations: '操作', rules: '规则', scripts: '脚本', other: '其他附件' };
const STATUS_COLOR = { added: 'green', deleted: 'red', modified: 'orange' };
const keyOf = (value) => value ? (value.kind === 'upload' ? `upload:${value.input_id}` : `release:${value.ontology_id || value.hub_package_id || ''}:${value.release_id}`) : undefined;
const packageOf = packageIdentity;
const displayName = (value) => value?.kind === 'upload' ? (value.package_version || value.package_name || value.filename || '本地知识包') : (value?.version || value?.release_id || '请选择版本');
const timestamp = (value) => value?.committed_at || value?.created_at;
const sourceFoot = (value) => {
  if (!value) return '请选择一个对比来源';
  const count = value.file_count ?? '—';
  if (value.kind === 'upload') return `本地临时知识包 · ${value.package_name || value.filename || '知识包'} · ${count} 个文件`;
  return `已发布快照 · ${value.package_name || '知识包'} · ${count} 个文件`;
};

function SourceCard({ side, value, releases, uploads, mode, uploading, onMode, onChange, onUpload, onCancelUpload }) {
  const choices = mode === 'release' ? releases : uploads;
  const options = choices.map((item) => ({ value: keyOf(item), item, label: item.kind === 'upload'
    ? `${item.package_version || item.filename || item.input_id} · ${item.file_count ?? '—'} 个文件`
    : `${item.version || item.release_id}${timestamp(item) ? `  ·  ${new Date(timestamp(item)).toLocaleString()}` : ''}` }));
  return <div className="package-compare-source-card">
    <div className="package-compare-source-top"><b>{side === 'left' ? '左侧 · 基准版本' : '右侧 · 目标版本'}</b>
      <Segmented size="small" value={mode} onChange={onMode} options={[{ value: 'release', label: '已发布版本' }, { value: 'upload', label: '导入知识包' }]} /></div>
    {mode === 'release' ? <Select value={keyOf(value)} options={options} style={{ width: '100%' }} placeholder="选择已发布版本" onChange={(key) => onChange(options.find((item) => item.value === key)?.item || null)} />
      : <div className="package-compare-upload-row"><Select value={keyOf(value)} options={options} style={{ flex: 1 }} placeholder="选择已导入知识包" onChange={(key) => onChange(options.find((item) => item.value === key)?.item || null)} />
        <Upload showUploadList={false} accept=".zip" beforeUpload={onUpload} disabled={uploading}><Button loading={uploading} icon={!uploading && <UploadCloud size={15} />}>{uploading ? '正在上传并校验' : '选择 ZIP'}</Button></Upload>
        {uploading && <Button aria-label="取消上传" icon={<X size={14} />} onClick={onCancelUpload}>取消</Button>}</div>}
    <div className="package-compare-source-foot">{sourceFoot(value)}</div>
  </div>;
}

function TextPane({ value, side }) {
  if (!value) return <div className="package-compare-empty-text">此版本无该文件</div>;
  if (value.encoding === 'binary') return <div className="package-compare-empty-text">二进制文件，仅展示文件大小（{value.bytes} bytes）</div>;
  if (['too_large', 'complex_text'].includes(value.encoding)) return <div className="package-compare-empty-text">{value.reason || '文件过大，已关闭正文差异预览'}（{value.bytes} bytes）</div>;
  return <div className={`package-compare-text-pane ${side}`}>{String(value.text || '').split('\n').map((line, index) => <div key={`${side}-${index}`}><span>{index + 1}</span><code>{line || ' '}</code></div>)}</div>;
}

export default function PackageCompareDialog({ projectId, projectName = '', releases, initialReleaseId = '', open, onClose }) {
  const [left, setLeft] = useState(null); const [right, setRight] = useState(null);
  const [modes, setModes] = useState({ left: 'release', right: 'release' });
  const [uploads, setUploads] = useState([]); const [available, setAvailable] = useState(releases || []);
  const [result, setResult] = useState(null); const [files, setFiles] = useState({ items: [], total: 0 });
  const [selected, setSelected] = useState(null); const [diff, setDiff] = useState(null);
  const [loading, setLoading] = useState(false); const [fileLoading, setFileLoading] = useState(false);
  const [uploading, setUploading] = useState({ left: false, right: false });
  const [error, setError] = useState(''); const [phase, setPhase] = useState('');
  const [status, setStatus] = useState('changed'); const [category, setCategory] = useState('');
  const [query, setQuery] = useState(''); const [page, setPage] = useState(1); const [tab, setTab] = useState('overview');
  const [queryInput, setQueryInput] = useState(''); const [sidebarOpen, setSidebarOpen] = useState(true);
  const [crossPackageConfirmed, setCrossPackageConfirmed] = useState(false);
  const compareRequest = useRef(createLatestRequest()); const diffRequest = useRef(createLatestRequest());
  const uploadRequest = useRef({ left: createLatestRequest(), right: createLatestRequest() });
  const uploadAbort = useRef({ left: createAbortableRequest(), right: createAbortableRequest() });
  const uploadIds = useRef({ left: '', right: '' });
  const releaseRequest = useRef(createLatestRequest()); const resultRef = useRef(null);
  const actionGeneration = useRef(createGenerationGuard()); const comparisonIds = useRef(new Set()); const inputIds = useRef(new Set());
  const queryDebounce = useRef(debounceValue((value) => { setQuery(value); setPage(1); }, 200));
  const compareRequestId = useRef('');
  const modesRef = useRef({ left: 'release', right: 'release' });
  const sourceGeneration = useRef({ left: 0, right: 0 });

  const normalizedReleases = useMemo(() => available.map((item) => ({ ...item, kind: 'release' })), [available]);
  const refOf = (value) => value.kind === 'upload' ? { kind: 'upload', input_id: value.input_id } : { kind: 'release', ontology_id: value.ontology_id || value.hub_package_id, release_id: value.release_id };
  const isCrossPackage = Boolean(packageOf(left) && packageOf(right) && packageOf(left) !== packageOf(right));
  const setTrackedResult = (value) => { resultRef.current = value; if (value?.comparison_id) comparisonIds.current.add(value.comparison_id); setResult(value); };
  const deleteComparison = async (pid, id) => { if (!id) return; try { await api.deletePackageCompare(pid, id); comparisonIds.current.delete(id); } catch (e) { if ([404, 410].includes(e.status)) comparisonIds.current.delete(id); } };
  const deleteInput = async (pid, id) => { try { await api.deletePackageCompareInput(pid, id); inputIds.current.delete(id); } catch (e) { if ([403, 404, 410].includes(e.status)) inputIds.current.delete(id); } };
  const cleanupResources = async (pid) => { const comparisons = [...comparisonIds.current]; const inputs = [...inputIds.current]; await Promise.allSettled(comparisons.map((id) => deleteComparison(pid, id))); await Promise.allSettled(inputs.map((id) => deleteInput(pid, id))); };
  const invalidateUpload = (side) => { const requestId = uploadIds.current[side]; uploadIds.current[side] = ''; uploadAbort.current[side].abort(); uploadRequest.current[side].invalidate(); if (requestId) void api.cancelPackageCompareUpload(projectId, requestId).catch(() => {}); };
  const resetLocalComparison = ({ keepConfirmation = false, keepRequestId = false } = {}) => { queryDebounce.current.cancel(); resultRef.current = null; if (!keepRequestId) compareRequestId.current = ''; setResult(null); setFiles({ items: [], total: 0 }); setSelected(null); setDiff(null); setError(''); setPhase(''); setLoading(false); setFileLoading(false); if (!keepConfirmation) setCrossPackageConfirmed(false); setStatus('changed'); setCategory(''); setQuery(''); setQueryInput(''); setPage(1); setTab('overview'); };
  const clearComparison = ({ removeRemote = true } = {}) => { actionGeneration.current.invalidate(); compareRequest.current.invalidate(); diffRequest.current.invalidate(); const id = resultRef.current?.comparison_id; if (removeRemote && id) void deleteComparison(projectId, id); resetLocalComparison(); };
  const invalidateRequests = () => { actionGeneration.current.invalidate(); compareRequest.current.invalidate(); diffRequest.current.invalidate(); releaseRequest.current.invalidate(); invalidateUpload('left'); invalidateUpload('right'); queryDebounce.current.cancel(); };
  const resetFilterPage = (fn) => { fn(); setPage(1); };
  const selectSource = (side, value) => { sourceGeneration.current[side] += 1; invalidateUpload(side); setUploading((old) => ({ ...old, [side]: false })); clearComparison(); (side === 'left' ? setLeft : setRight)(value); };
  const changeMode = (side, mode) => { modesRef.current[side] = mode; setModes((old) => ({ ...old, [side]: mode })); const choices = mode === 'release' ? normalizedReleases : uploads; selectSource(side, choices[0] || null); };
  const swapSources = () => { invalidateUpload('left'); invalidateUpload('right'); sourceGeneration.current.left += 1; sourceGeneration.current.right += 1; const nextModes = { left: modes.right, right: modes.left }; modesRef.current = nextModes; setUploading({ left: false, right: false }); setLeft(right); setRight(left); setModes(nextModes); clearComparison(); };

  useEffect(() => {
    if (!open) return undefined;
    invalidateRequests(); resetLocalComparison(); setUploads([]); setUploading({ left: false, right: false }); modesRef.current = { left: 'release', right: 'release' }; sourceGeneration.current = { left: 0, right: 0 }; setModes({ left: 'release', right: 'release' }); setStatus('changed'); setCategory(''); setQuery(''); setQueryInput(''); setPage(1); setSidebarOpen(true); setAvailable(releases || []);
    const seed = (releases || []).map((item) => ({ ...item, kind: 'release' }));
    const initialSources = chooseInitialReleases(seed, initialReleaseId);
    setLeft(initialSources.left);
    setRight(initialSources.right);
    const releaseGeneration = { ...sourceGeneration.current };
    releaseRequest.current.run(async () => { const rows = []; let offset = 0; let total = Infinity; while (offset < total) { const value = await api.packageReleases(projectId, { offset, limit: 100 }); const batch = value.releases || value.items || []; rows.push(...batch); total = Number(value.total ?? rows.length); if (!batch.length) break; offset += batch.length; } return rows; }, { success: (rows) => { setAvailable(rows); const normalized = rows.map((item) => ({ ...item, kind: 'release' })); if (canApplyReleaseRefresh(modesRef.current.left, sourceGeneration.current.left, releaseGeneration.left)) setLeft((currentLeft) => mergeReleaseRefresh({ releases: normalized, initialReleaseId, currentLeft }).left); if (canApplyReleaseRefresh(modesRef.current.right, sourceGeneration.current.right, releaseGeneration.right)) setRight((currentRight) => mergeReleaseRefresh({ releases: normalized, initialReleaseId, currentRight }).right); }, failure: (e) => setError(e.message), settled: () => {} });
    return () => { invalidateRequests(); void cleanupResources(projectId); };
  }, [open, projectId, initialReleaseId]);

  const upload = ({ file, side }) => { clearComparison(); uploadRequest.current[side].invalidate(); const signal = uploadAbort.current[side].begin(); const requestId = globalThis.crypto?.randomUUID?.().replaceAll('-', '') || `upload_${Date.now()}`; uploadIds.current[side] = requestId; setUploading((old) => ({ ...old, [side]: true })); uploadRequest.current[side].run(() => api.packageCompareUpload(projectId, file, { signal, requestId }), {
    success: (item) => { inputIds.current.add(item.input_id); const value = { ...item, filename: file.name, kind: 'upload' }; setUploads((old) => [...old.filter((entry) => entry.input_id !== value.input_id), value]); (side === 'left' ? setLeft : setRight)(value); setCrossPackageConfirmed(false); },
    failure: (e) => { if (e.name !== 'AbortError') message.error(e.message); }, settled: () => { uploadAbort.current[side].finish(signal); if (uploadIds.current[side] === requestId) uploadIds.current[side] = ''; setUploading((old) => ({ ...old, [side]: false })); }, stale: (item) => { if (item?.input_id) void api.deletePackageCompareInput(projectId, item.input_id).catch(() => {}); },
  }); return false; };
  const cancelUpload = (side) => { invalidateUpload(side); setUploading((old) => ({ ...old, [side]: false })); };
  const compare = async () => {
    if (!left || !right || keyOf(left) === keyOf(right)) return;
    if (isCrossPackage && !crossPackageConfirmed) { setError('两侧属于不同知识包，请先确认跨包对比'); return; }
    const generation = actionGeneration.current.begin(); const previous = resultRef.current?.comparison_id; if (previous) await deleteComparison(projectId, previous); if (!actionGeneration.current.isCurrent(generation)) return;
    compareRequest.current.invalidate(); diffRequest.current.invalidate(); resetLocalComparison({ keepConfirmation: true, keepRequestId: true }); setLoading(true); setStatus('changed');
    compareRequestId.current = compareRequestId.current || globalThis.crypto?.randomUUID?.().replaceAll('-', '') || `compare_${Date.now()}`;
    await compareRequest.current.run(() => api.packageCompare(projectId, refOf(left), refOf(right), { allow_cross_package: isCrossPackage, request_id: compareRequestId.current }), { success: (value) => { setTrackedResult(value); setPhase(value.phase || value.status || 'queued'); }, failure: (e) => setError(e.message), settled: () => setLoading(false), stale: (value) => { if (value?.comparison_id) void api.deletePackageCompare(projectId, value.comparison_id).catch(() => {}); } });
  };
  const openDiff = async (row) => { diffRequest.current.invalidate(); setSelected(row); setDiff(null); setFileLoading(true); await diffRequest.current.run(() => api.packageCompareFileDiff(projectId, resultRef.current.comparison_id, row.path), { success: setDiff, failure: (e) => message.error(e.message), settled: () => setFileLoading(false) }); };

  useEffect(() => {
    if (!result || result.status === 'ready' || result.status === 'failed') return undefined;
    return createSerialPoller({ poll: () => api.packageCompareResult(projectId, result.comparison_id), onValue: (value) => { setPhase(value.phase || value.status || 'running'); if (value.status === 'ready' || value.status === 'failed') setTrackedResult(value); }, onError: (e) => { setError(e.message); if ([401, 403, 410].includes(e.status)) { setTrackedResult({ ...resultRef.current, status: 'failed', error: e.message, error_code: e.status === 410 ? 'EXPIRED' : 'FORBIDDEN' }); return true; } return false; }, isDone: (value) => !value || value.status === 'ready' || value.status === 'failed' });
  }, [projectId, result?.comparison_id, result?.status]);
  useEffect(() => {
    if (!result || result.status !== 'ready') return undefined; let active = true;
    api.packageCompareFiles(projectId, result.comparison_id, { status: status || undefined, category: category || undefined, q: query, offset: (page - 1) * 50, limit: 50 }).then((value) => { if (!active) return; const validPage = validPageForTotal(page, value.total); if (validPage !== page) { setPage(validPage); return; } setFiles(value); const items = value.items || []; if (tab === 'files' && items.length && !items.some((row) => row.path === selected?.path)) void openDiff(items[0]); else if (shouldClearSelectedFile(items)) { diffRequest.current.invalidate(); setSelected(null); setDiff(null); setFileLoading(false); } }).catch((e) => { if (active) setError(e.message); }); return () => { active = false; };
  }, [projectId, result?.comparison_id, result?.status, status, category, query, page, tab]);

  const cancelComparison = async () => { actionGeneration.current.invalidate(); compareRequest.current.invalidate(); diffRequest.current.invalidate(); const id = resultRef.current?.comparison_id; resetLocalComparison(); if (id) await deleteComparison(projectId, id); };
  const closeDialog = () => { invalidateRequests(); resetLocalComparison(); setLeft(null); setRight(null); setUploads([]); setUploading({ left: false, right: false }); onClose(); };
  const showFiles = (nextStatus = 'changed', nextCategory = '', path = '') => { queryDebounce.current.cancel(); resetFilterPage(() => { setStatus(nextStatus); setCategory(nextCategory); setQueryInput(path); setQuery(path); }); setTab('files'); };
  const summary = result?.status === 'ready' ? result.summary : null; const rows = files.items || [];
  const categories = Object.entries(CATEGORY).map(([key, label]) => ({ key, label, ...(summary?.categories?.[key] || {}) })).filter((row) => summary?.categories?.[row.key]);
  const pageCount = Math.max(1, Math.ceil((files.total || 0) / 50)); const changedTotal = summary ? summary.files.added + summary.files.deleted + summary.files.modified : 0;
  const projectLabel = projectName || left?.package_name || right?.package_name || '当前项目';

  const affected = (summary?.affected_categories || []).map((key) => CATEGORY[key] || key);
  const overview = summary && <div className="package-compare-overview"><div className="package-compare-summary"><div className="package-compare-summary-title"><FileDiff size={17} /> 版本差异概述 <Tag color="blue">根据文件差异生成</Tag></div><p>从 <strong>{displayName(left)}</strong> 到 <strong>{displayName(right)}</strong>，知识包文件数由 <strong>{summary.left_file_count}</strong> 个变为 <strong>{summary.right_file_count}</strong> 个：新增 <strong>{summary.files.added}</strong> 个、删除 <strong>{summary.files.deleted}</strong> 个、修改 <strong>{summary.files.modified}</strong> 个，{summary.files.unchanged} 个未变化。{affected.length ? `变化涉及${affected.join('、')}。` : '两侧内容完全一致。'}</p>{summary.evidence?.length > 0 && <div className="package-compare-evidence-wrap"><div className="package-compare-evidence-title">{representativeChangesLabel(summary.evidence.length, changedTotal)}</div><div className="package-compare-evidence">{summary.evidence.map((item) => <Button key={`${item.status}:${item.path}`} size="small" onClick={() => showFiles(item.status, '', item.path)}>{STATUS[item.status]} {item.display_name || item.path}</Button>)}</div></div>}</div>
    <div className="package-compare-stats">{Object.entries(STATUS).map(([key, label]) => <button key={key} type="button" className={`package-compare-stat ${key}`} onClick={() => showFiles(key)}><span>{label}</span><strong>{summary.files[key]}</strong><small>个文件 · 点击查看</small></button>)}</div>
    <div className="package-compare-section-title"><b>按文件类别统计</b><span>所有发布文件均纳入统计，包含包元数据及数据映射</span></div>
    <Table size="small" pagination={false} rowKey="key" dataSource={categories} columns={[{ title: '文件类别', dataIndex: 'label' }, { title: '基准版本', render: (_, row) => (row.deleted || 0) + (row.modified || 0) + (row.unchanged || 0) }, { title: '目标版本', render: (_, row) => (row.added || 0) + (row.modified || 0) + (row.unchanged || 0) }, ...Object.entries(STATUS).map(([key, label]) => ({ title: label, dataIndex: key, render: (value, row) => value ? <Button type="link" size="small" onClick={() => showFiles(key, row.key)}>{value}</Button> : <span className="package-compare-muted">—</span> }))]} />
  </div>;

  const filesView = summary && <div className="package-compare-files"><div className="package-compare-filters"><Input prefix={<Search size={15} />} allowClear placeholder="搜索文件路径或业务名称" style={{ width: 280 }} value={queryInput} onChange={(e) => { setQueryInput(e.target.value); queryDebounce.current.schedule(e.target.value); }} />
    <Select aria-label="差异状态" style={{ width: 150 }} value={status} onChange={(value) => resetFilterPage(() => setStatus(value))} options={[{ value: 'changed', label: '仅有差异' }, ...Object.entries(STATUS).map(([value, label]) => ({ value, label })), { value: '', label: '全部状态' }]} />
    <Select aria-label="文件类别" style={{ width: 150 }} value={category} onChange={(value) => resetFilterPage(() => setCategory(value))} options={[{ value: '', label: '所有类别' }, ...Object.entries(CATEGORY).map(([value, label]) => ({ value, label }))]} /><span className="package-compare-file-count">{files.total} 个文件</span></div>
    <div className={`package-compare-workspace ${sidebarOpen ? '' : 'sidebar-closed'}`}><aside className="package-compare-sidebar">{rows.length ? rows.map((row) => <button type="button" key={row.path} className={selected?.path === row.path ? 'selected' : ''} onClick={() => openDiff(row)}><div><Tag color={STATUS_COLOR[row.status]}>{STATUS[row.status]}</Tag><span>{row.display_name || row.path.split('/').pop()}</span></div><small>{row.path}</small></button>) : <div className="package-compare-no-files">没有符合条件的文件</div>}{files.total > 50 && <div className="package-compare-pager"><Button size="small" icon={<ChevronLeft size={13} />} disabled={page <= 1} onClick={() => setPage((v) => v - 1)} /><span>{page}/{pageCount}</span><Button size="small" icon={<ChevronRight size={13} />} disabled={page >= pageCount} onClick={() => setPage((v) => v + 1)} /></div>}</aside>
      <section className="package-compare-diff"><header><div><Button type="text" size="small" aria-label={sidebarOpen ? '收起文件列表' : '展开文件列表'} icon={sidebarOpen ? <PanelLeftClose size={15} /> : <PanelLeftOpen size={15} />} onClick={() => setSidebarOpen((value) => !value)} /><b>{selected?.path || '选择文件查看差异'}</b>{selected && <Tag color={STATUS_COLOR[selected.status]}>{STATUS[selected.status]}</Tag>}</div>{selected && <Tag>只读</Tag>}</header>
        {fileLoading ? <div className="package-compare-loading"><Spin /></div> : diff ? <><div className="package-compare-version-head"><span><i className="package-compare-legend deleted" />基准 · {displayName(left)}</span><span><i className="package-compare-legend added" />目标 · {displayName(right)}</span></div>{!['binary', 'too_large', 'complex_text'].includes(diff.left?.encoding) && !['binary', 'too_large', 'complex_text'].includes(diff.right?.encoding) && diff.left && diff.right ? <ReadonlyPackageDiff left={diff.left.text || ''} right={diff.right.text || ''} status={diff.status} leftEncoding={diff.left.encoding} rightEncoding={diff.right.encoding} leftBytes={diff.left.bytes} rightBytes={diff.right.bytes} /> : <div className="package-compare-fallback"><TextPane value={diff.left} side="left" /><TextPane value={diff.right} side="right" /></div>}</> : <div className="package-compare-loading package-compare-muted">从左侧选择一个文件</div>}</section></div>
  </div>;

  return <Modal className="package-compare-modal" title={<div className="package-compare-title"><Button type="text" icon={<ArrowLeft size={16} />} onClick={closeDialog}>已发布版本</Button><b>知识包版本对比</b><Tag color="blue">{projectLabel}</Tag></div>} open={open} onCancel={closeDialog} width="96vw" style={{ top: 20, maxWidth: 1600 }} footer={null} destroyOnHidden>
    {error && <Alert closable type="error" showIcon message={error} />}
    <div className="package-compare-source-row"><SourceCard side="left" value={left} releases={normalizedReleases} uploads={uploads} mode={modes.left} uploading={uploading.left} onMode={(mode) => changeMode('left', mode)} onChange={(value) => selectSource('left', value)} onUpload={(file) => upload({ file, side: 'left' })} onCancelUpload={() => cancelUpload('left')} /><Button className="package-compare-swap" aria-label="交换左右版本" icon={<ArrowLeftRight size={17} />} onClick={swapSources} disabled={!left && !right} /><SourceCard side="right" value={right} releases={normalizedReleases} uploads={uploads} mode={modes.right} uploading={uploading.right} onMode={(mode) => changeMode('right', mode)} onChange={(value) => selectSource('right', value)} onUpload={(file) => upload({ file, side: 'right' })} onCancelUpload={() => cancelUpload('right')} /></div>
    {left && right && keyOf(left) === keyOf(right) && <Alert type="info" showIcon message="左右选择的是同一发布记录，无需对比" />}
    {isCrossPackage && !crossPackageConfirmed && <Alert type="warning" showIcon message="两侧属于不同知识包" description={`基准：${left?.package_name || packageOf(left)}（${displayName(left)}）；目标：${right?.package_name || packageOf(right)}（${displayName(right)}）。跨包结果不代表版本继承关系。`} action={<Button size="small" onClick={() => { setCrossPackageConfirmed(true); setError(''); }}>确认跨包对比</Button>} />}
    <div className="package-compare-action"><span>差异方向：左 → 右。对比为只读，不修改项目内容。</span><Button type="primary" loading={loading} disabled={!left || !right || keyOf(left) === keyOf(right) || uploading.left || uploading.right || (isCrossPackage && !crossPackageConfirmed)} onClick={compare}>{summary ? '重新对比' : '开始对比'}</Button></div>
    {(loading || (result && result.status !== 'ready' && result.status !== 'failed')) && <div className="package-compare-loading"><Spin tip={`正在准备对比：${phase || '排队中'}`} /><Button size="small" onClick={cancelComparison}>取消对比</Button></div>}
    {result?.status === 'failed' && <Alert type="error" showIcon message={result.error || '对比失败'} action={<Button size="small" onClick={compare}>重试</Button>} />}
    {!summary && !loading && !result && <div className="package-compare-blank"><FileDiff size={42} /><h3>选择两个知识包，查看版本变化</h3><p>支持已发布版本与版本、版本与本地 ZIP、两个本地 ZIP 对比。</p></div>}
    {summary && <Tabs activeKey={tab} onChange={setTab} items={[{ key: 'overview', label: '差异总览', children: overview }, { key: 'files', label: `文件差异（${changedTotal}）`, children: filesView }]} />}
  </Modal>;
}
