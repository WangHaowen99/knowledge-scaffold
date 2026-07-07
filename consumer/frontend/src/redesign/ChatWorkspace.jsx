import { useEffect, useMemo, useRef, useState } from 'react';
import useChatSessions from '../hooks/useChatSessions';
import useAgenticStream from '../hooks/useAgenticStream';
import { authFetch } from '../utils/auth';
import { useAuthContext } from './useAuthContext';
import Markdown from './Markdown';

// 知识应用：左侧历史记录 + 右侧富消息线程。
// 流式协议复用现有 useStreamManager / useAgenticStream（agent-stream.v2）。
export default function ChatWorkspace({ streamManager }) {
  const { workspaceId } = useAuthContext();
  const {
    sessions, currentChatId, messages, setMessages,
    loadSessions, createChat, deleteChat, selectChat, startNewChat, generateSessionTitle,
  } = useChatSessions(streamManager);
  const { sendAgentic } = useAgenticStream({ sessionId: currentChatId, setMessages, streamManager });
  const [input, setInput] = useState('');
  const [search, setSearch] = useState('');
  const bodyRef = useRef(null);

  useEffect(() => { loadSessions(true); }, [loadSessions, workspaceId]);

  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const send = async () => {
    const text = input.trim();
    if (!text) return;
    setInput('');
    let sessionId = currentChatId;
    let isNew = false;
    if (!sessionId) {
      const modelProfileId = localStorage.getItem(`mw_ws_model_${workspaceId}`) || undefined;
      const session = await createChat({ llmRequestMode: 'stream', modelProfileId });
      if (!session) return;
      sessionId = session.id;
      isNew = true;
    }
    sendAgentic(text, sessionId, {});
    if (isNew) generateSessionTitle(sessionId, text);
  };

  const groups = useMemo(() => groupSessions(sessions, search), [sessions, search]);
  const current = sessions.find((s) => s.id === currentChatId);
  const streamState = currentChatId ? streamManager.getStreamState(currentChatId) : { isLoading: false };

  return (
    <div className="chatwrap">
      <div className="hist">
        <div className="h-top">
          <button className="btn btn-p" style={{ width: '100%' }} onClick={() => startNewChat()}>＋ 新建对话</button>
          <input className="kb-search" placeholder="搜索历史对话…" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <div className="h-list">
          {groups.map(([label, items]) => (
            <div key={label}>
              <div className="hg">{label}</div>
              {items.map((s) => (
                <div key={s.id} className={`hs ${s.id === currentChatId ? 'on' : ''}`} onClick={() => selectChat(s.id)}>
                  <div className="t">{s.title || '未命名对话'}</div>
                  <div className="m">
                    {formatTime(s.updated_at || s.created_at)} · {s.message_count ?? 0} 条
                    <a style={{ float: 'right', color: 'var(--muted)' }} title="删除对话"
                      onClick={(e) => { e.stopPropagation(); if (window.confirm('删除该对话？')) deleteChat(s.id); }}>✕</a>
                  </div>
                </div>
              ))}
            </div>
          ))}
          {!sessions.length && <div className="empty">还没有对话，直接在右侧提问开始</div>}
        </div>
      </div>
      <div className="thread">
        <div className="th-head">
          <span className="tt">{current?.title || '新对话'}</span>
          {current && <span className="tm">{formatTime(current.updated_at || current.created_at)}</span>}
        </div>
        <div className="th-body" ref={bodyRef}>
          {messages.map((m) => <MessageBubble key={m.id} msg={m} />)}
          {!messages.length && (
            <div className="empty" style={{ paddingTop: 80 }}>
              向知识包提问，例如：“分析一下某个 CHANGE_ORDER 的综合影响、成本和周期”
            </div>
          )}
        </div>
        <div className="th-input">
          <div className="ti-box">
            <input value={input} placeholder="输入问题，Enter 发送…"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) send(); }} />
            {streamState.isLoading
              ? <button className="btn" onClick={() => streamManager.stopStreamForSession(currentChatId)}>停止</button>
              : <button className="btn btn-p" onClick={send}>发送</button>}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------------- 富消息渲染 ---------------- */

function MessageBubble({ msg }) {
  if (msg.role === 'user') {
    return (
      <div className="msg u">
        <div className="bubwrap">
          <div className="bub">{msg.content}</div>
          {msg.ts && <div className="time">{formatTime(msg.ts)}</div>}
        </div>
      </div>
    );
  }
  return <AssistantBubble msg={msg} />;
}

