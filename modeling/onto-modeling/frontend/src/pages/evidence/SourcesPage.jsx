// 业务系统数据源（系统逆向建模插件的资料子页，设计稿 sources）：
// 登记的数据源（EV-01）、授权本项目（EV-11）、测试连接（EV-08）、访问审计（EV-09）、本项目在用（EV-12）、
// 登记抽屉（管理员 EV-02 / EV-04～EV-07，普通用户 EV-03）、删除登记（EV-05）；缺提供方去插件中心（PL-01）。
// 上传程序的自助类数据源（F13）：列表里显示程序文件名和大小；登记人和管理员的 ⋯ 里有「上传新版本」、编辑、删除，没有测试连接和凭据。
import { useCallback, useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { Button, Card, Chip, Icon, Modal, Note, Seg, Table, Toggle, cx } from '../../ui/index.jsx';
import {
  ENV, JOB_STATE, KIND_ICON, canManageSource, connText, credState, fmtDuration, fmtNum, fmtWhen, isArtifactSource, monthText, providerKind, scopeWindowText,
} from './evidenceModel.js';
import { EvdPlg, EvidenceGate, RowMenu, useIsAdmin } from './parts.jsx';
import { SourceWizard } from './SourceWizard.jsx';

const FILTERS = [['all', '全部'], ['authorized', '已授权'], ['production', '生产环境']];
const OP_LABEL = { snapshot: '快照', discover: '发现', sample: '补采样本', test: '测试连接' };

// 只看写没写过（secret_fields），引用只有管理员和登记人看得到
function CredCell({ s }) {
  const c = credState(s);
  if (!c.needed && !c.ref) return <span className="faint sm">无需凭据</span>;
  if (!c.set) return <span className="sm" style={{ color: 'var(--amber2)' }}>凭据未写入</span>;
  return <span className="row g4"><Icon name="lock" size={12} />{c.ref ? <span className="anchor">{c.ref}</span> : <span className="sm">已设置</span>}</span>;
}

function LastJob({ job }) {
  if (!job) return <span className="faint sm">还没采集过</span>;
  const [t, tone, icon] = JOB_STATE[job.state] || [job.state, 'gray'];
  return (
    <>
      <div className="sm">{fmtWhen(job.at)}</div>
      {job.state === 'running' ? <span className="chip st-run sm"><span className="spin" />{t}</span> : <Chip tone={tone} sm icon={icon}>{t}</Chip>}
    </>
  );
}

// 访问审计（EV-09）：管理员看全部，项目所有者只看本项目的访问
function AuditModal({ source, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [rows, setRows] = useState(null);
  const [cursor, setCursor] = useState(null);
  const [more, setMore] = useState(false);
  const fetchPage = useCallback((after) => api.evidenceAccessLog(source.id, { project_id: pid, cursor: after || undefined }), [api, pid, source]);
  useEffect(() => {
    if (!source) return;
    setRows(null);
    setCursor(null);
    fetchPage().then((r) => { setRows(r.items || []); setCursor(r.next_cursor || null); }).catch((e) => { toast(errorText(e), 'alert'); setRows([]); });
  }, [source, fetchPage]);
  // 数据库快照的访问记录按语句逐条记，一页放不下时往后翻（EV-09 next_cursor）
  const loadMore = async () => {
    setMore(true);
    try {
      const r = await fetchPage(cursor);
      setRows((x) => [...(x || []), ...(r.items || [])]);
      setCursor(r.next_cursor || null);
    } catch (e) { toast(errorText(e), 'alert'); } finally { setMore(false); }
  };
  return (
    <Modal
      open={!!source}
      onClose={onClose}
      icon="list"
      title={`访问审计 · ${source?.display_name || ''}`}
      w={760}
      foot={<>{cursor ? <Button disabled={more} onClick={loadMore}>加载更多</Button> : null}<Button onClick={onClose}>关闭</Button></>}
    >
      {rows === null ? <div className="sm muted">正在读取…</div> : rows.length ? (
        <Table
          compact
          cols={['时间', '任务', '操作', '语句摘要', '行数', '字节', '耗时']}
          rows={rows.map((r) => [
            <span className="sm muted" style={{ whiteSpace: 'nowrap' }}>{fmtWhen(r.at)}</span>,
            <span className="mono" style={{ whiteSpace: 'nowrap' }}>{r.job_id || '—'}</span>,
            <span className="sm" style={{ whiteSpace: 'nowrap' }}>{OP_LABEL[r.operation] || r.operation}</span>,
            <span className="mono sm" style={{ color: 'var(--ink2)' }}>{r.statement_digest || '—'}</span>,
            <span className="sm num">{r.rows == null ? '—' : fmtNum(r.rows)}</span>,
            <span className="sm num muted" style={{ whiteSpace: 'nowrap' }}>{r.bytes ? formatBytes(r.bytes) : '—'}</span>,
            <span className="sm num muted" style={{ whiteSpace: 'nowrap' }}>{fmtDuration(r.duration_ms)}</span>,
          ])}
        />
      ) : <div className="sm muted">还没有访问记录。</div>}
    </Modal>
  );
}

function SourcesBody() {
  const { api, user } = useApp();
  const { pid } = useProject();
  const admin = useIsAdmin();
  const [filter, setFilter] = useState('all');
  const [all, setAll] = useState(null);
  const [rows, setRows] = useState(null);
  const [summary, setSummary] = useState(null);
  const [providers, setProviders] = useState(null);
  const [wizard, setWizard] = useState(null);
  const [audit, setAudit] = useState(null);
  const [del, setDel] = useState(null);
  const [testing, setTesting] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api.evidenceSources({ project_id: pid, filter: 'all' });
      setAll(r);
      setRows(filter === 'all' ? r.items : (await api.evidenceSources({ project_id: pid, filter })).items);
    } catch (e) { toast(errorText(e), 'alert'); }
    api.evidenceSummary(pid).then(setSummary).catch(() => {});
  }, [api, pid, filter]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { api.evidenceProviders().then(setProviders).catch(() => {}); }, [api]);

  const isArtifact = (s) => isArtifactSource(s, providers?.items);
  const authorizedList = (all?.items || []).filter((s) => s.authorized_for_project);
  // 上传程序的数据源没有连接可测
  const testable = authorizedList.filter((s) => !isArtifact(s));
  const selfServiceOn = (providers?.items || []).some((p) => p.self_service && p.installed);

  const grant = async (s, on) => {
    try {
      await api.setEvidenceGrant(pid, s.id, { authorized: on });
      toast(on ? `已授权「${s.display_name}」给本项目使用` : `已取消「${s.display_name}」的授权`, on ? 'check' : 'ban');
      load();
    } catch (e) { toast(errorText(e), 'alert'); }
  };
  const testOne = async (s) => {
    try {
      const r = await api.testEvidenceSource(s.id, { project_id: pid });
      const bad = r.checks.find((c) => !c.ok);
      toast(r.ok ? `「${s.display_name}」连接正常` : `「${s.display_name}」测试没通过：${bad?.detail || '请联系管理员'}`, r.ok ? 'check' : 'alert');
    } catch (e) { toast(errorText(e), 'alert'); }
  };
  const testAuthorized = async () => {
    setTesting(true);
    try {
      const res = await Promise.all(testable.map((s) => api.testEvidenceSource(s.id, { project_id: pid }).then((r) => [s, r]).catch(() => [s, null])));
      const bad = res.filter(([, r]) => !r?.ok);
      toast(bad.length ? `${bad.map(([s]) => s.display_name).join('、')} 连接没通过` : `已授权的 ${res.length} 个数据源连接正常`, bad.length ? 'alert' : 'activity');
    } finally {
      setTesting(false);
    }
  };
  const remove = async () => {
    try {
      await api.deleteEvidenceSource(del.id);
      toast(`已删除登记 ${del.id}`, 'trash');
      setDel(null);
      load();
    } catch (e) { toast(errorText(e), 'alert'); }
  };

  const menu = (s) => {
    const on = !!s.authorized_for_project;
    const ownerOnly = !admin && !on;
    const artifact = isArtifact(s);
    const items = [
      on ? { label: '取消授权', icon: 'ban', onClick: () => grant(s, false) } : { label: '授权本项目使用', icon: 'check', onClick: () => grant(s, true) },
      artifact ? null : { label: '测试连接', icon: 'activity', disabled: ownerOnly, title: ownerOnly ? '只能测试已授权给本项目的数据源' : undefined, onClick: () => testOne(s) },
      { label: '查看访问审计', icon: 'list', disabled: ownerOnly, title: ownerOnly ? '授权给本项目后能看本项目的访问记录' : undefined, onClick: () => setAudit(s) },
    ].filter(Boolean);
    if (artifact) {
      if (canManageSource(s, user, admin)) {
        items.push('-',
          { label: '上传新版本', icon: 'upload', onClick: () => setWizard({ mode: 'edit', source: s, focus: 'artifact' }) },
          { label: '编辑登记', icon: 'edit', onClick: () => setWizard({ mode: 'edit', source: s }) },
          { label: '删除登记', icon: 'trash', danger: true, onClick: () => setDel(s) });
      }
    } else if (admin) {
      items.push('-',
        { label: '编辑登记', icon: 'edit', onClick: () => setWizard({ mode: 'edit', source: s }) },
        { label: '轮换凭据', icon: 'key', onClick: () => setWizard({ mode: 'edit', source: s, focus: 'cred' }) },
        { label: '删除登记', icon: 'trash', danger: true, onClick: () => setDel(s) });
    }
    return items;
  };

  const kindIcon = (s) => KIND_ICON[providerKind(s.provider, providers?.items)] || 'db';
  const tableRows = (rows || []).map((s) => {
    const on = !!s.authorized_for_project;
    const [envT, envTone] = ENV[s.environment] || ['—', 'gray'];
    return [
      <div className="row">
        <span style={{ width: 30, height: 30, borderRadius: 9, display: 'grid', placeItems: 'center', background: 'var(--b50)', color: 'var(--blue)', flex: 'none' }}><Icon name={kindIcon(s)} size={14} /></span>
        <div><div className="b">{s.display_name}</div><div className="mono faint">{s.id}</div></div>
      </div>,
      <span className="tag">{s.provider?.id}@{s.provider?.version}</span>,
      <Chip tone={envTone} dot>{envT}</Chip>,
      <span className="mono sm" style={{ color: 'var(--ink2)' }}>{connText(s)}</span>,
      <CredCell s={s} />,
      <span className="sm">{scopeWindowText(s)}</span>,
      <LastJob job={s.last_job} />,
      <Toggle on={on} onChange={(v) => grant(s, v)} title={on ? '取消本项目的授权' : '授权本项目使用'} />,
      <RowMenu items={menu(s)} />,
    ];
  });

  const used = summary?.authorized_sources || [];
  const usedName = used.length === 1 ? used[0].display_name : used.length ? `${used.length} 个数据源` : '—';
  const usedNote = used.length === 1
    ? (used[0].environment === 'production' ? '生产环境' : '测试环境')
    : used.length ? used.map((s) => s.display_name).join('、') : '还没有授权的数据源';
  const month = summary?.jobs_this_month;

  return (
    <>
      <PageHead
        title={<>数据源 <EvdPlg /></>}
        actions={(
          <>
            <Button icon="activity" disabled={testing || !testable.length} title={authorizedList.length ? undefined : '本项目还没有授权的数据源'} onClick={testAuthorized}>测试已授权的连接</Button>
            <Button kind="primary" icon="plug" onClick={() => setWizard({ mode: 'new' })}>{admin || selfServiceOn ? '登记新数据源' : '申请登记新数据源'}</Button>
          </>
        )}
      />
      <Card
        title="已登记的数据源"
        sub={`共 ${all?.total ?? '…'} 个 · 本项目授权 ${all?.authorized_count ?? '…'} 个`}
        icon="db"
        extra={<Seg items={FILTERS} value={filter} onChange={setFilter} />}
      >
        {rows === null ? <div className="sm muted">正在读取…</div> : rows.length ? (
          <Table compact cols={['数据源', '连接器', '环境', '连接参数', '凭据引用', '范围与访问窗口', '最近采集', '授权本项目使用', '']} rows={tableRows} />
        ) : <div className="sm muted" style={{ padding: '18px 4px' }}>{filter === 'all' ? '还没有登记的数据源。' : '没有符合筛选的数据源。'}</div>}
      </Card>
      <div className="mt16">
        <Card
          title="本项目在用"
          icon="check"
          extra={(
            <>
              <Button size="sm" icon="scan" disabled={!used.length} onClick={() => navigate(pagePath('snapshots', pid))}>发起采集</Button>
              <Button size="sm" kind="soft" icon="search" onClick={() => navigate(pagePath('schema', pid))}>浏览证据</Button>
            </>
          )}
        >
          <div className="evd-useb">
            <div><span className="xs muted">数据源</span><b style={{ fontSize: 15 }}>{usedName}</b><span className="xs muted">{usedNote}</span></div>
            <div><span className="xs muted">快照</span><b>{summary?.snapshots?.count ?? '—'}</b><span className="xs muted">{summary?.snapshots?.last_at ? `最近 ${fmtWhen(summary.snapshots.last_at)}` : '还没有快照'}</span></div>
            <div><span className="xs muted">被引用出处</span><b>{summary?.referenced_anchors ?? '—'}</b></div>
            <div><span className="xs muted">本月采集</span><b>{month?.total ?? '—'}</b><span className="xs muted">{monthText(month) || '本月还没有采集'}</span></div>
          </div>
        </Card>
      </div>
      <div className="row mt16 g4">
        <Icon name="puzzle" size={14} />
        <a className="link" onClick={() => navigate(pagePath('plugins'), { category: 'capability' })}>安装更多数据源类型</a>
      </div>

      <SourceWizard
        open={!!wizard}
        mode={wizard?.mode}
        source={wizard?.source}
        focus={wizard?.focus}
        providers={providers}
        admin={admin}
        onClose={() => setWizard(null)}
        onSaved={(o) => { load(); if (!o?.silent) setWizard(null); }}
      />
      <AuditModal source={audit} onClose={() => setAudit(null)} />
      <Modal
        open={!!del}
        onClose={() => setDel(null)}
        danger
        icon="trash"
        title={`删除登记 ${del?.id || ''}？`}
        sub="删除后所有项目都不能再从它采集。"
        foot={<><Button onClick={() => setDel(null)}>取消</Button><Button kind="danger" className="fill" icon="trash" onClick={remove}>删除登记</Button></>}
      >
        <Note tone="amber">已有快照保留，出处照常解析。</Note>
      </Modal>
    </>
  );
}

export default function SourcesPage() {
  return (
    <ShellP active="sources">
      <EvidenceGate page="sources"><SourcesBody /></EvidenceGate>
    </ShellP>
  );
}
