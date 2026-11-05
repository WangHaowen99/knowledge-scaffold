import React, { useEffect, useState } from 'react';
import {
  ACTIVE,
  STATUS,
  needsAttention,
  optimizationTag,
  packageName,
  packageVersions,
  phaseName,
  runStartTime,
  shortId,
} from './domain.js';
import { Badge, Button, Empty, Heading, Icon, Progress, Skeleton } from './ui.jsx';

// The table scrolls inside a clipped panel, so an open row menu is placed against the window instead, opening
// upwards when there is no room below; scrolling or resizing closes it rather than leaving it behind.
function RowMenu({ label, children }) {
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
    <details className="action-menu" onToggle={toggle}>
      <summary aria-label={label}>···</summary>
      <div style={place?.style}>{children}</div>
    </details>
  );
}

function PackageNameCell({ run }) {
  const name = packageName(run);
  return (
    <td data-label="知识包名称" className="package-name" title={name}>
      {name}
    </td>
  );
}

function PackageVersionCell({ run }) {
  const { base, baseId } = packageVersions(run);
  return (
    <td data-label="知识包版本">
      <span className="version-tag" title={base ? undefined : `基础版本 ID：${baseId || '未知'}`}>
        {base || '—'}
      </span>
    </td>
  );
}

function StartTimeCell({ run }) {
  const start = runStartTime(run);
  return (
    <td data-label="启动时间" className="start-time">
      {start ? (
        <>
          <b className="tabular">{start.date}</b>
          <small className="tabular">{start.time}</small>
        </>
      ) : (
        '—'
      )}
    </td>
  );
}

