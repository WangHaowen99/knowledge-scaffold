// 对话里的产物卡与状态卡（契约 4.7 card_type、设计稿 wb-l2 与 chat-states 第 0～20 项）
import { createContext, Fragment, useContext, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { roundLockNote } from '../../state/readonly.js';
import { roundDiffLines, roundState } from './roundState.js';
import { Button, Chip, Field, Icon, KV, Note, Ring, Seg, St, cx } from '../../ui/index.jsx';
import {
  CC, DiffBlock, FlowThumb, Inline, Md, SrcRow, Tli, WbCtx, Wl, fmtDuration, hhmm,
} from './parts.jsx';
import { EXTRA_CARDS } from './extraCards.js';
import { cardComponent, cardLocked, cardOpen, declaredCardView } from './artifactCard.js';

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
  const { openArtifact } = useActions();
  // 工作台里双击就地打开（图示包自带的编辑器）；说明页等没有弹窗宿主的地方跳到 flowchart 页
  const open = () => (openArtifact && card.artifact ? openArtifact(card.artifact, card.changeset_id) : navigate(pagePath('flowchart', pid), card.artifact ? { key: card.artifact.key } : undefined));
  // 只看实时的写入范围锁：卡片在对话历史里，存在卡上的 locked 会过时
  const isLocked = !!locked;
  const actLabel = isLocked ? '打开查看' : '打开编辑';
  // 不带 eyebrow 的卡（设计稿 flowchart 页）：标题后跟状态，缩略图下面一行说明和按钮
  if (!card.eyebrow) {
    return (
      <CC
        icon="branch" tone="indigo" title={card.title} chip={<St k={card.state === 'confirmed' ? 'confirmed' : 'draft'} sm />} sub={card.subtitle}
        dbl onDoubleClick={open}
        foot={(
          <>
            <span className="xs muted grow">{isLocked ? '助手改动中，暂时只读' : null}</span>
            <Button size="xs" icon={isLocked ? 'lock' : 'edit'} onClick={open}>{actLabel}</Button>
          </>
        )}
      >
        <FlowThumb thumbnail={card.thumbnail} height={150} />
      </CC>
    );
  }
  return (
    <CC
      icon="branch" tone="indigo" eyebrow={card.eyebrow} title={card.title} sub={card.subtitle} dbl hint={false} onDoubleClick={open}
      acts={<Button size="sm" kind="ghost" icon={isLocked ? 'lock' : 'edit'} onClick={open}>{actLabel}</Button>}
    >
      <div className="wx-cv">
        <FlowThumb thumbnail={card.thumbnail} height={140} />
        {isLocked ? <span className="ov"><Icon name="lock" size={12} />助手改动中，暂时只读</span> : null}
      </div>
    </CC>
  );
}

// ---------- 这一轮改了什么 ----------
const CHANGE_LABEL = { mod: ['mod', '修改'], add: ['add', '新增'], del: ['mod', '删除'], test: ['tst', '场景'] };

export { roundState };

