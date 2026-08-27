import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUpRight, BrainCircuit, Check, ChevronDown, Gauge, GitBranch, PanelRightClose, PanelRightOpen, Sparkles } from 'lucide-react';
import { useAuthContext } from '../auth';
import { packageDisplayLabel } from '../knowledge';
import { authFetch } from '../../shared/api/auth';
import { StatusTag } from '../../shared/ui/primitives';
import ConversationView from './components/ConversationView';
import ChatPromptInput from './components/ChatPromptInput';
import TracePanel from './components/TracePanel';
import useSmartScroll from './hooks/useSmartScroll';
import { buildConversationViewModel } from './lib/conversationModel';
import { formatTime } from './lib/chatPresentation';
import { getChatDataSourceStatus } from './lib/chatDataSourceStatus';
import { isBlankChatSubmitting, scheduleBlankChatFocus } from './lib/firstSendCoordinator';
import { applyInteractionCancellation, findPendingInteraction } from './lib/pendingInteraction';
import { createIdempotencyKey } from './lib/runAdmission';
import {
  resolveSessionPreferences,
} from './lib/sessionModel';
import { listResumableSessions } from './lib/sessionScope';
import { publicErrorMessage } from './lib/streamHelpers';
import { resolveRunProgressView } from './lib/streamLifecycle';
import { buildTraceViewModel } from './lib/traceModel';
import { getWorkspaceAgentMode, normalizeAgentMode } from './lib/workspacePreferences';
import './chat.css';

const safeLocalGet = (key) => {
  try { return localStorage.getItem(key) || ''; } catch { return ''; }
};

const safeLocalSet = (key, value) => {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    // Storage is an optional convenience; runtime state remains authoritative.
  }
};

const STARTER_PROMPTS = [
  '总结当前知识包的核心业务对象与关系',
  '分析一个典型业务变更的影响范围',
  '列出当前知识包可以回答的典型问题',
];

const AGENT_MODE_META = {
  quick: { label: '快速思考', description: '响应更快，适合直接问答', Icon: Sparkles },
  thinking: { label: '深度思考', description: '加强推理，适合复杂分析', Icon: BrainCircuit },
  multi_agent: { label: '多智能体', description: '拆分任务并协同处理', Icon: GitBranch },
};

