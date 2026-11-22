import React, { useEffect, useRef, useState } from 'react';
import { useViewState } from './viewState.js';
import { allPages, api } from './api.js';
import {
  VERDICTS, carriedState, feedbackStatus, handledText, minute, notificationState, optimizationState, runStartTime, shortId,
} from './domain.js';
import { ArtifactPanel, CarryoverNote, DeliveryContent, RecordNumber } from './RunOutputs.jsx';
import { Alert, Badge, Button, Empty, Field, Heading, Icon, Modal, Progress, Segmented, Skeleton, Steps, Tabs, useAction } from './ui.jsx';

const EGRESS =
  '优化建议由管理员维护的默认判分模型生成：问题、回答、工具调用记录和知识包文档会发给它的网关；提交反馈的用户不会显示，只显示人数。';
// Why feedback on a package version is left out of its optimization, as the report names them.
const EXCLUDED = {
  runtime_error: ['运行环境问题', '超时、服务不可用等，与知识包无关'],
  insufficient_info: ['信息不足', '只有 👎，没有达到人数或次数门槛'],
  misjudged: ['用户误判', '负责人标为误判，不进入优化建议'],
  resolved_by_newer_version: ['新版本已修复', '同一问题在更新的版本上没有再出现'],
};
// A package's feedback as a whole, as the list names it.
const PACKAGE_STATUS = {
  pending: ['待确认', 'amber'],
  delivering: ['交付中', 'blue'],
  notified: ['待建模处理', 'blue'],
  failed: ['处理失败', 'red'],
  done: ['已处理', 'green'],
  collecting: ['收集中', 'neutral'],
  idle: ['无待办', 'neutral'],
  sync_pending: ['待同步', 'amber'],
  sync_failed: ['同步失败', 'red'],
  no_action: ['无需优化', 'green'],
};
const FILTERS = {
  all: () => true,
  pending: (item) => ['pending', 'delivering'].includes(item.status),
  notified: (item) => ['notified', 'sync_pending', 'sync_failed'].includes(item.status),
  failed: (item) => item.status === 'failed',
  done: (item) => item.status === 'done',
};

function day(seconds) {
  return runStartTime({ created_at: seconds })?.date || '—';
}

function scope(text) {
  return text ? text.replace('operation:', '操作：').replace('entity:', '对象：') : '';
}

function counts(cases, proposals) {
  return `问题 ${cases || 0} · 建议 ${proposals || 0}`;
}

const awaiting = (run) => run.optimization?.status === 'awaiting_review';
const proposalsOf = (run) => run.feedback?.suggestions || [];
const packagePath = (id) => `/feedback/package/${encodeURIComponent(id)}`;
// The package version users asked on; feedback of different versions is delivered separately and never spliced.
const versionOf = (run) => (run.base_ref || run.package_ref)?.version || '';
const drafted = (run) =>
  run.artifacts?.suggestions === 'ready' && !run.artifacts?.suggestions_stale && run.artifacts?.status !== 'generating';

function Flow() {
  return (
    <p className="feedback-flow" aria-label="反馈处理流程">
      {[
        ['collecting', '系统汇集反馈'],
        ['pending', '负责人核对并交付'],
        ['notified', '建模按建议修改'],
        ['done', '建模发布新版本'],
      ].map(([key, text], i) => (
        <React.Fragment key={key}>
          {i > 0 && <Icon name="right" />}
          <span>
            <Badge tone={PACKAGE_STATUS[key][1]}>{PACKAGE_STATUS[key][0]}</Badge> {text}
          </span>
        </React.Fragment>
      ))}
    </p>
  );
}

function resultText(result) {
  if (!result) return null;
  const outcome = result.result || {};
  return outcome.outcome !== 'failed'
    ? [`已处理`, 'green', `${handledText(outcome)} · ${day(outcome.reported_at)}`]
    : ['处理失败', 'red', `${outcome.note || '建模处理失败'} · ${day(outcome.reported_at)}`];
}

