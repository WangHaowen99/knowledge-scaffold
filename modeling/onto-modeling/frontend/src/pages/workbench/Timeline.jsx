// 对话时间线：把状态机分好的块渲染成设计稿的 m-u / m-a / m-sys / wx-day / 决策卡
import { Fragment, memo, useState } from 'react';
import { toast } from '../../app/toast.jsx';
import { liveActivity, timelineBlocks } from '../../state/conversation.js';
import { Button, Icon } from '../../ui/index.jsx';
import {
  CardItem, CompactedItem, ConfigPendingItem, ErrorItem, InterruptedItem, ReconnectedItem, useActions,
} from './cards.jsx';
import {
  LiveRow, MA, MDay, MSys, Md, SubagentCard, TimelineGroup, WxFile, WxProc, hhmm,
} from './parts.jsx';
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
          {actions.canSaveKnowledge ? <span onClick={() => actions.saveKnowledge?.(item)}><Icon name="book" size={12} />沉淀为业务知识</span> : null}
        </div>
      ) : null}
    </div>
  );
}, sameText);

function ProgressItem({ item, running, onInterrupt }) {
  return (
    <>
      {item.title ? <WxProc group={item} /> : <TimelineGroup group={item} />}
      {item.state === 'running' && running && onInterrupt ? (
        <div className="row mt8">
          <Button size="sm" kind="danger" icon="stop" title="打断（Esc）" onClick={onInterrupt}>打断</Button>
        </div>
      ) : null}
    </>
  );
}

function UserMessage({ item }) {
  const o = item.origin;
  const fromPage = o && o.page && o.page !== 'wb-l2' && o.label;
  return (
    <div className="m-u">
      <div style={{ maxWidth: '76%', display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
        {item.attachments?.length ? <div className="att">{item.attachments.map((a) => <WxFile key={a.upload_id} name={a.filename} kind={a.kind} />)}</div> : null}
        <div className="bub">
          {fromPage ? <div className="xs muted row g4" style={{ marginBottom: 3 }}><Icon name="puzzle" size={12} />来自「{o.label}」{o.kind === 'page' ? '页' : ''}</div> : null}
          {item.text}
        </div>
        {item.delivery === 'deferred' ? <div className="xs faint mt4">这一步结束后送达</div> : null}
        {item.delivery === 'queued' ? <div className="xs faint mt4">排队中</div> : null}
      </div>
    </div>
  );
}

export function AssistantItem({ it, ctx }) {
  switch (it.kind) {
    case 'assistant': return <AssistantText item={it} />;
    case 'progress': return <ProgressItem item={it} running={ctx.running} onInterrupt={ctx.onInterrupt} />;
    case 'card': return <CardItem card={it.card} scope={ctx.scope} />;
    case 'permission': return <PermissionCard item={it} />;
    case 'error': return <ErrorItem item={it} />;
    case 'interrupted': return <InterruptedItem item={it} />;
    case 'config_pending': return <ConfigPendingItem item={it} />;
    case 'reconnected': return <ReconnectedItem item={it} />;
    case 'compacted': return <CompactedItem item={it} />;
    case 'subagent': return <SubagentCard s={it} />;
    default: return null;
  }
}

// 时间线末尾：助手在跑时的「思考中」和打断（打断放在最后，不随某一段工作过程走）
function LiveTail({ activity, onInterrupt }) {
  return (
    <>
      {activity ? <LiveRow thinking={activity.thinking} /> : null}
      {onInterrupt ? (
        <div className="row mt8">
          <Button size="sm" kind="danger" icon="stop" title="打断（Esc）" onClick={onInterrupt}>打断</Button>
        </div>
      ) : null}
    </>
  );
}

export function Timeline({ state, scope, onInterrupt, live = true }) {
  const blocks = timelineBlocks(state);
  const running = ['running', 'connecting'].includes(state.session.state);
  const lastOpenQ = [...blocks].reverse().find((b) => b.type === 'question');
  // 自主构建的运行对话里分段进展块已经在说正在做什么，不再加「思考中」
  const activity = live ? liveActivity(state) : null;
  const tailInterrupt = running ? onInterrupt : null;
  const hasTail = !!(activity || tailInterrupt);
  const lastBlock = blocks[blocks.length - 1];
  const tail = hasTail ? <LiveTail activity={activity} onInterrupt={tailInterrupt} /> : null;
  const rendered = blocks.map((b) => {
    switch (b.type) {
      case 'day': return <MDay key={b.key} label={b.item.label} />;
      case 'notice': return b.item.align === 'left' ? <MSys key={b.key} left>{b.item.text}</MSys> : <MSys key={b.key}>{b.item.text}</MSys>;
      case 'user': return <UserMessage key={b.key} item={b.item} />;
      case 'decided': return <DecidedRow key={b.key} item={b.item} />;
      case 'withdrawn': return <MSys key={b.key} left>这个问题已失效：{b.item.withdrawn_reason || b.item.question.title}</MSys>;
      case 'question': return <MA key={b.key}><QuestionCard q={b.item.question} active={b === lastOpenQ} /></MA>;
      case 'assistant':
        return (
          <MA key={b.key} time={hhmm(b.ts)}>
            {b.items.map((it) => (
              <Fragment key={it.key}>
                <AssistantItem it={it} ctx={{ running, onInterrupt: null, scope }} />
              </Fragment>
            ))}
            {b === lastBlock ? tail : null}
          </MA>
        );
      default: return null;
    }
  });
  // 最后一块不是助手的（刚发出消息）：「思考中」另起一块
  if (hasTail && lastBlock?.type !== 'assistant') rendered.push(<MA key="live-tail">{tail}</MA>);
  return rendered;
}

