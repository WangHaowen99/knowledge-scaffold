// Content a backend produces in a shape only it knows (derived views, job reports, action results):
// markdown is rendered as such, JSON through a generic renderer that honours the declared hint.
import { useEffect, useState } from 'react';
import Markdown from './Markdown';
import type { ViewDecl } from './backend';
import { rpc } from './rpc';
import { errorText } from './toast';
import { Empty, Spinner } from './ui';

type Obj = Record<string, unknown>;
type Renderer = ViewDecl['renderer'];

const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const isScalar = (v: unknown) => v === null || ['string', 'number', 'boolean'].includes(typeof v);

const TIME_KEYS = ['time', 'date', 'timestamp', 'at', 'start', 'time_start', 'started_at', 'created_at'];
const TITLE_KEYS = ['title', 'label', 'name', 'event', 'summary'];
const TEXT_KEYS = ['description', 'detail', 'details', 'content', 'text', 'message'];
const LIST_KEYS = ['items', 'events', 'rows', 'entries', 'data'];
const MAX_COLUMNS = 8;
const MAX_ROWS = 500;

/** The rows of a list-shaped value: the value itself, or the first list of objects inside it. */
function rowsOf(data: unknown): Obj[] | null {
  const ok = (v: unknown): v is Obj[] => Array.isArray(v) && v.length > 0 && v.every(isObj);
  if (ok(data)) return data;
  if (Array.isArray(data) && data.length === 0) return [];
  if (!isObj(data)) return null;
  for (const key of [...LIST_KEYS, ...Object.keys(data)]) if (ok(data[key])) return data[key] as Obj[];
  return null;
}

const pick = (row: Obj, keys: string[]) => keys.map((k) => row[k]).find((v) => v !== undefined && v !== null && v !== '');

function Scalar({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === '') return <span className="muted">—</span>;
  if (typeof value === 'boolean') return <>{value ? '是' : '否'}</>;
  if (isScalar(value)) return <>{String(value)}</>;
  const text = JSON.stringify(value);
  return <code title={text.length > 120 ? text : undefined}>{text.length > 120 ? `${text.slice(0, 120)}…` : text}</code>;
}

function Table({ rows }: { rows: Obj[] }) {
  if (!rows.length) return <Empty title="暂无内容" />;
  const columns = Array.from(new Set(rows.slice(0, 50).flatMap((r) => Object.keys(r)))).slice(0, MAX_COLUMNS);
  return (
    <div className="json-table">
      <table className="table table-static">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, MAX_ROWS).map((row, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c}>
                  <Scalar value={row[c]} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Timeline({ rows }: { rows: Obj[] }) {
  if (!rows.length) return <Empty title="暂无内容" />;
  return (
    <ol className="timeline">
      {rows.slice(0, MAX_ROWS).map((row, i) => {
        const title = pick(row, TITLE_KEYS);
        const text = pick(row, TEXT_KEYS);
        return (
          <li key={i} className="timeline-item">
            <div className="timeline-time">
              <Scalar value={pick(row, TIME_KEYS)} />
            </div>
            <div className="timeline-body">
              {title !== undefined ? <div className="timeline-title">{String(title)}</div> : null}
              {text !== undefined ? <div className="timeline-text">{isScalar(text) ? String(text) : JSON.stringify(text)}</div> : null}
              {title === undefined && text === undefined ? <Scalar value={row} /> : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function KeyValues({ obj, depth }: { obj: Obj; depth: number }) {
  const entries = Object.entries(obj);
  if (!entries.length) return <Empty title="暂无内容" />;
  return (
    <dl className="info-grid">
      {entries.map(([k, v]) => (
        <div key={k} style={{ display: 'contents' }}>
          <dt>{k}</dt>
          <dd>
            <Generic data={v} depth={depth + 1} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Generic({ data, depth }: { data: unknown; depth: number }) {
  if (isScalar(data) || data === undefined) return <Scalar value={data} />;
  if (depth >= 2) return <pre className="json-pre">{JSON.stringify(data, null, 2)}</pre>;
  if (Array.isArray(data)) {
    if (data.every(isScalar)) return <>{data.map(String).join('、') || <span className="muted">—</span>}</>;
    const rows = rowsOf(data);
    if (rows) return <Table rows={rows} />;
    return <pre className="json-pre">{JSON.stringify(data, null, 2)}</pre>;
  }
  return <KeyValues obj={data as Obj} depth={depth} />;
}

export function JsonView({ data, renderer }: { data: unknown; renderer?: Renderer }) {
  if (renderer === 'timeline' || renderer === 'table') {
    const rows = rowsOf(data);
    if (rows) return renderer === 'timeline' ? <Timeline rows={rows} /> : <Table rows={rows} />;
  }
  if (renderer === 'kv' && isObj(data)) return <KeyValues obj={data} depth={0} />;
  return <Generic data={data} depth={0} />;
}

/** Markdown text or a JSON value; nothing to show is an empty state, not an error. */
export function ContentView({ content, renderer }: { content: unknown; renderer?: Renderer }) {
  if (content === null || content === undefined || content === '') return <Empty title="暂无内容" />;
  if (typeof content === 'string') return <Markdown text={content} />;
  return <JsonView data={content} renderer={renderer} />;
}

/** A declared view of a knowledge base or a document, fetched when shown. */
export function ViewPane({ view, target }: { view: ViewDecl; target: { kb_id: string } | { doc_id: string } }) {
  const [state, setState] = useState<{ content: unknown } | { error: string } | null>(null);
  const key = 'doc_id' in target ? target.doc_id : target.kb_id;
  useEffect(() => {
    let alive = true;
    setState(null);
    rpc
      .call<{ media_type: string | null; content: unknown }>('views.get', { ...target, view: view.id })
      .then((res) => alive && setState({ content: res.content }))
      .catch((err) => alive && setState({ error: errorText(err) }));
    return () => {
      alive = false;
    };
    // target is a fresh object on every render; its id is what decides a reload
  }, [key, view.id]);
  if (state === null) {
    return (
      <div className="empty">
        <Spinner size={18} />
      </div>
    );
  }
  if ('error' in state) return <div className="error-box">{state.error}</div>;
  return <ContentView content={state.content} renderer={view.renderer} />;
}
