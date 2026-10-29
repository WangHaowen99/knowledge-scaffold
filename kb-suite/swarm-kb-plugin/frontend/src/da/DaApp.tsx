import {
  Bot, Check, ChevronDown, ChevronRight, Compass, GitCompare, GitFork, History, Layers, Library, ListChecks,
  MessageCircleQuestion, Plus, Quote, ScanSearch, Search, Send, Sparkles, Square, Trash2, TriangleAlert, Wrench,
} from 'lucide-react';
import { useEffect, useMemo, useReducer, useRef, useState, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { formatTime } from '../format';
import { ToastProvider, useToast } from '../toast';
import { Badge, Button, Spinner } from '../ui';
import { channel, type Wire } from './channel';

type Kb = { id: string; name: string; documents: number };
type Engine = { id: string; label: string; ready: boolean };
type Tool = { id: string; name: string; arguments?: unknown; result?: unknown; done: boolean };
type Ask = {
  request_id: string;
  kind: 'question' | 'approval';
  prompt: string;
  choices?: string[];
  tool_name?: string;
  arguments?: unknown;
  answered?: string;
};
type Turn = {
  client_id: string;
  turn_id?: string;
  engine: string;
  input: string;
  steers: string[];
  outputs: { id: string; channel: string; text: string }[];
  tools: Tool[];
  asks: Ask[];
  status: 'queued' | 'running' | 'finished' | 'aborted' | 'failed';
  final_output?: string;
  error?: string;
  duration_ms?: number;
  tokens?: number;
};

type HistoryItem = {
  id: string;
  engine: string;
  title: string;
  updated_at: number;
  turns: number;
  running: boolean;
};

type State = { turns: Turn[]; running: boolean };
type Action =
  | { type: 'replay'; events: Wire[]; live: boolean; running: boolean; engineLabel: string }
  | { type: 'local'; turn: Turn }
  | { type: 'accepted'; client_id: string; turn_id: string }
  | { type: 'rejected'; client_id: string; message: string }
  | { type: 'steer'; text: string }
  | { type: 'answered'; request_id: string; label: string }
  | { type: 'wire'; msg: Wire }
  | { type: 'clear' };

const KB_KEY = 'da-analyst.kb-ids';
const ENGINE_KEY = 'da-analyst.engine';
const HISTORY_OPEN_KEY = 'da-analyst.history-open';
const DEFAULT_ENGINES: Engine[] = [{ id: 'deepanalyze', label: 'DeepAnalyze', ready: false }];
// labels for history entries of engines this server no longer offers
const ENGINE_LABELS: Record<string, string> = { deepanalyze: 'DeepAnalyze', loopforest: 'Loop Forest' };
// each engine keeps its own conversation (a different provider and session); the
// server keeps it running across page reloads, so remember it beyond this tab
const sessionKey = (engine: string) => `da-analyst.host-session.${engine}`;
// beyond this many knowledge bases the scope row folds the rest behind "+N"
const KB_FOLD = 6;

type Example = { icon: ReactNode; title: string; text: string };
const CAPS: { icon: ReactNode; label: string }[] = [
  { icon: <Search size={12} />, label: '多轮检索' },
  { icon: <GitCompare size={12} />, label: '交叉核对' },
  { icon: <MessageCircleQuestion size={12} />, label: '主动追问' },
  { icon: <Quote size={12} />, label: '引用溯源' },
];
const DA_EXAMPLES: Example[] = [
  { icon: <GitCompare size={14} />, title: '对比差异', text: '对比几份文档里对同一参数的规定，列出不一致的地方并注明出处' },
  { icon: <ListChecks size={14} />, title: '梳理流程', text: '把某项操作的步骤整理成检查清单，每一步写明合格标准和依据' },
  { icon: <TriangleAlert size={14} />, title: '故障归因', text: '某个异常现象可能由哪些原因引起？分别对应哪些处置措施？' },
];
const LF_EXAMPLES: Example[] = [
  { icon: <GitFork size={14} />, title: '多角度查证', text: '从设计要求、操作规程和历史记录三个角度核对同一个结论是否成立' },
  { icon: <ListChecks size={14} />, title: '汇总清单', text: '把知识库里所有提到的定期检查项汇总成一张表，按周期分组' },
  { icon: <TriangleAlert size={14} />, title: '风险排查', text: '列出文档中提到的高风险操作，以及每项对应的防护要求' },
];

// Turn events can reach the page before the `accepted` reply that carries the
// turn id; such events belong to the newest turn still waiting for its id.
function patchTurn(turns: Turn[], turnId: unknown, fn: (t: Turn) => Turn): Turn[] {
  if (!turnId) return turns;
  if (turns.some((t) => t.turn_id === turnId)) return turns.map((t) => (t.turn_id === turnId ? fn(t) : t));
  const pending = turns.map((t) => !t.turn_id).lastIndexOf(true);
  if (pending < 0) return turns;
  return turns.map((t, i) => (i === pending ? fn({ ...t, turn_id: String(turnId) }) : t));
}

// Rebuild a conversation from the server transcript: the page's own inputs
// (user / steer / answered / rejected) plus the recorded wire messages.
function replay(action: Extract<Action, { type: 'replay' }>): State {
  let state: State = { turns: [], running: false };
  for (const e of action.events) {
    if (e.t === 'user') {
      state = reducer(state, {
        type: 'local',
        turn: {
          client_id: String(e.client_id || e.turn_id), turn_id: e.turn_id ? String(e.turn_id) : undefined,
          engine: action.engineLabel, input: String(e.text ?? ''), steers: [], outputs: [], tools: [], asks: [],
          status: 'queued',
        },
      });
    } else if (e.t === 'rejected') {
      state = reducer(state, { type: 'rejected', client_id: String(e.client_id), message: String(e.message ?? '') });
    } else if (e.t === 'steer') {
      state = reducer(state, { type: 'steer', text: String(e.text ?? '') });
    } else if (e.t === 'answered') {
      state = reducer(state, { type: 'answered', request_id: String(e.request_id), label: String(e.label || '已回答') });
    } else {
      state = reducer(state, { type: 'wire', msg: e });
    }
  }
  state = { ...state, running: action.running };
  if (action.live) return state; // the live event stream settles any open turn
  // the harness behind this transcript is gone: a turn without its end event was cut off
  return {
    ...state,
    turns: state.turns.map((t) =>
      t.status === 'running' || t.status === 'queued'
        ? { ...t, status: 'aborted', error: t.error ?? '分析已中断（会话在执行中被关闭）' }
        : t,
    ),
  };
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'replay':
      return replay(action);
    case 'clear':
      return { turns: [], running: false };
    case 'local':
      return { ...state, turns: [...state.turns, action.turn] };
    case 'accepted':
      return {
        ...state,
        turns: state.turns.map((t) => (t.client_id === action.client_id ? { ...t, turn_id: action.turn_id } : t)),
      };
    case 'rejected':
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.client_id === action.client_id ? { ...t, status: 'failed', error: action.message } : t,
        ),
      };
    case 'steer': {
      const target = [...state.turns].reverse().find((t) => t.status === 'running' || t.status === 'queued');
      if (!target) return state;
      return {
        ...state,
        turns: state.turns.map((t) => (t === target ? { ...t, steers: [...t.steers, action.text] } : t)),
      };
    }
    case 'answered':
      return {
        ...state,
        turns: state.turns.map((t) => ({
          ...t,
          asks: t.asks.map((a) => (a.request_id === action.request_id ? { ...a, answered: action.label } : a)),
        })),
      };
    case 'wire': {
      const m = action.msg;
      if (m.t === 'state') return { ...state, running: m.state === 'running' };
      if (m.t === 'output') {
        return {
          ...state,
          turns: patchTurn(state.turns, m.turn_id, (t) => {
            const outputs = [...t.outputs];
            const idx = outputs.findIndex((o) => o.id === m.output_id);
            const text = String(m.content ?? '');
            if (idx < 0) outputs.push({ id: String(m.output_id), channel: String(m.channel), text });
            else outputs[idx] = { ...outputs[idx], text: m.op === 'delta' ? outputs[idx].text + text : text };
            return { ...t, outputs };
          }),
        };
      }
      if (m.t === 'tool') {
        return {
          ...state,
          turns: patchTurn(state.turns, m.turn_id, (t) => {
            const id = String(m.item_id);
            const existing = t.tools.find((x) => x.id === id);
            const tools = existing
              ? t.tools.map((x) => (x.id === id ? { ...x, result: m.result, done: m.phase === 'completed' } : x))
              : [...t.tools, { id, name: String(m.name ?? ''), arguments: m.arguments, done: m.phase === 'completed' }];
            return { ...t, tools };
          }),
        };
      }
      if (m.t === 'turn') {
        return {
          ...state,
          turns: patchTurn(state.turns, m.turn_id, (t) =>
            m.kind === 'started' || m.kind === 'paused' || m.kind === 'resumed'
              ? { ...t, status: 'running' }
              : {
                  ...t,
                  status: (m.kind as Turn['status']) ?? 'finished',
                  final_output: (m.final_output as string) ?? t.final_output,
                  error: (m.error as { message?: string } | null)?.message ?? undefined,
                  duration_ms: m.duration_ms as number,
                  tokens: (m.total_tokens as number) ?? undefined,
                  asks: t.asks.map((a) => (a.answered ? a : { ...a, answered: '（已结束）' })),
                },
          ),
        };
      }
      if (m.t === 'ask') {
        return {
          ...state,
          turns: patchTurn(state.turns, m.turn_id, (t) => ({
            ...t,
            asks: [...t.asks, m as unknown as Ask],
          })),
        };
      }
      if (m.t === 'ask_cancelled') {
        return {
          ...state,
          turns: state.turns.map((t) => ({
            ...t,
            asks: t.asks.map((a) => (a.request_id === m.request_id && !a.answered ? { ...a, answered: '（已撤回）' } : a)),
          })),
        };
      }
      return state;
    }
    default:
      return state;
  }
}

