import { Bot, ChevronDown, ChevronRight, Library, Plus, Send, Square, Wrench } from 'lucide-react';
import { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ToastProvider, useToast } from '../toast';
import { Badge, Button, Empty, Spinner } from '../ui';
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

type State = { turns: Turn[]; running: boolean };
type Action =
  | { type: 'local'; turn: Turn }
  | { type: 'accepted'; client_id: string; turn_id: string }
  | { type: 'rejected'; client_id: string; message: string }
  | { type: 'steer'; text: string }
  | { type: 'answered'; request_id: string; label: string }
  | { type: 'wire'; msg: Wire }
  | { type: 'clear' };

const KB_KEY = 'da-analyst.kb-ids';
const ENGINE_KEY = 'da-analyst.engine';
const DEFAULT_ENGINES: Engine[] = [{ id: 'deepanalyze', label: 'DeepAnalyze', ready: false }];
// each engine keeps its own conversation (a different provider and session)
const sessionKey = (engine: string) => `da-analyst.host-session.${engine}`;

// Turn events can reach the page before the `accepted` reply that carries the
// turn id; such events belong to the newest turn still waiting for its id.
function patchTurn(turns: Turn[], turnId: unknown, fn: (t: Turn) => Turn): Turn[] {
  if (!turnId) return turns;
  if (turns.some((t) => t.turn_id === turnId)) return turns.map((t) => (t.turn_id === turnId ? fn(t) : t));
  const pending = turns.map((t) => !t.turn_id).lastIndexOf(true);
  if (pending < 0) return turns;
  return turns.map((t, i) => (i === pending ? fn({ ...t, turn_id: String(turnId) }) : t));
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
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

function Shell() {
  const notify = useToast();
  const [state, dispatch] = useReducer(reducer, { turns: [], running: false });
  const [kbs, setKbs] = useState<Kb[]>([]);
  const [selected, setSelected] = useState<string[]>(() => JSON.parse(localStorage.getItem(KB_KEY) || '[]'));
  const [connected, setConnected] = useState(channel.connected);
  const [engines, setEngines] = useState<Engine[]>(DEFAULT_ENGINES);
  const [engine, setEngine] = useState<string>(() => localStorage.getItem(ENGINE_KEY) || 'deepanalyze');
  const engineRef = useRef(engine);
  engineRef.current = engine;
  const [started, setStarted] = useState(false);
  const [text, setText] = useState('');
  const bottom = useRef<HTMLDivElement>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  useEffect(() => channel.onStatus(setConnected), []);

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
          type: 'start', engine: chosen, host_session_id: sessionStorage.getItem(sessionKey(chosen)) || '', kb_ids: ids,
        });
      } else if (msg.t === 'started') {
        sessionStorage.setItem(sessionKey(String(msg.engine || engineRef.current)), String(msg.host_session_id));
        setStarted(true);
        if (msg.resumed) notify('已接上之前的会话');
      } else if (msg.t === 'accepted') {
        if (msg.mode !== 'steer') dispatch({ type: 'accepted', client_id: String(msg.client_id), turn_id: String(msg.turn_id) });
      } else if (msg.t === 'error') {
        notify(String(msg.message), true);
        if (msg.client_id) dispatch({ type: 'rejected', client_id: String(msg.client_id), message: String(msg.message) });
      } else {
        dispatch({ type: 'wire', msg });
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

  const newConversation = () => {
    sessionStorage.removeItem(sessionKey(engine));
    dispatch({ type: 'clear' });
    setStarted(false);
    channel.send({ type: 'start', engine, host_session_id: '', kb_ids: selected });
  };

  const switchEngine = (next: string) => {
    if (next === engine) return;
    setEngine(next);
    localStorage.setItem(ENGINE_KEY, next);
    dispatch({ type: 'clear' });
    setStarted(false);
    channel.send({ type: 'start', engine: next, host_session_id: sessionStorage.getItem(sessionKey(next)) || '', kb_ids: selected });
  };

  const toggleKb = (id: string) =>
    setSelected((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));

  const scopeText = useMemo(() => {
    if (!kbs.length) return '没有可用的知识库';
    return selected.length ? `${selected.length} 个知识库` : '全部知识库';
  }, [kbs, selected]);

  return (
    <div className="da-app">
      <header className="da-header">
        <div>
          <div className="da-title">DA 深度分析</div>
          <div className="da-sub">
            通过 HarnessProtocol 驱动 {engineLabel}，在 kb-service 知识库上做多步分析 ·
            <span className={`dot ${connected && current?.ready ? '' : 'dot-off'}`} style={{ display: 'inline-block', margin: '0 4px 0 8px' }} />
            {connected ? (current?.ready ? `${engineLabel} 已连接` : `${engineLabel} 未就绪`) : '正在连接…'}
          </div>
        </div>
        <div className="da-header-actions">
          {engines.length > 1 ? (
            <select
              className="select da-engine"
              value={engine}
              disabled={running}
              title="分析引擎"
              onChange={(e) => switchEngine(e.target.value)}
            >
              {engines.map((e) => (
                <option key={e.id} value={e.id}>{e.label}{e.ready ? '' : '（未就绪）'}</option>
              ))}
            </select>
          ) : null}
          <Button icon={<Plus size={14} />} onClick={newConversation} disabled={running}>新对话</Button>
        </div>
      </header>
      <div className="da-scope">
        <Library size={14} className="muted" />
        <span className="muted">检索范围：{scopeText}</span>
        {kbs.map((kb) => (
          <button
            key={kb.id}
            className={`da-chip ${selected.includes(kb.id) ? 'da-chip-on' : ''}`}
            onClick={() => toggleKb(kb.id)}
            title={`${kb.documents} 个已入库文档`}
          >
            {kb.name}
          </button>
        ))}
      </div>
      <main className="da-thread">
        {state.turns.length === 0 ? (
          <Empty
            icon={<Bot size={36} />}
            title={`向 ${engineLabel} 提一个分析问题`}
            hint={
              engine === 'loopforest'
                ? 'Loop Forest 会检索知识库，必要时把问题拆成分支并行查证再汇总。执行中可以继续输入补充指令，或点停止。'
                : 'DA 会检索、展开、交叉核对知识库里的文档后作答，必要时会向你追问。执行中可以继续输入补充指令，或点停止。'
            }
          />
        ) : (
          state.turns.map((turn) => (
            <TurnView
              key={turn.client_id}
              turn={turn}
              onAnswer={(ask, payload, label) => {
                channel.send({ type: 'answer', request_id: ask.request_id, ...payload });
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
          placeholder={running ? '输入补充指令，发送后注入到正在执行的分析' : '例如：对比几份手册里润滑油系统的报警值和联锁值，列出不一致的地方'}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="da-composer-actions">
          {running ? (
            <Button variant="danger" icon={<Square size={13} />} onClick={() => channel.send({ type: 'abort' })}>停止</Button>
          ) : null}
          <Button variant="primary" icon={<Send size={13} />} disabled={!text.trim() || !started} onClick={submit}>
            {running ? '补充指令' : '发送'}
          </Button>
        </div>
      </footer>
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
