// 版本历史与对比（设计稿 versions）：左栏是历史时间线（V-09，迭代与 Hub 发布版本合并，元素照 develop 的「历史」页签，
// ADR-0094），按来源筛选时改列 V-01；「改了什么」（V-02）、与当前对比（V-04，右侧可编辑，保存走 F-03）、
// 某版本的文件（V-03、F-06）、标记与删除标记（V-07、V-08）、回退（V-05 预览 → V-06）；项目流 workspace.changed（EP-05）刷新。
// 从别处跳来：?v=&run=&slice=（待处理、自主构建报告、运行矩阵的「查看改动」）选中那个版本；
//            ?path=&from=&to=WORKTREE（原文编辑器的「刷新后对比」）直接打开这个文件的对比。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { describeConflict, errorText } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { ScopeLockBar, useProjectEvent } from '../plugin/parts.jsx';
import {
  Button, Card, Chip, Icon, Menu, Note, Select, St, cx,
} from '../../ui/index.jsx';
import { createLatestRequest } from '../../lib/latestRequest.js';
import { EMPTY_HISTORY, appendHistoryPage, historyEntryKey, versionTitle } from '../../lib/versionHistory.js';
import { hasWorkspaceDirty, setWorkspaceDirty } from '../../lib/workspaceDirty.js';
import { openEvaluation, prefetchEvaluationEntry } from '../evaluation/evaluationEntry.js';
import CompareEditor from './CompareEditor.jsx';
import { usePopMenu } from './popMenu.js';
import { FilesDrawer, RevertConfirm, TagModal } from './dialogs.jsx';
import {
  SOURCES, SOURCE_FILTERS, byNewest, changeText, compareFromOf, compareOptions, defaultCompare, diffLines, editBlocks, entryPath,
  fmtStamp, pathLocked, syncMeta, pickLanding, rangeOptions, reviewView, revertBlockedText, srcClass, srcLabel, targetLabel, versionMenu,
  changeRows, pathLabel, versionSub,
} from './versionsModel.js';
import '../../styles/more.css';
import '../../styles/versions.css';

const LOCK_TEXT = {
  running: ['助手正在改这个文件，这一轮结束后才能在这里编辑'],
  pending: ['这个文件和助手这一轮的修改有冲突，选好保留哪边后才能在这里编辑'],
};

