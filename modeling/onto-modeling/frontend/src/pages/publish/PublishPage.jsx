// 发布知识包：一栏三块。
// 1. 发布卡：一句结论（能不能发、哪几项没通过）+「发布」+ 选版本；检查项小标签（PB-01 门禁链，点一下跳到对应问题）；
//    未确认改动、上次发布、发布后「去评测」；「⋯」里是重新检查、导出迁移包（P-09）、旧版评测记录（ES-09）。
// 2. 需要处理：没通过或有提醒的检查项各一张卡（PublishProblems：数据绑定修正 PB-02、求解契约、结构自检、包门禁「去运行」FL-05）。
// 3. 已发布版本：Hub 版本（HB-05，带评测 ES-02、对比 PC 组）与本地发布记录（PB-04～PB-07）合一张表，表头有共享（HB-11）。
// 发布走 PB-03（求解包要确认未验证；只剩数据绑定没过时直接按降级发布）；项目流 release.updated（EP-19）、flow.updated（EP-01）、
// workspace.changed 实时刷新。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath, useRoute } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { ShellP } from '../../shell/Shell.jsx';
import { useProjectEvent } from '../plugin/parts.jsx';
import { EvaluationDialogs, LegacyEvaluations, useReleaseEvaluations } from '../evaluation/ReleaseEvaluation.jsx';
import { notifyPackagePublished } from '../evaluation/evaluationModel.js';
import { useFlywheelPanel } from '../flywheel/FlywheelPanel.jsx';
import PackageCompare from './PackageCompare.jsx';
import PublishProblems from './PublishProblems.jsx';
import PublishedVersions from './PublishedVersions.jsx';
import KnowledgeSharing from './KnowledgeSharing.jsx';
import { PUBLISH_FAILED_WARNING } from './sharingModel.js';
import {
  gateMark, iterationOptions, problemGates, proceedState, publishVerdict, publishedText, writebackText,
} from './publishCheckModel.js';
import {
  Button, Drawer, Icon, KV, Menu, Note, Select, cx,
} from '../../ui/index.jsx';
import { byNewest, fileGroups, fmtStamp, syncMeta } from '../versions/versionsModel.js';
import { usePopMenu } from '../versions/popMenu.js';
import { formatLabel, gateBlockedText, mergeRelease } from './publishModel.js';
import '../../styles/more.css';
import '../../styles/publish.css';

const listed = (groups) => groups.reduce((n, g) => n + g.items.length, 0);

function MoreMenu({ items }) {
  const menu = usePopMenu({ width: 180, height: 140 });
  return (
    <span ref={menu.ref}>
      <Button icon="more" title="更多" onClick={menu.toggle} />
      {menu.at ? <Menu className="pb-menu" style={menu.at} items={items} onPick={menu.close} /> : null}
    </span>
  );
}

const jump = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });

function PublishCard({
  check, gates, verdict, versions, selCommit, onPickVersion, proceed, publishing, onPublish, menu, solverNotice, onEvaluate,
}) {
  const [hints, setHints] = useState(false);
  const warnings = check?.binding?.warnings || [];
  // 数据绑定有问题时提示在它的卡里；通过时零散提示收在这里
  const bindingOk = (gates.find((g) => g.id === 'data_binding')?.status || 'pass') === 'pass';
  const uncommitted = check?.uncommitted_count || check?.uncommitted?.length || 0;
  return (
    <section className={cx('card pb-hero', verdict.tone)}>
      <div className="pb-hero-top">
        <span className="pb-verdict-i"><Icon name={verdict.tone === 'bad' ? 'alert' : verdict.tone === 'idle' ? 'clock' : 'upload'} size={18} /></span>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="pb-verdict">{verdict.title}</div>
          {verdict.sub ? <div className="sm muted mt4">{verdict.sub}</div> : null}
        </div>
        <Select value={selCommit} options={iterationOptions(versions)} onChange={onPickVersion} icon="tag" w="150px" className="pb-sel" />
        <Button kind="primary" icon="upload" disabled={proceed.disabled || publishing} title={proceed.title || undefined} onClick={onPublish}>
          {publishing ? '发布中…' : '发布'}
        </Button>
        <MoreMenu items={menu} />
      </div>
      {check ? <div className="pb-gates">
        {gates.map((g) => {
          const m = gateMark(g);
          return (
            <span key={g.id} className={cx('pb-gate', m.tone)} title={`${g.title}：${g.summary}`}
              {...(m.tone === 'ok' ? {} : { role: 'button', onClick: () => jump(`pb-sec-${g.id}`) })}>
              <Icon name={m.icon} size={12} />{g.title}
            </span>
          );
        })}
        <span className="xs muted pb-format">{formatLabel(check)}</span>
        <span className="sp" />
        {bindingOk && warnings.length ? <span className="pb-link xs" role="button" onClick={() => setHints((v) => !v)}>{warnings.length} 条提示</span> : null}
      </div> : null}
      {hints && bindingOk ? <div className="pb-hints">{warnings.map((w) => <div key={w} className="xs">{w}</div>)}</div> : null}
      {uncommitted ? <div className="pb-line xs">{uncommitted} 处改动还没确认，不会随这次发布</div> : null}
      {solverNotice ? (
        <Note tone="amber" className="mt12">
          <span className="row"><span className="grow">{solverNotice.text}</span><Button size="xs" onClick={onEvaluate}>去评测</Button></span>
        </Note>
      ) : null}
    </section>
  );
}

