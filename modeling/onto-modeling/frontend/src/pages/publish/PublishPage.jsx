// 发布知识包（设计稿 publish）：门禁链（PB-01；合规检查「去运行」走 FL-05；建模阶段没做完时第一关挡住）、
// 发布检查（develop PublishCheckDialog 的元素：数据绑定修正 PB-02、求解契约、结构自检、表的来源）、
// 发布与降级发布（PB-03，经知识包 Hub，求解包要确认未验证；成功后打开「已发布版本」HB-05）、导出迁移包（P-09）、发布记录（PB-04）与包清单（PB-06）、下载标准包 / 图模式变体（PB-07）。
// 版本下拉取当前分支上的版本（V-01）；项目流 release.updated（EP-19）、flow.updated（EP-01）、workspace.changed 实时刷新。
// 发布记录带评测（ES-02，ADR-0092）与版本对比（PC 组，ADR-0095）。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath, useRoute } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { evidenceInProject } from '../../state/evidence.js';
import { useCommandTasks, useProjectEvent } from '../plugin/parts.jsx';
import { EvaluationDialogs, LegacyEvaluations, useReleaseEvaluations } from '../evaluation/ReleaseEvaluation.jsx';
import { notifyPackagePublished } from '../evaluation/evaluationModel.js';
import PackageCompare from './PackageCompare.jsx';
import PublishCheck from './PublishCheck.jsx';
import PublishedVersions from './PublishedVersions.jsx';
import { proceedState, publishedText, writebackText } from './publishCheckModel.js';
import { fileGroups } from '../versions/versionsModel.js';

const listed = (groups) => groups.reduce((n, g) => n + g.items.length, 0);
import {
  Button, Card, Chip, Drawer, Icon, KV, Note, Select, St, cx,
} from '../../ui/index.jsx';
import { byNewest, fmtStamp, syncMeta } from '../versions/versionsModel.js';
import {
  blockingSummary, formatLabel, gateBlockedText, gateView, mergeRelease, releaseGate, versionOptions, visibleGates,
} from './publishModel.js';
import '../../styles/more.css';
import '../../styles/publish.css';

function GateChain({ check, onAction, busy }) {
  const sum = blockingSummary(check);
  const evidenceOn = evidenceInProject(useProject());
  return (
    <>
      <div className="row mb8">
        <span className="sec-t" style={{ margin: 0 }}><Icon name="shield" size={14} />门禁链</span>
        {check ? <span className={cx('chip sm', sum.tone)}>{sum.text}</span> : null}
      </div>
      <div className="mr-gates">
        {visibleGates(check, evidenceOn).map((g, i) => {
          const v = gateView(g);
          return [
            i ? <span key={`a${g.id}`} className="mr-garr"><Icon name="chev" size={14} /></span> : null,
            <div key={g.id} className={cx('mr-gate', v.cls)} title={(g.findings || []).map((f) => f.message).join('\n') || undefined}>
              <div className="row">
                <span className="gi"><Icon name={v.icon} size={14} /></span>
                <div className="t grow">{g.title}</div>
                <St k={v.chip.k} sm label={v.chip.label} />
              </div>
              <div className="d mt8">{g.summary}</div>
              {v.action ? <div className="mt8"><Button size="xs" kind="soft" disabled={busy} onClick={() => onAction(g, v.action.action)}>{v.action.label}</Button></div> : null}
            </div>,
          ];
        })}
      </div>
    </>
  );
}

