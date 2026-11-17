// 等用户回答的卡片：决策卡（单选 + 后果 + 改动预览）、模拟分支、请专家确认、交互模拟、一般确认、权限确认
import { useEffect, useRef, useState } from 'react';
import { Button, Chip, Icon, Ring, cx } from '../../ui/index.jsx';
import { useActions } from './cards.jsx';
import { DiffBlock, Inline, MSys, Tli } from './parts.jsx';
import { decisionAnswer } from './questionModel.js';

function useAnswer(q) {
  const actions = useActions();
  const [busy, setBusy] = useState(false);
  const submit = async (body) => {
    setBusy(true);
    try { await actions.answer(q.question_id, body); } finally { setBusy(false); }
  };
  return [busy, submit];
}

// 结构化决策卡（wb-l2）：没选不能确认；键盘 1–4 选择、↵ 确认。多选题（q.multi）点选项是勾选 / 取消，答复传 option_ids
export function DecisionCard({ q, active }) {
  const actions = useActions();
  const [picks, setPicks] = useState([]);
  const [custom, setCustom] = useState('');
  const [busy, submit] = useAnswer(q);
  const toggle = (id) => setPicks((cur) => (q.multi ? (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]) : [id]));
  const chosen = q.options.filter((o) => picks.includes(o.id));
  const opt = q.multi ? null : chosen[0];
  const body = decisionAnswer(q, picks, custom);
  const ref = useRef({});
  ref.current = { body };

  useEffect(() => {
    if (!active) return undefined;
    const h = (e) => {
      const tag = (document.activeElement?.tagName || '');
      if (/TEXTAREA|INPUT/.test(tag) || e.metaKey || e.ctrlKey) return;
      const i = Number(e.key);
      if (i >= 1 && i <= q.options.length) { toggle(q.options[i - 1].id); e.preventDefault(); return; }
      if (e.key === 'Enter' && ref.current.body && !busy) {
        e.preventDefault();
        submit(ref.current.body);
      }
    };
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, [active, q.options, q.multi, busy]); // eslint-disable-line react-hooks/exhaustive-deps

  const label = q.multi
    ? (chosen.length ? `确认 ${chosen.map((o) => o.key).join('、')}` : '确认所选')
    : (opt ? `确认方向 ${opt.key}` : '确认这个方向');
  return (
    <div className="wb-dc">
      <div className="wx-eb"><Icon name="hand" />{q.eyebrow || `需要你决定 · ${q.step_label}`}</div>
      <h4>{q.title}</h4>
      {q.background_md ? <div className="wx-ctx"><Inline text={q.background_md} anchors={q.anchors} /></div> : null}
      <div className="opts">
        {q.options.map((o, i) => (
          <div key={o.id} className={cx('opt', picks.includes(o.id) && 'on')} onClick={() => toggle(o.id)} data-i={i + 1}>
            <span className="k">{o.key}</span>
            <div className="grow">
              <div className="t row g8"><span>{o.label}</span>{o.recommended ? <span className="chip blue sm">推荐</span> : null}</div>
              {o.consequence ? <div className="c">{o.consequence}</div> : null}
              {o.custom && picks.includes(o.id) ? (
                <div className="mt8" onClick={(e) => e.stopPropagation()}>
                  <div className="inp area" style={{ minHeight: 60 }}><textarea style={{ height: 44 }} autoFocus placeholder={q.custom_placeholder || '说说你的方向…'} value={custom} onChange={(e) => setCustom(e.target.value)} /></div>
                </div>
              ) : null}
            </div>
          </div>
        ))}
      </div>
      {opt?.preview?.length ? (
        <div className="wb-prev">
          <div className="lb"><Icon name="eye" size={14} />选 {opt.key} 会这样改</div>
          <DiffBlock lines={opt.preview} />
        </div>
      ) : null}
      <div className="wx-dcf">
        <span className="sp" />
        <Button size="sm" kind="ghost" onClick={() => actions.later?.(q)}>稍后决定</Button>
        <Button kind="primary" size="sm" title={`1–${q.options.length} 选择 · ↵ 确认`} disabled={!body || busy} onClick={() => submit(body)}>
          {label}
        </Button>
      </div>
    </div>
  );
}

