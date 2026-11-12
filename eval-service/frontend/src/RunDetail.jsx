import React, { useEffect, useRef, useState } from 'react';
import { allPages, api, requestId } from './api.js';
import {
  ACTIVE,
  ASSERTION_TITLES,
  ENDED,
  PHASES,
  RUN_TYPES,
  STATUS,
  VERDICTS,
  assertionNote,
  canResume,
  caseState,
  duration,
  isSolverRun,
  packageName,
  packageVersions,
  passRate,
  resultCounts,
  runStartTime,
  scoreText,
  shortId,
} from './domain.js';
import {
  Alert,
  Badge,
  Button,
  Empty,
  Heading,
  Icon,
  Modal,
  Progress,
  RESULT_PARTS,
  ResultBar,
  Segmented,
  Skeleton,
  Steps,
  Tabs,
  useAction,
} from './ui.jsx';
import { ArtifactPanel, DeliveryCard, RunHistory } from './RunOutputs.jsx';
import { RowMenu } from './TaskCenter.jsx';

const RUNNING_CASE = ['executing', 'unknown', 'answer_ready'];
// Result parts and the case filter each one opens.
const PART_FILTER = { pass: 'pass', partial: 'partial', fail: 'fail', error: 'errors', manual: 'manual', todo: 'pending' };

function caseMatches(item, filter) {
  const state = caseState(item);
  if (filter === 'all') return true;
  if (filter === 'active') return RUNNING_CASE.includes(state);
  if (filter === 'errors') return ['execution_error', 'judge_error'].includes(state);
  if (filter === 'pending') return ['pending', 'cancelled'].includes(state);
  return state === filter;
}

