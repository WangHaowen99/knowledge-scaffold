import React, { useEffect, useMemo, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from './api.js';
import {
  STATUS,
  artifactState,
  carriedState,
  carryoverCounts,
  notificationState,
  optimizationState,
  packageVersions,
  passRate,
  runStartTime,
  scoreText,
  shortId,
} from './domain.js';
import { Alert, Badge, Button, Empty, Icon, Modal, Skeleton, Steps, useAction } from './ui.jsx';

const HISTORY_PAGE = 10;
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

// Earlier runs of the same source still waiting for modeling that this push carries, or would carry once confirmed.
export function CarryoverNote({ carryover, feedback, navigate, preview = false }) {
  const counts = carryoverCounts(carryover);
  if (!counts.rounds && !counts.slidOut) return null;
  // Feedback arrives in batches the page never asks the owner to tell apart; evaluation runs are runs.
  const some = (n) => (feedback ? `${n} 批` : `${n} 个`),
    kind = feedback ? '反馈' : '评测轮次',
    path = feedback ? '/feedback/' : '/runs/';
  return (
    <p className="carryover-note">
      <Icon name="link" />
      <span>
        {counts.rounds > 0 && (
          <>
            {preview ? '确认后会一起带上' : '这次一起带上'} {some(counts.rounds)}建模还没处理的{kind}（版本{' '}
            {carryover.rounds.map((r, i) => (
              <React.Fragment key={r.run_id}>
                {i > 0 && '、'}
                <button type="button" className="text-link" onClick={() => navigate?.(path + r.run_id)}>
                  {r.version}
                </button>
              </React.Fragment>
            ))}
            ）：问题 {counts.cases} 条、建议 {counts.items} 条，建模的处理结果会一起回写。
          </>
        )}
        {counts.slidOut > 0 && `另有 ${some(counts.slidOut)}更早的${kind}因文件超过大小上限没有带上，仍待建模处理。`}
      </span>
    </p>
  );
}

// Where a stopped run's report and suggestions stand on their way to modeling: generated, delivered, handled,
// and verified on the version modeling published.
export function DeliveryCard({ run, onChanged, navigate }) {
  const { busy, error, act } = useAction();
  const state = artifactState(run),
    notice = notificationState(run.notification),
    handled = optimizationState(run),
    earlier = run.optimization?.history || [],
    feedback = run.origin === 'consumer_feedback',
    carried = carriedState(run);
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
  const generated = state.report && state.suggestions;
  const delivered = notice?.label === '已送达';
  const done = run.optimization?.status === 'modeling_done',
    failed = run.optimization?.status === 'modeling_failed';
  const items = [
    {
      label: state.report && state.suggestions ? '报告和建议已生成' : state.label,
      hint: state.hint,
      state: state.report && state.suggestions ? 'done' : state.tone === 'amber' ? 'failed' : 'current',
    },
    {
      label: notice ? `建模通知${notice.label}` : '通知建模',
      hint: notice?.hint || '报告和建议生成后发送',
      state: delivered ? 'done' : notice?.canRetry ? 'failed' : generated ? 'current' : '',
    },
    {
      label: done ? '建模已处理' : failed ? '建模处理失败' : handled?.label || '等待建模处理',
      hint: handled?.hint || '建模执行评测优化并发布新版本后，这里自动更新',
      state: done ? 'done' : failed ? 'failed' : delivered ? 'current' : '',
    },
    {
      label: '在新版本上验证',
      hint: done ? `可在新版本 ${run.optimization.result?.version || ''} 上再次评测` : '建模发布新版本后可用',
      state: done ? 'current' : '',
    },
  ];
  return (
    <section className="panel delivery-card">
      <div className="section-title">
        <h3>交付建模</h3>
        <div className="actions">
          {notice?.canRetry && (
            <Button variant="text" busy={busy} onClick={resend}>
              重新通知
            </Button>
          )}
          {state.canRegenerate && (
            <Button variant="text" icon="refresh" busy={busy} onClick={regenerate}>
              重新生成报告和建议
            </Button>
          )}
          {done && navigate && !feedback && (
            // The existing re-evaluation, on the version modeling published from these suggestions.
            <Button
              variant="primary"
              onClick={() =>
                navigate(`/new?from=${run.id}&release_id=${encodeURIComponent(run.optimization.result.release_id)}`)
              }
            >
              在新版本上再次评测
            </Button>
          )}
        </div>
      </div>
      <Steps items={feedback ? items.slice(0, 3) : items} />
      <CarryoverNote carryover={run.artifacts?.carryover} feedback={feedback} navigate={navigate} />
      {carried && (
        <p className="carryover-note">
          <Icon name="link" />
          <span>
            {carried.label}：{carried.hint}。
            <button
              type="button"
              className="text-link"
              onClick={() => navigate?.(`${feedback ? '/feedback/' : '/runs/'}${carried.runId}`)}
            >
              查看那一轮
            </button>
          </span>
        </p>
      )}
      {run.artifacts?.suggestions_error && <Alert tone="amber">{run.artifacts.suggestions_error}</Alert>}
      {error && <Alert tone="red">{error}</Alert>}
      {earlier.length > 0 && (
        <details className="optimization-history">
          <summary>历次回写（{earlier.length + 1}）</summary>
          <ul>
            {earlier.map((item) => (
              <li key={item.reported_at}>
                {item.outcome === 'done' ? `新版本 ${item.version || item.release_id}` : '处理失败'}
                {item.note && ` · ${item.note}`}
                <small>{runStartTime({ created_at: item.reported_at })?.date}</small>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

// The report or suggestions shown in place; long answers open from the attachment.
export function ArtifactPanel({ run, kind, ready, waiting, note }) {
  const [text, setText] = useState(null),
    [error, setError] = useState(''),
    [answerId, setAnswerId] = useState(null);
  const components = useMemo(() => markdownComponents(setAnswerId), []);
  const name = kind === 'report' ? 'report.md' : 'suggestions.md';
  useEffect(() => {
    if (!ready) return undefined;
    let live = true;
    setText(null);
    setError('');
    api(`/runs/${run.id}/artifacts/${name}`, undefined, 'GET', true, 'text')
      .then((value) => live && setText(value))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [run.id, name, ready, run.artifacts?.revision]);
  function download() {
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
    link.download = `${run.artifacts?.stem || shortId(run.id)}${kind === 'report' ? '' : '-suggestions'}.md`;
    link.click();
    URL.revokeObjectURL(link.href);
  }
  if (!ready)
    return (
      <section className="panel">
        <Empty title={kind === 'report' ? '评测报告还没有生成' : '优化建议还没有生成'} icon="set">
          {waiting}
        </Empty>
      </section>
    );
  return (
    <>
      {note}
      <section className="panel artifact-panel">
        {error ? (
          <Alert tone="red">{error}</Alert>
        ) : text === null ? (
          <Skeleton />
        ) : (
          <>
            <div className="artifact-tools">
              <Button icon="download" onClick={download}>
                下载
              </Button>
            </div>
            <article className="markdown-body artifact-markdown">
              <Markdown remarkPlugins={MARKDOWN_PLUGINS} components={components}>
                {text}
              </Markdown>
            </article>
          </>
        )}
      </section>
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

// Every run of the same knowledge package, newest first, a page at a time; the current one is marked.
export function RunHistory({ run, navigate, onTotal }) {
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
        onTotal?.(value.total);
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
    <section className="panel table-panel">
      {error && <Alert tone="red">{error}</Alert>}
      <div className="table-scroll">
        <table className="history-table">
          <thead>
            <tr>
              <th>版本</th>
              <th>开始时间</th>
              <th>状态</th>
              <th>得分 · 通过率</th>
              <th>建模处理</th>
              <th className="align-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {page.items.map((item) => {
              const start = runStartTime(item),
                current = item.id === run.id,
                handled = optimizationState(item),
                carried = carriedState(item);
              return (
                <tr
                  key={item.id}
                  className={current ? 'selected' : 'clickable'}
                  onClick={current ? undefined : () => navigate(`/runs/${item.id}`)}
                >
                  <td>
                    <span className="version-tag">{packageVersions(item).base || '—'}</span>
                    {current && <Badge tone="blue">当前</Badge>}
                  </td>
                  <td className="tabular">{start ? `${start.date} ${start.time}` : '—'}</td>
                  <td>
                    <Badge tone={STATUS[item.status]?.[1]}>{STATUS[item.status]?.[0] || item.status}</Badge>
                  </td>
                  <td className="tabular">
                    {item.summary?.score == null ? (
                      <span className="muted">未形成有效评测</span>
                    ) : (
                      <>
                        <b>{scoreText(item.summary)}</b> 分 · {passRate(item.summary)}
                      </>
                    )}
                  </td>
                  <td>
                    {handled ? <Badge tone={handled.tone}>{handled.label}</Badge> : <span className="muted">—</span>}
                    {carried && <small>{carried.label}</small>}
                  </td>
                  <td className="align-right">
                    {!current && (
                      <Button variant="text" onClick={() => navigate(`/runs/${item.id}`)}>
                        查看
                      </Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <footer className="table-footer">
          <span>共 {page.total} 次评测</span>
          <span className="pager">
            <Button variant="text" disabled={loading || pageNumber <= 1} onClick={() => setOffset(offset - HISTORY_PAGE)}>
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
          </span>
        </footer>
      )}
    </section>
  );
}
