// 发布知识包（设计稿 publish）：门禁链（PB-01；合规检查「去运行」走 FL-05）、
// 数据绑定修正（PB-02）、发布与降级发布（PB-03，经知识包 Hub，求解包要确认未验证）、导出迁移包（P-09）、发布记录（PB-04）与包清单（PB-06）、下载标准包 / 图模式变体（PB-07）。
// 版本下拉取当前分支上的版本（V-01）；项目流 release.updated（EP-19）、flow.updated（EP-01）、workspace.changed 实时刷新。
// 发布记录带评测（ES-02，ADR-0092）与版本对比（PC 组，ADR-0095）。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { evidenceInProject } from '../../state/evidence.js';
import { isLocked } from '../../state/readonly.js';
import { ScopeLockBar, useCommandTasks, useProjectEvent } from '../plugin/parts.jsx';
import {
  EvaluationCell, EvaluationDialogs, EvaluationNotices, FeedbackCell, LegacyEvaluations, SolverValidationCell, useReleaseEvaluations,
} from '../evaluation/ReleaseEvaluation.jsx';
import PackageCompare from './PackageCompare.jsx';
import { fileGroups } from '../versions/versionsModel.js';

const listed = (groups) => groups.reduce((n, g) => n + g.items.length, 0);
import {
  Button, Card, Check, Chip, Drawer, Icon, KV, Modal, Note, Select, St, cx,
} from '../../ui/index.jsx';
import { usePopMenu } from '../versions/popMenu.js';
import { byNewest, fmtStamp, syncMeta } from '../versions/versionsModel.js';
import {
  blockingSummary, canFix, degradeNote, fixOptions, formatLabel, gateBlockedText, gateView, issueView, mergeRelease,
  needsSolverAck, publishBlockedTitle, releaseGate, releasedText, unboundObjects, versionOptions, visibleGates,
} from './publishModel.js';
import '../../styles/more.css';
import '../../styles/publish.css';

const LOCK_TEXT = {
  running: ['助手正在改其中的对象，这一轮结束后才能改数据来源'],
  pending: ['其中的对象和助手这一轮的修改有冲突，选好保留哪边后才能改数据来源'],
};

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