function AssistantBubble({ msg }) {
  const segments = msg.streamSegments || [];
  const stepSegs = segments.filter((s) => s.type !== 'content');
  const toolNames = [...new Set(stepSegs.filter((s) => s.type === 'tool').map((s) => s.meta?.tool_name).filter(Boolean))];
  const running = !!msg.isLoading;
  const isError = msg.status === 'error';
  const content = msg.content || '';

  return (
    <div className="msg a">
      <div className="av">M</div>
      <div className="bubwrap">
        <div className={`bub ${isError ? 'err' : ''}`}>
          {toolNames.length > 0 && (
            <div className="refchips">
              {toolNames.map((t) => <span key={t} className="refchip">⚙ {t}</span>)}
            </div>
          )}
          {stepSegs.length > 0 && (
            <details className="steps" open={running}>
              <summary><span className="car">▶</span>执行过程 · {stepSegs.length} 步{running ? ' · 进行中' : ''}</summary>
              {stepSegs.map((seg, i) => <StepRow key={i} seg={seg} />)}
            </details>
          )}
          {content
            ? <Markdown>{content}</Markdown>
            : running && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 10, whiteSpace: 'nowrap' }}>
                <span className="tdots"><span /><span /><span /></span>
                <span style={{ color: 'var(--muted)', fontSize: 12.5 }}>正在思考…</span>
              </span>
            )}
          {isError && !content && <span>回答生成失败，请重试。</span>}
          {!running && (msg.status === 'finished' || isError || msg.status === 'cancelled') && (
            <div className="statusline">
              {isError ? '✗ 出错' : msg.status === 'cancelled' ? '⏹ 已停止' : '✓ 已完成'}
              {toolNames.length > 0 && ` · 调用 ${toolNames.length} 个工具`}
              {msg.runId && <FeedbackBar runId={msg.runId} content={content} />}
            </div>
          )}
        </div>
        {msg.ts && <div className="time">{formatTime(msg.ts)}</div>}
      </div>
    </div>
  );
}

function StepRow({ seg }) {
  const done = seg.finished || seg.state === 'done' || seg.state === 'final';
  let label = '';
  if (seg.type === 'tool') label = `${seg.meta?.tool_name ? `工具 ${seg.meta.tool_name}` : '工具调用'}${seg.text ? ` — ${truncate(seg.text, 120)}` : ''}`;
  else if (seg.type === 'reasoning') label = `思考：${truncate(seg.text, 120)}`;
  else if (seg.type === 'subagent' || seg.type === 'subagent_group') label = `子任务：${truncate(seg.text || seg.meta?.title || '', 120)}`;
  else label = truncate(seg.text || seg.type, 120);
  return (
    <div className="step">
      <span className="ok">{done ? '✓' : '…'}</span>
      <span>{label}</span>
    </div>
  );
}

function FeedbackBar({ runId, content }) {
  const [voted, setVoted] = useState('');
  const vote = async (v) => {
    setVoted(v);
    try {
      await authFetch(`/agentic/runs/${encodeURIComponent(runId)}/feedback`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ vote: v }),
      });
    } catch { /* 反馈失败静默 */ }
  };
  return (
    <span className="fb">
      <button title="有帮助" style={voted === 'up' ? { color: 'var(--good)' } : undefined} onClick={() => vote('up')}>👍</button>
      <button title="没帮助" style={voted === 'down' ? { color: 'var(--crit)' } : undefined} onClick={() => vote('down')}>👎</button>
      <button onClick={() => navigator.clipboard?.writeText(content || '')}>复制</button>
    </span>
  );
}

/* ---------------- 工具函数 ---------------- */

function groupSessions(sessions, search) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const yesterday = today - 86400000;
  const buckets = { 今天: [], 昨天: [], 更早: [] };
  for (const s of sessions) {
    if (search && !(s.title || '').toLowerCase().includes(search.toLowerCase())) continue;
    const ts = Date.parse(s.updated_at || s.created_at || '') || 0;
    if (ts >= today) buckets['今天'].push(s);
    else if (ts >= yesterday) buckets['昨天'].push(s);
    else buckets['更早'].push(s);
  }
  return Object.entries(buckets).filter(([, items]) => items.length);
}

function formatTime(ts) {
  if (!ts) return '';
  const d = typeof ts === 'number' ? new Date(ts) : new Date(String(ts));
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hh = `${d.getHours()}`.padStart(2, '0');
  const mm = `${d.getMinutes()}`.padStart(2, '0');
  return sameDay ? `${hh}:${mm}` : `${d.getMonth() + 1}-${d.getDate()} ${hh}:${mm}`;
}

function truncate(s, n) {
  const t = String(s || '').replace(/\s+/g, ' ').trim();
  return t.length > n ? `${t.slice(0, n)}…` : t;
}