export function RunDetail({ id, navigate, onAction, onChanged }) {
  const [run, setRun] = useState(null),
    [cases, setCases] = useState([]),
    [loading, setLoading] = useState(true),
    [syncError, setSyncError] = useState(''),
    [fatal, setFatal] = useState(''),
    [lastSync, setLastSync] = useState(null);
  const [tab, setTab] = useState('cases'),
    [filter, setFilter] = useState('all'),
    [search, setSearch] = useState(''),
    [limit, setLimit] = useState(20),
    [selected, setSelected] = useState(null),
    [publish, setPublish] = useState(false),
    [copyMessage, setCopyMessage] = useState(''),
    [historyTotal, setHistoryTotal] = useState(null);
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
    counts = resultCounts(summary);
  const visible = cases.filter((c) => caseMatches(c, filter) && [c.question, c.name, c.case_id, c.solver_key].filter(Boolean).join(' ').toLowerCase().includes(search.toLowerCase()));
  function showCases(value) {
    setTab('cases');
    setFilter(value);
    setLimit(20);
  }
  function locate() {
    const index = cases.findIndex((c) => RUNNING_CASE.includes(c.status));
    if (index < 0) return;
    setTab('cases');
    setFilter('all');
    setSearch('');
    setLimit(Math.max(limit, Math.ceil((index + 1) / 20) * 20));
    requestAnimationFrame(() =>
      requestAnimationFrame(() =>
        rowRefs.current.get(cases[index].id)?.scrollIntoView({
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
  if (loading && !run) return <Skeleton />;
  if (fatal) return <Empty title={fatal}>请确认登录账号和任务地址。</Empty>;
  if (!run)
    return (
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
    );
  const phaseIndex = run.phase === 'finished' ? 4 : PHASES.findIndex(([key]) => key === run.phase);
  const solver = isSolverRun(run);
  const current = cases.find((c) => RUNNING_CASE.includes(c.status));
  const versions = packageVersions(run),
    start = runStartTime(run),
    took = !active && duration(run),
    artifacts = run.artifacts || {},
    done = run.optimization?.status === 'modeling_done';
  const recoverable = canResume(run),
    stoppable = !ENDED.has(run.status) && run.status !== 'cancelling' && !recoverable;
  const next = [];
  if (recoverable && counts.todo)
    next.push([
      `${counts.todo} 条还没执行`,
      run.status === 'waiting_auth' ? '授权并继续' : '恢复并继续',
      () => onAction('resume', run, refresh),
    ]);
  if (counts.manual) next.push([`${counts.manual} 条没有参考答案`, '去判定', () => showCases('manual')]);
  if (counts.error) next.push([`${counts.error} 条执行或判分异常`, '查看', () => showCases('errors')]);
  if (counts.fail + counts.partial)
    next.push(
      artifacts.suggestions === 'ready'
        ? [`${counts.fail + counts.partial} 条没有通过`, solver ? '看修复指引' : '看优化建议', () => setTab('suggestions')]
        : [`${counts.fail + counts.partial} 条没有通过`, '查看', () => showCases('fail')],
    );
  if (done)
    next.push([
      `建模已发布 ${run.optimization.result?.version || '新版本'}`,
      '在新版本再次评测',
      () => navigate(`/new?from=${id}&release_id=${encodeURIComponent(run.optimization.result.release_id)}`),
    ]);
  const excluded = [counts.manual && `待人工 ${counts.manual}`, counts.error && `异常 ${counts.error}`, counts.todo && `未执行 ${counts.todo}`]
    .filter(Boolean)
    .join('、');
  return (
    <>
      <Heading
        title={
          <>
            {packageName(run)} <span className="version-tag">{versions.base || '—'}</span>
            <Badge tone={STATUS[run.status]?.[1]}>{STATUS[run.status]?.[0] || run.status}</Badge>
            {solver && <Badge tone="blue">{RUN_TYPES.solver}</Badge>}
          </>
        }
        actions={
          <>
            {stoppable ? (
              <Button variant="danger" icon="stop" onClick={() => onAction('cancel', run, refresh)}>
                停止评测
              </Button>
            ) : recoverable ? (
              <Button variant="primary" icon="play" onClick={() => onAction('resume', run, refresh)}>
                {run.status === 'waiting_auth' ? '授权并继续' : '恢复并继续'}
              </Button>
            ) : (
              ENDED.has(run.status) && (
                <Button variant="primary" icon="refresh" onClick={() => navigate(`/new?from=${id}`)}>
                  再次评测
                </Button>
              )
            )}
            <RowMenu label="更多操作">
              <button onClick={copyLink}>
                <Icon name="copy" />
                复制详情链接
              </button>
              {ENDED.has(run.status) && !recoverable && (
                <button onClick={() => navigate(`/new?from=${id}`)}>
                  <Icon name="refresh" />
                  再次评测
                </button>
              )}
              {run.mapping && (
                <button onClick={() => setPublish(true)}>
                  <Icon name="box" />
                  发布派生版本
                </button>
              )}
              {!ACTIVE.has(run.status) && (
                <button className="danger" onClick={() => onAction('delete', run, refresh)}>
                  <Icon name="trash" />
                  删除记录
                </button>
              )}
            </RowMenu>
          </>
        }
      >
        {[
          `评测集「${run.eval_set_name || '评测集快照'}」`,
          `数据源「${run.data_source_name || '不使用数据源'}」`,
          start && `${start.date.slice(5)} ${start.time.slice(0, 5)} 开始`,
          took && `用时 ${took}`,
          `任务 ${shortId(run.id)}`,
          active && lastSync && `自动更新中 · 最近同步 ${lastSync.toLocaleTimeString('zh-CN', { hour12: false })}`,
        ]
          .filter(Boolean)
          .join(' · ')}
      </Heading>
      {copyMessage && <Alert>{copyMessage}</Alert>}
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
                : '本轮执行暂时中断，进度已保存'
          }
          action={
            recoverable ? (
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
          {run.error?.message || '请确认有效登录与依赖后恢复，恢复后只继续未完成的题目。'}
        </Alert>
      )}
      {run.status === 'cancelling' && (
        <Alert tone="amber" title="正在停止并核对远端状态">
          已经完成的回答与判分会保留。完成对账后才允许删除评测记录。
        </Alert>
      )}
      {active ? (
        <section className="panel progress-card">
          <Steps
            items={PHASES.map(([key, label, hint], i) => ({
              label,
              hint: key === 'cases' ? `${summary.judged} / ${summary.planned} 已判分` : hint,
              state: i < phaseIndex ? 'done' : i === phaseIndex ? 'current' : '',
            }))}
          />
          <div className="progress-line">
            <Progress value={summary.judged / (summary.planned || 1)} />
            <span className="tabular">
              <b>{summary.judged}</b> / {summary.planned} 已判分 · 已获得回答 {summary.executed} · 并发 {run.concurrency || '—'}
            </span>
            <Button variant="text" onClick={locate} disabled={!current}>
              定位执行中
            </Button>
          </div>
          {current && (
            <p className="muted">
              正在处理用例 {Number(current.id) + 1}：{current.question}
            </p>
          )}
        </section>
      ) : (
        <>
          <section className="panel result-card">
            <div className="result-score">
              <span>得分</span>
              <b>
                {scoreText(summary)}
                {summary.score != null && <small>分</small>}
              </b>
              <p className="pass-rate">
                通过率 <b>{passRate(summary)}</b>
                <span className="tabular">
                  {counts.pass} / {summary.planned}
                </span>
              </p>
              <small>
                {summary.score == null
                  ? '没有有效用例，未形成有效评测（不是 0 分）'
                  : `得分只算有效的 ${summary.judged} 条${excluded ? `（${excluded} 不计入）` : ''}；通过率按全部 ${summary.planned} 条算，部分通过、未通过、异常、待人工都不算通过`}
              </small>
            </div>
            <div className="result-split">
              <span>结果分布，点击可筛选下方用例</span>
              <ResultBar counts={counts} tall />
              <div className="result-legend">
                {RESULT_PARTS.filter(([key]) => key !== 'todo' || counts.todo).map(([key, title]) => (
                  <button
                    key={key}
                    type="button"
                    className={filter === PART_FILTER[key] && tab === 'cases' ? 'selected' : ''}
                    onClick={() => showCases(filter === PART_FILTER[key] ? 'all' : PART_FILTER[key])}
                  >
                    <i className={key} />
                    {title}
                    <b>{counts[key]}</b>
                  </button>
                ))}
              </div>
            </div>
            <div className="result-next">
              <span>接下来</span>
              {next.length ? (
                next.map(([text, label, act]) => (
                  <p key={text}>
                    {text}
                    <Button variant="text" onClick={act}>
                      {label}
                    </Button>
                  </p>
                ))
              ) : (
                <p>全部通过，没有需要处理的问题。</p>
              )}
            </div>
          </section>
          <DeliveryCard run={run} onChanged={refresh} navigate={navigate} />
        </>
      )}
      <Tabs
        label="执行详情"
        value={tab}
        onChange={setTab}
        items={[
          ['cases', '用例结果', summary.planned],
          ['report', '评测报告'],
          ['suggestions', solver ? '修复指引' : '优化建议'],
          ['history', '历史记录', historyTotal ?? undefined],
          ['config', '本轮配置'],
        ]}
      />
      {tab === 'cases' && (
        <>
          <div className="list-toolbar">
            <Segmented
              label="按结论筛选"
              value={filter}
              onChange={(value) => {
                setFilter(value);
                setLimit(20);
              }}
              items={[
                ['all', '全部', summary.planned],
                ...(active ? [['active', '执行中', cases.filter((c) => caseMatches(c, 'active')).length]] : []),
                ['fail', '未通过', counts.fail],
                ...(!solver ? [['partial', '部分通过', counts.partial], ['manual', '待人工', counts.manual]] : []),
                ...(counts.error ? [['errors', '异常', counts.error]] : []),
                ...(!['all', 'active', 'fail', 'partial', 'manual', 'errors'].includes(filter)
                  ? [[filter, VERDICTS[filter]?.[0] || '未执行', visible.length]]
                  : []),
              ]}
            />
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
          </div>
          <section className="panel table-panel">
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
                      <th>#</th>
                      <th>问题</th>
                      <th>结论</th>
                      <th>判分理由</th>
                      <th aria-label="打开" />
                    </tr>
                  </thead>
                  <tbody>
                    {visible.slice(0, limit).map((c) => {
                      const state = caseState(c),
                        view = VERDICTS[state] || VERDICTS.pending,
                        reason = (c.override || c.judgement)?.reason || c.error?.message || '';
                      return (
                        <tr
                          key={c.id}
                          ref={(el) => {
                            if (el) rowRefs.current.set(c.id, el);
                          }}
                          className={`clickable ${state === 'executing' ? 'current-case' : ''}`}
                          onClick={() => setSelected(c.id)}
                        >
                          <td className="case-index">{Number(c.id) + 1}</td>
                          <td>
                            <button
                              className="case-question"
                              onClick={(e) => {
                                e.stopPropagation();
                                setSelected(c.id);
                              }}
                            >
                              {solver ? `${c.name || c.case_id} · ${c.solver_key}` : c.question}
                            </button>
                            {solver && c.judgement?.code && <small className="danger">{c.judgement.code}</small>}
                          </td>
                          <td>
                            <Badge tone={view[1]}>{view[0]}</Badge>
                          </td>
                          <td className="case-reason" title={reason}>
                            {reason || <span className="muted">—</span>}
                          </td>
                          <td className="case-open">
                            <Icon name="right" />
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
                已显示 {Math.min(limit, visible.length)} / {visible.length} 条
              </span>
              {visible.length > limit && (
                <Button variant="text" onClick={() => setLimit(limit + 20)}>
                  加载更多
                </Button>
              )}
            </footer>
          </section>
        </>
      )}
      {tab === 'report' && (
        <ArtifactPanel
          run={run}
          kind="report"
          ready={artifacts.report === 'ready'}
          waiting={active ? '评测结束后自动生成。' : '评测结束且有用例结果后自动生成。'}
        />
      )}
      {tab === 'suggestions' && (
        <ArtifactPanel
          run={run}
          kind="suggestions"
          ready={artifacts.suggestions === 'ready'}
          waiting={
            artifacts.status === 'generating'
              ? (solver ? '正在生成修复指引。' : '判分模型正在归因，生成后这里自动显示。')
              : artifacts.suggestions_error || '评测结束后自动生成。'
          }
          note={
            <Alert>
              {solver ? '修复指引来自确定性断言结果，不使用判分模型。' : <>这些建议由判分模型 {artifacts.suggestions_model || run.judge?.model || ''} 生成，只作归因初稿。建模执行「评测优化」时会逐条复核后才修改知识包。</>}
            </Alert>
          }
        />
      )}
      {tab === 'history' && <RunHistory run={run} navigate={navigate} onTotal={setHistoryTotal} />}
      {tab === 'config' && (
        <section className="panel">
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
            <dd>{summary.inputs?.data_source_revision || '—'}</dd>
            <dt>{solver ? '求解验证集' : '评测集'}</dt>
            <dd>{(solver ? run.solver_set_name : run.eval_set_name) || '快照'}</dd>
            <dt>用例修订</dt>
            <dd>{run.case_revision}</dd>
            <dt>问答模型</dt>
            <dd>{run.model?.id || summary.model?.id || 'consumer 默认模型'}</dd>
            <dt>并发数</dt>
            <dd>{run.concurrency || '—'}</dd>
            <dt>判分模型</dt>
            <dd>
              {solver ? '不使用（确定性断言）' : run.judge?.model || summary.judge?.model || '—'}
              {run.judge?.revision ? ` · 配置版本 ${run.judge.revision}` : ''}
            </dd>
          </dl>
          {summary.reproducibility_note && <Alert tone="amber">{summary.reproducibility_note}</Alert>}
        </section>
      )}
      {selected !== null && (
        <AnswerDrawer
          key={`${id}-${selected}`}
          run={run}
          caseId={selected}
          cases={visible.length ? visible : cases}
          onChange={setSelected}
          onClose={() => setSelected(null)}
          onSaved={async () => {
            await refresh();
            onChanged();
          }}
        />
      )}
      {publish && <Publication run={run} onClose={() => setPublish(false)} />}
    </>
  );
}

// 求解验证用例：判定来自确定性断言，展示断言结果与 consumer 公开证据，不提供人工改判
const EVIDENCE_LABELS = [
  ['job_id', '求解任务'],
  ['feasibility', '可行性'],
  ['result_artifact_ref', '结果引用'],
  ['verification_evidence_ref', '验证证据'],
  ['candidate_results_artifact_ref', '候选结果引用'],
  ['candidate_count', '候选数'],
  ['solver_tool', '求解工具'],
];
function SolverEvidence({ answer }) {
  const evidence = answer.solver || {};
  const judgement = answer.judgement;
  return (
    <>
      <div className="answer-block">
        <label>求解操作</label>
        <p>
          {answer.name || answer.case_id} · {answer.solver_key}
        </p>
      </div>
      <div className="answer-block">
        <label>判定</label>
        {judgement ? (
          <>
            <Badge tone={VERDICTS[judgement.verdict]?.[1]}>{VERDICTS[judgement.verdict]?.[0]}</Badge>
            {judgement.code && <code> {judgement.code}</code>}
            <p>{judgement.reason}</p>
          </>
        ) : (
          <p>{answer.error?.message || VERDICTS[answer.status]?.[0] || '暂无判定'}</p>
        )}
      </div>
      {evidence.assertions?.length > 0 && (
        <div className="answer-block">
          <label>断言</label>
          <ul className="check-list">
            {evidence.assertions.map((item) => (
              <li key={item.name}>
                <Icon name={item.passed ? 'check' : 'info'} />
                {ASSERTION_TITLES[item.name] || item.name}
                {assertionNote(item) && ` · ${assertionNote(item)}`}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="answer-block">
        <label>入参</label>
        <div className="answer-content">
          <pre>{JSON.stringify(answer.arguments ?? {}, null, 2)}</pre>
        </div>
      </div>
      <div className="answer-block">
        <label>{answer.question_generated ? '提问（自动生成）' : '提问'}</label>
        <div className="answer-content">{answer.question}</div>
      </div>
      <div className="answer-block">
        <label>证据</label>
        <dl className="snapshot">
          {EVIDENCE_LABELS.filter(([key]) => evidence[key] != null && evidence[key] !== '').map(([key, label]) => (
            <React.Fragment key={key}>
              <dt>{label}</dt>
              <dd>
                <code>{String(evidence[key])}</code>
              </dd>
            </React.Fragment>
          ))}
        </dl>
      </div>
      <div className="answer-block">
        <label>Agent 回复</label>
        <div className="answer-content">{answer.answer || '尚未获得回复'}</div>
      </div>
    </>
  );
}

function AnswerDrawer({ run, caseId, cases, onChange, onClose, onSaved }) {
  const [answer, setAnswer] = useState(null),
    [loading, setLoading] = useState(true),
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
  const solver = isSolverRun(run);
  const index = cases.findIndex((c) => String(c.id) === String(caseId)),
    canOverride = !solver && !ACTIVE.has(run.status) && !!answer?.answer,
    shown = answer && (answer.override || answer.judgement),
    failures = answer?.trace?.tool_failures || [];
  async function saveOverride() {
    if (!reason.trim()) throw new Error('请填写改判原因');
    await api(`/runs/${run.id}/results/${caseId}/override`, {
      verdict,
      reason: reason.trim(),
      revision: run.revision,
    });
    setReason('');
    await load();
    await onSaved();
  }
  return (
    <Modal
      drawer
      title={
        <>
          用例 {Number(caseId) + 1}{solver ? ' · 断言与证据 ' : ' '}
          {shown && <Badge tone={VERDICTS[shown.verdict]?.[1]}>{VERDICTS[shown.verdict]?.[0]}</Badge>}
        </>
      }
      onClose={onClose}
      footer={
        <>
          <Button icon="left" disabled={index <= 0 || busy} onClick={() => onChange(cases[index - 1].id)}>
            上一条
          </Button>
          <Button disabled={index < 0 || index >= cases.length - 1 || busy} onClick={() => onChange(cases[index + 1].id)}>
            下一条
            <Icon name="right" />
          </Button>
          <span className="muted footer-hint">
            {index + 1} / {cases.length}
          </span>
          {canOverride && (
            <Button variant="primary" busy={busy} onClick={() => act(saveOverride)}>
              保存改判
            </Button>
          )}
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
      ) : answer && solver ? (
        <SolverEvidence answer={answer} />
      ) : (
        answer && (
          <>
            <div className="answer-block">
              <label>问题</label>
              <p className="answer-question">{answer.question}</p>
            </div>
            <div className="answer-block">
              <label>参考答案</label>
              <div className="answer-content">{answer.reference || '未填写参考答案，等待人工判定'}</div>
            </div>
            <div className="answer-block">
              <label>智能体回答</label>
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
              <label>{answer.override ? '人工判定' : '判分理由'}</label>
              {shown ? <p>{shown.reason}</p> : <p>{VERDICTS[answer.status]?.[0] || '暂无判定'}</p>}
              {answer.override && answer.judgement && (
                <p className="muted">
                  模型原判：{VERDICTS[answer.judgement.verdict]?.[0]} · {answer.judgement.reason}
                </p>
              )}
            </div>
            {failures.length > 0 && (
              <div className="answer-block">
                <label>工具失败</label>
                <ul className="tool-failures">
                  {failures.map((item, i) => (
                    <li key={i}>
                      <code>{item.tool_name || '未知工具'}</code> {item.error}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {answer.overrides?.length > 0 && (
              <details className="audit-history">
                <summary>人工判定历史（{answer.overrides.length}）</summary>
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
            {canOverride ? (
              <div className="answer-block override-form">
                <label>人工改判</label>
                <div className="override-row">
                  <select aria-label="改判结论" value={verdict} onChange={(e) => setVerdict(e.target.value)}>
                    <option value="pass">通过</option>
                    <option value="partial">部分通过</option>
                    <option value="fail">未通过</option>
                  </select>
                  <input
                    aria-label="改判原因"
                    placeholder="改判原因（必填）"
                    maxLength={4000}
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </div>
                <small className="muted">保留模型原判，并记录当前账号、原因与时间；改判后自动重新生成报告，优化建议需要手动重新生成。</small>
              </div>
            ) : (
              ACTIVE.has(run.status) && <Alert title="任务仍在运行">可以查看已生成的答案。人工改判在任务停止后开放。</Alert>
            )}
          </>
        )
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