function json(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function ToolCard({ tool }: { tool: Tool }) {
  const [open, setOpen] = useState(false);
  const args = json(tool.arguments);
  return (
    <div className="da-tool">
      <button className="da-tool-head" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <Wrench size={13} />
        <span className="da-tool-name">{tool.name}</span>
        <span className="da-tool-args">{args.replace(/\s+/g, ' ').slice(0, 90)}</span>
        {tool.done ? <Badge tone="success">完成</Badge> : <Spinner size={10} />}
      </button>
      {open ? (
        <div className="da-tool-body">
          <div className="da-label">参数</div>
          <pre>{args || '—'}</pre>
          <div className="da-label">结果</div>
          <pre>{json(tool.result).slice(0, 6000) || '—'}</pre>
        </div>
      ) : null}
    </div>
  );
}

function AskCard({ ask, onAnswer }: { ask: Ask; onAnswer: (payload: Record<string, unknown>, label: string) => void }) {
  const [text, setText] = useState('');
  if (ask.answered) {
    return (
      <div className="da-ask da-ask-done">
        <div className="da-ask-q">{ask.kind === 'approval' ? `审批 ${ask.tool_name}` : ask.prompt}</div>
        <div className="muted">回复：{ask.answered}</div>
      </div>
    );
  }
  if (ask.kind === 'approval') {
    return (
      <div className="da-ask">
        <div className="da-ask-q">DA 请求执行工具 <b>{ask.tool_name}</b>{ask.prompt ? `：${ask.prompt}` : ''}</div>
        {ask.arguments ? <pre className="da-ask-args">{json(ask.arguments).slice(0, 1500)}</pre> : null}
        <div className="da-ask-actions">
          <Button size="sm" variant="primary" onClick={() => onAnswer({ decision: 'allow' }, '允许')}>允许</Button>
          <Button size="sm" onClick={() => onAnswer({ decision: 'deny' }, '拒绝')}>拒绝</Button>
          <Button size="sm" variant="danger" onClick={() => onAnswer({ decision: 'abort' }, '终止任务')}>终止任务</Button>
        </div>
      </div>
    );
  }
  return (
    <div className="da-ask">
      <div className="da-ask-q">{ask.prompt}</div>
      {ask.choices?.length ? (
        <div className="da-ask-actions">
          {ask.choices.map((c) => (
            <Button key={c} size="sm" onClick={() => onAnswer({ content: c }, c)}>{c}</Button>
          ))}
        </div>
      ) : null}
      <div className="da-ask-input">
        <input
          className="input"
          placeholder="输入回答"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && text.trim() && onAnswer({ content: text.trim() }, text.trim())}
        />
        <Button size="sm" variant="primary" disabled={!text.trim()} onClick={() => onAnswer({ content: text.trim() }, text.trim())}>
          回答
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onAnswer({ decline: true }, '（拒绝回答）')}>不回答</Button>
      </div>
    </div>
  );
}

