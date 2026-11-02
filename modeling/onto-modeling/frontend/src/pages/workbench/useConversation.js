// 对话数据：S-01 对话列表、S-05 状态快照、S-06 历史，再带 after=<最后 seq> 接 S-13 对话流
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { applyEvent, applyEvents, applySnapshot, initialConversation } from '../../state/conversation.js';

function reducer(state, action) {
  switch (action.type) {
    case 'reset': return initialConversation();
    case 'history': return applyEvents(initialConversation(), action.events);
    case 'event': return applyEvent(state, action.env);
    case 'snapshot': return applySnapshot(state, action.snap);
    default: return state;
  }
}

export function useConversation(preferredId, { preferAutobuild = false } = {}) {
  const { api, services } = useApp();
  const { pid, subscribe } = useProject();
  const [list, setList] = useState(null);
  const [cid, setCid] = useState(null);
  const [state, dispatch] = useReducer(reducer, undefined, initialConversation);
  const [stream, setStream] = useState('connecting');
  const [loadError, setLoadError] = useState(null);
  const lastSeq = useRef(0);
  lastSeq.current = state.lastSeq;

  const loadList = useCallback(async () => {
    const r = await api.conversations(pid);
    setList(r);
    return r;
  }, [api, pid]);

  // 选对话：地址里指定的 > 引擎在线的 > 最近更新的
  useEffect(() => {
    let alive = true;
    loadList().then((r) => {
      if (!alive) return;
      const items = r.items || [];
      // l4-run 页没指定对话时，取最近的运行对话
      const runConv = preferAutobuild ? items.find((c) => c.kind === 'autobuild') : null;
      const pick = items.find((c) => c.id === preferredId) || runConv || items.find((c) => c.id === r.live?.conversation_id) || items[0];
      setCid(pick?.id || null);
    }).catch((e) => alive && setLoadError(e));
    return () => { alive = false; };
  }, [loadList, preferredId, preferAutobuild]);

  useEffect(() => {
    if (!cid) return undefined;
    let alive = true;
    let stop = null;
    dispatch({ type: 'reset' });
    (async () => {
      try {
        const [hist, snap] = await Promise.all([api.messages(pid, cid, { limit: 200 }), api.conversationState(pid, cid)]);
        if (!alive) return;
        dispatch({ type: 'history', events: hist.items || [] });
        dispatch({ type: 'snapshot', snap });
        stop = services.openStream({
          kind: 'conversation',
          url: (q) => api.conversationEventsUrl(pid, cid, q),
          lastId: hist.last_seq || 0,
          onEvent: (env) => dispatch({ type: 'event', env }),
          onStatus: setStream,
        });
      } catch (e) {
        if (alive) setLoadError(e);
      }
    })();
    return () => { alive = false; stop?.(); };
  }, [api, services, pid, cid]);

  // 项目流里的对话列表变化（EP-21）
  useEffect(() => subscribe((env) => {
    if (env.type === 'conversation.updated') loadList().catch(() => {});
  }), [subscribe, loadList]);

  return { list, cid, setCid, state, dispatch, stream, loadError, reloadList: loadList };
}
