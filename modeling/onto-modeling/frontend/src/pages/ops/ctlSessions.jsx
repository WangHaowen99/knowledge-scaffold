// 会话池（O-15～O-19）：配置（契约 #56：运维面可改，覆盖环境变量默认值）、在线会话、回收、强制断开、排空
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import {
  Button, Empty, Field, Icon, Inp, KV, Modal, Note, Select, Table,
} from '../../ui/index.jsx';
import { ConfirmModal, ViewChip } from './ctlParts.jsx';
import {
  OVERFLOW_OPTIONS, SESSION_STATE, drainSummary, fmtCount, fmtDuration, idleText, nameOf, poolBody, poolDirty, poolForm, poolSub,
  sessionActions, sessionSub, validatePool,
} from './ctlModel.js';

export function SessionsSection({
  pool, names, perm, run, busy, reload, onDrained,
}) {
  const { api } = useApp();
  const [form, setForm] = useState(() => poolForm(pool?.config));
  const [dialog, setDialog] = useState(null); // { kind: 'recycle'|'kill'|'drain', item? }
  useEffect(() => { setForm(poolForm(pool?.config)); }, [pool?.config]);
  const errors = validatePool(form);
  const dirty = poolDirty(form, pool?.config);
  const deny = perm.opsWrite ? null : perm.hint('ops.write');
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e?.target ? e.target.value : e }));

  const save = () => run('pool', () => api.ops.saveSessionConfig(poolBody(form)), { ok: '已保存，下一个会话起生效' }).then((r) => r && reload('sessions'));
  const recycle = (item) => run('recycle', () => api.ops.recycleSession(item.project.id), { ok: '已回收该会话' }).then((r) => { if (r) reload('sessions'); return r; });
  const kill = (item, reason) => run('kill', () => api.ops.disconnectSession(item.project.id, { reason }), { ok: '已断开' }).then((r) => { if (r) reload('sessions'); return r; });
  const drain = () => run('drain', () => api.ops.drainSessions({ max_wait_s: 600 }), {
    ok: (r) => `已开始排空${r?.eta_ms ? ` · 预计 ${fmtDuration(r.eta_ms)}` : ''}`,
  }).then((r) => { if (r) { reload('sessions'); onDrained?.(); } return r; });

  const items = pool?.items || [];
  const sum = drainSummary(pool);
  const errTip = (k) => (errors[k] && dirty ? errors[k] : null);

  return (
    <div className="card mt16" data-sec="sessions">
      <div className="card-h">
        <span style={{ color: 'var(--blue)' }}><Icon name="msg" /></span>
        <h3>会话池</h3>
        <span className="sub">{poolSub(pool)}</span>
        <span className="sp" />
        <Button size="xs" icon="power" disabled={!perm.opsWrite || !sum.online} title={deny || (sum.online ? null : '没有在线会话')} onClick={() => setDialog({ kind: 'drain' })}>排空会话</Button>
      </div>
      <div className="card-b">
        <div className="row g16 mb12 oc-poolcfg">
          <Field label="并发上限" hint={errTip('max_sessions')}>
            <Inp w="110px" value={form.max_sessions} onChange={set('max_sessions')} disabled={!perm.opsWrite} error={!!errTip('max_sessions')} inputMode="numeric" after={<span className="faint xs">个会话</span>} />
          </Field>
          <Field label="空闲回收阈值" hint={errTip('idle_ttl_s')}>
            <Inp w="110px" value={form.idle_ttl_s} onChange={set('idle_ttl_s')} disabled={!perm.opsWrite} error={!!errTip('idle_ttl_s')} inputMode="numeric" after={<span className="faint xs">秒</span>} />
          </Field>
          <Field label="超限时">
            <Select w="260px" value={form.overflow_policy} options={OVERFLOW_OPTIONS} onChange={set('overflow_policy')} disabled={!perm.opsWrite} />
          </Field>
          <span className="sp" />
          <Button
            size="sm" kind="primary" disabled={!perm.opsWrite || !dirty || Object.keys(errors).length > 0 || busy === 'pool'}
            title={deny || (!dirty ? '没有改动' : Object.values(errors)[0] || null)} onClick={save}
          >
            保存
          </Button>
        </div>
        {items.length ? (
          <Table
            compact
            cols={['项目', '所有者', '引擎', '状态', '事件数', '空闲时长', '操作']}
            rows={items.map((it) => {
              const a = sessionActions(it, perm.opsWrite);
              return [
                <div className="row g4" title="只显示项目元数据"><Icon name="lock" size={12} /><b>{it.project?.name}</b></div>,
                nameOf(names, it.project?.owner),
                <span className="tag">{it.engine_label}</span>,
                <ViewChip v={SESSION_STATE[it.state]} />,
                <span className="num">{it.events == null ? '—' : fmtCount(it.events)}</span>,
                <span className="muted num">{idleText(it)}</span>,
                a.show ? (
                  <div className="row g4">
                    <Button size="xs" disabled={a.recycle.disabled} title={a.recycle.title} onClick={() => setDialog({ kind: 'recycle', item: it })}>回收</Button>
                    <Button size="xs" kind="danger" disabled={a.kill.disabled} title={a.kill.title} onClick={() => setDialog({ kind: 'kill', item: it })}>强制断开</Button>
                  </div>
                ) : <span className="xs faint">等待槽位</span>,
              ];
            })}
          />
        ) : (
          <Empty icon="msg" title="当前没有活跃会话" />
        )}
      </div>

      <Modal
        open={dialog?.kind === 'recycle'} onClose={() => setDialog(null)} icon="recycle" title="回收这个会话？"
        sub={dialog?.item ? sessionSub(dialog.item, names) : ''}
        foot={<><Button onClick={() => setDialog(null)}>取消</Button><Button kind="primary" disabled={busy === 'recycle'} onClick={() => recycle(dialog.item).then((r) => r && setDialog(null))}>回收</Button></>}
      >
        <Note tone="teal" icon="check">用户下次发消息时自动恢复</Note>
      </Modal>
      <ConfirmModal
        open={dialog?.kind === 'kill'} onClose={() => setDialog(null)} danger icon="power" title="强制断开会话？"
        sub={dialog?.item ? sessionSub(dialog.item, names, { engine: false }) : ''}
        note="正在进行的一轮会中断，用户回来后可以续跑"
        reason="required" okText="强制断开" onConfirm={(reason) => kill(dialog.item, reason)}
      />
      <Modal
        open={dialog?.kind === 'drain'} onClose={() => setDialog(null)} icon="power" title="排空会话"
        foot={<><Button onClick={() => setDialog(null)}>取消</Button><Button kind="primary" disabled={busy === 'drain'} onClick={() => drain().then((r) => r && setDialog(null))}>开始排空</Button></>}
      >
        <div className="col" style={{ gap: 10 }}>
          <Note tone="gray">排空期间拒绝新会话，在线会话跑完这一轮后回收</Note>
          <KV pairs={[
            ['在线会话', `${sum.online} 个（${sum.running} 个正在运行）`],
            ['自主构建', sum.autobuild ? `${sum.autobuild} 个，会在安全点暂停` : '没有'],
            ['最长等待', '10 分钟，超时的强制断开'],
          ]}
          />
        </div>
      </Modal>
    </div>
  );
}
