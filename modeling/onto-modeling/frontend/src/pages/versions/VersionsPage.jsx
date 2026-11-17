// 版本历史（设计稿 versions，元素照 develop 的「历史」页签，ADR-0094）：
// 左栏是历史时间线（V-09，迭代与 Hub 发布版本合并；按来源筛选时改列 V-01），每行只有版本号、来源、时间与一句说明，
//   查看文件（V-03、F-06）、标记（V-07、V-08）、回退（V-05 预览 → V-06）收在行尾 ⋯；发布版本的下载与评测也在 ⋯。
// 右栏是选中版本的详情：这一版是谁、什么时候、做了什么，这一版的操作，「改了什么」（V-02）；点一个条目在下面对比
//   （默认看这一版改了什么：上一版 → 这一版；也可看这一版到现在又变了什么，右侧是当前文件时可编辑，保存走 F-03）。
// 项目流 workspace.changed（EP-05）刷新。从别处跳来：?v=&run=&slice=（待处理、自主构建报告、运行矩阵的「查看改动」）选中那个版本；
//   ?path=&from=&to=WORKTREE（原文编辑器的「刷新后对比」）直接打开这个文件的对比。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { describeConflict, errorText } from '../../api/errors.js';
import { ShellP } from '../../shell/Shell.jsx';
import { ScopeLockBar, useProjectEvent } from '../plugin/parts.jsx';
import {
  Button, Card, Chip, Icon, Menu, Note, Select, St, cx,
} from '../../ui/index.jsx';
import { createLatestRequest } from '../../lib/latestRequest.js';
import { EMPTY_HISTORY, appendHistoryPage, historyEntryKey, versionTitle } from '../../lib/versionHistory.js';
import { hasWorkspaceDirty, setWorkspaceDirty } from '../../lib/workspaceDirty.js';
import { openEvaluation, prefetchEvaluationEntry } from '../evaluation/evaluationEntry.js';
import { releaseSource } from '../publish/publishCheckModel.js';
import CompareEditor from './CompareEditor.jsx';
import { usePopMenu } from './popMenu.js';
import { FilesDrawer, RevertConfirm, TagModal } from './dialogs.jsx';
import {
  SOURCES, SOURCE_FILTERS, byNewest, changeText, compareFromOf, compareOptions, defaultCompare, diffLines, editBlocks, entryPath,
  fmtStamp, knownChanges, otherChanges, pathLabel, pathLocked, pickLanding, rangeChoices, rangeValue, reviewView, revertBlockedText,
  selfRange, srcClass, srcLabel, syncMeta, targetLabel, versionMenu, versionSub,
} from './versionsModel.js';
import '../../styles/more.css';
import '../../styles/versions.css';

const LOCK_TEXT = {
  running: ['助手正在改这个文件，这一轮结束后才能在这里编辑'],
  pending: ['这个文件和助手这一轮的修改有冲突，选好保留哪边后才能在这里编辑'],
};

// 行尾 ⋯（fixed 定位，时间线有滚动）
function RowMenu({ items, onPick }) {
  const menu = usePopMenu({ width: 190, height: 150 });
  return (
    <span ref={menu.ref} onClick={(e) => e.stopPropagation()}>
      <Button size="xs" kind="ghost" icon="more" title="更多操作" onClick={menu.toggle} />
      {menu.at ? <Menu className="vr-menu" style={menu.at} items={items.map((x) => (x === '-' ? x : { ...x, onClick: () => onPick(x.key) }))} onPick={menu.close} /> : null}
    </span>
  );
}

