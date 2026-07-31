import React, { useCallback, useEffect, useRef, useState } from 'react';

import { advanceProjectSession, getProjectSession } from './authoring-api.js';
import AiConstructionWorkbench from './AiConstructionWorkbench.jsx';
import PackageWorkspace from './PackageWorkspace.jsx';
import { isProgressOnlyMessageEvent } from './aiConstructionLiveState.js';
import { ProjectEventStreamProvider, useProjectEvents } from './projectEventStream.jsx';

const REFRESH_DEBOUNCE_MS = 200;

export function projectWorkbenchMode(session) {
  if (session?.project?.kind === 'package') return 'package';
  return 'ai_construction';
}

/**
 * 工作流步骤状态只在事件到达时重取，且合并成批：
 * 事件常常成组到达（一次编排会写多条消息），逐条重取会让整个工作台反复抖动。
 */
function WorkflowRefreshOnEvents({ projectId, onRefresh }) {
  const timer = useRef(null);
  const refresh = useRef(onRefresh);
  refresh.current = onRefresh;

  useProjectEvents(projectId, (event) => {
    if (isProgressOnlyMessageEvent(event) || timer.current) return;
    timer.current = setTimeout(() => {
      timer.current = null;
      void refresh.current?.();
    }, REFRESH_DEBOUNCE_MS);
  });

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  return null;
}

export default function ProjectWorkbenchRouter({
  projectId,
  initialSession = null,
  onUnsavedChange,
}) {
  const [session, setSession] = useState(initialSession);
  const [error, setError] = useState('');

  const load = useCallback(async (options) => {
    // 进入项目时对账一次状态机；事件驱动的刷新保持纯读
    const readSession = options?.advance === true ? advanceProjectSession : getProjectSession;
    try {
      const next = await readSession(projectId);
      setSession(next);
      setError('');
      return next;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '项目工作台加载失败');
      throw cause;
    }
  }, [projectId]);

  useEffect(() => {
    if (initialSession?.project?.id === projectId) return;
    setSession(null);
    void load({ advance: true }).catch(() => undefined);
  }, [projectId, initialSession, load]);

  if (!session) return <div className="empty">{error || '加载项目工作台…'}</div>;
  const mode = projectWorkbenchMode(session);
  const content = mode === 'ai_construction'
    ? (
      <AiConstructionWorkbench
        projectId={projectId}
        session={session}
        onProjectChanged={load}
        onUnsavedChange={onUnsavedChange}
      />
    )
    : <PackageWorkspace projectId={projectId} initialSession={session}
        onUnsavedChange={onUnsavedChange} />;

  return (
    <ProjectEventStreamProvider key={projectId} projectId={projectId}>
      <WorkflowRefreshOnEvents projectId={projectId} onRefresh={load} />
      {content}
    </ProjectEventStreamProvider>
  );
}
