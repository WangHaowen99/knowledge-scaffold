// 调整建模蓝图（设计稿 wb-blueprint #enBpM）：要解决的问题、分段规划（拖动排序、合并、拆分、排除、改名、新增）、术语口径、待你回答
// BP-01 读、BP-02 整份分段列表保存；按章节拆分要读资料，走 S-20 命令。
// 要解决的问题就是项目的建模目标（F4b）：在这里改目标走 P-05；资料和目标对不上的那个问题放在目标下面答
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../../app/context.js';
import { toast } from '../../../app/toast.jsx';
import { errorText } from '../../../api/errors.js';
import {
  Button, Icon, Menu, Modal, Note, cx, useOutside,
} from '../../../ui/index.jsx';
import { pageCommand } from './parts.jsx';
import {
  addSlice, blueprintPayload, depAfter, depLabel, editableSlices, goalQuestion, isDirty, mergeSlices, moveSlice, orderLabel,
  renameSlice, toggleExcluded,
} from './model.js';
import '../../../styles/materials.css';

function initEdit(bp) {
  return {
    slices: editableSlices(bp.slices),
    terms: Object.fromEntries((bp.terms?.ambiguous || []).map((t) => [t.id, t.chosen])),
    answers: Object.fromEntries((bp.questions || []).map((q) => [q.id, q.answer?.option_id || null])),
  };
}

function OrderTag({ slice, index }) {
  const o = orderLabel(slice, index);
  if (o.tone === 'blue') return <span className="chip blue sm">{o.text}</span>;
  if (o.tone === 'amber') return <span className="chip amber sm">{o.text}</span>;
  return <span className="xs faint">{o.text}</span>;
}

function GripMenu({ index, total, onMove, onClose }) {
  const ref = useRef(null);
  useOutside(ref, onClose);
  const items = [
    { label: '上移', icon: 'chevu', disabled: index === 0, onClick: () => onMove(index - 1) },
    { label: '下移', icon: 'chevd', disabled: index === total - 1, onClick: () => onMove(index + 1) },
    '-',
    { label: '移到最前', disabled: index === 0, onClick: () => onMove(0) },
    { label: '移到最后', disabled: index === total - 1, onClick: () => onMove(total - 1) },
  ];
  return <div ref={ref} className="gmenu"><Menu items={items} onPick={onClose} /></div>;
}

