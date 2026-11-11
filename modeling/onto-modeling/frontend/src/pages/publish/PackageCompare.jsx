// 知识包版本对比（契约 PC-01～PC-08，ADR-0095）：两个发布版本、或一个版本和上传的 ZIP，逐文件看差异。只读。
// 挂载：<PackageCompare open projectId initialVersion onClose />，从发布页「发布记录」打开；发布记录自己取（PB-04）。
// 关闭时删掉这次建的对比和上传（服务端 24 小时后也会清）；迟到的响应一律丢弃（api/latestRequest）。
import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { collectAll } from '../../api/client.js';
import { errorText } from '../../api/errors.js';
import { createAbortableRequest, createLatestRequest, createSerialPoller } from '../../lib/latestRequest.js';
import {
  Button, Chip, Drawer, Icon, Inp, Note, Select, Tabs, cx,
} from '../../ui/index.jsx';
import PackageDiff from './PackageDiff.jsx';
import {
  STATUS, UPLOAD, appendPage, asRelease, categoryOptions, debounceValue, diffMode, fillSources, isCrossPackage, keyOf,
  paneNote, refOf, requestId, sameSource, shouldClearSelectedFile, sourceOptions, statusTabs,
} from './packageCompareModel.js';
import '../../styles/package-compare.css';

const DONE = ['ready', 'failed'];
const EMPTY = { items: [], next_cursor: null, total: 0 };

function SourcePick({ value, options, uploading, onPick, onCancel }) {
  if (uploading) {
    return (
      <div className="inp pc-pick">
        <Icon name="upload" size={14} />
        <span className="grow muted">上传中…</span>
        <button type="button" className="icon-btn" title="取消上传" onClick={onCancel}><Icon name="x" size={12} /></button>
      </div>
    );
  }
  return <div className="pc-pick"><Select value={keyOf(value)} options={options} onChange={onPick} icon={value?.kind === 'upload' ? 'archive' : 'tag'} w="100%" /></div>;
}

function Pane({ side }) {
  const note = paneNote(side);
  return <div className="pc-pane">{note ? <div className="sm muted">{note}</div> : <pre className="vr-pre">{side.text}</pre>}</div>;
}

