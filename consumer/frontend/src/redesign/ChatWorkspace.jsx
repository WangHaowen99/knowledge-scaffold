import { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ArrowUpRight, BrainCircuit, Check, ChevronDown, Circle, GitBranch, PanelRightClose, PanelRightOpen, Sparkles, Wrench, X } from 'lucide-react';
import useAgenticStream from '../hooks/useAgenticStream';
import { authFetch } from '../utils/auth';
import { getWorkspaceAgentMode, normalizeAgentMode } from '../utils/workspacePreferences';
import {
  isSessionResumableStatus,
  isSessionWaitingForUserStatus,
  resolveSessionPreferences,
} from '../utils/sessionModel';
import { useAuthContext } from './useAuthContext';
import Markdown from './Markdown';
import { formatTime, truncate } from './chatUtils';
import { StatusTag } from './ui';

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
// useChatSessions 下发。流式协议复用 useStreamManager / useAgenticStream（agent-stream.v2）。
export default function ChatWorkspace({ chat, streamManager }) {
  const { workspaceId } = useAuthContext();
  const { sessions, currentChatId, messages, setMessages, createChat, generateSessionTitle } = chat;
  const { sendAgentic, resumeAgentic } = useAgenticStream({ sessionId: currentChatId, setMessages, streamManager });
  const [input, setInput] = useState('');
  const inputRef = useRef(null);
  const [traceOpen, setTraceOpen] = useState(() => {
    try { return !window.matchMedia('(max-width: 900px)').matches; } catch { return true; }
  });
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
  // Trace 栏宽度：可拖拽分隔条调整，最小 300（初始尺寸），持久化到 localStorage。
  const TRACE_MIN_W = 300;
  const [traceWidth, setTraceWidth] = useState(
    () => Math.max(TRACE_MIN_W, Number(localStorage.getItem('mw_trace_w')) || TRACE_MIN_W)
  );
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
  const bodyRef = useRef(null);
  const resumedRunsRef = useRef(new Set());

  useEffect(() => {
    traceWidthRef.current = traceWidth;
  }, [traceWidth]);

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(([entry]) => {
      const maxWidth = Math.max(TRACE_MIN_W, entry.contentRect.width - 360);
      setTraceWidth((currentWidth) => Math.min(Math.max(TRACE_MIN_W, currentWidth), maxWidth));
    });
    observer.observe(wrap);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const current = sessions.find((session) => session.id === currentChatId);
  const sessionLocked = Boolean(currentChatId);
  const pendingInteraction = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (
        message?.role === 'assistant'
        && isSessionWaitingForUserStatus(message.status)
        && message.interaction?.id
      ) return message.interaction;
    }
    return null;
  }, [messages]);

  useEffect(() => {
    let cancelled = false;
    const loadOptions = async () => {
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
  }, [workspaceId]);

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
        defaultModelProfileId,
      });
      setAgentMode(preferences.agentMode);
      setModelProfileId(preferences.modelProfileId);
      setSelectedOntologyId(preferences.selectedOntologyId);
    }, 0);
    return () => clearTimeout(timer);
  }, [agentModeKey, current?.metadata, currentChatId, defaultModelProfileId, workspaceId]);

  useEffect(() => {
    const runId = String(current?.active_run_id || '').trim();
    const status = String(current?.active_run_status || '').trim();
    if (!runId || !isSessionResumableStatus(status)) return;
    const resumeKey = `${currentChatId}:${runId}`;
    if (resumedRunsRef.current.has(resumeKey)) return;
    resumedRunsRef.current.add(resumeKey);
    void resumeAgentic({
      runId,
      targetSessionId: currentChatId,
      afterSequence: current?.last_seq || 0,
      status,
    }).then((resumed) => {
      if (!resumed) resumedRunsRef.current.delete(resumeKey);
    });
  }, [current?.active_run_id, current?.active_run_status, current?.last_seq, currentChatId, resumeAgentic]);

  useEffect(() => {
    resumedRunsRef.current = new Set();
  }, [workspaceId]);

  const send = async () => {
    const text = input.trim();
    if (!text || streamState.isLoading) return;
    let sessionId = currentChatId;
    let isNew = false;
    try {
      if (!sessionId) {
        const session = await createChat({ llmRequestMode: 'stream', modelProfileId, agentMode });
        if (!session) return;
        sessionId = session.id;
        isNew = true;
        if (selectedOntologyId) {
          safeLocalSet(`mw_session_ontology_${workspaceId}_${sessionId}`, selectedOntologyId);
        }
      }
      sendAgentic(text, sessionId, { agentMode, modelProfileId, selectedOntologyId });
      setInput('');
      if (isNew) generateSessionTitle(sessionId, text);
    } catch {
      setInput(text);
    }
  };
  const streamState = currentChatId ? streamManager.getStreamState(currentChatId) : { isLoading: false };
  const streamStatusLabel = {
    queued: `排队中${streamState.queue?.position ? ` · 第 ${streamState.queue.position} 位` : ''}`,
    reconnecting: '连接恢复中',
    streaming: '运行中',
    finalizing: '收尾中',
    awaiting: '等待输入',
    cancelled: '已停止',
    error: '连接异常',
  }[streamState.status] || '';
  const submitInteraction = (displayText, response) => {
    if (!pendingInteraction?.id || !currentChatId) return;
    sendAgentic(String(displayText || response?.value || ''), currentChatId, {
      agentMode,
      modelProfileId,
      selectedOntologyId,
      interactionResponse: {
        interaction_id: pendingInteraction.id,
        type: response?.type || pendingInteraction.type || 'input',
        value: response?.value ?? displayText,
        option_id: response?.option_id,
      },
    });
  };

  return (
    <div className="chatwrap" ref={wrapRef}>
      <div className="thread">
        <div className="th-head">
          <span className="tt">{current?.title || '新对话'}</span>
          {current && <span className="tm">{formatTime(current.updated_at || current.created_at)}</span>}
          {streamStatusLabel && <StatusTag status={streamState.status}>{streamStatusLabel}</StatusTag>}
          <button className="iconbtn" style={{ marginLeft: 'auto' }} title={traceOpen ? '收起 Trace' : '展开 Trace'}
            onClick={() => setTraceOpen((v) => !v)}>
            {traceOpen ? <PanelRightClose size={15} /> : <PanelRightOpen size={15} />}
          </button>
        </div>
        <div className="th-body" ref={bodyRef}>
          {messages.map((m) => <MessageBubble key={m.id} msg={m} />)}
          {!messages.length && (
            <div className="chat-empty">
              <div className="chat-empty-visual" aria-hidden="true">
                <span className="orbit orbit-a" /><span className="orbit orbit-b" />
                <img src="/brand-mark.png" alt="" />
              </div>
              <div className="chat-empty-kicker"><Sparkles size={13} />知识与数据已就绪</div>
              <h2>从一个业务问题开始</h2>
              <p>我会结合当前工作空间的知识包、数据连接和运行工具给出可追溯的回答。</p>
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
        <div className="th-input">
          <div className="chat-config-locks">
            <select value={modelProfileId} title="模型在会话创建后锁定"
              disabled={sessionLocked || streamState.isLoading || !modelProfiles.length}
              onChange={(event) => {
                setModelProfileId(event.target.value);
                safeLocalSet(`mw_ws_model_${workspaceId}`, event.target.value);
              }}>
              {!modelProfiles.length && <option value="">暂无可用模型</option>}
              {modelProfiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name || profile.id}</option>)}
            </select>
            <select value={selectedOntologyId} title="知识包在会话创建后锁定"
              disabled={sessionLocked || streamState.isLoading}
              onChange={(event) => setSelectedOntologyId(event.target.value)}>
              <option value="">自动选择知识包</option>
              {ontologies.map((ontology) => <option key={ontology.id} value={ontology.id}>{ontology.name || ontology.id}</option>)}
            </select>
            {sessionLocked && <span className="session-lock-note">🔒 会话配置已锁定</span>}
          </div>
          {pendingInteraction ? (
            <InteractionComposer interaction={pendingInteraction} disabled={streamState.isLoading} onSubmit={submitInteraction} />
          ) : (
            <div className="ti-box">
              <input className="ti-input" ref={inputRef} value={input} aria-label="对话内容" placeholder="输入问题，Enter 发送…"
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) send(); }} />
              <div className="ti-actions">
                <AgentModePicker value={agentMode} onChange={changeAgentMode} disabled={sessionLocked || streamState.isLoading} />
                {streamState.isLoading
                  ? <button type="button" className="btn" onClick={() => streamManager.stopStreamForSession(currentChatId)}>停止</button>
                  : <button type="button" className="btn btn-p" disabled={!modelProfileId || !input.trim()} onClick={send}>发送</button>}
              </div>
            </div>
          )}
        </div>
      </div>
      {traceOpen && (
        <>
          <button className="trace-backdrop" type="button" aria-label="关闭运行轨迹"
            onClick={() => setTraceOpen(false)} />
          <div className="trace-resizer" onMouseDown={startResize} title="拖拽调整 Trace 宽度" />
          <TracePanel messages={messages} width={traceWidth} onClose={() => setTraceOpen(false)} />
        </>
      )}
    </div>
  );
}

