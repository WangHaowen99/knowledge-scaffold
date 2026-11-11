// 知识包版本对比（契约 PC-01～PC-08，ADR-0095）：元素、文案与行为照 develop 的 PackageCompareDialog，样式用 src/ui。
// 左右两侧各选「已发布版本」（Hub 上本项目关联包的版本，HB-05）或「导入知识包」（上传 ZIP），对比结果分「差异总览」
// 与「文件差异」两页。打开方式：不带初始版本时取最近两版；从某一行打开时左边是那一版（initialReleaseId，或按 initialVersion 找）。
// 关闭时删掉这次建的对比与上传；迟到的响应一律丢弃（lib/latestRequest）。
import { useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { createAbortableRequest, createGenerationGuard, createLatestRequest, createSerialPoller } from '../../lib/latestRequest.js';
import {
  canApplyReleaseRefresh, chooseInitialReleases, debounceValue, mergeReleaseRefresh, packageIdentity, representativeChangesLabel,
  shouldClearSelectedFile, validPageForTotal,
} from '../../lib/packageCompareView.js';
import {
  Button, Chip, Icon, Inp, Modal, Note, Seg, Select, Table, Tabs, cx,
} from '../../ui/index.jsx';
import ReadonlyPackageDiff from './ReadonlyPackageDiff.jsx';
import '../../styles/package-compare.css';

const STATUS = { added: '新增', deleted: '删除', modified: '修改', unchanged: '未变化' };
const CATEGORY = { indexes: '索引', metadata: '包元数据', mappings: '数据映射', metrics: '指标', objects: '对象', operations: '操作', rules: '规则', scripts: '脚本', other: '其他附件' };
const STATUS_TONE = { added: 'teal', deleted: 'red', modified: 'amber' };
const NO_TEXT = ['binary', 'too_large', 'complex_text'];
const PAGE = 50;
const keyOf = (value) => (value ? (value.kind === 'upload' ? `upload:${value.input_id}` : `release:${value.ontology_id || value.hub_package_id || ''}:${value.release_id}`) : undefined);
const packageOf = packageIdentity;
const displayName = (value) => (value?.kind === 'upload' ? (value.package_version || value.package_name || value.filename || '本地知识包') : (value?.version || value?.release_id || '请选择版本'));
const timestamp = (value) => value?.committed_at || value?.created_at;
const sourceFoot = (value) => {
  if (!value) return '请选择一个对比来源';
  const count = value.file_count ?? '—';
  if (value.kind === 'upload') return `本地临时知识包 · ${value.package_name || value.filename || '知识包'} · ${count} 个文件`;
  return `已发布快照 · ${value.package_name || '知识包'} · ${count} 个文件`;
};
const newRequestId = (prefix) => globalThis.crypto?.randomUUID?.().replaceAll('-', '') || `${prefix}_${Date.now()}`;

function SourceCard({ side, value, releases, uploads, mode, uploading, onMode, onChange, onUpload, onCancelUpload }) {
  const fileRef = useRef(null);
  const choices = mode === 'release' ? releases : uploads;
  const options = choices.map((item) => ({
    value: keyOf(item),
    item,
    label: item.kind === 'upload'
      ? `${item.package_version || item.filename || item.input_id} · ${item.file_count ?? '—'} 个文件`
      : `${item.version || item.release_id}${timestamp(item) ? `  ·  ${new Date(timestamp(item)).toLocaleString()}` : ''}`,
  }));
  const pick = (key) => onChange(options.find((item) => item.value === key)?.item || null);
  return (
    <div className="pc-card">
      <div className="row pc-card-top">
        <b className="grow">{side === 'left' ? '左侧 · 基准版本' : '右侧 · 目标版本'}</b>
        <Seg items={[['release', '已发布版本'], ['upload', '导入知识包']]} value={mode} onChange={onMode} />
      </div>
      {mode === 'release' ? (
        <Select value={keyOf(value)} options={options} onChange={pick} w="100%" placeholder="选择已发布版本" />
      ) : (
        <div className="row g8">
          <div className="grow" style={{ minWidth: 0 }}>
            <Select value={keyOf(value)} options={options} onChange={pick} w="100%" placeholder="选择已导入知识包" />
          </div>
          <input
            ref={fileRef}
            type="file"
            accept=".zip"
            hidden
            onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ''; if (file) onUpload(file); }}
          />
          <Button icon={uploading ? undefined : 'upload'} disabled={uploading} onClick={() => fileRef.current?.click()}>
            {uploading ? <><span className="spin" />正在上传并校验</> : '选择 ZIP'}
          </Button>
          {uploading ? <Button icon="x" aria-label="取消上传" onClick={onCancelUpload}>取消</Button> : null}
        </div>
      )}
      <div className="xs muted pc-card-foot">{sourceFoot(value)}</div>
    </div>
  );
}

