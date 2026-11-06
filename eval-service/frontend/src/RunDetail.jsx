import React, { useEffect, useRef, useState } from 'react';
import { allPages, api, requestId } from './api.js';
import {
  ACTIVE,
  ENDED,
  PHASES,
  STATUS,
  VERDICTS,
  canResume,
  caseState,
  packageName,
  packageVersions,
  percent,
  shortId,
} from './domain.js';
import {
  Alert,
  Badge,
  Breadcrumb,
  Button,
  Confirm,
  Empty,
  Field,
  Heading,
  Icon,
  Modal,
  Skeleton,
  useAction,
} from './ui.jsx';
import { RunArtifacts, RunHistory } from './RunOutputs.jsx';

export function RunDetail({ id, navigate, onAction, onChanged }) {
  const [run, setRun] = useState(null),
    [cases, setCases] = useState([]),
    [loading, setLoading] = useState(true),
    [syncError, setSyncError] = useState(''),
    [fatal, setFatal] = useState(''),
    [lastSync, setLastSync] = useState(null);
  const [filter, setFilter] = useState('all'),
    [search, setSearch] = useState(''),
    [limit, setLimit] = useState(20),
    [selected, setSelected] = useState(null),
    [showConfig, setShowConfig] = useState(false),
    [publish, setPublish] = useState(false),
    [copyMessage, setCopyMessage] = useState('');
  const generation = useRef(0),
    lock = useRef(false),
    rowRefs = useRef(new Map());
  async function refresh() {
    if (lock.current) return;
    lock.current = true;
    const version = generation.current;
    try {
      const [r, items] = await Promise.all([api(`/runs/${id}`), allPages(`/runs/${id}/results`)]);
      if (generation.current !== version) return;
      setRun(r);
      setCases(items);
      setSyncError('');
      setFatal('');
      setLastSync(new Date());
    } catch (e) {
      if (generation.current === version) {
        if (e.status === 404) setFatal('任务不存在或无权访问');
        else setSyncError(e.message);
      }
    } finally {
      if (generation.current === version) {
        lock.current = false;
        setLoading(false);
      }
    }
  }
  useEffect(() => {
    refresh();
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') refresh();
    }, 5000);
    const visible = () => {
      if (document.visibilityState === 'visible') refresh();
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      generation.current += 1;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [id]);
  const active = run && ACTIVE.has(run.status),
    summary = run?.summary,
    counts = summary?.counts || {};
  const visible = cases.filter((c) => {
    const state = caseState(c);
    const ok =
      filter === 'all' ||
      (filter === 'active'
        ? ['executing', 'unknown', 'answer_ready'].includes(state)
        : filter === 'errors'
          ? ['execution_error', 'judge_error'].includes(state)
          : state === filter);
    return ok && c.question.toLowerCase().includes(search.toLowerCase());
  });
  function locate() {
    const index = cases.findIndex((c) => ['executing', 'unknown', 'answer_ready'].includes(c.status));
    if (index < 0) return;
    setFilter('all');
    setSearch('');
    setLimit(Math.max(limit, Math.ceil((index + 1) / 20) * 20));
    requestAnimationFrame(() =>
      requestAnimationFrame(() =>
        rowRefs.current
          .get(cases[index].id)
          ?.scrollIntoView({
            block: 'nearest',
            behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
          }),
      ),
    );
  }
  async function copyLink() {
    try {
      await navigator.clipboard.writeText(`${location.origin}${location.pathname}#/runs/${id}`);
      setCopyMessage('详情链接已复制');
    } catch {
      setCopyMessage('复制不可用，请复制浏览器地址栏的任务链接');
    }
  }
  const breadcrumb = <Breadcrumb parent="评测任务" current="执行详情" onBack={() => navigate('/')} />;
  if (loading && !run)
    return (
      <>
        {breadcrumb}
        <Skeleton />
      </>
    );
  if (fatal)
    return (
      <>
        {breadcrumb}
        <Empty title={fatal}>请确认登录账号和任务地址。</Empty>
      </>
    );
  if (!run)
    return (
      <>
        {breadcrumb}
        <Empty
          title="任务暂时无法加载"
          action={
            <Button icon="refresh" onClick={refresh}>
              重试
            </Button>
          }
        >
          {syncError}
        </Empty>
      </>
    );
  const phaseIndex = run.phase === 'finished' ? 4 : PHASES.findIndex(([key]) => key === run.phase);
  const current = cases.find((c) => ['executing', 'unknown', 'answer_ready'].includes(c.status));
  const versions = packageVersions(run);
  const facts = [
    ['知识包 ID', run.package_ref?.ontology_id || '—'],
    ['知识包版本', `${versions.base || '—'}${versions.snapshot ? `（评测快照 ${versions.snapshot}）` : ''}`],
    ['知识包名称', packageName(run)],
    ['数据源', run.data_source_name || '数据源配置快照'],
    ['评测集', run.eval_set_name || '评测集快照'],
  ];
  return (
    <>
      {breadcrumb}
      <Heading
        title={
          <>
            {shortId(run.id)}{' '}
            <Badge tone={STATUS[run.status]?.[1]}>{STATUS[run.status]?.[0] || run.status}</Badge>
          </>
        }
        actions={
          <>
            <Button icon="link" onClick={copyLink}>
              复制详情链接
            </Button>
            {!ENDED.has(run.status) && run.status !== 'cancelling' ? (
              <Button variant="danger" icon="stop" onClick={() => onAction('cancel', run, refresh)}>
                停止评测
              </Button>
            ) : (
              <Button variant="primary" icon="plus" onClick={() => navigate(`/new?from=${id}`)}>
                再次评测
              </Button>
            )}
          </>
        }
      >
        {facts.map(([label, value, title], i) => (
          <React.Fragment key={label}>
            {i > 0 && ' / '}
            <span title={title}>
              {label}：{value}
            </span>
          </React.Fragment>
        ))}
      </Heading>
      {copyMessage && <Alert>{copyMessage}</Alert>}
      <div className="sync-status">
        <Badge tone={syncError ? 'amber' : active ? 'blue' : 'green'}>
          {syncError ? '同步中断' : active ? '自动更新中' : '结果已保存'}
        </Badge>
        <span>
          每 5 秒同步{lastSync && ` · 最近同步 ${lastSync.toLocaleTimeString('zh-CN', { hour12: false })}`}
        </span>
        <span className="push-right">离开页面后，可从「评测任务」回来查看</span>
      </div>
      {syncError && (
        <Alert
          tone="amber"
          action={
            <Button icon="refresh" onClick={refresh}>
              重试同步
            </Button>
          }
        >
          未能刷新，仍保留最后一次成功数据。{syncError}
        </Alert>
      )}
      {(run.error || ['interrupted', 'waiting_auth'].includes(run.status)) && (
        <Alert
          tone={run.error?.code === 'configuration_changed' ? 'red' : 'amber'}
          title={
            run.error?.code === 'configuration_changed'
              ? '模型配置已变化，请创建新评测'
              : run.status === 'waiting_auth'
                ? '任务需要重新授权'
                : '本轮执行暂时中断'
          }
          action={
            canResume(run) ? (
              <Button variant="primary" onClick={() => onAction('resume', run, refresh)}>
                {run.status === 'waiting_auth' ? '授权并继续' : '恢复并继续'}
              </Button>
            ) : run.error?.code?.startsWith('graph_') && run.status !== 'cancelling' ? (
              <Button variant="primary" onClick={() => navigate(`/new?from=${id}`)}>
                重新创建并确认范围
              </Button>
            ) : run.status === 'cancelling' ? (
              <Button onClick={() => onAction('resume', run, refresh)}>继续停止对账</Button>
            ) : undefined
          }
        >
          {run.error?.message || '本轮进度已保存，请确认有效登录与依赖后恢复。'}
        </Alert>
      )}
      {run.status === 'cancelling' && (
        <Alert tone="amber" title="正在停止并核对远端状态">
          已经完成的回答与判分会保留。完成对账后才允许删除评测记录。
        </Alert>
      )}
      <section className="panel phase-track">
        {PHASES.map(([key, label, hint], i) => (
          <div
            key={key}
            className={`phase ${i < phaseIndex ? 'done' : ''} ${i === phaseIndex ? 'current' : ''}`}
          >
            <span className="step-node">{i < phaseIndex ? <Icon name="check" /> : i + 1}</span>
            <div>
              <b>{label}</b>
              <small>{hint}</small>
            </div>
          </div>
        ))}
      </section>
      <div className="run-metrics">
        {[
          [
            'all',
            '已判分',
            `${summary.judged} / ${summary.planned}`,
            percent(summary.judged / (summary.planned || 1)),
          ],
          ['pass', '通过', counts.pass || 0],
          ['partial', '部分通过', counts.partial || 0],
          ['fail', '未通过', counts.fail || 0],
          ['manual', '待人工', counts.manual || 0],
        ].map(([key, label, value, hint]) => (
          <button
            key={key}
            className={`run-metric ${filter === key ? 'selected' : ''}`}
            aria-pressed={filter === key}
            onClick={() => {
              setFilter(filter === key ? 'all' : key);
              setLimit(20);
            }}
          >
            <span>{label}</span>
            <b>{value}</b>
            {hint && <small>{hint}</small>}
          </button>
        ))}
      </div>
      <div className="run-outputs-grid">
        <RunArtifacts run={run} onChanged={refresh} navigate={navigate} />
        <RunHistory run={run} navigate={navigate} />
      </div>
      <div className="run-grid">
        <section className="panel table-panel">
          <div className="table-toolbar">
            <div className="tabs">
              {[
                ['all', `全部用例 ${summary.planned}`],
                ['active', '执行中'],
                ['errors', `异常 ${(counts.execution_error || 0) + (counts.judge_error || 0)}`],
              ].map(([key, title]) => (
                <button
                  className={filter === key ? 'selected' : ''}
                  key={key}
                  onClick={() => {
                    setFilter(key);
                    setLimit(20);
                  }}
                >
                  {title}
                </button>
              ))}
            </div>
            <Button variant="text" onClick={locate} disabled={!current}>
              定位执行中
            </Button>
          </div>
          <div className="case-search">
            <label className="search-input">
              <Icon name="search" />
              <input
                aria-label="搜索用例问题"
                placeholder="搜索问题"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setLimit(20);
                }}
              />
            </label>
            {!['all', 'active', 'errors'].includes(filter) && (
              <Button variant="text" onClick={() => setFilter('all')}>
                当前：{VERDICTS[filter]?.[0]} · 清除筛选
              </Button>
            )}
          </div>
          {!visible.length ? (
            <Empty
              title="没有符合条件的用例"
              action={
                <Button
                  onClick={() => {
                    setFilter('all');
                    setSearch('');
                  }}
                >
                  显示全部用例
                </Button>
              }
            >
              可调整筛选，或等待执行结果更新。
            </Empty>
          ) : (
            <div className="table-scroll">
              <table className="case-table">
                <thead>
                  <tr>
                    <th>序号</th>
                    <th>问题</th>
                    <th>执行 / 判分状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.slice(0, limit).map((c) => {
                    const state = caseState(c),
                      view = VERDICTS[state] || VERDICTS.pending;
                    return (
                      <tr
                        key={c.id}
                        ref={(el) => {
                          if (el) rowRefs.current.set(c.id, el);
                        }}
                        className={state === 'executing' ? 'current-case' : ''}
                      >
                        <td className="case-index">{String(Number(c.id) + 1).padStart(2, '0')}</td>
                        <td>
                          <button className="case-question" onClick={() => setSelected(c.id)}>
                            {c.question}
                          </button>
                        </td>
                        <td>
                          <span className={`verdict ${view[1]}`}>
                            <b>{view[2]}</b>
                            {view[0]}
                          </span>
                          {['execution_error', 'judge_error'].includes(state) && c.error?.message && (
                            <small className="danger" title={c.error.message}>
                              {c.error.message}
                            </small>
                          )}
                        </td>
                        <td>
                          <Button variant="text" onClick={() => setSelected(c.id)}>
                            答案与判分
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <footer className="table-footer">
            <span>
              已展示 {Math.min(limit, visible.length)} / {visible.length} 条 · 保留当前筛选与展开项
            </span>
            {visible.length > limit && (
              <Button variant="text" onClick={() => setLimit(limit + 20)}>
                加载更多用例
              </Button>
            )}
          </footer>
        </section>
        <aside className="panel run-context">
          <h3>{active ? '当前执行' : '结果概览'}</h3>
          {!active && (
            <>
              <div className="result-score">{percent(summary.strict_pass_rate)}</div>
              <p>
                严格通过率 · {counts.pass || 0} / {summary.planned}
              </p>
              <hr />
              <p>
                已判分通过率 <b className="float-right">{percent(summary.judged_pass_rate)}</b>
              </p>
              <p>
                {counts.pass || 0} / {summary.judged} 已判分用例
              </p>
            </>
          )}
          {active && (
            <>
              {current ? (
                <>
                  <small>用例 {Number(current.id) + 1}</small>
                  <h4>{VERDICTS[current.status]?.[0] || '执行中'}</h4>
                  <p>{current.question}</p>
                </>
              ) : (
                <p>{run.status === 'queued' ? '等待执行槽位' : '后台正在处理本轮评测'}</p>
              )}
              <hr />
              <p>
                已获得回答{' '}
                <b className="float-right">
                  {summary.executed} / {summary.planned}
                </b>
              </p>
              <p>
                已判分{' '}
                <b className="float-right">
                  {summary.judged} / {summary.planned}
                </b>
              </p>
              <p>
                待人工判定 <b className="float-right">{counts.manual || 0}</b>
              </p>
            </>
          )}
          <hr />
          <h3>本轮配置快照</h3>
          <dl>
            <dt>问答模型</dt>
            <dd>{run.model?.id || summary.model?.id || '等待加载'}</dd>
            <dt>并发数</dt>
            <dd>{run.concurrency || '—'}</dd>
            <dt>判分模型</dt>
            <dd>{run.judge?.model || '—'}</dd>
            <dt>配置版本</dt>
            <dd>{run.judge?.revision || '—'}</dd>
          </dl>
          <div className="context-links">
            <Button variant="text" onClick={() => setShowConfig(true)}>
              查看配置快照
            </Button>
            {run.mapping && (
              <Button variant="text" onClick={() => setPublish(true)}>
                发布派生版本
              </Button>
            )}
          </div>
          {!ACTIVE.has(run.status) && (
            <Button variant="text danger" onClick={() => onAction('delete', run, refresh)}>
              删除评测记录
            </Button>
          )}
        </aside>
      </div>
      {selected !== null && (
        <AnswerDrawer
          key={`${id}-${selected}`}
          run={run}
          caseId={selected}
          cases={cases}
          onChange={setSelected}
          onClose={() => setSelected(null)}
          onSaved={async () => {
            await refresh();
            onChanged();
          }}
        />
      )}
      {showConfig && (
        <Modal
          title="本轮配置快照"
          onClose={() => setShowConfig(false)}
          footer={<Button onClick={() => setShowConfig(false)}>关闭</Button>}
        >
          <dl className="snapshot">
            <dt>任务标识</dt>
            <dd>{id}</dd>
            <dt>知识包</dt>
            <dd>{packageName(run)}</dd>
            <dt>知识包版本</dt>
            <dd>{versions.base || `—（基础版本 ID：${versions.baseId || '未知'}）`}</dd>
            <dt>评测快照版本</dt>
            <dd>{versions.snapshot || '未生成（直接使用发布版本）'}</dd>
            <dt>内容摘要</dt>
            <dd>
              <code>{run.package_ref?.package_digest}</code>
            </dd>
            <dt>数据源</dt>
            <dd>{run.data_source_name || '不使用数据源'}</dd>
            <dt>数据源修订</dt>
            <dd>{summary.inputs.data_source_revision || '—'}</dd>
            <dt>评测集</dt>
            <dd>{run.eval_set_name || '评测集快照'}</dd>
            <dt>用例修订</dt>
            <dd>{run.case_revision}</dd>
            <dt>问答模型</dt>
            <dd>{summary.model?.id || '—'}</dd>
            <dt>Judge</dt>
            <dd>
              {summary.judge?.model} / {summary.judge?.revision}
            </dd>
          </dl>
          {summary.reproducibility_note && <Alert tone="amber">{summary.reproducibility_note}</Alert>}
        </Modal>
      )}
      {publish && <Publication run={run} onClose={() => setPublish(false)} />}
    </>
  );
}

function AnswerDrawer({ run, caseId, cases, onChange, onClose, onSaved }) {
  const [answer, setAnswer] = useState(null),
    [loading, setLoading] = useState(true),
    [override, setOverride] = useState(false),
    [verdict, setVerdict] = useState('pass'),
    [reason, setReason] = useState('');
  const { busy, error, act } = useAction(),
    live = useRef(true);
  async function load() {
    const value = await api(`/runs/${run.id}/results/${caseId}/answer`);
    if (live.current) setAnswer(value);
  }
  useEffect(() => {
    live.current = true;
    act(async () => {
      await load();
      setLoading(false);
    });
    return () => {
      live.current = false;
    };
  }, [caseId]);
  const index = cases.findIndex((c) => String(c.id) === String(caseId)),
    canOverride = !ACTIVE.has(run.status) && !!answer?.answer;
  async function saveOverride() {
    if (!reason.trim()) throw new Error('请填写人工判定原因');
    await api(`/runs/${run.id}/results/${caseId}/override`, {
      verdict,
      reason: reason.trim(),
      revision: run.revision,
    });
    setOverride(false);
    setReason('');
    await load();
    await onSaved();
  }
  return (
    <Modal
      drawer
      title={`用例 ${Number(caseId) + 1} · 答案与判分`}
      onClose={onClose}
      footer={
        <>
          <span className="muted footer-hint">
            {Number(caseId) + 1} / {cases.length}
          </span>
          <Button disabled={index <= 0 || busy} onClick={() => onChange(cases[index - 1].id)}>
            上一条
          </Button>
          <Button
            disabled={index < 0 || index >= cases.length - 1 || busy}
            onClick={() => onChange(cases[index + 1].id)}
          >
            下一条
          </Button>
          <Button disabled={!canOverride} onClick={() => setOverride(true)}>
            人工改判
          </Button>
        </>
      }
    >
      {error && (
        <Alert
          tone="red"
          action={
            <Button busy={busy} onClick={() => act(load)}>
              重试
            </Button>
          }
        >
          {error}
        </Alert>
      )}
      {loading && !error ? (
        <Skeleton />
      ) : answer && (
        <>
          <div className="answer-block">
            <label>问题</label>
            <p>{answer.question}</p>
          </div>
          <div className="answer-block">
            <label>参考答案</label>
            <div className="answer-content">{answer.reference || '未填写参考答案，等待人工判定'}</div>
          </div>
          <div className="answer-block">
            <label>完整回答</label>
            <div className="answer-content">
              {answer.answer ||
                (answer.error?.message ? (
                  <span className="danger">未获得答案：{answer.error.message}</span>
                ) : (
                  '尚未获得答案'
                ))}
              {!answer.answer && ACTIVE.has(run.status) && (
                <Button variant="text" busy={busy} onClick={() => act(load)}>
                  刷新答案
                </Button>
              )}
            </div>
          </div>
          <div className="answer-block">
            <label>模型原判</label>
            {answer.judgement ? (
              <>
                <Badge tone={VERDICTS[answer.judgement.verdict]?.[1]}>
                  {VERDICTS[answer.judgement.verdict]?.[0]}
                </Badge>
                <p>{answer.judgement.reason}</p>
              </>
            ) : (
              <p>{VERDICTS[answer.status]?.[0] || '暂无判定'}</p>
            )}
          </div>
          {answer.override && (
            <div className="answer-block">
              <label>当前人工判定</label>
              <Badge tone={VERDICTS[answer.override.verdict]?.[1]}>
                {VERDICTS[answer.override.verdict]?.[0]}
              </Badge>
              <p>{answer.override.reason}</p>
            </div>
          )}
          {answer.overrides?.length > 0 && (
            <details className="audit-history">
              <summary>查看人工判定历史（{answer.overrides.length}）</summary>
              {answer.overrides.map((item, i) => (
                <article key={i}>
                  <Badge tone={VERDICTS[item.verdict]?.[1]}>{VERDICTS[item.verdict]?.[0]}</Badge>
                  <p>{item.reason}</p>
                  <small>
                    {item.actor} · {new Date(item.time * 1000).toLocaleString('zh-CN')}
                  </small>
                </article>
              ))}
            </details>
          )}
          {ACTIVE.has(run.status) && (
            <Alert title="任务仍在运行">可以查看已生成的答案。人工改判将在任务停止执行后开放。</Alert>
          )}
        </>
      )}
      {override && (
        <Modal
          title="人工改判"
          onClose={() => setOverride(false)}
          footer={
            <>
              <Button onClick={() => setOverride(false)}>取消</Button>
              <Button busy={busy} variant="primary" onClick={() => act(saveOverride)}>
                保存人工判定
              </Button>
            </>
          }
        >
          <p className="help-text">保留模型原判，并记录当前账号、原因与时间。</p>
          <Field label="本次判定 *">
            <select value={verdict} onChange={(e) => setVerdict(e.target.value)}>
              <option value="pass">通过</option>
              <option value="partial">部分通过</option>
              <option value="fail">未通过</option>
            </select>
          </Field>
          <Field label="判定原因 *">
            <textarea rows={4} maxLength={4000} value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
          {error && <Alert tone="red">{error}。输入已保留；如版本冲突，请刷新详情后重试。</Alert>}
        </Modal>
      )}
    </Modal>
  );
}

function Publication({ run, onClose }) {
  const { busy, error, act } = useAction(),
    [receipt, setReceipt] = useState(null),
    // A derived release is built on the release the user chose, not on the evaluation snapshot.
    { base, baseId } = packageVersions(run);
  async function publish() {
    const key = requestId('publish', run.preparation_id);
    const result = await api('/package-publications', {
      preparation_id: run.preparation_id,
      request_id: key.id,
    });
    key.done();
    setReceipt(result);
  }
  return (
    <Modal
      title="发布派生知识包版本"
      onClose={() => {
        if (!busy) onClose();
      }}
      footer={
        receipt ? (
          <Button onClick={onClose}>完成</Button>
        ) : (
          <>
            <Button disabled={busy} onClick={onClose}>
              取消
            </Button>
            <Button busy={busy} variant="primary" onClick={() => act(publish)}>
              确认发布
            </Button>
          </>
        )
      }
    >
      <dl className="snapshot">
        <dt>知识包</dt>
        <dd>{packageName(run)}</dd>
        <dt>来源版本</dt>
        <dd>{base || baseId || '—'}</dd>
        <dt>准备结果</dt>
        <dd>{run.preparation_id}</dd>
      </dl>
      {receipt ? (
        <Alert tone="green" title={`发布成功 · ${receipt.version}`}>
          Hub 正式版本回执：{receipt.release_id}
          <br />
          <code>{receipt.package_digest}</code>
        </Alert>
      ) : (
        <Alert tone="amber" title="确认创建 Hub 新版本">
          发布当前准备结果中的映射派生副本，不自动激活，不修改历史快照。
        </Alert>
      )}
      {error && <Alert tone="red">{error}。输入已保留，重试沿用同一发布请求。</Alert>}
    </Modal>
  );
}