function ManifestDrawer({ rec, onClose, onDownload }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [m, setM] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    setM(null);
    setError('');
    if (!rec) return undefined;
    let alive = true;
    api.releaseManifest(pid, rec.version).then((x) => alive && setM(x)).catch((e) => alive && setError(errorText(e)));
    return () => { alive = false; };
  }, [api, pid, rec]);
  const groups = fileGroups(m?.files);
  return (
    <Drawer
      open={!!rec}
      onClose={onClose}
      icon="list"
      w={520}
      title={`包清单 · ${rec?.version || ''}`}
      sub={rec ? `${rec.version_label} · ${rec.format_label} · ${fmtStamp(rec.at)}` : ''}
      foot={(
        <>
          <Button size="sm" kind="ghost" onClick={() => onDownload(rec, 'graph')}>下载图模式包</Button>
          <Button size="sm" kind="primary" icon="download" onClick={() => onDownload(rec, 'standard')}>下载标准包</Button>
        </>
      )}
    >
      {error ? <Note tone="red">{error}</Note> : null}
      {!m && !error ? <div className="sm muted">正在读取包清单…</div> : null}
      {m ? (
        <>
          <KV
            w="96px"
            pairs={[
              ['包名', <span className="mono">{m.package_name}</span>],
              ['包版本', <span className="mono">{m.version}</span>],
              ['格式', `${rec.format_label}（schema ${m.schema_version}）`],
              ['源版本', rec.version_label],
              ['发布时间', fmtStamp(m.released_at)],
              ['能力版本', (m.packs || []).length ? m.packs.map((p) => `${p.name}@${p.version}`).join('、') : '—'],
              ['求解契约', m.solver ? m.solver.revision : '没有启用'],
              ['降级', m.degraded ? `${m.degraded.unbound_objects.join('、')} 不绑定数据表` : '没有降级'],
              ['大小', formatBytes(m.size)],
            ]}
          />
          {/* 包里的内容按业务类型列名字，不出目录和文件名（F4b-paths）；清单、说明这类文件只计数 */}
          <div className="sec-t mt16"><Icon name="file" size={14} />包里的内容<span className="sp" /><span className="faint">{m.files.length}</span></div>
          <div className="pb-files">
            {groups.map((g) => (
              <div key={g.key} className="row"><span className="b" style={{ flex: 'none', marginRight: 8 }}>{g.label}</span><span className="grow ell">{g.items.map((f) => f.name).join('、')}</span><span className="xs faint num">{g.items.length}</span></div>
            ))}
            {m.files.length > listed(groups) ? <div className="row"><span className="grow faint">另有 {m.files.length - listed(groups)} 个文件</span></div> : null}
          </div>
          {m.manifest ? (
            <details className="mt12">
              <summary className="sm link">manifest.yaml 原文</summary>
              <pre className="pb-pre">{m.manifest}</pre>
            </details>
          ) : null}
        </>
      ) : null}
    </Drawer>
  );
}

