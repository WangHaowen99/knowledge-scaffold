/**
 * AgenticChat - Main container for agentic workflow chat interface.
 *
 * Integrates:
 * - useAgenticStream hook for workflow handling
 * - ChatMessage for displaying messages
 */

import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { Activity, FolderPlus, Info, Send } from 'lucide-react';
import ChatMessage from './ChatMessage';
import ChatInput from './ChatInput';
import SessionPerformanceAnalysis from './SessionPerformanceAnalysis';
import useAgenticStream from '../features/chat/hooks/useAgenticStream';
import { buildConversationTurns } from '../utils/conversationTurns';
import { buildIndexedSegmentGroups } from '../utils/segmentGroups';
import { parseSlashOntologyCommand } from '../utils/ontologyCommand';
import {
  getLocalObservabilityRuntime,
  getSessionPerformanceEntryState,
  getUnknownLocalObservabilityRuntime,
} from '../utils/observabilityStatus';
import { authFetch } from '../shared/api/auth';

const DRAFT_SESSION_KEY = '__draft__';
const DEFAULT_AGENT_MODE = 'quick';
const DEFAULT_LLM_REQUEST_MODE = 'stream';
// 临时隐藏“流式/非流式”模式面板，仅隐藏 UI，保留请求模式逻辑与切换代码便于快速恢复。
const SHOW_LLM_REQUEST_MODE_UI = false;

function getSessionStateKey(sessionId) {
  return sessionId || DRAFT_SESSION_KEY;
}

function normalizeLlmRequestMode(value) {
  const raw = String(value || '').trim().toLowerCase();
  if (raw === 'sync') return 'sync';
  return DEFAULT_LLM_REQUEST_MODE;
}

function isAwaitingInteractionMessage(message) {
  return Boolean(
    message
    && message.kind === 'agent_step'
    && (message.status === 'awaiting' || message.status === 'awaiting_input')
    && message.interaction
    && typeof message.interaction === 'object'
    && message.interaction.id
  );
}

function findPendingInteraction(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const item = messages[index];
    if (item?.role === 'user') return null;
    if (isAwaitingInteractionMessage(item)) {
      return {
        message: item,
        interaction: item.interaction,
      };
    }
  }
  return null;
}

function normalizeInteractionOption(option, index) {
  if (option && typeof option === 'object' && !Array.isArray(option)) {
    const label = String(option.label || option.description || option.id || `选项 ${index + 1}`).trim();
    return {
      id: String(option.id || '').trim(),
      label,
      value: Object.prototype.hasOwnProperty.call(option, 'value') ? option.value : label,
      description: String(option.description || '').trim(),
      type: String(option.type || '').trim(),
    };
  }
  const label = String(option ?? '').trim() || `选项 ${index + 1}`;
  return {
    id: '',
    label,
    value: label,
    description: '',
    type: '',
  };
}