// 历史时间线的一条（元素照 develop 的「历史」页签）：发布版本带「下载」「评测」；迭代带说明、标记、
// 「标记」「回退到此版本」，另有本分支的来源、当前标记与 ⋯（查看文件、对比）
function HistoryRow({ v, on, onSelect, onMenu, onTag, onDelTag, onRevert, reverting, downloading, onDownload, onEvaluate }) {
  const menu = usePopMenu({ width: 190, height: 120 });
  if (v.kind === 'release') {
    return (
      <div className="mr-vr vr-release">
        <span className="dot" />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row">
            <b className="v">{versionTitle(v.kind, v.version)}</b>
            <span className="xs muted num">{v.time ? fmtStamp(v.time * 1000) : ''}</span>
          </div>
          <div className="sm mt4">发布知识包{v.source_service ? ` · 来源 ${v.source_service}` : ''}</div>
          <div className="row mt4 g4">
            <Button size="xs" kind="ghost" disabled={downloading === v.release_id} onClick={() => onDownload(v.release_id)}>下载</Button>
            <Button size="xs" kind="ghost" onClick={() => onEvaluate(v)}>评测</Button>
          </div>
        </div>
      </div>
    );
  }
  const tags = v.tags || [];
  return (
    <div className={cx('mr-vr', v.current && 'head', on && 'on', tags.length && 'vr-tagged')} onClick={() => onSelect(v)}>
      <span className="dot" />
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="row">
          <b className="v">{versionTitle('iteration', v.version)}</b>
          {v.source ? <span className={cx('mr-src', srcClass(v.source))}>{srcLabel(v)}</span> : null}
          <span className="xs muted num">{fmtStamp(v.at || (v.time ? v.time * 1000 : ''))}</span>
          {v.current ? <Chip tone="blue" sm>当前</Chip> : null}
          <span className="sp" />
          <span ref={menu.ref} onClick={(e) => e.stopPropagation()}>
            <Button size="xs" kind="ghost" icon="more" title="更多操作" onClick={menu.toggle} />
            {menu.at ? (
              <Menu
                className="vr-menu"
                style={menu.at}
                items={versionMenu(v).map((x) => (x === '-' ? x : { ...x, onClick: () => onMenu(x.key, v) }))}
                onPick={menu.close}
              />
            ) : null}
          </span>
        </div>
        <div className="sm mt4">{v.summary || v.message}</div>
        {tags.length ? (
          <div className="row mt4 g4 wrap">
            {tags.map((t) => (
              <span key={t.name} className="mr-tagc">
                <Icon name="tag" size={12} />{t.label}
                <button type="button" title="删除标记" onClick={(e) => { e.stopPropagation(); onDelTag(t); }}><Icon name="x" size={12} /></button>
              </span>
            ))}
          </div>
        ) : null}
        <div className="row mt4 g4" onClick={(e) => e.stopPropagation()}>
          <Button size="xs" kind="ghost" onClick={() => onTag(v)}>标记</Button>
          {!v.current && v.can_revert !== false ? (
            <Button size="xs" kind="danger" disabled={!!reverting} onClick={() => onRevert(v)}>
              {reverting === v.commit ? '回退中…' : '回退到此版本'}
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// 「vX 改了什么」：点一行，对比切到这个条目
function ChangesCard({ detail, onPick, current }) {
  if (!detail) return null;
  const changes = detail.changes || [];
  const n = changes.filter((c) => c.review === 'needs_review').length;
  return (
    <Card
      title={`${versionTitle('iteration', detail.n)} 改了什么`}
      icon="file"
      sub={versionSub(detail)}
      extra={n ? <Chip tone="indigo" sm icon="eye">{n} 项需复核</Chip> : null}
    >
      {changes.length ? (
        <div className="tbl-wrap">
          <table className="tbl compact">
            <thead><tr><th>内容</th><th>改动</th><th>复核</th></tr></thead>
            <tbody>
              {changeRows(changes).map((c, i) => {
                const p = entryPath(c.target);
                const r = reviewView(c.review);
                return (
                  <tr key={i} className={cx(p && 'vr-click', p && p === current && 'vr-cur')} onClick={() => p && onPick(p)} title={p ? '和当前对比这个条目' : undefined}>
                    <td>{targetLabel(c.target)}</td>
                    <td>{changeText(c)}</td>
                    <td>{r?.st ? <St k={r.st} sm /> : r?.chip ? <Chip tone={r.chip[1]} sm>{r.chip[0]}</Chip> : <span className="faint">—</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : <div className="sm muted">这个版本没有改动条目</div>}
    </Card>
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
      setCmp({ path: null, from: compareFromOf(v, items)?.commit || null, to: 'WORKTREE' });
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

  const selectVersion = (v, scroll) => {
    setSelected(v.commit);
    setNote('');
    setCmp((c) => ({ ...c, from: compareFromOf(v, items)?.commit || v.commit, to: 'WORKTREE' }));
    if (scroll) cmpRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const onMenu = (key, v) => {
    if (key === 'files') setFilesFor(v);
    if (key === 'compare') selectVersion(v, true);
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

  const rangeOpts = rangeOptions(items || []);
  const objOpts = options.length ? options : [{ value: '', label: '选择对象' }];
  const leftLabel = fromV ? versionTitle('iteration', fromV.n) : (diff?.from?.version_label || '');

  return (
    <ShellP active="versions">
      <PageHead
        title="版本历史"
        actions={<Button icon="tag" disabled={!head} onClick={() => setTagFor(head?.commit)}>标记当前版本</Button>}
      />
      <div className="mr-2c vr-page" style={{ gridTemplateColumns: '480px minmax(0,1fr)' }}>
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
                onTag={(x) => setTagFor(x.commit)}
                onDelTag={deleteTag}
                onRevert={revertTo}
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
          <div className="row xs muted" style={{ padding: '10px 14px' }}>
            {SOURCES.map(([k, t, c]) => <span key={k} className={cx('mr-src', c)}>{t}</span>)}
          </div>
        </div>

        <div className="col" style={{ gap: 16 }}>
          {note ? <Note tone="gray" icon="info">{note}</Note> : null}
          <div className="card" ref={cmpRef}>
            <div className="card-h">
              <span style={{ color: 'var(--blue)' }}><Icon name="split" /></span>
              <h3>对比</h3>
              <span className="sp" />
              <Select value={cmp.path || ''} options={objOpts} onChange={(p) => p && setCmp((c) => ({ ...c, path: p }))} icon="cube" w="220px" className="vr-sel" />
              <Select
                value={cmp.from || ''}
                options={rangeOpts.length ? rangeOpts : [{ value: '', label: '—' }]}
                onChange={(c) => { setSelected(c); setNote(''); setCmp((x) => ({ ...x, from: c, to: 'WORKTREE' })); }}
                icon="history"
                w="150px"
                className="vr-sel"
              />
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
              {!cmp.path && detail && !diffErr ? (
                <div className="sm muted">这个版本没有条目改动</div>
              ) : null}
              {diff && view ? (
                <div className="mr-cmp">
                  <div className="c">
                    <div className="hd"><Icon name="lock" size={12} /><b>{leftLabel}</b><span className="muted">{fromV ? `${fmtStamp(fromV.at)} · ` : ''}只读</span></div>
                    <Lines lines={view.left} empty="这个版本里还没有这个文件" />
                  </div>
                  <div className={cx('c', !readOnly && 'ed')}>
                    <div className="hd">
                      <Icon name={readOnly ? 'lock' : 'edit'} size={12} />
                      <b>{toWorktree ? '当前' : diff.to.version_label}</b>
                      <span className="muted">{readOnly ? (locked ? '只读 · 在助手的写入范围内' : toWorktree && !diff.right_revision ? '当前没有这个文件' : '只读') : '可编辑'}</span>
                      <span className="sp" />
                      {dirty ? <Chip tone="amber" sm>未保存 {blocks} 处</Chip> : !view.left.some((l) => l.k) && !view.right.some((l) => l.k) ? <Chip tone="gray" sm>没有差别</Chip> : null}
                    </div>
                    {readOnly ? <Lines lines={view.right} empty="当前没有这个文件" /> : (
                      <CompareEditor key={`${gen}`} left={diff.left} value={diff.right} readOnly={false} onChange={setText} />
                    )}
                  </div>
                </div>
              ) : null}
              {diff ? (
                <div className="row mt12">
                  <span className="sp" />
                  <Button size="sm" kind="ghost" disabled={!dirty || saving} onClick={() => { setText(diff.right); setGen((g) => g + 1); setConflict(null); }}>放弃修改</Button>
                  <Button size="sm" kind="primary" disabled={!dirty || readOnly || saving} onClick={save}>保存为新版本</Button>
                </div>
              ) : null}
              {fromV ? (
                <>
                  <div className="divider" />
                  <div className="row">
                    <Button size="sm" icon="eye" onClick={() => setFilesFor(fromV)}>查看{versionTitle('iteration', fromV.n)}的全部文件</Button>
                    <Button size="sm" icon="undo" disabled={fromV.current || !!reverting} title={fromV.current ? '已是当前版本' : undefined} onClick={() => revertTo(fromV)}>回退到此版本</Button>
                  </div>
                </>
              ) : null}
            </div>
          </div>
          <ChangesCard detail={detail} current={cmp.path} onPick={(p) => setCmp((c) => ({ ...c, path: p }))} />
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