/* ---------------- Trace 竖栏（树形层级：轮次 → 思考/工具/子任务，可展开明细） ---------------- */

function TracePanel({ messages, width, onClose }) {
  const turns = messages.filter((m) => m.role === 'assistant');
  return (
    <div className="trace" style={width ? { width } : undefined}>
      <div className="tr-head">
        <span className="trace-title"><Activity size={14} />运行轨迹</span>
        <span className="trace-caption">Trace</span>
        <button className="iconbtn trace-close" type="button" aria-label="关闭运行轨迹" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      <div className="tr-body">
        {!turns.length && <div className="ch-empty" style={{ padding: '30px 12px' }}>发起对话后，这里按轮次展示模型思考、工具调用与状态明细，可逐层展开</div>}
        {turns.map((m, i) => (
          <TraceTurn key={m.id} msg={m} index={i + 1} defaultOpen={!!m.isLoading || i === turns.length - 1} />
        ))}
      </div>
    </div>
  );
}

// 从 parentKey（形如 iteration:main:3）解析出人类可读的分组标签。
function synthLabelForKey(key) {
  const m = /^iteration:(?:[^:]+:)*(\d+)$/.exec(String(key || ''));
  if (m) return `迭代 ${m[1]}`;
  if (String(key || '').startsWith('agent_loop')) return '主循环';
  return '分组';
}