// Packages with consumer feedback that the user may read in Hub, one row each, with what waits and what was handled.
export function FeedbackList({ navigate }) {
  const [packages, setPackages] = useState(null),
    [filter, setFilter] = useViewState('feedback_filter', 'all'),
    [search, setSearch] = useViewState('feedback_search', ''),
    [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    const load = () =>
      allPages('/package-tasks?origin=consumer_feedback')
        .then((items) => live && (setPackages(items), setError('')))
        .catch((e) => live && setError(e.message));
    load();
    const timer = setInterval(() => document.visibilityState === 'visible' && load(), 5000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, []);
  const list = (packages || []).map((item) => {
    const t = item.transport;
    const status = item.status !== 'notified' || !t ? item.status
      : t.failed ? 'sync_failed' : t.pending ? 'sync_pending' : t.delivered ? 'notified' : 'no_action';
    return { ...item, status };
  });
  const visible = list.filter(
    (item) =>
      (FILTERS[filter] || FILTERS.all)(item) && `${item.name || ''} ${item.ontology_id}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <>
      <Heading title="用户反馈">
        消费服务用户提交的错误问答和改进建议，按知识包汇总。经知识包负责人核对并交付后，由建模按优化建议修改知识包。
      </Heading>
      <Flow />
      {error && <Alert tone="red">{error}</Alert>}
      {!packages ? (
        !error && <Skeleton />
      ) : !list.length ? (
        <section className="panel">
          <Empty title="暂无用户反馈" icon="message">
            消费服务推送反馈后，这里按知识包显示。
          </Empty>
        </section>
      ) : (
        <>
          <div className="list-toolbar">
            <Segmented
              label="按状态筛选"
              value={filter}
              onChange={setFilter}
              items={[
                ['all', '全部', list.length],
                ['pending', '待确认', list.filter(FILTERS.pending).length],
                ['notified', '待建模处理', list.filter(FILTERS.notified).length],
                ['failed', '处理失败', list.filter(FILTERS.failed).length],
                ['done', '已处理', list.filter(FILTERS.done).length],
              ]}
            />
            <label className="search-input">
              <Icon name="search" />
              <input aria-label="搜索知识包" placeholder="搜索知识包名称或 ID" value={search} onChange={(e) => setSearch(e.target.value)} />
            </label>
          </div>
          <section className="panel table-panel">
            {!visible.length ? (
              <Empty
                title="没有符合条件的知识包"
                action={
                  <Button
                    onClick={() => {
                      setFilter('all');
                      setSearch('');
                    }}
                  >
                    清除筛选
                  </Button>
                }
              >
                调整筛选条件后再查看。
              </Empty>
            ) : (
              <div className="table-scroll">
                <table className="feedback-table">
                  <thead>
                    <tr>
                      <th>知识包</th>
                      <th>状态</th>
                      <th>待确认</th>
                      <th>交付与同步</th>
                      <th>最近处理结果</th>
                      <th>累计反馈</th>
                      <th>最近反馈时间</th>
                      <th className="col-actions">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((item) => {
                      const status = PACKAGE_STATUS[item.status] || PACKAGE_STATUS.idle,
                        result = resultText(item.latest_result),
                        pending = ['pending', 'delivering'].includes(item.status);
                      return (
                        <tr key={item.ontology_id} className="clickable" onClick={() => navigate(packagePath(item.ontology_id))}>
                          <td data-label="知识包">
                            <b className="package-name">{item.name || item.ontology_id}</b>
                            <small className="mono-id">{item.ontology_id}</small>
                          </td>
                          <td data-label="状态">
                            <Badge tone={status[1]}>{status[0]}</Badge>
                            {item.status === 'collecting' && <small>{item.collecting_clusters} 个问题待整理</small>}
                          </td>
                          <td data-label="待确认">
                            {item.awaiting_review ? (
                              <>
                                <b className="tabular nowrap">{counts(item.awaiting_cases, item.awaiting_suggestions)}</b>
                                <small className="nowrap">最早 {day(item.awaiting_since)} 收到</small>
                                <small className="nowrap">
                                  {{ ready: '优化建议已生成', generating: '优化建议生成中', failed: '优化建议生成失败' }[item.suggestions]}
                                </small>
                              </>
                            ) : (
                              <span className="muted">—</span>
                            )}
                          </td>
                          <td data-label="待建模处理" className="nowrap">
                            {item.handed ? (
                              <>
                                <span className="tabular">{item.handed} 次交付</span>
                                {item.transport ? <small>已送达 {item.transport.delivered} · 待同步 {item.transport.pending} · 同步失败 {item.transport.failed} · 无需优化 {item.transport.none}</small> : <small>{day(item.handed_since)} 起</small>}
                              </>
                            ) : (
                              <span className="muted">—</span>
                            )}
                          </td>
                          <td data-label="最近处理结果">
                            {result ? (
                              <>
                                <Badge tone={result[1]}>{result[0]}</Badge>
                                <small>{result[2]}</small>
                              </>
                            ) : (
                              <span className="muted">—</span>
                            )}
                          </td>
                          <td data-label="累计反馈" className="nowrap tabular">
                            {item.total_cases || item.total_suggestions ? counts(item.total_cases, item.total_suggestions) : <span className="muted">—</span>}
                          </td>
                          <td data-label="最近反馈时间" className="nowrap tabular">
                            {item.last_received ? (
                              <>
                                {day(item.last_received)}
                                <small>{minute(item.last_received).slice(11)}</small>
                              </>
                            ) : (
                              '—'
                            )}
                          </td>
                          <td className="col-actions">
                            <Button
                              variant={item.status === 'pending' ? 'primary' : ''}
                              onClick={(e) => {
                                e.stopPropagation();
                                navigate(packagePath(item.ontology_id));
                              }}
                            >
                              {pending ? '处理' : '查看'}
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
              <span>共 {visible.length} 个知识包</span>
              <span>只显示你在 Hub 上有权限的知识包</span>
            </footer>
          </section>
        </>
      )}
    </>
  );
}

// How the suggestions of the feedback waiting for confirmation stand, over all its batches.
function generation(pending, now) {
  const metas = pending.map((run) => run.artifacts || {});
  if (metas.some((a) => a.suggestions === 'failed'))
    return { state: 'failed', error: metas.find((a) => a.suggestions === 'failed')?.suggestions_error || '' };
  if (pending.every(drafted)) return { state: 'ready', at: Math.max(...metas.map((a) => a.generated_at || 0)) };
  // Batches written count whole; a batch being drafted counts the share of its model requests already answered.
  const done = pending.reduce((sum, run) => {
    if (drafted(run)) return sum + 1;
    const p = run.artifacts?.progress;
    return sum + (p?.batches ? (p.batch - 1) / p.batches : 0);
  }, 0);
  const started = metas.map((a) => a.progress?.started_at).filter(Boolean);
  return {
    state: 'generating',
    fraction: Math.max(0.05, done / pending.length),
    elapsed: started.length ? Math.max(0, now - Math.min(...started)) : null,
    queued: metas.some((a) => a.queue != null && a.queue > 0),
  };
}

function elapsedText(seconds) {
  const m = Math.floor(seconds / 60),
    s = Math.floor(seconds % 60);
  return m ? `${m} 分 ${s} 秒` : `${s} 秒`;
}

// All feedback of one package: what waits for confirmation, delivered as a whole, and every delivery so far.
export function FeedbackPackage({ ontologyId, navigate, onChanged }) {
  const [runs, setRuns] = useState(null),
    [cases, setCases] = useState({}),
    [tab, setTab] = useViewState('tab', 'pending'),
    [drawer, setDrawer] = useState(null),
    [recordId, setRecordId] = useViewState('record', ''),
    [editing, setEditing] = useState(null),
    [confirming, setConfirming] = useState(false),
    [delivered, setDelivered] = useState(null),
    [now, setNow] = useState(Date.now() / 1000),
    [fatal, setFatal] = useState(''),
    { busy, error, act } = useAction();
  const record = runs?.find((r) => r.id === recordId) || null;
  const setRecord = (value) => {
    if (value?.id !== recordId) {
      const [path, search = ''] = location.hash.split('?');
      const query = new URLSearchParams(search); query.delete('detail_tab');
      history.replaceState(history.state, '', `${path}${query.size ? `?${query}` : ''}`);
    }
    setRecordId(value?.id || '');
  };
  const loaded = useRef({}),
    requested = useRef(false);
  async function refresh() {
    const list = await allPages(`/runs?origin=consumer_feedback&ontology_id=${encodeURIComponent(ontologyId)}`);
    const next = {};
    for (const run of list.filter(awaiting)) {
      const known = loaded.current[run.id];
      // Questions are read again only after a correction or a new draft changed them.
      next[run.id] =
        known && known.revision === run.revision ? known : { revision: run.revision, items: await allPages(`/runs/${run.id}/results`) };
    }
    loaded.current = next;
    setCases(next);
    setRuns(list);
    setNow(Date.now() / 1000);
    const waiting = list.filter(awaiting);
    // An owner asked to deliver and nothing waits any more: the delivery went out.
    if (requested.current && !waiting.length) {
      const newest = list.filter((run) => run.optimization?.notified_at).sort((a, b) => b.optimization.notified_at - a.optimization.notified_at)[0];
      setDelivered(newest || null);
      onChanged?.();
    }
    requested.current = waiting.some((run) => run.optimization?.handover);
  }
  useEffect(() => {
    refresh().catch((e) => setFatal(e.status === 404 ? '没有这个知识包的反馈，或你在 Hub 上没有这个知识包的权限' : e.message));
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') refresh().catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, [ontologyId]);
  if (fatal)
    return (
      <Empty title={fatal} icon="message">
        只有在 Hub 上有该知识包权限的用户可以查看。
      </Empty>
    );
  if (!runs) return <Skeleton />;
  // Oldest first: delivering in this order lets the newest batch carry the others to modeling.
  const pending = runs.filter(awaiting).sort((a, b) => (a.created_at || 0) - (b.created_at || 0)),
    collecting = runs.find((run) => run.status === 'collecting'),
    handed = runs.filter((run) => run.optimization?.status === 'notified' && !run.optimization?.carried_into),
    records = runs
      .filter((run) => ['notified', 'modeling_done', 'modeling_failed', 'dismissed'].includes(run.optimization?.status))
      .sort((a, b) => deliveredAt(b) - deliveredAt(a)),
    name = runs[0]?.package_ref?.name || ontologyId;
  const questions = pending.flatMap((run) => (cases[run.id]?.items || []).map((item, index) => ({ run, item, index }))),
    proposals = pending.flatMap((run) => proposalsOf(run).map((item) => ({ run, item }))),
    gen = pending.length ? generation(pending, now) : null,
    // Asked to deliver and still waiting for the suggestions; a failed generation needs the owner again.
    delivering = pending.some((run) => run.optimization?.handover) && gen?.state !== 'failed',
    checked = questions.filter(({ item }) => item.override).length,
    versions = [...new Set(pending.map(versionOf))],
    // Only earlier deliveries of the versions now delivered ride along.
    carriedRuns = handed.filter((run) => versions.includes(versionOf(run))),
    status = delivering ? 'delivering' : pending.length ? 'pending'
      : handed.length ? handed.every((run) => run.delivery?.state === 'none') ? 'no_action'
        : handed.some((run) => ['failed', 'rejected'].includes(run.notification?.status)) ? 'sync_failed'
        : handed.some((run) => run.notification?.status !== 'delivered' && run.delivery?.state !== 'none') ? 'sync_pending' : 'notified'
      : collecting ? 'collecting' : 'idle',
    totalCases = runs.filter((run) => run.status === 'completed').reduce((sum, run) => sum + (run.case_count || 0), 0),
    totalProposals = runs.reduce((sum, run) => sum + proposalsOf(run).length, 0);
  const deliver = () =>
    act(async () => {
      for (const run of pending) await api(`/runs/${run.id}/notifications/retry`, { decision: 'confirm' });
      setConfirming(false);
      requested.current = true;
      await refresh();
      onChanged?.();
    });
  const retry = () =>
    act(async () => {
      for (const run of pending.filter((run) => run.artifacts?.suggestions === 'failed'))
        await api(`/runs/${run.id}/artifacts/regenerate`, {});
      await refresh();
    });
  const seal = () => act(async () => (await api(`/runs/${collecting.id}/artifacts/regenerate`, {}), refresh()));
  const steps = gen && [
    {
      label: '生成优化建议（自动）',
      hint:
        gen.state === 'ready'
          ? `已生成：${minute(gen.at)}`
          : gen.state === 'failed'
            ? `生成失败：${gen.error || '请重新生成'}`
            : gen.elapsed != null
              ? `生成中：已用时 ${elapsedText(gen.elapsed)}`
              : '排队中，马上开始生成',
      state: gen.state === 'ready' ? 'done' : gen.state === 'failed' ? 'failed' : 'current',
    },
    {
      label: '核对问题（可选）',
      hint: checked ? `已核对 ${checked} 条，改过的部分自动重新生成` : '可补充标准答案或标记误判',
      state: checked ? 'done' : '',
    },
    {
      label: '交付建模',
      hint: delivering ? '优化建议生成完成后自动交付' : '交付后不能再修改',
      state: delivering || gen.state === 'ready' ? 'current' : '',
    },
  ];
  return (
    <>
      <Heading
        title={
          <>
            {name} <Badge tone={PACKAGE_STATUS[status][1]}>{PACKAGE_STATUS[status][0]}</Badge>
          </>
        }
      >
        <span className="mono-id">{ontologyId}</span> · 累计问题 {totalCases} 条、建议 {totalProposals} 条
      </Heading>
      {error && <Alert tone="red">{error}</Alert>}
      <Tabs
        label="用户反馈"
        value={tab}
        onChange={setTab}
        items={[
          ['pending', '待确认', questions.length + proposals.length],
          ['records', '交付记录', records.length],
        ]}
      />
      {tab === 'pending' && (
        <>
          {delivered && !pending.length && (
            <Alert
              tone="green"
              title="已交付建模"
              action={<Button onClick={() => setTab('records')}>查看交付记录</Button>}
            >
              {minute(delivered.optimization.notified_at)} 交付。建模发布新版本后，「交付记录」中自动显示处理结果。
            </Alert>
          )}
          {pending.length > 0 ? (
            <section className="panel deliver-card">
              <div className="deliver-head">
                <div>
                  <h3>
                    待确认：问题 {questions.length} 条 · 建议 {proposals.length} 条
                  </h3>
                  <p>
                    收到时间 {day(pending[0].feedback?.window?.from || pending[0].created_at)} 至{' '}
                    {day(pending.at(-1).feedback?.window?.to || pending.at(-1).finished_at)}。
                    {versions.length > 1
                      ? `涉及版本 ${versions.join('、')}，按版本分别交付建模。`
                      : `知识包版本 ${versions[0] || '—'}。`}
                    {carriedRuns.length > 0 &&
                      `同版本有 ${carriedRuns.length} 次历史交付可能纳入，去重后的内容可在交付前预览。`}
                  </p>
                </div>
                <div className="actions">
                  {delivering ? (
                    <span className="button primary busy" role="status">
                      <Icon name="refresh" className="icon spin" />
                      交付中…
                    </span>
                  ) : (
                    <>
                      {gen.state === 'failed' && (
                        <Button busy={busy} onClick={retry}>
                          重新生成
                        </Button>
                      )}
                      <Button variant="primary" icon="arrow" disabled={busy} onClick={() => setConfirming(true)}>
                        交付建模
                      </Button>
                    </>
                  )}
                </div>
              </div>
              <Steps items={steps} />
              {gen.state === 'generating' && (
                <div className="deliver-progress">
                  <Progress value={gen.fraction} label="优化建议生成进度" />
                  <span>
                    {delivering
                      ? '已确认交付：优化建议生成完成后自动交付建模，通常需要 2–5 分钟，可以离开此页面。'
                      : '正在生成优化建议，通常需要 2–5 分钟。现在点「交付建模」也可以，生成完成后自动交付。'}
                  </span>
                </div>
              )}
              {gen.state === 'failed' && (
                <Alert tone="red">
                  优化建议生成失败：{gen.error || '判分模型没有返回结果'}。点「重新生成」重试，或请管理员检查默认判分模型；点「交付建模」会先重新生成，完成后自动交付。
                </Alert>
              )}
            </section>
          ) : (
            !delivered && (
              <Alert tone="blue" title="暂无待确认的反馈">
                {handed.length ? `已交付 ${handed.length} 次，等建模处理。` : '新的反馈整理完成后会出现在这里。'}
              </Alert>
            )
          )}
          {collecting && (
            <Alert
              action={
                <Button disabled={busy || delivering} onClick={seal}>
                  立即整理
                </Button>
              }
            >
              另有 {collecting.feedback?.clusters?.length || 0} 个问题正在收集（最近{' '}
              {minute(collecting.feedback?.last_included || collecting.created_at)} 收到），满 10 个问题或 60
              分钟没有新反馈时自动整理，加入待确认。
            </Alert>
          )}
          {questions.length > 0 && (
            <>
              <h2 className="section-heading">
                问题（{questions.length}）<small>点击行查看用户提问时的回答和工具失败</small>
              </h2>
              <section className="panel table-panel">
                <div className="table-scroll">
                  <table className="question-table">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>问题</th>
                        <th>判定</th>
                        <th>来源</th>
                        <th>用户数</th>
                        <th>收到日期</th>
                        <th>标准答案</th>
                        <th className="col-actions">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {questions.map(({ run, item, index }, i) => {
                        const verdict = item.override?.verdict || item.judgement?.verdict,
                          meta = item.feedback_meta || {};
                        return (
                          <tr key={`${run.id}-${item.id}`} className="clickable" onClick={() => setDrawer(i)}>
                            <td className="case-index">{i + 1}</td>
                            <td>
                              <b className="question-text">{item.question}</b>
                              <small>{(item.override || item.judgement)?.reason}</small>
                            </td>
                            <td className="nowrap">
                              <Badge tone={verdict === 'pass' ? 'green' : VERDICTS[verdict]?.[1]}>
                                {verdict === 'pass' ? '用户误判' : VERDICTS[verdict]?.[0]}
                              </Badge>
                            </td>
                            <td className="nowrap">{meta.kind || '—'}</td>
                            <td className="tabular">{meta.users || 1}</td>
                            <td className="nowrap tabular">{day(run.created_at)}</td>
                            <td className="nowrap">
                              {item.override?.reason === '负责人补充标准答案' ? <Badge tone="green">已补充</Badge> : <span className="muted">未提供</span>}
                            </td>
                            <td className="col-actions">
                              {delivering ? (
                                <span className="muted">交付中</span>
                              ) : (
                                <EditButtons
                                  verdict={verdict}
                                  onEdit={(mode) => setEditing({ run, index, mode, question: item.question })}
                                />
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </section>
            </>
          )}
          {proposals.length > 0 && (
            <>
              <h2 className="section-heading">用户建议（{proposals.length}）</h2>
              <ProposalTable rows={proposals} />
            </>
          )}
          {gen?.state === 'ready' && (
            <>
              <h2 className="section-heading">
                优化建议<small>反馈整理完成后自动生成，交付时一起发给建模</small>
              </h2>
              <Alert>{EGRESS}</Alert>
              {pending.map((run) => (
                <ArtifactPanel key={run.id} run={run} kind="suggestions" ready />
              ))}
            </>
          )}
        </>
      )}
      {tab === 'records' && (
        <>
          <p className="muted section-lead">
            每次交付建模的内容和建模处理结果。之前交付、建模还没处理的内容，会在同一版本的下一次交付时一并带上，原记录显示“已合并”；不同版本的反馈分别交付。
          </p>
          <section className="panel table-panel">
            {!records.length ? (
              <Empty title="暂无交付记录" icon="message">
                交付建模后在这里查看处理结果。
              </Empty>
            ) : (
              <div className="table-scroll">
                <table className="record-table">
                  <thead>
                    <tr>
                      <th>编号</th>
                      <th>交付时间</th>
                      <th>内容</th>
                      <th>知识包版本</th>
                      <th>同步状态</th>
                      <th>建模处理</th>
                      <th className="col-actions">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {records.map((run) => {
                      const handling = recordState(run, records.map((r) => r.id)),
                        carry = run.artifacts?.carryover?.rounds || [];
                      return (
                        <tr key={run.id} className="clickable" onClick={() => setRecord(run)}>
                          <td className="nowrap">
                            <RecordNumber id={run.id} peers={records.map((r) => r.id)} />
                          </td>
                          <td className="nowrap tabular">{minute(deliveredAt(run))}</td>
                          <td>
                            <b className="tabular">{counts(run.case_count, proposalsOf(run).length)}</b>
                            {carry.length > 0 && (
                              <small>一并带上之前 {carry.length} 次交付的问题 {carry.reduce((sum, r) => sum + (r.cases || 0), 0)} 条</small>
                            )}
                            <small>
                              {day(run.feedback?.window?.from || run.created_at)} 至 {day(run.feedback?.window?.to || run.finished_at)} 收到
                            </small>
                          </td>
                          <td>
                            <span className="version-tag">{run.package_ref?.version || '—'}</span>
                          </td>
                          <td><Badge tone={notificationState(run.notification)?.tone || 'neutral'}>{notificationState(run.notification)?.label || '未同步'}</Badge></td>
                          <td>
                            <Badge tone={handling[1]}>{handling[0]}</Badge>
                            {handling[2] && <small>{handling[2]}</small>}
                            {run.optimization?.carried_into && <Button variant="text" onClick={(e) => { e.stopPropagation(); navigate(`/feedback/${run.optimization.carried_into.run_id}?tab=delivery`); }}>查看承接交付</Button>}
                          </td>
                          <td className="col-actions">
                            <Button
                              onClick={(e) => {
                                e.stopPropagation();
                                setRecord(run);
                              }}
                            >
                              详情
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
        </>
      )}
      {drawer !== null && questions[drawer] && (
        <QuestionDrawer
          entry={questions[drawer]}
          number={drawer + 1}
          total={questions.length}
          locked={delivering}
          onMove={(step) => setDrawer(drawer + step)}
          onEdit={(mode) => setEditing({ ...questions[drawer], mode, question: questions[drawer].item.question })}
          onClose={() => setDrawer(null)}
        />
      )}
      {record && (
        <Modal drawer title={`交付详情 · ${shortId(record.id)}`} onClose={() => setRecord(null)}>
          <RunContent key={record.id} run={record} handling={recordState(record)} navigate={navigate} />
        </Modal>
      )}
      {editing && (
        <CaseEdit
          run={editing.run}
          index={editing.index}
          mode={editing.mode}
          question={editing.question}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await refresh();
          }}
        />
      )}
      {confirming && (
        <Modal
          title="确认交付建模"
          onClose={() => !busy && setConfirming(false)}
          footer={
            <>
              <Button disabled={busy} onClick={() => setConfirming(false)}>
                取消
              </Button>
              <Button variant="primary" busy={busy} onClick={deliver}>
                确认交付
              </Button>
            </>
          }
        >
          <p>交付后这些反馈进入建模的待处理列表，不能再修改。</p>
          <dl className="snapshot">
            <dt>本次交付</dt>
            <dd>
              问题 {questions.length} 条、建议 {proposals.length} 条
            </dd>
            <dt>知识包版本</dt>
            <dd>{versions.length > 1 ? `${versions.join('、')}（按版本分别交付）` : versions[0] || '—'}</dd>
            {carriedRuns.length > 0 && <><dt>历史交付</dt><dd>{carriedRuns.length} 次可能纳入，去重后的内容见下方预览。</dd></>}
            <dt>优化建议</dt>
            <dd>
              {gen.state === 'ready'
                ? '已生成，确认后立即交付'
                : gen.state === 'failed'
                  ? '上次生成失败：将重新生成，完成后自动交付，期间可以离开页面'
                  : '生成中：完成后自动交付，期间可以离开页面'}
            </dd>
          </dl>
          {pending.map((run) => <DeliveryContent key={run.id} run={run} navigate={navigate} />)}
          {error && <Alert tone="red">{error}</Alert>}
        </Modal>
      )}
    </>
  );
}

function deliveredAt(run) {
  const o = run.optimization || {};
  return o.notified_at || o.updated_at || run.finished_at || 0;
}

// What modeling did with one delivery, told by delivery times rather than IDs.
function recordState(run, peers = []) {
  const o = run.optimization || {},
    result = o.result || {},
    via = result.via;
  if (o.status === 'dismissed') return ['已驳回', 'neutral', o.reason ? `原因：${o.reason}` : ''];
  if (o.status === 'notified') { const state = optimizationState(run, peers); return [state.label, state.tone, state.hint]; }
  const together = result.via ? `随 ${shortId(result.via)} 一起处理 · ` : '';
  if (o.status === 'modeling_done')
    return ['已处理', 'green', `${together}${handledText(result)} · ${day(result.reported_at)}`];
  return ['处理失败', 'red', `${together}${via ? '' : result.note || '建模处理失败'}${via ? '' : ' · '}${day(result.reported_at)}`];
}

function EditButtons({ verdict, onEdit }) {
  return (
    <>
      <Button
        variant="text"
        onClick={(e) => {
          e.stopPropagation();
          onEdit('reference');
        }}
      >
        补充标准答案
      </Button>
      {verdict !== 'pass' && (
        <Button
          variant="text"
          onClick={(e) => {
            e.stopPropagation();
            onEdit('misjudged');
          }}
        >
          标记误判
        </Button>
      )}
    </>
  );
}

function ProposalTable({ rows }) {
  return (
    <section className="panel table-panel">
      <div className="table-scroll">
        <table className="plain-table">
          <thead>
            <tr>
              <th>#</th>
              <th>建议内容</th>
              <th>关联</th>
              <th>用户数</th>
              <th>收到日期</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ run, item }, i) => (
              <tr key={`${run.id}-${item.cluster_id}`}>
                <td className="case-index">{i + 1}</td>
                <td>{item.content}</td>
                <td>{item.scope ? <Badge>{scope(item.scope)}</Badge> : <span className="muted">—</span>}</td>
                <td className="tabular">{item.users}</td>
                <td className="nowrap tabular">{day(run.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// A question users reported, with the answer they got, its reference and tool failures.
function QuestionDrawer({ entry, number, total, locked, onMove, onEdit, onClose }) {
  const { run, item, index } = entry,
    [answer, setAnswer] = useState(null),
    [error, setError] = useState('');
  const verdict = item.override?.verdict || item.judgement?.verdict,
    meta = item.feedback_meta || {};
  useEffect(() => {
    let live = true;
    setAnswer(null);
    setError('');
    api(`/runs/${run.id}/results/${index}/answer`)
      .then((value) => live && setAnswer(value))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [run.id, index, run.revision]);
  const failures = answer?.trace?.tool_failures || [];
  return (
    <Modal
      drawer
      title={
        <>
          问题 {number} / {total}{' '}
          <Badge tone={verdict === 'pass' ? 'green' : VERDICTS[verdict]?.[1]}>{verdict === 'pass' ? '用户误判' : VERDICTS[verdict]?.[0]}</Badge>
        </>
      }
      onClose={onClose}
      footer={
        <>
          <Button icon="left" disabled={number <= 1} onClick={() => onMove(-1)}>
            上一条
          </Button>
          <Button disabled={number >= total} onClick={() => onMove(1)}>
            下一条
            <Icon name="right" />
          </Button>
          <span className="footer-hint" />
          {locked ? <span className="muted">交付中，暂不能修改</span> : <EditButtons verdict={verdict} onEdit={onEdit} />}
        </>
      }
    >
      <div className="answer-block">
        <label>问题</label>
        <p className="answer-question">{item.question}</p>
        {meta.original_question && <p className="muted">用户原话：{meta.original_question}</p>}
      </div>
      <div className="answer-block">
        <label>来源</label>
        <p>
          {meta.kind || '用户反馈'} · {meta.users || 1} 位用户 · {day(run.created_at)} 收到
        </p>
      </div>
      <div className="answer-block">
        <label>依据</label>
        <p>{(item.override || item.judgement)?.reason || '—'}</p>
      </div>
      {error ? (
        <Alert tone="red">{error}</Alert>
      ) : !answer ? (
        <Skeleton />
      ) : (
        <>
          <div className="answer-block">
            <label>用户提问时的回答</label>
            <div className="answer-content">{answer.answer || '（无）'}</div>
          </div>
          <div className="answer-block">
            <label>标准答案</label>
            <div className="answer-content">{answer.reference || <span className="muted">未提供</span>}</div>
          </div>
          {failures.length > 0 && (
            <div className="answer-block">
              <label>工具失败</label>
              <ul className="tool-failures">
                {failures.map((failure, i) => (
                  <li key={i}>
                    <code>{failure.tool_name || '未知工具'}</code> {failure.error}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </Modal>
  );
}

// What one delivery handed to modeling: its questions, user suggestions, report, suggestions and what was left out.
function RunContent({ run, handling, navigate }) {
  const [tab, setTab] = useViewState('detail_tab', ['delivery', 'report', 'advice'].includes(new URLSearchParams(location.hash.split('?')[1] || '').get('tab')) ? new URLSearchParams(location.hash.split('?')[1] || '').get('tab') : 'cases'),
    [items, setItems] = useState(null);
  useEffect(() => {
    let live = true;
    if (run.status === 'completed')
      allPages(`/runs/${run.id}/results`)
        .then((value) => live && setItems(value))
        .catch(() => live && setItems([]));
    else setItems([]);
    return () => {
      live = false;
    };
  }, [run.id, run.revision]);
  const a = run.artifacts || {},
    proposals = proposalsOf(run),
    excluded = Object.entries(run.feedback?.excluded || {}).filter(([, count]) => count);
  return (
    <>
      <dl className="snapshot">
        <dt>编号</dt>
        <dd>
          <code>{run.id}</code>
        </dd>
        <dt>交付时间</dt>
        <dd>{run.optimization?.notified_at ? minute(run.optimization.notified_at) : '—'}</dd>
        <dt>本轮内容</dt>
        <dd>{counts(run.case_count, proposals.length)}</dd>
        <dt>同步状态</dt>
        <dd>{notificationState(run.notification)?.label || '未同步'}</dd>
        {run.delivery && <><dt>历史内容</dt><dd>问题 {run.delivery.history.cases} 条 · 优化建议 {run.delivery.history.items} 条</dd></>}
        <dt>收到时间</dt>
        <dd>
          {day(run.feedback?.window?.from || run.created_at)} 至 {day(run.feedback?.window?.to || run.finished_at)}
        </dd>
        <dt>知识包版本</dt>
        <dd>{run.package_ref?.version || '—'}</dd>
        {handling && (
          <>
            <dt>建模处理</dt>
            <dd>
              <Badge tone={handling[1]}>{handling[0]}</Badge> {handling[2]}
            </dd>
          </>
        )}
      </dl>
      <CarryoverNote carryover={a.carryover} feedback navigate={navigate} />
      <Tabs
        label="交付内容"
        value={tab}
        onChange={setTab}
        items={[
          ['cases', '本轮问题', run.case_count || undefined],
          ['suggestions', '用户建议', proposals.length || undefined],
          ['delivery', '全部交付内容'],
          ['report', '报告'],
          ['advice', '优化建议'],
          ['excluded', '未纳入', excluded.reduce((sum, [, count]) => sum + count, 0) || undefined],
        ]}
      />
      {tab === 'cases' &&
        (!items ? (
          <Skeleton />
        ) : !items.length ? (
          <Empty title="没有错误问答" icon="message">
            优化依据是用户建议。
          </Empty>
        ) : (
          <table className="plain-table">
            <thead>
              <tr>
                <th>#</th>
                <th>问题</th>
                <th>判定</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, i) => {
                const verdict = item.override?.verdict || item.judgement?.verdict;
                return (
                  <tr key={item.id}>
                    <td className="case-index">{i + 1}</td>
                    <td>{item.question}</td>
                    <td>
                      <Badge tone={verdict === 'pass' ? 'green' : VERDICTS[verdict]?.[1]}>
                        {verdict === 'pass' ? '用户误判' : VERDICTS[verdict]?.[0]}
                      </Badge>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ))}
      {tab === 'suggestions' &&
        (!proposals.length ? (
          <Empty title="没有用户建议" icon="message">
            用户在消费服务里提交的建议会显示在这里。
          </Empty>
        ) : (
          <ProposalTable rows={proposals.map((item) => ({ run, item }))} />
        ))}
      {tab === 'delivery' && <DeliveryContent run={run} navigate={navigate} />}
      {tab === 'report' && <ArtifactPanel run={run} kind="report" ready={a.report === 'ready'} waiting="报告生成中。" />}
      {tab === 'advice' && (
        <ArtifactPanel
          run={run}
          kind="suggestions"
          ready={a.suggestions === 'ready'}
          waiting={a.suggestions === 'generating' ? '优化建议生成中，生成后这里自动显示。' : a.suggestions_error || '优化建议还没有生成。'}
        />
      )}
      {tab === 'excluded' &&
        (!excluded.length ? (
          <Empty title="没有未纳入的反馈" icon="message">
            这个版本上的反馈都已纳入。
          </Empty>
        ) : (
          <table className="plain-table">
            <thead>
              <tr>
                <th>原因</th>
                <th>条数</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {excluded.map(([key, count]) => (
                <tr key={key}>
                  <td>{EXCLUDED[key]?.[0] || key}</td>
                  <td className="tabular">{count}</td>
                  <td className="muted">{EXCLUDED[key]?.[1] || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </>
  );
}

// One batch of feedback opened by its address, as earlier links and notifications name it.
export function FeedbackRun({ id, navigate }) {
  const [run, setRun] = useState(null);
  useEffect(() => {
    let live = true;
    const load = () =>
      api(`/runs/${id}`)
        .then((value) => live && setRun(value))
        .catch(() => live && setRun(false));
    load();
    const timer = setInterval(load, 5000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [id]);
  if (run === false)
    return (
      <Empty title="这条反馈记录不存在或无权访问" icon="message">
        只有在 Hub 上有该知识包权限的用户可以查看。
      </Empty>
    );
  if (!run) return <Skeleton />;
  const ontologyId = run.package_ref?.ontology_id,
    state = feedbackStatus(run),
    open = run.status === 'collecting' || awaiting(run),
    handled = optimizationState(run);
  return (
    <>
      <Heading
        title={
          <>
            {run.package_ref?.name || ontologyId} <Badge tone={state.tone}>{state.label}</Badge>
          </>
        }
        actions={<Button onClick={() => navigate(packagePath(ontologyId))}>这个知识包的全部反馈</Button>}
      >
        {day(run.feedback?.window?.from || run.created_at)} 至 {day(run.feedback?.window?.to || run.finished_at)} 收到 · 编号{' '}
        {shortId(run.id)}
      </Heading>
      {open && (
        <Alert
          tone="amber"
          title={run.status === 'collecting' ? '还在收集' : '还没交付'}
          action={
            <Button variant="primary" onClick={() => navigate(packagePath(ontologyId))}>
              去处理
            </Button>
          }
        >
          这个知识包的反馈在一个页面里统一核对和交付。
        </Alert>
      )}
      {!open && handled && <Alert tone={handled.tone === 'neutral' ? 'blue' : handled.tone}>{handled.label}：{handled.hint}</Alert>}
      <section className="panel">
        <RunContent run={run} handling={null} navigate={navigate} />
      </section>
    </>
  );
}

function CaseEdit({ run, index, mode, question, onClose, onSaved }) {
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
      title={misjudged ? '标记为用户误判' : '补充标准答案'}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" busy={busy} onClick={save}>
            {misjudged ? '标记' : '保存'}
          </Button>
        </>
      }
    >
      {question && <p>问题：{question}</p>}
      {error && <Alert tone="red">{error}</Alert>}
      <Field
        label={misjudged ? '原因 *' : '标准答案 *'}
        hint={
          misjudged
            ? '标记后这条问题不进入优化建议，它所在部分的优化建议自动重新生成'
            : '最多 4000 字；保存后这条问题判定为“未通过”，它所在部分的优化建议自动重新生成'
        }
      >
        <textarea rows={misjudged ? 4 : 6} maxLength={4000} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
    </Modal>
  );
}
