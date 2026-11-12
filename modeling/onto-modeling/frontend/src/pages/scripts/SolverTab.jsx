// 「求解契约」页签：一份求解契约一张卡（SV-01），表单生成 / 手动编辑，即时检查（SV-03），保存（SV-02，带 base_revision），
// 验证回执（SV-04）。这份契约在助手写入范围里时整卡只读（mr-ro），保存与放弃禁用；范围外照常编辑，保存即生效。
// 保存后往工作台对话发「生成评估器」；手动编辑改回表单要「按模板重新生成」（都发命令，S-20）。
// 模式只显示当前状态（R10）：表单生成时只有「转为手动编辑」，手动编辑时只有「按模板重新生成」，两个都先确认。
import { useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import {
  diffContracts, draftKey as draftKeyOf, engineCapabilities, findProblemFamily, isEnabled, newDraftItem, problemFamily, regenerate,
  renderedSidecars, setEnabled, setEngineCapabilities, setField, setOperation, setOptionLabel, setProblemFamily, setSidecar,
  setTemplateParam, toManual, toPayload,
} from '../../lib/solver-contract-model.js';
import { sendHint } from '../../state/commandTask.js';
import {
  Area, Button, Card, Chip, Empty, Field, Icon, Inp, Modal, Note, Select, Toggle, cx,
} from '../../ui/index.jsx';
import { ScopeLockBar } from '../plugin/parts.jsx';
import SolverValidationCard from './SolverValidationCard.jsx';
import {
  SIDECAR_FILES, baseOperations, checkLines, contractLocked, contractProblems, contractTitle, draftOf, evaluatorCommand,
  evaluatorOptions, evaluatorTarget, familyOptions, fieldProblem, itemKey, lockText, removeUnit, revisionTag, saveErrorView,
  saveState, savedToast, setUnit, templateCommand, toggleCapability,
} from './scriptsModel.js';

const Err = ({ p }) => (p ? <span style={{ color: p.severity === 'warning' ? 'var(--amber)' : 'var(--red)' }}>{p.message}</span> : null);

function CheckLine({ l }) {
  return (
    <div className={cx('row g4 scr-cl', `t-${l.tone}`)}>
      {l.icon === 'spin' ? <span className="spin" /> : <Icon name={l.icon} size={14} />}
      <span>{l.text}</span>
    </div>
  );
}

// 字段单位：表格里直接改；清空的行保存时去掉
function Units({ item, problems, onChange }) {
  const rows = Array.isArray(item.binding.units) ? item.binding.units : [];
  const set = (i, k, v) => onChange(setField(item, 'units', setUnit(rows, i, k, v)));
  const p = fieldProblem(problems, item.uid, 'units');
  return (
    <>
      <div className="mr-sec row">
        <span className="grow">字段单位</span>
        <span className="scr-link" onClick={() => onChange(setField(item, 'units', [...rows, { field: '', unit: '', note: '' }]))}>＋ 添加字段</span>
      </div>
      <div className="tbl-wrap">
        <table className="tbl compact scr-units">
          <thead><tr><th style={{ width: '46.5%' }}>字段（英文名）</th><th style={{ width: '20%' }}>单位</th><th>说明</th></tr></thead>
          <tbody>
            {rows.length ? rows.map((u, i) => (
              <tr key={i}>
                <td><input value={u.field ?? ''} placeholder="如 batch_qty" onChange={(e) => set(i, 'field', e.target.value)} /></td>
                <td><input value={u.unit ?? ''} placeholder="单位" onChange={(e) => set(i, 'unit', e.target.value)} /></td>
                <td className="scr-note">
                  <input value={u.note ?? ''} placeholder="说明" onChange={(e) => set(i, 'note', e.target.value)} />
                  <button type="button" className="scr-del" title="删掉这一行" onClick={() => onChange(setField(item, 'units', removeUnit(rows, i)))}><Icon name="x" size={12} /></button>
                </td>
              </tr>
            )) : <tr><td colSpan={3} className="muted">还没有字段单位</td></tr>}
          </tbody>
        </table>
      </div>
      {p ? <div className="xs mt4"><Err p={p} /></div> : null}
    </>
  );
}

// 引擎能力：已选的蓝色，问题族目录里还能选的虚线，点一下切换
function Capabilities({ item, families, onChange }) {
  const caps = engineCapabilities(item.binding);
  const fam = findProblemFamily(families, problemFamily(item.binding));
  const allowed = fam?.capabilities || [];
  const toggle = (c) => onChange(setEngineCapabilities(item, toggleCapability(caps, c)));
  return (
    <div className="inp scr-caps">
      {caps.map((c) => (
        <Chip key={c} tone={allowed.includes(c) || !fam ? 'blue' : 'red'} sm onClick={() => toggle(c)}>{c}</Chip>
      ))}
      {allowed.filter((c) => !caps.includes(c)).map((c) => (
        <Chip key={c} tone="line" sm icon="plus" onClick={() => toggle(c)}>{c}</Chip>
      ))}
    </div>
  );
}

export default function SolverTab({ view, lock, tasks, onSaved, reload }) {
  const { api } = useApp();
  const { pid, reloadScope } = useProject();
  const [base, setBase] = useState(null);
  const [items, setItems] = useState([]);
  const [selUid, setSelUid] = useState(null);
  const [live, setLive] = useState(null);
  const [summary, setSummary] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState(null);
  const [newer, setNewer] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [templateOpen, setTemplateOpen] = useState(false);
  const [fileRole, setFileRole] = useState('entry');
  const [previewRole, setPreviewRole] = useState(null);

  const diff = base ? diffContracts(baseOperations(base), items) : { dirty: false, added: [], changed: [], removed: [] };
  const dirty = diff.dirty;
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;
  const resetNext = useRef(true);
  const baseRevision = useRef(null);

  // 服务端视图变了：没有草稿就跟着刷新；有草稿只提示「有新修订」
  useEffect(() => {
    if (!view) return;
    if (resetNext.current || !dirtyRef.current) {
      resetNext.current = false;
      baseRevision.current = view.revision;
      setBase(view);
      setItems(draftOf(view));
      setNewer(false);
    } else if (view.revision !== baseRevision.current) setNewer(true);
  }, [view]);

  const key = draftKeyOf(items);
  // 即时检查（SV-03，不落盘）：停手 0.4 秒后按保存的同一套规则查草稿，顺带拿模板生成的附属文件
  useEffect(() => {
    if (!base || !items.length) { setLive(null); return undefined; }
    let alive = true;
    const t = setTimeout(async () => {
      try {
        const r = await api.checkSolverContract(pid, { contract: { operations: toPayload(items) } });
        if (alive) setLive({ key, ...r });
      } catch {
        if (alive) setLive({ key, rejected: [], findings: [], rendered: [], evaluator: null, failed: true });
      }
    }, 400);
    return () => { alive = false; clearTimeout(t); };
  }, [key, base, api, pid]); // items 由 key 代表


  if (!view || !base) return <div className="card"><div className="card-b muted sm">正在读取…</div></div>;

  const ops = base.available_operations || [];
  const families = base.problem_families || [];
  const current = items.find((i) => i.uid === selUid) || items.find((i) => isEnabled(i.binding)) || items[0] || null;
  const problems = contractProblems(items, base, current);
  const update = (next) => { setItems((list) => list.map((i) => (i.uid === next.uid ? next : i))); setSaveErr(null); };
  const addNew = () => {
    const free = ops.map((o) => o.name).find((n) => !items.some((i) => i.binding.operation === n)) || '';
    const it = newDraftItem(base.template, free, items);
    setItems((list) => [...list, it]);
    setSelUid(it.uid);
  };
  const refresh = () => { resetNext.current = true; setSaveErr(null); setSummary(''); reload(); };

  if (!current) {
    return (
      <div className="card">
        <Empty icon="cpu" title="还没有求解契约" actions={<Button kind="primary" icon="plus" onClick={addNew}>新的求解契约</Button>} />
      </div>
    );
  }

  const title = contractTitle(current, ops);
  const enabled = isEnabled(current.binding);
  const locked = contractLocked(lock, current);
  const itemOf = (uid) => items.find((i) => i.uid === uid);
  const touchedLocked = [...diff.added, ...diff.changed].some((x) => contractLocked(lock, itemOf(x.uid)))
    || diff.removed.some((r) => contractLocked(lock, { origin: r.solverKey }));
  const ss = saveState({ dirty, locked: locked || touchedLocked, problems, live, draftKey: key, saving });
  const idx = items.indexOf(current);
  const rendered = live && live.key === key ? live.rendered?.[idx] || null : null;
  const lines = checkLines({ view: base, item: current, dirty, live, draftKey: key, problems });
  const savedOp = baseOperations(base).find((op) => op.solver_key === current.origin);
  const params = current.params;
  const fp = (field) => fieldProblem(problems, current.uid, field);
  const tpl = templateCommand(itemKey(current), title);

  // 这次才转成手动、还没保存的：按模板重新生成就是在本地改回表单；已保存为手动的发命令
  const savedManual = !!savedOp && !savedOp.managed;
  const openTemplate = () => {
    if (savedManual && dirty) { toast('先保存或放弃修改，再按模板重新生成', 'info'); return; }
    setTemplateOpen(true);
  };
  const confirmTemplate = () => {
    setTemplateOpen(false);
    if (savedManual) sendTemplate();
    else update(regenerate(current));
  };
  const confirmManual = () => {
    update(toManual(current, renderedSidecars(current, rendered) || current.sidecars || {}));
    setFileRole('entry');
    setManualOpen(false);
  };
  const sendTemplate = () => {
    if (dirty) { toast('先保存或放弃修改，再按模板重新生成', 'info'); return; }
    tasks.send(tpl);
  };
  const discard = () => { setItems(draftOf(base)); setSaveErr(null); setSummary(''); };
  const save = async () => {
    const target = evaluatorTarget(diff, items, current);
    setSaving(true);
    setSaveErr(null);
    try {
      const res = await api.saveSolverContract(pid, { base_revision: base.revision, contract: { operations: toPayload(items) }, ...(summary.trim() ? { summary: summary.trim().slice(0, 80) } : {}) });
      setSelUid(`saved:${itemKey({ binding: current.binding })}`);
      setSummary('');
      resetNext.current = true;
      onSaved(res);
      if (target) {
        const sent = await tasks.send(evaluatorCommand(String(target.binding.solver_key).trim(), contractTitle(target, ops)));
        toast(sent ? `${savedToast(res)}，正在生成评估器` : savedToast(res), 'check');
      } else toast(savedToast(res), 'check');
    } catch (e) {
      setSaveErr(saveErrorView(e));
      if (e?.code === 'write_scope_locked') reloadScope?.();
    } finally {
      setSaving(false);
    }
  };

  const others = items.filter((i) => i !== current);
  const changedUids = new Set([...diff.added, ...diff.changed].map((x) => x.uid));
  const opOptions = ops.map((o) => ({ value: o.name, label: o.name }));
  if (current.binding.operation && !opOptions.some((o) => o.value === current.binding.operation)) {
    opOptions.push({ value: current.binding.operation, label: `${current.binding.operation}（不存在）` });
  }
  const preview = previewRole ? renderedSidecars(current, rendered)?.[previewRole] : null;

  return (
    <>
      <div className="mr-2c w">
        <div className="card">
          <div className="card-h">
            <span style={{ color: 'var(--blue)' }}><Icon name="cpu" /></span>
            <h3>{title}</h3>
            {enabled ? <Chip tone="blue" sm>已启用</Chip> : <Chip tone="gray" sm icon="ban">已停用</Chip>}
            {revisionTag(base, dirty) ? <span className="tag">{revisionTag(base, dirty)}</span> : null}
            <span className="sp" />
            <span className="row g4 sm">对外提供求解能力 <Toggle on={enabled} disabled={locked} onChange={(v) => update(setEnabled(current, v))} /></span>
          </div>
          <div className="card-b">
            {locked ? <ScopeLockBar lock={lock} text={lockText(lock, title)} /> : null}
            <div className={cx(locked && 'mr-ro')}>
              <div className="row mb12">
                <Chip tone="line" sm icon={current.managed ? 'list' : 'edit'}>{current.managed ? '表单生成' : '手动编辑'}</Chip>
                <span className="sp" />
                {current.managed
                  ? <Button size="xs" kind="ghost" onClick={() => setManualOpen(true)}>转为手动编辑</Button>
                  : <Button size="xs" kind="ghost" data-pop={savedManual ? sendHint(tpl.text) : undefined} disabled={savedManual && tasks.sending} onClick={openTemplate}>按模板重新生成</Button>}
              </div>
              <div className="grid2" style={{ gap: 12 }}>
                <Field label="绑定的操作" hint={fp('operation') ? <Err p={fp('operation')} /> : null}>
                  <Select value={current.binding.operation || ''} options={opOptions} icon="flow" onChange={(v) => update(setOperation(current, v))} />
                </Field>
                <Field label="求解标识" hint={fp('solver_key') ? <Err p={fp('solver_key')} /> : '小写字母开头，只含小写字母、数字、下划线'}>
                  <Inp mono placeholder="solver_key" value={current.binding.solver_key || ''} error={!!fp('solver_key')} onChange={(e) => update(setField(current, 'solver_key', e.target.value))} />
                </Field>
                <Field label="问题族" hint={fp('problem_family') ? <Err p={fp('problem_family')} /> : null}>
                  <Select value={problemFamily(current.binding)} options={familyOptions(families, problemFamily(current.binding))} onChange={(v) => update(setProblemFamily(current, v))} />
                </Field>
                <Field label="引擎能力" hint={fp('engine_capabilities') ? <Err p={fp('engine_capabilities')} /> : null}>
                  <Capabilities item={current} families={families} onChange={update} />
                </Field>
              </div>
              <Units item={current} problems={problems} onChange={update} />
              {current.managed ? (
                <>
                  <div className="grid2 mt12" style={{ gap: 12 }}>
                    <Field label="评估器">
                      <Select value={params.evaluator_path || ''} options={evaluatorOptions(base, current)} onChange={(v) => update(setTemplateParam(current, 'evaluator_path', v))} />
                    </Field>
                    <Field label="两个选项的显示名称" hint={fp('display_mapping') ? <Err p={fp('display_mapping')} /> : null}>
                      <div className="row">
                        <Inp className="grow" value={params.display_mapping?.OPTION_0 || ''} placeholder="选项一" error={!!fp('display_mapping')} onChange={(e) => update(setOptionLabel(current, 'OPTION_0', e.target.value))} />
                        <Inp className="grow" value={params.display_mapping?.OPTION_1 || ''} placeholder="选项二" error={!!fp('display_mapping')} onChange={(e) => update(setOptionLabel(current, 'OPTION_1', e.target.value))} />
                      </div>
                    </Field>
                    <Field label="结果标题" hint={fp('title') ? <Err p={fp('title')} /> : null}>
                      <Inp value={params.title || ''} placeholder="求解结果展示时的标题" onChange={(e) => update(setTemplateParam(current, 'title', e.target.value))} />
                    </Field>
                    <Field label="结果文字（可选）" hint={fp('text_presentation') ? <Err p={fp('text_presentation')} /> : '两项须同时填写'}>
                      <div className="row">
                        <Inp className="grow" value={params.process_name || ''} placeholder="结果名称" error={!!fp('text_presentation')} onChange={(e) => update(setTemplateParam(current, 'process_name', e.target.value))} />
                        <Inp className="grow" value={params.stream_name || ''} placeholder="选项所属字段" error={!!fp('text_presentation')} onChange={(e) => update(setTemplateParam(current, 'stream_name', e.target.value))} />
                      </div>
                    </Field>
                  </div>
                  <div className="row mt12 wrap g4">
                    {SIDECAR_FILES.map((f) => (
                      <span key={f.role} className="mr-file scr-file" title="只读" onClick={() => setPreviewRole(f.role)}>
                        <Icon name="lock" size={12} /><span className="mono">{f.file}</span>
                      </span>
                    ))}
                  </div>
                </>
              ) : (
                <>
                  <div className="row mt12 wrap g4">
                    {SIDECAR_FILES.map((f) => (
                      <button type="button" key={f.role} className={cx('mr-file scr-file', fileRole === f.role && 'on', fp(f.role) && 'bad')} onClick={() => setFileRole(f.role)}>
                        <Icon name="file" size={12} /><span className="mono">{f.file}</span>
                      </button>
                    ))}
                  </div>
                  <Area
                    className="mt8 scr-area"
                    value={current.sidecars?.[fileRole] ?? ''}
                    spellCheck={false}
                    textareaStyle={{ height: 220 }}
                    onChange={(e) => update(setSidecar(current, fileRole, e.target.value))}
                  />
                  {fp(fileRole) ? <div className="xs mt4"><Err p={fp(fileRole)} /></div> : null}
                </>
              )}
            </div>
          </div>
        </div>
        <div className="col" style={{ gap: 16 }}>
          <Card title="检查" icon="shieldok">
            <div className="col sm" style={{ gap: 8 }}>{lines.map((l, i) => <CheckLine key={i} l={l} />)}</div>
            {newer && !saveErr ? (
              <Note tone="amber" className="mt12">
                <div className="row">
                  <span className="grow">求解契约已在别处更新，请先刷新</span>
                  <Button size="xs" icon="refresh" onClick={refresh}>刷新</Button>
                </div>
              </Note>
            ) : null}
            {saveErr ? (
              <Note tone={saveErr.kind === 'conflict' || saveErr.kind === 'locked' ? 'amber' : 'red'} className="mt12">
                <div className="row" style={{ alignItems: 'flex-start' }}>
                  <div className="grow">
                    {saveErr.text}
                    {(saveErr.findings || []).map((f, i) => <div key={i} className="xs mt4">· {f.message}</div>)}
                  </div>
                  {saveErr.kind === 'conflict' ? <Button size="xs" icon="refresh" onClick={refresh}>刷新</Button> : null}
                </div>
              </Note>
            ) : null}
            <div className="mt12">
              <Field label="改动说明">
                <Area placeholder="如：批次量单位改为箱" maxLength={80} minHeight={60} textareaStyle={{ height: 40 }} value={summary} disabled={locked} onChange={(e) => setSummary(e.target.value)} />
              </Field>
            </div>
            <div className="row mt12">
              <Button size="sm" kind="ghost" disabled={!dirty || locked || saving} onClick={discard}>放弃修改</Button>
              <span className="sp" />
              <Button size="sm" kind="primary" disabled={ss.disabled} title={ss.title || undefined} onClick={save}>保存求解契约</Button>
            </div>
          </Card>
          <SolverValidationCard view={base} dirty={dirty} />
          <Card title="其他求解契约" icon="list">
            {others.map((it) => (
              <div key={it.uid} className="row sm scr-other" onClick={() => setSelUid(it.uid)}>
                <span className="grow">{contractTitle(it, ops)}</span>
                {changedUids.has(it.uid) ? <Chip tone="amber" sm dot>有修改</Chip> : null}
                {isEnabled(it.binding) ? <Chip tone="blue" sm>已启用</Chip> : <Chip tone="gray" sm icon="ban">已停用</Chip>}
              </div>
            ))}
            {!others.length ? <div className="xs muted">只有这一份求解契约。</div> : null}
            <div className="mt12"><Button size="sm" icon="plus" onClick={addNew}>新的求解契约</Button></div>
          </Card>
        </div>
      </div>
      <Modal
        open={manualOpen}
        onClose={() => setManualOpen(false)}
        icon="edit"
        title="转为手动编辑？"
        foot={<><Button onClick={() => setManualOpen(false)}>取消</Button><Button kind="primary" onClick={confirmManual}>转为手动编辑</Button></>}
      >
        <div className="sm">转换后表单不再生效；改回表单要「按模板重新生成」，手动修改会被覆盖</div>
      </Modal>
      <Modal
        open={templateOpen}
        onClose={() => setTemplateOpen(false)}
        icon="refresh"
        title="按模板重新生成？"
        foot={<><Button onClick={() => setTemplateOpen(false)}>取消</Button><Button kind="primary" onClick={confirmTemplate}>按模板重新生成</Button></>}
      >
        <div className="sm">手动修改会被覆盖</div>
      </Modal>
      <Modal
        open={!!previewRole}
        onClose={() => setPreviewRole(null)}
        icon="file"
        title={SIDECAR_FILES.find((f) => f.role === previewRole)?.file || ''}
        sub="只读"
        w={640}
      >
        <div className="code scr-preview">{preview ?? '参数还在生成，稍等片刻再看。'}</div>
      </Modal>
    </>
  );
}