// 「改为 xxx」下拉：选中即提交（PB-02 table）；表格外层会裁切，菜单用 fixed 定位
function FixSelect({ issue, tables, disabled, onPick }) {
  const menu = usePopMenu({ width: 280, height: 300, align: 'left' });
  const { placeholder, options } = fixOptions(issue, tables);
  return (
    <div ref={menu.ref} className="grow" style={{ minWidth: 0 }}>
      <div className="inp sel" role="button" style={{ cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.6 : 1 }} onClick={(e) => !disabled && menu.toggle(e)}>
        <span className="grow ell">{placeholder}</span>
      </div>
      {menu.at ? (
        <div className="menu pb-menu" style={menu.at}>
          {options.map((o) => (
            <div key={o.value} className="mi" onClick={() => { menu.close(); onPick(o.value); }}>
              <Icon name="table" size={14} /><span className="grow ell mono">{o.label}</span>{o.hint ? <span className="k">{o.hint}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function BindingCard({ check, scope, fixing, onFix, onDegrade, onRecheck, checking }) {
  const { pid } = useProject();
  const issues = check.binding?.issues || [];
  const lockedAny = issues.some((x) => isLocked(scope, x.object));
  // 除数据绑定外还有门禁挡着时，降级也发不了：按钮置灰，悬停写是哪一关（R7）
  const degrade = degradeNote(check);
  if (!issues.length) {
    return (
      <Card title="数据绑定检查" icon="db" extra={<Chip tone="teal" sm icon="check">全部对上</Chip>}>
        <Note tone="gray" icon="check">{check.binding?.bound_objects ?? 0} 个对象、{check.binding?.table_count ?? 0} 张表已核对</Note>
        <div className="row mt12"><span className="sp" /><Button size="sm" kind="soft" icon="refresh" disabled={checking} onClick={onRecheck}>重新检查</Button></div>
      </Card>
    );
  }
  return (
    <Card title="数据绑定检查" icon="db" extra={<Chip tone="red" sm>{issues.length} 个对象</Chip>}>
      {lockedAny ? <ScopeLockBar lock={scope} targets={issues.map((x) => x.object)} text={LOCK_TEXT} /> : null}
      <div className="tbl-wrap">
        <table className="tbl compact">
          <thead>
            <tr><th style={{ width: 110 }}>对象</th><th style={{ width: 130 }}>声明的来源</th><th>问题</th><th style={{ width: 250 }}>修正</th></tr>
          </thead>
          <tbody>
            {issues.map((x) => {
              const v = issueView(x);
              const locked = isLocked(scope, x.object);
              const busy = fixing === x.object.key;
              const dis = locked || !!fixing;
              const soft = !canFix(x, 'set_table');
              return (
                <tr key={x.object.id} title={locked ? '助手在改，暂不能修改' : undefined}>
                  <td><b>{x.object.name}</b></td>
                  <td><span className="mono">{x.declared || '—'}</span></td>
                  <td><Chip tone={v.chip[1]} sm>{v.chip[0]}</Chip><div className="xs muted mt4">{v.note}</div></td>
                  <td>
                    <div className="row g4">
                      {canFix(x, 'set_table') ? <FixSelect issue={x} tables={check.binding.tables} disabled={dis} onPick={(t) => onFix(x, 'table', t)} /> : null}
                      {canFix(x, 'set_none') ? <Button size="xs" kind={soft ? 'soft' : undefined} disabled={dis} onClick={() => onFix(x, 'none')}>{busy ? '正在改…' : '改为「无」'}</Button> : null}
                      {canFix(x, 'mark_schema_material') ? <Button size="xs" onClick={() => navigate(pagePath('resources', pid))}>标注 Schema 资料</Button> : null}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="row mt12">
        {check.degradable ? (
          <Button size="sm" icon="alert" disabled={degrade.blocked} title={degrade.text || undefined} onClick={onDegrade}>忽略并降级发布</Button>
        ) : null}
        <span className="sp" />
        <Button size="sm" kind="soft" icon="refresh" disabled={checking} onClick={onRecheck}>修完重新检查</Button>
      </div>
    </Card>
  );
}

function ReleaseRecords({ records, ev, onManifest, onDownload, onCompare }) {
  // 评测看 Hub 版本：行上带 release_id、ontology_id、package_digest
  const rows = (records || []).map((r) => ({ ...(r.hub || {}), ...r }));
  const solver = ev.hasSolver(rows);
  return (
    <Card className="mt16" title="发布记录" icon="history">
      <EvaluationNotices ev={ev} />
      {records && !records.length ? (
        <div className="sm muted">还没有发布过</div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl compact">
            <thead>
              <tr>
                <th>版本</th><th>时间</th><th>格式</th><th>门禁</th><th>包清单</th>
                {ev.enabled ? <th>评测</th> : null}
                {solver ? <th>求解验证</th> : null}
                {ev.hasFeedback ? <th>用户反馈</th> : null}
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
                    {ev.enabled ? <td><EvaluationCell ev={ev} release={r} /></td> : null}
                    {solver ? <td><SolverValidationCell ev={ev} release={r} /></td> : null}
                    {ev.hasFeedback ? <td><FeedbackCell ev={ev} release={r} /></td> : null}
                    <td>
                      <span className="row" style={{ justifyContent: 'flex-end', gap: 12 }}>
                        <span className="xs pb-link" onClick={() => onCompare(r.version)}>对比</span>
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

function DegradeModal({ open, check, busy, onClose, onConfirm }) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      icon="alert"
      title="忽略并降级发布？"
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={busy} onClick={onConfirm}>降级发布</Button>
        </>
      )}
    >
      <div className="row wrap g4">{unboundObjects(check).map((o) => <Chip key={o.id} tone="line" sm icon="cube">{o.name}</Chip>)}</div>
      <div className="sm mt12">这些对象发布后不能查库。</div>
    </Modal>
  );
}

export default function PublishPage() {
  const { api, services } = useApp();
  const { pid, meta, scope, setMeta } = useProject();
  const [versions, setVersions] = useState(null);
  const [commit, setCommit] = useState(null); // null 表示跟随当前版本
  const [check, setCheck] = useState(null);
  const [checking, setChecking] = useState(false);
  const [records, setRecords] = useState(null);
  const [fixing, setFixing] = useState(null);
  const [degOpen, setDegOpen] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [ack, setAck] = useState(false); // 求解能力未验证，仍发布
  const [manifestFor, setManifestFor] = useState(null);
  const [starting, setStarting] = useState(false);
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
  const ackNeeded = needsSolverAck(check);
  const blockedTitle = publishBlockedTitle(check) || (ackNeeded && !ack ? '先确认求解能力未验证' : '');

  const publish = async (degrade = false) => {
    setPublishing(true);
    try {
      const r = await api.publishRelease(pid, {
        commit: check?.commit, ...(degrade ? { degrade: true } : {}), ...(ackNeeded && ack ? { solver_unverified_acknowledged: true } : {}),
      });
      toast(releasedText(r), 'upload');
      if (r.local_record_pending) loadRecords();
      else setRecords((list) => mergeRelease(list, r));
      setAck(false);
      setDegOpen(false);
      loadVersions();
      runCheck();
    } catch (e) {
      toast(e.code === 'gate_blocked' ? gateBlockedText(e.details) : errorText(e), 'alert');
      if (e.code === 'gate_blocked') setDegOpen(false);
    } finally {
      setPublishing(false);
    }
  };
  const fix = async (issue, kind, value) => {
    setFixing(issue.object.key);
    try {
      const r = await api.setObjectDataSource(pid, issue.object.key, { kind, ...(value ? { value } : {}), base_revision: issue.revision });
      toast(`已把「${issue.object.name}」的数据来源改为${kind === 'none' ? '「无」' : ` ${value}`}，保存为版本 ${r.version_label}`, 'check');
      setCommit(null);
      loadVersions();
      await runCheck();
    } catch (e) {
      toast(errorText(e), e.isLocked ? 'lock' : 'alert');
      if (e.isConflict) runCheck();
    } finally {
      setFixing(null);
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
        actions={<><LegacyEvaluations pid={pid} /><Button icon="refresh" disabled={checking} onClick={() => runCheck({ refresh: true })}>重新检查</Button></>}
      />
      <GateChain check={check} onAction={gateAction} busy={starting} />
      {check?.uncommitted?.count ? (
        <Note tone="amber" className="mb12">{check.uncommitted.count} 处改动未确认，不会随这次发布</Note>
      ) : null}
      <div className="mr-2c" style={{ gridTemplateColumns: 'minmax(0,1fr) 400px' }}>
        {check ? (
          <BindingCard
            check={check}
            scope={scope}
            fixing={fixing}
            checking={checking}
            onFix={fix}
            onDegrade={() => setDegOpen(true)}
            onRecheck={() => runCheck({ refresh: true })}
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
                <Button size="sm" kind="primary" disabled={!check || !!blockedTitle || publishing} title={blockedTitle || undefined} onClick={() => publish(false)}>发布</Button>
              </div>
              {ackNeeded ? <div className="mt8"><Check on={ack} onChange={setAck} label="求解能力未经端到端验证，仍发布" disabled={publishing} /></div> : null}
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
      <ReleaseRecords records={records} ev={ev} onManifest={setManifestFor} onDownload={download} onCompare={setCompareFor} />
      <EvaluationDialogs ev={ev} tasks={tasks} />
      <PackageCompare open={!!compareFor} projectId={pid} initialVersion={compareFor || ''} onClose={() => setCompareFor(null)} />
      <DegradeModal open={degOpen} check={check} busy={publishing} onClose={() => setDegOpen(false)} onConfirm={() => publish(true)} />
      <ManifestDrawer rec={manifestFor} onClose={() => setManifestFor(null)} onDownload={download} />
    </ShellP>
  );
}
