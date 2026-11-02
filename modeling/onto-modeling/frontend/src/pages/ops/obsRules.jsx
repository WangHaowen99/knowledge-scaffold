// 可观测 · 告警规则（O-08 列表、O-09 新建、O-10 修改 / 启停 / 删除），每条带处置手册（runbook_md）。
// 包声明的规则（source 是包版本）只能启停，不能改、不能删；只读（管理员）时写按钮 disabled 并带说明。
import { useCallback, useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Md } from '../workbench/parts.jsx';
import { Area, Button, Card, Check, Chip, Field, Icon, Inp, Modal, Note, Seg, Table, Toggle } from '../../ui/index.jsx';
import { settleScroll } from './obsTrace.jsx';
import { RULE_LEVELS, emptyRule, isPackRule, ruleProblems, rulePatch, ruleView, rulesSub } from './obsModel.js';

const errHint = (text) => (text ? <span style={{ color: 'var(--red)' }}>{text}</span> : null);

function RuleModal({ rule, isNew, perm, onClose, onDone }) {
  const { api } = useApp();
  const [form, setForm] = useState(() => (isNew ? emptyRule() : { ...rule, window: rule.window || '', runbook_md: rule.runbook_md || '' }));
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [confirmDel, setConfirmDel] = useState(false);
  const pack = !isNew && isPackRule(rule);
  const editable = perm.opsWrite && !pack;
  const canToggle = perm.opsWrite;
  const problems = editable ? ruleProblems(form) : {};
  const shown = touched ? problems : {};
  const patch = isNew ? null : rulePatch(rule, form);
  const dirty = isNew || Object.keys(patch).length > 0;
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }));
  const denied = perm.opsWrite ? undefined : perm.hint('ops.write');

  const save = async () => {
    setTouched(true);
    if (Object.keys(problems).length) return;
    setBusy(true);
    setError(null);
    try {
      if (isNew) {
        const { name, condition, window, level, runbook_md, auto_action, enabled } = form;
        await api.ops.createAlertRule({ name: name.trim(), condition: condition.trim(), window: window.trim(), level, runbook_md, auto_action, enabled });
        toast('已新建告警规则');
      } else {
        await api.ops.updateAlertRule(rule.id, patch);
        toast(pack ? (form.enabled ? '已启用这条规则' : '已停用这条规则') : '已保存告警规则');
      }
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!confirmDel) { setConfirmDel(true); return; }
    setBusy(true);
    try {
      await api.ops.deleteAlertRule(rule.id);
      toast('已删除告警规则');
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  };

  let sub = '规则按条件与窗口评估；触发时按级别通知，处置手册随告警一起带上。';
  if (pack) sub = `由 ${rule.source} 在包清单里声明：只能停用或启用，不能修改和删除。`;
  else if (!perm.opsWrite) sub = `${perm.hint('ops.write')}。`;
  const title = isNew ? '新建告警规则' : editable ? '修改告警规则' : '告警规则';

  return (
    <Modal
      open onClose={onClose} w={600} icon="bell" title={title} sub={sub}
      foot={(
        <>
          {!isNew && !pack ? (
            <Button kind="danger" disabled={!perm.opsWrite || busy} title={denied} onClick={remove}>{confirmDel ? '确认删除' : '删除'}</Button>
          ) : null}
          <span className="sp" />
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={!canToggle || busy || !dirty} title={denied} onClick={save}>{isNew ? '新建规则' : '保存'}</Button>
        </>
      )}
    >
      <div className="col op-rule" style={{ gap: 12 }}>
        <Field label="名称" req hint={errHint(shown.name)}>
          <Inp value={form.name} disabled={!editable} error={!!shown.name} onChange={(e) => set('name')(e.target.value)} placeholder="如：节点失败率过高" />
        </Field>
        <div className="row g12" style={{ alignItems: 'flex-start' }}>
          <Field label="触发条件" req className="grow" hint={errHint(shown.condition) || '指标表达式，如 node.fail_rate > 5%'}>
            <Inp mono value={form.condition} disabled={!editable} error={!!shown.condition} onChange={(e) => set('condition')(e.target.value)} placeholder="node.fail_rate > 5%" />
          </Field>
          <Field label="窗口" hint="留空表示即时判断">
            <Inp w={150} value={form.window} disabled={!editable} onChange={(e) => set('window')(e.target.value)} placeholder="1 小时" />
          </Field>
        </div>
        <Field label="级别">
          <div style={editable ? undefined : { pointerEvents: 'none', opacity: 0.6 }}>
            <Seg items={RULE_LEVELS} value={form.level} onChange={set('level')} />
          </div>
        </Field>
        <Field label="处置手册" hint={editable ? 'Markdown；告警通知和总览里会带上第一句' : null}>
          {editable ? (
            <Area minHeight={130} value={form.runbook_md} onChange={(e) => set('runbook_md')(e.target.value)} placeholder={'第一句写最该做的事，比如「熔断出错的包」。\n\n1. …'} />
          ) : (
            <div className="card pad sm op-rb" style={{ background: 'var(--panel2)' }}>{form.runbook_md ? <Md text={form.runbook_md} /> : <span className="muted">还没写处置手册</span>}</div>
          )}
        </Field>
        <Check
          on={form.auto_action === 'breaker'} disabled={!editable} label="触发时自动熔断出错的包"
          onChange={(v) => set('auto_action')(v ? 'breaker' : null)}
        />
        <div className="row">
          <Toggle on={form.enabled} disabled={!canToggle} title={denied} onChange={set('enabled')} />
          <span className="sm">{form.enabled ? '启用中' : '已停用'}</span>
          {!isNew && rule.state === 'firing' && rule.enabled ? <Chip tone="red" sm dot>触发中</Chip> : null}
        </div>
        {error ? <Note tone="red">{error}</Note> : null}
      </div>
    </Modal>
  );
}

