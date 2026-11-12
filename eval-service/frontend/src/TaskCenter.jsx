import React, { useEffect, useState } from 'react';
import {
  ACTIVE,
  RUN_TYPES,
  STATUS,
  isSolverRun,
  needsAttention,
  optimizationState,
  packageName,
  packageVersions,
  passRate,
  phaseName,
  resultCounts,
  runStartTime,
  scoreText,
  shortId,
} from './domain.js';
import { Badge, Button, Empty, Heading, Icon, Progress, ResultBar, Segmented, Skeleton } from './ui.jsx';

// The table scrolls inside a clipped panel, so an open row menu is placed against the window instead, opening
// upwards when there is no room below; scrolling or resizing closes it rather than leaving it behind.
export function RowMenu({ label, children }) {
  const [place, setPlace] = useState(null);
  useEffect(() => {
    if (!place) return undefined;
    const close = () => {
      place.menu.open = false;
      setPlace(null);
    };
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [place]);
  function toggle(event) {
    const menu = event.currentTarget;
    if (!menu.open) return setPlace(null);
    const anchor = menu.firstElementChild.getBoundingClientRect(),
      height = menu.lastElementChild.offsetHeight,
      upward = window.innerHeight - anchor.bottom < height + 8 && anchor.top > height + 8;
    setPlace({
      menu,
      style: {
        position: 'fixed',
        right: window.innerWidth - anchor.right,
        ...(upward ? { top: 'auto', bottom: window.innerHeight - anchor.top + 4 } : { top: anchor.bottom + 4 }),
      },
    });
  }
  return (
    <details className="action-menu" onToggle={toggle} onClick={(e) => e.stopPropagation()}>
      <summary aria-label={label}>
        <Icon name="more" />
      </summary>
      <div style={place?.style}>{children}</div>
    </details>
  );
}

const FILTERS = {
  all: () => true,
  active: (r) => ACTIVE.has(r.status),
  attention: needsAttention,
  ended: (r) => ['completed', 'failed', 'cancelled'].includes(r.status),
};

// The newest run's result: score and pass rate once it stopped, judging progress while it runs.
function ResultCell({ run }) {
  const counts = resultCounts(run.summary),
    running = ACTIVE.has(run.status),
    open = [
      counts.fail && `未通过 ${counts.fail}`,
      counts.partial && `部分 ${counts.partial}`,
      counts.error && `异常 ${counts.error}`,
      counts.manual && `待人工 ${counts.manual}`,
      counts.todo && `未执行 ${counts.todo}`,
    ].filter(Boolean);
  return (
    <td data-label="结果" className="result-cell">
      <div className="result-line">
        <ResultBar counts={counts} />
        {running ? (
          <span className="tabular">
            <b>{run.summary.judged}</b> / {run.summary.planned} 已判分
          </span>
        ) : (
          <span className="tabular">
            <b>{scoreText(run.summary)}</b> 分 · 通过率 <b>{passRate(run.summary)}</b>
          </span>
        )}
      </div>
      {open.length > 0 && <small>{open.join(' · ')}</small>}
    </td>
  );
}

export function TaskCenter({ runs, loading, navigate, lastSync, searchState, setSearchState, onAction }) {
  const { tab, search } = searchState;
  const active = runs.filter((r) => ACTIVE.has(r.status)),
    [more, setMore] = useState(20);
  const visible = runs.filter(
    (r) =>
      (FILTERS[tab] || FILTERS.all)(r) &&
      `${r.id} ${r.package_ref?.ontology_id || ''} ${packageName(r)} ${r.eval_set_name || ''}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  const update = (value) => {
    setSearchState({ ...searchState, ...value });
    setMore(20);
  };
  const first = active[0];
  return (
    <>
      <Heading
        title="评测任务"
        actions={
          <Button icon="plus" variant="primary" onClick={() => navigate('/new')}>
            新建评测
          </Button>
        }
      >
        你发起的知识包评测，同一知识包的多次评测合并为一行。用户提交的问题在「用户反馈」中查看。
      </Heading>
      {first && (
        <section className="panel running-banner">
          <Badge tone="blue">{active.length > 1 ? `${active.length} 个进行中` : '进行中'}</Badge>
          <div className="running-name">
            <b>
              {packageName(first)} <span className="version-tag">{packageVersions(first).base || '—'}</span>
            </b>
            <small>{phaseName(first)}</small>
          </div>
          <Progress value={first.summary.judged / (first.summary.planned || 1)} />
          <span className="tabular">
            <b>{first.summary.judged}</b> / {first.summary.planned} 已判分
          </span>
          <Button onClick={() => navigate(`/runs/${first.id}`)}>查看进度</Button>
        </section>
      )}
      <div className="list-toolbar">
        <Segmented
          label="按状态筛选"
          value={tab}
          onChange={(value) => update({ tab: value })}
          items={[
            ['all', '全部', runs.length],
            ['active', '进行中', runs.filter(FILTERS.active).length],
            ['attention', '需处理', runs.filter(FILTERS.attention).length],
            ['ended', '已结束', runs.filter(FILTERS.ended).length],
          ]}
        />
        <label className="search-input">
          <Icon name="search" />
          <input
            aria-label="搜索知识包或任务编号"
            placeholder="搜索知识包或任务编号"
            value={search}
            onChange={(e) => update({ search: e.target.value })}
          />
        </label>
      </div>
      <section className="panel table-panel">
        {loading && !runs.length ? (
          <Skeleton />
        ) : !visible.length ? (
          <Empty
            title={runs.length ? '没有符合条件的任务' : '还没有评测任务'}
            action={
              runs.length ? (
                <Button onClick={() => update({ tab: 'all', search: '' })}>清除筛选</Button>
              ) : (
                <Button variant="primary" icon="plus" onClick={() => navigate('/new')}>
                  新建评测
                </Button>
              )
            }
          >
            {runs.length ? '调整筛选条件后再看看。' : '选择知识包、数据源和评测集，开始第一次评测。'}
          </Empty>
        ) : (
          <div className="table-scroll">
            <table className="task-table">
              <thead>
                <tr>
                  <th>知识包</th>
                  <th>状态</th>
                  <th>结果</th>
                  <th>建模处理</th>
                  <th className="align-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {visible.slice(0, more).map((run) => {
                  const start = runStartTime(run),
                    handled = optimizationState(run),
                    status = STATUS[run.status];
                  return (
                    <tr key={run.id} className="clickable" onClick={() => navigate(`/runs/${run.id}`)}>
                      <td data-label="知识包">
                        <b className="package-name" title={run.package_ref?.ontology_id}>
                          {packageName(run)} <span className="version-tag">{packageVersions(run).base || '—'}</span>
                          {isSolverRun(run) && <Badge tone="blue">{RUN_TYPES.solver}</Badge>}
                        </b>
                        <small>
                          最近一次 {start ? `${start.date.slice(5)} ${start.time.slice(0, 5)}` : '—'}
                          {run.run_count > 1 ? ` · 共 ${run.run_count} 次评测` : ''}
                        </small>
                      </td>
                      <td data-label="状态">
                        <Badge tone={status?.[1]}>{status?.[0] || run.status}</Badge>
                        {needsAttention(run) && run.error?.message && (
                          <small className="status-reason" title={run.error.message}>
                            {run.error.message}
                          </small>
                        )}
                      </td>
                      <ResultCell run={run} />
                      <td data-label="建模处理">
                        {handled ? <Badge tone={handled.tone}>{handled.label}</Badge> : <span className="muted">—</span>}
                      </td>
                      <td className="align-right">
                        <div className="row-actions">
                          <Button
                            variant={needsAttention(run) ? 'primary' : ''}
                            onClick={(e) => {
                              e.stopPropagation();
                              navigate(`/runs/${run.id}`);
                            }}
                          >
                            {ACTIVE.has(run.status) ? '查看进度' : needsAttention(run) ? '查看并处理' : '查看结果'}
                          </Button>
                          <RowMenu label={`任务 ${shortId(run.id)} 的更多操作`}>
                            <button onClick={() => navigate(`/new?from=${run.id}`)}>
                              <Icon name="refresh" />
                              再次评测
                            </button>
                            {!['completed', 'cancelled', 'failed', 'cancelling'].includes(run.status) && (
                              <button onClick={() => onAction('cancel', run)}>
                                <Icon name="stop" />
                                停止评测
                              </button>
                            )}
                            {!ACTIVE.has(run.status) && (
                              <button className="danger" onClick={() => onAction('delete', run)}>
                                <Icon name="trash" />
                                删除记录
                              </button>
                            )}
                          </RowMenu>
                        </div>
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
          {visible.length > more && (
            <Button variant="text" onClick={() => setMore(more + 20)}>
              加载更多
            </Button>
          )}
          <span>
            {lastSync
              ? `每 5 秒自动更新 · 最近同步 ${lastSync.toLocaleTimeString('zh-CN', { hour12: false })}`
              : '等待同步'}
          </span>
        </footer>
      </section>
    </>
  );
}
