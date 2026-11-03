// 可观测 · 导出器（O-11 列表、O-12 保存）：卡片与弹窗（OTel / Prometheus / 本地文件 / 外部平台）。
// 卡片上的开关不直接保存，带着这次改动打开弹窗确认。不能关的（required）不出开关，内置的（builtin）地址与信号固定、不出编辑。
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Card, Check, Field, Icon, Inp, Modal, Note, Toggle, cx } from '../../ui/index.jsx';
import { SIGNALS, exporterProblems, exporterView, exportersBody, exportersDirty } from './obsModel.js';

export function ExportersCard({ data, error, perm, onOpen }) {
  const items = data || [];
  const denied = perm.opsWrite ? undefined : perm.hint('ops.write');
  return (
    <Card title="导出器" icon="cloud" extra={<Button size="xs" disabled={!!error} onClick={() => onOpen()}>配置</Button>}>
      {error ? <div className="muted sm">导出器没取到：{error}</div> : items.map((e) => {
        const v = exporterView(e);
        return (
          <div className="op-ck" key={e.id}>
            <span className={cx('ci', e.enabled ? 'ok' : 'w')}><Icon name="cloud" size={12} /></span>
            <div className="grow">
              <div className="row g4"><b className="sm">{v.label}</b></div>
              <div className="xs muted mono ell" style={{ maxWidth: 240 }} title={v.target}>{v.target}</div>
              <div className="xs faint">{v.signals}</div>
            </div>
            {e.required ? null : <Toggle on={e.enabled} disabled={!perm.opsWrite} title={denied} onChange={(on) => onOpen({ id: e.id, enabled: on })} />}
          </div>
        );
      })}
    </Card>
  );
}

/**
 * @param {object} o
 * @param {object[]} o.items 服务端当前的导出器
 * @param {{id: string, enabled: boolean}|null} o.change 从卡片开关带过来的改动
 */
export function ExportersModal({ items, change, perm, onClose, onSaved }) {
  const { api } = useApp();
  const [draft, setDraft] = useState(() => items.map((e) => ({ ...e, signals: [...(e.signals || [])], ...(change && e.id === change.id ? { enabled: change.enabled } : null) })));
  const [editing, setEditing] = useState(() => new Set(change?.enabled ? items.filter((e) => e.id === change.id && !e.configured).map((e) => e.id) : []));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const problems = exporterProblems(draft);
  const dirty = exportersDirty(draft, items);
  const denied = perm.opsWrite ? undefined : perm.hint('ops.write');

  const patch = (id, p) => setDraft((d) => d.map((e) => (e.id === id ? { ...e, ...p } : e)));
  const toggleEdit = (id) => setEditing((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });
  const setOn = (e, on) => {
    patch(e.id, { enabled: on });
    if (on && !String(e.target || '').trim()) setEditing((s) => new Set(s).add(e.id));
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.ops.saveExporters(exportersBody(draft));
      toast('已保存');
      onSaved(r.items);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  };

  return (
    <Modal
      open onClose={onClose} w={640} icon="sliders"
      title="导出器"
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={!perm.opsWrite || busy || !dirty || Object.keys(problems).length > 0} title={denied || problems[Object.keys(problems)[0]]} onClick={save}>
            保存
          </Button>
        </>
      )}
    >
      <div className="col" style={{ gap: 10 }}>
        {draft.map((e) => {
          const v = exporterView({ ...e, configured: !!String(e.target || '').trim() });
          const fixed = e.implementation === 'builtin';
          const open = !fixed && editing.has(e.id);
          const canEdit = perm.opsWrite && !fixed;
          return (
            <div className="card pad" key={e.id}>
              <div className="row g12">
                <span style={{ width: 34, height: 34, borderRadius: 10, display: 'grid', placeItems: 'center', background: 'var(--b50)', color: 'var(--blue)', flex: 'none' }}>
                  <Icon name="cloud" size={18} />
                </span>
                <div className="grow" style={{ cursor: canEdit ? 'pointer' : undefined, minWidth: 0 }} title={canEdit ? '点开修改地址与信号' : undefined} onClick={() => canEdit && toggleEdit(e.id)}>
                  <div className="row g4"><b>{v.label}</b><span className="tag">{v.impl}</span></div>
                  <div className="xs muted mono ell">{v.target}</div>
                  <div className="xs muted">{v.signals}</div>
                </div>
                {!e.enabled && !fixed ? <Button size="xs" disabled={!perm.opsWrite} title={denied} onClick={() => toggleEdit(e.id)}>配置</Button> : null}
                {e.required ? null : <Toggle on={e.enabled} disabled={!perm.opsWrite} title={denied} onChange={(on) => setOn(e, on)} />}
              </div>
              {open ? (
                <div className="op-exp-ed">
                  <Field label="地址" req={e.enabled} hint={e.id === 'prometheus' ? '运维监听上的抓取路径' : null}>
                    <Inp mono value={e.target || ''} onChange={(ev) => patch(e.id, { target: ev.target.value })} placeholder="grpc://collector.example.com:4317" />
                  </Field>
                  <Field label="信号">
                    <div className="row g16">
                      {SIGNALS.map(([k, t]) => (
                        <Check key={k} on={e.signals.includes(k)} label={t} onChange={(on) => patch(e.id, { signals: on ? [...e.signals, k] : e.signals.filter((x) => x !== k) })} />
                      ))}
                    </div>
                  </Field>
                </div>
              ) : null}
              {problems[e.id] ? <div className="xs mt8" style={{ color: 'var(--red)' }}>{problems[e.id]}</div> : null}
            </div>
          );
        })}
        {error ? <Note tone="red">{error}</Note> : null}
      </div>
    </Modal>
  );
}

