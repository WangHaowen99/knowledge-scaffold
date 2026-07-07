import { useEffect, useRef, useState } from 'react';
import { PanelRightClose, PanelRightOpen } from 'lucide-react';
import useAgenticStream from '../hooks/useAgenticStream';
import { authFetch } from '../utils/auth';
import { useAuthContext } from './useAuthContext';
import Markdown from './Markdown';
import { formatTime, truncate } from './chatUtils';

// 新建对话（知识应用）：消息线程 + 输入区 + 右侧 Trace 竖栏。
// 历史对话列表在应用侧边栏（ChatHistorySidebar）；会话状态由 AppShell 的
// useChatSessions 下发。流式协议复用 useStreamManager / useAgenticStream（agent-stream.v2）。
export default function ChatWorkspace({ chat, streamManager }) {
  const { workspaceId } = useAuthContext();
  const { sessions, currentChatId, messages, setMessages, createChat, generateSessionTitle } = chat;
  const { sendAgentic } = useAgenticStream({ sessionId: currentChatId, setMessages, streamManager });
  const [input, setInput] = useState('');
  const [traceOpen, setTraceOpen] = useState(true);
  const bodyRef = useRef(null);

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

  const current = sessions.find((s) => s.id === currentChatId);
  const streamState = currentChatId ? streamManager.getStreamState(currentChatId) : { isLoading: false };

  return (
    <div className="chatwrap">
      <div className="thread">
        <div className="th-head">
          <span className="tt">{current?.title || '新对话'}</span>
          {current && <span className="tm">{formatTime(current.updated_at || current.created_at)}</span>}
          <button className="iconbtn" style={{ marginLeft: 'auto' }} title={traceOpen ? '收起 Trace' : '展开 Trace'}
            onClick={() => setTraceOpen((v) => !v)}>
            {traceOpen ? <PanelRightClose size={15} /> : <PanelRightOpen size={15} />}
          </button>
        </div>
        <div className="th-body" ref={bodyRef}>
          {messages.map((m) => <MessageBubble key={m.id} msg={m} />)}
          {!messages.length && (
            <div className="empty" style={{ paddingTop: 80 }}>
              开始新对话——向知识包提问，例如：“分析一下某个 CHANGE_ORDER 的综合影响、成本和周期”
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
      {traceOpen && <TracePanel messages={messages} />}
    </div>
  );
}

/* ---------------- Trace 竖栏 ---------------- */

function TracePanel({ messages }) {
  const turns = messages.filter((m) => m.role === 'assistant');
  return (
    <div className="trace">
      <div className="tr-head">运行轨迹 · Trace</div>
      <div className="tr-body">
        {!turns.length && <div className="ch-empty" style={{ padding: '30px 12px' }}>发起对话后，这里按轮次展示模型思考、工具调用与状态明细</div>}
        {turns.map((m, i) => <TraceTurn key={m.id} msg={m} index={i + 1} />)}
      </div>
    </div>
  );
}

function TraceTurn({ msg, index }) {
  const segs = (msg.streamSegments || []).filter((s) => s.type !== 'content');
  const running = !!msg.isLoading;
  const status = running ? 'running' : (msg.status || 'finished');
  const statusText = { running: '进行中', finished: '成功', error: '出错', cancelled: '已停止', awaiting: '等待输入' }[status] || status;
  return (
    <div className="tr-turn">
      <div className="tr-turn-h">
        <span className={`tr-dot ${status}`} />
        <span className="tr-turn-t">第 {index} 轮</span>
        <span className="tr-turn-s">{statusText}{msg.ts ? ` · ${formatTime(msg.ts)}` : ''}</span>
      </div>
      {segs.map((seg, i) => <TraceRow key={i} seg={seg} />)}
      {msg.runId && <div className="tr-run">run: {String(msg.runId).slice(0, 24)}…</div>}
    </div>
  );
}

function TraceRow({ seg }) {
  const done = seg.finished || seg.state === 'done' || seg.state === 'final';
  let icon = '·';
  let label = '';
  if (seg.type === 'tool') {
    icon = '⚙';
    label = seg.meta?.tool_name || '工具调用';
  } else if (seg.type === 'reasoning') {
    icon = '🧠';
    label = '模型思考';
  } else if (seg.type === 'subagent' || seg.type === 'subagent_group') {
    icon = '⑂';
    label = seg.meta?.title || '子任务';
  } else {
    label = seg.type;
  }
  const elapsed = seg.meta?.elapsed_ms ? `${(seg.meta.elapsed_ms / 1000).toFixed(1)}s` : '';
  const detail = truncate(seg.text || '', 60);
  return (
    <div className="tr-row" title={seg.text || ''}>
      <span className="tr-ic">{icon}</span>
      <span className="tr-lb">{label}{detail && seg.type === 'tool' ? ` · ${detail}` : ''}</span>
      <span className="tr-st">{elapsed || (done ? '✓' : '…')}</span>
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
      <div className="av">知</div>
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
