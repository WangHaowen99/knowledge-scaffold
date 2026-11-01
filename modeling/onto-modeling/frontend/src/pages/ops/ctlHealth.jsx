// 健康、依赖探测、版本与资源（O-13、O-14；C-03 / C-04 只显示路径）
import { Button, Card, Chip, Icon, KV, Prog, Table, cx } from '../../ui/index.jsx';
import { Missing, ViewChip } from './ctlParts.jsx';
import {
  depView, engineParts, exitText, fmtBytes, fmtBytesPair, liveView, loadText, readyView,
} from './ctlModel.js';

export function HealthSection({ health, startup, perm, busy, onProbe }) {
  const live = liveView(health?.live);
  const ready = readyView(health?.ready);
  return (
    <div data-sec="health">
      <div className="sec-t">
        <Icon name="heart" size={14} />健康<span className="sp" />
        <span className="xs" style={{ fontWeight: 500 }}>健康检查分存活和就绪。就绪会逐个探测模型网关、auth-server、两个网关、磁盘和 git。</span>
        <Button
          size="xs" icon="refresh" disabled={!perm.opsWrite || busy === 'probe'} title={perm.opsWrite ? null : perm.hint('ops.write')}
          onClick={onProbe}
        >
          {busy === 'probe' ? '探测中…' : '立即探测'}
        </Button>
      </div>
      <div className="op-g12">
        <div className={cx('op-big', !live.ok && 'oc-red')}>
          <div className="ico" style={live.ok ? { background: 'var(--b50)', color: 'var(--blue)' } : { background: 'var(--r50)', color: 'var(--red)' }}><Icon name="heart" size={24} /></div>
          <div className="grow">
            <div className="row"><h3>存活</h3><ViewChip v={live.chip} sm={false} /></div>
            <div className="muted sm mt4">{health ? <><span className="mono">{live.lead}</span>{live.text ? ` · ${live.text}` : ''}</> : '暂时读不到'}</div>
          </div>
        </div>
        <div className={cx('op-big', ready.tone === 'amber' && 'oc-amber', ready.tone === 'red' && 'oc-red')}>
          <div className="ico" style={ready.tone === 'red' ? { background: 'var(--r50)', color: 'var(--red)' } : ready.tone ? { background: 'var(--a50)', color: 'var(--amber2)' } : { background: 'var(--b50)', color: 'var(--blue)' }}>
            <Icon name="gauge" size={24} />
          </div>
          <div className="grow">
            <div className="row"><h3>就绪</h3><ViewChip v={ready.chip} sm={false} /></div>
            <div className="muted sm mt4">{health ? <><span className="mono">{ready.lead}</span>{ready.text ? ` · ${ready.text}` : ''}</> : '暂时读不到'}</div>
          </div>
        </div>
      </div>
      <div className="op-g21 mt16">
        <Card title="依赖探测" icon="activity" sub="每 30 秒一次">
          <Table
            compact
            cols={['依赖', '探测内容', '耗时', '状态', '最近异常']}
            rows={(health?.dependencies || []).map((d) => {
              const v = depView(d);
              return [
                <b>{v.name}</b>,
                <span className="muted sm">{v.probe}</span>,
                <span className={cx('num', v.late && 'down')}>{v.latency}</span>,
                <ViewChip v={v.chip} />,
                <span className="xs muted">{v.issue}</span>,
              ];
            })}
          />
          {!health ? <Missing /> : null}
        </Card>
        <RuntimeCard runtime={health?.runtime} startup={startup} />
      </div>
    </div>
  );
}

function RuntimeCard({ runtime, startup }) {
  if (!runtime) return <Card title="版本与资源" icon="cpu"><Missing /></Card>;
  const engines = runtime.engines || [];
  const disk = runtime.disk || {};
  const exit = exitText(runtime.last_exit || startup?.last_exit || null);
  const pairs = [
    ...engines.map((e) => [e.label, (
      <span>
        {engineParts(e).map(([k, v], i) => (
          <span key={i}>{i ? ' · ' : ''}{k ? <>{k} <span className="mono">{v}</span></> : v}</span>
        ))}
        {!engineParts(e).length ? '—' : null}
      </span>
    )]),
    ['磁盘 /data', (
      <>
        <div>{fmtBytes(disk.total)} · 已用 {disk.used_pct ?? '—'}%</div>
        <Prog p={disk.used_pct} tone={disk.warn ? 'amber' : undefined} thin />
      </>
    )],
    ['负载 1/5/15', <><span className="num">{loadText(runtime.load)}</span>{runtime.cpu_cores ? <span className="muted"> · {runtime.cpu_cores} 核</span> : null}</>],
    ['内存', <span className="num">{fmtBytesPair(runtime.memory?.used, runtime.memory?.total)}</span>],
    ['引擎可用性', (
      <span className="row wrap" style={{ gap: 4 }}>
        {engines.map((e) => <Chip key={e.id} tone={e.available ? 'blue' : 'red'} sm>{e.label}{e.available ? '可用' : '不可用'}</Chip>)}
      </span>
    )],
    ['进程', runtime.process ? (
      <><span className="mono">{runtime.process.cmd}</span> · PID {runtime.process.pid}{runtime.process.init_managed ? ' 由 init 托管' : ''}</>
    ) : '—'],
    ['上次退出', exit || '没有记录'],
  ];
  return (
    <Card title="版本与资源" icon="cpu">
      <KV w="92px" pairs={pairs} />
    </Card>
  );
}