// 把带 spanKey/parentKey 的 segment 组织成父子树。
// 若 parentKey 指向的父 span 本身没有对应 segment（如 iteration 不单独成段），
// 则合成一个分组父节点把子节点挂进去，从而形成「迭代 N → 工具」的层级；
// 完全没有 spanKey 时降级为平铺列表。
function buildTraceTree(segs) {
  const hasSpan = segs.some((s) => s.spanKey);
  if (!hasSpan) return segs.map((seg) => ({ seg, children: [] }));

  const bySpan = new Map();
  const nodes = segs.map((seg) => {
    const node = { seg, children: [] };
    if (seg.spanKey) bySpan.set(seg.spanKey, node);
    return node;
  });

  const synthByKey = new Map();
  const roots = [];
  const ensureSynthParent = (parentKey) => {
    if (synthByKey.has(parentKey)) return synthByKey.get(parentKey);
    const synth = {
      seg: { type: 'group', synthetic: true, spanKey: parentKey, label: synthLabelForKey(parentKey) },
      children: [],
    };
    synthByKey.set(parentKey, synth);
    roots.push(synth); // 合成的分组节点作为根
    return synth;
  };

  nodes.forEach((node) => {
    const parentKey = node.seg.parentKey;
    if (!parentKey) { roots.push(node); return; }
    const realParent = bySpan.get(parentKey);
    if (realParent && realParent !== node) { realParent.children.push(node); return; }
    ensureSynthParent(parentKey).children.push(node); // 父 span 无对应 segment → 合成分组
  });
  return roots;
}

function TraceTreeNode({ node }) {
  if (!node.children.length) return <TraceNode seg={node.seg} />;
  return (
    <div className="trn-branch">
      <TraceNode seg={node.seg} />
      <div className="trn-children" style={{ marginLeft: 14, borderLeft: '1px solid var(--line)', paddingLeft: 8 }}>
        {node.children.map((child, i) => <TraceTreeNode key={child.seg.spanKey || i} node={child} />)}
      </div>
    </div>
  );
}