function TurnView({ turn, onAnswer }: { turn: Turn; onAnswer: (ask: Ask, payload: Record<string, unknown>, label: string) => void }) {
  const [showReasoning, setShowReasoning] = useState(false);
  const reasoning = turn.outputs.filter((o) => o.channel === 'reasoning').map((o) => o.text).join('\n');
  const answerParts = turn.outputs.filter((o) => o.channel === 'answer').map((o) => o.text);
  const answer = answerParts.join('\n\n') || turn.final_output || '';
  return (
    <div className="da-turn">
      <div className="da-user">{turn.input}</div>
      {turn.steers.map((s, i) => (
        <div key={i} className="da-steer">补充指令：{s}</div>
      ))}
      <div className="da-assistant">
        <div className="da-assistant-head">
          <Bot size={15} />
          <span>{turn.engine}</span>
          {turn.status === 'running' || turn.status === 'queued' ? (
            <Badge tone="info"><Spinner size={10} />{turn.status === 'queued' ? '排队中' : '分析中'}</Badge>
          ) : turn.status === 'finished' ? (
            <Badge tone="success">完成</Badge>
          ) : turn.status === 'aborted' ? (
            <Badge tone="warning">已停止</Badge>
          ) : (
            <Badge tone="danger">失败</Badge>
          )}
          {turn.duration_ms ? <span className="muted">{(turn.duration_ms / 1000).toFixed(1)}s</span> : null}
          {turn.tokens ? <span className="muted">{turn.tokens} tokens</span> : null}
        </div>
        {reasoning ? (
          <div className="da-reasoning">
            <button className="link-btn" onClick={() => setShowReasoning(!showReasoning)}>
              {showReasoning ? '收起思考过程' : '展开思考过程'}
            </button>
            {showReasoning ? <div className="da-reasoning-text">{reasoning}</div> : null}
          </div>
        ) : null}
        {turn.tools.map((tool) => <ToolCard key={tool.id} tool={tool} />)}
        {turn.asks.map((ask) => (
          <AskCard key={ask.request_id} ask={ask} onAnswer={(payload, label) => onAnswer(ask, payload, label)} />
        ))}
        {answer ? (
          <div className="markdown da-answer">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown>
          </div>
        ) : null}
        {turn.error ? <div className="error-box">{turn.error}</div> : null}
      </div>
    </div>
  );
}