function AgentModePicker({ value, onChange, disabled }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const current = AGENT_MODE_META[value] || AGENT_MODE_META.quick;
  const CurrentIcon = current.Icon;

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsideClick = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    const closeOnEscape = (event) => {
      if (event.key !== 'Escape') return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener('mousedown', closeOnOutsideClick);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutsideClick);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [open]);

  const toggle = () => {
    if (disabled) return;
    setOpen((wasOpen) => {
      const nextOpen = !wasOpen;
      if (nextOpen) {
        requestAnimationFrame(() => rootRef.current?.querySelector('[aria-checked="true"]')?.focus());
      }
      return nextOpen;
    });
  };

  const handleOptionKeyDown = (event) => {
    const options = [...(rootRef.current?.querySelectorAll('.mode-option') || [])];
    const index = options.indexOf(event.currentTarget);
    let nextIndex = index;
    if (event.key === 'ArrowDown') nextIndex = (index + 1) % options.length;
    else if (event.key === 'ArrowUp') nextIndex = (index - 1 + options.length) % options.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = options.length - 1;
    else return;
    event.preventDefault();
    options[nextIndex]?.focus();
  };

  return (
    <div className="mode-dropdown" ref={rootRef}>
      <button ref={triggerRef} type="button" className="mode-picker"
        aria-label={`对话模式：${current.label}`} aria-haspopup="menu" aria-expanded={open}
        title="选择对话模式" disabled={disabled} onClick={toggle}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (!open) toggle();
          }
        }}>
        <CurrentIcon className="mode-icon" size={14} strokeWidth={2} aria-hidden="true" />
        <span className="mode-label">{current.label}</span>
        <ChevronDown className={`mode-chevron ${open ? 'open' : ''}`} size={13} strokeWidth={2} aria-hidden="true" />
      </button>
      {open && (
        <div className="mode-menu" role="menu" aria-label="选择对话模式">
          <div className="mode-menu-title">对话模式</div>
          {Object.entries(AGENT_MODE_META).map(([mode, meta]) => {
            const OptionIcon = meta.Icon;
            const selected = mode === value;
            return (
              <button key={mode} type="button" className={`mode-option ${selected ? 'selected' : ''}`}
                role="menuitemradio" aria-checked={selected} onKeyDown={handleOptionKeyDown}
                onClick={() => { onChange(mode); setOpen(false); triggerRef.current?.focus(); }}>
                <span className="mode-option-icon"><OptionIcon size={15} strokeWidth={1.9} /></span>
                <span className="mode-option-copy">
                  <span className="mode-option-label">{meta.label}</span>
                  <span className="mode-option-desc">{meta.description}</span>
                </span>
                {selected && <Check className="mode-option-check" size={15} strokeWidth={2.2} aria-hidden="true" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// 新建对话（知识应用）：消息线程 + 输入区 + 右侧 Trace 竖栏。
// 历史对话列表在应用侧边栏（ChatHistorySidebar）；会话状态由 AppShell 的
// useChatSessions 下发。新版 PublicEvent 流由 useStreamManager / useAgenticStream 统一投影。
export default function ChatWorkspace({
  chat,
  streamManager,
  agenticStream,
  onOpenObservability,
  onConfigureDataSource,
}) {
  const { workspaceId, workspaceDataReady } = useAuthContext();
  const {
    sessions,
    currentChatId,
    messages,
    setMessages,
    createChat,
    firstSendPending,
    runFirstSend,
    hydrateChat,
    generateSessionTitle,
    touchSession,
  } = chat;
  const { sendAgentic, resumeAgentic } = agenticStream;
  const [input, setInput] = useState('');
  const [interactionCancelError, setInteractionCancelError] = useState('');
  const inputRef = useRef(null);
  const currentChatIdRef = useRef(currentChatId);
  const [traceOpen, setTraceOpen] = useState(() => {
    try { return !window.matchMedia('(max-width: 900px)').matches; } catch { return true; }
  });
  const traceToggleLabel = traceOpen ? '收起运行轨迹' : '展开运行轨迹';
  // Agent 模式：quick(快速) / thinking(深度思考) / multi_agent(多智能体)。
  // 按 workspace 持久化到 localStorage，默认 quick。
  const agentModeKey = `mw_ws_agentmode_${workspaceId}`;
  const [agentMode, setAgentMode] = useState(
    () => getWorkspaceAgentMode(localStorage, workspaceId)
  );
  const changeAgentMode = (mode) => {
    const nextMode = normalizeAgentMode(mode);
    setAgentMode(nextMode);
    safeLocalSet(agentModeKey, nextMode);
  };
  const [modelProfiles, setModelProfiles] = useState([]);
  const [defaultModelProfileId, setDefaultModelProfileId] = useState('');
  const [modelProfileId, setModelProfileId] = useState('');
  const [ontologies, setOntologies] = useState([]);
  const [selectedOntologyId, setSelectedOntologyId] = useState('');
  const [selectedOntologyVersion, setSelectedOntologyVersion] = useState('');
  // Trace 栏宽度：可拖拽分隔条调整，最小 300（初始尺寸），持久化到 localStorage。
  const TRACE_MIN_W = 300;
  const [traceWidth, setTraceWidth] = useState(
    () => Math.max(TRACE_MIN_W, Number(localStorage.getItem('mw_trace_w')) || TRACE_MIN_W)
  );
  const [traceMaxWidth, setTraceMaxWidth] = useState(TRACE_MIN_W);
  const wrapRef = useRef(null);
  const startResize = (e) => {
    e.preventDefault();
    const onMove = (ev) => {
      const wrap = wrapRef.current;
      if (!wrap) return;
      const right = wrap.getBoundingClientRect().right;
      // 分隔条向左拖 → Trace 变大；右边界减去光标 X 即 Trace 宽度
      const next = Math.round(right - ev.clientX);
      const maxW = wrap.getBoundingClientRect().width - 360; // 给对话区留最小空间
      const clamped = Math.max(TRACE_MIN_W, Math.min(next, Math.max(TRACE_MIN_W, maxW)));
      setTraceWidth(clamped);
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.userSelect = '';
      try { localStorage.setItem('mw_trace_w', String(traceWidthRef.current)); } catch { /* ignore */ }
    };
    document.body.style.userSelect = 'none';
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };
  const traceWidthRef = useRef(traceWidth);
  const resumedRunsRef = useRef(new Set());

  useEffect(() => {
    traceWidthRef.current = traceWidth;
  }, [traceWidth]);

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(([entry]) => {
      const maxWidth = Math.max(TRACE_MIN_W, entry.contentRect.width - 360);
      setTraceMaxWidth(maxWidth);
      setTraceWidth((currentWidth) => Math.min(Math.max(TRACE_MIN_W, currentWidth), maxWidth));
    });
    observer.observe(wrap);
    return () => observer.disconnect();
  }, []);

  const current = sessions.find((session) => session.id === currentChatId);
  const selectedOntology = useMemo(
    () => ontologies.find((ontology) => ontology.id === selectedOntologyId) || null,
    [ontologies, selectedOntologyId],
  );
  const displayedOntologyVersion = useMemo(() => (
    String(
      current?.metadata?.selected_ontology_version
      || current?.metadata?.selected_version
      || selectedOntologyVersion
      || selectedOntology?.active_version
      || selectedOntology?.version
      || ''
    ).trim()
  ), [current?.metadata, selectedOntology, selectedOntologyVersion]);
  const emptyState = useMemo(
    () => getChatDataSourceStatus(selectedOntology),
    [selectedOntology],
  );
  const conversationModel = useMemo(() => buildConversationViewModel(messages), [messages]);
  const traceModel = useMemo(() => buildTraceViewModel(conversationModel), [conversationModel]);
  const {
    containerRef: bodyRef,
    isFollowing: isFollowingMessages,
    onScroll: handleMessageScroll,
    scrollToBottom,
  } = useSmartScroll(messages, `${workspaceId}:${currentChatId || 'new'}`);
  const sessionLocked = Boolean(currentChatId);
  const pendingInteraction = useMemo(() => findPendingInteraction(messages), [messages]);

  useEffect(() => {
    currentChatIdRef.current = currentChatId;
  }, [currentChatId]);

  useEffect(() => {
    setInteractionCancelError('');
  }, [currentChatId, pendingInteraction?.id]);

  useEffect(() => {
    let cancelled = false;
    const loadOptions = async () => {
      if (!workspaceDataReady) {
        setModelProfiles([]);
        setDefaultModelProfileId('');
        setOntologies([]);
        return;
      }
      try {
        const [modelsResponse, ontologiesResponse] = await Promise.all([
          authFetch('/agentic/model-profiles'),
          authFetch('/agentic/ontologies'),
        ]);
        const modelsPayload = await modelsResponse.json().catch(() => ({}));
        const ontologiesPayload = await ontologiesResponse.json().catch(() => ({}));
        if (cancelled) return;
        const profiles = modelsResponse.ok && Array.isArray(modelsPayload.profiles) ? modelsPayload.profiles : [];
        const availableOntologies = ontologiesResponse.ok && Array.isArray(ontologiesPayload.ontologies)
          ? ontologiesPayload.ontologies.filter((ontology) => ontology.enabled !== false)
          : [];
        setModelProfiles(profiles);
        setDefaultModelProfileId(modelsPayload.default_profile_id || profiles[0]?.id || '');
        setOntologies(availableOntologies);
      } catch {
        if (!cancelled) {
          setModelProfiles([]);
          setDefaultModelProfileId('');
          setOntologies([]);
        }
      }
    };
    const timer = setTimeout(loadOptions, 0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [workspaceDataReady, workspaceId]);

  useEffect(() => {
    const metadata = current?.metadata || {};
    const timer = setTimeout(() => {
      const preferences = resolveSessionPreferences({
        metadata,
        storedAgentMode: safeLocalGet(agentModeKey),
        storedModelProfileId: safeLocalGet(`mw_ws_model_${workspaceId}`),
        storedOntologyId: currentChatId
          ? safeLocalGet(`mw_session_ontology_${workspaceId}_${currentChatId}`)
          : '',
        storedOntologyVersion: '',
        defaultModelProfileId,
      });
      setAgentMode(preferences.agentMode);
      setModelProfileId(preferences.modelProfileId);
      setSelectedOntologyId(preferences.selectedOntologyId);
      setSelectedOntologyVersion(preferences.selectedOntologyVersion);
    }, 0);
    return () => clearTimeout(timer);
  }, [agentModeKey, current?.metadata, currentChatId, defaultModelProfileId, workspaceId]);

  useEffect(() => {
    if (currentChatId) return;
    const nextVersion = selectedOntologyId
      ? String(selectedOntology?.active_version || selectedOntology?.version || '').trim()
      : '';
    if (nextVersion !== selectedOntologyVersion) {
      setSelectedOntologyVersion(nextVersion);
    }
  }, [currentChatId, selectedOntology, selectedOntologyId, selectedOntologyVersion]);

  useEffect(() => {
    let cancelled = false;
    for (const resumable of listResumableSessions(sessions)) {
      const resumeKey = `${resumable.sessionId}:${resumable.runId}`;
      if (resumedRunsRef.current.has(resumeKey)) continue;
      resumedRunsRef.current.add(resumeKey);
      void hydrateChat(resumable.sessionId).then((data) => {
        if (cancelled || !data) {
          resumedRunsRef.current.delete(resumeKey);
          return false;
        }
        return resumeAgentic({
          runId: String(data.active_run_id || resumable.runId),
          targetSessionId: resumable.sessionId,
          afterSequence: data.last_seq ?? resumable.afterSequence,
          status: String(data.active_run_status || resumable.status),
        });
      }).then((resumed) => {
        if (!resumed) resumedRunsRef.current.delete(resumeKey);
      });
    }
    return () => { cancelled = true; };
  }, [hydrateChat, resumeAgentic, sessions]);

  useEffect(() => {
    resumedRunsRef.current = new Set();
  }, [workspaceId]);

  const send = async () => {
    const text = input.trim();
    if (!workspaceDataReady || !text || streamState.isLoading || pendingInteraction) return;

    const startRun = (sessionId, isNew = false) => {
      const refreshSessionTime = () => touchSession?.(sessionId);
      refreshSessionTime();
      void sendAgentic(text, sessionId, {
        agentMode,
        modelProfileId,
        selectedOntologyId,
        onSessionUpdated: refreshSessionTime,
      });
      if (isNew) void generateSessionTitle(sessionId, text);
      return sessionId;
    };

    if (currentChatId) {
      startRun(currentChatId);
      setInput('');
      return;
    }

    const flight = runFirstSend(async () => {
      const session = await createChat({
        llmRequestMode: 'stream',
        modelProfileId,
        agentMode,
        selectedOntologyId,
      });
      return session ? startRun(session.id, true) : null;
    });
    if (!flight.started) return;

    try {
      const sessionId = await flight.promise;
      if (sessionId) {
        setInput('');
        return;
      }
      scheduleBlankChatFocus({
        getCurrentChatId: () => currentChatIdRef.current,
        getInput: () => inputRef.current,
        scheduleFrame: (callback) => window.requestAnimationFrame(callback),
      });
    } catch {
      setInput(text);
      scheduleBlankChatFocus({
        getCurrentChatId: () => currentChatIdRef.current,
        getInput: () => inputRef.current,
        scheduleFrame: (callback) => window.requestAnimationFrame(callback),
      });
    }
  };
  const streamState = currentChatId ? streamManager.getStreamState(currentChatId) : { isLoading: false };
  const runProgress = resolveRunProgressView(streamState);
  const isSubmitting = isBlankChatSubmitting(currentChatId, firstSendPending);
  const latestTurnStatus = conversationModel.turns.at(-1)?.status || '';
  const latestTurnErrorCode = conversationModel.turns.at(-1)?.errorCode || '';
  const displayedStreamStatus = streamState.status === 'awaiting' && latestTurnStatus === 'cancelled'
    ? 'cancelled'
    : streamState.status;
  const streamStatusLabel = runProgress?.label || ({
    queued: `排队中${streamState.queue?.position ? ` · 第 ${streamState.queue.position} 位` : ''}`,
    reconnecting: '连接恢复中',
    streaming: '执行问答任务中',
    finalizing: '整理最终答案中',
    awaiting: '等待输入',
    cancelled: '已停止',
    cancel_error: streamState.error || '停止失败，运行仍在继续',
    error: streamState.error || '连接异常',
  }[displayedStreamStatus] || '');
  const liveStatusLabel = streamStatusLabel || ({
    finished: '本轮回答已完成',
    cancelled: '本轮运行已停止',
    error: latestTurnErrorCode === 'MODEL_OUTPUT_TRUNCATED' ? '本轮回答未完成' : '本轮运行失败',
    awaiting: '本轮正在等待输入',
  }[latestTurnStatus] || '');
  const resizeTraceByKeyboard = (event) => {
    let nextWidth = traceWidth;
    if (event.key === 'ArrowLeft') nextWidth += 16;
    else if (event.key === 'ArrowRight') nextWidth -= 16;
    else if (event.key === 'Home') nextWidth = TRACE_MIN_W;
    else if (event.key === 'End') nextWidth = traceMaxWidth;
    else return;
    event.preventDefault();
    const clamped = Math.max(TRACE_MIN_W, Math.min(nextWidth, traceMaxWidth));
    setTraceWidth(clamped);
    try { localStorage.setItem('mw_trace_w', String(clamped)); } catch { /* ignore */ }
  };
  const submitInteraction = (displayText, response) => {
    if (!workspaceDataReady || !pendingInteraction?.id || !currentChatId) return;
    const refreshSessionTime = () => touchSession?.(currentChatId);
    refreshSessionTime();
    sendAgentic(String(displayText || response?.value || ''), currentChatId, {
      agentMode,
      modelProfileId,
      selectedOntologyId,
      onSessionUpdated: refreshSessionTime,
      interactionResponse: {
        interaction_id: pendingInteraction.id,
        type: response?.type || pendingInteraction.type || 'input',
        value: response?.value ?? displayText,
        option_id: response?.option_id,
      },
    });
  };
  const cancelInteraction = async () => {
    if (!workspaceDataReady || !pendingInteraction?.id || !currentChatId) return false;
    const interactionId = String(pendingInteraction.id);
    const targetSessionId = currentChatId;
    setInteractionCancelError('');
    try {
      const response = await authFetch(
        `/agentic/sessions/${encodeURIComponent(targetSessionId)}/interactions/${encodeURIComponent(interactionId)}/cancel`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': createIdempotencyKey() },
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(publicErrorMessage(
          payload.error ?? payload.detail ?? payload,
          `取消失败（${response.status}）`,
        ));
      }
      setMessages(
        (previous) => applyInteractionCancellation(previous, {
          ...payload,
          interaction_id: interactionId,
        }),
        targetSessionId,
      );
      touchSession?.(targetSessionId);
      if (currentChatIdRef.current === targetSessionId) {
        window.requestAnimationFrame(() => inputRef.current?.focus());
      }
      return true;
    } catch (error) {
      if (currentChatIdRef.current === targetSessionId) {
        setInteractionCancelError(error?.message || '取消失败，请重试');
      }
      return false;
    }
  };

  return (
    <div className="chatwrap" ref={wrapRef}>
      <span className="sr-only" aria-live="polite" aria-atomic="true">{liveStatusLabel}</span>
      <div className="thread">
        <div className="th-head">
          <span className="tt">{current?.title || '新对话'}</span>
          {current && <span className="tm">{formatTime(current.updated_at || current.created_at)}</span>}
          {streamStatusLabel && <StatusTag status={displayedStreamStatus}>{streamStatusLabel}</StatusTag>}
          <button className="iconbtn" style={{ marginLeft: 'auto' }} type="button" title="会话性能分析"
            aria-label="打开会话性能分析" disabled={!currentChatId}
            onClick={() => onOpenObservability?.('session')}>
            <Gauge size={15} />
          </button>
          <button className="iconbtn" type="button" title={traceToggleLabel} aria-label={traceToggleLabel}
            onClick={() => setTraceOpen((v) => !v)}>
            {traceOpen ? <PanelRightClose size={15} /> : <PanelRightOpen size={15} />}
          </button>
        </div>
        <div className="th-body" ref={bodyRef} onScroll={handleMessageScroll}>
          <ConversationView model={conversationModel} sessionId={currentChatId} runState={streamState} />
          {!messages.length && !streamState.isLoading && (
            <div className="chat-empty">
              <div className="chat-empty-visual" aria-hidden="true">
                <span className="orbit orbit-a" /><span className="orbit orbit-b" />
                <img src="/brand-mark.png" alt="" />
              </div>
              <div className={`chat-empty-kicker ${emptyState.kind}`}>
                <Sparkles size={13} />{emptyState.kicker}
              </div>
              <h2>{emptyState.title}</h2>
              <p>{emptyState.description}</p>
              {emptyState.actionLabel && selectedOntology && (
                <button className="btn chat-empty-data-action" type="button"
                  onClick={() => onConfigureDataSource?.(selectedOntology.id)}>
                  {emptyState.actionLabel}<ArrowUpRight size={14} />
                </button>
              )}
              <div className="chat-starters">
                {STARTER_PROMPTS.map((prompt) => (
                  <button key={prompt} type="button" onClick={() => { setInput(prompt); inputRef.current?.focus(); }}>
                    <span>{prompt}</span><ArrowUpRight size={14} />
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
        {!isFollowingMessages && messages.length > 0 && (
          <button type="button" className="chat-return-bottom" onClick={() => scrollToBottom('smooth')}>
            <ArrowDown size={13} />返回最新消息
          </button>
        )}
        <div className="th-input">
          <div className="chat-config-locks">
            <select value={modelProfileId} title="模型在会话创建后锁定"
              disabled={sessionLocked || isSubmitting || streamState.isLoading || !modelProfiles.length}
              onChange={(event) => {
                setModelProfileId(event.target.value);
                safeLocalSet(`mw_ws_model_${workspaceId}`, event.target.value);
              }}>
              {!modelProfiles.length && <option value="">暂无可用模型</option>}
              {modelProfiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name || profile.id}</option>)}
            </select>
            <select value={selectedOntologyId} title="知识包在会话创建后锁定"
              disabled={sessionLocked || isSubmitting || streamState.isLoading}
              onChange={(event) => setSelectedOntologyId(event.target.value)}>
              <option value="">自动选择知识包</option>
              {ontologies.map((ontology) => <option key={ontology.id} value={ontology.id}>{packageDisplayLabel(ontology)}</option>)}
            </select>
            {displayedOntologyVersion && (
              <span className="session-version-note">
                {sessionLocked ? `Locked version ${displayedOntologyVersion}` : `Current active ${displayedOntologyVersion}`}
              </span>
            )}
            {sessionLocked && <span className="session-lock-note">🔒 会话配置已锁定</span>}
          </div>
          {pendingInteraction ? (
            <InteractionComposer
              interaction={pendingInteraction}
              disabled={streamState.isLoading}
              cancelError={interactionCancelError}
              onSubmit={submitInteraction}
              onCancel={cancelInteraction}
            />
          ) : (
            <div className="ti-box">
              <ChatPromptInput inputRef={inputRef} value={input} isSubmitting={isSubmitting}
                onChange={(e) => setInput(e.target.value)}
                onSend={send} />
              <div className="ti-actions">
                <AgentModePicker value={agentMode} onChange={changeAgentMode} disabled={!workspaceDataReady || sessionLocked || isSubmitting || streamState.isLoading} />
                {streamState.isLoading
                  ? <button type="button" className="btn" onClick={() => streamManager.stopStreamForSession(currentChatId)}>停止</button>
                  : <button type="button" className="btn btn-p" disabled={isSubmitting || !workspaceDataReady || !modelProfileId || !input.trim()} onClick={send}>{isSubmitting ? '发送中' : '发送'}</button>}
              </div>
            </div>
          )}
        </div>
      </div>
      {traceOpen && (
        <>
          <button className="trace-backdrop" type="button" aria-label="关闭运行轨迹"
            onClick={() => setTraceOpen(false)} />
          <div className="trace-resizer" onMouseDown={startResize} onKeyDown={resizeTraceByKeyboard}
            role="separator" tabIndex={0} aria-label="调整运行轨迹宽度" aria-orientation="vertical"
            aria-valuemin={TRACE_MIN_W} aria-valuemax={Math.round(traceMaxWidth)} aria-valuenow={Math.round(traceWidth)}
            title="拖拽或使用方向键调整 Trace 宽度" />
          <TracePanel model={traceModel} sessionId={currentChatId} width={traceWidth} />
        </>
      )}
    </div>
  );
}

function InteractionComposer({ interaction, disabled, cancelError, onSubmit, onCancel }) {
  const [draft, setDraft] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const submittedRef = useRef(false);
  const type = String(interaction?.type || 'input');
  const prompt = String(interaction?.prompt || interaction?.text || '请补充信息后继续。');
  let options = Array.isArray(interaction?.options) ? interaction.options : [];
  if (type === 'approval' && options.length === 0) {
    options = [
      { id: 'approve', label: '批准', value: 'approve' },
      { id: 'reject', label: '拒绝', value: 'reject' },
    ];
  }
  const normalizedOptions = options.map((option, index) => (
    typeof option === 'object'
      ? {
          id: option.id || option.option_id || `option-${index}`,
          label: option.label || option.text || option.value || `选项 ${index + 1}`,
          description: option.description || '',
          value: option.value ?? option.id ?? option.label,
        }
      : { id: `option-${index}`, label: String(option), description: '', value: option }
  ));
  const isInput = type === 'input' || normalizedOptions.length === 0;
  const interactionDisabled = disabled || submitted;

  const beginSubmission = () => {
    if (disabled || submittedRef.current) return false;
    submittedRef.current = true;
    setSubmitted(true);
    return true;
  };

  const requestCancel = useCallback(async () => {
    if (disabled || submittedRef.current) return;
    submittedRef.current = true;
    setSubmitted(true);
    const cancelled = await onCancel?.();
    if (!cancelled) {
      submittedRef.current = false;
      setSubmitted(false);
    }
  }, [disabled, onCancel]);

  useEffect(() => {
    submittedRef.current = false;
    if (disabled) return undefined;
    const resetTimer = window.setTimeout(() => setSubmitted(false), 0);
    return () => window.clearTimeout(resetTimer);
  }, [disabled, interaction?.id]);

  useEffect(() => {
    if (disabled) return undefined;
    const cancelOnEscape = (event) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      event.preventDefault();
      void requestCancel();
    };
    document.addEventListener('keydown', cancelOnEscape);
    return () => document.removeEventListener('keydown', cancelOnEscape);
  }, [disabled, requestCancel]);

  return (
    <div className="mwr-interaction" role="region" aria-label="等待用户交互">
      <div className="mwr-interaction-head">
        <StatusTag status="awaiting">等待{type === 'approval' ? '确认' : type === 'single_choice' ? '选择' : '输入'}</StatusTag>
        <strong>{prompt}</strong>
      </div>
      {interaction?.executed_summary && <p><b>已执行：</b>{interaction.executed_summary}</p>}
      {interaction?.next_plan && <p><b>下一步：</b>{interaction.next_plan}</p>}
      {isInput ? (
        <form onSubmit={(event) => {
          event.preventDefault();
          const value = draft.trim();
          if (!value || !beginSubmission()) return;
          onSubmit(value, { type, value });
          setDraft('');
        }}>
          <input value={draft} disabled={interactionDisabled} autoFocus placeholder="补充信息…"
            onChange={(event) => setDraft(event.target.value)} />
          <button type="submit" className="btn btn-p" disabled={interactionDisabled || !draft.trim()}>继续</button>
        </form>
      ) : (
        <div className="mwr-interaction-options">
          {normalizedOptions.map((option) => (
            <button key={option.id} type="button" className="btn" disabled={interactionDisabled}
              onClick={() => {
                if (!beginSubmission()) return;
                onSubmit(option.label, {
                  type,
                  value: option.value,
                  option_id: option.id,
                });
              }}>
              <strong>{option.label}</strong>
              {option.description && <span>{option.description}</span>}
            </button>
          ))}
        </div>
      )}
      <div className="mwr-interaction-actions">
        <button type="button" className="btn" disabled={interactionDisabled}
          aria-label="取消本次交互（Esc）" onClick={() => void requestCancel()}>
          取消（Esc）
        </button>
      </div>
      {cancelError && <p className="mwr-interaction-error" role="alert">{cancelError}</p>}
    </div>
  );
}