export function RulesCard({ perm, focus }) {
  const { api } = useApp();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(null); // {rule} | {isNew: true}

  const load = useCallback(() => api.ops.alertRules()
    .then((r) => { setData(r); setError(null); })
    .catch((e) => setError(errorText(e))), [api]);
  useEffect(() => { load(); }, [load]);

  // 总览「全部规则」带 ?section=rules 过来：取到规则后滚到这张卡
  const loaded = !!(data || error);
  useEffect(() => {
    if (focus && loaded) settleScroll(() => document.getElementById('op-rules'));
  }, [focus, loaded]);

  const items = data?.items || [];
  const denied = perm.opsWrite ? undefined : perm.hint('ops.write');
  return (
    <>
      <Card
        id="op-rules" style={{ scrollMarginTop: 16 }} title="告警规则" icon="bell" sub={data ? rulesSub(items) : ''}
        extra={<Button size="xs" icon="plus" disabled={!perm.opsWrite || !!error} title={denied} onClick={() => setOpen({ isNew: true })}>新建规则</Button>}
      >
        {error ? <div className="muted sm">告警规则没取到：{error}</div> : (
          <Table
            compact
            cols={['规则', '条件', '状态', '级别', '来源', '处置手册']}
            rowClassName={(i) => (items[i]?.enabled ? undefined : 'op-off')}
            rows={items.map((r) => {
              const v = ruleView(r);
              return [
                <b key="n" className="sm" style={{ cursor: 'pointer' }} onClick={() => setOpen({ rule: r })}>{r.name}</b>,
                <span key="c" className="xs muted mono">{v.condition}</span>,
                <Chip key="s" tone={v.state.tone} sm dot={v.state.dot}>{v.state.label}</Chip>,
                <Chip key="l" tone={v.level[1]} sm>{v.level[0]}</Chip>,
                v.source ? <span key="o" className="tag" title="包在清单 contributes.metrics 里声明">{v.source}</span> : <span key="o" className="muted sm">内核</span>,
                <a key="h" className="link sm" data-pop={v.summary} onClick={() => setOpen({ rule: r })}><Icon name="book" size={12} /> 手册</a>,
              ];
            })}
          />
        )}
      </Card>
      {open ? (
        <RuleModal
          key={open.rule?.id || 'new'} rule={open.rule} isNew={!!open.isNew} perm={perm}
          onClose={() => setOpen(null)} onDone={() => { setOpen(null); load(); }}
        />
      ) : null}
    </>
  );
}
