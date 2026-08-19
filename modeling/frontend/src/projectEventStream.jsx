import React, { createContext, useContext, useEffect, useMemo, useRef } from 'react';

import { subscribeProjectEvents } from './authoring-api.js';

const ProjectEventStreamContext = createContext(null);

/**
 * 一个项目只开一条事件流，组件树内的订阅者共享同一条连接。
 *
 * 默认从 0 开始回放：晚挂载的订阅者要靠历史事件重建进行中的流式状态（如流程图生成）。
 * 订阅者各自用 createProjectEventReplayGate 过滤已体现在自己快照里的历史事件。
 * 起始游标只在首次挂载时读取：连接建立后由 subscribeProjectEvents 自己按 seq 续传，
 * 若把游标放进依赖，每来一个事件都会重建连接。
 */
export function ProjectEventStreamProvider({ projectId, initialCursor = 0, onError, children }) {
  const handlers = useRef(null);
  if (handlers.current === null) handlers.current = new Set();
  const errorHandler = useRef(onError);
  errorHandler.current = onError;
  const startCursor = useRef(null);
  if (startCursor.current === null) {
    const parsed = Number(initialCursor);
    startCursor.current = Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
  }

  useEffect(() => subscribeProjectEvents(projectId, {
    after: startCursor.current,
    onEvent: (event) => {
      for (const handler of [...handlers.current]) {
        try { handler(event); } catch { /* 单个订阅者异常不影响其他订阅者 */ }
      }
    },
    onError: (error) => errorHandler.current?.(error),
  }), [projectId]);

  const stream = useMemo(() => ({
    subscribe(handler) {
      handlers.current.add(handler);
      return () => { handlers.current.delete(handler); };
    },
  }), []);

  return (
    <ProjectEventStreamContext.Provider value={stream}>
      {children}
    </ProjectEventStreamContext.Provider>
  );
}

/**
 * 订阅项目事件。有 Provider 时复用共享连接；没有时自建一条，
 * 使组件在独立挂载（历史入口、测试）下行为一致。
 */
export function useProjectEvents(projectId, handler, { initialCursor = 0, enabled = true } = {}) {
  const stream = useContext(ProjectEventStreamContext);
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  const fallbackCursor = useRef(null);
  if (fallbackCursor.current === null) fallbackCursor.current = initialCursor;

  useEffect(() => {
    if (!enabled) return undefined;
    const deliver = (event) => { handlerRef.current?.(event); };
    if (stream) return stream.subscribe(deliver);
    return subscribeProjectEvents(projectId, { after: fallbackCursor.current, onEvent: deliver });
  }, [stream, projectId, enabled]);
}
