// 采集任务与证据快照（系统逆向建模插件的资料子页，设计稿 snapshots）：
// 统计（EV-12）、发起采集（EV-13 预览 → 生产库确认弹窗 → EV-14，缺 token 时 428 也弹）、采集任务（EV-15、EV-17，项目流 EP-13）、
// 失败任务联系管理员（EV-30）、快照列表与清理（EV-18、EV-20）、快照对比（EV-21）、推送建议修订（EV-22）、在对话里分析影响（S-20）。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { Button, Card, Chip, Field, Icon, Inp, KV, Modal, Note, Prog, Seg, Select, Stat, Table, Toggle, cx } from '../../ui/index.jsx';
import { CommandRunBar, useCommandTasks, useProjectEvent } from '../plugin/parts.jsx';
import {
  DIFF_CHANGE, GATEWAY_STATE, JOB_FILTERS, collectBody, defaultDiffPair, diffPair, fmtBudget, fmtDuration, fmtNum, fmtWhen,
  isJobActive, jobActions, jobDuration, jobFilterQuery, jobMatches, jobOpText, jobPhase, mergeJob, monthText, previewPairs,
  scopeTag, shortSnap, snapRecords, toggleSnapPick,
} from './evidenceModel.js';
import { EvdPlg, EvidenceGate, JobChip } from './parts.jsx';

const OPS = [['discover', '发现'], ['snapshot', '快照'], ['sample', '补采样本']];

function PhaseCell({ job }) {
  const ph = jobPhase(job);
  if (job.state === 'running') return <><div className="sm">{ph.text}</div><Prog p={ph.pct} thin /></>;
  if (ph.snapshot) return <span className="sm">{ph.text} <span className="mono">{ph.snapshot}</span></span>;
  return <span className={cx('sm', ph.muted && 'muted')} style={ph.tone === 'red' ? { color: 'var(--red)' } : undefined}>{ph.text}</span>;
}

// 生产库采集确认（设计稿 #evdCf）：EV-13 的 summary 展开成键值
function ConfirmModal({ confirm, onClose, onAgree, busy }) {
  const src = confirm?.source;
  return (
    <Modal
      open={!!confirm}
      onClose={onClose}
      icon="shield"
      title={`采集生产库 ${src?.id || ''}？`}
      sub="生产环境的数据源每次采集都要你同意。"
      w={540}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" icon="check" disabled={busy} onClick={onAgree}>同意并开始采集</Button></>}
    >
      <KV pairs={previewPairs(confirm?.preview?.summary)} w="96px" />
      <div className="mt12"><Note tone="gray" icon="list">同意后由内核下发票据交连接器网关执行，每次访问业务系统都写审计（语句摘要、行数、字节数、耗时）。</Note></div>
    </Modal>
  );
}