export default function PackageCompare({ open, projectId, initialVersion = '', onClose }) {
  const { api } = useApp();
  const pid = projectId;
  const [releases, setReleases] = useState([]);
  const [uploads, setUploads] = useState([]);
  const [left, setLeft] = useState(null);
  const [right, setRight] = useState(null);
  const [uploading, setUploading] = useState({ left: false, right: false });
  const [result, setResult] = useState(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('changed');
  const [category, setCategory] = useState('');
  const [queryInput, setQueryInput] = useState('');
  const [query, setQuery] = useState('');
  const [files, setFiles] = useState(EMPTY);
  const [selected, setSelected] = useState(null);
  const [diff, setDiff] = useState(null);
  const [diffLoading, setDiffLoading] = useState(false);

  const startRequest = useRef(createLatestRequest());
  const diffRequest = useRef(createLatestRequest());
  const releaseRequest = useRef(createLatestRequest());
  const uploadRequest = useRef({ left: createLatestRequest(), right: createLatestRequest() });
  const uploadAbort = useRef({ left: createAbortableRequest(), right: createAbortableRequest() });
  const uploadIds = useRef({ left: '', right: '' });
  const fileInput = useRef(null);
  const fileSide = useRef('left');
  const resultRef = useRef(null);
  const created = useRef({ comparisons: new Set(), inputs: new Set() });
  const debounced = useRef(debounceValue((value) => setQuery(value), 200));
  const sides = useRef({ left: null, right: null });
  sides.current = { left, right };

  const track = (value) => {
    resultRef.current = value;
    if (value?.comparison_id) created.current.comparisons.add(value.comparison_id);
    setResult(value);
  };
  const removeComparison = async (id) => {
    if (!id) return;
    try {
      await api.deleteComparison(pid, id);
      created.current.comparisons.delete(id);
    } catch (e) {
      if ([404, 410].includes(e.status)) created.current.comparisons.delete(id);
    }
  };
  const cancelUploadOf = (side) => {
    const id = uploadIds.current[side];
    uploadIds.current[side] = '';
    uploadAbort.current[side].abort();
    uploadRequest.current[side].invalidate();
    if (id) api.cancelCompareUpload(pid, id).catch(() => {});
    setUploading((u) => ({ ...u, [side]: false }));
  };
  const clearResult = () => {
    startRequest.current.invalidate();
    diffRequest.current.invalidate();
    debounced.current.cancel();
    const id = resultRef.current?.comparison_id;
    if (id) removeComparison(id);
    resultRef.current = null;
    setResult(null);
    setStarting(false);
    setError('');
    setFiles(EMPTY);
    setSelected(null);
    setDiff(null);
    setStatus('changed');
    setCategory('');
    setQueryInput('');
    setQuery('');
  };

  useEffect(() => {
    if (!open) return undefined;
    setReleases([]);
    setUploads([]);
    setLeft(null);
    setRight(null);
    setUploading({ left: false, right: false });
    clearResult();
    releaseRequest.current.run(() => collectAll((q) => api.releases(pid, { ...q, limit: 200 })), {
      success: (rows) => {
        const list = rows.map(asRelease);
        setReleases(list);
        // 发布记录晚到：加载期间已经选过的那侧不覆盖
        const next = fillSources({ releases: list, initialVersion, ...sides.current });
        setLeft(next.left);
        setRight(next.right);
      },
      failure: (e) => setError(errorText(e)),
      settled: () => {},
    });
    const session = created.current;
    return () => {
      releaseRequest.current.invalidate();
      ['left', 'right'].forEach(cancelUploadOf);
      startRequest.current.invalidate();
      diffRequest.current.invalidate();
      debounced.current.cancel();
      const comparisons = [...session.comparisons];
      const inputs = [...session.inputs];
      session.comparisons.clear();
      session.inputs.clear();
      // 先删对比再删输入：还被对比用着的输入删不掉（409）
      Promise.allSettled(comparisons.map((id) => api.deleteComparison(pid, id)))
        .then(() => Promise.allSettled(inputs.map((id) => api.deleteCompareInput(pid, id))));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, pid, initialVersion]);

  // 排队、冻结、解析中：串行轮询到出结果
  useEffect(() => {
    if (!result?.comparison_id || DONE.includes(result.status)) return undefined;
    return createSerialPoller({
      delay: result.poll_after_ms || 700,
      poll: () => api.comparison(pid, result.comparison_id),
      onValue: (value) => { if (DONE.includes(value.status)) track(value); },
      onError: (e) => {
        if (![401, 403, 410].includes(e.status)) return false;
        track({ ...resultRef.current, status: 'failed', error: errorText(e) });
        return true;
      },
      isDone: (value) => DONE.includes(value?.status),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, result?.comparison_id, result?.status]);

  const openDiff = (row) => {
    setSelected(row);
    setDiff(null);
    setDiffLoading(true);
    diffRequest.current.run(() => api.comparisonFileDiff(pid, resultRef.current.comparison_id, row.path), {
      success: setDiff,
      failure: (e) => toast(errorText(e), 'alert'),
      settled: () => setDiffLoading(false),
    });
  };

  const ready = result?.status === 'ready' ? result : null;
  const filterQuery = (cursor) => ({ status, category: category || undefined, q: query || undefined, cursor, limit: 100 });

  useEffect(() => {
    if (!ready) return undefined;
    let alive = true;
    api.comparisonFiles(pid, ready.comparison_id, filterQuery()).then((page) => {
      if (!alive) return;
      setFiles(appendPage(EMPTY, page));
      const items = page.items || [];
      if (shouldClearSelectedFile(items)) {
        diffRequest.current.invalidate();
        setSelected(null);
        setDiff(null);
        setDiffLoading(false);
      } else if (!items.some((row) => row.path === selected?.path)) openDiff(items[0]);
    }).catch((e) => alive && setError(errorText(e)));
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, ready?.comparison_id, status, category, query]);

  const loadMore = () => api.comparisonFiles(pid, ready.comparison_id, filterQuery(files.next_cursor))
    .then((page) => setFiles((prev) => appendPage(prev, page)))
    .catch((e) => toast(errorText(e), 'alert'));

  const pick = (side, value) => {
    if (value === UPLOAD) {
      fileSide.current = side;
      fileInput.current?.click();
      return;
    }
    const source = [...releases, ...uploads].find((s) => keyOf(s) === value) || null;
    cancelUploadOf(side);
    clearResult();
    (side === 'left' ? setLeft : setRight)(source);
  };

  const upload = (side, file) => {
    cancelUploadOf(side);
    clearResult();
    const signal = uploadAbort.current[side].begin();
    const id = requestId();
    uploadIds.current[side] = id;
    setUploading((u) => ({ ...u, [side]: true }));
    uploadRequest.current[side].run(() => api.uploadCompareInput(pid, file, { signal, requestId: id }), {
      success: (item) => {
        created.current.inputs.add(item.input_id);
        const value = { ...item, kind: 'upload', filename: file.name };
        setUploads((list) => [...list.filter((x) => x.input_id !== value.input_id), value]);
        (side === 'left' ? setLeft : setRight)(value);
      },
      failure: (e) => { if (e.name !== 'AbortError') toast(errorText(e), 'alert'); },
      settled: () => {
        uploadAbort.current[side].finish(signal);
        if (uploadIds.current[side] === id) uploadIds.current[side] = '';
        setUploading((u) => ({ ...u, [side]: false }));
      },
      // 已经换了来源：迟到的上传结果不要了，顺手删掉
      stale: (item) => { if (item?.input_id) api.deleteCompareInput(pid, item.input_id).catch(() => {}); },
    });
  };

  const swap = () => {
    ['left', 'right'].forEach(cancelUploadOf);
    clearResult();
    setLeft(right);
    setRight(left);
  };

  const compare = async ({ cross = false } = {}) => {
    if (!left || !right || sameSource(left, right)) return;
    const previous = resultRef.current?.comparison_id;
    clearResult();
    if (previous) await removeComparison(previous);
    setStarting(true);
    await startRequest.current.run(() => api.startComparison(pid, {
      left: refOf(left), right: refOf(right), allow_cross_package: cross, request_id: requestId(),
    }), {
      success: track,
      failure: (e) => setError(errorText(e)),
      settled: () => setStarting(false),
      stale: (value) => { if (value?.comparison_id) api.deleteComparison(pid, value.comparison_id).catch(() => {}); },
    });
  };

  const options = sourceOptions(releases, uploads);
  const busy = starting || (result && !DONE.includes(result.status));
  const same = sameSource(left, right);
  const canCompare = left && right && !same && !uploading.left && !uploading.right && !busy;
  const failed = result?.status === 'failed' ? result : null;

  return (
    <Drawer open={open} onClose={onClose} icon="split" title="版本对比" w="min(1240px, 96vw)" className="pc">
      <input
        ref={fileInput}
        type="file"
        accept=".zip"
        hidden
        onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) upload(fileSide.current, f); }}
      />
      <div className="row g8">
        <SourcePick value={left} options={options} uploading={uploading.left} onPick={(v) => pick('left', v)} onCancel={() => cancelUploadOf('left')} />
        <Button kind="ghost" icon="arrow" title="交换" disabled={!left && !right} onClick={swap} />
        <SourcePick value={right} options={options} uploading={uploading.right} onPick={(v) => pick('right', v)} onCancel={() => cancelUploadOf('right')} />
        <Button kind="primary" disabled={!canCompare} title={same ? '两侧相同' : undefined} onClick={() => compare()}>
          {busy ? '对比中…' : '对比'}
        </Button>
        {busy && result ? <Button kind="ghost" onClick={clearResult}>取消</Button> : null}
      </div>
      {error ? <Note tone="red" className="mt12">{error}</Note> : null}
      {failed && isCrossPackage(failed) ? (
        <Note tone="amber" className="mt12">
          <div className="row">
            <span className="grow">两侧是不同的知识包</span>
            <Button size="xs" kind="soft" onClick={() => compare({ cross: true })}>仍要对比</Button>
          </div>
        </Note>
      ) : null}
      {failed && !isCrossPackage(failed) ? (
        <Note tone="red" className="mt12">
          <div className="row">
            <span className="grow">{failed.error || '对比失败'}</span>
            <Button size="xs" kind="soft" onClick={() => compare()}>重试</Button>
          </div>
        </Note>
      ) : null}
      {ready ? (
        <>
          <div className="mt16">
            <Tabs items={statusTabs(ready.summary)} value={status} onChange={setStatus} />
          </div>
          <div className="pc-body">
            <aside className="pc-side">
              <div className="col g8 pc-filters">
                <Inp
                  icon="search"
                  placeholder="搜索文件"
                  value={queryInput}
                  onChange={(e) => { setQueryInput(e.target.value); debounced.current.schedule(e.target.value); }}
                />
                <Select value={category} options={categoryOptions(ready.summary)} onChange={setCategory} icon="filter" w="100%" />
              </div>
              <div className="pc-list">
                {files.items.length ? files.items.map((row) => {
                  const [label, tone] = STATUS[row.status];
                  const name = row.display_name || row.path.split('/').pop();
                  return (
                    <button
                      type="button"
                      key={row.path}
                      className={cx('pc-file', selected?.path === row.path && 'on')}
                      title={row.path}
                      onClick={() => openDiff(row)}
                    >
                      <Chip tone={tone} sm>{label}</Chip>
                      <span className="grow ell">{name}</span>
                    </button>
                  );
                }) : <div className="sm muted pc-none">没有文件</div>}
                {files.next_cursor ? <Button size="xs" kind="ghost" className="pc-more" onClick={loadMore}>更多</Button> : null}
              </div>
            </aside>
            <section className="pc-main">
              {selected ? (
                <div className="row g8 pc-head">
                  <span className="mono sm ell grow" title={selected.path}>{selected.path}</span>
                  <Chip tone={STATUS[selected.status][1]} sm>{STATUS[selected.status][0]}</Chip>
                </div>
              ) : null}
              {diffLoading ? <div className="sm muted pc-none">加载中…</div> : null}
              {!diffLoading && diff && diffMode(diff) === 'merge' ? <PackageDiff diff={diff} /> : null}
              {!diffLoading && diff && diffMode(diff) === 'panes' ? (
                <div className="pc-panes"><Pane side={diff.left} /><Pane side={diff.right} /></div>
              ) : null}
            </section>
          </div>
        </>
      ) : null}
    </Drawer>
  );
}
