/**
 * Owns session listing, selection, hydration and durable session metadata.
 * StreamManager remains the owner of active transports and per-session message caches.
 */

import { useState, useCallback, useEffect, useRef } from 'react';
import createLogger from '../../../shared/lib/logger';
import { authFetch, getCurrentWorkspaceId } from '../../../shared/api/auth';
import {
  buildMessagesFromEvents,
  historyToMessages,
} from '../lib/messageBuilder';
import {
  beginSessionPinUpdate,
  commitSessionPinUpdate,
  rollbackSessionPinUpdate,
  sortSessions,
} from '../lib/sessionModel';
import {
  createSessionScope,
  isCurrentSessionRead,
  isSameSessionScope,
  mergeSessionPage,
  mergeSessionCollections,
  normalizeSessionPage,
  resolveInitialSessionSelection,
  selectedSessionStorageKey,
  shouldCommitSessionSelection,
} from '../lib/sessionScope';
import { createFirstSendCoordinator } from '../lib/firstSendCoordinator';

const logger = createLogger('ChatSessions');

const readSelectedSession = (workspaceId) => {
  try {
    return (
      window.localStorage.getItem(selectedSessionStorageKey(workspaceId)) || ''
    );
  } catch {
    return '';
  }
};

const persistSelectedSession = (workspaceId, sessionId) => {
  try {
    const key = selectedSessionStorageKey(workspaceId);
    if (sessionId) {
      window.localStorage.setItem(key, sessionId);
    } else {
      window.localStorage.removeItem(key);
    }
  } catch {
    // Browser storage can be unavailable in private/restricted contexts.
  }
};

const sessionMessages = (data) => {
  const eventMessages =
    Array.isArray(data?.events) && data.events.length > 0 ?
      buildMessagesFromEvents(data.events) :
      [];
  const rawHistory = Array.isArray(data?.messages) ?
    data.messages :
    Array.isArray(data?.history) ?
      data.history :
      [];
  const historyMessages =
    eventMessages.length > 0 ? [] : historyToMessages(rawHistory);
  return [...historyMessages, ...eventMessages];
};

const sessionSummary = (data) => {
  const {
    events: _events,
    messages: _messages,
    history: _history,
    ...summary
  } = data || {};
  return summary;
};

