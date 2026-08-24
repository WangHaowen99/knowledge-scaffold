import { useEffect, useLayoutEffect, useRef } from 'react';
import { store, actions } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { Block } from './blocks/Blocks.jsx';
import { CONVERSATIONS } from '../lib/mock/conversations.js';
import ActivityLine from './ActivityLine.jsx';
import Composer from './Composer.jsx';
import Icon from '../ui/Icon.jsx';
import { userFacingText } from '../lib/userFacingText.js';

const selUser = (s) => s.userMessages;
const selScene = (s) => s.scene;
const selDemo = (s) => s.demo;
const selMessages = (s) => s.messages;
const selError = (s) => s.error;
const selLoading = (s) => s.loading;
const selActivity = (s) => s.activity;
const selInteraction = (s) => s.interaction;
const selPendingInteractionMessageId = (s) => {
  for (let index = s.messages.length - 1; index >= 0; index -= 1) {
    const interaction = s.messages[index]?.blocks?.find((block) => block.interaction)?.interaction;
    if (interaction && interaction.status !== 'answered') return interaction.id || s.messages[index].id;
  }
  return s.interaction?.status !== 'answered' ? s.interaction?.id || '' : '';
};
const useTailLayoutEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect;

/* 只有一条流：状态、计划、问题、diff、确认、策略都是流里的消息。
   独立的待办队列只在顶栏计数点开时以浮层出现，不常驻。 */
export default function RightPane() {
  const user = useStore(store, selUser);
  const scene = useStore(store, selScene);
  const demo = useStore(store, selDemo);
  const messages = useStore(store, selMessages);
  const error = useStore(store, selError);
  const loading = useStore(store, selLoading);
  const activity = useStore(store, selActivity);
  const interaction = useStore(store, selInteraction);
  const interactionId = useStore(store, selPendingInteractionMessageId);
  const scroller = useRef(null);
  const stream = useRef(null);
  const followingTail = useRef(true);
  const draggingScroll = useRef(false);
  const previousInteractionId = useRef('');
  const base = demo ? CONVERSATIONS[scene] || [] : messages;
  const streamTail = messages.map((message) => `${message.id}:${message.streaming ? 1 : 0}:${message.blocks?.[0]?.text?.length || 0}`).join('|');
  const activityTail = activity
    ? `${activity.id || activity.text}:${activity.detail || ''}:${activity.progress?.current || 0}`
    : '';
  const interactionInStream = Boolean(interaction && messages.some((message) => (
    message.blocks?.some((block) => block.interaction?.id === interaction.id)
  )));
  const restoredInteractionMessage = interaction && !interactionInStream
    ? messageForInteraction(interaction)
    : null;

  useTailLayoutEffect(() => {
    const newInteraction = Boolean(interactionId && interactionId !== previousInteractionId.current);
    previousInteractionId.current = interactionId;
    if (!followingTail.current && !newInteraction) return;
    if (newInteraction) followingTail.current = true;
    if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [user.length, scene, streamTail, activityTail, interactionId]);

  useTailLayoutEffect(() => {
    const element = scroller.current;
    const content = stream.current;
    if (!element || !content) return undefined;

    const scrollToTail = () => {
      if (followingTail.current) element.scrollTop = element.scrollHeight;
    };
    scrollToTail();

    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(scrollToTail);
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  return (
    <>
      <div className="wb-head">
        <b>建模助手</b>
      </div>

      <div ref={scroller} className="wb-scroll"
        onWheel={(event) => { if (event.deltaY < 0) followingTail.current = false; }}
        onPointerDown={() => { draggingScroll.current = true; }}
        onPointerUp={() => { draggingScroll.current = false; }}
        onPointerCancel={() => { draggingScroll.current = false; }}
        onKeyDown={(event) => {
          if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) followingTail.current = false;
          if (event.key === 'End') followingTail.current = true;
        }}
        onScroll={(event) => {
        const element = event.currentTarget;
        const nearTail = element.scrollHeight - element.scrollTop - element.clientHeight < 72;
        if (nearTail) followingTail.current = true;
        else if (draggingScroll.current) followingTail.current = false;
      }}>
        <div ref={stream} className="wb-stream">
          {base.map((m) => <Message key={m.id} m={m} />)}
          {user.map((m) => <Message key={m.id} m={m} />)}
          {!demo && restoredInteractionMessage && <Message m={restoredInteractionMessage} />}
          {!demo && !base.length && !restoredInteractionMessage && <div className="empty">{loading ? '正在加载项目…' : '助手接手后，进度、问题和变更都会出现在这里'}</div>}
          {error && (
            <div className="card card-stale" role="alert">
              <div className="card-h">
                <b>有一项内容未能读取</b>
                <span className="spacer" />
                <button className="btn btn-ghost" aria-label="关闭错误提示" title="关闭"
                  onClick={actions.clearError}><Icon name="x" size={13} /></button>
              </div>
              <div className="card-b t-sm">{userFacingText(error)}</div>
            </div>
          )}
          <ActivityLine />
          <div />
        </div>
      </div>

      <Composer />
    </>
  );
}

export function messageForInteraction(interaction) {
  const type = interaction.kind === 'confirmation'
    ? 'confirm'
    : interaction.kind === 'release_confirmation' ? 'release' : 'ask';
  return {
    id: `interaction-${interaction.id}`,
    role: 'assistant',
    createdAt: interaction.createdAt,
    blocks: [{ type, interaction }],
  };
}

function Message({ m }) {
  const mine = m.role === 'user';
  const system = m.role === 'system';
  const at = m.at || formatTime(m.createdAt);
  return (
    <div className="wb-msg">
      <div className="wb-who">
        <b>{mine ? '你' : system ? '资料' : '助手'}</b>{at ? ` · ${at}` : ''}
        {m.focus && (
          <>
            <span className="spacer" />
            {/* 助手说「看这个」，点一下中栏就跳过去并重新跟随 */}
            <button className="wb-jump" onClick={() => actions.focusFrom(m.focus)}>
              <Icon name="arrow" size={11} />{m.focusLabel || '定位'}
            </button>
          </>
        )}
      </div>
      {mine ? (
        <div className="wb-bubble">{messageText(m.blocks?.[0], false)}</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
          {m.blocks.map((b, i) => <Block key={i} block={b} />)}
          {m.streaming && <span className="wb-stream-cursor" aria-label="助手正在输出" />}
        </div>
      )}
    </div>
  );
}

export function messageText(block = {}, sanitize = true) {
  const visible = (value) => sanitize ? userFacingText(value) : String(value ?? '');
  if (block.text) return visible(block.text);
  if (block.type === 'answer') {
    const answer = block.answer || {};
    if (answer.displayText) return visible(answer.displayText);
    if (answer.approved === true) return '已接受所选修改';
    if (answer.approved === false) return answer.feedback ? `已退回调整：${visible(answer.feedback)}` : '已退回调整';
    return visible(answer.displayText || answer.text || answer.value || answer.feedback
      || (answer.selected || answer.selected_groups || []).join('、')
      || '已提交选择');
  }
  return '';
}

function formatTime(value) {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
    : '';
}