function TextPane({ value, side }) {
  if (!value) return <div className="sm muted pc-empty-text">此版本无该文件</div>;
  if (value.encoding === 'binary') return <div className="sm muted pc-empty-text">二进制文件，仅展示文件大小（{value.bytes} bytes）</div>;
  if (['too_large', 'complex_text'].includes(value.encoding)) {
    return <div className="sm muted pc-empty-text">{value.reason || '文件过大，已关闭正文差异预览'}（{value.bytes} bytes）</div>;
  }
  return (
    <div className={cx('pc-text-pane', side)}>
      {String(value.text || '').split('\n').map((line, index) => <div key={`${side}-${index}`}><span>{index + 1}</span><code>{line || ' '}</code></div>)}
    </div>
  );
}

export default function PackageCompare({ projectId, projectName = '', releases, initialReleaseId = '', initialVersion = '', open, onClose }) {
  const { api } = useApp();
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
  const refOf = (value) => (value.kind === 'upload' ? { kind: 'upload', input_id: value.input_id } : { kind: 'release', ontology_id: value.ontology_id || value.hub_package_id, release_id: value.release_id });
  const isCrossPackage = Boolean(packageOf(left) && packageOf(right) && packageOf(left) !== packageOf(right));
  const setTrackedResult = (value) => { resultRef.current = value; if (value?.comparison_id) comparisonIds.current.add(value.comparison_id); setResult(value); };
  const deleteComparison = async (pid, id) => {
    if (!id) return;
    try { await api.deletePackageCompare(pid, id); comparisonIds.current.delete(id); } catch (e) { if ([404, 410].includes(e.status)) comparisonIds.current.delete(id); }
  };
  const deleteInput = async (pid, id) => {
    try { await api.deletePackageCompareInput(pid, id); inputIds.current.delete(id); } catch (e) { if ([403, 404, 410].includes(e.status)) inputIds.current.delete(id); }
  };
  const cleanupResources = async (pid) => {
    const comparisons = [...comparisonIds.current]; const inputs = [...inputIds.current];
    await Promise.allSettled(comparisons.map((id) => deleteComparison(pid, id)));
    await Promise.allSettled(inputs.map((id) => deleteInput(pid, id)));
  };
  const invalidateUpload = (side) => {
    const requestId = uploadIds.current[side]; uploadIds.current[side] = '';
    uploadAbort.current[side].abort(); uploadRequest.current[side].invalidate();
    if (requestId) api.cancelPackageCompareUpload(projectId, requestId).catch(() => {});
  };
  const resetLocalComparison = ({ keepConfirmation = false, keepRequestId = false } = {}) => {
    queryDebounce.current.cancel(); resultRef.current = null; if (!keepRequestId) compareRequestId.current = '';
    setResult(null); setFiles({ items: [], total: 0 }); setSelected(null); setDiff(null); setError(''); setPhase(''); setLoading(false); setFileLoading(false);
    if (!keepConfirmation) setCrossPackageConfirmed(false);
    setStatus('changed'); setCategory(''); setQuery(''); setQueryInput(''); setPage(1); setTab('overview');
  };
  const clearComparison = ({ removeRemote = true } = {}) => {
    actionGeneration.current.invalidate(); compareRequest.current.invalidate(); diffRequest.current.invalidate();
    const id = resultRef.current?.comparison_id; if (removeRemote && id) deleteComparison(projectId, id);
    resetLocalComparison();
  };
  const invalidateRequests = () => {
    actionGeneration.current.invalidate(); compareRequest.current.invalidate(); diffRequest.current.invalidate(); releaseRequest.current.invalidate();
    invalidateUpload('left'); invalidateUpload('right'); queryDebounce.current.cancel();
  };
  const resetFilterPage = (fn) => { fn(); setPage(1); };
  const selectSource = (side, value) => {
    sourceGeneration.current[side] += 1; invalidateUpload(side); setUploading((old) => ({ ...old, [side]: false })); clearComparison();
    (side === 'left' ? setLeft : setRight)(value);
  };
  const changeMode = (side, mode) => {
    modesRef.current[side] = mode; setModes((old) => ({ ...old, [side]: mode }));
    const choices = mode === 'release' ? normalizedReleases : uploads; selectSource(side, choices[0] || null);
  };
  const swapSources = () => {
    invalidateUpload('left'); invalidateUpload('right'); sourceGeneration.current.left += 1; sourceGeneration.current.right += 1;
    const nextModes = { left: modes.right, right: modes.left }; modesRef.current = nextModes;
    setUploading({ left: false, right: false }); setLeft(right); setRight(left); setModes(nextModes); clearComparison();
  };

  useEffect(() => {
    if (!open) return undefined;
    invalidateRequests(); resetLocalComparison(); setUploads([]); setUploading({ left: false, right: false });
    modesRef.current = { left: 'release', right: 'release' }; sourceGeneration.current = { left: 0, right: 0 };
    setModes({ left: 'release', right: 'release' }); setStatus('changed'); setCategory(''); setQuery(''); setQueryInput(''); setPage(1); setSidebarOpen(true);
    setAvailable(releases || []);
    const seed = (releases || []).map((item) => ({ ...item, kind: 'release' }));
    const initialId = (rows) => initialReleaseId || rows.find((item) => initialVersion && item.version === initialVersion)?.release_id || '';
    const initialSources = chooseInitialReleases(seed, initialId(seed));
    setLeft(initialSources.left);
    setRight(initialSources.right);
    const releaseGeneration = { ...sourceGeneration.current };
    releaseRequest.current.run(async () => {
      const rows = []; let offset = 0; let total = Infinity;
      while (offset < total) {
        const value = await api.projectHubReleases(projectId, offset, 100);
        const batch = value.releases || value.items || [];
        rows.push(...batch); total = Number(value.total ?? rows.length);
        if (!batch.length) break;
        offset += batch.length;
      }
      return rows;
    }, {
      success: (rows) => {
        setAvailable(rows);
        const normalized = rows.map((item) => ({ ...item, kind: 'release' }));
        const id = initialId(normalized);
        if (canApplyReleaseRefresh(modesRef.current.left, sourceGeneration.current.left, releaseGeneration.left)) {
          setLeft((currentLeft) => mergeReleaseRefresh({ releases: normalized, initialReleaseId: id, currentLeft }).left);
        }
        if (canApplyReleaseRefresh(modesRef.current.right, sourceGeneration.current.right, releaseGeneration.right)) {
          setRight((currentRight) => mergeReleaseRefresh({ releases: normalized, initialReleaseId: id, currentRight }).right);
        }
      },
      failure: (e) => setError(errorText(e)),
      settled: () => {},
    });
    return () => { invalidateRequests(); cleanupResources(projectId); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, projectId, initialReleaseId, initialVersion]);

  const upload = ({ file, side }) => {
    clearComparison(); uploadRequest.current[side].invalidate();
    const signal = uploadAbort.current[side].begin(); const requestId = newRequestId('upload');
    uploadIds.current[side] = requestId; setUploading((old) => ({ ...old, [side]: true }));
    uploadRequest.current[side].run(() => api.packageCompareUpload(projectId, file, { signal, requestId }), {
      success: (item) => {
        inputIds.current.add(item.input_id);
        const value = { ...item, filename: file.name, kind: 'upload' };
        setUploads((old) => [...old.filter((entry) => entry.input_id !== value.input_id), value]);
        (side === 'left' ? setLeft : setRight)(value); setCrossPackageConfirmed(false);
      },
      failure: (e) => { if (e.name !== 'AbortError') toast(errorText(e), 'alert'); },
      settled: () => { uploadAbort.current[side].finish(signal); if (uploadIds.current[side] === requestId) uploadIds.current[side] = ''; setUploading((old) => ({ ...old, [side]: false })); },
      stale: (item) => { if (item?.input_id) api.deletePackageCompareInput(projectId, item.input_id).catch(() => {}); },
    });
  };
  const cancelUpload = (side) => { invalidateUpload(side); setUploading((old) => ({ ...old, [side]: false })); };
  const compare = async () => {
    if (!left || !right || keyOf(left) === keyOf(right)) return;
    if (isCrossPackage && !crossPackageConfirmed) { setError('两侧属于不同知识包，请先确认跨包对比'); return; }
    const generation = actionGeneration.current.begin(); const previous = resultRef.current?.comparison_id;
    if (previous) await deleteComparison(projectId, previous);
    if (!actionGeneration.current.isCurrent(generation)) return;
    compareRequest.current.invalidate(); diffRequest.current.invalidate();
    resetLocalComparison({ keepConfirmation: true, keepRequestId: true }); setLoading(true); setStatus('changed');
    compareRequestId.current = compareRequestId.current || newRequestId('compare');
    await compareRequest.current.run(() => api.packageCompare(projectId, refOf(left), refOf(right), { allow_cross_package: isCrossPackage, request_id: compareRequestId.current }), {
      success: (value) => { setTrackedResult(value); setPhase(value.phase || value.status || 'queued'); },
      failure: (e) => setError(errorText(e)),
      settled: () => setLoading(false),
      stale: (value) => { if (value?.comparison_id) api.deletePackageCompare(projectId, value.comparison_id).catch(() => {}); },
    });
  };
  const openDiff = async (row) => {
    diffRequest.current.invalidate(); setSelected(row); setDiff(null); setFileLoading(true);
    await diffRequest.current.run(() => api.packageCompareFileDiff(projectId, resultRef.current.comparison_id, row.path), {
      success: setDiff, failure: (e) => toast(errorText(e), 'alert'), settled: () => setFileLoading(false),
    });
  };

  useEffect(() => {
    if (!result || result.status === 'ready' || result.status === 'failed') return undefined;
    return createSerialPoller({
      poll: () => api.packageCompareResult(projectId, result.comparison_id),
      onValue: (value) => { setPhase(value.phase || value.status || 'running'); if (value.status === 'ready' || value.status === 'failed') setTrackedResult(value); },
      onError: (e) => {
        setError(errorText(e));
        if ([401, 403, 410].includes(e.status)) {
          setTrackedResult({ ...resultRef.current, status: 'failed', error: errorText(e), error_code: e.status === 410 ? 'EXPIRED' : 'FORBIDDEN' });
          return true;
        }
        return false;
      },
      isDone: (value) => !value || value.status === 'ready' || value.status === 'failed',
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, result?.comparison_id, result?.status]);
  useEffect(() => {
    if (!result || result.status !== 'ready') return undefined;
    let active = true;
    api.packageCompareFiles(projectId, result.comparison_id, { status: status || undefined, category: category || undefined, q: query, offset: (page - 1) * PAGE, limit: PAGE }).then((value) => {
      if (!active) return;
      const validPage = validPageForTotal(page, value.total);
      if (validPage !== page) { setPage(validPage); return; }
      setFiles(value);
      const items = value.items || [];
      if (tab === 'files' && items.length && !items.some((row) => row.path === selected?.path)) openDiff(items[0]);
      else if (shouldClearSelectedFile(items)) { diffRequest.current.invalidate(); setSelected(null); setDiff(null); setFileLoading(false); }
    }).catch((e) => { if (active) setError(errorText(e)); });
    return () => { active = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, result?.comparison_id, result?.status, status, category, query, page, tab]);

  const cancelComparison = async () => {
    actionGeneration.current.invalidate(); compareRequest.current.invalidate(); diffRequest.current.invalidate();
    const id = resultRef.current?.comparison_id; resetLocalComparison(); if (id) await deleteComparison(projectId, id);
  };
  const closeDialog = () => {
    invalidateRequests(); resetLocalComparison(); setLeft(null); setRight(null); setUploads([]); setUploading({ left: false, right: false }); onClose();
  };
  const showFiles = (nextStatus = 'changed', nextCategory = '', path = '') => {
    queryDebounce.current.cancel();
    resetFilterPage(() => { setStatus(nextStatus); setCategory(nextCategory); setQueryInput(path); setQuery(path); });
    setTab('files');
  };
  const summary = result?.status === 'ready' ? result.summary : null; const rows = files.items || [];
  const categories = Object.entries(CATEGORY).map(([key, label]) => ({ key, label, ...(summary?.categories?.[key] || {}) })).filter((row) => summary?.categories?.[row.key]);
  const pageCount = Math.max(1, Math.ceil((files.total || 0) / PAGE));
  const changedTotal = summary ? summary.files.added + summary.files.deleted + summary.files.modified : 0;
  const projectLabel = projectName || left?.package_name || right?.package_name || '当前项目';
  const affected = (summary?.affected_categories || []).map((key) => CATEGORY[key] || key);
  const countLink = (value, onClick) => (value ? <Button size="xs" kind="ghost" className="pc-link" onClick={onClick}>{value}</Button> : <span className="muted">—</span>);

  const overview = summary && (
    <div className="pc-overview">
      <div className="pc-summary">
        <div className="row g8 pc-summary-title"><Icon name="split" size={16} /><b>版本差异概述</b><Chip tone="blue" sm>根据文件差异生成</Chip></div>
        <p>
          从 <strong>{displayName(left)}</strong> 到 <strong>{displayName(right)}</strong>，知识包文件数由 <strong>{summary.left_file_count}</strong> 个变为 <strong>{summary.right_file_count}</strong> 个：
          新增 <strong>{summary.files.added}</strong> 个、删除 <strong>{summary.files.deleted}</strong> 个、修改 <strong>{summary.files.modified}</strong> 个，{summary.files.unchanged} 个未变化。
          {affected.length ? `变化涉及${affected.join('、')}。` : '两侧内容完全一致。'}
        </p>
        {summary.evidence?.length > 0 ? (
          <div className="pc-evidence-wrap">
            <div className="xs muted">{representativeChangesLabel(summary.evidence.length, changedTotal)}</div>
            <div className="row g8 wrap">
              {summary.evidence.map((item) => (
                <Button key={`${item.status}:${item.path}`} size="xs" onClick={() => showFiles(item.status, '', item.path)}>{STATUS[item.status]} {item.display_name || item.path}</Button>
              ))}
            </div>
          </div>
        ) : null}
      </div>
      <div className="pc-stats">
        {Object.entries(STATUS).map(([key, label]) => (
          <button key={key} type="button" className={cx('stat pc-stat', key)} onClick={() => showFiles(key)}>
            <div className="lb">{label}</div>
            <div className="v">{summary.files[key]}</div>
            <div className="d">个文件 · 点击查看</div>
          </button>
        ))}
      </div>
      <div className="row g8 pc-section-title"><b>按文件类别统计</b><span className="xs muted">所有发布文件均纳入统计，包含包元数据及数据映射</span></div>
      <Table
        compact
        cols={['文件类别', '基准版本', '目标版本', ...Object.values(STATUS)]}
        rows={categories.map((row) => [
          row.label,
          (row.deleted || 0) + (row.modified || 0) + (row.unchanged || 0),
          (row.added || 0) + (row.modified || 0) + (row.unchanged || 0),
          ...Object.keys(STATUS).map((key) => countLink(row[key], () => showFiles(key, row.key))),
        ])}
      />
    </div>
  );

  const textDiff = diff && !NO_TEXT.includes(diff.left?.encoding) && !NO_TEXT.includes(diff.right?.encoding) && diff.left && diff.right;
  const filesView = summary && (
    <div className="pc-files">
      <div className="row g8 pc-filters">
        <Inp
          icon="search"
          placeholder="搜索文件路径或业务名称"
          w="280px"
          value={queryInput}
          onChange={(e) => { setQueryInput(e.target.value); queryDebounce.current.schedule(e.target.value); }}
          after={queryInput ? <button type="button" className="icon-btn" aria-label="清除" onClick={() => { queryDebounce.current.cancel(); setQueryInput(''); setQuery(''); setPage(1); }}><Icon name="x" size={12} /></button> : null}
        />
        <Select w="150px" value={status} onChange={(value) => resetFilterPage(() => setStatus(value))} options={[{ value: 'changed', label: '仅有差异' }, ...Object.entries(STATUS).map(([value, label]) => ({ value, label })), { value: '', label: '全部状态' }]} />
        <Select w="150px" value={category} onChange={(value) => resetFilterPage(() => setCategory(value))} options={[{ value: '', label: '所有类别' }, ...Object.entries(CATEGORY).map(([value, label]) => ({ value, label }))]} />
        <span className="xs muted">{files.total} 个文件</span>
      </div>
      <div className={cx('pc-workspace', !sidebarOpen && 'sidebar-closed')}>
        <aside className="pc-sidebar">
          {rows.length ? rows.map((row) => (
            <button type="button" key={row.path} className={cx('pc-file', selected?.path === row.path && 'on')} onClick={() => openDiff(row)}>
              <div className="row g8"><Chip tone={STATUS_TONE[row.status] || 'gray'} sm>{STATUS[row.status]}</Chip><span className="ell">{row.display_name || row.path.split('/').pop()}</span></div>
              <small className="ell">{row.path}</small>
            </button>
          )) : <div className="sm muted pc-none">没有符合条件的文件</div>}
          {files.total > PAGE ? (
            <div className="row g8 pc-pager">
              <Button size="xs" icon="chevl" aria-label="上一页" disabled={page <= 1} onClick={() => setPage((v) => v - 1)} />
              <span className="xs">{page}/{pageCount}</span>
              <Button size="xs" icon="chev" aria-label="下一页" disabled={page >= pageCount} onClick={() => setPage((v) => v + 1)} />
            </div>
          ) : null}
        </aside>
        <section className="pc-diff-pane">
          <header className="row g8">
            <Button size="xs" kind="ghost" icon={sidebarOpen ? 'chevl' : 'chev'} aria-label={sidebarOpen ? '收起文件列表' : '展开文件列表'} title={sidebarOpen ? '收起文件列表' : '展开文件列表'} onClick={() => setSidebarOpen((value) => !value)} />
            <b className="ell grow">{selected?.path || '选择文件查看差异'}</b>
            {selected ? <Chip tone={STATUS_TONE[selected.status] || 'gray'} sm>{STATUS[selected.status]}</Chip> : null}
            {selected ? <Chip sm>只读</Chip> : null}
          </header>
          {fileLoading ? <div className="pc-loading"><span className="spin" /></div> : diff ? (
            <>
              <div className="row pc-version-head">
                <span><i className="pc-legend deleted" />基准 · {displayName(left)}</span>
                <span><i className="pc-legend added" />目标 · {displayName(right)}</span>
              </div>
              {textDiff ? (
                <ReadonlyPackageDiff left={diff.left.text || ''} right={diff.right.text || ''} status={diff.status} leftEncoding={diff.left.encoding} rightEncoding={diff.right.encoding} leftBytes={diff.left.bytes} rightBytes={diff.right.bytes} />
              ) : <div className="pc-fallback"><TextPane value={diff.left} side="left" /><TextPane value={diff.right} side="right" /></div>}
            </>
          ) : <div className="pc-loading sm muted">从左侧选择一个文件</div>}
        </section>
      </div>
    </div>
  );

  const title = (
    <div className="row g8 pc-title">
      <Button size="sm" kind="ghost" icon="arrowl" onClick={closeDialog}>已发布版本</Button>
      <b>知识包版本对比</b>
      <Chip tone="blue" sm>{projectLabel}</Chip>
    </div>
  );
  const busy = loading || (result && result.status !== 'ready' && result.status !== 'failed');

  return (
    <Modal open={open} onClose={closeDialog} title={title} w="min(1600px, 96vw)" className="pc">
      {error ? (
        <Note tone="red" className="mb8">
          <div className="row"><span className="grow">{error}</span><button type="button" className="icon-btn" aria-label="关闭提示" onClick={() => setError('')}><Icon name="x" size={12} /></button></div>
        </Note>
      ) : null}
      <div className="pc-source-row">
        <SourceCard side="left" value={left} releases={normalizedReleases} uploads={uploads} mode={modes.left} uploading={uploading.left} onMode={(mode) => changeMode('left', mode)} onChange={(value) => selectSource('left', value)} onUpload={(file) => upload({ file, side: 'left' })} onCancelUpload={() => cancelUpload('left')} />
        <Button className="pc-swap" icon="arrow" aria-label="交换左右版本" title="交换左右版本" onClick={swapSources} disabled={!left && !right} />
        <SourceCard side="right" value={right} releases={normalizedReleases} uploads={uploads} mode={modes.right} uploading={uploading.right} onMode={(mode) => changeMode('right', mode)} onChange={(value) => selectSource('right', value)} onUpload={(file) => upload({ file, side: 'right' })} onCancelUpload={() => cancelUpload('right')} />
      </div>
      {left && right && keyOf(left) === keyOf(right) ? <Note tone="blue" className="mt12">左右选择的是同一发布记录，无需对比</Note> : null}
      {isCrossPackage && !crossPackageConfirmed ? (
        <Note tone="amber" className="mt12">
          <div className="row g8">
            <div className="grow">
              <b>两侧属于不同知识包</b>
              <div className="sm">{`基准：${left?.package_name || packageOf(left)}（${displayName(left)}）；目标：${right?.package_name || packageOf(right)}（${displayName(right)}）。跨包结果不代表版本继承关系。`}</div>
            </div>
            <Button size="xs" kind="soft" onClick={() => { setCrossPackageConfirmed(true); setError(''); }}>确认跨包对比</Button>
          </div>
        </Note>
      ) : null}
      <div className="row g8 pc-action">
        <span className="sm muted grow">差异方向：左 → 右。对比为只读，不修改项目内容。</span>
        <Button kind="primary" disabled={!left || !right || keyOf(left) === keyOf(right) || uploading.left || uploading.right || (isCrossPackage && !crossPackageConfirmed) || loading} onClick={compare}>
          {loading ? <span className="spin" /> : null}{summary ? '重新对比' : '开始对比'}
        </Button>
      </div>
      {busy ? (
        <div className="row g8 pc-loading"><span className="spin" /><span className="sm">{`正在准备对比：${phase || '排队中'}`}</span><Button size="xs" onClick={cancelComparison}>取消对比</Button></div>
      ) : null}
      {result?.status === 'failed' ? (
        <Note tone="red" className="mt12">
          <div className="row"><span className="grow">{result.error || '对比失败'}</span><Button size="xs" kind="soft" onClick={compare}>重试</Button></div>
        </Note>
      ) : null}
      {!summary && !loading && !result ? (
        <div className="pc-blank"><Icon name="split" size={42} /><h3>选择两个知识包，查看版本变化</h3><p>支持已发布版本与版本、版本与本地 ZIP、两个本地 ZIP 对比。</p></div>
      ) : null}
      {summary ? (
        <div className="mt12">
          <Tabs items={[['overview', '差异总览'], ['files', `文件差异（${changedTotal}）`]]} value={tab} onChange={setTab} />
          <div className="mt12">{tab === 'overview' ? overview : filesView}</div>
        </div>
      ) : null}
    </Modal>
  );
}
