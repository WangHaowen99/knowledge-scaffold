import React, { useEffect, useState } from 'react';
import { api } from './api.js';
import { VERDICTS, feedbackStatus, optimizationState, runStartTime, shortId } from './domain.js';
import { ArtifactViewer } from './RunOutputs.jsx';
import { Alert, Badge, Button, Empty, Field, Heading, Modal, Skeleton, useAction } from './ui.jsx';

const EGRESS =
  '生成优化建议时，代表问答、工具调用记录和知识包文档会发给你当前生效的判分模型网关；提交反馈的用户不会显示，只显示人数。';

function day(seconds) {
  return runStartTime({ created_at: seconds })?.date || '—';
}

// Packages with consumer feedback that the user may read in Hub, and the feedback runs of the selected one.
export function FeedbackList({ navigate }) {
  const [packages, setPackages] = useState(null),
    [selected, setSelected] = useState(''),
    [runs, setRuns] = useState(null),
    [error, setError] = useState('');
  useEffect(() => {
    api('/package-tasks?origin=consumer_feedback')
      .then((value) => {
        setPackages(value.items);
        if (value.items.length) setSelected(value.items[0].ontology_id);
      })
      .catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    if (!selected) return undefined;
    let live = true;
    setRuns(null);
    api(`/runs?origin=consumer_feedback&ontology_id=${encodeURIComponent(selected)}`)
      .then((value) => live && setRuns(value.items))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [selected]);
  return (
    <>
      <Heading title="用户反馈">
        消费服务中用户提交的错误问答和建议，按知识包和版本汇总成反馈轮次。在 Hub 上能看到该知识包的用户可以查看、修正，并确认交给建模。
      </Heading>
      <Alert tone="blue">{EGRESS}</Alert>
      {error && <Alert tone="red">{error}</Alert>}
      {!packages ? (
        <Skeleton />
      ) : !packages.length ? (
        <Empty title="还没有用户反馈" icon="message">
          消费服务推送反馈后，这里按知识包显示反馈轮次。
        </Empty>
      ) : (
        <div className="feedback-layout">
          <section className="panel feedback-packages">
            <h3>知识包</h3>
            <ul>
              {packages.map((item) => (
                <li key={item.ontology_id}>
                  <button
                    type="button"
                    className={selected === item.ontology_id ? 'selected' : ''}
                    aria-pressed={selected === item.ontology_id}
                    onClick={() => setSelected(item.ontology_id)}
                  >
                    <b title={item.ontology_id}>{item.name || item.ontology_id}</b>
                    <small>
                      {item.run_count} 个反馈轮次{item.collecting ? ' · 收集中' : ''}
                    </small>
                  </button>
                </li>
              ))}
            </ul>
          </section>
          <section className="panel table-panel">
            {!runs ? (
              <Skeleton />
            ) : (
              <div className="table-scroll">
                <table className="task-table">
                  <thead>
                    <tr>
                      <th>反馈轮次</th>
                      <th>知识包版本</th>
                      <th>反馈</th>
                      <th>状态</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => {
                      const state = feedbackStatus(run),
                        stats = run.feedback?.stats || {};
                      return (
                        <tr key={run.id}>
                          <td>
                            <b className="tabular">{shortId(run.id)}</b>
                            <small>{day(run.created_at)} 起</small>
                          </td>
                          <td>{run.package_ref?.version || '—'}</td>
                          <td>
                            {run.status === 'collecting'
                              ? `${run.feedback?.clusters?.length || 0} 个问题簇`
                              : `${stats.signals || 0} 条 · ${stats.users || 0} 位用户`}
                          </td>
                          <td>
                            <Badge tone={state.tone}>{state.label}</Badge>
                          </td>
                          <td>
                            <Button variant="text" onClick={() => navigate(`/feedback/${run.id}`)}>
                              查看
                            </Button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      )}
    </>
  );
}

// One feedback run: what users reported, the owner's corrections, and handing it to modeling.
export function FeedbackRun({ id, navigate }) {
  const [run, setRun] = useState(null),
    [cases, setCases] = useState([]),
    [viewing, setViewing] = useState(null),
    [editing, setEditing] = useState(null),
    [dismissing, setDismissing] = useState(false),
    { busy, error, act } = useAction();
  async function refresh() {
    const value = await api(`/runs/${id}`);
    setRun(value);
    if (value.status === 'completed') setCases((await api(`/runs/${id}/results`)).items);
  }
  useEffect(() => {
    refresh().catch(() => setRun(false));
    const timer = setInterval(() => refresh().catch(() => {}), 5000);
    return () => clearInterval(timer);
  }, [id]);
  if (run === false)
    return (
      <Empty title="反馈轮次不存在或无权访问" icon="message">
        只有在 Hub 上能看到该知识包的用户可以查看。
      </Empty>
    );
  if (!run) return <Skeleton />;
  const state = feedbackStatus(run),
    handled = optimizationState(run),
    reviewing = run.optimization?.status === 'awaiting_review',
    a = run.artifacts || {},
    stats = run.feedback?.stats || {},
    suggestions = run.feedback?.suggestions || [];
  const generate = () => act(async () => (await api(`/runs/${id}/artifacts/regenerate`, {}), refresh()));
  const decide = (body) => act(async () => (await api(`/runs/${id}/notifications/retry`, body), refresh()));
  return (
    <>
      <Heading
        title={`反馈轮次 ${shortId(run.id)}`}
        breadcrumb={
          <button className="text-link" onClick={() => navigate('/feedback')}>
            用户反馈
          </button>
        }
        actions={
          <>
            {run.status === 'collecting' && (
              <Button variant="primary" busy={busy} onClick={generate}>
                立即生成报告和建议
              </Button>
            )}
            {run.status === 'completed' && a.suggestions !== 'generating' && reviewing && (
              <Button busy={busy} onClick={generate}>
                {a.suggestions === 'ready' ? '重新生成优化建议' : '生成优化建议'}
              </Button>
            )}
            {reviewing && (
              <>
                <Button variant="primary" busy={busy} disabled={a.suggestions !== 'ready'} onClick={() => decide({ decision: 'confirm' })}>
                  确认并通知建模
                </Button>
                <Button variant="text" onClick={() => setDismissing(true)}>
                  驳回
                </Button>
              </>
            )}
          </>
        }
      >
        知识包 {run.package_ref?.name || run.package_ref?.ontology_id} · 版本 {run.package_ref?.version || '—'}
      </Heading>
      {error && <Alert tone="red">{error}</Alert>}
      <section className="panel feedback-summary">
        <div className="run-outputs-state">
          {/* Once with modeling, the row below says where it stands. */}
          {(!handled || reviewing) && <Badge tone={state.tone}>{state.label}</Badge>}
          <small>
            {run.status === 'collecting'
              ? `收集中：已有 ${run.feedback?.clusters?.length || 0} 个问题簇，达到条件后自动生成报告`
              : `反馈 ${stats.signals || 0} 条，来自 ${stats.users || 0} 位用户；问题簇 ${run.feedback?.clusters?.length || 0} 个`}
          </small>
        </div>
        {handled && run.optimization?.status !== 'awaiting_review' && (
          <div className="run-outputs-state">
            <span className="run-outputs-label">建模处理</span>
            <Badge tone={handled.tone}>{handled.label}</Badge>
            {handled.hint && <small>{handled.hint}</small>}
          </div>
        )}
        {reviewing && a.suggestions !== 'generating' && (a.suggestions !== 'ready' || a.suggestions_stale) && (
          <Alert tone="amber">
            {a.suggestions === 'ready'
              ? '判分已修改，当前优化建议基于修改前的内容。建议重新生成后再确认，否则建模拿到的仍是旧建议。'
              : '确认前请先生成优化建议：补充标准答案或标记误判后，可重新生成，建议会按修正后的内容归因。'}
          </Alert>
        )}
        <div className="context-links">
          <Button variant="text" disabled={a.report !== 'ready'} onClick={() => setViewing('report')}>
            查看评测报告
          </Button>
          <Button variant="text" disabled={a.suggestions !== 'ready'} onClick={() => setViewing('suggestions')}>
            查看优化建议
          </Button>
        </div>
        <Alert tone="blue">{EGRESS}</Alert>
      </section>
      {run.status === 'completed' && (
        <section className="panel feedback-section">
          <div className="section-title">
            <h3>代表问答</h3>
            <small>每个问题簇最多 3 条；用户问错或理解错的可标为误判，不进入优化建议</small>
          </div>
          {!cases.length ? (
            <p className="field-hint">本轮没有错误问答，优化依据是下方的用户建议。</p>
          ) : (
            <ul className="feedback-cases">
              {cases.map((item, index) => {
                const verdict = item.override?.verdict || item.judgement?.verdict;
                return (
                  <li key={item.id}>
                    <Badge tone={VERDICTS[verdict]?.[1]}>{verdict === 'pass' ? '用户误判' : VERDICTS[verdict]?.[0]}</Badge>
                    <span className="feedback-question">
                      {index + 1}. {item.question}
                    </span>
                    {reviewing && (
                      <span className="actions">
                        <Button variant="text" onClick={() => setEditing({ index, mode: 'reference' })}>
                          补标准答案
                        </Button>
                        {verdict !== 'pass' && (
                          <Button variant="text" onClick={() => setEditing({ index, mode: 'misjudged' })}>
                            标为误判
                          </Button>
                        )}
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}
      {suggestions.length > 0 && (
        <section className="panel feedback-section">
          <div className="section-title">
            <h3>用户建议</h3>
          </div>
          <ul className="feedback-cases">
            {suggestions.map((item, index) => (
              <li key={item.cluster_id}>
                <Badge tone="blue">建议 {index + 1}</Badge>
                <span className="feedback-question">{item.content}</span>
                <small>
                  {item.scope ? item.scope.replace('operation:', '操作：').replace('entity:', '对象：') : ''} · {item.users} 位用户
                </small>
              </li>
            ))}
          </ul>
        </section>
      )}
      {viewing && <ArtifactViewer run={run} kind={viewing} onClose={() => setViewing(null)} />}
      {editing && (
        <CaseEdit
          run={run}
          index={editing.index}
          mode={editing.mode}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await refresh();
          }}
        />
      )}
      {dismissing && (
        <Dismiss
          onClose={() => setDismissing(false)}
          onConfirm={async (reason) => {
            await api(`/runs/${id}/notifications/retry`, { decision: 'dismiss', reason });
            setDismissing(false);
            await refresh();
          }}
        />
      )}
    </>
  );
}

function CaseEdit({ run, index, mode, onClose, onSaved }) {
  const [text, setText] = useState(''),
    { busy, error, act } = useAction();
  const misjudged = mode === 'misjudged';
  const save = () =>
    act(async () => {
      if (!text.trim()) throw new Error(misjudged ? '请说明为什么是用户误判' : '请填写标准答案');
      await api(`/runs/${run.id}/results/${index}/override`, {
        verdict: misjudged ? 'pass' : 'fail',
        reason: misjudged ? text.trim() : '负责人补充标准答案',
        ...(misjudged ? {} : { reference: text.trim() }),
        revision: run.revision,
      });
      await onSaved();
    });
  return (
    <Modal
      title={misjudged ? '标为用户误判' : '补充标准答案'}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" busy={busy} onClick={save}>
            保存
          </Button>
        </>
      }
    >
      {error && <Alert tone="red">{error}</Alert>}
      <Field label={misjudged ? '原因 *' : '标准答案 *'} hint={misjudged ? '标为误判的问答不进入优化建议' : '最多 4000 字'}>
        <textarea rows={6} maxLength={4000} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
    </Modal>
  );
}

function Dismiss({ onClose, onConfirm }) {
  const [reason, setReason] = useState(''),
    { busy, error, act } = useAction();
  return (
    <Modal
      title="驳回这一轮反馈？"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button
            variant="danger"
            busy={busy}
            onClick={() =>
              act(async () => {
                if (!reason.trim()) throw new Error('请填写驳回原因');
                await onConfirm(reason.trim());
              })
            }
          >
            驳回
          </Button>
        </>
      }
    >
      {error && <Alert tone="red">{error}</Alert>}
      <p>驳回后不会交给建模。</p>
      <Field label="原因 *">
        <textarea rows={4} maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} />
      </Field>
    </Modal>
  );
}