// 模拟分支选择（第 7 项）：点选项即回答；也可以输入自定义回答后回车
export function BranchCard({ q, initialPick = null }) {
  const [pick, setPick] = useState(initialPick);
  const [custom, setCustom] = useState('');
  const [busy, submit] = useAnswer(q);
  const choose = (id) => {
    if (busy) return;
    setPick(id);
    submit({ option_id: id });
  };
  return (
    <div className="wb-dc" style={{ marginTop: 0 }}>
      <div className="row"><h4><Icon name="fork" size={14} />{q.title || '模拟分支选择'}</h4><span className="sp" />{q.scenario ? <Chip tone="line" sm>{q.scenario.title}</Chip> : null}</div>
      <div className="sm muted mt4">{q.context_text || q.background_md}</div>
      <div className="opts">
        {q.options.map((o) => (
          <div key={o.id} className={cx('opt', pick === o.id && 'on')} onClick={() => choose(o.id)}>
            <span className="k">{o.key}</span><div className="t">{o.label}</div>
          </div>
        ))}
      </div>
      <div className="mt8">
        <div className="inp">
          <input
            placeholder="或输入自定义回答…" value={custom} disabled={busy} onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && custom.trim() && !e.nativeEvent.isComposing) submit({ custom_text: custom.trim() }); }}
          />
        </div>
      </div>
    </div>
  );
}

// 请专家确认（第 7 项）
export function ExpertCard({ q }) {
  const [busy, submit] = useAnswer(q);
  const main = q.options.filter((o) => !o.dismiss && o.id !== 'dismiss');
  const dismiss = q.options.find((o) => o.dismiss || o.id === 'dismiss');
  return (
    <div className="wb-dc" style={{ marginTop: 10, borderColor: 'var(--a100)', background: 'linear-gradient(180deg,#FFFBF2,#fff 50%)' }}>
      <h4><Icon name="badge" size={14} />{q.eyebrow || '请专家确认'}</h4>
      <div className="sm muted mt4">{q.title}</div>
      <div className="row mt8 wrap">
        {main.map((o) => <Button key={o.id} size="sm" disabled={busy} onClick={() => submit({ option_id: o.id })}>{o.label}</Button>)}
        <span className="sp" />
        {dismiss ? <Button size="xs" kind="ghost" disabled={busy} onClick={() => submit({ option_id: dismiss.id })}>{dismiss.label}</Button> : null}
      </div>
    </div>
  );
}

// 交互模拟卡（第 20 项）：判断点就是决策卡
export function WalkthroughCard({ q }) {
  const [pick, setPick] = useState(q.options.find((o) => o.recommended)?.id || null);
  const [busy, submit] = useAnswer(q);
  return (
    <div className="wb-dc" style={{ marginTop: 0 }}>
      <h4><Icon name="hand" size={14} />{q.title}</h4>
      <div className="row wrap mt8" style={{ gap: 6 }}>
        {q.operation ? <Chip tone="indigo" sm icon="layers">{q.operation.name}</Chip> : null}
        {(q.inputs || []).map((x) => <span key={x.name} className="chip line sm">{x.name} = <b style={{ marginLeft: 2 }}>{x.value}</b></span>)}
      </div>
      <div className="tl mt12">
        {(q.steps || []).map((s) => <Tli key={s.n} k="ok" t={<Inline text={`步骤 ${s.n} ${s.text}`} />} m={s.source} />)}
        {q.prompt ? <Tli k="waiting" t={<b>判断点：{q.prompt}</b>} /> : null}
      </div>
      <div className="opts" style={{ marginTop: 4 }}>
        {q.options.map((o) => (
          <div key={o.id} className={cx('opt', pick === o.id && 'on')} onClick={() => setPick(o.id)}>
            <span className="k">{o.key}</span>
            <div className="grow"><div className="t">{o.label}</div>{o.consequence ? <div className="c">{o.consequence}</div> : null}</div>
          </div>
        ))}
      </div>
      <div className="row mt12">
        <span className="sp" />
        <Button kind="primary" size="sm" icon="play" disabled={!pick || busy} onClick={() => submit({ option_id: pick })}>继续推演</Button>
      </div>
    </div>
  );
}