export function RoundChangesCard({ card, scope }) {
  const { api } = useApp();
  const { pid } = useContext(WbCtx);
  const actions = useActions();
  const [diff, setDiff] = useState(null);
  const [showDiff, setShowDiff] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef(null);
  // F3：一轮结束就合入成版本；卡片只说改了什么，按钮只留撤回这一轮（合入冲突时去待处理）
  const st = roundState(card);

  const toggleDiff = async () => {
    if (!showDiff && !diff) {
      try {
        const r = await api.changesetDiff(pid, card.changeset_id);
        setDiff(roundDiffLines(r.items));
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
  const undone = st === 'reverted';
  return (
    <div ref={ref}>
      <CC
        icon={undone ? 'undo' : st === 'conflict' ? 'merge' : 'check'} tone={undone ? undefined : st === 'conflict' ? 'amber' : 'green'}
        eyebrow={card.eyebrow || '这一轮'} title={card.title}
        foot={(
          <>
            <Button size="sm" kind="ghost" icon="eye" onClick={toggleDiff}>{showDiff ? '收起改动' : '查看改动'}</Button>
            {st === 'merged' ? <Button size="sm" kind="ghost" icon="undo" disabled={busy} onClick={() => run(() => actions.discard(card.changeset_id))}>撤回这一轮</Button> : null}
            <span className="sp" />
            {st === 'merged' ? <span className="wx-ok"><Icon name="check" size={14} />{card.merged_version_label || card.status?.label}</span>
              : st === 'conflict' ? <Button size="sm" kind="primary" icon="merge" onClick={() => actions.resolve?.()}>去处理</Button>
                : <span className="xs" style={{ color: 'var(--faint)' }}>{card.status?.label || '已撤回'}</span>}
          </>
        )}
      >
        <div className="wx-chg">
          {rows.map((r, i) => {
            // 「另有 N 处改动」那一行没有动词标签（change 为空）
            if (!r.change) return <div className="r" key={i}><span className="muted">{r.text}</span></div>;
            const [cls, label] = CHANGE_LABEL[r.change] || CHANGE_LABEL.mod;
            return <div className="r" key={i}><span className={cx('wx-v', cls)}>{label}</span><span><Inline text={r.text} /></span></div>;
          })}
        </div>
        {(card.hints || []).map((h, i) => (
          <div key={i} className="xs row g4 mt8" style={{ color: h.tone === 'warn' ? 'var(--amber)' : 'var(--faint)' }}>
            <Icon name={h.tone === 'warn' ? 'alert' : 'layers'} size={12} />{h.text}
          </div>
        ))}
        {st === 'conflict' ? (
          <div className="xs row g4" style={{ color: 'var(--faint)', marginTop: 8 }}><Icon name="lock" size={12} />{roundLockNote(scope)}</div>
        ) : null}
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
      {roundState(card) === 'merged' ? (
        <div className="row mt8">
          <Button size="sm" icon="undo" onClick={() => actions.discard?.(card.changeset_id)}>撤回这一轮</Button>
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
            ['怎么继续', card.resumable ? '从中断处接着做' : `${card.not_resumable_reason || '输入已经变了'}，按新版从头做`],
          ]} />
        </div>
        <div className="row mt12">
          <Button size="sm" kind="ghost" onClick={() => actions.abandon?.(card.exec_id)}>放弃，从头再来</Button>
          <span className="sp" />
          {card.state === 'interrupted' ? <Button size="sm" kind="primary" icon="play" onClick={() => actions.resume?.(card.exec_id)}>{card.resumable ? `从步骤 ${card.position?.step} 续跑` : '按新版重新做'}</Button> : null}
        </div>
      </div>
    </>
  );
}

// ---------- 助手推荐插件（第 16 项） ----------
export function PackRecommendationCard({ card }) {
  const { pid } = useContext(WbCtx);
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
        <Button size="sm" kind="soft" icon="puzzle" onClick={() => navigate('/plugins', card.pack ? { pack: card.pack.name, project: pid } : { project: pid })}>去插件中心看看</Button>
      </div>
    </div>
  );
}

// 通用卡：插件产物卡按包的 views.<类型>.card 声明画（契约 4.7 artifact，ADR-0055）；没有专用组件的其余卡片
// （资料地图、蓝图、变更点清单、运行报告等）也走这里，有声明画声明，没有就是标题加状态
export function GenericCard({ card, locked }) {
  const { pid } = useContext(WbCtx);
  const { openArtifact } = useActions();
  const v = declaredCardView(card, { locked });
  const target = cardOpen(card);
  let open = null;
  if (target?.kind === 'artifact') open = openArtifact ? () => openArtifact(target.ref, card.changeset_id) : null;
  else if (target?.kind === 'page') open = () => navigate(pagePath(target.page, pid));
  const chip = v.status ? <St k={v.status.key} sm label={v.status.label} />
    : v.chips.length ? <span className="row g4">{v.chips.map((c) => <Chip key={c.key} tone={c.tone} sm>{c.text}</Chip>)}</span> : null;
  return (
    <CC
      icon={v.icon} tone={v.plugin ? 'indigo' : undefined} eyebrow={v.eyebrow} title={v.title} sub={v.sub} chip={chip}
      dbl={!!open} onDoubleClick={open || undefined}
      acts={v.locked ? <Chip tone="gray" icon="lock" sm>只读</Chip> : null}
    >
      {v.fields.length ? <KV w="72px" pairs={v.fields} /> : null}
    </CC>
  );
}

// 插件产物卡可以按 card.component 选的平台专用组件（契约 4.7 flowchart 卡保留兼容）
const ARTIFACT_CARDS = { flowchart: FlowchartCard };

export function CardItem({ card, scope }) {
  switch (card.card_type) {
    case 'step_output': return <StepOutputCard card={card} />;
    case 'flowchart': return <FlowchartCard card={card} locked={cardLocked(card, scope)} />;
    case 'artifact': {
      const Comp = ARTIFACT_CARDS[cardComponent(card, Object.keys(ARTIFACT_CARDS))];
      return Comp ? <Comp card={card} locked={cardLocked(card, scope)} /> : <GenericCard card={card} locked={cardLocked(card, scope)} />;
    }
    case 'round_changes': {
      // 增量更新执行后的那一轮（带 change_list_id）按设计稿 incremental 的样子画
      const Inc = card.change_list_id ? EXTRA_CARDS['round_changes.incremental'] : null;
      return Inc ? <Inc card={card} scope={scope} /> : <RoundChangesCard card={card} scope={scope} />;
    }
    case 'resume': return <ResumeCard card={card} />;
    case 'pack_recommendation': return <PackRecommendationCard card={card} />;
    case 'knowledge_saved': return <div className="row mt8"><Chip tone="teal" icon="check">已入库</Chip></div>;
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
      </div>
      <div className="xs muted mt12" style={{ marginLeft: 30 }}>
        错误编号 {item.error_id}{canSee ? <> · <a className="link" href={`#/ops-users?tab=errors&error=${encodeURIComponent(item.error_id)}`}>查看</a></> : null}
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
      <div className="mt4">{item.effective === 'next_safe_point' ? '下一个安全点起生效' : '下一条消息起生效'}</div>
      {item.detail_link ? <div className="row mt8"><a className="link sm" href={`#/plugin-detail?pack=${encodeURIComponent(item.detail_link.pack)}`}>查看更新内容</a></div> : null}
    </Note>
  );
}

export function ReconnectedItem({ item }) {
  return <div className="tl mt12"><Tli k="ok" t={item.context_kept ? '已切换到新版本并重新连接，上下文保留' : '已切换到新版本并重新连接'} /></div>;
}

export function CompactedItem({ item }) {
  return (
    <div className="tl mt12">
      <Tli k="info" t={`已${item.trigger === 'manual' ? '' : '自动'}压缩上下文，释放 ${Math.round((item.freed_tokens || 0) / 1000)}k`} m={hhmm(item.at || item.ts)} />
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
      <div className="row"><Ring p={c.pct} r={16} color={warn ? '#F59E0B' : '#2563EB'} /><div><div className="b">{k(c.used)} / {k(c.window)} tokens</div><div className="xs muted">本会话上下文</div></div></div>
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
        ['上次调用', c.last_call ? `输入 ${(c.last_call.input / 1000).toFixed(1)}k / 输出 ${(c.last_call.output / 1000).toFixed(1)}k` : '—'],
        ['项目累计', usage ? `输入 ${fmtTokens(usage.input_tokens)} · 输出 ${fmtTokens(usage.output_tokens)}${usage.cost?.amount != null ? ` · 约 ${usage.cost.currency === 'USD' ? '$' : '¥'}${Math.round(usage.cost.amount)}` : ''}` : '—'],
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