export function BlueprintDialog({ onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [bp, setBp] = useState(null);
  const [error, setError] = useState(null);
  const [edit, setEdit] = useState(null);
  const [sel, setSel] = useState([]);
  const [renaming, setRenaming] = useState(null);
  const [goalDraft, setGoalDraft] = useState(null);
  const [drag, setDrag] = useState(null);
  const [over, setOver] = useState(null);
  const [grip, setGrip] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api.blueprint(pid).then((b) => { setBp(b); setEdit(initEdit(b)); setSel([]); setRenaming(null); }).catch(setError), [api, pid]);
  useEffect(() => { load(); }, [load]);

  const slices = edit?.slices || [];
  const setSlices = (fn) => setEdit((e) => ({ ...e, slices: fn(e.slices) }));
  const toggleSel = (id) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  const nameOf = (id) => slices.find((s) => s.id === id)?.name || '';

  const commitRename = () => {
    if (!renaming) return null;
    const next = renaming.value.trim();
    if (next && next !== nameOf(renaming.id)) {
      setSlices((l) => renameSlice(l, renaming.id, next));
      toast(`已改名：${next}`, 'edit');
    }
    setRenaming(null);
    return next;
  };
  const merge = () => {
    const { list, merged } = mergeSlices(slices, sel);
    if (!merged) return;
    setSlices(() => list);
    setSel([merged.id]);
    setRenaming({ id: merged.id, value: merged.name, original: merged.name });
    toast('已合并', 'merge');
  };
  const split = () => {
    if (sel.length !== 1) { toast('选一段后按章节拆分', 'info'); return; }
    const s = slices.find((x) => x.id === sel[0]);
    pageCommand(api, pid, {
      command: '/ontology-research', args: { split_slice: s.id, by: 'chapter' }, text: `把分段「${s.name}」按章节拆分`,
      page: 'wb-blueprint', label: '调整建模蓝图', done: '已发给助手',
    });
  };
  const exclude = () => {
    if (!sel.length) { toast('先勾选要排除的分段', 'info'); return; }
    const restoring = slices.filter((s) => sel.includes(s.id)).every((s) => s.excluded);
    setSlices((l) => toggleExcluded(l, sel));
    toast(restoring ? '已恢复' : '已排除', 'ban');
  };
  const rename = () => {
    if (sel.length !== 1) { toast('勾选一段再改名', 'info'); return; }
    const s = slices.find((x) => x.id === sel[0]);
    setRenaming({ id: s.id, value: s.name, original: s.name });
  };
  const add = () => {
    const { list, added } = addSlice(slices);
    setSlices(() => list);
    setRenaming({ id: added.id, value: added.name, original: added.name, fresh: true });
  };
  const move = (from, to) => setSlices((l) => moveSlice(l, from, to));

  // 改目标就是改项目的建模目标（P-05）；蓝图里不另存一份
  const saveGoal = async () => {
    const goal = (goalDraft || '').trim();
    if (!goal || busy) return;
    setBusy(true);
    try {
      await api.patchProject(pid, { goal });
      // 目标问题还没答：改了目标就是答了它（新目标作为回答），蓝图才能确认
      if (goalAsk && !goalAsk.answer) {
        await api.saveBlueprint(pid, { base_revision: bp.revision, mode: 'draft', answers: [{ question_id: goalAsk.id, custom_text: goal }] });
      }
      setGoalDraft(null);
      toast('建模目标已更新', 'check');
      await load();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const save = async (mode) => {
    if (!bp || busy) return;
    let current = edit;
    if (renaming?.value.trim()) current = { ...edit, slices: renameSlice(edit.slices, renaming.id, renaming.value) };
    const body = blueprintPayload(bp, current, mode);
    if (!isDirty(body)) {
      toast('蓝图没有改动', 'info');
      if (mode === 'adjust') onClose();
      return;
    }
    setBusy(true);
    try {
      const r = await api.saveBlueprint(pid, body);
      if (mode === 'adjust') {
        toast(`蓝图已更新为${r.version_label}`, 'check');
        onClose();
      } else {
        toast('草案已保存', 'download');
        setBp(r);
        setEdit(initEdit(r));
        setRenaming(null);
      }
    } catch (e) {
      toast(errorText(e), 'alert');
      if (e.code === 'revision_conflict') load();
    } finally {
      setBusy(false);
    }
  };

  const live = slices.filter((s) => !s.excluded).length;
  const terms = bp?.terms || { aligned: [], ambiguous: [] };
  // 目标问题放在「要解决的问题」下面答，其余问题照旧在「待你回答」
  const goalAsk = goalQuestion(bp?.questions || []);
  const questions = (bp?.questions || []).filter((q) => q !== goalAsk);
  return (
    <Modal
      open onClose={onClose} icon="layers" title="调整建模蓝图" w={1100}
      sub={bp?.version_label || '建模蓝图'}
      foot={(
        <>
          <Button kind="ghost" icon="download" disabled={!bp || busy} onClick={() => save('draft')}>保存为草案</Button>
          <span className="sp" />
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" icon="check" disabled={!bp || busy} onClick={() => save('adjust')}>保存调整</Button>
        </>
      )}
    >
      {error ? <Note tone="red">{errorText(error)}</Note> : null}
      {!bp && !error ? <div className="muted sm">正在读取建模蓝图…</div> : null}
      {bp && edit ? (
        <div style={{ marginTop: -20 }}>
          <div className="en-bp-sec">
            <h4>
              <Icon name="target" size={14} />要解决的问题<span className="sp" />
              {goalDraft == null ? <Button size="xs" kind="ghost" icon="edit" onClick={() => setGoalDraft(bp.goal_md || '')}>修改</Button> : null}
            </h4>
            {goalDraft == null ? (
              <div className="sm" style={{ lineHeight: 1.7, color: 'var(--ink2)' }}>{bp.goal_md}</div>
            ) : (
              <div className="row g8">
                <div className="inp focus grow" style={{ height: 32, padding: '0 8px' }}>
                  <input
                    autoFocus value={goalDraft} maxLength={500}
                    onChange={(e) => setGoalDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') { e.stopPropagation(); setGoalDraft(null); }
                      if (e.key === 'Enter' && !e.nativeEvent.isComposing) saveGoal();
                    }}
                  />
                </div>
                <Button size="sm" onClick={() => setGoalDraft(null)}>取消</Button>
                <Button size="sm" kind="primary" disabled={busy || !goalDraft.trim()} onClick={saveGoal}>保存</Button>
              </div>
            )}
            {goalAsk ? (
              <div className="en-q mt8" key={goalAsk.id}>
                <div className="b sm">{goalAsk.title}</div>
                {goalAsk.background ? <div className="xs muted mt4">{goalAsk.background}</div> : null}
                <div className="os">
                  {goalAsk.options.map((o, k) => (
                    <span key={o.id} className={cx(edit.answers[goalAsk.id] === o.id && 'on')} onClick={() => setEdit((e) => ({ ...e, answers: { ...e.answers, [goalAsk.id]: e.answers[goalAsk.id] === o.id ? null : o.id } }))}>
                      <b>{String.fromCharCode(65 + k)}</b>{o.label}
                    </span>
                  ))}
                  {/* 助手没给建议改写时，另一个选择就是自己改目标 */}
                  {!goalAsk.suggested_goal && !goalAsk.answer ? (
                    <span onClick={() => setGoalDraft(bp.goal_md || '')}><b>{String.fromCharCode(65 + goalAsk.options.length)}</b>改目标</span>
                  ) : null}
                </div>
              </div>
            ) : null}
          </div>

          <div className="en-bp-sec">
            <h4>
              <Icon name="layers" size={14} />分段规划 <span className="n">{live} 段</span><span className="sp" />
              <Button size="xs" icon="merge" disabled={sel.length < 2} onClick={merge}>{sel.length >= 2 ? `合并 ${sel.length} 段` : '合并'}</Button>
              <Button size="xs" icon="split" onClick={split}>拆分</Button>
              <Button size="xs" icon="ban" onClick={exclude}>排除</Button>
              <Button size="xs" icon="edit" onClick={rename}>改名</Button>
              <Button size="xs" icon="plus" kind="ghost" onClick={add}>新增</Button>
            </h4>
            <div className="en-sl hd"><span /><span /><span>分段</span><span>中心流程</span><span>规模</span><span>依赖 · 顺序</span><span /></div>
            {slices.map((s, i) => {
              const on = sel.includes(s.id);
              const isRen = renaming?.id === s.id;
              return (
                <div
                  key={s.id}
                  className={cx('en-sl', on && 'sel', s.excluded && 'ex', drag === i && 'drag', drag != null && over === i && drag !== i && 'over')}
                  draggable={!isRen}
                  onDragStart={(e) => { setDrag(i); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', s.id); }}
                  onDragOver={(e) => { if (drag == null) return; e.preventDefault(); e.stopPropagation(); if (over !== i) setOver(i); }}
                  onDrop={(e) => { e.preventDefault(); e.stopPropagation(); if (drag != null) move(drag, i); setDrag(null); setOver(null); }}
                  onDragEnd={() => { setDrag(null); setOver(null); }}
                >
                  <span className={cx('chk', on && 'on')} onClick={() => toggleSel(s.id)} />
                  <span className="ix">{i + 1}</span>
                  <div style={{ minWidth: 0 }}>
                    {isRen ? (
                      <>
                        <div className="row g4">
                          <div className="inp focus" style={{ height: 28, flex: 1, padding: '0 8px' }}>
                            <input
                              autoFocus value={renaming.value} maxLength={40}
                              onChange={(e) => setRenaming((r) => ({ ...r, value: e.target.value }))}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' && !e.nativeEvent.isComposing) commitRename();
                                if (e.key === 'Escape') { e.stopPropagation(); setRenaming(null); }
                              }}
                            />
                          </div>
                          <Button icon="check" size="xs" kind="primary" title="确定" disabled={!renaming.value.trim()} onClick={commitRename} />
                          <Button icon="x" size="xs" kind="ghost" title="取消" onClick={() => setRenaming(null)} />
                        </div>
                        <div className="xs muted mt4">{renaming.fresh ? '新分段' : `改名中 · 原名「${renaming.original}」`}</div>
                      </>
                    ) : <div className="nm b ell" onDoubleClick={() => setRenaming({ id: s.id, value: s.name, original: s.name })} title="双击改名">{s.name}</div>}
                  </div>
                  <div className="muted ell">{s.core_flow || '—'}</div>
                  <div className="xs num" style={{ lineHeight: 1.5 }}>
                    {s.scale?.objects || s.scale?.rules ? <>对象 ≈{s.scale.objects}<br />规则 ≈{s.scale.rules}</> : <span className="faint">保存后估算</span>}
                  </div>
                  <div className="col" style={{ gap: 3, alignItems: 'flex-start' }}>
                    <span className="xs muted">
                      依赖 <b style={{ color: 'var(--ink)' }}>{depLabel(s, slices)}</b>
                      {depAfter(s, slices) ? <span title="依赖的分段排在后面" style={{ color: 'var(--amber)', marginLeft: 4 }}><Icon name="alert" size={12} /></span> : null}
                    </span>
                    <OrderTag slice={s} index={i} />
                  </div>
                  <span className="grip" title="拖动排序" onMouseDown={(e) => e.stopPropagation()} onClick={() => setGrip(grip === s.id ? null : s.id)}><Icon name="more" size={14} /></span>
                  {grip === s.id ? <GripMenu index={i} total={slices.length} onMove={(to) => move(i, to)} onClose={() => setGrip(null)} /> : null}
                </div>
              );
            })}
          </div>

          <div className="en-bp-sec">
            <h4><Icon name="book" size={14} />术语与口径 <span className="n">{terms.aligned.length} 条已对齐 · {terms.ambiguous.length} 条有歧义</span></h4>
            <div className="row wrap" style={{ gap: 6 }}>
              {terms.aligned.map((t) => <span key={t} className="chip blue sm"><Icon name="check" />{t}</span>)}
            </div>
            {terms.ambiguous.length ? (
              <div className="row wrap mt12" style={{ gap: 10 }}>
                {terms.ambiguous.map((t) => (
                  <div className="en-term" key={t.id}>
                    <b>{t.variants.join(' / ')}</b><span className="xs muted">统一叫：</span>
                    {t.variants.map((v) => (
                      <span key={v} className={cx('o', edit.terms[t.id] === v && 'on')} onClick={() => setEdit((e) => ({ ...e, terms: { ...e.terms, [t.id]: v } }))}>{v}</span>
                    ))}
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          <div className="en-bp-sec">
            <h4><Icon name="hand" size={14} />待你回答 <span className="n">{questions.length} 个问题</span></h4>
            {questions.length ? (
              <div className="grid3" style={{ gap: 10 }}>
                {questions.map((q) => (
                  <div className="en-q" key={q.id}>
                    <div className="b sm">{q.title}</div>
                    <div className="xs muted mt4">{q.background}</div>
                    <div className="os">
                      {q.options.map((o, k) => (
                        <span key={o.id} className={cx(edit.answers[q.id] === o.id && 'on')} onClick={() => setEdit((e) => ({ ...e, answers: { ...e.answers, [q.id]: e.answers[q.id] === o.id ? null : o.id } }))}>
                          <b>{String.fromCharCode(65 + k)}</b>{o.label}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            ) : <div className="xs muted">没有要你回答的问题</div>}
          </div>

        </div>
      ) : null}
    </Modal>
  );
}
