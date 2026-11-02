// 运维总览（运维中心，设计稿 ops）：O-01 一次取全——统计卡、建议条、按日用量、告警、运行中的 L4、启动自检摘要、用户活跃、项目占用。
// 项目只给元数据视图（名称、所有者、阶段、占用），不能点进去看内容。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate } from '../../app/router.js';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Avatar, Card, Chip, Icon, Note, Prog, Seg, St, Stat, Table, cx } from '../../ui/index.jsx';
import { Bars } from './charts.jsx';
import {
  RANGES, USAGE_KEYS, adviceView, alertView, autobuildTodayView, avatarIndex, callsText, fmtBytes, firingCount, nameMap,
  rangeSub, relTime, runRow, startupAt, startupItemView, statCards, usageSummary,
} from './overviewModel.js';
import { OpsShell } from './parts.jsx';

function StatRow({ stats }) {
  const s = statCards(stats);
  return (
    <div className="op-k6">
      <Stat label="在线用户" value={s.online.value} icon="users" desc={s.online.desc} />
      <div className="stat">
        <div className="lb"><Icon name="msg" size={14} />活跃会话</div>
        <div className="v">{s.sessions.value}<small>/ {s.sessions.max}</small></div>
        <div className="mt4"><Prog p={s.sessions.pct} thin /></div>
        <div className="d">{s.sessions.desc}</div>
      </div>
      <Stat label="项目" value={s.projects.value} icon="grid" desc={s.projects.desc} />
      <div className="stat l4-border">
        <div className="lb"><Icon name="rocket" size={14} />运行中的 L4</div>
        <div className="v">{s.autobuild.value}</div>
        <div className="d">{s.autobuild.desc}</div>
      </div>
      <Stat label="累计成本" value={s.cost.value} icon="pie" desc={s.cost.desc} />
      <div className="stat" style={s.disk.warn ? { borderColor: 'var(--a100)', background: 'linear-gradient(180deg,#FFFBF2,#fff)' } : undefined}>
        <div className="lb"><Icon name="hdd" size={14} />磁盘可用</div>
        <div className="v" style={s.disk.warn ? { color: 'var(--amber)' } : undefined}>{s.disk.value}<small>{s.disk.small}</small></div>
        <div className="mt4"><Prog p={s.disk.used} thin tone={s.disk.warn ? 'amber' : undefined} /></div>
        <div className="d" style={s.disk.warn ? { color: 'var(--amber)' } : undefined}>{s.disk.desc}</div>
      </div>
    </div>
  );
}

function UsageCard({ usage, range }) {
  const [key, setKey] = useState('calls');
  const u = usageSummary(usage, key, range);
  const n = u.values.length;
  return (
    <Card title="按日用量" sub={rangeSub(range)} icon="bars" extra={<Seg items={USAGE_KEYS} value={key} onChange={setKey} />}>
      <div className="op-us">
        <div><span>{u.totalLabel}（{u.unit}）</span><b>{u.total}</b></div>
        <div><span>日均</span><b>{u.avg}</b></div>
        <div><span>{u.peakLabel}</span><b>{u.peak}</b></div>
        <div><span>{u.vsLabel}</span><b className={u.vsCls}>{u.vs}</b></div>
      </div>
      <Bars values={u.values} w={720} h={128} hi={n - 1} title={(i) => `${u.axis[i]} · ${u.values[i]}`} />
      <div className="op-axis" style={{ width: 720, maxWidth: '100%', justifyContent: n >= 10 ? 'space-between' : 'space-around' }}>
        {u.axis.map((x, i) => <span key={i}>{x}</span>)}
      </div>
    </Card>
  );
}

function AlertsCard({ alerts }) {
  const firing = firingCount(alerts);
  const now = Date.now();
  return (
    <Card
      title="告警" icon="bell" sub={firing ? `${firing} 条触发中` : '没有触发中的告警'}
      extra={<a className="link sm" onClick={() => navigate('/ops-obs', { section: 'rules' })}>全部规则</a>}
    >
      {alerts?.length ? alerts.map((a, i) => {
        const v = alertView(a, now);
        return (
          <div className="op-al" key={`${a.rule_id}-${i}`}>
            <span className="lv" style={{ background: v.dot }} />
            <div className="grow"><div className="b">{v.title}</div><div className="xs muted">{v.detail}</div></div>
            <Chip tone={v.chip[1]} sm>{v.chip[0]}</Chip>
          </div>
        );
      }) : <div className="muted sm" style={{ padding: '10px 0' }}>最近没有告警。</div>}
    </Card>
  );
}

function RunsCard({ runs, today, ownerName }) {
  const rows = (runs || []).map(runRow);
  return (
    <Card title="运行中的 L4" icon="rocket" sub="只显示进度与预算">
      {rows.length ? (
        <Table
          compact
          cols={['项目', '所有者', '已运行', '进度', '预算消耗', '状态']}
          rows={rows.map((r) => [
            <b key="n" title="只能看元数据，内容只归所有者">{r.name}</b>,
            ownerName(r.owner),
            r.elapsed,
            <div key="p" className="row"><Prog p={r.pct} tone="l4" w="90px" /><span className="xs muted">{r.slices}</span></div>,
            <div key="b" className="row"><Prog p={r.budgetPct} w="70px" /><span className="xs muted num">{r.budget}</span></div>,
            r.state.k ? <St key="s" k={r.state.k} sm label={r.state.label} /> : <Chip key="s" tone={r.state.tone} sm>{r.state.label}</Chip>,
          ])}
        />
      ) : <div className="muted sm" style={{ padding: '10px 0' }}>现在没有运行中的自主构建。</div>}
      <div className="row g16 mt12 sm muted">
        {autobuildTodayView(today).map(([label, value, unit]) => (
          <span key={label}>{label} <b className="num" style={{ color: 'var(--ink)' }}>{value}</b>{unit ? ` ${unit}` : ''}</span>
        ))}
        <span className="sp" />
        <span className="xs">维护模式下会在安全点暂停</span>
      </div>
    </Card>
  );
}