function ReleaseRecords({ records, onManifest, onDownload, onCompare }) {
  const rows = records || [];
  return (
    <Card className="mt16" title="发布记录" icon="history">
      {records && !records.length ? (
        <div className="sm muted">还没有发布过</div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl compact">
            <thead>
              <tr>
                <th>版本</th><th>时间</th><th>格式</th><th>门禁</th><th>包清单</th>
                <th style={{ width: 170 }} />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const g = releaseGate(r);
                return (
                  <tr key={r.version}>
                    <td><b title={`包版本 ${r.version}`}>{r.version_label}</b></td>
                    <td className="num">{fmtStamp(r.at)}</td>
                    <td>{r.format_label}</td>
                    <td><Chip tone={g.chip[1]} sm>{g.chip[0]}</Chip>{g.note ? <> <span className="xs muted">{g.note}</span></> : null}</td>
                    <td><span className="xs pb-link" onClick={() => onManifest(r)} title="查看包清单">{r.manifest_attached ? '已附包清单' : '没有留存包'}</span></td>
                    <td>
                      <span className="row" style={{ justifyContent: 'flex-end', gap: 12 }}>
                        <span className="xs pb-link" onClick={() => onCompare({ releaseId: r.hub?.release_id || '', version: r.version })}>与其他版本对比</span>
                        <span className="xs pb-link row g4" title="下载标准包" onClick={() => onDownload(r, 'standard')}><Icon name="download" size={12} />标准包</span>
                        <span className="xs pb-link" title="下载图模式包" onClick={() => onDownload(r, 'graph')}>图模式</span>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
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
      title={`包清单 · ${rec?.version_label || ''}`}
      sub={rec ? `包版本 ${rec.version} · ${rec.format_label} · ${fmtStamp(rec.at)}` : ''}
      foot={(
        <>
          <Button size="sm" kind="ghost" onClick={() => onDownload(rec, 'graph')}>下载图模式变体</Button>
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
  const [solverNotice, setSolverNotice] = useState(null); // 求解包发布后待验证：develop 成功提示里的「去评测」
  // 已发布版本（HB-05）：项目列表菜单带 ?versions=1 进来时直接打开；发布成功后自动打开
  const [versionsOpen, setVersionsOpen] = useState(() => route?.query?.versions === '1');
  const [manifestFor, setManifestFor] = useState(null);
  const [starting, setStarting] = useState(false);
  // 版本对比（P4 的 PackageCompare）：{releaseId, version}，空对象表示不带初始版本、默认比最近两版
  const [compareFor, setCompareFor] = useState(null);
  const tasks = useCommandTasks('publish');
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
  useEffect(() => { runCheck(); }, [runCheck, commit]);

  const timer = useRef(null);
  const later = (fn) => { clearTimeout(timer.current); timer.current = setTimeout(fn, 400); };
  useEffect(() => () => clearTimeout(timer.current), []);
  useProjectEvent('release.updated', (d) => d.release && setRecords((list) => mergeRelease(list, d.release)));
  useProjectEvent('flow.updated', () => later(() => runCheck()));
  useProjectEvent('workspace.changed', () => { loadVersions(); later(() => runCheck()); });

  const head = (versions || []).find((v) => v.current) || null;
  const ackNeeded = !!check?.solver?.requires_validation_after_publish;
  const proceed = proceedState(check, ack);

  const publish = async (degrade = false) => {
    setPublishing(true);
    try {
      const acknowledged = ackNeeded && ack;
      const r = await api.publishRelease(pid, {
        commit: check?.commit, ...(degrade ? { degrade: true } : {}), ...(acknowledged ? { solver_unverified_acknowledged: true } : {}),
      });
      const done = publishedText(r, { degrade, acknowledged });
      toast(done.text, done.tone === 'warning' ? 'alert' : 'upload');
      notifyPackagePublished(pid);
      setSolverNotice(done.evaluate && r.hub?.release_id ? { text: done.text, release: { ...r.hub, version: r.version } } : null);
      const writeback = writebackText(r);
      if (writeback) setTimeout(() => toast(writeback.text, writeback.tone === 'warning' ? 'alert' : 'check'), 2500);
      if (r.local_record_pending) loadRecords();
      else setRecords((list) => mergeRelease(list, r));
      setAck(false);
      loadVersions();
      runCheck();
      setVersionsOpen(true);
    } catch (e) {
      toast(e.code === 'gate_blocked' ? gateBlockedText(e.details) : errorText(e), 'alert');
      runCheck();
    } finally {
      setPublishing(false);
    }
  };
  const gateAction = async (g, action) => {
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

  const pkg = check?.package_name || meta?.package_name || '';
  const mainLabel = head?.version_label || meta?.version_label || '';
  const selCommit = commit || head?.commit || '';

  return (
    <ShellP active="publish">
      <PageHead
        title="发布知识包"
        desc={`${pkg} · 当前版本 ${mainLabel}`}
        actions={<><LegacyEvaluations pid={pid} tasks={tasks} /><Button kind="primary" icon="split" onClick={() => setCompareFor({})}>知识包版本对比</Button><Button icon="history" onClick={() => setVersionsOpen(true)}>已发布版本（知识包评测）</Button><Button icon="refresh" disabled={checking} onClick={() => runCheck({ refresh: true })}>重新检查</Button></>}
      />
      <GateChain check={check} onAction={gateAction} busy={starting} />
      {solverNotice ? (
        <Note tone="amber" className="mb12">
          <span className="row"><span className="grow">{solverNotice.text}</span>
            <Button size="xs" onClick={() => { ev.evaluate(solverNotice.release, 'solver'); setSolverNotice(null); }}>去评测</Button></span>
        </Note>
      ) : null}
      {check?.uncommitted?.count ? (
        <Note tone="amber" className="mb12">{check.uncommitted.count} 处改动未确认，不会随这次发布</Note>
      ) : null}
      <div className="mr-2c" style={{ gridTemplateColumns: 'minmax(0,1fr) 400px' }}>
        {check ? (
          <PublishCheck
            check={check}
            checking={checking}
            acknowledged={ack}
            onAcknowledge={setAck}
            publishing={publishing}
            onRecheck={() => runCheck()}
            onDegrade={() => publish(true)}
            onChanged={() => { setCommit(null); loadVersions(); }}
          />
        ) : <div className="card pad sm muted">正在检查…</div>}
        <div className="col" style={{ gap: 12 }}>
          <div className="mr-exp main">
            <span className="ei"><Icon name="upload" size={18} /></span>
            <div className="grow">
              <div className="b mb8">发布为知识包</div>
              <div className="row">
                <Chip tone="blue" icon="cpu">{formatLabel(check)}</Chip>
                <Select value={selCommit} options={versionOptions(versions)} onChange={(c) => setCommit(c === head?.commit ? null : c)} icon="tag" w="104px" className="pb-sel" />
                <span className="sp" />
                <Button size="sm" kind="primary" disabled={proceed.disabled || publishing} title={proceed.title || undefined} onClick={() => publish(false)}>{proceed.label}</Button>
              </div>
              {proceed.title ? <div className="xs muted mt8">{proceed.title}</div> : null}
            </div>
          </div>
          <div className="mr-exp">
            <span className="ei"><Icon name="archive" size={18} /></span>
            <div className="grow">
              <div className="b">导出迁移包</div>
              <div className="sm muted">含资料和版本历史</div>
            </div>
            <Button size="sm" onClick={exportTransfer}>导出</Button>
          </div>
        </div>
      </div>
      <ReleaseRecords records={records} onManifest={setManifestFor} onDownload={download} onCompare={setCompareFor} />
      <PublishedVersions open={versionsOpen} projectId={pid} projectName={meta?.name || ''} ev={ev}
        onClose={() => setVersionsOpen(false)} onCompare={(target) => setCompareFor(target || {})} />
      <EvaluationDialogs ev={ev} tasks={tasks} />
      <PackageCompare open={!!compareFor} projectId={pid} projectName={meta?.name || ''} initialReleaseId={compareFor?.releaseId || ''}
        initialVersion={compareFor?.version || ''} onClose={() => setCompareFor(null)} />
      <ManifestDrawer rec={manifestFor} onClose={() => setManifestFor(null)} onDownload={download} />
    </ShellP>
  );
}
