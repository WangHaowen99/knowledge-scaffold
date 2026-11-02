// 维护模式（O-20～O-24；契约 1.13、#57；B3b 第 11、12 条）：四步向导、进入维护模式弹窗、强制断开剩余会话
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import {
  Area, Button, Chip, Field, Icon, Modal, Prog, Seg, Select, cx,
} from '../../ui/index.jsx';
import { ConfirmModal } from './ctlParts.jsx';
import {
  MAINT_MODES, currentStep, defaultMaintMessage, maintActions, maintBody, maintErrorText, maintProblem, maintSteps, maintTitle,
} from './ctlModel.js';

export function MaintenanceCard({ state, perm, run, busy, onChanged }) {
  const { api } = useApp();
  const [force, setForce] = useState(false);
  const active = !!state?.active;
  const steps = maintSteps(state);
  const acts = maintActions(state, perm.opsWrite);
  const cur = currentStep(state);
  const errText = (e) => maintErrorText(e, null);

  const advance = () => run('maint', () => api.ops.advanceMaintenance(), { ok: '已进入下一步，已写审计', errText }).then((r) => r && onChanged(r));
  const exit = (msg) => run('maint', () => api.ops.exitMaintenance(), { ok: msg, errText }).then((r) => r && onChanged(r));
  const forceDisconnect = (reason) => run('maint', () => api.ops.forceDisconnectMaintenance({ reason }), { ok: '已强制断开剩余会话，已写审计', errText })
    .then((r) => { if (r) onChanged(r); return r; });

  return (
    <div className="card" data-sec="maintenance">
      <div className="card-h">
        <span style={{ color: 'var(--blue)' }}><Icon name="wrench" /></span>
        <h3>{maintTitle(state)}</h3>
        {active ? <Chip tone="blue" dot>进行中</Chip> : <Chip tone="gray">没在维护</Chip>}
        <span className="sp" />
        {active ? (
          <>
            <span className="xs muted">模式</span>
            {/* 模式在进入时选定，维护中只显示；要换先取消维护 */}
            <Seg
              items={[['read_only', '只读'], ['reject_new_sessions', '拒绝新会话']]} value={state.mode}
              onChange={(k) => { if (k !== state.mode) toast('维护中不能换模式：先取消维护，再按新的模式进入', 'info'); }}
            />
          </>
        ) : null}
      </div>
      <div className="card-b">
        <div className="op-step">
          {steps.map((s) => (
            <div key={s.id} className={cx('s', s.cls)}>
              <span className="n">{s.cls === 'done' ? <Icon name="check" size={12} /> : s.n}</span>
              <div className="t">{s.title}</div>
              <div className="d">{s.detail}</div>
              {s.progress != null ? <div className="mt8"><Prog p={s.progress} thin /></div> : null}
            </div>
          ))}
        </div>
        {acts ? (
          <div className="row mt12">
            <Button size="sm" kind="danger" disabled={acts.force.disabled || busy === 'maint'} title={acts.force.title} onClick={() => setForce(true)}>跳过等待，强制断开剩余会话</Button>
            <span className="sp" />
            <Button size="sm" kind="ghost" disabled={acts.cancel.disabled || busy === 'maint'} title={acts.cancel.title} onClick={() => exit('已取消维护，限制已解除')}>取消维护</Button>
            <Button
              size="sm" kind="primary" disabled={acts.next.disabled || busy === 'maint'} title={acts.next.title}
              onClick={() => (acts.next.op === 'exit' ? exit('已恢复：限制解除，L4 从检查点续跑') : advance())}
            >
              {acts.next.label}
            </Button>
          </div>
        ) : (
          <div className="row mt12 xs muted">
            <Icon name="info" size={12} />没有进行中的维护。点页头的「进入维护模式」按这四步走，每一步都写审计。
          </div>
        )}
      </div>
      <ConfirmModal
        open={force} onClose={() => setForce(false)} danger icon="power" title="强制断开剩余会话？"
        sub={cur?.id === 'drain' && cur.detail ? `排空 · ${cur.detail}` : '维护窗口 · 排空'}
        note="正在进行的一轮会被中断，历史里标记「上一轮未正常结束」，执行记录标为「在某阶段中断」，用户回来时可以续跑。"
        reason="required" placeholder="例如：维护窗口到点，剩余会话强制断开" okText="强制断开" onConfirm={forceDisconnect}
      />
    </div>
  );
}

// 页头「进入维护模式」：限制方式、对用户的提示、L4 运行，另加升级目标（有 core beta 时默认带上）
export function EnterMaintModal({ open, onClose, core, run, busy, onEntered }) {
  const { api } = useApp();
  const beta = core?.beta?.version || null;
  const init = () => ({ mode: 'reject_new_sessions', message: defaultMaintMessage('reject_new_sessions', { upgrade: !!beta }), target: beta, touched: false });
  const [f, setF] = useState(init);
  useEffect(() => { if (open) setF(init()); }, [open, beta]);
  const pickMode = (mode) => setF((x) => ({ ...x, mode, message: x.touched ? x.message : defaultMaintMessage(mode, { upgrade: !!x.target }) }));
  const pickTarget = (v) => setF((x) => ({ ...x, target: v || null, message: x.touched ? x.message : defaultMaintMessage(x.mode, { upgrade: !!v }) }));
  const problem = maintProblem(f);
  const go = () => run('enter', () => api.ops.enterMaintenance(maintBody(f)), { ok: '已进入维护模式，已写审计', errText: (e) => maintErrorText(e, null) })
    .then((r) => { if (r) { onEntered(r); onClose(); } });
  const targets = [
    ...(beta ? [{ value: beta, label: `升级 core 到 ${beta}（现为 beta）` }] : []),
    { value: '', label: '不升级，只做维护' },
  ];
  return (
    <Modal
      open={open} onClose={onClose} icon="wrench" title="进入维护模式" sub="步骤：设为只读或拒绝新会话 → 排空 → 升级 → 恢复"
      foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={!!problem || busy === 'enter'} title={problem} onClick={go}>开始</Button></>}
    >
      <div className="col" style={{ gap: 12 }}>
        <Field label="限制方式">
          <div className="col" style={{ gap: 8 }}>
            {Object.entries(MAINT_MODES).map(([k, m], i) => (
              <div key={k} className={cx('opt', f.mode === k && 'on')} role="radio" aria-checked={f.mode === k} onClick={() => pickMode(k)}>
                <span className="k">{i + 1}</span>
                <div><div className="t">{m.label}</div><div className="c">{m.desc}</div></div>
              </div>
            ))}
          </div>
        </Field>
        <Field label="对用户的提示">
          <Area minHeight={56} textareaStyle={{ height: 36 }} value={f.message} onChange={(e) => setF((x) => ({ ...x, message: e.target.value, touched: true }))} />
        </Field>
        <Field label="L4 运行">
          <Select value="pause_at_safe_point" options={[{ value: 'pause_at_safe_point', label: '在安全点暂停，恢复后从检查点续跑' }]} />
        </Field>
        <Field label="升级目标">
          <Select value={f.target || ''} options={targets} onChange={pickTarget} />
        </Field>
      </div>
    </Modal>
  );
}
