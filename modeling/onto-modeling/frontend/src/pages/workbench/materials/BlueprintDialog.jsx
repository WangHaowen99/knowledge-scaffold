// 建模蓝图（设计稿 wb-blueprint #enBpM）：要解决的问题、术语口径、待你回答。F8 起不分段、不用确认，这里只答问题、定叫法
// BP-01 读、BP-02 保存（mode=draft，草案号不变，同待处理里回答）。
// 要解决的问题就是项目的建模目标（F4b）：在这里改目标走 P-05；资料和目标对不上的那个问题放在目标下面答，没答之前按原目标建
import { useCallback, useEffect, useState } from 'react';
import { useApp, useProject } from '../../../app/context.js';
import { toast } from '../../../app/toast.jsx';
import { errorText } from '../../../api/errors.js';
import {
  Button, Icon, Modal, Note, cx,
} from '../../../ui/index.jsx';
import { blueprintPayload, goalQuestion, isDirty } from './model.js';
import '../../../styles/materials.css';

function initEdit(bp) {
  return {
    terms: Object.fromEntries((bp.terms?.ambiguous || []).map((t) => [t.id, t.chosen])),
    answers: Object.fromEntries((bp.questions || []).map((q) => [q.id, q.answer?.option_id || null])),
  };
}

export function BlueprintDialog({ onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [bp, setBp] = useState(null);
  const [error, setError] = useState(null);
  const [edit, setEdit] = useState(null);
  const [goalDraft, setGoalDraft] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api.blueprint(pid).then((b) => { setBp(b); setEdit(initEdit(b)); }).catch(setError), [api, pid]);
  useEffect(() => { load(); }, [load]);

  // 改目标就是改项目的建模目标（P-05）；蓝图里不另存一份
  const saveGoal = async () => {
    const goal = (goalDraft || '').trim();
    if (!goal || busy) return;
    setBusy(true);
    try {
      await api.patchProject(pid, { goal });
      // 目标问题还没答：改了目标就是答了它（新目标作为回答）
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

  const save = async () => {
    if (!bp || busy) return;
    const body = blueprintPayload(bp, edit);
    if (!isDirty(body)) { onClose(); return; }
    setBusy(true);
    try {
      await api.saveBlueprint(pid, body);
      toast('蓝图已保存', 'check');
      onClose();
    } catch (e) {
      toast(errorText(e), 'alert');
      if (e.code === 'revision_conflict') load();
    } finally {
      setBusy(false);
    }
  };

  const terms = bp?.terms || { aligned: [], ambiguous: [] };
  // 目标问题放在「要解决的问题」下面答，其余问题照旧在「待你回答」
  const goalAsk = goalQuestion(bp?.questions || []);
  const questions = (bp?.questions || []).filter((q) => q !== goalAsk);
  return (
    <Modal
      open onClose={onClose} icon="layers" title="建模蓝图" w={1100}
      sub={bp?.version_label}
      foot={(
        <>
          <span className="sp" />
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" icon="check" disabled={!bp || busy} onClick={save}>保存</Button>
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
                    autoFocus value={goalDraft} maxLength={2000}
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
