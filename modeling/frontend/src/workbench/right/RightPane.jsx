import { useEffect, useLayoutEffect, useRef } from 'react';
import { store, actions } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { Block } from './blocks/Blocks.jsx';
import { CONVERSATIONS } from '../lib/mock/conversations.js';
import ActivityLine from './ActivityLine.jsx';
import Composer from './Composer.jsx';
import Icon from '../ui/Icon.jsx';
import { userFacingText } from '../lib/userFacingText.js';
import SemanticBindingPanel from '../../SemanticBindingPanel.jsx';

const selUser = (s) => s.userMessages;
const selScene = (s) => s.scene;
const selDemo = (s) => s.demo;
const selMessages = (s) => s.messages;
const selError = (s) => s.error;
const selLoading = (s) => s.loading;
const selActivity = (s) => s.activity;
const selInteraction = (s) => s.interaction;
const selProjectId = (s) => s.projectId;
const selRevision = (s) => s.revision;
const selPackageVersion = (s) => s.packageVersion;
const selStatusText = (s) => s.statusText;
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
  const projectId = useStore(store, selProjectId);
  const revision = useStore(store, selRevision);
  const packageVersion = useStore(store, selPackageVersion);
  const statusText = useStore(store, selStatusText);
  const interactionId = useStore(store, selPendingInteractionMessageId);
  const scroller = useRef(null);
  const stream = useRef(null);
  const followingTail = useRef(true);
  const draggingScroll = useRef(false);
  const previousInteractionId = useRef('');
  const base = demo ? CONVERSATIONS[scene] || [] : messages;
  const streamTail = messages.map((message) => {
    const blocks = (message.blocks || []).map((block) => (
      `${block.type}:${block.streaming ? 1 : 0}:${block.text?.length || 0}`
    )).join(',');
    return `${message.id}:${message.streaming ? 1 : 0}:${blocks}`;
  }).join('|');
  const activityTail = activity
    ? `${activity.id || activity.text}:${activity.detail || ''}:${activity.progress?.current || 0}`
    : '';
  const interactionInStream = Boolean(interaction && messages.some((message) => (
    message.blocks?.some((block) => block.interaction?.id === interaction.id)
  )));
  const restoredInteractionMessage = interaction && !interactionInStream
    ? messageForInteraction(interaction)
    : null;
  const interactionGroupCount = interaction?.kind === 'confirmation'
    ? (interaction.payload?.changeGroups || []).length
    : 0;
  const focusPendingInteraction = () => {
    const element = scroller.current;
    const target = [...(stream.current?.querySelectorAll('[data-interaction-id]') || [])]
      .find((item) => item.dataset.interactionId === interactionId);
    if (!element || !target) return;
    followingTail.current = false;
    element.scrollTo({ top: Math.max(0, target.offsetTop - 12), behavior: 'smooth' });
    target.querySelector('button, input, textarea')?.focus({ preventScroll: true });
  };

  useTailLayoutEffect(() => {
    const newInteraction = Boolean(interactionId && interactionId !== previousInteractionId.current);
    previousInteractionId.current = interactionId;
    if (newInteraction) {
      followingTail.current = false;
      requestAnimationFrame(focusPendingInteraction);
      return;
    }
    if (!followingTail.current) return;
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
        {statusText && <span className="wb-head-status ellip" title={statusText}>{userFacingText(statusText)}</span>}
      </div>

      {interaction && (
        <button type="button" className="wb-current-task" onClick={focusPendingInteraction}>
          <span className="wb-current-task-dot" aria-hidden="true" />
          <span>
            <b>当前待处理决定</b>
            <small>{interactionGroupCount
              ? `1 个决定 · ${interactionGroupCount} 组变更`
              : userFacingText(interaction.payload?.question || '需要你确认后才能继续')}</small>
          </span>
          <Icon name="arrow" size={12} />
        </button>
      )}

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
                <b>{errorTitle(error)}</b>
                <span className="spacer" />
                <button className="btn btn-ghost" aria-label="关闭错误提示" title="关闭"
                  onClick={actions.clearError}><Icon name="x" size={13} /></button>
              </div>
              <div className="card-b t-sm">
                <div>{userFacingText(errorMessage(error))}</div>
                {typeof error === 'object' && error?.recovery && <small className="wb-error-recovery">{userFacingText(error.recovery)}</small>}
              </div>
            </div>
          )}
          {!demo && projectId && revision && packageVersion && (
            <SemanticBindingPanel
              projectId={projectId}
              packageRevision={revision}
              onApplied={() => void actions.refreshSnapshot()}
            />
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
    <div className={`wb-msg${mine ? ' wb-msg-user' : ''}${m.sendFailed ? ' wb-msg-failed' : ''}${m.incomplete ? ' wb-msg-incomplete' : ''}`}
      data-message-id={m.id}
      data-interaction-id={m.blocks?.find((block) => block.interaction)?.interaction?.id || undefined}>
      <div className="wb-who">
        <b>{mine ? '你' : system ? '资料' : '助手'}</b>{at ? ` · ${at}` : ''}
        {m.focus && (
          <>
            <span className="spacer" />
            <button className="wb-jump" onClick={() => actions.focusFrom(m.focus)}>
              <Icon name="arrow" size={11} />{m.focusLabel || '定位'}
            </button>
          </>
        )}
      </div>
      {mine ? (
        <><div className="wb-bubble">{messageText(m.blocks?.[0], false)}</div>
          {m.sendFailed && <div className="wb-send-failed" role="status">未发送，内容已保留在输入框</div>}</>
      ) : (
        <div className="wb-msg-assistant-body">
          {m.blocks.map((b, i) => <Block key={i} block={b} />)}
          {m.streaming && <span className="wb-stream-cursor" aria-label="助手正在输出" />}
          {m.incomplete && <div className="wb-incomplete-note" role="status">这段回复因中断未完成，不作为修改或确认结论</div>}
        </div>
      )}
    </div>
  );
}

function errorMessage(error) {
  return typeof error === 'object' && error ? error.message : error;
}

function errorTitle(error) {
  const scope = typeof error === 'object' && error ? error.scope : '';
  return ({
    send: '消息没有发送成功',
    write: '修改没有保存成功',
    confirmation: '确认没有提交成功',
    evaluation: '评测操作没有完成',
  })[scope] || '有一项内容暂时不可用';
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