// 时间线的一条：发布版本（下载、评测在 ⋯）；迭代（版本号、来源、时间、一句说明，标记与回退在 ⋯）
function HistoryRow({ v, on, onSelect, onMenu, onDelTag, reverting, downloading, onDownload, onEvaluate }) {
  if (v.kind === 'release') {
    return (
      <div className="mr-vr vr-release">
        <span className="dot" />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row g6">
            <b className="v">{versionTitle(v.kind, v.version)}</b>
            <span className="xs muted num">{v.time ? fmtStamp(v.time * 1000) : ''}</span>
            <span className="sp" />
            <RowMenu
              items={[
                { key: 'download', label: downloading === v.release_id ? '下载中…' : '下载知识包', icon: 'download', disabled: downloading === v.release_id },
                { key: 'evaluate', label: '评测', icon: 'gauge' },
              ]}
              onPick={(key) => (key === 'download' ? onDownload(v.release_id) : onEvaluate(v))}
            />
          </div>
          <div className="sm muted vr-sum">发布知识包{releaseSource(v) ? ` · ${releaseSource(v)}` : ''}</div>
        </div>
      </div>
    );
  }
  const tags = v.tags || [];
  return (
    <div className={cx('mr-vr', v.current && 'head', on && 'on', tags.length && 'vr-tagged')} onClick={() => onSelect(v)}>
      <span className="dot" />
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="row g6">
          <b className="v">{versionTitle('iteration', v.version)}</b>
          {v.source ? <span className={cx('mr-src', srcClass(v.source))}>{srcLabel(v)}</span> : null}
          {v.current ? <Chip tone="blue" sm>当前</Chip> : null}
          <span className="sp" />
          <span className="xs muted num">{fmtStamp(v.at || (v.time ? v.time * 1000 : ''))}</span>
          <RowMenu items={versionMenu(v).map((x) => (x !== '-' && x.key === 'revert' && reverting === v.commit ? { ...x, label: '回退中…', disabled: true } : x))}
            onPick={(key) => onMenu(key, v)} />
        </div>
        <div className="sm vr-sum" title={v.summary || v.message}>{v.summary || v.message}</div>
        {tags.length || v.release ? (
          <div className="row mt4 g4 wrap">
            {v.release ? <span className="vr-rel"><Icon name="upload" size={12} />已发布 {v.release}</span> : null}
            {tags.map((t) => (
              <span key={t.name} className="mr-tagc">
                <Icon name="tag" size={12} />{t.label}
                <button type="button" title="删除标记" onClick={(e) => { e.stopPropagation(); onDelTag(t); }}><Icon name="x" size={12} /></button>
              </span>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// 选中版本的详情：是谁、什么时候、做了什么；这一版的操作；改了什么（点一行在下面对比这个条目）
function VersionDetail({ v, detail, current, note, reverting, onPick, onFiles, onTag, onRevert }) {
  if (!v) return <div className="card pad sm muted">在左边选一个版本</div>;
  const changes = detail?.commit === v.commit ? detail.changes || [] : null;
  const known = changes ? knownChanges(changes) : [];
  const others = changes ? otherChanges(changes) : 0;
  const review = known.filter((c) => c.review === 'needs_review').length;
  return (
    <div className="card vr-detail">
      <div className="vr-dh">
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row g6">
            <h3>{versionTitle('iteration', v.n)}</h3>
            {v.source ? <span className={cx('mr-src', srcClass(v.source))}>{srcLabel(v)}</span> : null}
            {v.current ? <Chip tone="blue" sm>当前</Chip> : null}
            {v.release ? <span className="vr-rel"><Icon name="upload" size={12} />已发布 {v.release}</span> : null}
            {review ? <Chip tone="indigo" sm icon="eye">{review} 项需复核</Chip> : null}
          </div>
          <div className="sm mt4">{v.summary || v.message}</div>
          <div className="xs muted mt4">{[versionSub(v), v.author].filter(Boolean).join(' · ')}</div>
        </div>
        <div className="row g6" style={{ flex: 'none' }}>
          <Button size="sm" kind="ghost" icon="tag" onClick={() => onTag(v)}>标记</Button>
          <Button size="sm" kind="ghost" icon="eye" onClick={() => onFiles(v)}>全部文件</Button>
          {!v.current && v.can_revert !== false ? (
            <Button size="sm" icon="undo" disabled={!!reverting} onClick={() => onRevert(v)}>{reverting === v.commit ? '回退中…' : '回退到这一版'}</Button>
          ) : null}
        </div>
      </div>
      {note ? <Note tone="gray" icon="info" className="mt12">{note}</Note> : null}
      <div className="vr-changes">
        <div className="sec-t" style={{ margin: 0 }}>改了什么</div>
        {!changes ? <div className="sm muted mt8">正在读取…</div> : null}
        {changes && known.length ? (
          <div className="tbl-wrap mt8">
            <table className="tbl compact">
              <tbody>
                {known.map((c, i) => {
                  const p = entryPath(c.target);
                  const r = reviewView(c.review);
                  return (
                    // eslint-disable-next-line react/no-array-index-key
                    <tr key={i} className={cx(p && 'vr-click', p && p === current && 'vr-cur')} onClick={() => p && onPick(p)} title={p ? '在下面对比这个条目' : undefined}>
                      <td>{targetLabel(c.target)}</td>
                      <td>{changeText(c)}</td>
                      <td style={{ width: 120 }}>{r?.st ? <St k={r.st} sm /> : r?.chip ? <Chip tone={r.chip[1]} sm>{r.chip[0]}</Chip> : null}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
        {changes && !known.length ? <div className="sm muted mt8">这一版没有改对象、操作、规则这些条目</div> : null}
        {others ? <div className="xs muted mt8">{known.length ? '另有' : '只改了'} {others} 个其他文件（报告、流程记录等），在「全部文件」里看</div> : null}
      </div>
    </div>
  );
}

function Lines({ lines, empty }) {
  if (!lines.length) return <div className="ln"><i /><span className="faint">{empty}</span></div>;
  return lines.map((l) => <div key={l.n} className={cx('ln', l.k)}><i>{l.n}</i>{l.t || ' '}</div>);
}

export default function VersionsPage({ route }) {
  const { api, services } = useApp();
  const { pid, scope, setMeta } = useProject();
  const q = route?.query || {};
  const [items, setItems] = useState(null);
  const [total, setTotal] = useState(0);
  const [history, setHistory] = useState(EMPTY_HISTORY);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyRevision, setHistoryRevision] = useState(0);
  const [downloading, setDownloading] = useState('');
  const [reverting, setReverting] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const diffReq = useMemo(() => createLatestRequest(), []);
  const [filter, setFilter] = useState('');
  const [filtered, setFiltered] = useState(null);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [cmp, setCmp] = useState({ path: null, from: null, to: 'WORKTREE' });
  const [diff, setDiff] = useState(null);
  const [diffErr, setDiffErr] = useState('');
  const [text, setText] = useState('');
  const [gen, setGen] = useState(0);
  const [note, setNote] = useState('');
  const [conflict, setConflict] = useState(null);
  const [outside, setOutside] = useState(null);
  const [saving, setSaving] = useState(false);
  const [tagFor, setTagFor] = useState(null);
  const [filesFor, setFilesFor] = useState(null);
  const landed = useRef('');
  const stepBack = useRef(false);
  const cmpRef = useRef(null);

  const loadItems = useCallback(async () => {
    try {
      const r = await api.versions(pid, { limit: 200 });
      setItems(byNewest(r.items));
      setTotal(r.total ?? r.items.length);
      setMeta((m) => syncMeta(m, r));
      return r.items;
    } catch (e) {
      toast(errorText(e), 'alert');
      return null;
    }
  }, [api, pid, setMeta]);
  useEffect(() => { setItems(null); loadItems(); }, [loadItems]);

  // 历史时间线：版本列表重读（有了新版本）或点「重试」时从第一页重新读取
  useEffect(() => setHistory(EMPTY_HISTORY), [pid]);
  // 失败不阻断：点击「评测」时会重新读取并给出原因
  useEffect(() => { prefetchEvaluationEntry(api.evaluationEntry).catch(() => {}); }, [api]);
  useEffect(() => {
    let active = true;
    setHistoryLoading(true);
    api.history(pid)
      .then((page) => { if (active) setHistory(appendHistoryPage(EMPTY_HISTORY, page)); })
      .catch((error) => { if (active) setHistory({ ...EMPTY_HISTORY, error: errorText(error) }); })
      .finally(() => { if (active) setHistoryLoading(false); });
    return () => { active = false; };
  }, [api, pid, items, historyRevision]);
  const loadMoreHistory = async () => {
    const cursor = history.nextCursor;
    setHistoryLoading(true);
    try {
      const page = await api.history(pid, { cursor });
      // 读取期间列表已重新加载（游标变了）时丢弃这一页
      setHistory((prev) => (prev.nextCursor === cursor ? appendHistoryPage(prev, page) : prev));
    } catch (error) {
      toast(errorText(error), 'alert');
    } finally {
      setHistoryLoading(false);
    }
  };
  const downloadRelease = async (releaseId) => {
    setDownloading(releaseId);
    try {
      await services.download(api.hubReleaseDownloadPath(pid, releaseId), 'package.zip');
    } catch (error) {
      toast(errorText(error), 'alert');
    } finally {
      setDownloading('');
    }
  };
  const evaluateRelease = (release) => openEvaluation(release, {
    loadEntry: api.evaluationEntry,
    tokens: () => ({ access_token: services.tokens.access, refresh_token: services.tokens.refresh }),
  }).catch((error) => toast(errorText(error), 'alert'));
  const deleteTag = (tag) => api.deleteVersionTag(pid, tag.name).then(loadItems).catch((error) => toast(errorText(error), 'alert'));
  useEffect(() => {
    if (!filter) { setFiltered(null); return undefined; }
    let alive = true;
    api.versions(pid, { source: filter, limit: 200 }).then((r) => alive && setFiltered(byNewest(r.items))).catch((e) => toast(errorText(e), 'alert'));
    return () => { alive = false; };
  }, [api, pid, filter, items]);

  const byCommit = useMemo(() => new Map((items || []).map((v) => [v.commit, v])), [items]);
  const head = (items || []).find((v) => v.current) || null;
  const sel = byCommit.get(selected) || null;
  const timeline = filtered || items || [];

  // 落地：跳转参数变了才重新选（之后用户在页面里怎么点都不再被覆盖）
  const landKey = [q.v, q.run, q.slice, q.path, q.from, q.to].join('|');
  useEffect(() => {
    if (!items || landed.current === landKey) return;
    landed.current = landKey;
    const pick = pickLanding(items, q);
    const v = byCommit.get(pick.commit);
    setNote(pick.note);
    setSelected(pick.commit);
    if (q.path) {
      const from = pick.note ? compareFromOf(head, items) : v;
      stepBack.current = !pick.note;
      setCmp({ path: q.path, from: from?.commit || null, to: q.to && q.to !== 'WORKTREE' ? q.to : 'WORKTREE' });
    } else {
      stepBack.current = false;
      setCmp({ path: null, ...selfRange(v || head, items) });
    }
  }, [items, landKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // 选中版本的详情
  useEffect(() => {
    if (!selected) return undefined;
    let alive = true;
    api.version(pid, selected).then((d) => alive && setDetail(d)).catch((e) => alive && toast(errorText(e), 'alert'));
    return () => { alive = false; };
  }, [api, pid, selected, items]);

  // 没指定对比对象时取这个版本改过的第一个对象
  const options = useMemo(() => compareOptions(detail?.changes, cmp.path), [detail, cmp.path]);
  useEffect(() => {
    if (!cmp.path && detail && detail.commit === selected) {
      const p = defaultCompare(options, null);
      if (p) setCmp((c) => ({ ...c, path: p }));
    }
  }, [detail, selected, options, cmp.path]);

  const loadDiff = useCallback(async (c = cmp) => {
    if (!c.path || !c.from) { diffReq.invalidate(); setDiff(null); return; }
    setDiffErr('');
    // 连着换对象或版本时，先发的请求晚回来也不覆盖后选的
    await diffReq.run(() => api.versionDiff(pid, { from: c.from, to: c.to || 'WORKTREE', path: c.path }), {
      success: (d) => {
        // 从原文编辑器跳来：from 往往就是这个文件最近一次改动的版本，和当前一样时改比它的上一个版本，才看得出改了什么
        if (stepBack.current && d.left === d.right) {
          stepBack.current = false;
          const fromV = byCommit.get(c.from);
          const prev = fromV && byNewest(items).find((x) => x.n < fromV.n);
          if (prev) {
            setNote(`${fromV.version_label} 与当前相同，已改为对比 ${prev.version_label}`);
            setCmp({ ...c, from: prev.commit });
            return;
          }
        }
        stepBack.current = false;
        setDiff(d);
        setText(d.right);
        setConflict(null);
        setOutside(null);
        setGen((g) => g + 1);
      },
      failure: (e) => {
        setDiff(null);
        setDiffErr(e.code === 'file_not_found' ? '这个版本和当前都没有这个文件' : errorText(e));
      },
      settled: () => {},
    });
  }, [api, pid, cmp, byCommit, items, diffReq]);
  useEffect(() => { loadDiff(cmp); }, [cmp.path, cmp.from, cmp.to]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectVersion = (v) => {
    setSelected(v.commit);
    setNote('');
    stepBack.current = false;
    setCmp({ path: null, ...selfRange(v, items) });
  };
  const onMenu = (key, v) => {
    if (key === 'files') setFilesFor(v);
    if (key === 'tag') setTagFor(v.commit);
    if (key === 'revert') revertTo(v);
  };

  // 回退（develop 的流程与提示）：页面上有没保存的修改先挡；预览说不能回退就提示原因；能回退再确认
  const revertTo = async (version) => {
    if (hasWorkspaceDirty()) {
      toast('当前页面有未保存修改，请先保存或放弃后再回退', 'alert');
      return;
    }
    setReverting(version.commit);
    try {
      const preview = await api.revertPreview(pid, version.commit);
      if (!preview?.ready) {
        toast(revertBlockedText(preview), 'alert');
        setReverting(null);
        return;
      }
      setConfirm({ version, preview });
    } catch (error) {
      toast(errorText(error), 'alert');
      setReverting(null);
    }
  };
  const cancelRevert = () => { setConfirm(null); setReverting(null); };
  const confirmRevert = async () => {
    const { version, preview } = confirm;
    setConfirm(null);
    try {
      const result = await api.revertVersion(pid, version.commit, preview.expected_head);
      toast('已回退并生成新版本', 'undo');
      if (result.warning) toast(result.warning, 'alert');
      const list = await loadItems();
      const nv = (list || []).find((x) => x.commit === result.commit);
      if (nv) selectVersion(nv);
    } catch (error) {
      toast(errorText(error), 'alert');
    } finally {
      setReverting(null);
    }
  };

  // 别处改了文件（EP-05）：刷新列表；改到正在对比的文件时，没改过就直接刷新，改过就提示
  useProjectEvent('workspace.changed', (d) => {
    loadItems();
    const paths = [...(d.paths || []), ...(d.entries || []).map(entryPath)];
    if (!diff || !paths.includes(diff.path) || saving) return;
    if (text === diff.right) loadDiff(cmp);
    else setOutside({ version_label: d.version_label, by: d.source });
  });

  const fromV = byCommit.get(diff?.from?.commit || cmp.from) || null;
  const toWorktree = (diff?.to?.commit || null) === null;
  const locked = toWorktree && pathLocked(scope, diff?.path);
  const readOnly = !diff || !toWorktree || locked || !diff.right_revision;
  const dirty = !!diff && text !== diff.right;
  const blocks = dirty ? editBlocks(diff.right, text) : 0;
  // 对比里没保存的修改：回退前要先保存或放弃
  useEffect(() => {
    setWorkspaceDirty('diff-editor', dirty);
    return () => setWorkspaceDirty('diff-editor', false);
  }, [dirty]);
  const view = useMemo(() => (diff ? diffLines(diff.left, text) : null), [diff, text]);

  const save = async () => {
    setSaving(true);
    try {
      const r = await api.saveFileContent(pid, { path: diff.path, content: text, base_revision: diff.right_revision, summary: `版本对比里编辑 ${pathLabel(diff.path)}` });
      toast(`已保存为版本 ${r.version_label}`, 'check');
      await loadItems();
      await loadDiff(cmp);
    } catch (e) {
      if (e.isConflict) setConflict(e.details || {});
      else toast(errorText(e), e.isLocked ? 'lock' : 'alert');
    } finally {
      setSaving(false);
    }
  };
  const copyMine = async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制你改过的内容', 'copy');
    } catch {
      toast('复制失败，请手动选择', 'alert');
    }
  };

  const ranges = rangeChoices(sel, items || [], cmp);
  const toV = diff?.to?.commit ? byCommit.get(diff.to.commit) : null;
  const leftLabel = fromV ? versionTitle('iteration', fromV.n) : (diff?.from?.version_label || '');
  const rightLabel = toWorktree ? '当前' : (toV ? versionTitle('iteration', toV.n) : diff?.to?.version_label || '');
  const cmpLabel = (options.find((o) => o.value === cmp.path) || {}).label || (cmp.path ? pathLabel(cmp.path) : '');

  return (
    <ShellP active="versions">
      <div className="mr-2c vr-page" style={{ gridTemplateColumns: '420px minmax(0,1fr)' }}>
        <div className="card" style={{ overflow: 'hidden' }}>
          <div className="row" style={{ padding: '12px 14px', borderBottom: '1px solid var(--line)' }}>
            <b>{items ? `${filtered ? filtered.length : total} 个版本` : '正在读取…'}</b>
            <span className="sp" />
            <Select value={filter} options={SOURCE_FILTERS} onChange={setFilter} icon="filter" w="130px" className="vr-sel" />
          </div>
          {!filter && (history.error || history.releaseError) ? (
            <div className="note amber" style={{ margin: '10px 14px 0' }}>
              <Icon name="alert" />
              <div className="grow">
                {history.error ? `历史记录读取失败：${history.error}` : `发布版本读取失败：${history.releaseError}`}
              </div>
              <Button size="xs" onClick={() => setHistoryRevision((n) => n + 1)}>重试</Button>
            </div>
          ) : null}
          <div className="mr-tl">
            {(filter ? timeline : history.items).map((v) => (
              <HistoryRow
                key={historyEntryKey(v.kind ? v : { ...v, kind: 'iteration' })}
                v={v.kind ? v : { ...v, kind: 'iteration', version: v.n, can_revert: true }}
                on={v.commit === selected}
                onSelect={(x) => selectVersion(x)}
                onMenu={onMenu}
                onDelTag={deleteTag}
                reverting={reverting}
                downloading={downloading}
                onDownload={downloadRelease}
                onEvaluate={evaluateRelease}
              />
            ))}
            {filter && items && !timeline.length ? <div className="sm muted" style={{ padding: 14 }}>没有这个来源的版本。</div> : null}
            {!filter && !history.items.length && historyLoading ? <div className="sm muted" style={{ padding: 14 }}>正在读取…</div> : null}
            {!filter && !history.items.length && !historyLoading && !history.error ? (
              <div className="sm muted" style={{ padding: 14 }}>还没有版本记录</div>
            ) : null}
          </div>
          {!filter && history.nextCursor ? (
            <div className="vr-more"><Button size="sm" kind="ghost" disabled={historyLoading} onClick={loadMoreHistory}>加载更多</Button></div>
          ) : null}
        </div>

        <div className="col" style={{ gap: 16 }}>
          <VersionDetail
            v={sel ? { ...sel, can_revert: true } : null}
            detail={detail}
            current={cmp.path}
            note={note}
            reverting={reverting}
            onPick={(p) => setCmp((c) => ({ ...c, path: p }))}
            onFiles={setFilesFor}
            onTag={(v) => setTagFor(v.commit)}
            onRevert={revertTo}
          />
          {cmp.path ? (
            <div className="card" ref={cmpRef}>
              <div className="card-h">
                <span style={{ color: 'var(--blue)' }}><Icon name="split" /></span>
                <h3 className="ell">{cmpLabel}</h3>
                <span className="sp" />
                {ranges.length > 1 ? (
                  <Select
                    value={rangeValue(ranges, cmp)}
                    options={ranges}
                    onChange={(value) => { const r = ranges.find((o) => o.value === value); if (r) setCmp((c) => ({ ...c, from: r.from, to: r.to })); }}
                    icon="history"
                    w="190px"
                    className="vr-sel"
                  />
                ) : <span className="xs muted">{ranges[0]?.label || ''}</span>}
              </div>
              <div className="card-b">
                {locked ? <ScopeLockBar lock={scope} text={LOCK_TEXT} /> : null}
                {conflict ? (
                  <div className="note amber mb12">
                    <Icon name="alert" />
                    <div className="grow">{describeConflict(conflict).replace('，请刷新对比后再保存', '')}</div>
                    <Button size="xs" icon="copy" onClick={copyMine}>复制我的修改</Button>
                    <Button size="xs" kind="primary" onClick={() => loadDiff(cmp)}>刷新对比</Button>
                  </div>
                ) : null}
                {outside && !conflict ? (
                  <div className="note amber mb12">
                    <Icon name="alert" />
                    <div className="grow">这个文件刚被{SOURCES.find(([k]) => k === outside.by)?.[1] || '别处'}修改（版本 {outside.version_label}）</div>
                    <Button size="xs" icon="copy" onClick={copyMine}>复制我的修改</Button>
                    <Button size="xs" kind="primary" onClick={() => loadDiff(cmp)}>刷新对比</Button>
                  </div>
                ) : null}
                {diffErr ? <Note tone="gray">{diffErr}</Note> : null}
                {!cmp.from && !diffErr ? <div className="sm muted">这是第一个版本，没有更早的版本可比</div> : null}
                {diff && view ? (
                  <div className="mr-cmp">
                    <div className="c">
                      <div className="hd"><Icon name="lock" size={12} /><b>{leftLabel}</b><span className="muted">{fromV ? `${fmtStamp(fromV.at)} · ` : ''}只读</span></div>
                      <Lines lines={view.left} empty="这个版本里还没有这个文件" />
                    </div>
                    <div className={cx('c', !readOnly && 'ed')}>
                      <div className="hd">
                        <Icon name={readOnly ? 'lock' : 'edit'} size={12} />
                        <b>{rightLabel}</b>
                        <span className="muted">{readOnly ? (locked ? '只读 · 在助手的写入范围内' : toWorktree && !diff.right_revision ? '当前没有这个文件' : '只读') : '可编辑'}</span>
                        <span className="sp" />
                        {dirty ? <Chip tone="amber" sm>未保存 {blocks} 处</Chip> : !view.left.some((l) => l.k) && !view.right.some((l) => l.k) ? <Chip tone="gray" sm>没有差别</Chip> : null}
                      </div>
                      {readOnly ? <Lines lines={view.right} empty={toWorktree ? '当前没有这个文件' : '这个版本里没有这个文件'} /> : (
                        <CompareEditor key={`${gen}`} left={diff.left} value={diff.right} readOnly={false} onChange={setText} />
                      )}
                    </div>
                  </div>
                ) : null}
                {diff && !readOnly ? (
                  <div className="row mt12">
                    <span className="sp" />
                    <Button size="sm" kind="ghost" disabled={!dirty || saving} onClick={() => { setText(diff.right); setGen((g) => g + 1); setConflict(null); }}>放弃修改</Button>
                    <Button size="sm" kind="primary" disabled={!dirty || saving} onClick={save}>保存为新版本</Button>
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <TagModal commit={tagFor} onClose={() => setTagFor(null)} onDone={() => { setTagFor(null); loadItems(); }} />
      <RevertConfirm target={confirm} onCancel={cancelRevert} onConfirm={confirmRevert} />
      <FilesDrawer
        version={filesFor}
        onClose={() => setFilesFor(null)}
        onCompare={(p) => { const v = filesFor; setFilesFor(null); setSelected(v.commit); setCmp({ path: p, from: v.commit, to: 'WORKTREE' }); }}
      />
    </ShellP>
  );
}