const responseError = async (response, fallback) => {
  const payload = await response.json().catch(() => ({}));
  return new Error(
    payload.error || payload.detail || `${fallback} (${response.status})`,
  );
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

  const [currentSession, setCurrentSessionState] = useState({
    id: null,
    messages: [],
  });
  const currentSessionRef = useRef(currentSession);
  const setCurrentSession = useCallback((updater) => {
    setCurrentSessionState((previous) => {
      const next = typeof updater === 'function' ? updater(previous) : updater;
      currentSessionRef.current = next;
      return next;
    });
  }, []);
  const currentChatId = currentSession.id;
  const { messages } = currentSession;
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [loadingMoreSessions, setLoadingMoreSessions] = useState(false);
  const [hasMoreSessions, setHasMoreSessions] = useState(false);
  const loadingMoreSessionsRef = useRef(false);
  const nextSessionCursorRef = useRef(null);
  const [firstSendPending, setFirstSendPending] = useState(false);
  const [firstSendCoordinator] = useState(() =>
    createFirstSendCoordinator({
      onPendingChange: setFirstSendPending,
    }),
  );

  const streamManagerRef = useRef(streamManager);
  const scopeRef = useRef(createSessionScope(getCurrentWorkspaceId(), 0));
  const selectionEpochRef = useRef(0);
  const requestControllersRef = useRef(new Set());
  const hydratedSessionsRef = useRef(new Set());
  const sessionDataRef = useRef(new Map());
  const collectionMutationEpochRef = useRef(0);
  const sessionMutationEpochRef = useRef(new Map());
  const hydrateSequenceRef = useRef(new Map());
  const hydrateInFlightRef = useRef(new Map());
  const deletedSessionIdsRef = useRef(new Set());

  useEffect(() => {
    streamManagerRef.current = streamManager;
  }, [streamManager]);

  const captureScope = useCallback(() => ({ ...scopeRef.current }), []);
  const isCurrentScope = useCallback(
    (scope) => isSameSessionScope(scope, scopeRef.current),
    [],
  );
  const noteSessionMutation = useCallback((sessionId) => {
    collectionMutationEpochRef.current += 1;
    if (!sessionId) {
      return;
    }
    const current = sessionMutationEpochRef.current.get(sessionId) || 0;
    sessionMutationEpochRef.current.set(sessionId, current + 1);
  }, []);

  const scopedFetch = useCallback(
    async (url, options = {}, scope = captureScope()) => {
      const controller = new AbortController();
      requestControllersRef.current.add(controller);
      try {
        const response = await authFetch(url, {
          ...options,
          signal: controller.signal,
        });
        if (!isCurrentScope(scope)) {
          return null;
        }
        return response;
      } finally {
        requestControllersRef.current.delete(controller);
      }
    },
    [captureScope, isCurrentScope],
  );

  const messagesCacheState = streamManager.messagesCache;
  useEffect(() => {
    const sm = streamManagerRef.current;
    if (!sm || !currentChatId) {
      return;
    }
    const cached = sm.getMessagesForSession(currentChatId);
    setCurrentSession((previous) =>
      previous.id === currentChatId ?
        { id: currentChatId, messages: cached } :
        previous,
    );
  }, [currentChatId, messagesCacheState, setCurrentSession]);

  const setMessages = useCallback((updater, sessionId = null) => {
    const targetSessionId = sessionId || currentSessionRef.current.id;
    if (targetSessionId) {
      streamManagerRef.current?.setMessagesForSession(targetSessionId, updater);
    }
  }, []);

  const hydrateChat = useCallback(
    (id, { refresh = false } = {}) => {
      if (!id || deletedSessionIdsRef.current.has(id)) {
        return Promise.resolve(null);
      }
      const scope = captureScope();
      if (!refresh && hydratedSessionsRef.current.has(id)) {
        return Promise.resolve(sessionDataRef.current.get(id) || null);
      }
      const existing = hydrateInFlightRef.current.get(id);
      const currentMutationEpoch = sessionMutationEpochRef.current.get(id) || 0;
      if (!refresh && existing?.mutationEpoch === currentMutationEpoch) {
        return existing.promise;
      }

      const sequence = (hydrateSequenceRef.current.get(id) || 0) + 1;
      hydrateSequenceRef.current.set(id, sequence);
      const mutationEpoch = currentMutationEpoch;
      const requestIsCurrent = () =>
        isCurrentSessionRead({
          requestScope: scope,
          currentScope: scopeRef.current,
          requestSequence: sequence,
          currentSequence: hydrateSequenceRef.current.get(id),
          requestMutationEpoch: mutationEpoch,
          currentMutationEpoch: sessionMutationEpochRef.current.get(id) || 0,
          deleted: deletedSessionIdsRef.current.has(id),
        });

      logger.info(`Hydrating chat: ${id}`);
      const promise = (async () => {
        try {
          const response = await scopedFetch(
            `/agentic/sessions/${id}`,
            {},
            scope,
          );
          if (!response || !requestIsCurrent()) {
            return null;
          }
          logger.api('GET', `/agentic/sessions/${id}`, response.status);
          if (!response.ok) {
            throw await responseError(response, 'Failed to load chat');
          }
          const data = await response.json();
          if (!requestIsCurrent()) {
            return null;
          }

          const nextMessages = sessionMessages(data);
          const manager = streamManagerRef.current;
          // A newly-started local run can race a background hydration request.
          // Preserve its live cache; the durable snapshot is only authoritative before attachment.
          if (!manager?.isSessionLoading(id)) {
            manager?.setMessagesForSession(id, nextMessages);
          }
          hydratedSessionsRef.current.add(id);
          sessionDataRef.current.set(id, data);
          setSessions((previous) => {
            const summary = sessionSummary(data);
            const exists = previous.some((session) => session.id === data.id);
            return sortSessions(
              exists ?
                previous.map((session) =>
                  session.id === data.id ?
                    { ...session, ...summary } :
                    session,
                ) :
                [summary, ...previous],
            );
          });
          return data;
        } catch (error) {
          if (error?.name !== 'AbortError') {
            logger.error('Failed to hydrate chat', {
              id,
              error: error.message,
            });
          }
          return null;
        } finally {
          if (hydrateInFlightRef.current.get(id)?.sequence === sequence) {
            hydrateInFlightRef.current.delete(id);
          }
        }
      })();
      hydrateInFlightRef.current.set(id, { sequence, mutationEpoch, promise });
      return promise;
    },
    [captureScope, scopedFetch, setSessions],
  );

  const openChat = useCallback(
    async (id) => {
      if (!id) {
        return null;
      }
      const scope = captureScope();
      const selectionEpoch = ++selectionEpochRef.current;
      const previousSession = currentSessionRef.current;
      persistSelectedSession(scope.workspaceId, id);

      if (hydratedSessionsRef.current.has(id)) {
        const cached =
          streamManagerRef.current?.getMessagesForSession(id) || [];
        if (
          shouldCommitSessionSelection({
            requestScope: scope,
            currentScope: scopeRef.current,
            requestSelectionEpoch: selectionEpoch,
            currentSelectionEpoch: selectionEpochRef.current,
          })
        ) {
          setCurrentSession({ id, messages: cached });
        }
        return (
          sessionDataRef.current.get(id) ||
          sessionsRef.current.find((session) => session.id === id) ||
          null
        );
      }

      const data = await hydrateChat(id);
      if (
        data &&
        shouldCommitSessionSelection({
          requestScope: scope,
          currentScope: scopeRef.current,
          requestSelectionEpoch: selectionEpoch,
          currentSelectionEpoch: selectionEpochRef.current,
        })
      ) {
        setCurrentSession({
          id: data.id,
          messages:
            streamManagerRef.current?.getMessagesForSession(data.id) || [],
        });
        return data;
      }

      if (
        !data &&
        shouldCommitSessionSelection({
          requestScope: scope,
          currentScope: scopeRef.current,
          requestSelectionEpoch: selectionEpoch,
          currentSelectionEpoch: selectionEpochRef.current,
        })
      ) {
        persistSelectedSession(scope.workspaceId, previousSession.id);
      }
      return data;
    },
    [captureScope, hydrateChat, setCurrentSession],
  );

  const createChat = useCallback(
    async (options = {}) => {
      const scope = captureScope();
      const selectionEpoch = ++selectionEpochRef.current;
      logger.info('Creating new chat');
      try {
        const llmRequestMode =
          typeof options.llmRequestMode === 'string' ?
            options.llmRequestMode :
            '';
        const modelProfileId =
          typeof options.modelProfileId === 'string' ?
            options.modelProfileId :
            '';
        const agentMode =
          typeof options.agentMode === 'string' ? options.agentMode : '';
        const selectedOntologyId =
          typeof options.selectedOntologyId === 'string' ?
            options.selectedOntologyId.trim() :
            '';
        const response = await scopedFetch(
          '/agentic/sessions',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              ...(llmRequestMode ? { llm_request_mode: llmRequestMode } : {}),
              ...(modelProfileId ? { model_profile_id: modelProfileId } : {}),
              ...(agentMode ? { agent_mode: agentMode } : {}),
              ...(selectedOntologyId ?
                { selected_ontology_id: selectedOntologyId } :
                {}),
            }),
          },
          scope,
        );
        if (!response || !isCurrentScope(scope)) {
          return null;
        }
        logger.api('POST', '/agentic/sessions', response.status);
        if (!response.ok) {
          throw await responseError(response, 'Failed to create chat');
        }
        const data = await response.json();
        if (
          !shouldCommitSessionSelection({
            requestScope: scope,
            currentScope: scopeRef.current,
            requestSelectionEpoch: selectionEpoch,
            currentSelectionEpoch: selectionEpochRef.current,
          })
        ) {
          return null;
        }

        deletedSessionIdsRef.current.delete(data.id);
        noteSessionMutation(data.id);
        setSessions((previous) =>
          sortSessions([
            data,
            ...previous.filter((session) => session.id !== data.id),
          ]),
        );
        hydratedSessionsRef.current.add(data.id);
        sessionDataRef.current.set(data.id, data);
        streamManagerRef.current?.setMessagesForSession(data.id, []);
        setCurrentSession({ id: data.id, messages: [] });
        persistSelectedSession(scope.workspaceId, data.id);
        return data;
      } catch (error) {
        if (error?.name !== 'AbortError') {
          logger.error('Failed to create chat', { error: error.message });
        }
        return null;
      }
    },
    [
      captureScope,
      isCurrentScope,
      noteSessionMutation,
      scopedFetch,
      setCurrentSession,
      setSessions,
    ],
  );

  const runFirstSend = useCallback(
    (operation) => firstSendCoordinator.run(operation),
    [firstSendCoordinator],
  );

  const deleteChat = useCallback(
    async (id, onDeleted) => {
      if (!id) {
        return false;
      }
      const scope = captureScope();
      logger.info(`Deleting chat: ${id}`);

      try {
        const response = await scopedFetch(
          `/agentic/sessions/${id}`,
          { method: 'DELETE' },
          scope,
        );
        if (!response || !isCurrentScope(scope)) {
          return false;
        }
        logger.api('DELETE', `/agentic/sessions/${id}`, response.status);
        if (!response.ok) {
          throw await responseError(response, 'Failed to delete chat');
        }

        deletedSessionIdsRef.current.add(id);
        noteSessionMutation(id);
        streamManagerRef.current?.clearCacheForSession(id);
        hydratedSessionsRef.current.delete(id);
        sessionDataRef.current.delete(id);
        setSessions((previous) =>
          previous.filter((session) => session.id !== id),
        );
        if (currentSessionRef.current.id === id) {
          selectionEpochRef.current += 1;
          setCurrentSession({ id: null, messages: [] });
          persistSelectedSession(scope.workspaceId, '');
        }
        onDeleted?.();
        return true;
      } catch (error) {
        if (error?.name !== 'AbortError') {
          logger.error('Failed to delete chat', { id, error: error.message });
        }
        return false;
      }
    },
    [
      captureScope,
      isCurrentScope,
      noteSessionMutation,
      scopedFetch,
      setCurrentSession,
      setSessions,
    ],
  );

  const loadSessions = useCallback(
    async (selectLatest = false) => {
      const scope = captureScope();
      const selectionEpochAtStart = selectionEpochRef.current;
      const mutationEpochAtStart = collectionMutationEpochRef.current;
      logger.info('Loading chat sessions', {
        selectLatest,
        workspaceId: scope.workspaceId,
      });
      setLoadingSessions(true);
      try {
        const response = await scopedFetch(
          '/agentic/sessions?limit=50',
          {},
          scope,
        );
        if (!response || !isCurrentScope(scope)) {
          return false;
        }
        logger.api('GET', '/agentic/sessions?limit=50', response.status);
        if (!response.ok) {
          throw await responseError(response, 'Failed to load chats');
        }
        const payload = await response.json();
        if (!isCurrentScope(scope)) {
          return false;
        }
        const page = normalizeSessionPage(payload);
        const sorted = sortSessions(page.sessions);
        const serverSessions =
          mutationEpochAtStart === collectionMutationEpochRef.current ?
            sorted :
            sortSessions(
              mergeSessionCollections(
                sorted,
                sessionsRef.current,
                deletedSessionIdsRef.current,
              ),
            );
        const nextSessions = sortSessions(
          mergeSessionPage({
            current: sessionsRef.current,
            incoming: serverSessions,
            selectedId: currentSessionRef.current.id,
            deletedSessionIds: deletedSessionIdsRef.current,
          }),
        );
        setSessions(nextSessions);
        nextSessionCursorRef.current = page.nextCursor;
        setHasMoreSessions(page.hasMore);

        if (selectionEpochRef.current !== selectionEpochAtStart) {
          return true;
        }
        const selection = resolveInitialSessionSelection({
          persistedId: readSelectedSession(scope.workspaceId),
          currentId: currentSessionRef.current.id,
          sessions: nextSessions,
          selectLatest,
        });
        const nextId = selection.sessionId;

        if (!nextId) {
          selectionEpochRef.current += 1;
          setCurrentSession({ id: null, messages: [] });
          persistSelectedSession(scope.workspaceId, '');
          return true;
        }
        const opened = await openChat(nextId);
        if (
          !opened &&
          selection.requiresDirectHydration &&
          nextSessions[0]?.id
        ) {
          await openChat(nextSessions[0].id);
        }
        return true;
      } catch (error) {
        if (error?.name !== 'AbortError') {
          logger.error('Failed to load sessions', { error: error.message });
        }
        return false;
      } finally {
        if (isCurrentScope(scope)) {
          setLoadingSessions(false);
        }
      }
    },
    [
      captureScope,
      isCurrentScope,
      openChat,
      scopedFetch,
      setCurrentSession,
      setSessions,
    ],
  );

  const loadMoreSessions = useCallback(async () => {
    const cursor = nextSessionCursorRef.current;
    if (!cursor || !hasMoreSessions || loadingMoreSessionsRef.current) {
      return false;
    }
    const scope = captureScope();
    loadingMoreSessionsRef.current = true;
    setLoadingMoreSessions(true);
    const url = `/agentic/sessions?limit=50&cursor=${encodeURIComponent(cursor)}`;
    try {
      const response = await scopedFetch(url, {}, scope);
      if (!response || !isCurrentScope(scope)) {
        return false;
      }
      logger.api('GET', url, response.status);
      if (!response.ok) {
        if (response.status === 400) {
          await loadSessions(false);
          return false;
        }
        throw await responseError(response, 'Failed to load more chats');
      }
      const page = normalizeSessionPage(await response.json());
      if (!isCurrentScope(scope)) {
        return false;
      }
      setSessions((current) =>
        sortSessions(
          mergeSessionPage({
            current,
            incoming: page.sessions,
            append: true,
            selectedId: currentSessionRef.current.id,
            deletedSessionIds: deletedSessionIdsRef.current,
          }),
        ),
      );
      nextSessionCursorRef.current = page.nextCursor;
      setHasMoreSessions(page.hasMore);
      return true;
    } catch (error) {
      if (error?.name !== 'AbortError') {
        logger.error('Failed to load more sessions', { error: error.message });
      }
      return false;
    } finally {
      loadingMoreSessionsRef.current = false;
      if (isCurrentScope(scope)) {
        setLoadingMoreSessions(false);
      }
    }
  }, [
    captureScope,
    hasMoreSessions,
    isCurrentScope,
    loadSessions,
    scopedFetch,
    setSessions,
  ]);

  const startNewChat = useCallback(() => {
    const scope = captureScope();
    selectionEpochRef.current += 1;
    setCurrentSession({ id: null, messages: [] });
    persistSelectedSession(scope.workspaceId, '');
  }, [captureScope, setCurrentSession]);

  const resetSessionsState = useCallback(() => {
    logger.info('Resetting chat session state for workspace/auth scope change');
    requestControllersRef.current.forEach((controller) => controller.abort());
    requestControllersRef.current = new Set();
    scopeRef.current = createSessionScope(
      getCurrentWorkspaceId(),
      scopeRef.current.epoch + 1,
    );
    selectionEpochRef.current += 1;
    hydratedSessionsRef.current = new Set();
    sessionDataRef.current = new Map();
    collectionMutationEpochRef.current += 1;
    sessionMutationEpochRef.current = new Map();
    hydrateSequenceRef.current = new Map();
    hydrateInFlightRef.current = new Map();
    deletedSessionIdsRef.current = new Set();
    pinTransactionsRef.current = new Map();
    firstSendCoordinator.reset();
    setLoadingSessions(false);
    setLoadingMoreSessions(false);
    loadingMoreSessionsRef.current = false;
    setHasMoreSessions(false);
    nextSessionCursorRef.current = null;
    setSessions([]);
    setCurrentSession({ id: null, messages: [] });
  }, [firstSendCoordinator, setCurrentSession, setSessions]);

  const selectChat = useCallback(
    async (id) => {
      if (!id || id === currentSessionRef.current.id) {
        return null;
      }
      return openChat(id);
    },
    [openChat],
  );

  const touchSession = useCallback(
    (sessionId, timestamp = new Date().toISOString()) => {
      if (!sessionId) {
        return;
      }
      noteSessionMutation(sessionId);
      setSessions((previous) =>
        sortSessions(
          previous.map((session) =>
            session.id === sessionId ?
              { ...session, updated_at: timestamp } :
              session,
          ),
        ),
      );
    },
    [noteSessionMutation, setSessions],
  );

  const generateSessionTitle = useCallback(
    async (sessionId, message) => {
      if (!sessionId || !message) {
        return false;
      }
      const scope = captureScope();
      try {
        const response = await scopedFetch(
          `/agentic/sessions/${sessionId}/generate-title`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message }),
          },
          scope,
        );
        if (!response || !isCurrentScope(scope)) {
          return false;
        }
        if (!response.ok) {
          throw await responseError(response, 'Failed to generate title');
        }
        const data = await response.json();
        noteSessionMutation(sessionId);
        const updatedAt = data.updated_at || new Date().toISOString();
        setSessions((previous) =>
          sortSessions(
            previous.map((session) =>
              session.id === sessionId ?
                { ...session, title: data.title, updated_at: updatedAt } :
                session,
            ),
          ),
        );
        return true;
      } catch (error) {
        if (error?.name !== 'AbortError') {
          logger.error('Failed to generate session title', {
            error: error.message,
          });
        }
        return false;
      }
    },
    [
      captureScope,
      isCurrentScope,
      noteSessionMutation,
      scopedFetch,
      setSessions,
    ],
  );

  const setPinned = useCallback(
    async (sessionId, pinned) => {
      if (!sessionId || typeof pinned !== 'boolean') {
        return false;
      }
      const scope = captureScope();
      const transactionId = Symbol(sessionId);
      const transaction = beginSessionPinUpdate(
        sessionsRef.current,
        sessionId,
        pinned,
      );
      noteSessionMutation(sessionId);
      pinTransactionsRef.current.set(sessionId, { transactionId, transaction });
      setSessions(transaction.optimistic);
      try {
        const response = await scopedFetch(
          `/agentic/sessions/${sessionId}`,
          {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pinned }),
          },
          scope,
        );
        if (!response || !isCurrentScope(scope)) {
          return false;
        }
        if (!response.ok) {
          throw await responseError(response, 'Failed to update session pin');
        }
        const updated = await response.json();
        if (
          pinTransactionsRef.current.get(sessionId)?.transactionId ===
          transactionId
        ) {
          noteSessionMutation(sessionId);
          setSessions((previous) =>
            commitSessionPinUpdate(previous, sessionId, updated),
          );
          pinTransactionsRef.current.delete(sessionId);
        }
        return true;
      } catch (error) {
        if (error?.name !== 'AbortError') {
          logger.error('Failed to update session pin', {
            sessionId,
            error: error.message,
          });
        }
        if (
          isCurrentScope(scope) &&
          pinTransactionsRef.current.get(sessionId)?.transactionId ===
            transactionId
        ) {
          noteSessionMutation(sessionId);
          setSessions((previous) =>
            rollbackSessionPinUpdate(previous, transaction),
          );
          pinTransactionsRef.current.delete(sessionId);
        }
        return false;
      }
    },
    [
      captureScope,
      isCurrentScope,
      noteSessionMutation,
      scopedFetch,
      setSessions,
    ],
  );

  return {
    sessions,
    currentChatId,
    messages,
    activeSessionIds: streamManager.activeSessionIds,
    streamStates: streamManager.streamStates,
    setMessages,
    loadingSessions,
    loadingMoreSessions,
    hasMoreSessions,
    firstSendPending,
    loadSessions,
    loadMoreSessions,
    createChat,
    runFirstSend,
    deleteChat,
    selectChat,
    startNewChat,
    resetSessionsState,
    openChat,
    hydrateChat,
    generateSessionTitle,
    setPinned,
    touchSession,
  };
};

export default useChatSessions;
