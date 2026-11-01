// 对话里的产物卡与状态卡（契约 4.7 card_type、设计稿 wb-l2 与 chat-states 第 0～20 项）
import { createContext, Fragment, useContext, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { roundLockNote } from '../../state/readonly.js';
import { Button, Chip, Field, Icon, KV, Note, Ring, Seg, St, cx } from '../../ui/index.jsx';
import {
  CC, DiffBlock, FlowThumb, Inline, Md, SrcRow, Tli, WbCtx, Wl, fmtDuration, hhmm,
} from './parts.jsx';
import { EXTRA_CARDS } from './extraCards.js';

// 卡片按钮要调的动作，由工作台页注入
export const ActionsCtx = createContext({});
export const useActions = () => useContext(ActionsCtx);

const STATUS_KEY = { pending: 'pending', confirmed: 'confirmed', draft: 'draft', stale: 'stale', needs_review: 'needs_review' };

// ---------- 本步产出卡 ----------
export function StepOutputCard({ card }) {
  const inputs = [];
  (card.inputs || []).forEach((x, i) => {
    if (x.text && /^[（(]/.test(x.text)) { inputs.push(<Fragment key={i}>{x.text}</Fragment>); return; }
    if (inputs.length) inputs.push(<Fragment key={`s${i}`}>、</Fragment>);
    inputs.push(x.text ? <Fragment key={i}>{x.text}</Fragment> : <Wl key={i} name={x.name} />);
  });
  const join = (list) => (list || []).map((x, i) => (
    <Fragment key={i}>{i ? '、' : ''}{x.text ? x.text : <Wl name={x.name} />}</Fragment>
  ));
  const status = card.status?.key || STATUS_KEY[card.state] || 'pending';
  return (
    <CC icon="flow" eyebrow={card.eyebrow} title={card.title} chip={<span><St k={status} sm label={card.status?.label} /></span>}>
      <dl className="wx-def" style={{ margin: 0 }}>
        <dt>输入</dt><dd>{inputs}</dd>
        <dt>步骤</dt>
        <dd>
          <ol className="wx-ol">
            {(card.steps || []).map((s) => (
              <li key={s.n} className={s.awaiting_question_id ? 'wx-hl' : undefined}>
                <Inline text={s.text_md} anchors={s.anchors} />
                {s.awaiting_question_id ? <span className="wx-tag amber"><Icon name="hand" />等你决定</span> : null}
              </li>
            ))}
          </ol>
        </dd>
        <dt>输出</dt><dd>{join(card.outputs)}</dd>
        {card.rules?.length ? (
          <>
            <dt>规则</dt>
            <dd>
              {card.rules.map((r, i) => (
                <Fragment key={i}>
                  {i ? '、' : ''}<Wl name={r.ref.name} />{' '}
                  {r.state === 'draft' || r.state === 'pending' ? (
                    <span className="wx-tag" style={{ background: 'rgba(15,23,42,.05)', color: 'var(--sub)' }}>{r.state === 'draft' ? '草稿' : '待确认'}</span>
                  ) : null}
                </Fragment>
              ))}
            </dd>
          </>
        ) : null}
      </dl>
      {card.sources?.length ? <SrcRow sources={card.sources} /> : null}
    </CC>
  );
}

// ---------- 流程图卡 ----------
export function FlowchartCard({ card, locked }) {
  const { pid } = useContext(WbCtx);
  const { openFlowchart } = useActions();
  // 工作台里双击就地打开弹窗；说明页等没有弹窗宿主的地方跳到 flowchart 页
  const open = () => (openFlowchart ? openFlowchart(card.artifact) : navigate(pagePath('flowchart', pid), card.artifact ? { key: card.artifact.key } : undefined));
  const isLocked = locked || card.locked;
  return (
    <CC
      icon="branch" tone="indigo" eyebrow={card.eyebrow} title={card.title} sub={card.subtitle} dbl hint={false} onDoubleClick={open}
      acts={<Button size="sm" kind="ghost" icon={isLocked ? 'lock' : 'edit'} onClick={open}>{isLocked ? '打开查看' : '打开编辑'}</Button>}
    >
      <div className="wx-cv">
        <FlowThumb thumbnail={card.thumbnail} height={140} />
        <span className="ov"><Icon name={isLocked ? 'lock' : 'maximize'} size={12} />{isLocked ? '在助手写入范围内，确认生效或撤回前只读' : '双击打开编辑，保存后和 Studio 是同一份'}</span>
      </div>
    </CC>
  );
}

// ---------- 这一轮改了什么 ----------
const CHANGE_LABEL = { mod: ['mod', '修改'], add: ['add', '新增'], del: ['mod', '删除'], test: ['tst', '场景'] };

export function RoundChangesCard({ card, scope }) {
  const { api } = useApp();
  const { pid } = useContext(WbCtx);
  const actions = useActions();
  const [diff, setDiff] = useState(null);
  const [showDiff, setShowDiff] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef(null);
  const merged = card.state === 'merged';
  const discarded = card.state === 'discarded';

  const toggleDiff = async () => {
    if (!showDiff && !diff) {
      try {
        const r = await api.changesetDiff(pid, card.changeset_id);
        setDiff(r.items.flatMap((it) => it.sections.flatMap((s) => s.lines)));
      } catch (e) {
        actions.fail?.(e);
        return;
      }
    }
    setShowDiff((v) => !v);
  };
  useEffect(() => actions.registerRound?.(card.changeset_id, () => {
    if (!showDiff) toggleDiff();
    ref.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }));

  const run = async (fn) => {
    setBusy(true);
    try { await fn(); } finally { setBusy(false); }
  };
  const rows = (card.groups || []).flatMap((g) => g.items);
  return (
    <div ref={ref}>
      <CC
        icon={discarded ? 'undo' : 'check'} tone={discarded ? undefined : 'green'} eyebrow={card.eyebrow || '这一轮'} title={card.title}
        foot={(
          <>
            <Button size="sm" kind="ghost" icon="eye" onClick={toggleDiff}>{showDiff ? '收起改动' : '查看改动'}</Button>
            {!merged && !discarded ? <Button size="sm" kind="ghost" icon="undo" disabled={busy} onClick={() => run(() => actions.discard(card.changeset_id))}>撤回这一轮</Button> : null}
            <span className="sp" />
            {merged ? <span className="wx-ok"><Icon name="check" size={14} />已确认生效 · 版本 {card.merged_version_label}</span>
              : discarded ? <span className="xs" style={{ color: 'var(--faint)' }}>已撤回这一轮，主线没有变化</span>
                : (
                  <>
                    <span className="xs" style={{ color: 'var(--faint)' }}>确认后保存为版本 {card.next_version_label}</span>
                    <span><Button size="sm" kind="primary" icon="merge" disabled={busy} onClick={() => run(() => actions.merge(card.changeset_id))}>确认生效</Button></span>
                  </>
                )}
          </>
        )}
      >
        <div className="wx-chg">
          {rows.map((r, i) => {
            const [cls, label] = CHANGE_LABEL[r.change] || CHANGE_LABEL.mod;
            return <div className="r" key={i}><span className={cx('wx-v', cls)}>{label}</span><span><Inline text={r.text} /></span></div>;
          })}
        </div>
        {!merged && !discarded ? (
          <div className="xs row g4" style={{ color: 'var(--faint)', marginTop: 8 }}><Icon name="lock" size={12} />{card.lock_note || roundLockNote(scope)}</div>
        ) : null}
        {card.remaining_failures ? <Note tone="amber" className="mt8">还有 {card.remaining_failures} 项检查没通过</Note> : null}
        {showDiff && diff ? <DiffBlock lines={diff} style={{ marginTop: 10 }} /> : null}
      </CC>
    </div>
  );
}

// chat-states 第 8 项：展开明细（按类型分组，每条可对比）
export function RoundChangesDetail({ card, onCollapse }) {
  const actions = useActions();
  const groups = card.detail_groups || card.groups || [];
  const icon = { object: 'cube', operation: 'flow', rule: 'scale', metric: 'gauge', script: 'code', scenario: 'flask', artifact: 'layers' };
  return (
    <div className="wb-cc" style={{ marginTop: 0 }}>
      <div className="row"><b>这一轮改了什么</b><span className="sp" /><a className="link sm" onClick={onCollapse}>收起明细</a></div>
      {groups.map((g) => (
        <Fragment key={g.kind}>
          <div className="xs b muted mt8 row g4"><Icon name={icon[g.kind] || 'layers'} size={12} />{g.label} {g.items.length}</div>
          {g.items.map((it, i) => (
            <li key={i} style={{ paddingLeft: 16 }}>
              <span className="grow"><b style={{ fontWeight: 600 }}>{it.name || it.target?.name || it.target?.title}</b> <span className="muted">· {it.detail || it.text}</span></span>
              <a className="link xs" onClick={() => actions.compare?.(card.changeset_id, it.target)}>对比</a>
            </li>
          ))}
        </Fragment>
      ))}
      <div className="xs faint mt8">你在 Studio 或文件里另外保存的改动不算在这里</div>
      {card.state === 'pending_confirm' ? <div className="xs faint mt4 row g4"><Icon name="lock" size={12} />确认生效前，这些条目在 Studio 和插件页里只读；撤回这一轮也会解除</div> : null}
      {card.state === 'pending_confirm' ? (
        <div className="row mt8">
          <Button size="sm" icon="undo" onClick={() => actions.discard?.(card.changeset_id)}>撤回这一轮</Button>
          <span className="sp" />
          <Button size="sm" kind="primary" icon="merge" onClick={() => actions.merge?.(card.changeset_id)}>确认生效</Button>
        </div>
      ) : null}
    </div>
  );
}

// ---------- 中断续跑卡（第 5 项） ----------
export function ResumeCard({ card }) {
  const actions = useActions();
  return (
    <>
      <div style={{ border: '1px solid var(--a100)', background: 'linear-gradient(180deg,#FFFBF2,#fff)', borderRadius: 13, padding: 12 }}>
        <div className="row"><span style={{ color: 'var(--amber2)' }}><Icon name="history" size={18} /></span><b>{card.title}</b></div>
        <div className="mt8">
          <KV w="72px" pairs={[
            ['原因', card.reason],
            ['中断位置', card.position?.label],
            ['已保存', card.saved ? `${card.saved.files} 个文件，版本 ${card.saved.version_label}` : '—'],
            [card.resumable ? '可以续跑' : '不能续跑', card.resumable ? '输入没有变化' : (card.not_resumable_reason || '输入已经变了')],
          ]} />
        </div>
        <div className="row mt12">
          <Button size="sm" kind="ghost" onClick={() => actions.abandon?.(card.exec_id)}>放弃，从头再来</Button>
          <span className="sp" />
          {card.resumable ? <Button size="sm" kind="primary" icon="play" onClick={() => actions.resume?.(card.exec_id)}>从步骤 {card.position?.step} 续跑</Button> : null}
        </div>
      </div>
      <div className="xs faint mt8">执行进度由平台记录，助手进程退出后也能续跑</div>
    </>
  );
}

// ---------- 助手推荐插件（第 16 项） ----------
export function PackRecommendationCard({ card }) {
  const [hidden, setHidden] = useState(false);
  if (hidden) return null;
  return (
    <div className="card mt8" style={{ padding: 12 }}>
      <div className="row">
        <span style={{ width: 34, height: 34, borderRadius: 10, display: 'grid', placeItems: 'center', background: 'var(--t50)', color: 'var(--teal)' }}><Icon name="db" size={18} /></span>
        <div className="grow"><div className="b">{card.pack?.label || card.title}</div><div className="xs muted">{card.category_label}</div></div>
      </div>
      <div className="xs muted mt8">{card.description}</div>
      <div className="row mt8">
        <Button size="sm" kind="ghost" onClick={() => setHidden(true)}>暂时不用</Button>
        <span className="sp" />
        <Button size="sm" kind="soft" icon="puzzle" onClick={() => navigate('/plugins', card.pack ? { pack: card.pack.name } : undefined)}>去插件中心看看</Button>
      </div>
    </div>
  );
}

// 其余产物卡（资料地图、蓝图、变更点清单、运行报告等）：显示标题并能打开对应页面，弹窗在后续阶段接入
const GENERIC_ICON = { material_map: 'map', blueprint: 'layers', change_list: 'refresh', run_report: 'rocket', research_summary: 'filetext', impact: 'target', regression: 'flask', external_feedback: 'inbox', autobuild_start: 'rocket', slice_progress: 'layers', auto_decision: 'bot', suspension: 'pause', build_choice: 'compass', upload_prompt: 'upload' };
const GENERIC_PAGE = { material_map: 'wb-materials', blueprint: 'wb-blueprint', change_list: 'incremental', run_report: 'l4-run', impact: 'incremental', regression: 'incremental', external_feedback: 'incremental', autobuild_start: 'l4-run', slice_progress: 'l4-run', auto_decision: 'inbox', suspension: 'inbox' };
export function GenericCard({ card }) {
  const { pid } = useContext(WbCtx);
  const page = card.open?.kind === 'page' ? card.open.target : GENERIC_PAGE[card.card_type];
  return (
    <CC
      icon={GENERIC_ICON[card.card_type] || 'filetext'} eyebrow={card.eyebrow} title={card.title} sub={card.subtitle}
      chip={card.status ? <St k={card.status.key} sm label={card.status.label} /> : null}
      dbl={!!page} onDoubleClick={page ? () => navigate(pagePath(page, pid)) : undefined}
    />
  );
}

export function CardItem({ card, scope }) {
  switch (card.card_type) {
    case 'step_output': return <StepOutputCard card={card} />;
    case 'flowchart': return <FlowchartCard card={card} locked={!!scope && scope.state !== 'none' && (scope.artifacts || []).some((a) => a.type === card.artifact?.type && a.key === card.artifact?.key)} />;
    case 'round_changes': return <RoundChangesCard card={card} scope={scope} />;
    case 'resume': return <ResumeCard card={card} />;
    case 'pack_recommendation': return <PackRecommendationCard card={card} />;
    case 'knowledge_saved': return <div className="row mt8"><Chip tone="teal" icon="check">已入库，后续建模会自动应用</Chip></div>;
    default: {
      const Extra = EXTRA_CARDS[card.card_type];
      return Extra ? <Extra card={card} scope={scope} /> : <GenericCard card={card} />;
    }
  }
}

// ---------- 会话状态行（第 4、14、15、10 项） ----------
export function ErrorItem({ item }) {
  const actions = useActions();
  const { user } = useApp();
  const canSee = user?.role === 'admin' || user?.role === 'ops';
  return (
    <>
      <div className="tl"><Tli k="err" t={`这一步没完成：${item.message}`} m={item.retries ? `已自动重试 ${item.retries} 次` : ''} /></div>
      <div className="row" style={{ marginLeft: 30 }}>
        {item.retryable !== false ? <Button size="sm" kind="primary" icon="refresh" onClick={() => actions.retry?.()}>重试</Button> : null}
        <Button size="sm" onClick={() => actions.clearInput?.()}>换个方向</Button>
      </div>
      <div className="xs muted mt12" style={{ marginLeft: 30 }}>
        原始错误已留档（编号 {item.error_id}）{canSee ? <>，可以在 <a className="link" href="#/ops-users">运维中心 › 错误留档</a> 查看</> : '，管理员可以在运维中心查看'}
      </div>
    </>
  );
}

export function InterruptedItem({ item }) {
  return <div className="tl"><Tli k="gray" t={item.position_label || '这一步被打断了'} m={`${hhmm(item.at || item.ts)} · 已打断${item.saved?.version_label ? ` · 已保存到 ${item.saved.version_label}` : ''}`} /></div>;
}

export function ConfigPendingItem({ item }) {
  const summary = (item.changes || []).map((c) => c.summary).filter(Boolean)[0];
  return (
    <Note icon="pkg">
      <b>{summary || '建模能力有更新'}</b>
      <div className="mt4">{item.effective === 'next_safe_point' ? '到下一个安全点生效；正在进行的这一片按原来的方式跑完。' : '下一条消息开始生效；正在进行的这一轮按原来的方式跑完。'}</div>
      {item.detail_link ? <div className="row mt8"><a className="link sm" href={`#/plugin-detail?pack=${encodeURIComponent(item.detail_link.pack)}`}>查看更新内容</a></div> : null}
    </Note>
  );
}

export function ReconnectedItem({ item }) {
  return <div className="tl mt12"><Tli k="ok" t={item.context_kept ? '已切换到新版本并重新连接，上下文保留' : '已切换到新版本并重新连接'} m={`你发出下一条消息时 · ${((item.duration_ms || 0) / 1000).toFixed(1)} 秒`} /></div>;
}

export function CompactedItem({ item }) {
  const k = item.kept || {};
  return (
    <div className="tl mt12">
      <Tli k="info" t={`已${item.trigger === 'manual' ? '' : '自动'}压缩上下文：释放 ${Math.round((item.freed_tokens || 0) / 1000)}k，保留 ${k.decisions || 0} 个决定${k.artifacts ? '、产物清单' : ''}${k.todos ? '和没做完的事' : ''}`} m={hhmm(item.at || item.ts)} />
    </div>
  );
}

// 排队卡（第 9 项）
export function QueueCard({ queue, onCancel }) {
  return (
    <div className="m-a">
      <div className="bd">
        <div className="g2-q mt12">
          <b>{queue.position}</b>
          <div className="grow">
            <div className="b sm">正在排队，前面还有 {queue.ahead ?? Math.max(0, queue.position - 1)} 个</div>
            <div className="xs muted mt4">服务器同时最多运行 {queue.pool?.max ?? '—'} 个助手。轮到你时自动开始，不用守着页面。</div>
            {queue.progress_pct != null ? <div className="mt4"><div className="prog amber thin"><i style={{ width: `${queue.progress_pct}%` }} /></div></div> : null}
          </div>
        </div>
        <div className="row mt8"><span className="sp" /><Button size="sm" kind="ghost" onClick={onCancel}>取消排队</Button></div>
      </div>
    </div>
  );
}

// 上下文占用（第 10 项）
export function ContextPanel({ context, usage }) {
  const c = context || {};
  const b = c.breakdown;
  const k = (n) => `${Math.round((n || 0) / 1000)}k`;
  const total = c.window || 1;
  const warn = (c.pct || 0) >= 75;
  return (
    <div className="g2-mm" style={{ boxShadow: 'none' }}>
      <div className="row"><Ring p={c.pct} r={16} color={warn ? '#F59E0B' : '#2563EB'} /><div><div className="b">{k(c.used)} / {k(c.window)} tokens</div><div className="xs muted">本会话上下文 · 到 {c.compact_at_pct ?? 80}% 自动压缩</div></div></div>
      {b ? (
        <>
          <div className="g2-stack mt12">
            <i style={{ width: `${(b.rules / total) * 100}%`, background: '#94A3B8' }} />
            <i style={{ width: `${(b.materials / total) * 100}%`, background: '#3B82F6' }} />
            <i style={{ width: `${(b.conversation / total) * 100}%`, background: '#93C5FD' }} />
            <i style={{ width: `${(b.tool_results / total) * 100}%`, background: '#F59E0B' }} />
            <i style={{ flex: 1, background: 'var(--panel3)' }} />
          </div>
          <div className="row wrap xs muted mt4" style={{ gap: 10 }}>
            <span>规则 {k(b.rules)}</span><span style={{ color: '#2563EB' }}>资料摘录 {k(b.materials)}</span><span>对话 {k(b.conversation)}</span><span style={{ color: '#B45309' }}>工具结果 {k(b.tool_results)}</span>
          </div>
        </>
      ) : null}
      <div className="divider" />
      <KV w="60px" pairs={[
        ['上次调用', c.last_call ? `输入 ${(c.last_call.input / 1000).toFixed(1)}k / 输出 ${(c.last_call.output / 1000).toFixed(1)}k${c.last_call.estimated ? '（按流式片段估算）' : ''}` : '—'],
        ['项目累计', usage ? `含全部会话：输入 ${fmtTokens(usage.input_tokens)} · 输出 ${fmtTokens(usage.output_tokens)}${usage.cost?.amount != null ? ` · 约 ${usage.cost.currency === 'USD' ? '$' : '¥'}${Math.round(usage.cost.amount)}` : ''}` : '—'],
      ]} />
    </div>
  );
}

export function fmtTokens(n) {
  if (n == null) return '—';
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}

