/**
 * Hook for managing chat sessions (list, create, delete, select).
 *
 * Integrates with StreamManager for multi-session support:
 * - Messages are stored in StreamManager's cache
 * - Session switching does not interrupt background streams
 */

import { useState, useCallback, useEffect, useRef } from 'react';
import createLogger from '../utils/logger';
import { authFetch, getCurrentWorkspaceId } from '../utils/auth';
import { buildMessagesFromEvents, historyToMessages } from '../utils/messageBuilder';
import {
  beginSessionPinUpdate,
  commitSessionPinUpdate,
  rollbackSessionPinUpdate,
  sortSessions,
} from '../utils/sessionModel';

const logger = createLogger('ChatSessions');

const selectedSessionKey = () => `onto_agent_current_session_${getCurrentWorkspaceId() || 'default'}`;

const readSelectedSession = () => {
  try { return window.localStorage.getItem(selectedSessionKey()) || ''; } catch { return ''; }
};

const persistSelectedSession = (sessionId) => {
  try {
    if (sessionId) window.localStorage.setItem(selectedSessionKey(), sessionId);
    else window.localStorage.removeItem(selectedSessionKey());
  } catch {
    // Browser storage can be unavailable in private/restricted contexts.
  }
};

const useChatSessions = (streamManager) => {
  const [sessions, setSessionsState] = useState([]);
  const sessionsRef = useRef([]);
  const pinTransactionsRef = useRef(new Map());
  const setSessions = useCallback((updater) => {
    setSessionsState((previous) => {
      const next = typeof updater === 'function' ? updater(previous) : updater;
      sessionsRef.current = next;
      return next;
    });
  }, []);
  // currentSession holds both id and messages atomically to avoid blank-flash on switch
  const [currentSession, setCurrentSession] = useState({ id: null, messages: [] });
  const currentChatId = currentSession.id;
  const messages = currentSession.messages;
  const [loadingSessions, setLoadingSessions] = useState(false);
  const currentChatIdRef = useRef(currentChatId);

  // Keep streamManager in a ref to avoid dependency issues
  const streamManagerRef = useRef(streamManager);
  useEffect(() => {
    streamManagerRef.current = streamManager;
  }, [streamManager]);

  useEffect(() => {
    currentChatIdRef.current = currentChatId;
  }, [currentChatId]);

  // When streaming updates messagesCache for the current session, sync to local state
  const messagesCacheState = streamManager.messagesCache;
  useEffect(() => {
    const sm = streamManagerRef.current;
    if (sm && currentChatId) {
      const cached = sm.getMessagesForSession(currentChatId);
      setCurrentSession((prev) => (prev.id === currentChatId ? { id: currentChatId, messages: cached } : prev));
    }
  }, [currentChatId, messagesCacheState]);

  // setMessages: update streamManager cache (streaming path)
  const setMessages = useCallback((updater, sessionId = null) => {
    const sm = streamManagerRef.current;
    const targetSessionId = sessionId || currentChatId;
    if (sm && targetSessionId) {
      sm.setMessagesForSession(targetSessionId, updater);
    }
  }, [currentChatId]);

  const openChat = useCallback(async (id) => {
    logger.info(`Opening chat: ${id}`);
    try {
      const res = await authFetch(`/agentic/sessions/${id}`);
      logger.api('GET', `/agentic/sessions/${id}`, res.status);

      if (!res.ok) throw new Error('Failed to load chat');
      const data = await res.json();

      const eventMsgs = Array.isArray(data.events) && data.events.length > 0
        ? buildMessagesFromEvents(data.events)
        : [];
      const rawHistory = Array.isArray(data.messages)
        ? data.messages
        : (Array.isArray(data.history) ? data.history : []);
      const historyMsgs = eventMsgs.length > 0 ? [] : historyToMessages(rawHistory);

      logger.debug(`Loaded chat ${id}`, { history: historyMsgs.length, events: eventMsgs.length });

      const newMessages = [...historyMsgs, ...eventMsgs];
      const sm = streamManagerRef.current;
      if (sm) {
        sm.setMessagesForSession(id, newMessages);
      }

      // Set id and messages atomically — no blank flash
      currentChatIdRef.current = data.id;
      setCurrentSession({ id: data.id, messages: newMessages });
      persistSelectedSession(data.id);
      setSessions((prev) => {
        const { events: _events, messages: _messages, history: _history, ...sessionSummary } = data;
        const exists = prev.some((session) => session.id === data.id);
        const next = exists
          ? prev.map((session) => (session.id === data.id ? { ...session, ...sessionSummary } : session))
          : [sessionSummary, ...prev];
        return sortSessions(next);
      });

      return data;
    } catch (err) {
      logger.error('Failed to open chat', { id, error: err.message });
      return null;
    }
  }, [setSessions]);

  const createChat = useCallback(async (options = {}) => {
    logger.info('Creating new chat');
    try {
      const llmRequestMode = typeof options?.llmRequestMode === 'string'
        ? options.llmRequestMode
        : undefined;
      const modelProfileId = typeof options?.modelProfileId === 'string'
        ? options.modelProfileId
        : undefined;
      const agentMode = typeof options?.agentMode === 'string'
        ? options.agentMode
        : undefined;
      const res = await authFetch('/agentic/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...(llmRequestMode ? { llm_request_mode: llmRequestMode } : {}),
          ...(modelProfileId ? { model_profile_id: modelProfileId } : {}),
          ...(agentMode ? { agent_mode: agentMode } : {}),
        }),
      });
      logger.api('POST', '/agentic/sessions', res.status);

      if (!res.ok) throw new Error('Failed to create chat');
      const data = await res.json();

      logger.info(`Created chat: ${data.id}`);
      setSessions((prev) => sortSessions([data, ...(prev || [])]));
      currentChatIdRef.current = data.id;
      setCurrentSession({ id: data.id, messages: [] });
      persistSelectedSession(data.id);

      const sm = streamManagerRef.current;
      if (sm) {
        sm.setMessagesForSession(data.id, []);
      }

      return data;
    } catch (err) {
      logger.error('Failed to create chat', { error: err.message });
      return null;
    }
  }, [setSessions]);

  const deleteChat = useCallback(async (id, onDeleted) => {
    logger.info(`Deleting chat: ${id}`);

    // Stop stream if running for this session
    const sm = streamManagerRef.current;
    if (sm) {
      sm.stopStreamForSession(id);
      sm.clearCacheForSession(id);
    }

    try {
      const res = await authFetch(`/agentic/sessions/${id}`, { method: 'DELETE' });
      logger.api('DELETE', `/agentic/sessions/${id}`, res.status);

      setSessions((prev) => (prev || []).filter((s) => s.id !== id));
      if (currentChatId === id) {
        currentChatIdRef.current = null;
        setCurrentSession({ id: null, messages: [] });
        persistSelectedSession('');
      }
      if (onDeleted) onDeleted();
    } catch (err) {
      logger.error('Failed to delete chat', { id, error: err.message });
    }
  }, [currentChatId, setSessions]);

  const loadSessions = useCallback(async (selectLatest = false) => {
    logger.info('Loading chat sessions', { selectLatest });
    setLoadingSessions(true);

    try {
      const res = await authFetch('/agentic/sessions');
      logger.api('GET', '/agentic/sessions', res.status);

      if (!res.ok) throw new Error('Failed to load chats');
      const data = await res.json();
      const list = data.sessions || [];
      const sortedList = sortSessions(list);

      logger.debug(`Loaded ${list.length} sessions`);
      setSessions(sortedList);

      let nextId = currentChatIdRef.current;

      if (selectLatest && sortedList.length > 0) {
        nextId = sortedList[0].id;
      } else {
        const persistedId = readSelectedSession();
        if (persistedId && sortedList.some((session) => session.id === persistedId)) {
          nextId = persistedId;
        } else if (!nextId || !sortedList.some((session) => session.id === nextId)) {
          nextId = sortedList[0]?.id || null;
        }
      }

      // If no current session and no sessions exist, show new chat UI (don't create backend record)
      if (!nextId) {
        setCurrentSession({ id: null, messages: [] });
        logger.info('No sessions, showing new chat interface');
      } else {
        // Load the selected session
        await openChat(nextId);
      }
    } catch (err) {
      logger.error('Failed to load sessions', { error: err.message });
    } finally {
      setLoadingSessions(false);
    }
  }, [openChat, setSessions]);

  /**
   * Start a new chat interface without creating backend record.
   * The actual session will be created when user sends the first message.
   */
  const startNewChat = useCallback(() => {
    logger.info('Starting new chat interface');
    currentChatIdRef.current = null;
    setCurrentSession({ id: null, messages: [] });
    persistSelectedSession('');
  }, []);

  const resetSessionsState = useCallback(() => {
    logger.info('Resetting chat session state for workspace switch');
    currentChatIdRef.current = null;
    setSessions([]);
    setCurrentSession({ id: null, messages: [] });
  }, [setSessions]);

  /**
   * Select a chat session.
   * Switching does NOT interrupt background streams.
   */
  const selectChat = useCallback(async (id) => {
    if (!id) return;
    if (id === currentChatId) return;

    logger.info(`Selecting chat: ${id}`);
    currentChatIdRef.current = id;
    persistSelectedSession(id);

    // Check if we already have messages cached for this session
    const sm = streamManagerRef.current;
    const cachedMessages = sm.getMessagesForSession(id);
    if (cachedMessages && cachedMessages.length > 0) {
      // Switch id and messages atomically from cache
      logger.debug(`Using cached messages for session ${id}`, { count: cachedMessages.length });
      setCurrentSession({ id, messages: cachedMessages });
      return;
    }

    // Fetch from server — openChat sets currentSession atomically on completion
    await openChat(id);
  }, [currentChatId, openChat]);

  /**
   * Generate and update session title using backend API.
   * Called asynchronously after user sends first message.
   */
  const generateSessionTitle = useCallback(async (sessionId, message) => {
    if (!sessionId || !message) return;

    logger.info(`Generating title for session: ${sessionId}`);

    try {
      const res = await authFetch(`/agentic/sessions/${sessionId}/generate-title`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
      });

      if (!res.ok) {
        logger.warn(`Failed to generate title: ${res.status}`);
        return;
      }

      const data = await res.json();
      const newTitle = data.title;

      logger.info(`Generated title: ${newTitle}`);

      // Update session title in local state
      setSessions((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, title: newTitle } : s))
      );
    } catch (err) {
      logger.error('Failed to generate session title', { error: err.message });
    }
  }, [setSessions]);

  const setPinned = useCallback(async (sessionId, pinned) => {
    if (!sessionId || typeof pinned !== 'boolean') return false;

    const transactionId = Symbol(sessionId);
    const transaction = beginSessionPinUpdate(sessionsRef.current, sessionId, pinned);
    pinTransactionsRef.current.set(sessionId, { transactionId, transaction });
    setSessions(transaction.optimistic);

    try {
      const res = await authFetch(`/agentic/sessions/${sessionId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pinned }),
      });
      if (!res.ok) throw new Error(`Failed to update session pin (${res.status})`);
      const updated = await res.json();

      if (pinTransactionsRef.current.get(sessionId)?.transactionId === transactionId) {
        setSessions((prev) => commitSessionPinUpdate(prev, sessionId, updated));
        pinTransactionsRef.current.delete(sessionId);
      }
      return true;
    } catch (err) {
      logger.error('Failed to update session pin', { sessionId, error: err.message });
      if (pinTransactionsRef.current.get(sessionId)?.transactionId === transactionId) {
        setSessions((prev) => rollbackSessionPinUpdate(prev, transaction));
        pinTransactionsRef.current.delete(sessionId);
      }
      return false;
    }
  }, [setSessions]);

  return {
    sessions,
    currentChatId,
    messages,
    setMessages,
    loadingSessions,
    loadSessions,
    createChat,
    deleteChat,
    selectChat,
    startNewChat,
    resetSessionsState,
    openChat,
    generateSessionTitle,
    setPinned,
  };
};

export default useChatSessions;
