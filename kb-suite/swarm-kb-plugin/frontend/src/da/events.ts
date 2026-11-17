// What DA reports beyond the harness protocol, folded into the turn that shows it.
//
// The deepanalyze provider forwards DA's own SSE events unchanged as provider events
// (`{t: 'provider', event_type, payload}`): `push_content` (a card DA delivers),
// `todo_update` (its whole todo list) and `workflow_event` (sub-agent progress, `payload.type`
// is workflow_start / workflow_agent_<kind> / workflow_complete). A pushed file has no event
// of its own: it is the result object of the `push_file` tool.
//
// No runtime imports: node tests load this file directly.

export type Pushed = {
  type: string;
  title: string;
  data: string;
  format?: string;
  filePath?: string;
  autoPushed?: boolean;
};
export type Todo = { id: string; content: string; status: string };
export type WorkflowAgent = { id: string; label: string; task?: string; status: string; tool?: string; durationMs?: number };
export type Workflow = {
  id: string;
  goal?: string;
  mode?: string;
  agentCount?: number;
  status: string;
  durationMs?: number;
  resultCount?: number;
  agents: WorkflowAgent[];
};
// local: the harness fetched it to the plugin host, so the page can download it by name
export type PushedFile = { name: string; size?: number; mimeType?: string; downloadUrl?: string; local?: boolean };
export type Extras = { pushes: Pushed[]; todos: Todo[] | null; workflows: Workflow[]; files: PushedFile[] };

export const NO_EXTRAS: Extras = { pushes: [], todos: null, workflows: [], files: [] };

