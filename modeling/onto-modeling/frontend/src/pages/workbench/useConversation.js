// 对话数据：S-01 对话列表、S-05 状态快照、S-06 历史，再带 after=<最后 seq> 接 S-13 对话流
import {
  startTransition, useCallback, useEffect, useReducer, useRef, useState,
} from 'react';
import { useApp, useProject } from '../../app/context.js';
import { take } from '../../app/earlyData.js';
import {
  ackLocalUser, addLocalUser, applyEvent, applyEvents, applySnapshot, dropLocalUser, initialConversation, prependHistory,
} from '../../state/conversation.js';

function reducer(state, action) {
  switch (action.type) {
    case 'reset': return initialConversation();
    // loaded：历史已到，时间线据此分出「打开时已有的」（不播入场动画）和之后新出现的
    case 'history': return { ...applyEvents(initialConversation(), action.events), loaded: true };
    case 'event': return applyEvent(state, action.env);
    case 'events': return applyEvents(state, action.list);
    case 'snapshot': return applySnapshot(state, action.snap);
    case 'prepend': return prependHistory(state, action.events);
    case 'local_user': return addLocalUser(state, action.id, action.text);
    case 'local_ack': return ackLocalUser(state, action.id, action.messageId);
    case 'local_drop': return dropLocalUser(state, action.id);
    default: return state;
  }
}

// 打开时取最近 200 条持久事件（S-06），往上滚再每页 200 条往前翻
const HISTORY_FIRST = 200;
const HISTORY_PAGE = 200;

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
  // 往前翻页：firstSeq 是已加载的最早一条
  const [older, setOlder] = useState({ hasMore: false, firstSeq: null, loading: false });
  const olderRef = useRef(older);
  olderRef.current = older;
  const cidRef = useRef(cid);
  cidRef.current = cid;

  const loadList = useCallback(async () => {
    const r = await take(`conversations:${pid}`, () => api.conversations(pid));
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
    // 同一帧里到的事件（流式片段一秒几十个）攒成一次更新，时间线一帧只重画一次
    let pending = [];
    let frame = 0;
    const flush = () => { frame = 0; const list = pending; pending = []; if (alive && list.length) dispatch({ type: 'events', list }); };
    const onEvent = (env) => {
      pending.push(env);
      if (!frame) frame = (window.requestAnimationFrame || ((f) => setTimeout(f, 16)))(flush);
    };
    dispatch({ type: 'reset' });
    setOlder({ hasMore: false, firstSeq: null, loading: false });
    (async () => {
      try {
        const [hist, snap] = await Promise.all([api.messages(pid, cid, { limit: HISTORY_FIRST }), api.conversationState(pid, cid)]);
        if (!alive) return;
        // 历史一次画一整条时间线（Markdown 解析占大头）：放进 transition 分片渲染，不在一个长任务里卡住输入
        startTransition(() => {
          dispatch({ type: 'history', events: hist.items || [] });
          setOlder({ hasMore: !!hist.has_more, firstSeq: hist.first_seq ?? null, loading: false });
          dispatch({ type: 'snapshot', snap });
        });
        stop = services.openStream({
          kind: 'conversation',
          url: (q) => api.conversationEventsUrl(pid, cid, q),
          lastId: hist.last_seq || 0,
          onEvent,
          onStatus: setStream,
        });
      } catch (e) {
        if (alive) setLoadError(e);
      }
    })();
    return () => { alive = false; stop?.(); if (frame) (window.cancelAnimationFrame || clearTimeout)(frame); };
  }, [api, services, pid, cid]);

  // 项目流里的对话列表变化（EP-21）
  useEffect(() => subscribe((env) => {
    if (env.type === 'conversation.updated') loadList().catch(() => {});
  }), [subscribe, loadList]);

  const loadEarlier = useCallback(async () => {
    const cur = olderRef.current;
    if (!cid || !cur.hasMore || cur.loading || cur.firstSeq == null) return;
    setOlder({ ...cur, loading: true });
    try {
      const r = await api.messages(pid, cid, { limit: HISTORY_PAGE, before_seq: cur.firstSeq });
      if (cidRef.current !== cid) return;
      startTransition(() => dispatch({ type: 'prepend', events: r.items || [] }));
      setOlder({ hasMore: !!r.has_more, firstSeq: r.first_seq ?? cur.firstSeq, loading: false });
    } catch {
      if (cidRef.current === cid) setOlder({ ...cur, loading: false });
    }
  }, [api, pid, cid]);

  return {
    list, cid, setCid, state, dispatch, stream, loadError, reloadList: loadList, older, loadEarlier,
  };
}
