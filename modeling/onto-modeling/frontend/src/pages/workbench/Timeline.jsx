// 对话时间线：把状态机分好的块渲染成设计稿的 m-u / m-a / m-sys / wx-day / 决策卡
import { Fragment, useState } from 'react';
import { toast } from '../../app/toast.jsx';
import { timelineBlocks } from '../../state/conversation.js';
import { Button, Icon } from '../../ui/index.jsx';
import {
  CardItem, CompactedItem, ConfigPendingItem, ErrorItem, InterruptedItem, ReconnectedItem, useActions,
} from './cards.jsx';
import {
  MA, MDay, MSys, Md, SubagentRows, TimelineGroup, WxFile, WxProc, hhmm,
} from './parts.jsx';
import { DecidedRow, PermissionCard, QuestionCard } from './questions.jsx';

function AssistantText({ item }) {
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
}

function ProgressItem({ item, running, onInterrupt }) {
  return (
    <>
      {item.title ? <WxProc group={item} /> : <TimelineGroup group={item} />}
      {item.state === 'running' && running ? (
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
    case 'subagents': return <SubagentRows list={it.subagents} />;
    default: return null;
  }
}

export function Timeline({ state, scope, onInterrupt }) {
  const blocks = timelineBlocks(state);
  const running = ['running', 'connecting'].includes(state.session.state);
  const lastOpenQ = [...blocks].reverse().find((b) => b.type === 'question');
  return blocks.map((b) => {
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
                <AssistantItem it={it} ctx={{ running, onInterrupt, scope }} />
              </Fragment>
            ))}
          </MA>
        );
      default: return null;
    }
  });
}

