// 发布知识包（设计稿 publish）：门禁链（PB-01；合规检查「去运行」走 FL-05，求解契约回执的结论来自 SV-04）、
// 数据绑定修正（PB-02）、发布与降级发布（PB-03）、导出迁移包（P-09）、发布记录（PB-04）与包清单（PB-06）、下载标准包 / 图模式变体（PB-07）。
// 版本下拉取主线版本（V-01）；项目流 release.updated（EP-19，评测服务拉取）、flow.updated（EP-01）、workspace.changed 实时刷新。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { isLocked } from '../../state/readonly.js';
import { ScopeLockBar, useProjectEvent } from '../plugin/parts.jsx';
import {
  Button, Card, Chip, Drawer, Icon, KV, Modal, Note, Select, St, cx,
} from '../../ui/index.jsx';
import { usePopMenu } from '../versions/popMenu.js';
import { byNewest, fmtStamp, syncMeta } from '../versions/versionsModel.js';
import {
  blockingSummary, canFix, degradeNote, fixOptions, formatHint, formatLabel, gateBlockedText, gateView, issueView, mergeRelease,
  publishBlockedTitle, pullTitle, pullView, releaseGate, releasedText, unboundObjects, versionOptions,
} from './publishModel.js';
import '../../styles/more.css';
import '../../styles/publish.css';

const LOCK_TEXT = {
  running: ['助手正在改其中的对象，确认生效或撤回后才能改数据来源', '可以先发布已有的版本；锁住的对象改不了数据来源。'],
  pending: ['助手的修改还没确认，确认生效或撤回后才能改数据来源', '确认生效后会生成新版本，门禁链按新版本重新检查。'],
};