type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Obj) : {});
const str = (v: unknown): string => (typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '');
const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : undefined);
const first = (...vs: unknown[]): string => vs.map(str).find((s) => s.trim() !== '') ?? '';
const baseName = (path: string) => path.split(/[\\/]/).pop() ?? path;
const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n)}…` : s);

export const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'skipped']);

function addFile(files: PushedFile[], file: PushedFile): PushedFile[] {
  const at = files.findIndex((f) => f.name === file.name);
  return at < 0 ? [...files, file] : files.map((f, i) => (i === at ? { ...f, ...file } : f));
}

/** The file a `push_file` tool result delivered; null for anything else (including a skipped duplicate). */
export function pushedFileOf(result: unknown): PushedFile | null {
  let value = result;
  if (typeof value === 'string' && value.trim().startsWith('{')) {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  let r = obj(value);
  if (r.pushed === undefined && r.data && typeof r.data === 'object') r = obj(r.data);
  if (!(r.pushed === true || r.pushed === 'true') || str(r.type) !== 'file') return null;
  const name = first(r.fileName, baseName(str(r.filePath)), r.title);
  if (!name) return null;
  return { name, size: num(r.fileSize), mimeType: str(r.mimeType) || undefined, downloadUrl: str(r.downloadUrl) || undefined };
}

function todosOf(payload: unknown): Todo[] | null {
  const p = obj(payload);
  const list = [p.todos, p.items, p.value, payload].find(Array.isArray) as unknown[] | undefined;
  if (!list) return null;
  return list
    .map((item, i) => {
      const t = obj(item);
      return { id: first(t.id, t.task_id) || String(i), content: first(t.content, t.text, t.title, t.description), status: str(t.status) || 'pending' };
    })
    .filter((t) => t.content);
}

function workflowEvent(workflows: Workflow[], type: string, p: Obj): Workflow[] {
  const known = str(p.workflowId);
  // an agent event without a workflow id belongs to the latest workflow
  const id = known || workflows[workflows.length - 1]?.id || 'workflow';
  const at = workflows.findIndex((w) => w.id === id);
  const current: Workflow = at >= 0 ? workflows[at] : { id, status: 'running', agents: [] };
  let next: Workflow = current;
  if (type === 'workflow_start') {
    next = {
      ...current,
      goal: first(p.goal) || current.goal,
      mode: first(p.mode, p.teamName) || current.mode,
      agentCount: num(p.agentCount) ?? current.agentCount,
      status: 'running',
    };
  } else if (type === 'workflow_complete') {
    next = { ...current, status: str(p.status) || 'completed', durationMs: num(p.totalDuration), resultCount: num(p.resultCount) };
  } else if (type.startsWith('workflow_agent_')) {
    const kind = type.slice('workflow_agent_'.length);
    const agentId = first(p.agentId, p.member_id, p.memberId, p.step_id, p.role) || `agent-${current.agents.length + 1}`;
    const found = current.agents.find((a) => a.id === agentId);
    const agent: WorkflowAgent = found ?? { id: agentId, label: first(p.role, p.name, agentId), status: 'running' };
    const task = first(p.task, p.goal);
    const updated: WorkflowAgent = {
      ...agent,
      task: task ? clip(task, 300) : agent.task,
      status: kind === 'complete' ? str(p.status) || 'completed' : TERMINAL.has(agent.status) ? agent.status : 'running',
      tool: kind === 'tool_call' ? first(p.toolName, p.tool, p.name) || agent.tool : agent.tool,
      durationMs: num(p.duration_ms) ?? num(p.durationMs) ?? agent.durationMs,
    };
    next = { ...current, agents: found ? current.agents.map((a) => (a.id === agentId ? updated : a)) : [...current.agents, updated] };
  } else {
    return workflows;
  }
  return at >= 0 ? workflows.map((w, i) => (i === at ? next : w)) : [...workflows, next];
}

/** One provider event applied to what a turn shows; unknown events leave it as it was. */
export function applyProviderEvent(x: Extras, eventType: string, payload: unknown): Extras {
  const p = obj(payload);
  if (eventType === 'push_content') {
    const type = str(p.type) || 'text';
    if (type === 'file') {
      const name = first(baseName(str(p.filePath)), p.title);
      return name ? { ...x, files: addFile(x.files, { name, size: num(p.fileSize) }) } : x;
    }
    const data = typeof p.data === 'string' ? p.data : p.data === undefined || p.data === null ? '' : JSON.stringify(p.data, null, 2);
    const pushed: Pushed = {
      type,
      title: first(p.title, baseName(str(p.filePath))),
      data,
      format: str(p.format) || undefined,
      filePath: str(p.filePath) || undefined,
      autoPushed: p.autoPushed === true || undefined,
    };
    return { ...x, pushes: [...x.pushes, pushed] };
  }
  if (eventType === 'todo_update') {
    const todos = todosOf(payload);
    return todos ? { ...x, todos } : x;
  }
  if (eventType === 'workflow_event' || eventType === 'workflow_start' || eventType === 'workflow_complete') {
    const workflows = workflowEvent(x.workflows, str(p.type) || eventType, p);
    return workflows === x.workflows ? x : { ...x, workflows };
  }
  return x;
}

/** A `file` item (the harness fetched a pushed file to the plugin host) applied: that file can be downloaded. */
export function applyFileItem(x: Extras, item: { name?: unknown; size?: unknown; mime_type?: unknown }): Extras {
  const name = str(item.name);
  if (!name) return x;
  return { ...x, files: addFile(x.files, { name, size: num(item.size), mimeType: str(item.mime_type) || undefined, local: true }) };
}

/** A tool result applied: a delivered file joins the turn's file list. */
export function applyToolResult(x: Extras, result: unknown): Extras {
  const file = pushedFileOf(result);
  return file ? { ...x, files: addFile(x.files, file) } : x;
}

export type Table = { columns: string[]; rows: string[][] };

function cell(v: unknown): string {
  if (v === null || v === undefined) return '';
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

function splitDelimited(line: string, sep: string): string[] {
  const out: string[] = [];
  let cur = '';
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const c = line[i];
    if (quoted) {
      if (c === '"' && line[i + 1] === '"') {
        cur += '"';
        i += 1;
      } else if (c === '"') quoted = false;
      else cur += c;
    } else if (c === '"' && cur === '') quoted = true;
    else if (c === sep) {
      out.push(cur.trim());
      cur = '';
    } else cur += c;
  }
  out.push(cur.trim());
  return out;
}

/**
 * Table data of a `table` card as columns and rows: JSON (a list of objects, a list of rows with a header
 * row, or `{columns, rows}`), CSV or TSV. Null for anything else (a Markdown table renders as Markdown).
 */
export function tableOf(data: string, format?: string): Table | null {
  const text = data.trim();
  if (!text) return null;
  if (text.startsWith('[') || text.startsWith('{')) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = undefined;
    }
    if (parsed !== undefined) {
      const o = obj(parsed);
      const head = (Array.isArray(o.columns) ? o.columns : Array.isArray(o.headers) ? o.headers : null) as unknown[] | null;
      if (head && Array.isArray(o.rows)) {
        const columns = head.map((h) => first(obj(h).title, obj(h).name, h));
        const rows = (o.rows as unknown[]).map((r) => (Array.isArray(r) ? r.map(cell) : columns.map((c) => cell(obj(r)[c]))));
        return { columns, rows };
      }
      const list = Array.isArray(parsed) ? parsed : [o.items, o.data, o.rows].find(Array.isArray);
      if (Array.isArray(list) && list.length) {
        if (list.every((r) => r && typeof r === 'object' && !Array.isArray(r))) {
          const columns = Array.from(new Set(list.slice(0, 50).flatMap((r) => Object.keys(r as Obj))));
          return { columns, rows: list.map((r) => columns.map((c) => cell((r as Obj)[c]))) };
        }
        if (list.every(Array.isArray)) {
          const [header, ...rows] = list as unknown[][];
          return { columns: header.map(cell), rows: rows.map((r) => r.map(cell)) };
        }
      }
      return null;
    }
  }
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== '');
  const fmt = (format ?? '').toLowerCase();
  if (lines.some((l) => /^\s*\|.*\|\s*$/.test(l))) return null;
  const sep = fmt === 'tsv' || (fmt !== 'csv' && lines[0].includes('\t')) ? '\t' : ',';
  if (fmt !== 'csv' && fmt !== 'tsv' && !lines[0].includes(sep)) return null;
  const [header, ...rows] = lines.map((l) => splitDelimited(l, sep));
  return { columns: header, rows };
}

/** Content long enough to start folded. */
export function isLong(text: string): boolean {
  return text.length > 1500 || text.split('\n').length > 24;
}

/** Done and total of a todo list, cancelled items left out of both. */
export function todoProgress(todos: Todo[]): { done: number; total: number } {
  const live = todos.filter((t) => t.status !== 'cancelled');
  return { done: live.filter((t) => t.status === 'completed').length, total: live.length };
}
