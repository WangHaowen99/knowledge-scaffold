import React, { useEffect, useMemo, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from './api.js';
import { STATUS, artifactState, notificationState, packageVersions, runStartTime, shortId } from './domain.js';
import { Alert, Badge, Button, Modal, Skeleton, useAction } from './ui.jsx';

const HISTORY_PAGE = 3;
const MARKDOWN_PLUGINS = [remarkGfm];
// The report links each long answer to its entry in this run's answers attachment.
const ANSWER_LINK = /^reports\/[^\s#]+\.answers\.json#(case-\d+)$/;

function markdownComponents(onOpenAnswer) {
  return {
    a: ({ href = '', children }) => {
      const answer = ANSWER_LINK.exec(href);
      if (answer)
        return (
          <button type="button" className="text-link" onClick={() => onOpenAnswer(answer[1])}>
            {children}
          </button>
        );
      // Other relative links point into the modeling workspace (reports/…), which this service cannot open.
      return /^https?:\/\//i.test(href) ? (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      ) : (
        <span className="markdown-link" title={`建模工作区文件：${href}`}>
          {children}
        </span>
      );
    },
  };
}

// Report and suggestions of a stopped run, and whether onto-modeling has been told about them.
export function RunArtifacts({ run, onChanged }) {
  const [viewing, setViewing] = useState(null),
    { busy, error, act } = useAction();
  const state = artifactState(run),
    notice = notificationState(run.notification);
  const regenerate = () =>
    act(async () => {
      await api(`/runs/${run.id}/artifacts/regenerate`, {});
      await onChanged();
    });
  const resend = () =>
    act(async () => {
      await api(`/runs/${run.id}/notifications/retry`, {});
      await onChanged();
    });
  return (
    <section className="panel run-outputs">
      <h3>评测报告与优化建议</h3>
      <div className="run-outputs-body">
        <p className="run-outputs-state">
          <Badge tone={state.tone}>{state.label}</Badge>
          {state.hint && <small>{state.hint}</small>}
        </p>
        {run.artifacts?.suggestions_error && <Alert tone="amber">{run.artifacts.suggestions_error}</Alert>}
        {error && <Alert tone="red">{error}</Alert>}
        {notice && (
          <p className="run-outputs-state">
            <span className="run-outputs-label">建模通知</span>
            <Badge tone={notice.tone}>{notice.label}</Badge>
            {notice.hint && <small>{notice.hint}</small>}
            {notice.canRetry && (
              <Button variant="text" busy={busy} onClick={resend}>
                重新通知
              </Button>
            )}
          </p>
        )}
      </div>
      <div className="context-links">
        <Button variant="text" disabled={!state.report} onClick={() => setViewing('report')}>
          查看评测报告
        </Button>
        <Button variant="text" disabled={!state.suggestions} onClick={() => setViewing('suggestions')}>
          查看优化建议
        </Button>
        {state.canRegenerate && (
          <Button variant="text" busy={busy} onClick={regenerate}>
            重新生成
          </Button>
        )}
      </div>
      {viewing && <ArtifactViewer run={run} kind={viewing} onClose={() => setViewing(null)} />}
    </section>
  );
}

function ArtifactViewer({ run, kind, onClose }) {
  const [text, setText] = useState(null),
    [error, setError] = useState(''),
    [answerId, setAnswerId] = useState(null);
  const components = useMemo(() => markdownComponents(setAnswerId), []);
  useEffect(() => {
    let live = true;
    api(`/runs/${run.id}/artifacts/${kind === 'report' ? 'report.md' : 'suggestions.md'}`, undefined, 'GET', true, 'text')
      .then((value) => live && setText(value))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [run.id, kind, run.artifacts?.revision]);
  return (
    <>
      <Modal title={kind === 'report' ? '评测报告' : '优化建议'} onClose={onClose} drawer>
        {error ? (
          <Alert tone="red">{error}</Alert>
        ) : text === null ? (
          <Skeleton />
        ) : (
          <article className="markdown-body artifact-markdown">
            <Markdown remarkPlugins={MARKDOWN_PLUGINS} components={components}>
              {text}
            </Markdown>
          </article>
        )}
      </Modal>
      {/* A sibling, not a child: Escape and backdrop clicks close only the preview. */}
      {answerId && <AnswerPreview run={run} resultId={answerId} onClose={() => setAnswerId(null)} />}
    </>
  );
}

// One full answer from the answers attachment of the current revision, by result ID (case-N).
function AnswerPreview({ run, resultId, onClose }) {
  const [record, setRecord] = useState(null),
    [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    api(`/runs/${run.id}/artifacts/answers.json`)
      .then((value) => {
        if (!live) return;
        const found = (value.answers || []).find((item) => item.resultId === resultId);
        if (found) setRecord(found);
        else setError('完整回答不存在，报告可能已重新生成，请重新打开');
      })
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [run.id, resultId, run.artifacts?.revision]);
  const number = resultId.slice('case-'.length);
  return (
    <Modal title={`用例 ${number} · 完整回答`} onClose={onClose}>
      {error ? (
        <Alert tone="red">{error}</Alert>
      ) : record === null ? (
        <Skeleton />
      ) : (
        <>
          <p className="muted">
            原始回答 {record.originalLength} 字符
            {record.truncated && `，附件仅保留前 ${String(record.finalAnswer || '').length} 字符`}
          </p>
          <article className="markdown-body artifact-markdown">
            <Markdown remarkPlugins={MARKDOWN_PLUGINS}>{String(record.finalAnswer || '')}</Markdown>
          </article>
        </>
      )}
    </Modal>
  );
}

// Every run of the same knowledge package, newest first, a few per page; the current one is marked.
export function RunHistory({ run, navigate }) {
  const packageId = run.package_ref?.ontology_id,
    [offset, setOffset] = useState(0),
    [page, setPage] = useState({ items: [], total: 0 }),
    [loading, setLoading] = useState(false),
    [error, setError] = useState('');
  useEffect(() => {
    if (!packageId) return undefined;
    let live = true;
    setLoading(true);
    api(`/runs?ontology_id=${encodeURIComponent(packageId)}&offset=${offset}&limit=${HISTORY_PAGE}`)
      .then((value) => {
        if (!live) return;
        setPage({ items: value.items, total: value.total });
        setError('');
      })
      .catch((e) => live && setError(e.message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [packageId, offset, run.status]);
  if (!packageId) return null;
  const pages = Math.max(1, Math.ceil(page.total / HISTORY_PAGE)),
    pageNumber = Math.floor(offset / HISTORY_PAGE) + 1;
  return (
    <section className="panel run-history">
      <h3>历史执行记录 <small>共 {page.total} 次</small></h3>
      {error && <Alert tone="red">{error}</Alert>}
      <ul>
        {page.items.map((item) => {
          const start = runStartTime(item),
            current = item.id === run.id;
          return (
            <li key={item.id} className={current ? 'selected' : ''}>
              <b className="tabular">{shortId(item.id)}</b>
              {current && <span className="current-tag">当前</span>}
              <span className="version-tag">{packageVersions(item).base || '—'}</span>
              <small className="tabular">{start ? `${start.date} ${start.time}` : '—'}</small>
              <Badge tone={STATUS[item.status]?.[1]}>{STATUS[item.status]?.[0] || item.status}</Badge>
              {!current && (
                <span className="history-open">
                  <Button variant="text" onClick={() => navigate(`/runs/${item.id}`)}>
                    查看执行详情
                  </Button>
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {page.total > 0 && (
        <footer className="context-links history-pager">
          <Button
            variant="text"
            disabled={loading || pageNumber <= 1}
            onClick={() => setOffset(offset - HISTORY_PAGE)}
          >
            上一页
          </Button>
          <span className="tabular">
            {pageNumber} / {pages}
          </span>
          <Button
            variant="text"
            disabled={loading || pageNumber >= pages}
            onClick={() => setOffset(offset + HISTORY_PAGE)}
          >
            下一页
          </Button>
        </footer>
      )}
    </section>
  );
}