function StartupCard({ startup }) {
  return (
    <Card
      title="最近一次启动自检" icon="shieldok" sub={startupAt(startup)}
      extra={<a className="link sm" onClick={() => navigate('/ops-ctl', { section: 'startup' })}>完整报告</a>}
    >
      {startup?.items?.length ? startup.items.map((it, i) => {
        const v = startupItemView(it);
        return (
          <div className="op-ck" key={i}>
            <span className={cx('ci', v.cls)}><Icon name={v.icon} size={12} /></span>
            <div className="grow sm">{v.text}</div>
            <Chip tone={v.tone} sm>{v.label}</Chip>
          </div>
        );
      }) : <div className="muted sm" style={{ padding: '10px 0' }}>还没有自检记录。</div>}
    </Card>
  );
}

function UsersCard({ users, authEnabled }) {
  const now = Date.now();
  return (
    <Card title="用户活跃" icon="users" sub={authEnabled ? '登录已开启' : '本地模式，没有开启登录'}>
      <Table
        compact
        cols={['用户', '状态', '最近活跃', '今日调用']}
        rows={(users || []).map((u) => [
          <div key="u" className="row">
            <Avatar text={(u.display_name || u.username).slice(0, 1)} size="sm" i={avatarIndex(u.username)} />
            <b>{u.display_name || u.username}</b><span className="xs faint mono">{u.username}</span>
          </div>,
          u.online ? <Chip key="o" tone="teal" dot sm>在线</Chip> : <Chip key="o" tone="gray" sm>离线</Chip>,
          <span key="t" className="muted">{relTime(u.last_seen_at, now)}</span>,
          <span key="c" className="num">{callsText(u.calls_today)}</span>,
        ])}
      />
    </Card>
  );
}

function ProjectsCard({ projects, ownerName }) {
  const now = Date.now();
  return (
    <Card title="项目占用" icon="hdd" sub="按磁盘占用排序">
      <Table
        compact
        cols={['项目', '所有者', '阶段', '模式', '磁盘', '最近活动']}
        rows={(projects || []).map((p) => [
          <div key="n" className="row g4" title="只能看元数据，内容只归所有者"><Icon name="lock" size={12} /><b>{p.name}</b></div>,
          ownerName(p.owner),
          <span key="s" className="muted">{p.stage_label || '—'}</span>,
          p.build_mode === 'L4' ? <Chip key="m" tone="indigo" sm>L4</Chip> : <Chip key="m" tone="blue" sm>L2</Chip>,
          <span key="d" className="num">{fmtBytes(p.disk_bytes)}</span>,
          <span key="a" className="muted">{relTime(p.last_activity_at, now, { short: true })}</span>,
        ])}
      />
    </Card>
  );
}

function Overview() {
  const { api, config } = useApp();
  const [range, setRange] = useState('14d');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    api.ops.overview({ range })
      .then((r) => { if (alive) { setData(r); setError(null); } })
      .catch((e) => alive && setError(errorText(e)));
    return () => { alive = false; };
  }, [api, range]);

  const ownerName = nameMap(data?.users);
  return (
    <>
      <PageHead
        eyebrow="运维中心"
        title="运维总览"
        desc="平台运行状态一屏看全。项目只显示元数据，内容只归所有者。"
        actions={<Seg items={RANGES} value={range} onChange={setRange} />}
      />
      {error ? <Note tone="red" className="mb12">总览没取到：{error}</Note> : null}
      {data ? (
        <>
          <StatRow stats={data.stats} />
          {(data.advice || []).map((a, i) => {
            const v = adviceView(a);
            return (
              <div className="mt12" key={i}>
                <Note tone={v.tone} icon={v.icon}>{v.bold ? <b>{v.bold}</b> : null}{v.rest}</Note>
              </div>
            );
          })}
          <div className="op-g21 mt16">
            <UsageCard usage={data.usage_daily} range={range} />
            <AlertsCard alerts={data.alerts} />
          </div>
          <div className="op-g21 mt16">
            <RunsCard runs={data.autobuild_runs} today={data.autobuild_today} ownerName={ownerName} />
            <StartupCard startup={data.startup} />
          </div>
          <div className="op-g12 mt16" style={{ gridTemplateColumns: '1fr 1.5fr' }}>
            <UsersCard users={data.users} authEnabled={config?.auth?.enabled !== false} />
            <ProjectsCard projects={data.projects} ownerName={ownerName} />
          </div>
        </>
      ) : null}
    </>
  );
}

export default function Page() {
  return (
    <OpsShell active="ops">
      <Overview />
    </OpsShell>
  );
}
