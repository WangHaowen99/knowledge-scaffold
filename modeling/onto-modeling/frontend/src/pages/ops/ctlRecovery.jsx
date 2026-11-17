// 启动自检报告（O-35）、备份与恢复演练（O-36～O-39）、任务（O-40、O-41）
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Card, Chip, Drawer, Icon, Prog, Table,
} from '../../ui/index.jsx';
import { Missing, ViewChip } from './ctlParts.jsx';
import {
  CHECK_ICON, CHECK_RESULT, SCOPE_LABEL, TASK_STATE, TASK_TYPE, backupScope, drillView, fmtBytes, fmtDateTime, fmtDuration, fmtWhen,
  nameOf, scheduleText, startReloads, startupSub, taskAction, taskActionToast, taskProgress,
} from './ctlModel.js';

export function StartupCard({ report }) {
  return (
    <Card title="启动自检报告" icon="shieldok" sub={startupSub(report)} data-sec="startup">
      {(report?.items || []).map((x) => {
        const [cls, icon] = CHECK_ICON[x.tone] || CHECK_ICON.ok;
        const r = CHECK_RESULT[x.result] || { label: x.result, tone: 'gray' };
        return (
          <div className="op-ck" key={x.id}>
            <span className={`ci ${cls}`}><Icon name={icon} size={12} /></span>
            <div className="grow"><div className="b sm">{x.title}</div><div className="xs muted">{x.detail}</div></div>
            <Chip tone={r.tone} sm>{r.label}</Chip>
          </div>
        );
      })}
      {report && !(report.items || []).length ? <div className="muted sm">这次启动没有需要处理的项。</div> : null}
      {!report ? <Missing /> : null}
    </Card>
  );
}

export function BackupsCard({
  backups, drills, perm, run, busy, reload,
}) {
  const { api } = useApp();
  const [open, setOpen] = useState(false);
  const deny = perm.opsWrite ? null : perm.hint('ops.write');
  const items = backups?.items || [];
  const dv = drillView(backups?.last_drill || null);
  const backupNow = () => run('backup', () => api.ops.backupNow(), { ok: '已开始备份', icon: 'archive' }).then((r) => r && reload(...startReloads('backup')));
  const drill = () => run('drill', () => api.ops.startDrill({ backup: items[0].name, sample_projects: 5 }), {
    ok: '已开始恢复演练',
  }).then((r) => r && reload(...startReloads('drill')));
  return (
    <Card
      title="备份与恢复" icon="archive" sub={scheduleText(backups?.schedule)} data-sec="backups"
      extra={<Button size="xs" kind="primary" icon="archive" disabled={busy === 'backup'} onClick={backupNow}>立即备份</Button>}
    >
      <div className="xs muted mb8">备份范围</div>
      <div className="op-scope mb12">
        {(backups?.scope || []).map((k) => <Chip key={k} tone="blue" sm icon="check">{SCOPE_LABEL[k] || k}</Chip>)}
      </div>
      <Table
        compact
        cols={['文件', '大小', '时间', '范围']}
        rows={items.map((b) => {
          const sc = backupScope(b);
          return [
            <span className="mono xs">{b.name}</span>, fmtBytes(b.size), fmtDateTime(b.created_at),
            sc.chip ? <span className="chip amber sm">{sc.text}</span> : sc.text,
          ];
        })}
      />
      {!backups ? <Missing /> : null}
      <div className={`row mt12 oc-drill ${dv.tone}`}>
        <Icon name={dv.icon} size={14} />
        <div className="grow sm">
          <b>{dv.title}</b>
          {(drills?.items || []).length ? <a className="link xs oc-drill-log" onClick={() => setOpen(true)}>演练记录</a> : null}
          {dv.detail ? <div className="xs muted">{dv.detail}</div> : null}
        </div>
        <Button size="xs" disabled={!perm.opsWrite || !items.length || busy === 'drill'} title={deny || (items.length ? `用最新的备份 ${items[0].name} 演练` : '还没有备份')} onClick={drill}>发起演练</Button>
      </div>
      <Drawer open={open} onClose={() => setOpen(false)} icon="history" title="恢复演练记录" w={560}>
        <Table
          compact
          cols={['时间', '备份', '用时', '结果']}
          rows={(drills?.items || []).map((d) => [
            fmtDateTime(d.at), <span className="mono xs">{d.backup}</span>, fmtDuration(d.duration_ms),
            <span title={d.detail}><Chip tone={d.result === 'passed' ? 'teal' : 'red'} sm>{d.result === 'passed' ? '通过' : '没通过'}</Chip></span>,
          ])}
        />
      </Drawer>
    </Card>
  );
}

export function TasksCard({ tasks, names, perm, reload }) {
  const { api } = useApp();
  const [busy, setBusy] = useState(null);
  const act = async (t, a) => {
    setBusy(t.id);
    try {
      await api.ops.taskAction(t.id, a.op);
      toast(taskActionToast(a.op, t, null), 'check');
    } catch (e) {
      toast(taskActionToast(a.op, t, e) || errorText(e), 'alert');
    } finally {
      setBusy(null);
      reload('tasks');
    }
  };
  const now = Date.now();
  return (
    <Card className="mt16" title="任务" icon="list" data-sec="tasks">
      <Table
        compact
        cols={['类型', '对象', '发起人', '状态', '进度', '开始', '操作']}
        rows={(tasks?.items || []).map((t) => {
          const p = taskProgress(t);
          const a = taskAction(t, perm.opsWrite);
          return [
            <b>{TASK_TYPE[t.type] || t.type}</b>,
            <span className="muted" title={t.error || undefined}>{t.target}</span>,
            nameOf(names, t.initiator),
            <ViewChip v={TASK_STATE[t.state] || { label: t.state, tone: 'gray' }} />,
            <div className="row"><Prog p={p.pct} w="90px" thin tone={p.tone} /><span className="num xs">{p.pct}%</span></div>,
            <span className="muted">{fmtWhen(t.started_at, now)}</span>,
            a ? <Button size="xs" kind={a.kind} disabled={a.disabled || busy === t.id} title={a.title} onClick={() => act(t, a)}>{a.label}</Button> : <span className="faint xs">—</span>,
          ];
        })}
      />
      {tasks && !(tasks.items || []).length ? <div className="muted sm mt8">最近没有后台任务。</div> : null}
      {!tasks ? <Missing /> : null}
    </Card>
  );
}