function GateChain({ check, onAction, busy }) {
  const sum = blockingSummary(check);
  return (
    <>
      <div className="row mb8">
        <span className="sec-t" style={{ margin: 0 }}><Icon name="shield" size={14} />门禁链</span>
        {check ? <span className={cx('chip sm', sum.tone)}>{sum.text}</span> : null}
        <span className="xs muted">平台检查 + 已启用能力带来的检查，按顺序执行</span>
      </div>
      <div className="mr-gates">
        {(check?.gates || []).map((g, i) => {
          const v = gateView(g);
          return [
            i ? <span key={`a${g.id}`} className="mr-garr"><Icon name="chev" size={14} /></span> : null,
            <div key={g.id} className={cx('mr-gate', v.cls)} title={(g.findings || []).map((f) => f.message).join('\n') || undefined}>
              <div className="row">
                <span className="gi"><Icon name={v.icon} size={14} /></span>
                <div className="t grow">{g.title}</div>
                <St k={v.chip.k} sm label={v.chip.label} />
              </div>
              <div className="o mt8">{g.source?.label}</div>
              <div className="d" style={{ marginTop: 4 }}>{g.summary}</div>
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
  if (!issues.length) {
    return (
      <Card title="数据绑定检查" icon="db" sub="对象「# 数据来源」声明的表要真实存在、列要对得上" extra={<Chip tone="teal" sm icon="check">全部对上</Chip>}>
        <Note tone="gray" icon="check">{check.binding?.bound_objects ?? 0} 个对象的数据来源都对得上（{check.binding?.table_count ?? 0} 张表可核对）。确实不落库的对象写的是「无」。</Note>
        <div className="row mt12"><span className="sp" /><Button size="sm" kind="soft" icon="refresh" disabled={checking} onClick={onRecheck}>重新检查</Button></div>
      </Card>
    );
  }
  return (
    <Card title="数据绑定检查" icon="db" sub="对象「# 数据来源」声明的表要真实存在、列要对得上" extra={<Chip tone="red" sm>{issues.length} 个对象</Chip>}>
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
                <tr key={x.object.id} title={locked ? '这个对象在助手的写入范围内，确认生效或撤回后才能改' : undefined}>
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
      <div className="note gray mt12">
        <Icon name="info" />
        <div>
          修正口径：首行只写现有数据表名，逐字相同；确实不落库的对象填「无」；不要臆造表名。<br />
          有数据库证据时，数据来源可以直接写锚点，如 <span className="anchor">db:erp-prod/erp.store</span>，表名列名从快照解析 —— 门店已经这样写，绑定通过。
        </div>
      </div>
      <div className="row mt12">
        {check.degradable ? <Button size="sm" icon="alert" onClick={onDegrade}>忽略并降级发布</Button> : null}
        <span className="xs muted">只放宽数据绑定，求解契约问题必须先修</span>
        <span className="sp" />
        <Button size="sm" kind="soft" icon="refresh" disabled={checking} onClick={onRecheck}>修完重新检查</Button>
      </div>
    </Card>
  );
}

function ReleaseRecords({ records, onManifest, onDownload }) {
  return (
    <Card className="mt16" title="发布记录" icon="history" extra={<span className="xs muted">评测服务按包清单拉取</span>}>
      {records && !records.length ? (
        <div className="sm muted">还没有发布过。门禁链全部通过后，从上面的「发布」出包，包清单会一起交给评测服务。</div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl compact">
            <thead><tr><th>版本</th><th>时间</th><th>格式</th><th>门禁</th><th>包清单</th><th>评测服务</th><th style={{ width: 130 }} /></tr></thead>
            <tbody>
              {(records || []).map((r) => {
                const g = releaseGate(r);
                const p = pullView(r);
                return (
                  <tr key={r.version}>
                    <td><b title={`包版本 ${r.version} · 标签 ${r.tag}`}>{r.version_label}</b></td>
                    <td className="num">{fmtStamp(r.at)}</td>
                    <td>{r.format_label}</td>
                    <td><Chip tone={g.chip[1]} sm>{g.chip[0]}</Chip>{g.note ? <> <span className="xs muted">{g.note}</span></> : null}</td>
                    <td><span className="xs pb-link" onClick={() => onManifest(r)} title="查看包清单">{r.manifest_attached ? '已附包清单' : '没有留存包'}</span></td>
                    <td><Chip tone={p.tone} sm icon={p.icon} title={pullTitle(r) || undefined}>{p.text}</Chip></td>
                    <td>
                      <span className="row" style={{ justifyContent: 'flex-end', gap: 12 }}>
                        <span className="xs pb-link row g4" title="下载标准包" onClick={() => onDownload(r, 'standard')}><Icon name="download" size={12} />标准包</span>
                        <span className="xs pb-link" title="图模式变体：给评测服务导入图数据库用，同一提交渲染" onClick={() => onDownload(r, 'graph')}>图模式</span>
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
              ['源提交', <span className="mono">{String(m.source_commit).slice(0, 8)}</span>],
              ['发布时间', fmtStamp(m.released_at)],
              ['能力版本', (m.packs || []).length ? m.packs.map((p) => `${p.name}@${p.version}`).join('、') : '—'],
              ['流程摘要', m.flow_hash ? <span className="mono">{m.flow_hash}</span> : '—'],
              ['求解契约', m.solver ? `${m.solver.revision}${m.solver.receipt ? ` · 回执${m.solver.receipt.status === 'passed' ? '已通过' : '未通过'}` : ' · 没有回执'}` : '没有启用'],
              ['降级', m.degraded ? `${m.degraded.unbound_objects.join('、')} 不绑定数据表` : '没有降级'],
              ['大小', `${formatBytes(m.size)} · sha256 ${String(m.sha256).slice(0, 12)}…`],
            ]}
          />
          <div className="sec-t mt16"><Icon name="file" size={14} />包里的文件<span className="sp" /><span className="faint">{m.files.length}</span></div>
          <div className="pb-files">
            {m.files.map((f) => (
              <div key={f.path} className="row"><span className="grow ell mono">{f.path}</span><span className="xs faint num">{formatBytes(f.size)}</span></div>
            ))}
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
  const n = degradeNote(check);
  return (
    <Modal
      open={open}
      onClose={onClose}
      icon="alert"
      title="忽略并降级发布？"
      sub="这些对象发布后不绑定数据表，发布记录会注明降级发布"
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={busy} onClick={onConfirm}>降级发布</Button>
        </>
      )}
    >
      <div className="row wrap g4">{unboundObjects(check).map((o) => <Chip key={o.id} tone="line" sm icon="cube">{o.name}</Chip>)}</div>
      <div className="sm mt12">消费端问到这些对象时无法查库，只能按知识回答。</div>
      <Note tone={n.tone === 'amber' ? 'amber' : 'gray'}>{n.text}</Note>
    </Modal>
  );
}

export default function PublishPage() {
  const { api, services } = useApp();
  const { pid, meta, scope, setMeta } = useProject();
  const [versions, setVersions] = useState(null);
  const [commit, setCommit] = useState(null); // null 表示跟随主线最新版本
  const [check, setCheck] = useState(null);
  const [checking, setChecking] = useState(false);
  const [records, setRecords] = useState(null);
  const [fixing, setFixing] = useState(null);
  const [degOpen, setDegOpen] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [manifestFor, setManifestFor] = useState(null);
  const [starting, setStarting] = useState(false);
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
      if (refresh) toast(`已重新检查，用时 ${Math.max(1, Math.round((c.duration_ms || 0) / 1000))} 秒`, 'refresh');
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
  const blockedTitle = publishBlockedTitle(check);

  const publish = async (degrade = false) => {
    setPublishing(true);
    try {
      const r = await api.publishRelease(pid, { commit: check?.commit, ...(degrade ? { degrade: true } : {}) });
      const fresh = !(records || []).some((x) => x.version === r.version);
      toast(releasedText(r, fresh), 'upload');
      setRecords((list) => mergeRelease(list, r));
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
        desc={`${pkg} · 主线 ${mainLabel} · 发布从版本标签出，过完门禁链才能发。`}
        actions={(
          <>
            <Button icon="refresh" disabled={checking} onClick={() => runCheck({ refresh: true })}>重新检查</Button>
            <Button kind="primary" icon="upload" disabled={!check || !!blockedTitle || publishing} title={blockedTitle || undefined} onClick={() => publish(false)}>发布为知识包</Button>
          </>
        )}
      />
      <GateChain check={check} onAction={gateAction} busy={starting} />
      {check?.uncommitted?.count ? (
        <Note tone="amber" className="mb12">有 {check.uncommitted.count} 处改动还没成为版本，发布不会带上它们；确认生效后再发布。</Note>
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
              <div className="b">发布为知识包（供消费端导入）</div>
              <div className="xs muted" style={{ margin: '2px 0 4px' }}>{formatHint(check)}</div>
              <div className="sm muted mb8">附包清单，供消费端导入</div>
              <div className="row">
                <Chip tone="blue" icon="cpu">{formatLabel(check)}</Chip>
                <Select value={selCommit} options={versionOptions(versions)} onChange={(c) => setCommit(c === head?.commit ? null : c)} icon="tag" w="104px" className="pb-sel" />
                <span className="sp" />
                <Button size="sm" kind="primary" disabled={!check || !!blockedTitle || publishing} title={blockedTitle ? '门禁未通过' : undefined} onClick={() => publish(false)}>发布</Button>
              </div>
            </div>
          </div>
          <div className="mr-exp">
            <span className="ei"><Icon name="archive" size={18} /></span>
            <div className="grow">
              <div className="b">导出迁移包（完整工作区）</div>
              <div className="sm muted">含资料和版本历史；不含数据源凭据，导入后重新录入</div>
            </div>
            <Button size="sm" onClick={exportTransfer}>导出</Button>
          </div>
        </div>
      </div>
      <ReleaseRecords records={records} onManifest={setManifestFor} onDownload={download} />
      <DegradeModal open={degOpen} check={check} busy={publishing} onClose={() => setDegOpen(false)} onConfirm={() => publish(true)} />
      <ManifestDrawer rec={manifestFor} onClose={() => setManifestFor(null)} onDownload={download} />
    </ShellP>
  );
}