const engineIcon = (id: string) => (id === 'loopforest' ? <GitFork size={14} /> : <ScanSearch size={14} />);

function EnginePicker({ engines, value, onChange }: { engines: Engine[]; value: string; onChange: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const current = engines.find((e) => e.id === value) ?? engines[0];

  useEffect(() => {
    if (!open) return undefined;
    const onPointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const pick = (id: string) => {
    setOpen(false);
    onChange(id);
  };

  return (
    <div className="da-picker" ref={rootRef}>
      <button
        type="button"
        className="da-picker-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="分析引擎"
        title={current.ready ? current.label : `${current.label} 未就绪`}
        onClick={() => setOpen(!open)}
      >
        {engineIcon(current.id)}
        <span className="da-picker-label">{current.label}</span>
        <span className={`da-engine-dot ${current.ready ? '' : 'da-engine-dot-off'}`} />
        <ChevronDown size={14} className={`da-picker-caret ${open ? 'da-picker-caret-open' : ''}`} />
      </button>
      {open ? (
        <ul className="da-picker-menu" role="listbox" aria-label="分析引擎">
          {engines.map((e) => (
            <li
              key={e.id}
              role="option"
              aria-selected={e.id === current.id}
              tabIndex={0}
              className={e.id === current.id ? 'da-picker-on' : ''}
              onClick={() => pick(e.id)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  pick(e.id);
                }
              }}
            >
              {engineIcon(e.id)}
              <span className="da-picker-label">{e.label}</span>
              <span className="da-picker-state">
                <span className={`da-engine-dot ${e.ready ? '' : 'da-engine-dot-off'}`} />
                {e.ready ? '就绪' : '未就绪'}
              </span>
              {e.id === current.id ? <Check size={14} className="da-picker-check" /> : <span className="da-picker-check" />}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function Shell() {
  const notify = useToast();
  const [state, dispatch] = useReducer(reducer, { turns: [], running: false });
  const [kbs, setKbs] = useState<Kb[]>([]);
  const [selected, setSelected] = useState<string[]>(() => JSON.parse(localStorage.getItem(KB_KEY) || '[]'));
  const [connected, setConnected] = useState(channel.connected);
  const [engines, setEngines] = useState<Engine[]>(DEFAULT_ENGINES);
  const enginesRef = useRef(engines);
  enginesRef.current = engines;
  const [engine, setEngine] = useState<string>(() => localStorage.getItem(ENGINE_KEY) || 'deepanalyze');
  const engineRef = useRef(engine);
  engineRef.current = engine;
  const [started, setStarted] = useState(false);
  const [sessionId, setSessionId] = useState('');
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [historyOpen, setHistoryOpen] = useState(() => localStorage.getItem(HISTORY_OPEN_KEY) !== '0');
  const [text, setText] = useState('');
  const [showAllKbs, setShowAllKbs] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  useEffect(() => {
    const off = channel.onStatus(setConnected);
    // the socket opens on module load and may have connected before this effect ran
    setConnected(channel.connected);
    return off;
  }, []);

  useEffect(() => {
    const off = channel.on((msg) => {
      if (msg.t === 'hello') {
        const list = (msg.kbs as Kb[]) ?? [];
        setKbs(list);
        const offered = (msg.engines as Engine[] | undefined)?.length
          ? (msg.engines as Engine[])
          : [{ ...DEFAULT_ENGINES[0], ready: Boolean(msg.da_ready) }];
        setEngines(offered);
        const chosen = offered.some((e) => e.id === engineRef.current) ? engineRef.current : offered[0].id;
        setEngine(chosen);
        let ids = selectedRef.current.filter((id) => list.some((k) => k.id === id));
        if (!ids.length && list.length) ids = list.map((k) => k.id);
        setSelected(ids);
        channel.send({
          type: 'start', engine: chosen, host_session_id: localStorage.getItem(sessionKey(chosen)) || '', kb_ids: ids,
        });
      } else if (msg.t === 'started') {
        const opened = String(msg.engine || engineRef.current);
        localStorage.setItem(sessionKey(opened), String(msg.host_session_id));
        localStorage.setItem(ENGINE_KEY, opened);
        setEngine(opened);
        setSessionId(String(msg.host_session_id));
        const label = enginesRef.current.find((e) => e.id === opened)?.label ?? ENGINE_LABELS[opened] ?? opened;
        dispatch({
          type: 'replay', events: (msg.transcript as Wire[]) ?? [], live: Boolean(msg.live), running: Boolean(msg.running),
          engineLabel: label,
        });
        const kbIds = (msg.kb_ids as string[] | undefined) ?? [];
        if (kbIds.length) setSelected(kbIds);
        setStarted(true);
        channel.send({ type: 'history' });
      } else if (msg.t === 'history') {
        setHistory((msg.items as HistoryItem[]) ?? []);
      } else if (msg.t === 'accepted') {
        if (msg.mode !== 'steer') dispatch({ type: 'accepted', client_id: String(msg.client_id), turn_id: String(msg.turn_id) });
      } else if (msg.t === 'error') {
        notify(String(msg.message), true);
        if (msg.client_id) dispatch({ type: 'rejected', client_id: String(msg.client_id), message: String(msg.message) });
      } else {
        dispatch({ type: 'wire', msg });
        // a finished turn changes the history entry (title, time, running mark)
        if (msg.t === 'turn' && msg.kind !== 'started' && msg.kind !== 'paused' && msg.kind !== 'resumed') {
          channel.send({ type: 'history' });
        }
      }
    });
    return off;
  }, [notify]);

  useEffect(() => {
    if (connected) channel.send({ type: 'hello' });
    else setStarted(false);
  }, [connected]);

  useEffect(() => {
    localStorage.setItem(KB_KEY, JSON.stringify(selected));
  }, [selected]);

  useEffect(() => {
    localStorage.setItem(HISTORY_OPEN_KEY, historyOpen ? '1' : '0');
  }, [historyOpen]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' });
  }, [state.turns]);

  const running = state.running || state.turns.some((t) => t.status === 'running' || t.status === 'queued');
  const current = engines.find((e) => e.id === engine) ?? engines[0];
  const engineLabel = current?.label ?? 'DeepAnalyze';

  const submit = () => {
    const value = text.trim();
    if (!value || !started) return;
    if (running) {
      channel.send({ type: 'send', text: value, steer: true });
      dispatch({ type: 'steer', text: value });
    } else {
      const client_id = `c-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
      dispatch({
        type: 'local',
        turn: {
          client_id, engine: engineLabel, input: value, steers: [], outputs: [], tools: [], asks: [], status: 'queued',
        },
      });
      channel.send({ type: 'send', text: value, kb_ids: selected, client_id });
    }
    setText('');
  };

  // Leaving a conversation does not stop it: a running analysis finishes on the
  // server and stays in the history list.
  const open = (next: string, hostSessionId: string) => {
    if (started && next === engine && hostSessionId && hostSessionId === sessionId) return;
    setEngine(next);
    localStorage.setItem(ENGINE_KEY, next);
    dispatch({ type: 'clear' });
    setStarted(false);
    channel.send({ type: 'start', engine: next, host_session_id: hostSessionId, kb_ids: selected });
  };

  const newConversation = () => {
    localStorage.removeItem(sessionKey(engine));
    open(engine, '');
  };

  const switchEngine = (next: string) => {
    if (next !== engine) open(next, localStorage.getItem(sessionKey(next)) || '');
  };

  const removeHistory = (item: HistoryItem) => {
    if (!window.confirm(`删除对话「${item.title || '未命名对话'}」？${item.running ? '正在进行的分析会被停止，' : ''}删除后不可恢复。`)) return;
    channel.send({ type: 'delete', host_session_id: item.id });
    if (item.id === sessionId) newConversation();
  };

  const toggleKb = (id: string) =>
    setSelected((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));

  const scopeText = useMemo(() => {
    if (!kbs.length) return '没有可用的知识库';
    return selected.length ? `${selected.length} 个知识库` : '全部知识库';
  }, [kbs, selected]);

  const kbNames = kbs.filter((kb) => selected.includes(kb.id)).map((kb) => kb.name);
  // selected knowledge bases stay visible when the list is folded
  const shownKbs = showAllKbs || kbs.length <= KB_FOLD
    ? kbs
    : [...kbs.filter((kb) => selected.includes(kb.id)), ...kbs.filter((kb) => !selected.includes(kb.id))].slice(0, KB_FOLD);
  const status = !connected
    ? { tone: 'off', text: '正在连接…' }
    : current?.ready
      ? { tone: 'on', text: `${engineLabel} 已连接` }
      : { tone: 'warn', text: `${engineLabel} 未就绪` };
  const examples = engine === 'loopforest' ? LF_EXAMPLES : DA_EXAMPLES;

  return (
    <div className="da-layout">
      {historyOpen ? (
        <aside className="da-history">
          <div className="da-history-head">历史对话</div>
          {history.length === 0 ? (
            <div className="da-history-empty muted">还没有对话</div>
          ) : (
            history.map((item) => (
              <div
                key={item.id}
                className={`da-history-item ${item.id === sessionId ? 'da-history-on' : ''}`}
                onClick={() => open(item.engine, item.id)}
                title={item.title}
              >
                <div className="da-history-title">{item.title || '未命名对话'}</div>
                <div className="da-history-meta">
                  <span>{ENGINE_LABELS[item.engine] ?? item.engine}</span>
                  <span>{formatTime(new Date(item.updated_at * 1000).toISOString())}</span>
                  {item.running ? <Badge tone="info"><Spinner size={9} />分析中</Badge> : null}
                </div>
                <button
                  className="icon-btn da-history-del"
                  title="删除"
                  onClick={(e) => {
                    e.stopPropagation();
                    removeHistory(item);
                  }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))
          )}
        </aside>
      ) : null}
      <div className="da-app">
        <header className="da-header">
          <div className="da-brand">
            <div className="da-logo"><Compass size={20} /></div>
            <div>
              <div className="da-title">知识求索</div>
              <div className="da-sub">
                <span className={`da-status da-status-${status.tone}`}><i />{status.text}</span>
                <span>在知识库上做多步检索、核对与归纳</span>
              </div>
            </div>
          </div>
          <div className="da-header-actions">
            {engines.length > 1 ? <EnginePicker engines={engines} value={engine} onChange={switchEngine} /> : null}
            <Button
              icon={<History size={14} />}
              variant={historyOpen ? 'secondary' : 'ghost'}
              onClick={() => setHistoryOpen(!historyOpen)}
              title={historyOpen ? '收起历史对话' : '展开历史对话'}
            >
              历史
            </Button>
            <Button icon={<Plus size={14} />} onClick={newConversation}>新对话</Button>
          </div>
        </header>
        <div className="da-scope">
          <span className="da-scope-label">
            <Library size={14} />
            检索范围 <b>{scopeText}</b>
          </span>
          {kbs.length ? (
            <button
              className={`da-chip da-chip-all ${selected.length === 0 ? 'da-chip-on' : ''}`}
              onClick={() => setSelected([])}
            >
              <span className="da-chip-check"><Layers size={11} /></span>
              全部
            </button>
          ) : null}
          {shownKbs.map((kb) => (
            <button
              key={kb.id}
              className={`da-chip ${selected.includes(kb.id) ? 'da-chip-on' : ''}`}
              onClick={() => toggleKb(kb.id)}
              title={`${kb.documents} 个已入库文档`}
            >
              <span className="da-chip-check"><Check size={11} /></span>
              {kb.name}
              <span className="da-chip-count">{kb.documents}</span>
            </button>
          ))}
          {kbs.length > KB_FOLD ? (
            <button className="da-chip da-chip-more" onClick={() => setShowAllKbs(!showAllKbs)}>
              {showAllKbs ? '收起' : `+${kbs.length - KB_FOLD}`}
            </button>
          ) : null}
          <span className="da-scope-hint">不选 = 检索全部知识库</span>
        </div>
        <main className={`da-thread ${state.turns.length ? '' : 'da-thread-empty'}`}>
          {state.turns.length === 0 ? (
            <div className="da-hero">
              <div className="da-hero-icon">
                <div className="da-hero-ring" />
                <div className="da-hero-core"><Bot size={34} /></div>
                <div className="da-hero-spark"><Sparkles size={13} /></div>
              </div>
              <h2>向 {engineLabel} 提一个分析问题</h2>
              <p>
                {engine === 'loopforest'
                  ? 'Loop Forest 会检索知识库，必要时把问题拆成分支并行查证再汇总。'
                  : 'DA 会检索、展开、交叉核对知识库里的文档后作答，必要时会向你追问。'}
                <br />
                执行中可以继续输入补充指令，或随时停止。
              </p>
              <div className="da-caps">
                {CAPS.map(({ icon, label }) => (
                  <span key={label} className="da-cap">{icon}{label}</span>
                ))}
              </div>
              <div className="da-examples">
                {examples.map((ex) => (
                  <button key={ex.title} className="da-example" onClick={() => setText(ex.text)}>
                    <span className="da-example-title">{ex.icon}{ex.title}</span>
                    {ex.text}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            state.turns.map((turn) => (
              <TurnView
                key={turn.client_id}
                turn={turn}
                onAnswer={(ask, payload, label) => {
                  channel.send({ type: 'answer', request_id: ask.request_id, label, ...payload });
                  dispatch({ type: 'answered', request_id: ask.request_id, label });
                }}
              />
            ))
          )}
          <div ref={bottom} />
        </main>
        <footer className="da-composer">
          <textarea
            className="textarea"
            placeholder={running ? '输入补充指令，发送后注入到正在执行的分析' : '输入分析问题，Enter 发送，Shift+Enter 换行'}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div className="da-composer-foot">
            <span className="muted">
              {running
                ? '分析执行中，发送的内容会作为补充指令'
                : kbNames.length
                  ? `将在「${kbNames.join('」「')}」中检索`
                  : '将在全部知识库中检索'}
            </span>
            <div className="da-composer-actions">
              {running ? (
                <Button variant="danger" icon={<Square size={13} />} onClick={() => channel.send({ type: 'abort' })}>停止</Button>
              ) : null}
              <Button variant="primary" icon={<Send size={13} />} disabled={!text.trim() || !started} onClick={submit}>
                {running ? '补充指令' : '发送'}
              </Button>
            </div>
          </div>
        </footer>
      </div>
    </div>
  );
}

export default function DaApp() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  );
}
