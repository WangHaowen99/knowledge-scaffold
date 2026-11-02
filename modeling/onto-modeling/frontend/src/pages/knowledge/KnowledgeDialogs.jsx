// 知识沉淀页的弹窗（设计稿 mrKnAdd / mrKnDel / mrKnNom / mrKnConfM）：新增与编辑、删除、提名回流、冲突检查明细
import { useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Area, Button, Check, Chip, Field, Icon, Inp, Modal, Note, Seg,
} from '../../ui/index.jsx';
import {
  TYPE_ITEMS, checkModalSub, checkRows, conflictDetail, conflictKindTitle, countsLine, deleteImpact, formBody,
  markSegments, materialsLine, nominateTitle, validateForm,
} from './knowledgeModel.js';
import { ScopePicker } from './ScopePicker.jsx';

// 文本框高度：连边框正好是 .inp.area 的最小高度 72px（设计稿的占位框）
const AREA_H = { height: 52 };

// 新增 / 编辑：mode=add 走 K-03，mode=edit 走 K-04（带 base_revision）
export function EntryFormModal({ open, mode, initial, detail, onClose, onSaved }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [form, setForm] = useState(initial);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setForm(initial); setTried(false); } }, [open, initial]);
  if (!open || !form) return null;
  const errs = validateForm(form);
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }));
  const edit = mode === 'edit';
  const save = async () => {
    setTried(true);
    if (Object.keys(errs).length) return;
    setBusy(true);
    try {
      const body = formBody(form, { base_revision: edit ? detail?.revision : undefined });
      const r = edit ? await api.updateKnowledge(pid, detail.id, body) : await api.createKnowledge(pid, body);
      onSaved?.(r, mode);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      onClose={onClose}
      icon={edit ? 'edit' : 'plus'}
      title={edit ? '编辑业务知识' : '新增业务知识'}
      w={600}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={busy} onClick={save}>{edit ? '保存' : '确认入库'}</Button></>}
    >
      <div className="col kn-form" style={{ gap: 14 }}>
        <Field label="条目名称" req hint={tried && errs.title ? <span className="kn-err">{errs.title}</span> : null}>
          <Inp placeholder="如：隔日到货的牛奶陈列上限按区域配额执行" maxLength={60} value={form.title} error={tried && errs.title} onChange={(e) => set('title')(e.target.value)} />
        </Field>
        <div className="grid2">
          <Field label="类型"><span><Seg items={TYPE_ITEMS} value={form.type} onChange={set('type')} /></span></Field>
          <Field label="适用范围"><ScopePicker value={form.scope} onChange={set('scope')} /></Field>
        </div>
        <Field label="背景">
          <Area textareaStyle={AREA_H} placeholder="为什么有这条知识，什么情况下会用错" value={form.background} onChange={(e) => set('background')(e.target.value)} />
        </Field>
        <Field label="规则" hint={tried && errs.rule ? <span className="kn-err">{errs.rule}</span> : null}>
          <Area textareaStyle={AREA_H} className={tried && errs.rule ? 'err' : undefined} placeholder={'## 规则\n写成可以照着做的一句话或几条'} value={form.rule} onChange={(e) => set('rule')(e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}

// 删除（K-05）：被覆盖的行业包条目重新生效
export function DeleteModal({ open, detail, onClose, onDeleted }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [busy, setBusy] = useState(false);
  if (!open || !detail) return null;
  const del = async () => {
    setBusy(true);
    try {
      const r = await api.deleteKnowledge(pid, detail.id, detail.revision);
      onDeleted?.(r);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      danger
      onClose={onClose}
      icon="trash"
      title="删除该知识条目？"
      sub={detail.title}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="danger fill" disabled={busy} onClick={del}>删除</Button></>}
    >
      <div className="col sm" style={{ gap: 8 }}>
        <div>可在版本历史里找回</div>
        <div>{deleteImpact(detail)}</div>
      </div>
    </Modal>
  );
}

// 提名回流到行业包（K-07 预览脱敏结果，K-08 提交）
export function NominateModal({ open, detail, pack, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState(null);
  const [general, setGeneral] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open || !detail) return undefined;
    let alive = true;
    setPreview(null);
    setError(null);
    setGeneral(false);
    api.nominationPreview(pid, detail.id, { industry_pack: pack?.name })
      .then((r) => { if (alive) setPreview(r); })
      .catch((e) => { if (alive) setError(errorText(e)); });
    return () => { alive = false; };
  }, [api, pid, open, detail, pack]);
  if (!open || !detail) return null;
  const label = preview?.pack?.label || pack?.label;
  const submit = async () => {
    setBusy(true);
    try {
      await api.nominateKnowledge(pid, detail.id, { industry_pack: preview.pack?.name || pack?.name, sanitized_text: preview.sanitized_text, confirm_general: true });
      toast('已提交，等待管理员审核', 'upload');
      onClose();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      onClose={onClose}
      icon="upload"
      title={nominateTitle(label)}
      w={620}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={!preview || !general || busy} onClick={submit}>提交提名</Button></>}
    >
      {error ? <Note tone="red">{error}</Note> : null}
      {!preview && !error ? <div className="sm muted">正在做脱敏检查…</div> : null}
      {preview ? (
        <>
          <div className="mr-sec" style={{ marginTop: 0 }}>脱敏后的条目</div>
          <div className="mr-quote">
            {markSegments(preview.sanitized_text, preview.highlights).map((s, i) => (s.mark ? <span key={i} className="kn-mark">{s.v}</span> : <span key={i}>{s.v}</span>))}
          </div>
          <div className="mr-sec">自动脱敏检查</div>
          <div className="tbl-wrap">
            <table className="tbl compact">
              <thead><tr><th>检查项</th><th>结果</th><th>处理</th></tr></thead>
              <tbody>
                {checkRows(preview.checks).map((c) => (
                  <tr key={c.label}><td>{c.label}</td><td><Chip tone={c.result[1]} sm>{c.result[0]}</Chip></td><td>{c.action}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mr-sec">人工审核</div>
          <div><Check on={general} onChange={setGeneral} label="我确认这条知识在行业内通用" /></div>
        </>
      ) : null}
    </Modal>
  );
}

// 冲突检查明细（K-06）：按类型分组，口径冲突可以直接提名回流
export function ConflictsModal({ open, check, onClose, onNominate }) {
  if (!open || !check) return null;
  const groups = ['scope_conflict', 'duplicate', 'broken_ref']
    .map((kind) => ({ kind, items: (check.conflicts || []).filter((c) => c.kind === kind) }))
    .filter((g) => g.items.length);
  const clean = !(check.duplicates || check.broken_refs);
  return (
    <Modal
      open
      onClose={onClose}
      icon="shield"
      title="知识冲突检查"
      sub={checkModalSub(check)}
      foot={<Button kind="primary" onClick={onClose}>知道了</Button>}
    >
      <div className="col sm" style={{ gap: 10 }}>
        {groups.map((g) => g.items.map((c, i) => (
          <div className="row g12" key={c.id || `${g.kind}-${i}`}>
            <span style={{ color: 'var(--amber2)' }}><Icon name="alert" size={14} /></span>
            <div className="grow">
              {i === 0 ? <b>{conflictKindTitle(g.kind)} {g.items.length}</b> : null}
              <div className="muted">{conflictDetail(c)}</div>
            </div>
            {g.kind === 'scope_conflict' && c.project_entry?.id ? <Button size="xs" kind="soft" onClick={() => onNominate?.(c.project_entry.id)}>提名回流</Button> : null}
          </div>
        )))}
        {!groups.length ? (
          <div className="row g12"><span style={{ color: 'var(--blue)' }}><Icon name="check" size={14} /></span><div className="grow">没有口径冲突</div></div>
        ) : null}
        <div className="row g12"><span style={{ color: 'var(--blue)' }}><Icon name="check" size={14} /></span><div className="grow">{materialsLine(check)}</div></div>
        <div className="row g12">
          <span style={{ color: clean ? 'var(--blue)' : 'var(--amber2)' }}><Icon name={clean ? 'check' : 'alert'} size={14} /></span>
          <div className="grow">{countsLine(check)}</div>
        </div>
      </div>
    </Modal>
  );
}
