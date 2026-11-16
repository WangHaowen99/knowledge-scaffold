// 项目设置 · 基本信息（P-04、P-05，重名用 P-03 实时检查）与引擎与模型（C-06、P-12、A-09）
import { useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Area, Button, Chip, Field, Icon, Inp, KV, Modal, Note, cx,
} from '../../ui/index.jsx';
import {
  basicPatch, createdText, engineCards, modelDisplayName, nameProblem,
} from './settingsModel.js';
import { NameCount, NameLimitHint, limitProjectName, nameAtLimit } from '../projects/NameCount.jsx';

function useNameCheck(name, excludeId, enabled) {
  const { api } = useApp();
  const [res, setRes] = useState(null);
  useEffect(() => {
    setRes(null);
    if (!enabled || !name.trim()) return undefined;
    let alive = true;
    const t = setTimeout(() => { api.nameCheck(name.trim(), excludeId).then((r) => alive && setRes(r)).catch(() => {}); }, 250);
    return () => { alive = false; clearTimeout(t); };
  }, [api, name, excludeId, enabled]);
  return res;
}

export function BasicSection({ archived }) {
  const { api } = useApp();
  const { pid, meta, setMeta } = useProject();
  const [form, setForm] = useState({ name: meta.name, goal: meta.goal || '' });
  const [busy, setBusy] = useState(false);
  useEffect(() => { setForm({ name: meta.name, goal: meta.goal || '' }); }, [meta.name, meta.goal]);
  const patch = basicPatch(meta, form);
  const renaming = 'name' in patch;
  const chk = useNameCheck(form.name, pid, renaming);
  const problem = renaming ? nameProblem(chk, form.name) : null;
  const dirty = Object.keys(patch).length > 0;

  const save = async () => {
    setBusy(true);
    try {
      const m = await api.patchProject(pid, patch);
      setMeta(m);
      toast('已保存');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card" data-sec="basic">
      <div className="card-h">
        <h3>基本信息</h3><span className="sp" />
        <Button size="sm" kind="primary" disabled={!dirty || !!problem || busy || archived} onClick={save}>保存</Button>
      </div>
      <div className="card-b">
        <div className="grid2">
          <Field label="项目名称" hint={problem ? <span style={{ color: 'var(--red)' }}>{problem}</span> : nameAtLimit(form.name) ? <NameLimitHint /> : null}>
            <Inp value={form.name} error={!!problem} disabled={archived} after={<NameCount value={form.name} />} onChange={(e) => setForm((f) => ({ ...f, name: limitProjectName(e.target.value) }))} />
          </Field>
          <Field label="知识包名">
            <Inp mono value={meta.package_name || ''} readOnly after={<Icon name="lock" size={14} />} title="知识包名创建后不能改" />
          </Field>
        </div>
        <div className="mt12">
          <Field label="一句话目标">
            <Area minHeight={56} textareaStyle={{ height: 36 }} value={form.goal} disabled={archived} onChange={(e) => setForm((f) => ({ ...f, goal: e.target.value }))} />
          </Field>
        </div>
        <div className="row mt12 g16">
          <KV w="72px" pairs={[['当前版本', meta.version_label || '—'], ['创建', createdText(meta)]]} />
        </div>
      </div>
    </div>
  );
}

export function EngineSection({ d, put, archived, active }) {
  const { api } = useApp();
  const { pid, meta, setMeta } = useProject();
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);
  const current = d.engine?.engine || meta.engine;
  const cards = engineCards(d.engines, current);
  const locked = archived || !!active;
  const cfg = d.model?.engines?.[current];

  const pick = (c) => {
    if (c.on || !c.installed) return;
    if (locked) { toast(archived ? '项目已归档，只能查看' : '自主构建运行中，换引擎要等运行结束', 'lock'); return; }
    setConfirm(c);
  };
  const go = async () => {
    setBusy(true);
    try {
      const r = await api.setEngine(pid, { engine: confirm.id });
      put('engine', (x) => ({ ...x, engine: r.engine }));
      setMeta({ ...meta, engine: r.engine, engine_label: confirm.label });
      toast(`已切换到${confirm.label}`, 'cpu');
      setConfirm(null);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card" data-sec="engine">
      <div className="card-h"><h3>引擎与模型</h3></div>
      <div className="card-b">
        <div className="grid2">
          {cards.map((c) => (
            <div key={c.id} className={cx('evd-eng', c.on && 'on', !c.installed && 'off', locked && !c.on && 'lock')} onClick={() => pick(c)} title={c.reason || undefined}>
              <span className="ic"><Icon name={c.icon} size={18} /></span>
              <div className="grow">
                <div className="row">
                  <b>{c.label}</b>
                  {c.badge ? <Chip tone={c.badge[1]} sm>{c.badge[0]}</Chip> : null}
                  {c.reason ? <Chip tone="gray" sm>{c.reason}</Chip> : null}
                </div>
                <div className="sm muted mt4">{c.desc}</div>
              </div>
              <span className={cx('rad', c.on && 'on')} />
            </div>
          ))}
        </div>
        {/* 模型跟着个人设置走，这里只读一行（同新建向导第 4 步）：不画成下拉框，旁边给改的入口 */}
        <div className="en-model mt12">
          <span className="xs muted">模型</span>
          <Icon name="sparkle" size={14} />
          <b className="sm ell">{modelDisplayName(cfg)}</b>
          <a className="link xs" href="#/me?section=model">改我的模型配置</a>
        </div>
      </div>
      {confirm ? (
        <Modal
          open onClose={() => setConfirm(null)} icon="cpu" title={`切换到${confirm.label}？`} w={480}
          foot={<><Button onClick={() => setConfirm(null)}>取消</Button><Button kind="primary" disabled={busy} onClick={go}>切换引擎</Button></>}
        >
          <Note tone="amber">本项目的会话会关闭，正在进行的一轮会中断。</Note>
        </Modal>
      ) : null}
    </div>
  );
}