// 一般的是 / 否确认
export function ConfirmCard({ q }) {
  const [busy, submit] = useAnswer(q);
  return (
    <div className="wb-dc">
      {q.eyebrow ? <div className="wx-eb"><Icon name="hand" />{q.eyebrow}</div> : null}
      <h4>{q.title}</h4>
      {q.background_md || q.collection?.summary ? <div className="wx-ctx"><Inline text={q.background_md || q.collection.summary} anchors={q.anchors} /></div> : null}
      <div className="wx-dcf">
        <span className="sp" />
        {q.options.map((o, i) => <Button key={o.id} size="sm" kind={i === 0 ? 'primary' : undefined} disabled={busy} onClick={() => submit({ option_id: o.id })}>{o.label}</Button>)}
      </div>
    </div>
  );
}

export function QuestionCard({ q, active }) {
  switch (q.kind) {
    case 'branch': return <BranchCard q={q} />;
    case 'expert': return <ExpertCard q={q} />;
    case 'walkthrough': return <WalkthroughCard q={q} />;
    case 'collection':
    case 'confirm': return <ConfirmCard q={q} />;
    default: return <DecisionCard q={q} active={active} />;
  }
}

// 回答后收起成一行
export function DecidedRow({ item }) {
  const q = item.question;
  let text = item.summary;
  if (!text) {
    const o = q.options?.find((x) => x.id === (item.answer?.option_id || q.answer?.option_id));
    text = o ? `${q.step_label ? `${q.step_label} ` : ''}选了 ${o.key}：${o.custom && item.answer?.custom_text ? item.answer.custom_text : o.label}` : `已回答：${q.title}`;
  }
  return <MSys left>{text}</MSys>;
}

// 权限确认卡（第 6 项）：600 秒倒计时，超时按拒绝
export function PermissionCard({ item }) {
  const actions = useActions();
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (item.resolved) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [item.resolved]);
  if (item.resolved) {
    const r = item.resolved;
    return <MSys left>{r.behavior === 'allow' ? '已允许' : r.by === 'timeout' ? '超时，已拒绝' : '已拒绝'}：{item.description}</MSys>;
  }
  const total = (item.timeout_s || 600) * 1000;
  const left = Math.max(0, (item.expires_at ? Date.parse(item.expires_at) : Date.parse(item.ts) + total) - now);
  const mm = `${Math.floor(left / 60000)}:${String(Math.floor((left % 60000) / 1000)).padStart(2, '0')}`;
  const reply = async (behavior) => {
    setBusy(true);
    try { await actions.permission(item.request_id, behavior); } finally { setBusy(false); }
  };
  return (
    <div className="wb-dc" style={{ marginTop: 0 }}>
      <div className="row"><h4><Icon name="shield" size={14} />{item.title}</h4><span className="sp" /><span className="g2-cd"><Ring p={(left / total) * 100} r={7} color="#F59E0B" />{mm}</span></div>
      <div className="sm mt8">{item.description}</div>
      {item.details_text ? <details className="mt8"><summary className="xs muted" style={{ cursor: 'pointer' }}>查看技术细节</summary><div className="code mt4" style={{ fontSize: 11 }}>{item.details_text}</div></details> : null}
      <div className="row mt12">
        <span className="xs faint grow">超时按拒绝</span>
        <Button size="sm" disabled={busy} onClick={() => reply('deny')}>拒绝</Button>
        <Button size="sm" kind="primary" disabled={busy} onClick={() => reply('allow')}>允许</Button>
      </div>
    </div>
  );
}