function SnapshotsBody({ route }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [summary, setSummary] = useState(null);
  const [sources, setSources] = useState(null);
  const [jobs, setJobs] = useState(null);
  const [jobFilter, setJobFilter] = useState('all');
  const [snaps, setSnaps] = useState(null);
  const [picked, setPicked] = useState([]);
  const [pair, setPair] = useState(null);
  const [diff, setDiff] = useState(null);
  const [change, setChange] = useState(null);
  const [form, setForm] = useState({ sourceId: '', operation: 'snapshot', incremental: true, budget: '', sampleTable: '' });
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());
  const jobsRef = useRef([]);
  jobsRef.current = jobs || [];
  const tasks = useCommandTasks('snapshots', { onFinish: (t) => t.state === 'done' && toast('影响分析跑完了，结果在工作台对话里', 'msg') });

  const loadSummary = useCallback(() => api.evidenceSummary(pid).then(setSummary).catch(() => {}), [api, pid]);
  const loadSnaps = useCallback(() => api.evidenceSnapshots(pid).then((r) => setSnaps(r.items || [])).catch((e) => toast(errorText(e), 'alert')), [api, pid]);
  const loadJobs = useCallback(() => api.evidenceJobs(pid, { state: jobFilterQuery(jobFilter) }).then((r) => setJobs(r.items || [])).catch((e) => toast(errorText(e), 'alert')), [api, pid, jobFilter]);
  useEffect(() => { loadSummary(); loadSnaps(); }, [loadSummary, loadSnaps]);
  useEffect(() => { loadJobs(); }, [loadJobs]);
  useEffect(() => {
    api.evidenceSources({ project_id: pid, filter: 'authorized' }).then((r) => {
      const list = (r.items || []).filter((s) => s.authorized_for_project);
      setSources(list);
      setForm((f) => (f.sourceId || !list[0] ? f : { ...f, sourceId: list[0].id, budget: fmtBudget(list[0].budget) }));
    }).catch(() => setSources([]));
  }, [api, pid]);

  // 默认对比同一数据源最新的两份完整快照；从证据浏览器带 ?to= 来时对比它和上一份
  const wantTo = route?.query?.to;
  useEffect(() => {
    if (!snaps || pair) return;
    let p = defaultDiffPair(snaps);
    if (wantTo && snaps.some((s) => s.id === wantTo)) {
      const to = snaps.find((s) => s.id === wantTo);
      const prev = snaps.filter((s) => s.complete && s.id !== to.id && s.source?.id === to.source?.id && String(s.started_at) < String(to.started_at))
        .sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0];
      if (prev) p = { from: prev.id, to: to.id };
    }
    if (p) { setPair(p); setPicked([p.from, p.to]); }
  }, [snaps, pair, wantTo]);
  useEffect(() => {
    if (!pair) return;
    let alive = true;
    api.evidenceSnapshotDiff(pid, { from: pair.from, to: pair.to, change: change || undefined })
      .then((r) => alive && setDiff(r)).catch((e) => { if (alive) { setDiff(null); toast(errorText(e), 'alert'); } });
    return () => { alive = false; };
  }, [api, pid, pair, change]);

  // 进度实时推送（EP-13）；任务结束时刷新快照和统计
  useProjectEvent('evidence.job.updated', (job) => {
    const before = jobsRef.current.find((j) => j.id === job.id);
    setJobs((list) => {
      const merged = mergeJob(list || [], job);
      return merged.filter((j) => jobMatches(j, jobFilter));
    });
    if (!before || before.state !== job.state) { loadSummary(); if (!isJobActive(job)) loadSnaps(); }
  });
  const anyRunning = (jobs || []).some((j) => j.state === 'running');
  useEffect(() => {
    if (!anyRunning) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [anyRunning]);

  const source = (sources || []).find((s) => s.id === form.sourceId) || null;
  const base = useMemo(() => (snaps || []).filter((s) => s.complete && s.source?.id === form.sourceId)
    .sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0]?.id || null, [snaps, form.sourceId]);
  const production = source?.environment === 'production';

  const started = (j, msg) => {
    setJobs((list) => mergeJob(list || [], j).filter((x) => jobMatches(x, jobFilter)));
    loadSummary();
    toast(j.state === 'queued' ? `任务 ${j.id} 已排队：${j.queue_reason || '等访问时间窗口'}` : msg || `已下发票据，任务 ${j.id} 开始执行`, 'play');
  };
  // 生产库缺 token（428）或过期（410）都回到确认弹窗
  const handleConsentError = async (e, ctx) => {
    if (e.needsConfirmation && e.details) { setConfirm({ ...ctx, preview: e.details }); return true; }
    if (e.code === 'confirmation_expired' && ctx.body) {
      toast('确认已过期，重新确认一次', 'clock');
      try { setConfirm({ ...ctx, preview: await api.previewEvidenceJob(pid, ctx.body) }); } catch (x) { toast(errorText(x), 'alert'); }
      return true;
    }
    return false;
  };

  const collect = async () => {
    if (!source) return;
    if (form.operation === 'sample' && !form.sampleTable.trim()) { toast('补采样本要写表名', 'alert'); return; }
    const body = collectBody({ ...form, base });
    setBusy(true);
    try {
      const pv = await api.previewEvidenceJob(pid, body);
      if (pv.requires_confirmation) setConfirm({ source, body, preview: pv });
      else started(await api.startEvidenceJob(pid, body));
    } catch (e) {
      if (!(await handleConsentError(e, { source, body }))) toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const agree = async () => {
    const c = confirm;
    const token = c.preview?.confirmation_token;
    setBusy(true);
    try {
      if (c.retryJob) {
        const j = await api.evidenceJobAction(pid, c.retryJob.id, 'retry', { confirmation_token: token });
        setConfirm(null);
        started(j);
      } else {
        const j = await api.startEvidenceJob(pid, { ...c.body, confirmation_token: token });
        setConfirm(null);
        started(j);
      }
    } catch (e) {
      if (!(await handleConsentError(e, c))) { setConfirm(null); toast(errorText(e), 'alert'); }
    } finally {
      setBusy(false);
    }
  };

  const jobAction = async (job, op) => {
    if (op === 'browse') { navigate(pagePath('schema', pid), { snapshot: job.result?.snapshot_id }); return; }
    if (op === 'notify_admin') {
      const reason = job.error?.code === 'credential_expired' ? 'credential_expired' : 'connection_failed';
      try {
        await api.notifyEvidenceAdmin(job.source.id, { project_id: pid, job_id: job.id, reason });
        toast(`已通知管理员：${job.source.id} 的${reason === 'credential_expired' ? '凭据已过期' : '连接有问题'}`, 'msg');
      } catch (e) { toast(errorText(e), 'alert'); }
      return;
    }
    try {
      const j = await api.evidenceJobAction(pid, job.id, op);
      if (op === 'cancel') {
        setJobs((list) => mergeJob(list || [], j).filter((x) => jobMatches(x, jobFilter)));
        loadSummary();
        toast(`已取消 ${job.id}`, 'ban');
      } else {
        started(j, op === 'resume' ? `已从断点续采，任务 ${j.id} 开始执行` : undefined);
      }
    } catch (e) {
      const src = (sources || []).find((s) => s.id === job.source.id) || job.source;
      if (!(await handleConsentError(e, { source: src, retryJob: job }))) toast(errorText(e), 'alert');
    }
  };

  const clean = async (s) => {
    try {
      const r = await api.deleteEvidenceSnapshot(pid, s.id);
      toast(`快照已清理，大件在 blob 仓保留 ${r.blob_retention_days} 天`, 'trash');
      setPicked((p) => p.filter((x) => x !== s.id));
      if (pair && (pair.from === s.id || pair.to === s.id)) { setPair(null); setDiff(null); }
      loadSnaps();
      loadSummary();
    } catch (e) { toast(errorText(e), 'alert'); }
  };

  const pickPair = diffPair(picked, snaps || []);
  const compare = () => { if (pickPair?.sameSource) { setChange(null); setPair({ from: pickPair.from, to: pickPair.to }); } };
  const analyze = () => tasks.send({
    command: '/ontology-evidence-impact', args: { from: pair.from, to: pair.to },
    text: `分析快照 ${shortSnap(pair.from)} → ${shortSnap(pair.to)} 的变化对已确认产物的影响`,
  });
  const push = async () => {
    try {
      const r = await api.pushSnapshotSuggestions(pid, { from: pair.from, to: pair.to });
      toast(r.created ? `已生成 ${r.created} 条建议修订，进入待处理` : '这次对比没有涉及已确认产物，不用修订', 'send');
    } catch (e) { toast(errorText(e), 'alert'); }
  };

  const s = summary;
  const [gwText, gwColor] = GATEWAY_STATE[s?.gateway?.status] || ['—', ''];
  const pairSnap = pair && (snaps || []).find((x) => x.id === pair.to);
  const counts = diff?.counts;

  return (
    <>
      <PageHead title={<>采集任务与证据快照 <EvdPlg /></>} desc="快照提交后不再修改。建模只读快照，不碰运行中的系统。" />
      <div className="grid4 mb16" style={{ gridTemplateColumns: 'repeat(5,1fr)' }}>
        <Stat label="快照" value={s?.snapshots?.count ?? '—'} icon="db" desc={`${s?.snapshots?.sources ?? 0} 个数据源`} />
        <Stat label="被引用的快照" value={s?.snapshots?.referenced ?? '—'} icon="lock" desc="不能清理，锚点在用" />
        <Stat label="本月采集" value={s?.jobs_this_month?.total ?? '—'} unit="次" icon="scan" desc={monthText(s?.jobs_this_month) || '本月还没有采集'} />
        <Stat label="进行中" value={s?.running_jobs ?? '—'} unit="个" icon="activity" desc={`排队 ${s?.queued_jobs ?? 0} 个`} />
        <Stat label="连接器网关" value={<span style={{ fontSize: 18, color: gwColor || undefined }}>{gwText}</span>} icon="server" desc={`已加载 ${s?.gateway?.providers_loaded ?? 0} 个提供方`} />
      </div>
      <div className="evd-top">
        <Card title="发起采集" icon="scan">
          {sources === null ? <div className="sm muted">正在读取…</div> : !sources.length ? (
            <div className="sm muted" style={{ lineHeight: 1.7 }}>
              本项目还没有授权的数据源。先到<a className="link" onClick={() => navigate(pagePath('sources', pid))}>业务系统数据源</a>授权一个，再回来采集。
            </div>
          ) : (
            <>
              <Field label="数据源">
                <Select icon="db" value={form.sourceId} options={sources.map((x) => ({ value: x.id, label: `${x.id} · ${x.display_name}` }))} onChange={(id) => { const x = sources.find((y) => y.id === id); setForm((f) => ({ ...f, sourceId: id, budget: fmtBudget(x?.budget) })); }} />
              </Field>
              <div className="row mt8 g12">
                <div className="grow"><Field label="操作"><Seg items={OPS} value={form.operation} onChange={(operation) => setForm((f) => ({ ...f, operation }))} /></Field></div>
                <div>
                  <Field label="增量采集">
                    <div className="row" style={{ height: 30 }}>
                      <Toggle on={form.operation === 'snapshot' && form.incremental && !!base} disabled={form.operation !== 'snapshot' || !base} onChange={(incremental) => setForm((f) => ({ ...f, incremental }))} title={form.operation !== 'snapshot' ? '只有快照能增量采集' : undefined} />
                      <span className="sm muted">{base ? `基于 ${shortSnap(base)}` : '还没有基线'}</span>
                    </div>
                  </Field>
                </div>
              </div>
              <div className="mt8">
                <Field label="范围" hint={form.operation === 'sample' ? '每表最多 20 行，按脱敏策略处理' : undefined}>
                  {form.operation === 'sample'
                    ? <Inp mono icon="table" value={form.sampleTable} placeholder="表名，如 replenish_std" onChange={(e) => setForm((f) => ({ ...f, sampleTable: e.target.value }))} />
                    : <div className="evd-tags"><span className="tag">{scopeTag(source?.scope_summary)}</span><span className="xs muted">沿用登记白名单</span></div>}
                </Field>
              </div>
              {form.operation !== 'sample' ? <div className="mt8"><Field label="预算"><Inp value={form.budget} onChange={(e) => setForm((f) => ({ ...f, budget: e.target.value }))} /></Field></div> : null}
              {production ? <div className="mt12"><Note tone="amber" icon="alert">生产环境：提交后会弹出确认卡，需要你同意才会下发票据。</Note></div> : null}
              <div className="row mt12"><span className="sp" /><Button kind="primary" icon="play" disabled={busy} onClick={collect}>发起采集</Button></div>
            </>
          )}
        </Card>
        <div>
          <Card title="采集任务" sub="经连接器网关执行 · 进度实时推送" icon="activity" extra={<Seg items={JOB_FILTERS} value={jobFilter} onChange={setJobFilter} />}>
            {jobs === null ? <div className="sm muted">正在读取…</div> : jobs.length ? (
              <Table
                compact
                cols={['任务', '数据源', '操作与范围', '状态', '阶段 / 结果', '开始', '耗时', '']}
                rows={jobs.map((j) => {
                  const dur = jobDuration(j, now);
                  return [
                    <span className="mono" style={{ whiteSpace: 'nowrap' }}>{j.id}</span>,
                    <span className="b" style={{ whiteSpace: 'nowrap' }}>{j.source?.id}</span>,
                    <span className="sm">{jobOpText(j)}</span>,
                    <span style={{ whiteSpace: 'nowrap' }}><JobChip state={j.state} /></span>,
                    <div style={{ minWidth: 190 }}><PhaseCell job={j} /></div>,
                    <span className="sm muted" style={{ whiteSpace: 'nowrap' }}>{j.started_at ? fmtWhen(j.started_at, now) : '—'}</span>,
                    <span className="sm muted num" style={{ whiteSpace: 'nowrap' }}>{dur == null ? '—' : fmtDuration(dur)}</span>,
                    <div className="row g4">{jobActions(j).map((a) => <Button key={a.op} size="xs" kind={a.kind} icon={a.icon} onClick={() => jobAction(j, a.op)}>{a.label}</Button>)}</div>,
                  ];
                })}
              />
            ) : <div className="sm muted" style={{ padding: '18px 4px' }}>{jobFilter === 'run' ? '现在没有进行中的采集任务。' : '还没有采集任务。'}</div>}
          </Card>
        </div>
      </div>

      <Card
        title="证据快照"
        sub="按记录 ID + 哈希做对比；快照本身永远不进知识包"
        icon="db"
        extra={(
          <>
            <span className="xs muted">{pickPair && !pickPair.sameSource ? '只能对比同一数据源的两份快照' : '选两份快照对比'}</span>
            <Button size="sm" icon="split" disabled={!pickPair?.sameSource} onClick={compare}>
              {pickPair ? `对比 ${shortSnap(pickPair.from)} → ${shortSnap(pickPair.to)}` : '对比'}
            </Button>
          </>
        )}
      >
        {snaps === null ? <div className="sm muted">正在读取…</div> : snaps.length ? (
          <div className="tbl-wrap">
            <table className="tbl compact evp-snap">
              <thead><tr>{['快照', '数据源', '完整度', '覆盖率', '记录条数', '被引用', ''].map((c) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {snaps.map((x) => (
                  <tr key={x.id} className={picked.includes(x.id) ? 'pick' : undefined} title="点一下选中，选两份对比" onClick={() => setPicked((p) => toggleSnapPick(p, x.id))}>
                    <td><span className="mono b" style={{ fontSize: 12 }}>{x.id}</span></td>
                    <td>{x.source?.id}</td>
                    <td>{x.complete ? <Chip tone="teal" icon="check" sm>完整</Chip> : <Chip tone="amber" icon="alert" sm>不完整</Chip>}</td>
                    <td><div style={{ width: 90 }}><Prog p={x.coverage_pct} thin tone={x.coverage_pct < 100 ? 'amber' : ''} /><span className="xs muted num">{x.coverage_pct}%</span></div></td>
                    <td><div className="evd-rec">{snapRecords(x.records).map(([k, n]) => <span key={k}>{k} {fmtNum(n)}</span>)}</div></td>
                    <td>
                      {x.referenced
                        ? <span className="row g4" data-pop="被已确认产物引用，不能清理；否则锚点会断"><Icon name="lock" size={12} /><b className="num">{x.referenced}</b><span className="xs muted">处</span></span>
                        : <span className="faint sm">0</span>}
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <div className="row g4">
                        <Button size="xs" kind="ghost" icon="search" title="浏览" onClick={() => navigate(pagePath('schema', pid), { snapshot: x.id })} />
                        {x.referenced ? <Button size="xs" disabled title="被引用，不能清理">清理</Button> : <Button size="xs" onClick={() => clean(x)}>清理</Button>}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="sm muted" style={{ padding: '18px 4px' }}>还没有快照。发起一次「快照」采集后会出现在这里。</div>}
      </Card>

      {pair ? (
        <div className="mt16">
          <Card
            title={`对比 ${pair.from} → ${pair.to}`}
            sub={`${pairSnap?.source?.id || ''} · 变化的锚点进入影响分析`}
            icon="split"
            extra={(
              <>
                <Button size="sm" icon="send" disabled={!counts?.touching_confirmed} onClick={push}>推送建议修订</Button>
                <Button size="sm" kind="primary" icon="target" disabled={!!tasks.active || tasks.sending} data-pop="发到工作台对话里执行：分析这两份快照的变化影响到哪些已确认产物。页面上显示进度，跑完结果在对话里" onClick={analyze}>在对话里分析影响</Button>
              </>
            )}
          >
            <CommandRunBar task={tasks.active} detail="结果在工作台对话里" style={{ marginBottom: 12 }} />
            <div className="evd-diffc">
              {[['added', 'a'], ['removed', 'd'], ['modified', 'm']].map(([k, cls]) => (
                <div key={k} className={cx(cls, 'f', change === k && 'on')} title={change === k ? '再点一下看全部' : `只看${DIFF_CHANGE[k][0]}`} onClick={() => setChange((c) => (c === k ? null : k))}>
                  <span className="xs muted">{DIFF_CHANGE[k][0]}</span><b>{counts ? counts[k] : '—'}</b>
                </div>
              ))}
              <div className="x"><span className="xs muted">涉及已确认产物的锚点</span><b>{counts ? counts.touching_confirmed : '—'}</b></div>
            </div>
            {diff?.items?.length ? (
              <Table
                compact
                cols={['变化', '类型', '锚点', '内容', '影响的产物']}
                rows={diff.items.map((it) => [
                  <Chip tone={DIFF_CHANGE[it.change]?.[1]} sm>{DIFF_CHANGE[it.change]?.[0] || it.change}</Chip>,
                  it.type,
                  <span className="anchor">{it.anchor}</span>,
                  it.summary,
                  it.affected?.length ? it.affected.map((r, i) => (
                    <span key={r.id || r.key}>
                      {i ? ' · ' : ''}
                      <span className="wl" onClick={() => (r.type ? navigate(pagePath('studio-table', pid)) : navigate(pagePath('studio-table', pid), { entry: r.id }))}>{r.name || r.title}</span>
                    </span>
                  )) : <span className="faint sm">还没有产物用到</span>,
                ])}
              />
            ) : <div className="sm muted" style={{ padding: '6px 2px' }}>{diff ? (change ? `没有${DIFF_CHANGE[change][0]}的记录。` : '两份快照之间没有记录变化。') : '正在比对…'}</div>}
          </Card>
        </div>
      ) : null}

      <ConfirmModal confirm={confirm} busy={busy} onClose={() => setConfirm(null)} onAgree={agree} />
    </>
  );
}

export default function SnapshotsPage({ route }) {
  return (
    <ShellP active="snapshots">
      <EvidenceGate page="snapshots"><SnapshotsBody route={route} /></EvidenceGate>
    </ShellP>
  );
}