export function TaskCenter({
  runs,
  loading,
  onRefresh,
  navigate,
  lastSync,
  searchState,
  setSearchState,
  onAction,
}) {
  const { tab, search, packageFilter } = searchState;
  const active = runs.find((r) => ACTIVE.has(r.status)),
    [more, setMore] = useState(20);
  const visible = runs.filter(
    (r) =>
      (tab === 'active'
        ? ACTIVE.has(r.status)
        : tab === 'attention'
          ? needsAttention(r)
          : tab === 'ended'
            ? ['completed', 'failed', 'cancelled'].includes(r.status)
            : true) &&
      (!packageFilter || packageName(r) === packageFilter) &&
      `${r.id} ${r.package_ref?.ontology_id || ''} ${packageName(r)} ${r.eval_set_name || ''}`.toLowerCase().includes(search.toLowerCase()),
  );
  const update = (value) => {
    setSearchState({ ...searchState, ...value });
    setMore(20);
  };
  return (
    <>
      <Heading
        title="任务中心"
        actions={
          <>
            <Button icon="refresh" busy={loading} onClick={onRefresh}>
              刷新
            </Button>
            <Button icon="plus" variant="primary" onClick={() => navigate('/new')}>
              新建评测
            </Button>
          </>
        }
      >
        同一知识包的评测合并为一项，执行详情中可查看历史执行记录。
      </Heading>
      {active && (
        <div className="active-task">
          <span className="active-task-icon">
            <Icon name="task" />
          </span>
          <div className="active-task-body">
            <div className="inline">
              <h2>有 {runs.filter((r) => ACTIVE.has(r.status)).length} 个评测正在进行</h2>
              <Badge tone={STATUS[active.status]?.[1]}>{STATUS[active.status]?.[0]}</Badge>
            </div>
            <p>
              {shortId(active.id)}
              <span>·</span>
              {packageName(active)}
              <span>·</span>
              {packageVersions(active).base || active.package_ref?.version}
            </p>
            <div className="active-progress">
              <Progress value={active.summary.judged / (active.summary.planned || 1)} />
              <strong>
                {active.summary.judged} / {active.summary.planned} 已判分
              </strong>
              <small>当前：{phaseName(active)}</small>
            </div>
          </div>
          <Button icon="arrow" variant="primary" onClick={() => navigate(`/runs/${active.id}`)}>
            查看执行详情
          </Button>
        </div>
      )}
      <div className="metric-grid">
        {[
          ['全部任务', runs.length, '保留完整运行记录', 'all'],
          ['进行中', runs.filter((r) => ACTIVE.has(r.status)).length, '后台持续执行', 'active'],
          ['需要处理', runs.filter(needsAttention).length, '待授权或中断恢复', 'attention'],
          ['已完成', runs.filter((r) => r.status === 'completed').length, '可查看答案与判分', 'ended'],
        ].map(([label, count, hint, value]) => (
          <button className="metric-card" key={label} onClick={() => update({ tab: value })}>
            <span>{label}</span>
            <b>{loading && !runs.length ? '—' : count}</b>
            <small>{hint}</small>
          </button>
        ))}
      </div>
      <section className="panel table-panel">
        <div className="table-toolbar">
          <div className="tabs">
            {[
              ['all', '全部任务'],
              ['active', '进行中'],
              ['attention', '需要处理'],
              ['ended', '已结束'],
            ].map(([value, title]) => (
              <button
                key={value}
                type="button"
                className={tab === value ? 'selected' : ''}
                aria-pressed={tab === value}
                onClick={() => update({ tab: value })}
              >
                {title}
              </button>
            ))}
          </div>
          <div className="filters">
            <label className="search-input">
              <Icon name="search" />
              <input
                aria-label="搜索任务编号或知识包"
                placeholder="搜索任务编号 / 知识包"
                value={search}
                onChange={(e) => update({ search: e.target.value })}
              />
            </label>
            <select
              aria-label="筛选知识包"
              value={packageFilter}
              onChange={(e) => update({ packageFilter: e.target.value })}
            >
              <option value="">全部知识包</option>
              {[...new Set(runs.map(packageName))].map((name) => (
                <option key={name}>{name}</option>
              ))}
            </select>
          </div>
        </div>
        {loading && !runs.length ? (
          <Skeleton />
        ) : !visible.length ? (
          <Empty
            title={runs.length ? '没有符合条件的任务' : '还没有评测任务'}
            action={
              runs.length ? (
                <Button onClick={() => update({ tab: 'all', search: '', packageFilter: '' })}>
                  清空筛选
                </Button>
              ) : (
                <Button variant="primary" icon="plus" onClick={() => navigate('/new')}>
                  新建评测
                </Button>
              )
            }
          >
            {runs.length
              ? '尝试调整筛选条件，查看其他评测记录。'
              : '选择知识包、数据源与评测集，开始第一次评测。'}
          </Empty>
        ) : (
          <div className="table-scroll">
            <table className="task-table">
              <thead>
                <tr>
                  <th>任务 ID / 知识包 ID</th>
                  <th>知识包名称</th>
                  <th>知识包版本</th>
                  <th>启动时间</th>
                  <th>状态</th>
                  <th>判分进度</th>
                  <th>当前阶段</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {visible.slice(0, more).map((run) => (
                  <tr key={run.id}>
                    <td data-label="任务 ID / 知识包 ID">
                      <button className="run-link" onClick={() => navigate(`/runs/${run.id}`)}>
                        {run.id}
                      </button>
                      <small>
                        {run.package_ref?.ontology_id || '—'}
                        {run.run_count > 1 && ` · 共 ${run.run_count} 次执行`}
                      </small>
                    </td>
                    <PackageNameCell run={run} />
                    <PackageVersionCell run={run} />
                    <StartTimeCell run={run} />
                    <td data-label="状态">
                      <Badge tone={STATUS[run.status]?.[1]}>{STATUS[run.status]?.[0] || run.status}</Badge>
                      {optimizationTag(run) && (
                        <Badge tone={optimizationTag(run).tone}>{optimizationTag(run).label}</Badge>
                      )}
                    </td>
                    <td data-label="判分进度">
                      <b className="tabular">
                        {run.summary.judged} / {run.summary.planned}
                      </b>
                      <small>已判分 / 总用例</small>
                    </td>
                    <td data-label="阶段">{phaseName(run)}</td>
                    <td>
                      <div className="actions">
                        <Button variant="text" onClick={() => navigate(`/runs/${run.id}`)}>
                          {ACTIVE.has(run.status)
                            ? '查看执行详情'
                            : needsAttention(run)
                              ? '查看并处理'
                              : '查看结果'}
                        </Button>
                        <RowMenu label={`任务 ${shortId(run.id)} 的更多操作`}>
                          <button onClick={() => navigate(`/new?from=${run.id}`)}>再次评测</button>
                          {!['completed', 'cancelled', 'failed', 'cancelling'].includes(run.status) && (
                            <button onClick={() => onAction('cancel', run)}>停止评测</button>
                          )}
                          {!ACTIVE.has(run.status) && (
                            <button className="danger" onClick={() => onAction('delete', run)}>
                              删除评测记录
                            </button>
                          )}
                        </RowMenu>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <footer className="table-footer">
          <span>
            已展示 {Math.min(more, visible.length)} / {visible.length} 条任务
          </span>
          <span>
            {lastSync
              ? `每 5 秒更新 · 最近同步 ${lastSync.toLocaleTimeString('zh-CN', { hour12: false })}`
              : '等待同步'}
          </span>
          {visible.length > more && (
            <Button variant="text" onClick={() => setMore(more + 20)}>
              加载更多任务
            </Button>
          )}
        </footer>
      </section>
    </>
  );
}