function InteractionComposer({ interaction, isLoading, disabled, onSubmit }) {
  const [draft, setDraft] = useState('');
  const interactionType = String(interaction?.type || 'input').trim();
  const prompt = String(interaction?.prompt || interaction?.text || '请补充信息后继续。').trim();
  const options = Array.isArray(interaction?.options)
    ? interaction.options.map((option, index) => normalizeInteractionOption(option, index))
    : [];
  const isInput = interactionType === 'input' || options.length === 0;
  const canSubmitDraft = draft.trim().length > 0 && !isLoading && !disabled;
  const typeLabel = interactionType === 'single_choice'
    ? '需要选择'
    : (interactionType === 'approval' ? '等待确认' : '需要补充信息');

  const submitDraft = (event) => {
    event.preventDefault();
    const value = draft.trim();
    if (!value || isLoading || disabled) return;
    onSubmit(value, {
      type: interactionType,
      value,
    });
    setDraft('');
  };

  const submitOption = (option) => {
    if (isLoading || disabled) return;
    onSubmit(option.label, {
      type: interactionType || option.type || 'single_choice',
      value: option.value,
      option_id: option.id || undefined,
    });
  };

  return (
    <div className="input-stack">
      <div className="interaction-composer" role="region" aria-label="等待用户交互">
        <div className="interaction-composer-header">
          <span className="interaction-composer-icon">
            <Info size={16} />
          </span>
          <span className="interaction-composer-type">{typeLabel}</span>
        </div>
        <div className="interaction-composer-prompt">{prompt}</div>
        {interaction?.executed_summary && (
          <div className="interaction-composer-section">
            <span className="interaction-composer-section-title">已执行</span>
            <span>{interaction.executed_summary}</span>
          </div>
        )}
        {interaction?.next_plan && (
          <div className="interaction-composer-section">
            <span className="interaction-composer-section-title">下一步</span>
            <span>{interaction.next_plan}</span>
          </div>
        )}
        {isInput ? (
          <form className="interaction-composer-form" onSubmit={submitDraft}>
            <textarea
              className="interaction-composer-input"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="输入补充信息..."
              disabled={isLoading || disabled}
              rows={2}
            />
            <button
              className="interaction-composer-submit"
              type="submit"
              disabled={!canSubmitDraft}
              title="提交"
            >
              <Send size={20} />
            </button>
          </form>
        ) : (
          <div className="interaction-composer-options">
            {options.map((option, index) => (
              <button
                key={option.id || `${option.label}-${index}`}
                type="button"
                className="interaction-composer-option"
                onClick={() => submitOption(option)}
                disabled={isLoading || disabled}
                title={option.description || undefined}
              >
                <span className="interaction-composer-option-label">{option.label}</span>
                {option.description && (
                  <span className="interaction-composer-option-desc">{option.description}</span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function AgenticChat({
  sessionId,
  sessionMetadata = null,
  sessionMessageCount = 0,
  messages,
  setMessages,
  input,
  setInput,
  streamManager,
  isLoading: isLoadingProp = false,
  onStop = null,
  onGenerateTitle = null,
  onCreateChat = null,
  activeReasoningSegment = null,
  onSegmentClick = null,
  ontologyOptions = [],
  currentWorkspaceId = '',
  onNavigateToWorkspace,
  onOpenSettings = null,
  onOpenOntologyConfig = null,
  onPerformanceViewChange = null,
}) {
  const isLoading = isLoadingProp;
  const stop = onStop;
  const [llmRequestModeBySession, setLlmRequestModeBySession] = useState({});
  const [agentModeBySession, setAgentModeBySession] = useState({});
  const [modelProfileBySession, setModelProfileBySession] = useState({});
  const [modelProfiles, setModelProfiles] = useState([]);
  const [defaultModelProfileId, setDefaultModelProfileId] = useState('');
  const [hasLoadedModelProfiles, setHasLoadedModelProfiles] = useState(false);
  const [_runtimeReadiness, setRuntimeReadiness] = useState(null);
  const [hasLoadedRuntimeReadiness, setHasLoadedRuntimeReadiness] = useState(false);
  const [dismissedLlmConfigPrompts, setDismissedLlmConfigPrompts] = useState({});
  const [dismissedOntologyConfigPrompts, setDismissedOntologyConfigPrompts] = useState({});
  const [lockedOntologyBySession, setLockedOntologyBySession] = useState({});
  const [performanceSessionId, setPerformanceSessionId] = useState(null);
  const [localObservabilityRuntime, setLocalObservabilityRuntime] = useState(getUnknownLocalObservabilityRuntime);
  const [sessionPerformanceAvailability, setSessionPerformanceAvailability] = useState({
    loading: false,
    loaded: false,
    runCount: 0,
  });
  const sessionStateKey = getSessionStateKey(sessionId);
  const enabledOntologyOptions = useMemo(
    () => ontologyOptions.filter((ontology) => ontology.enabled !== false),
    [ontologyOptions]
  );
  const lockedOntology = lockedOntologyBySession[sessionStateKey] || null;
  const agentMode = agentModeBySession[sessionStateKey] || DEFAULT_AGENT_MODE;
  const availableModelProfileIds = useMemo(
    () => new Set(modelProfiles.map((profile) => profile.id).filter(Boolean)),
    [modelProfiles]
  );
  const sessionModelProfileId = String(sessionMetadata?.model_profile_id || '').trim();
  const selectedModelProfileId = modelProfileBySession[sessionStateKey]
    || (availableModelProfileIds.size === 0 || availableModelProfileIds.has(sessionModelProfileId) ? sessionModelProfileId : '')
    || defaultModelProfileId
    || modelProfiles[0]?.id
    || '';
  const hasConfiguredModelProfiles = modelProfiles.length > 0;
  const hasLoadedRequiredConfig = hasLoadedModelProfiles && hasLoadedRuntimeReadiness;
  const lockedOntologyDetails = lockedOntology?.id
    ? ontologyOptions.find((ontology) => ontology.id === lockedOntology.id) || lockedOntology
    : null;
  const lockedOntologyDisabled = Boolean(lockedOntologyDetails?.id && lockedOntologyDetails?.enabled === false);
  const lockedOntologyNeedsBinding = Boolean(
    lockedOntologyDetails?.id
    && !lockedOntologyDisabled
    && !lockedOntologyDetails?.business_data?.ready
  );
  const isConfigBlockingInput = !hasLoadedRequiredConfig || !hasConfiguredModelProfiles || lockedOntologyNeedsBinding;
  const llmConfigPromptKey = currentWorkspaceId || '__no_workspace__';
  const showLlmConfigPrompt = Boolean(
    currentWorkspaceId
    && hasLoadedModelProfiles
    && !hasConfiguredModelProfiles
    && !dismissedLlmConfigPrompts[llmConfigPromptKey]
  );
  const showOntologyConfigPrompt = Boolean(
    currentWorkspaceId
    && hasLoadedModelProfiles
    && hasConfiguredModelProfiles
    && hasLoadedRuntimeReadiness
    && lockedOntologyNeedsBinding
    && !dismissedOntologyConfigPrompts[llmConfigPromptKey]
  );

  // Agentic stream handler
  const {
    sendAgentic,
  } = useAgenticStream({
    sessionId,
    setMessages,
    streamManager,
  });

  // Auto-scroll state
  const [autoScroll, setAutoScroll] = useState(true);
  const userScrolledRef = useRef(false);
  const messagesEndRef = useRef(null);
  const messagesContainerRef = useRef(null);
  const shouldAutoScroll = autoScroll;

  useEffect(() => {
    let cancelled = false;
    const loadModelProfiles = async () => {
      try {
        const response = await authFetch('/agentic/model-profiles');
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          if (!cancelled) {
            setModelProfiles([]);
            setDefaultModelProfileId('');
            setHasLoadedModelProfiles(true);
          }
          return;
        }
        const profiles = Array.isArray(data?.profiles) ? data.profiles : [];
        if (cancelled) return;
        setModelProfiles(profiles);
        setDefaultModelProfileId(typeof data?.default_profile_id === 'string' ? data.default_profile_id : '');
        setHasLoadedModelProfiles(true);
      } catch {
        if (!cancelled) {
          setModelProfiles([]);
          setDefaultModelProfileId('');
          setHasLoadedModelProfiles(true);
        }
      }
    };
    loadModelProfiles();
    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId]);

  useEffect(() => {
    let cancelled = false;
    const loadRuntimeReadiness = async () => {
      try {
        const response = await authFetch('/api/system-config/readiness');
        const data = await response.json().catch(() => ({}));
        if (!cancelled) {
          setRuntimeReadiness(response.ok ? data : null);
          setHasLoadedRuntimeReadiness(true);
        }
      } catch {
        if (!cancelled) {
          setRuntimeReadiness(null);
          setHasLoadedRuntimeReadiness(true);
        }
      }
    };
    loadRuntimeReadiness();
    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId]);

  useEffect(() => {
    let cancelled = false;
    const loadObservabilityConfig = async () => {
      try {
        const response = await authFetch('/api/system-config');
        const data = await response.json().catch(() => ({}));
        if (!cancelled) {
          setLocalObservabilityRuntime(response.ok
            ? getLocalObservabilityRuntime(data)
            : getUnknownLocalObservabilityRuntime());
        }
      } catch {
        if (!cancelled) {
          setLocalObservabilityRuntime(getUnknownLocalObservabilityRuntime());
        }
      }
    };
    loadObservabilityConfig();
    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId]);

  useEffect(() => {
    if (!sessionId) {
      setSessionPerformanceAvailability({ loading: false, loaded: false, runCount: 0 });
      return undefined;
    }

    const controller = new AbortController();
    const loadSessionPerformanceAvailability = async () => {
      setSessionPerformanceAvailability((current) => ({
        ...current,
        loading: true,
      }));
      try {
        const response = await authFetch(
          `/agentic/sessions/${encodeURIComponent(sessionId)}/performance/availability`,
          { signal: controller.signal },
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data?.error || data?.detail || '加载会话性能摘要失败');
        }
        if (!controller.signal.aborted) {
          setSessionPerformanceAvailability({
            loading: false,
            loaded: true,
            runCount: data?.available ? 1 : 0,
          });
        }
      } catch (error) {
        if (error?.name !== 'AbortError') {
          setSessionPerformanceAvailability({
            loading: false,
            loaded: true,
            runCount: 0,
          });
        }
      }
    };

    loadSessionPerformanceAvailability();
    return () => controller.abort();
  }, [isLoading, sessionId, sessionMessageCount]);

  // Auto-scroll effect
  useEffect(() => {
    if (autoScroll && !userScrolledRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, autoScroll]);

  useEffect(() => {
    if (!activeReasoningSegment) return undefined;
    const { turnIndex, globalIndex } = activeReasoningSegment;
    if (turnIndex == null || globalIndex == null) return undefined;

    let retryTimer = null;
    let rafId = null;
    let cancelled = false;
    const maxRetries = 8;

    const tryScrollTarget = (attempt = 0) => {
      if (cancelled) return;
      const container = messagesContainerRef.current;
      const selector = `[data-turn-index="${turnIndex}"][data-output-group-index="${globalIndex}"]`;
      const block = container?.querySelector(selector);
      if (block) {
        block.scrollIntoView({ behavior: 'smooth', block: 'center' });
        userScrolledRef.current = true;
        setAutoScroll(false);
        return;
      }
      if (attempt < maxRetries) {
        retryTimer = window.setTimeout(() => tryScrollTarget(attempt + 1), 40);
      }
    };

    rafId = window.requestAnimationFrame(() => tryScrollTarget(0));

    return () => {
      cancelled = true;
      if (rafId) window.cancelAnimationFrame(rafId);
      if (retryTimer) window.clearTimeout(retryTimer);
    };
  }, [activeReasoningSegment]);

  // Handle scroll to detect if user scrolled up
  const handleMessagesScroll = useCallback(() => {
    const container = messagesContainerRef.current;
    if (!container) return;
    const { scrollTop, scrollHeight, clientHeight } = container;
    const isNearBottom = scrollHeight - scrollTop - clientHeight < 10;

    if (!isNearBottom) {
      userScrolledRef.current = true;
      setAutoScroll(false);
    } else {
      userScrolledRef.current = false;
      setAutoScroll(true);
    }
  }, []);

  let latestTurnStatus = '';
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (
      msg?.role === 'assistant'
      && msg?.kind === 'agent_step'
      && msg?.stepLabel === 'turn_stream'
    ) {
      latestTurnStatus = String(msg.status || '').toLowerCase();
      break;
    }
  }
  const lastMessageRole = messages[messages.length - 1]?.role || '';
  const hasPendingTurn = (
    lastMessageRole === 'user'
    || isLoading
    || latestTurnStatus === 'running'
    || latestTurnStatus === 'streaming'
  );
  const persistedLlmRequestMode = normalizeLlmRequestMode(sessionMetadata?.llm_request_mode);
  const draftLlmRequestMode = normalizeLlmRequestMode(llmRequestModeBySession[sessionStateKey]);
  const effectiveLlmRequestMode = sessionId
    ? normalizeLlmRequestMode(sessionMetadata?.llm_request_mode || llmRequestModeBySession[sessionStateKey])
    : draftLlmRequestMode;
  const llmRequestModeLocked = messages.length > 0 || sessionMessageCount > 0 || hasPendingTurn;
  const llmRequestModeHint = llmRequestModeLocked ? '会话开始后不可切换' : '新对话可切换';
  const llmRequestModeLabel = effectiveLlmRequestMode === 'sync' ? '非流式' : '流式';
  const llmRequestModeDescription = effectiveLlmRequestMode === 'sync'
    ? '等待完整结果后再返回'
    : '边接收边解析增量结果';

  const setLockedOntologyValue = useCallback((targetSessionKey, ontology) => {
    setLockedOntologyBySession((prev) => ({
      ...prev,
      [targetSessionKey]: ontology?.id
        ? {
          id: ontology.id,
          name: ontology.name || ontology.id,
        }
        : null,
    }));
  }, []);

  const migrateLockedOntologyState = useCallback((fromSessionKey, toSessionKey) => {
    if (!fromSessionKey || !toSessionKey || fromSessionKey === toSessionKey) {
      return;
    }

    setLockedOntologyBySession((prev) => {
      const next = { ...prev };
      const nextValue = next[fromSessionKey];
      if (nextValue) {
        next[toSessionKey] = nextValue;
        if (fromSessionKey === DRAFT_SESSION_KEY) {
          delete next[fromSessionKey];
        }
      }
      return next;
    });
  }, []);

  const migrateLlmRequestModeState = useCallback((fromSessionKey, toSessionKey) => {
    if (!fromSessionKey || !toSessionKey || fromSessionKey === toSessionKey) {
      return;
    }

    setLlmRequestModeBySession((prev) => {
      const next = { ...prev };
      const nextValue = normalizeLlmRequestMode(next[fromSessionKey]);
      next[toSessionKey] = nextValue;
      if (fromSessionKey === DRAFT_SESSION_KEY) {
        delete next[fromSessionKey];
      }
      return next;
    });
  }, []);

  const migrateAgentModeState = useCallback((fromSessionKey, toSessionKey) => {
    if (!fromSessionKey || !toSessionKey || fromSessionKey === toSessionKey) {
      return;
    }

    setAgentModeBySession((prev) => {
      const next = { ...prev };
      const nextValue = next[fromSessionKey];
      if (nextValue) {
        next[toSessionKey] = nextValue;
        if (fromSessionKey === DRAFT_SESSION_KEY) {
          delete next[fromSessionKey];
        }
      }
      return next;
    });
  }, []);

  const migrateModelProfileState = useCallback((fromSessionKey, toSessionKey) => {
    if (!fromSessionKey || !toSessionKey || fromSessionKey === toSessionKey) {
      return;
    }

    setModelProfileBySession((prev) => {
      const next = { ...prev };
      const nextValue = next[fromSessionKey];
      if (nextValue) {
        next[toSessionKey] = nextValue;
        if (fromSessionKey === DRAFT_SESSION_KEY) {
          delete next[fromSessionKey];
        }
      }
      return next;
    });
  }, []);

  const toggleLlmRequestMode = useCallback(() => {
    if (llmRequestModeLocked) return;
    setLlmRequestModeBySession((prev) => ({
      ...prev,
      [sessionStateKey]: effectiveLlmRequestMode === 'stream' ? 'sync' : 'stream',
    }));
  }, [effectiveLlmRequestMode, llmRequestModeLocked, sessionStateKey]);

  const changeAgentMode = useCallback((mode) => {
    setAgentModeBySession((prev) => ({
      ...prev,
      [sessionStateKey]: mode,
    }));
  }, [sessionStateKey]);

  const changeModelProfile = useCallback((profileId) => {
    setModelProfileBySession((prev) => ({
      ...prev,
      [sessionStateKey]: profileId,
    }));
  }, [sessionStateKey]);

  // Send handler
  const handleSend = useCallback(
    async (overrideMessage, sendOverrides = null) => {
      const rawMessage = overrideMessage || input;
      const parsedSlashCommand = parseSlashOntologyCommand(rawMessage, enabledOntologyOptions);
      const explicitOntology = sendOverrides?.selectedOntology || parsedSlashCommand.selectedOntology;
      const messageToSend = (parsedSlashCommand.cleanMessage || '').trim();

      if (!messageToSend || (isLoading && !overrideMessage)) return;

      const activeSessionIds = streamManager?.activeSessionIds || [];
      const hasReachedRunLimit = streamManager
        && activeSessionIds.length >= 2
        && (!sessionId || !activeSessionIds.includes(sessionId));
      if (hasReachedRunLimit) {
        window.alert('当前最多只能同时运行两个会话，请先结束其他会话或等待完成。');
        return;
      }

      if (parsedSlashCommand.isSlashMode && !explicitOntology) {
        return;
      }

      if (!hasConfiguredModelProfiles) {
        setDismissedLlmConfigPrompts((prev) => ({ ...prev, [llmConfigPromptKey]: false }));
        return;
      }

      const sourceSessionKey = getSessionStateKey(sessionId);
      const resolvedSelectedOntology = explicitOntology || lockedOntologyBySession[sourceSessionKey] || null;
      const pendingInteraction = sendOverrides?.interactionResponse
        ? null
        : findPendingInteraction(messages)?.interaction;
      const interactionResponse = sendOverrides?.interactionResponse || (
        pendingInteraction
          ? {
            interaction_id: pendingInteraction.id,
            type: pendingInteraction.type,
            value: messageToSend,
          }
          : null
      );
      const resolvedOntologyDetails = resolvedSelectedOntology?.id
        ? ontologyOptions.find((ontology) => ontology.id === resolvedSelectedOntology.id) || resolvedSelectedOntology
        : null;
      if (resolvedOntologyDetails?.id && resolvedOntologyDetails?.enabled === false) {
        setLockedOntologyValue(sourceSessionKey, null);
        return;
      }
      if (resolvedOntologyDetails?.id && !resolvedOntologyDetails?.business_data?.ready) {
        setDismissedOntologyConfigPrompts((prev) => ({ ...prev, [llmConfigPromptKey]: false }));
        return;
      }

      if (!overrideMessage) {
        setInput('');
      }

      userScrolledRef.current = false;
      setAutoScroll(true);

      const llmRequestModeForSession = sessionId
        ? persistedLlmRequestMode
        : effectiveLlmRequestMode;

      // If no session exists, create one first
      let activeSessionId = sessionId;
      let activeSessionKey = sourceSessionKey;
      if (!activeSessionId && onCreateChat) {
        const newSession = await onCreateChat({
          llmRequestMode: llmRequestModeForSession,
          modelProfileId: selectedModelProfileId,
        });
        if (!newSession?.id) return;
        activeSessionId = newSession.id;
        activeSessionKey = getSessionStateKey(activeSessionId);
        migrateLlmRequestModeState(sourceSessionKey, activeSessionKey);
        migrateAgentModeState(sourceSessionKey, activeSessionKey);
        migrateModelProfileState(sourceSessionKey, activeSessionKey);
        migrateLockedOntologyState(sourceSessionKey, activeSessionKey);
      }

      // Generate title if new
      const isFirstMessage = messages.length === 0;
      if (isFirstMessage && onGenerateTitle && activeSessionId) {
        onGenerateTitle(activeSessionId, messageToSend);
      }

      if (explicitOntology?.id) {
        setLockedOntologyValue(activeSessionKey, explicitOntology);
      }

      sendAgentic(messageToSend, activeSessionId, {
        selectedOntologyId: resolvedSelectedOntology?.id || '',
        agentMode,
        modelProfileId: selectedModelProfileId,
        interactionResponse,
      });
    }, [
    effectiveLlmRequestMode,
    input,
    isLoading,
    lockedOntologyBySession,
    messages,
    migrateLlmRequestModeState,
    migrateAgentModeState,
    migrateModelProfileState,
    migrateLockedOntologyState,
    onCreateChat,
    onGenerateTitle,
    enabledOntologyOptions,
    ontologyOptions,
    persistedLlmRequestMode,
    sendAgentic,
    sessionId,
    setInput,
    setLockedOntologyValue,
    streamManager,
    agentMode,
    selectedModelProfileId,
    hasConfiguredModelProfiles,
    llmConfigPromptKey,
  ]);

  // Compute segment index offsets and turn indices per message
  const { segmentOffsets, turnIndexByMsgId } = useMemo(() => {
    const offsets = new Map();
    const turnIndices = new Map();
    const turns = buildConversationTurns(messages);
    turns.forEach((turn) => {
      let offset = 0;
      turn.agentMsgs.forEach((agentMsg) => {
        offsets.set(agentMsg.id, offset);
        turnIndices.set(agentMsg.id, turn.index);
        const { completedGroups } = buildIndexedSegmentGroups(agentMsg.streamSegments || []);
        offset += completedGroups.length;
      });
    });
    return { segmentOffsets: offsets, turnIndexByMsgId: turnIndices };
  }, [messages]);

  const pendingInteractionEntry = useMemo(() => findPendingInteraction(messages), [messages]);
  const pendingInteraction = pendingInteractionEntry?.interaction || null;

  // Handler for clicking options in chat bubbles
  const handleOptionClick = useCallback((optionText, meta = {}) => {
    if (meta?.action === 'fill') {
      setInput(String(optionText ?? ''));
      return;
    }

    const interaction = meta?.interaction;
    if (interaction?.id) {
      const option = meta?.option || {};
      const displayText = String(option.label ?? optionText ?? '').trim();
      handleSend(displayText, {
        interactionResponse: {
          interaction_id: interaction.id,
          type: interaction.type || option.type || 'single_choice',
          value: option.value ?? optionText,
          option_id: meta.optionId || option.id || undefined,
        },
      });
      return;
    }

    handleSend(String(optionText ?? ''));
  }, [handleSend, setInput]);

  const handleInteractionSubmit = useCallback((displayText, response) => {
    if (!pendingInteraction?.id) return;
    handleSend(String(displayText ?? response?.value ?? ''), {
      interactionResponse: {
        interaction_id: pendingInteraction.id,
        type: response?.type || pendingInteraction.type || 'input',
        value: response?.value ?? displayText,
        option_id: response?.option_id,
      },
    });
  }, [handleSend, pendingInteraction]);

  const handleQuickSelectOntology = useCallback((ontology) => {
    if (!ontology?.id) return;
    setLockedOntologyValue(sessionStateKey, ontology);
  }, [sessionStateKey, setLockedOntologyValue]);

  const handleClearLockedOntology = useCallback(() => {
    setLockedOntologyValue(sessionStateKey, null);
  }, [sessionStateKey, setLockedOntologyValue]);

  const isNewChat = !sessionId;
  const isPerformanceViewOpen = !isNewChat && performanceSessionId === sessionId;
  const performanceEntryState = getSessionPerformanceEntryState({
    hasLocalData: sessionPerformanceAvailability.runCount > 0,
    isWritingLocal: localObservabilityRuntime.isWritingLocal,
    configLoaded: localObservabilityRuntime.loaded,
    loading: sessionPerformanceAvailability.loading && !sessionPerformanceAvailability.loaded,
  });
  const lastUserMessageIndex = useMemo(
    () => messages.map((msg) => msg.role).lastIndexOf('user'),
    [messages]
  );

  useEffect(() => {
    if (!onPerformanceViewChange) return undefined;
    onPerformanceViewChange(isPerformanceViewOpen);
    return () => {
      onPerformanceViewChange(false);
    };
  }, [isPerformanceViewOpen, onPerformanceViewChange]);

  const openPerformanceView = useCallback(() => {
    if (!sessionId) return;
    onPerformanceViewChange?.(true);
    setPerformanceSessionId(sessionId);
  }, [onPerformanceViewChange, sessionId]);

  const closePerformanceView = useCallback(() => {
    onPerformanceViewChange?.(false);
    setPerformanceSessionId(null);
  }, [onPerformanceViewChange]);

  const renderDraftModeSelector = () => (
    <div className="new-chat-mode-panel">
      <div className="new-chat-mode-copy">
        <span className="new-chat-mode-label">模型请求模式</span>
        <span className="new-chat-mode-text">
          {llmRequestModeDescription}
          {llmRequestModeHint ? `，${llmRequestModeHint}` : ''}
        </span>
      </div>
      <button
        type="button"
        className={`action-chip request-mode-toggle ${effectiveLlmRequestMode === 'stream' ? 'active' : 'is-sync'}`}
        onClick={toggleLlmRequestMode}
        disabled={llmRequestModeLocked}
      >
        {llmRequestModeLabel}
      </button>
    </div>
  );

  const renderSessionModeInfo = () => (
    <div className={`chat-session-mode-note mode-${effectiveLlmRequestMode}`}>
      <span className="chat-session-mode-label">当前会话模型请求</span>
      <span className="chat-session-mode-value">{llmRequestModeLabel}</span>
      <span className="chat-session-mode-text">{llmRequestModeDescription}</span>
    </div>
  );

  const renderComposer = (modeMenuPlacement) => (
    pendingInteraction ? (
      <InteractionComposer
        interaction={pendingInteraction}
        isLoading={isLoading}
        disabled={!currentWorkspaceId || isConfigBlockingInput}
        onSubmit={handleInteractionSubmit}
      />
    ) : (
      <ChatInput
        input={input}
        setInput={setInput}
        isLoading={isLoading}
        onSend={() => handleSend()}
        onStop={stop}
        agentMode={agentMode}
        onChangeAgentMode={changeAgentMode}
        modelProfiles={modelProfiles}
        selectedModelProfileId={selectedModelProfileId}
        onChangeModelProfile={changeModelProfile}
        modeMenuPlacement={modeMenuPlacement}
        ontologyOptions={enabledOntologyOptions}
        lockedOntology={lockedOntology}
        onSelectOntology={handleQuickSelectOntology}
        onClearOntology={handleClearLockedOntology}
        disabled={!currentWorkspaceId || isConfigBlockingInput}
      />
    )
  );

  if (isPerformanceViewOpen) {
    return (
      <section className="card chat-card agentic-chat performance-chat-card">
        <SessionPerformanceAnalysis
          sessionId={sessionId}
          onBack={closePerformanceView}
        />
      </section>
    );
  }

  return (
    <section className={`card chat-card agentic-chat ${isNewChat ? 'new-chat-centered' : ''}`}>
      {isNewChat ? (
        /* New chat centered layout */
        <div className="new-chat-container">
          <div className="new-chat-content">
            <h2 className="new-chat-title">有什么可以帮您？</h2>
            {!currentWorkspaceId && (
              <div className="new-chat-workspace-hint" onClick={() => onNavigateToWorkspace?.()}>
                <FolderPlus size={16} />
                <span>请先<a className="new-chat-workspace-link">创建工作空间</a>再开始对话</span>
              </div>
            )}
            {currentWorkspaceId && hasLoadedModelProfiles && !hasConfiguredModelProfiles && (
              <div
                className="new-chat-workspace-hint"
                onClick={() => {
                  if (onOpenSettings) onOpenSettings();
                }}
              >
                <FolderPlus size={16} />
                <span>
                  当前未配置模型，请先
                  <a className="new-chat-workspace-link">{onOpenSettings ? '前往配置中心' : ''}</a>
                  {onOpenSettings ? '完成配置' : '联系管理员完成配置'}
                </span>
              </div>
            )}
            {currentWorkspaceId && hasLoadedModelProfiles && hasConfiguredModelProfiles && hasLoadedRuntimeReadiness && lockedOntologyNeedsBinding && (
              <div
                className="new-chat-workspace-hint"
                onClick={() => {
                  if (onOpenOntologyConfig) {
                    onOpenOntologyConfig();
                  } else if (onNavigateToWorkspace) {
                    onNavigateToWorkspace();
                  }
                }}
              >
                <FolderPlus size={16} />
                <span>
                  当前本体未绑定业务数据源，请先
                  <a className="new-chat-workspace-link">{onOpenOntologyConfig || onNavigateToWorkspace ? '前往本体绑定' : ''}</a>
                  {onOpenOntologyConfig || onNavigateToWorkspace ? '完成绑定' : '联系管理员完成绑定'}
                </span>
              </div>
            )}
            <div className="chat-refresh-notice">
              提示：刷新页面会断开正在运行的对话连接并终止当前运行状态
            </div>
            {/* 仅隐藏 UI：保留流式模式面板代码，后续将 SHOW_LLM_REQUEST_MODE_UI 改为 true 即可恢复。 */}
            {SHOW_LLM_REQUEST_MODE_UI && renderDraftModeSelector()}
            {renderComposer('bottom')}
          </div>
        </div>
      ) : (
        /* Regular chat layout with messages */
        <>
          <div className="chat-performance-toolbar">
            <button
              className={`chat-performance-entry tone-${performanceEntryState.tone}`}
              onClick={openPerformanceView}
              disabled={performanceEntryState.disabled}
              type="button"
              title={performanceEntryState.title}
            >
              <Activity size={15} />
              <span>{performanceEntryState.label}</span>
            </button>
          </div>
          <div
            className="messages"
            ref={messagesContainerRef}
            onScroll={handleMessagesScroll}
          >
            {messages.map((msg, idx) => (
              <ChatMessage
                key={msg.id || idx}
                msg={msg}
                isLastMessage={idx === messages.length - 1}
                shouldAutoScroll={shouldAutoScroll}
                onOptionClick={handleOptionClick}
                segmentIndexOffset={segmentOffsets.get(msg.id) ?? 0}
                hasUserReplyAfter={msg.role === 'assistant' && lastUserMessageIndex > idx}
                turnIndex={turnIndexByMsgId.get(msg.id) ?? null}
                activeReasoningSegment={activeReasoningSegment}
                onSegmentClick={onSegmentClick}
              />
            ))}
            <div ref={messagesEndRef} />
          </div>

          <div className="chat-refresh-notice">
            提示：刷新页面会断开正在运行的对话连接并终止当前运行状态
          </div>
          {/* 仅隐藏 UI：保留流式模式信息代码，后续将 SHOW_LLM_REQUEST_MODE_UI 改为 true 即可恢复。 */}
          {SHOW_LLM_REQUEST_MODE_UI && renderSessionModeInfo()}
          {renderComposer('top')}
        </>
      )
      }

      {showLlmConfigPrompt && currentWorkspaceId && !hasConfiguredModelProfiles && (
        <div
          className="modal-overlay"
          role="presentation"
          onClick={(event) => {
            if (event.target === event.currentTarget) {
              setDismissedLlmConfigPrompts((prev) => ({ ...prev, [llmConfigPromptKey]: true }));
            }
          }}
        >
          <div className="modal-content settings-delete-modal" role="dialog" aria-modal="true" aria-labelledby="llm-config-prompt-title">
            <div className="modal-header">
              <h2 id="llm-config-prompt-title">请先配置模型</h2>
            </div>
            <div className="modal-body">
              <div className="settings-import-copy">
                <p>当前系统还没有可用的 LLM 模型配置，暂时无法开始对话。</p>
                <p>{onOpenSettings ? '请先前往配置中心完成模型配置。' : '请联系管理员先完成模型配置。'}</p>
              </div>
            </div>
            <div className="modal-footer">
              <button
                className="modal-btn-cancel"
                type="button"
                onClick={() => setDismissedLlmConfigPrompts((prev) => ({ ...prev, [llmConfigPromptKey]: true }))}
              >
                关闭
              </button>
              {onOpenSettings && (
                <button
                  className="modal-btn-confirm"
                  type="button"
                  onClick={() => {
                    setDismissedLlmConfigPrompts((prev) => ({ ...prev, [llmConfigPromptKey]: true }));
                    onOpenSettings();
                  }}
                >
                  去配置模型
                </button>
              )}
            </div>
          </div>
        </div>
      )}
      {showOntologyConfigPrompt && currentWorkspaceId && hasConfiguredModelProfiles && lockedOntologyNeedsBinding && (
        <div
          className="modal-overlay"
          role="presentation"
          onClick={(event) => {
            if (event.target === event.currentTarget) {
              setDismissedOntologyConfigPrompts((prev) => ({ ...prev, [llmConfigPromptKey]: true }));
            }
          }}
        >
          <div className="modal-content settings-delete-modal" role="dialog" aria-modal="true" aria-labelledby="ontology-config-prompt-title">
            <div className="modal-header">
              <h2 id="ontology-config-prompt-title">请先绑定业务数据源</h2>
            </div>
            <div className="modal-body">
              <div className="settings-import-copy">
                <p>当前选中的本体还没有绑定业务数据源，暂时无法查询业务数据。</p>
                <p>{lockedOntologyDetails?.id ? `本体 ${lockedOntologyDetails.id} 需要绑定数据连接。` : '请先在本体配置中完成数据绑定。'}</p>
              </div>
            </div>
            <div className="modal-footer">
              <button
                className="modal-btn-cancel"
                type="button"
                onClick={() => setDismissedOntologyConfigPrompts((prev) => ({ ...prev, [llmConfigPromptKey]: true }))}
              >
                关闭
              </button>
              {(onOpenOntologyConfig || onNavigateToWorkspace) && (
                <button
                  className="modal-btn-confirm"
                  type="button"
                  onClick={() => {
                    setDismissedOntologyConfigPrompts((prev) => ({ ...prev, [llmConfigPromptKey]: true }));
                    if (onOpenOntologyConfig) {
                      onOpenOntologyConfig();
                    } else {
                      onNavigateToWorkspace();
                    }
                  }}
                >
                  去绑定数据源
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </section >
  );
}

export default AgenticChat;
