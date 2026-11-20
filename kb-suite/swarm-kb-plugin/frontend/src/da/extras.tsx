// What DA delivers or reports inside a turn besides its answer: pushed cards, its todo list,
// sub-agent progress and pushed files (see events.ts for where each comes from).
import {
  BarChart3, CheckCircle2, Circle, CircleDot, Code2, Download, FileText, GitFork, ListChecks, Table2, XCircle,
} from 'lucide-react';
import { useState, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { formatBytes } from '../format';
import { Spinner } from '../ui';
import { TERMINAL, isLong, tableOf, todoProgress, type Pushed, type PushedFile, type Todo, type Workflow } from './events';

const seconds = (ms: number) => (ms >= 60000 ? `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s` : `${(ms / 1000).toFixed(1)}s`);

function StatusIcon({ status, live }: { status: string; live: boolean }) {
  if (status === 'completed') return <CheckCircle2 size={13} className="da-st-done" />;
  if (status === 'failed') return <XCircle size={13} className="da-st-failed" />;
  if (status === 'cancelled' || status === 'skipped') return <XCircle size={13} className="da-st-off" />;
  if (status === 'in_progress' || status === 'running') return live ? <Spinner size={11} /> : <CircleDot size={13} className="da-st-off" />;
  return <Circle size={13} className="da-st-off" />;
}

export function TodoPanel({ todos, live }: { todos: Todo[]; live: boolean }) {
  const { done, total } = todoProgress(todos);
  return (
    <div className="da-panel">
      <div className="da-panel-head">
        <ListChecks size={13} />
        <span className="num">{done}/{total}</span>
      </div>
      <ul className="da-todo">
        {todos.map((t) => (
          <li key={t.id} className={`da-todo-${t.status}`}>
            <StatusIcon status={t.status} live={live} />
            <span>{t.content}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function WorkflowPanel({ flow, live }: { flow: Workflow; live: boolean }) {
  const total = flow.agentCount ?? flow.agents.length;
  const done = flow.agents.filter((a) => TERMINAL.has(a.status)).length;
  const running = !TERMINAL.has(flow.status);
  return (
    <div className="da-panel">
      <div className="da-panel-head">
        <GitFork size={13} />
        <span className="da-panel-title" title={flow.goal}>{flow.goal || flow.mode || ''}</span>
        {total ? <span className="num">{Math.min(done, total)}/{total}</span> : null}
        <StatusIcon status={running ? 'running' : flow.status} live={live && running} />
        {flow.durationMs ? <span className="muted">{seconds(flow.durationMs)}</span> : null}
      </div>
      {flow.agents.length ? (
        <ul className="da-flow">
          {flow.agents.map((a) => (
            <li key={a.id}>
              <StatusIcon status={a.status} live={live} />
              <span className="da-flow-agent">{a.label}</span>
              <span className="da-flow-task" title={a.task}>{a.task}</span>
              {a.tool && !TERMINAL.has(a.status) ? <code>{a.tool}</code> : null}
              {a.durationMs ? <span className="muted">{seconds(a.durationMs)}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

const PUSH_ICON: Record<string, ReactNode> = {
  table: <Table2 size={13} />,
  code: <Code2 size={13} />,
  chart: <BarChart3 size={13} />,
};

function PushBody({ push }: { push: Pushed }) {
  const markdown = (text: string) => (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
  if (push.type === 'table') {
    const table = tableOf(push.data, push.format);
    if (!table) return markdown(push.data);
    return (
      <div className="da-push-table">
        <table>
          <thead>
            <tr>{table.columns.map((c, i) => <th key={i}>{c}</th>)}</tr>
          </thead>
          <tbody>
            {table.rows.map((row, i) => (
              <tr key={i}>{row.map((v, j) => <td key={j}>{v}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  if (push.type === 'code' || push.type === 'chart') return <pre className="da-push-code">{push.data}</pre>;
  if (push.type === 'image' && push.data.startsWith('data:image/')) return <img className="da-push-img" src={push.data} alt={push.title} />;
  if (push.type === 'markdown' || push.format === 'markdown' || push.format === 'md') return markdown(push.data);
  return <div className="da-push-text">{push.data}</div>;
}

export function PushCard({ push }: { push: Pushed }) {
  const long = isLong(push.data);
  const [open, setOpen] = useState(false);
  return (
    <div className="da-push">
      <div className="da-push-head">
        {PUSH_ICON[push.type] ?? <FileText size={13} />}
        <span className="da-panel-title">{push.title}</span>
      </div>
      <div className={`da-push-body ${long && !open ? 'da-push-folded' : ''}`}>
        <PushBody push={push} />
      </div>
      {long ? (
        <button className="link-btn da-push-toggle" onClick={() => setOpen(!open)}>
          {open ? '收起' : '展开'}
        </button>
      ) : null}
    </div>
  );
}

// onDownload is given the files the plugin host has (the harness fetched them); others stay on DA
export function FileList({ files, onDownload }: { files: PushedFile[]; onDownload: (file: PushedFile) => void }) {
  return (
    <div className="da-files">
      {files.map((f) => (
        <div key={f.name} className="da-file">
          <FileText size={14} />
          <span className="da-file-name" title={f.name}>{f.name}</span>
          {f.size !== undefined ? <span className="muted">{formatBytes(f.size)}</span> : null}
          <button
            className="icon-btn"
            title={f.local ? '下载' : '文件还在 DA 上，取不到'}
            disabled={!f.local}
            onClick={() => onDownload(f)}
          >
            <Download size={13} />
          </button>
        </div>
      ))}
    </div>
  );
}

