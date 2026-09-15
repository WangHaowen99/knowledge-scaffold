import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  store,
  actions,
  materialPurposeLabel,
  pendingMaterialPurposeReviews,
} from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { Block } from './blocks/Blocks.jsx';
import { CONVERSATIONS } from '../lib/mock/conversations.js';
import ActivityLine from './ActivityLine.jsx';
import Composer from './Composer.jsx';
import Icon from '../ui/Icon.jsx';
import { userFacingText } from '../lib/userFacingText.js';
import SemanticBindingPanel from '../../SemanticBindingPanel.jsx';
import CandidateRevisionPanel from '../../CandidateRevisionPanel.jsx';
import { ASSISTANT_SCROLL_ACTION, assistantScrollAction } from './scrollPolicy.js';

const selUser = (s) => s.userMessages;
const selScene = (s) => s.scene;
const selDemo = (s) => s.demo;
const selMessages = (s) => s.messages;
const selError = (s) => s.error;
const selRecovery = (s) => s.recovery;
const selPausedPlan = (s) => s.pausedPlan;
const selLoading = (s) => s.loading;
const selActivity = (s) => s.activity;
const selInteraction = (s) => s.interaction;
const selProjectId = (s) => s.projectId;
const selRevision = (s) => s.revision;
const selPackageVersion = (s) => s.packageVersion;
const selStatusText = (s) => s.statusText;
const selReplayUntilSeq = (s) => s.replayUntilSeq;
const selMaterials = (s) => s.materials;
const selCandidateRevisionRefreshKey = (s) => s.candidateRevisionRefreshKey;
const selPendingInteractionMessageId = (s) => {
  for (let index = s.messages.length - 1; index >= 0; index -= 1) {
    const interaction = s.messages[index]?.blocks?.find((block) => block.interaction)?.interaction;
    if (interaction?.status === 'pending') return interaction.id || s.messages[index].id;
  }
  return s.interaction?.status === 'pending' ? s.interaction?.id || '' : '';
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
  const recovery = useStore(store, selRecovery);
  const pausedPlan = useStore(store, selPausedPlan);
  const loading = useStore(store, selLoading);
  const activity = useStore(store, selActivity);
  const interaction = useStore(store, selInteraction);
  const projectId = useStore(store, selProjectId);
  const revision = useStore(store, selRevision);
  const packageVersion = useStore(store, selPackageVersion);
  const statusText = useStore(store, selStatusText);
  const replayUntilSeq = useStore(store, selReplayUntilSeq);
  const materials = useStore(store, selMaterials);
  const candidateRevisionRefreshKey = useStore(store, selCandidateRevisionRefreshKey);
  const interactionId = useStore(store, selPendingInteractionMessageId);
  const [candidateAttentionKey, setCandidateAttentionKey] = useState('');
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  const [pausedPlanBusy, setPausedPlanBusy] = useState('');
  const scroller = useRef(null);
  const stream = useRef(null);
  const followingTail = useRef(true);
  const draggingScroll = useRef(false);
  const previousInteractionId = useRef('');
  const previousProjectId = useRef(projectId);
  const wasReplayingHistory = useRef(false);
  const replayingHistory = !demo && Number(replayUntilSeq) > 0;
  const replayingHistoryRef = useRef(replayingHistory);
  replayingHistoryRef.current = replayingHistory;
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
  const purposeReviewTail = materials.map((material) => (
    `${material.id}:${material.purposeReview?.status || ''}:${material.purposeReview?.suggestedBy || ''}`
  )).join('|');
  const interactionInStream = Boolean(interaction && messages.some((message) => (
    message.blocks?.some((block) => block.interaction?.id === interaction.id)
  )));
  const restoredInteractionMessage = interaction && !interactionInStream
    ? messageForInteraction(interaction)
    : null;
  const interactionGroupCount = interaction?.kind === 'confirmation'
    ? (interaction.payload?.changeGroups || []).length
    : 0;
  const runRecovery = async () => {
    if (recoveryBusy) return;
    setRecoveryBusy(true);
    try {
      await actions.terminateStaleRun();
    } finally {
      setRecoveryBusy(false);
    }
  };
  const resumePausedPlan = async () => {
    if (!pausedPlan?.id || pausedPlanBusy) return;
    setPausedPlanBusy('resume');
    try { await actions.resumePausedPlan(pausedPlan.id); } finally { setPausedPlanBusy(''); }
  };
  const endPausedPlan = async () => {
    if (!pausedPlan?.id || pausedPlanBusy) return;
    setPausedPlanBusy('end');
    try { await actions.endPausedPlan(pausedPlan.id); } finally { setPausedPlanBusy(''); }
  };
  const focusPendingInteraction = (behavior = 'smooth') => {
    const element = scroller.current;
    const target = [...(stream.current?.querySelectorAll('[data-interaction-id]') || [])]
      .find((item) => item.dataset.interactionId === interactionId);
    if (!element || !target) return;
    followingTail.current = false;
    const top = Math.max(0, target.offsetTop - 12);
    if (behavior === 'instant') element.scrollTop = top;
    else element.scrollTo({ top, behavior: 'smooth' });
    target.querySelector('button, input, textarea')?.focus({ preventScroll: true });
  };

  useTailLayoutEffect(() => {
    const projectChanged = projectId !== previousProjectId.current;
    if (projectChanged) {
      previousProjectId.current = projectId;
      previousInteractionId.current = '';
      followingTail.current = true;
      draggingScroll.current = false;
      wasReplayingHistory.current = false;
    }
    const historyJustFinished = wasReplayingHistory.current && !replayingHistory;
    wasReplayingHistory.current = replayingHistory;
    const newInteraction = Boolean(interactionId && interactionId !== previousInteractionId.current);
    const scrollAction = assistantScrollAction({
      replayingHistory,
      historyJustFinished,
      followingTail: followingTail.current,
      hasNewInteraction: newInteraction,
    });
    if (scrollAction === ASSISTANT_SCROLL_ACTION.HOLD) return;
    previousInteractionId.current = interactionId;
    if (scrollAction === ASSISTANT_SCROLL_ACTION.FOCUS_INSTANT
      || scrollAction === ASSISTANT_SCROLL_ACTION.FOCUS_SMOOTH) {
      followingTail.current = false;
      requestAnimationFrame(() => focusPendingInteraction(
        scrollAction === ASSISTANT_SCROLL_ACTION.FOCUS_INSTANT ? 'instant' : 'smooth',
      ));
      return;
    }
    if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [user.length, scene, streamTail, activityTail, purposeReviewTail, interactionId, projectId, replayingHistory]);

  useTailLayoutEffect(() => {
    const element = scroller.current;
    const content = stream.current;
    if (!element || !content) return undefined;

    const scrollToTail = () => {
      if (!replayingHistoryRef.current && followingTail.current) {
        element.scrollTop = element.scrollHeight;
      }
    };
    scrollToTail();

    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(scrollToTail);
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  useTailLayoutEffect(() => {
    if (!candidateAttentionKey) return;
    followingTail.current = true;
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' });
  }, [candidateAttentionKey]);

  useEffect(() => {
    const focusPending = (event) => {
      followingTail.current = true;
      const interactionId = String(event.detail?.interactionId || '');
      const pending = [...(stream.current?.querySelectorAll('[data-pending-interaction]') || [])]
        .find((element) => !interactionId || element.dataset.pendingInteraction === interactionId);
      if (pending) {
        pending.scrollIntoView({ block: 'center', behavior: 'smooth' });
        pending.querySelector('button:not([disabled]), textarea:not([disabled]), input:not([disabled])')
          ?.focus({ preventScroll: true });
        return;
      }
      if (scroller.current) scroller.current.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' });
    };
    window.addEventListener('mw-pending-attention', focusPending);
    return () => window.removeEventListener('mw-pending-attention', focusPending);
  }, []);

  return (
    <>
      <div className="wb-head">
        <b>建模助手</b>
        {statusText && <span className="wb-head-status ellip" title={statusText}>{userFacingText(statusText)}</span>}
      </div>

      {interaction && (
        <button type="button" className="wb-current-task" onClick={() => focusPendingInteraction('smooth')}>
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

      <div ref={scroller} className="wb-scroll" aria-busy={replayingHistory || undefined}
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
          {!demo && <MaterialPurposeBatchCard materials={materials} />}
          {!demo && projectId && <CandidateRevisionPanel
            projectId={projectId}
            refreshKey={candidateRevisionRefreshKey}
            onVisibilityChange={setCandidateAttentionKey}
            onPreview={actions.openCandidateRevisionPreview}
            onChanged={async () => {
              actions.refreshCandidateRevisions();
              await actions.refreshSnapshot();
            }}
            title="需要你确认的修改"
          />}
          {!demo && !base.length && !restoredInteractionMessage
            && pendingMaterialPurposeReviews(materials).length === 0
            && !candidateAttentionKey
            && <div className="empty">{loading ? '正在加载项目…' : '助手接手后，进度、问题和变更都会出现在这里'}</div>}
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
                {typeof error === 'object' && error?.staleRun && (
                  <div className="btn-row" style={{ marginTop: 'var(--s-2)' }}>
                    <button type="button" className="btn btn-p btn-sm" disabled={recoveryBusy}
                      onClick={() => void runRecovery()}>安全终止失效计划后重新规划</button>
                  </div>
                )}
              </div>
            </div>
          )}
          {pausedPlan && (
            <div className="card card-wait" role="status">
              <div className="card-h">
                <b>未完成的建模计划已暂停</b>
                <span className="spacer" />
                {Number(pausedPlan.remaining_count) || 0} 项待完成
              </div>
              <div className="card-b t-sm">
                <div>{userFacingText(pausedPlan.goal || '计划进度和候选内容已保留。')}</div>
                <ul style={{ margin: 'var(--s-2) 0 0', paddingLeft: 'var(--s-3)' }}>
                  {(pausedPlan.tasks || [])
                    .filter((task) => task.required && !['accepted', 'skipped'].includes(task.status))
                    .slice(0, 6)
                    .map((task) => <li key={task.id}>{userFacingText(task.title)}</li>)}
                </ul>
                <div className="btn-row" style={{ marginTop: 'var(--s-3)' }}>
                  <button type="button" className="btn btn-p btn-sm" disabled={Boolean(pausedPlanBusy)}
                    onClick={() => void resumePausedPlan()}>
                    {pausedPlanBusy === 'resume' ? '正在恢复…' : '继续未完成计划'}
                  </button>
                  <button type="button" className="btn btn-danger btn-sm" disabled={Boolean(pausedPlanBusy)}
                    onClick={() => void endPausedPlan()}>
                    {pausedPlanBusy === 'end' ? '正在结束…' : '结束该计划并重新规划'}
                  </button>
                </div>
              </div>
            </div>
          )}
          {recovery && (
            <div className="card card-stale" role="alert">
              <div className="card-h">
                <b>建模运行需要恢复</b>
                <span className="spacer" />
                <button className="btn btn-ghost" aria-label="刷新恢复状态" title="刷新"
                  onClick={() => void actions.refreshAgentState()}><Icon name="history" size={13} /></button>
              </div>
              <div className="card-b t-sm">
                <div>{recoveryStatusText(recovery.status)}</div>
                {Array.isArray(recovery.nextSteps) && recovery.nextSteps.length > 0 && (
                  <ul style={{ margin: 'var(--s-2) 0 0', paddingLeft: 'var(--s-3)' }}>
                    {recovery.nextSteps.map((step) => <li key={step}>{userFacingText(step)}</li>)}
                  </ul>
                )}
                {(recovery.status === 'stale_run' || recovery.status === 'plan_unreadable') && (
                  <div className="btn-row" style={{ marginTop: 'var(--s-2)' }}>
                    <button type="button" className="btn btn-p btn-sm" disabled={recoveryBusy}
                      onClick={() => void runRecovery()}>安全终止失效计划后重新规划</button>
                  </div>
                )}
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

export function MaterialPurposeBatchCard({ materials }) {
  const [saving, setSaving] = useState(false);
  const pending = pendingMaterialPurposeReviews(materials);
  if (!pending.length) return null;
  const ready = pending.every((material) => material.purposeReview?.suggestedBy === 'agent');
  const first = pending.find((material) => material.purposeReview?.suggestedBy === 'agent') || pending[0];
  const confirmAll = async () => {
    if (!ready || saving) return;
    setSaving(true);
    try {
      await actions.confirmAllMaterialPurposeReviews();
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="wb-msg" aria-label="资料用途批量确认">
      <div className="wb-who"><b>助手</b></div>
      <div className="wb-msg-assistant-body">
        <div className="card card-wait wb-purpose-batch">
          <div className="card-h">确认资料用途<span className="spacer" />{pending.length} 份</div>
          <div className="card-b">
            <div className="wb-purpose-batch-intro">
              {ready ? '已完成本批资料的用途预判，请确认是否采用。' : '正在分析本批资料用途，完成后可一键确认。'}
            </div>
            <ul className="wb-purpose-batch-files">
              {pending.map((material) => {
                const review = material.purposeReview || {};
                const analyzed = review.suggestedBy === 'agent';
                const sheetSuggestions = review.sheetSuggestions || [];
                const objectSheetCount = sheetSuggestions.filter((item) => (
                  item.importDisposition === 'object'
                )).length;
                return <li key={material.id}>
                  <span title={material.filename || material.name}>{material.filename || material.name}</span>
                  <div className="wb-purpose-batch-result">
                    <b className={analyzed ? '' : 'pending'}>
                      {analyzed ? materialPurposeLabel(review.suggestedPurpose) : '分析中'}
                    </b>
                    {analyzed && sheetSuggestions.length > 0 && <small>{objectSheetCount} 个实体 Sheet</small>}
                  </div>
                </li>;
              })}
            </ul>
            <div className="btn-row">
              <button className="btn btn-p" type="button" disabled={!ready || saving}
                aria-busy={saving} onClick={() => void confirmAll()}>
                {saving ? '确认中…' : '一键确认全部'}
              </button>
              <button className="btn" type="button" disabled={saving}
                onClick={() => actions.selectMaterial(first.id)}>
                逐个调整
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function messageForInteraction(interaction) {
  const type = (() => { if (interaction.kind === 'confirmation') return 'confirm'; return interaction.kind === 'release_confirmation' ? 'release' : 'ask'; })();
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
        <b>{(() => { if (mine) return '你'; return system ? '资料' : '助手'; })()}</b>{at ? ` · ${at}` : ''}
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

function recoveryStatusText(status) {
  return ({
    stale_run: '当前运行占用了建模计划，但运行记录已丢失，旧计划不可执行，也无法直接新建计划。',
    plan_unreadable: '存在活动计划但当前运行无法读取，需要先安全终止失效计划。',
    checkpoint_missing: '检查点记录缺失，恢复不完整。',
    volume_mismatch: '运行与检查点数据库不在同一持久卷，恢复不完整。',
    resume_failed: '自动恢复失败，已安全停在最近进展，可取消当前运行后重新规划。',
  })[status] || '建模运行需要人工恢复。';
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
    if (answer.displayText && answer.text) {
      return `${visible(answer.displayText)}\n补充说明：${visible(answer.text)}`;
    }
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
