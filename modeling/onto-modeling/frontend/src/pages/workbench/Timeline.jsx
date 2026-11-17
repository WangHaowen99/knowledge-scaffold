// 对话时间线：把状态机分好的块渲染成设计稿的 m-u / m-a / m-sys / wx-day / 决策卡
import {
  Fragment, memo, useEffect, useRef, useState,
} from 'react';
import { toast } from '../../app/toast.jsx';
import { timelineBlocks } from '../../state/conversation.js';
import { Icon, cx } from '../../ui/index.jsx';
import {
  CardItem, CompactedItem, ConfigPendingItem, ErrorItem, InterruptedItem, ReconnectedItem, useActions,
} from './cards.jsx';
import {
  MA, MDay, MSys, Md, TimelineGroup, WxFile, WxProc, hhmm,
} from './parts.jsx';
import { SubagentCard } from './subagent.jsx';
import { deliveryNote } from './composerModel.js';
import { useTick } from './RunLine.jsx';
import { DecidedRow, PermissionCard, QuestionCard } from './questions.jsx';

// 正文按内容记忆：流式片段到达时只重画在流的那一条，不重新解析前面的 Markdown
const sameText = (a, b) => a.item.text_md === b.item.text_md && a.item.streaming === b.item.streaming && a.item.anchors === b.item.anchors;
const AssistantText = memo(function AssistantText({ item }) {
  const actions = useActions();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(item.text_md.replace(/\[\[|\]\]/g, '').replace(/\[\^\d+\]/g, ''));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast('复制失败，请手动选择文字', 'alert');
    }
  };
  return (
    <div className="wb-msg">
      <Md text={item.text_md} anchors={item.anchors} />
      {item.streaming ? <span className="wb-caret" /> : null}
      {!item.streaming ? (
        <div className="g2-acts wb-msg-acts">
          <span className={copied ? 'on' : ''} onClick={copy}><Icon name={copied ? 'check' : 'copy'} size={12} />{copied ? '已复制' : '复制'}</span>
        </div>
      ) : null}
    </div>
  );
}, sameText);

function ProgressItem({ item }) {
  return item.title ? <WxProc group={item} /> : <TimelineGroup group={item} />;
}

// 气泡下面的送达说明：运行中插的话等了多久、在等什么；等久了给「打断并发送」
function DeliveryNote({ item }) {
  const actions = useActions();
  const waiting = item.delivery === 'steer' && (item.delivery_state?.state || 'pending') === 'pending';
  const now = useTick(waiting, 15000);
  const note = deliveryNote(item, now);
  if (!note) return null;
  return (
    <div className="xs faint mt4 row g6">
      <span>{note.text}</span>
      {note.stopAndSend && actions.stopAndSend && item.message_id ? <span className="link" onClick={() => actions.stopAndSend(item.message_id)}>打断并发送</span> : null}
    </div>
  );
}