function TraceTurn({ msg, index, defaultOpen }) {
  const segs = (msg.streamSegments || []).filter((s) => s.type !== 'content');
  const running = !!msg.isLoading;
  const status = running ? 'running' : (msg.status || 'finished');
  const statusText = { running: '进行中', finished: '成功', error: '出错', cancelled: '已停止', awaiting: '等待输入' }[status] || status;
  const toolCount = segs.filter((s) => s.type === 'tool').length;
  const tree = buildTraceTree(segs);
  return (
    <details className="trt" open={defaultOpen}>
      <summary>
        <span className="car">▶</span>
        <span className={`tr-dot ${status}`} />
        <span className="trt-t">第 {index} 轮</span>
        <span className="trt-s">{statusText}{toolCount ? ` · ${toolCount} 次工具` : ''}{msg.ts ? ` · ${formatTime(msg.ts)}` : ''}</span>
      </summary>
      <div className="trt-kids">
        {!segs.length && <div className="trn-plain">直接回答，未调用工具</div>}
        {tree.map((node, i) => <TraceTreeNode key={node.seg.spanKey || i} node={node} />)}
        {msg.runId && <div className="tr-run" title={String(msg.runId)}>run · {String(msg.runId).slice(0, 26)}…</div>}
      </div>
    </details>
  );
}

function TraceNode({ seg }) {
  // 合成的分组节点（如「迭代 N」）：只作层级标题，无明细
  if (seg.type === 'group' && seg.synthetic) {
    return (
      <div className="trn-plain trn-group" title={seg.label}>
        <span className="tr-ic"><GitBranch size={11} /></span>
        <span className="tr-lb">{seg.label}</span>
      </div>
    );
  }
  const done = seg.finished || seg.state === 'done' || seg.state === 'final';
  const meta = seg.meta || {};
  const reasoningSummary = seg.type === 'reasoning'
    ? String(meta.reasoning_summary || '').trim()
    : '';
  let Icon = Circle;
  let label = '';
  if (seg.type === 'tool') { Icon = Wrench; label = meta.tool_name || '工具调用'; }
  else if (seg.type === 'reasoning') {
    Icon = BrainCircuit;
    // 有摘要时用摘要做标签（一等公民：一眼看到「想了什么」），否则回退通用文案
    label = reasoningSummary ? `思考：${truncate(reasoningSummary, 60)}` : '模型思考';
  }
  else if (seg.type === 'subagent' || seg.type === 'subagent_group') { Icon = GitBranch; label = meta.title || '子任务'; }
  else { label = seg.type; }
  const elapsed = meta.elapsed_ms ? `${(meta.elapsed_ms / 1000).toFixed(1)}s` : '';
  const stateMark = elapsed || (done ? '✓' : '⋯');
  const hasError = meta.error || meta.status === 'error';
  const sourcePath = meta.source_path || meta.file_path || meta.source_file || meta.source || meta.path;
  const sourceOntologyId = meta.ontology_id || meta.ontologyId || '';
  const canOpenSource = typeof sourcePath === 'string' && /^(objects|operations|assets)\//.test(sourcePath);
  const openSource = (event) => {
    event.preventDefault();
    event.stopPropagation();
    window.dispatchEvent(new CustomEvent('mwr-open-kb-source', {
      detail: { ontologyId: sourceOntologyId, path: sourcePath },
    }));
  };

  // 可展开的明细：工具的输入参数 / 输出预览 / 错误，思考与子任务的文本
  const detailBlocks = [];
  if (seg.type === 'tool') {
    if (meta.input && Object.keys(meta.input).length) {
      detailBlocks.push(['输入', safeJson(meta.input)]);
    }
    if (meta.output_preview) {
      detailBlocks.push([`输出${meta.output_truncated ? '（截断）' : ''}`, String(meta.output_preview)]);
    }
    if (meta.error) detailBlocks.push(['错误', String(meta.error)]);
    if (!detailBlocks.length && seg.text) detailBlocks.push(['进度', String(seg.text)]);
  } else if (seg.type === 'reasoning') {
    // reasoning 一等公民：摘要在前，完整思考链在后，二者都可见
    if (reasoningSummary) detailBlocks.push(['思考摘要', reasoningSummary]);
    if (seg.text) detailBlocks.push(['完整思考过程', String(seg.text)]);
  } else if (seg.text) {
    detailBlocks.push(['内容', String(seg.text)]);
  }

  if (!detailBlocks.length) {
    return (
      <div className="trn-plain" title={label}>
        <span className="tr-ic"><Icon size={11} /></span>
        <span className="tr-lb">{label}</span>
        {canOpenSource && <button type="button" className="tr-source-link" onClick={openSource}>打开来源</button>}
        <span className="tr-st">{stateMark}</span>
      </div>
    );
  }
  return (
    <details className="trn">
      <summary>
        <span className="car">▶</span>
        <span className="tr-ic"><Icon size={11} /></span>
        <span className={`tr-lb ${hasError ? 'err' : ''}`}>{label}</span>
        <span className="tr-st">{stateMark}</span>
      </summary>
      <div className="trn-detail">
        {detailBlocks.map(([title, text]) => (
          <div key={title} className="trn-block">
            <div className="trn-bt">{title}</div>
            <pre className="trn-pre">{truncate(text, 800)}</pre>
          </div>
        ))}
        {canOpenSource && <button type="button" className="btn tr-source-link" onClick={openSource}>在知识包中打开 {sourcePath}</button>}
      </div>
    </details>
  );
}