export default function PublishPage() {
  const { api, services } = useApp();
  const { pid, meta, setMeta } = useProject();
  const route = useRoute();
  const [versions, setVersions] = useState(null);
  const [commit, setCommit] = useState(null); // null 表示跟随当前版本
  const [check, setCheck] = useState(null);
  const [checking, setChecking] = useState(false);
  const [records, setRecords] = useState(null);
  const [publishing, setPublishing] = useState(false);
  const [ack, setAck] = useState(false); // 我已了解，发布后需完成求解验证
  const [releasesTick, setReleasesTick] = useState(0);
  const [solverNotice, setSolverNotice] = useState(null); // 求解包发布后待验证：develop 成功提示里的「去评测」
  const [manifestFor, setManifestFor] = useState(null);
  const [starting, setStarting] = useState(false);
  const [legacyOpen, setLegacyOpen] = useState(false);
  // 版本对比（PackageCompare）：{releaseId, version}，空对象表示不带初始版本、默认比最近两版
  const [compareFor, setCompareFor] = useState(null);
  // 发布或回退后记录数变了就重读评测状态
  const ev = useReleaseEvaluations(pid, records ? records.length : 0);
  const commitRef = useRef(commit);
  commitRef.current = commit;

  const loadVersions = useCallback(() => api.versions(pid, { limit: 200 }).then((r) => {
    setVersions(byNewest(r.items));
    setMeta((m) => syncMeta(m, r));
  }).catch((e) => toast(errorText(e), 'alert')), [api, pid, setMeta]);
  const loadRecords = useCallback(() => api.releases(pid).then((r) => setRecords(r.items || [])).catch((e) => toast(errorText(e), 'alert')), [api, pid]);
  const runCheck = useCallback(async ({ refresh = false } = {}) => {
    setChecking(true);
    try {
      const c = await api.publishCheck(pid, { commit: commitRef.current || undefined, refresh: refresh || undefined });
      setCheck(c);
      if (refresh) toast('已重新检查', 'refresh');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setChecking(false);
    }
  }, [api, pid]);

  useEffect(() => { loadVersions(); loadRecords(); }, [loadVersions, loadRecords]);
  // 数据飞轮改写工作区或发布新版本后：重读版本、发布记录、发布检查与「已发布版本」
  const flywheel = useFlywheelPanel(pid, () => { loadVersions(); loadRecords(); runCheck(); setReleasesTick((n) => n + 1); });
  useEffect(() => { runCheck(); }, [runCheck, commit]);
  // 项目列表菜单「已发布版本」带 ?versions=1 进来：滚到已发布版本
  useEffect(() => {
    if (route?.query?.versions !== '1') return undefined;
    const t = setTimeout(() => jump('pb-releases'), 300);
    return () => clearTimeout(t);
  }, [route?.query?.versions]);

  const timer = useRef(null);
  const later = (fn) => { clearTimeout(timer.current); timer.current = setTimeout(fn, 400); };
  useEffect(() => () => clearTimeout(timer.current), []);
  useProjectEvent('release.updated', (d) => {
    if (!d.release) return;
    setRecords((list) => mergeRelease(list, d.release));
    setReleasesTick((n) => n + 1);
  });
  useProjectEvent('flow.updated', () => later(() => runCheck()));
  useProjectEvent('workspace.changed', () => { loadVersions(); later(() => runCheck()); });

  const head = (versions || []).find((v) => v.current) || null;
  const selCommit = commit || head?.commit || '';
  const selected = (versions || []).find((v) => v.commit === selCommit) || head;
  const ackNeeded = !!check?.solver?.requires_validation_after_publish;
  const proceed = proceedState(check, ack);
  const gates = check?.gates || [];
  const verdict = publishVerdict(check, { versionLabel: selected?.version_label || meta?.version_label || '', acknowledged: ack });
  // 需要处理：没通过或有提醒的检查项；求解包要确认未验证时求解契约也列出来（说明与勾选在它的卡里）
  const problems = problemGates(gates).concat(
    ackNeeded ? gates.filter((g) => g.id === 'solver_contract' && g.status === 'pass') : [],
  );

  const publish = async (degrade = false) => {
    setPublishing(true);
    try {
      const acknowledged = ackNeeded && ack;
      const r = await api.publishRelease(pid, {
        commit: check?.commit, ...(degrade ? { degrade: true } : {}), ...(acknowledged ? { solver_unverified_acknowledged: true } : {}),
      });
      // 是否降级以发布记录为准：带了 degrade、但发布时已没有对不上的对象，记录就是正常发布
      const done = publishedText(r, { degrade: Boolean(r.gates?.degraded), acknowledged });
      toast(done.text, done.tone === 'warning' ? 'alert' : 'upload');
      notifyPackagePublished(pid);
      setSolverNotice(done.evaluate && r.hub?.release_id ? { text: done.text, release: { ...r.hub, version: r.version } } : null);
      const writeback = writebackText(r);
      if (writeback) setTimeout(() => toast(writeback.text, writeback.tone === 'warning' ? 'alert' : 'check'), 2500);
      // 共享用户的 Hub 授权同步失败（develop !590）：发布照常成功，去表头「共享」重试
      if (r.hub_members?.status === 'failed') setTimeout(() => toast(PUBLISH_FAILED_WARNING, 'alert'), writeback ? 5000 : 2500);
      if (r.local_record_pending) loadRecords();
      else setRecords((list) => mergeRelease(list, r));
      setReleasesTick((n) => n + 1);
      setAck(false);
      loadVersions();
      runCheck();
      setTimeout(() => jump('pb-releases'), 300);
    } catch (e) {
      toast(e.code === 'gate_blocked' ? gateBlockedText(e.details) : errorText(e), 'alert');
      runCheck();
    } finally {
      setPublishing(false);
    }
  };
  const gateAction = async (g) => {
    const action = g.action;
    if (!action) return;
    if (action.type === 'navigate') { navigate(pagePath(action.page, pid), action.params); return; }
    if (action.type !== 'start_node') return;
    setStarting(true);
    try {
      const r = await api.startNode(pid, action.node_id, { origin: { page: 'publish', label: '发布' } });
      toast(`已在工作台对话里开始「${g.title}」`, 'play');
      const cid = r.conversation_id || r.task?.conversation_id;
      navigate(pagePath('wb-l2', pid), cid ? { c: cid } : undefined);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setStarting(false);
    }
  };
  const download = async (rec, variant) => {
    try {
      await services.download(
        `/projects/${encodeURIComponent(pid)}/releases/${encodeURIComponent(rec.version)}/download${variant === 'graph' ? '?variant=graph' : ''}`,
        `${meta?.package_name || 'package'}-${rec.version}${variant === 'graph' ? '-graph' : ''}.zip`,
      );
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const exportTransfer = async () => {
    try {
      await services.download(`/projects/${encodeURIComponent(pid)}/transfer-package`, `${meta?.name || 'project'}-transfer.zip`);
      toast('已导出迁移包', 'archive');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  const menu = [
    { label: '重新检查', icon: 'refresh', disabled: checking, onClick: () => runCheck({ refresh: true }) },
    { label: '导出迁移包', icon: 'archive', hint: '含资料和版本历史', onClick: exportTransfer },
    { label: '旧版评测记录', icon: 'history', onClick: () => setLegacyOpen(true) },
  ];

  return (
    <ShellP active="publish">
      <div className="pb-page">
        <PublishCard
          check={check}
          gates={gates}
          verdict={verdict}
          versions={versions}
          selCommit={selCommit}
          onPickVersion={(c) => setCommit(c === head?.commit ? null : c)}
          proceed={proceed}
          publishing={publishing}
          onPublish={() => publish(proceed.degrade)}
          menu={menu}
          solverNotice={solverNotice}
          onEvaluate={() => { ev.evaluate(solverNotice.release, 'solver'); setSolverNotice(null); }}
        />
        {check ? (
          <PublishProblems
            check={check}
            gates={problems}
            acknowledged={ack}
            onAcknowledge={setAck}
            onRecheck={() => runCheck()}
            onChanged={() => { setCommit(null); loadVersions(); }}
            onGateAction={gateAction}
            busy={starting}
          />
        ) : null}
        <PublishedVersions
          actions={flywheel.entry}
          projectId={pid}
          projectName={meta?.name || ''}
          ev={ev}
          records={records}
          refreshKey={releasesTick}
          sharing={<KnowledgeSharing projectId={pid} refreshKey={releasesTick} />}
          onCompare={(target) => setCompareFor(target || {})}
          onManifest={setManifestFor}
          onDownloadLocal={download}
        />
      </div>
      <EvaluationDialogs ev={ev} />
      <LegacyEvaluations pid={pid} open={legacyOpen} onClose={() => setLegacyOpen(false)} />
      <PackageCompare open={!!compareFor} projectId={pid} projectName={meta?.name || ''} initialReleaseId={compareFor?.releaseId || ''}
        initialVersion={compareFor?.version || ''} onClose={() => setCompareFor(null)} />
      <ManifestDrawer rec={manifestFor} onClose={() => setManifestFor(null)} onDownload={download} />
      {flywheel.dialog}
    </ShellP>
  );
}