function UserMessage({ item, enter }) {
  const o = item.origin;
  const fromPage = o && o.page && o.page !== 'wb-l2' && o.label;
  return (
    <div className={cx('m-u', enter && 'mo-send')}>
      <div style={{ maxWidth: '76%', display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
        {item.attachments?.length ? <div className="att">{item.attachments.map((a) => <WxFile key={a.upload_id} name={a.filename} kind={a.kind} />)}</div> : null}
        <div className="bub">
          {fromPage ? <div className="xs muted row g4" style={{ marginBottom: 3 }}><Icon name="puzzle" size={12} />来自「{o.label}」{o.kind === 'page' ? '页' : ''}</div> : null}
          {item.text}
        </div>
        <DeliveryNote item={item} />
      </div>
    </div>
  );
}

export function AssistantItem({ it, ctx }) {
  switch (it.kind) {
    case 'assistant': return <AssistantText item={it} />;
    case 'progress': return <ProgressItem item={it} />;
    case 'card': return <CardItem card={it.card} scope={ctx.scope} cardKey={it.key} />;
    case 'permission': return <div data-qid={it.request_id}><PermissionCard item={it} /></div>;
    case 'error': return <ErrorItem item={it} />;
    case 'interrupted': return <InterruptedItem item={it} />;
    case 'config_pending': return <ConfigPendingItem item={it} />;
    case 'reconnected': return <ReconnectedItem item={it} />;
    case 'compacted': return <CompactedItem item={it} />;
    case 'subagent': return <SubagentCard s={it} ended={!ctx.running} />;
    default: return null;
  }
}

// 卡片类的条目入场带弹性，文字类只上浮淡入
const POP_KINDS = new Set(['card', 'permission', 'subagent', 'progress', 'error', 'interrupted']);

// 入场动画只给打开之后新出现的条目：历史一到就记下当时已有的键，往前翻出来的旧记录也记下，这些不播
// （切对话时随状态重置清空）
function useFreshKeys(state) {
  const seen = useRef(null);
  if (!state.loaded) {
    seen.current = null;
    return () => false;
  }
  if (!seen.current) seen.current = { known: new Set(state.order), gen: state.historyGen || 0 };
  if ((state.historyGen || 0) !== seen.current.gen) {
    (state.prependedKeys || []).forEach((k) => seen.current.known.add(k));
    seen.current.gen = state.historyGen || 0;
  }
  const { known } = seen.current;
  return (key) => !known.has(key);
}

// 时间线顶部：还有更早的记录时，用户往上滚到这里自动往前翻一页（也可以点）。
// 打开页面时不自动翻：内容不满一屏时感应条一直可见，会接连翻好几页、反复重画，拖慢首屏
export function EarlierHistory({ older, onLoad, rootRef }) {
  const ref = useRef(null);
  const [armed, setArmed] = useState(false);
  const { hasMore, loading } = older || {};
  useEffect(() => {
    const root = rootRef?.current;
    if (!root || armed) return undefined;
    let last = root.scrollTop;
    const onScroll = () => {
      if (root.scrollTop < last - 4) setArmed(true);
      last = root.scrollTop;
    };
    root.addEventListener('scroll', onScroll, { passive: true });
    return () => root.removeEventListener('scroll', onScroll);
  }, [rootRef, armed]);
  useEffect(() => {
    const el = ref.current;
    if (!el || !armed || !hasMore || loading || typeof IntersectionObserver === 'undefined') return undefined;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) onLoad();
    }, { root: rootRef?.current || null, rootMargin: '600px 0px 0px 0px' });
    io.observe(el);
    return () => io.disconnect();
  }, [armed, hasMore, loading, onLoad, rootRef]);
  if (!hasMore) return null;
  return (
    <div ref={ref} className="wb-earlier">
      {loading ? <span className="spin" /> : <span className="link" onClick={onLoad}>更早的记录</span>}
    </div>
  );
}

const NOTICE_ICON = { plugin_installed: 'puzzle' };

// 在做什么、停止按钮在输入框那里（RunLine、Composer），时间线末尾不再放「思考中」和打断。
// lead：放在时间线最前面（开头的日期之后）的一块，如插件对话的「已切到插件开发」
export function Timeline({ state, scope, lead }) {
  const blocks = timelineBlocks(state);
  const fresh = useFreshKeys(state);
  const running = ['running', 'connecting'].includes(state.session.state);
  const lastOpenQ = [...blocks].reverse().find((b) => b.type === 'question');
  const rendered = blocks.map((b) => {
    const enter = (cls) => (fresh(b.key) ? cls : undefined);
    switch (b.type) {
      case 'day': return <MDay key={b.key} label={b.item.label} className={enter('mo-fade')} />;
      case 'notice': return <MSys key={b.key} left={b.item.align === 'left'} icon={NOTICE_ICON[b.item.notice_kind]} className={enter('mo-fade')}>{b.item.text}</MSys>;
      case 'user': return <UserMessage key={b.item.local_key || b.key} item={b.item} enter={fresh(b.key)} />;
      case 'decided': return <DecidedRow key={b.key} item={b.item} />;
      case 'withdrawn': return <MSys key={b.key} left className={enter('mo-fade')}>这个问题已失效：{b.item.withdrawn_reason || b.item.question.title}</MSys>;
      case 'question': return <MA key={b.key} className={enter('mo-ask')}><div data-qid={b.item.question.question_id}><QuestionCard q={b.item.question} active={b === lastOpenQ} /></div></MA>;
      case 'assistant': {
        // 新的一块整块上浮；已有的块里后到的条目各自入场
        const blockFresh = fresh(b.items[0].key);
        return (
          <MA key={b.key} time={hhmm(b.ts)} className={blockFresh ? 'mo-up' : undefined}>
            {b.items.map((it, i) => (fresh(it.key) && (i > 0 || !blockFresh) ? (
              <div key={it.key} className={cx('mo-in', POP_KINDS.has(it.kind) ? 'mo-pop' : 'mo-up')}>
                <AssistantItem it={it} ctx={{ running, scope }} />
              </div>
            ) : (
              <Fragment key={it.key}>
                <AssistantItem it={it} ctx={{ running, scope }} />
              </Fragment>
            )))}
          </MA>
        );
      }
      default: return null;
    }
  });
  if (lead) rendered.splice(blocks[0]?.type === 'day' ? 1 : 0, 0, <Fragment key="lead">{lead}</Fragment>);
  return rendered;
}