function safeJson(v) {
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}

function InteractionComposer({ interaction, disabled, onSubmit }) {
  const [draft, setDraft] = useState('');
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
          if (!value) return;
          onSubmit(value, { type, value });
          setDraft('');
        }}>
          <input value={draft} disabled={disabled} autoFocus placeholder="补充信息…"
            onChange={(event) => setDraft(event.target.value)} />
          <button type="submit" className="btn btn-p" disabled={disabled || !draft.trim()}>继续</button>
        </form>
      ) : (
        <div className="mwr-interaction-options">
          {normalizedOptions.map((option) => (
            <button key={option.id} type="button" className="btn" disabled={disabled}
              onClick={() => onSubmit(option.label, {
                type,
                value: option.value,
                option_id: option.id,
              })}>
              <strong>{option.label}</strong>
              {option.description && <span>{option.description}</span>}
            </button>
          ))}
        </div>
      )}
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
              {toolNames.map((t) => <span key={t} className="refchip"><Wrench size={11} />{t}</span>)}
            </div>
          )}
          {stepSegs.length > 0 && (
            <details className="steps" open={running}>
              <summary><span className="car">▶</span>执行过程 · {stepSegs.length} 步{running ? ' · 进行中' : ''}</summary>
              {stepSegs.map((seg, i) => <StepRow key={seg.spanKey || `${seg.type}-${i}`} seg={seg} />)}
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
          {!running && (msg.status === 'finished' || isError || msg.status === 'cancelled' || msg.status === 'aborted' || msg.status === 'awaiting') && (
            <div className="statusline">
              {isError ? '✗ 出错' : ['cancelled', 'aborted'].includes(msg.status) ? '⏹ 已停止' : msg.status === 'awaiting' ? '⏸ 等待输入' : '✓ 已完成'}
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
  const isReasoning = seg.type === 'reasoning';
  let label = '';
  if (seg.type === 'tool') label = `${seg.meta?.tool_name ? `工具 ${seg.meta.tool_name}` : '工具调用'}${seg.text ? ` — ${truncate(seg.text, 120)}` : ''}`;
  else if (isReasoning) {
    // 优先展示思考摘要（reasoning_summary），无摘要再回退原始思考文本
    const summary = String(seg.meta?.reasoning_summary || '').trim();
    label = `思考：${truncate(summary || seg.text || '', 120)}`;
  }
  else if (seg.type === 'subagent' || seg.type === 'subagent_group') label = `子任务：${truncate(seg.text || seg.meta?.title || '', 120)}`;
  else label = truncate(seg.text || seg.type, 120);
  return (
    <div className={`step${isReasoning ? ' step-reasoning' : ''}`}>
      <span className="ok">{isReasoning ? <BrainCircuit size={13} /> : (done ? <Check size={13} /> : '…')}</span>
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
