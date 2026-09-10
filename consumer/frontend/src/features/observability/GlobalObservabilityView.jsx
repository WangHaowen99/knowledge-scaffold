import { useEffect, useMemo, useState } from 'react';
import { Filter, RefreshCw } from 'lucide-react';

import { requestObservabilityJson } from './api.js';
import {
  DataTable,
  DiagnosticsBanner,
  DistributionBars,
  MetricTile,
  ObservabilityPageHeader,
  ObservabilityPanel,
  ObservabilityState,
  Pagination,
  RuntimeBanner,
  StatusPill,
  TrendChart,
} from './components.jsx';
import {
  EMPTY_OBSERVABILITY_FILTERS,
  OBSERVABILITY_VIEWS,
  buildGlobalObservabilityUrls,
  buildRunDetailUrl,
  detailForSelectedRun,
  findRunById,
  formatCacheHit,
  formatDuration,
  formatMetric,
  formatPercent,
  formatPerfAgentModeLabel,
  formatPerfComponentLabel,
  formatPerfPhaseLabel,
  formatPerfToolLabel,
  formatTimestamp,
  getFailedLocalObservabilityRuntime,
  getLocalObservabilityRuntime,
  getPageInfo,
  getUnknownLocalObservabilityRuntime,
  normalizeGlobalOverview,
  normalizeGlobalRuns,
  normalizeObservabilityFilters,
  normalizeRunDetail,
  resolveCollectionState,
  selectRunId,
  shortId,
} from './model.js';
import RunDetail from './RunDetail.jsx';

const WINDOW_PRESETS = [
  { id: '1h', label: '1 小时' },
  { id: '24h', label: '24 小时' },
  { id: '7d', label: '7 天' },
];
const PAGE_SIZE = 50;

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
export default function GlobalObservabilityView({
  workspaceId = '',
  workspaces = [],
  onBack,
  request,
}) {
  const [windowId, setWindowId] = useState('24h');
  const [windowEndMs, setWindowEndMs] = useState(() => Date.now());
  const [draftFilters, setDraftFilters] = useState({
    ...EMPTY_OBSERVABILITY_FILTERS,
  });
  const [filters, setFilters] = useState({ ...EMPTY_OBSERVABILITY_FILTERS });
  const [offset, setOffset] = useState(0);
  const [overview, setOverview] = useState(null);
  const [runs, setRuns] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedRunId, setSelectedRunId] = useState('');
  const [runDetail, setRunDetail] = useState(null);
  const [runLoading, setRunLoading] = useState(false);
  const [runError, setRunError] = useState('');
  const [runErrorStatus, setRunErrorStatus] = useState(null);
  const [runtime, setRuntime] = useState(getUnknownLocalObservabilityRuntime);
  const [runtimeRefreshKey, setRuntimeRefreshKey] = useState(0);

  const urls = useMemo(
    () =>
      buildGlobalObservabilityUrls({
        windowId,
        endMs: windowEndMs,
        filters,
        limit: PAGE_SIZE,
        offset,
      }),
    [filters, offset, windowEndMs, windowId],
  );

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setLoading(true);
      setError('');
      try {
        const [overviewPayload, runsPayload] = await Promise.all([
          requestObservabilityJson(
            request,
            urls.overview,
            { signal: controller.signal },
            '加载本地观测总览失败',
          ),
          requestObservabilityJson(
            request,
            urls.runs,
            { signal: controller.signal },
            '加载运行列表失败',
          ),
        ]);
        if (controller.signal.aborted) {
          return;
        }
        const nextOverview = normalizeGlobalOverview(overviewPayload);
        const nextRuns = normalizeGlobalRuns(runsPayload);
        setOverview(nextOverview);
        setRuns(nextRuns);
        setSelectedRunId((current) =>
          selectRunId(current, nextOverview.slow_runs, nextRuns.runs),
        );
      } catch (nextError) {
        if (nextError?.name !== 'AbortError' && !controller.signal.aborted) {
          setError(nextError.message || '加载本地观测总览失败');
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    };
    load();
    return () => controller.abort();
  }, [request, urls]);

  useEffect(() => {
    const controller = new AbortController();
    const loadRuntime = async () => {
      if (!workspaceId) {
        setRuntime(getUnknownLocalObservabilityRuntime());
        return;
      }
      try {
        const payload = await requestObservabilityJson(
          request,
          `/agentic/workspaces/${encodeURIComponent(workspaceId)}/observability-config`,
          { signal: controller.signal },
          '读取观测配置失败',
        );
        if (!controller.signal.aborted) {
          setRuntime(getLocalObservabilityRuntime({ observability: payload }));
        }
      } catch (nextError) {
        if (nextError?.name !== 'AbortError' && !controller.signal.aborted) {
          setRuntime(getFailedLocalObservabilityRuntime(nextError.message));
        }
      }
    };
    loadRuntime();
    return () => controller.abort();
  }, [request, runtimeRefreshKey, workspaceId]);

  const selectedRun = findRunById(
    selectedRunId,
    overview?.slow_runs,
    runs?.runs,
  );
  const selectedWorkspaceId =
    selectedRun?.workspace_id || filters.workspace_id || '';

  useEffect(() => {
    if (!selectedRunId) {
      setRunDetail(null);
      setRunError('');
      setRunErrorStatus(null);
      return undefined;
    }
    const controller = new AbortController();
    const load = async () => {
      setRunLoading(true);
      setRunError('');
      setRunErrorStatus(null);
      setRunDetail(null);
      try {
        const payload = await requestObservabilityJson(
          request,
          buildRunDetailUrl({
            runId: selectedRunId,
            view: OBSERVABILITY_VIEWS.GLOBAL,
            query: urls.query,
            workspaceId: selectedWorkspaceId,
          }),
          { signal: controller.signal },
          '加载运行详情失败',
        );
        if (!controller.signal.aborted) {
          setRunDetail(normalizeRunDetail(payload));
        }
      } catch (nextError) {
        if (nextError?.name !== 'AbortError' && !controller.signal.aborted) {
          setRunError(nextError.message || '加载运行详情失败');
          setRunErrorStatus(Number(nextError.status) || null);
        }
      } finally {
        if (!controller.signal.aborted) {
          setRunLoading(false);
        }
      }
    };
    load();
    return () => controller.abort();
  }, [request, selectedRunId, selectedWorkspaceId, urls.query]);

  const summary = overview?.summary || {};
  const runCount = Number(summary.run_count) || 0;
  const state = resolveCollectionState({ loading, error, itemCount: runCount });
  const pageInfo = getPageInfo(runs || { count: 0, limit: PAGE_SIZE, offset });
  const selectedRunDetail = detailForSelectedRun(runDetail, selectedRunId);

  const applyFilters = () => {
    setOffset(0);
    setFilters(normalizeObservabilityFilters(draftFilters));
  };

  const refresh = () => {
    setOffset(0);
    setWindowEndMs(Date.now());
    setRuntime(getUnknownLocalObservabilityRuntime());
    setRuntimeRefreshKey((current) => current + 1);
  };

  return (
    <div className="obs-view obs-global-view">
      <ObservabilityPageHeader
        eyebrow="GLOBAL OBSERVABILITY"
        title="本地观测分析"
        description="面向管理员聚合本地 perf 记录，按时间窗口和工作空间定位慢运行。"
        onBack={onBack}
        actions={
          <>
            <WindowPicker
              value={windowId}
              onChange={(next) => {
                setWindowId(next);
                setOffset(0);
                setWindowEndMs(Date.now());
              }}
            />
            <button
              className="obs-button"
              onClick={refresh}
              type="button"
              disabled={loading}
            >
              <RefreshCw
                className={loading ? 'obs-spin' : ''}
                size={15}
                aria-hidden="true"
              />
              刷新
            </button>
          </>
        }
      />

      <FilterStrip
        draftFilters={draftFilters}
        setDraftFilters={setDraftFilters}
        workspaces={workspaces}
        onApply={applyFilters}
        disabled={loading}
      />

      {error && runCount > 0 && (
        <div className="obs-banner tone-error">
          刷新失败，当前保留上次数据：{error}
        </div>
      )}
      <RuntimeBanner runtime={runtime} />
      <DiagnosticsBanner
        diagnostics={overview?.diagnostics}
        includeScannedFiles
      />

      {state === 'loading' && (
        <ObservabilityPanel>
          <ObservabilityState kind="loading" title="正在聚合全局运行数据" />
        </ObservabilityPanel>
      )}
      {state === 'error' && (
        <ObservabilityPanel>
          <ObservabilityState
            kind="error"
            title="全局观测加载失败"
            detail={error}
            action={
              <button className="obs-button" type="button" onClick={refresh}>
                重试
              </button>
            }
          />
        </ObservabilityPanel>
      )}

      {state !== 'loading' && state !== 'error' && (
        <>
          <GlobalMetrics summary={summary} />
          {state === 'empty' ? (
            <ObservabilityPanel>
              <ObservabilityState
                title="当前窗口没有匹配的本地运行记录"
                detail="调整时间窗口或筛选条件后重试。"
              />
            </ObservabilityPanel>
          ) : (
            <>
              <div className="obs-overview-grid">
                <ObservabilityPanel>
                  <TrendChart
                    points={overview?.trends || []}
                    valueKey="p90_total_duration_ms"
                    secondaryValueKey="run_count"
                    label="按小时 P90 总耗时与运行量"
                    primaryLabel="P90 总耗时"
                    secondaryLabel="运行量"
                  />
                </ObservabilityPanel>
                <ObservabilityPanel>
                  <DistributionBars
                    rows={overview?.duration_buckets || []}
                    label="总耗时分布"
                  />
                </ObservabilityPanel>
              </div>

              <div className="obs-admin-grid">
                <RunExplorer
                  slowRows={overview?.slow_runs || []}
                  recentRows={runs?.runs || []}
                  recentCount={runs?.count || 0}
                  selectedRunId={selectedRunId}
                  onSelect={setSelectedRunId}
                  pageInfo={pageInfo}
                  onPrevious={() => setOffset(pageInfo.previousOffset)}
                  onNext={() => setOffset(pageInfo.nextOffset)}
                  loading={loading}
                />
                <ObservabilityPanel
                  title="整体瓶颈"
                  description="按当前窗口聚合工具和内部阶段耗时。"
                  className="obs-ranking-panel"
                >
                  <Rankings
                    tools={overview?.tools || []}
                    phases={overview?.runtime_phases || []}
                  />
                </ObservabilityPanel>
              </div>

              <RunDetail
                key={selectedRunId || 'empty'}
                detail={selectedRunDetail}
                loading={runLoading}
                error={runError}
                errorStatus={runErrorStatus}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}

function GlobalMetrics({ summary }) {
  const totalDuration = summary.total_duration_ms || {};
  return (
    <div className="obs-metric-grid">
      <MetricTile
        label="运行数"
        value={formatMetric(summary.run_count)}
        detail={`成功率 ${formatPercent(summary.success_rate)}`}
      />
      <MetricTile
        label="P50 / P90"
        value={`${formatDuration(totalDuration.p50)} / ${formatDuration(totalDuration.p90)}`}
        detail="总耗时"
      />
      <MetricTile
        label="P99"
        value={formatDuration(totalDuration.p99)}
        detail="尾部运行总耗时"
      />
      <MetricTile
        label="失败 / 取消"
        value={`${formatMetric(summary.error_count)} / ${formatMetric(summary.cancelled_count)}`}
        detail={`成功 ${formatMetric(summary.success_count)}`}
      />
      <MetricTile
        label="缓存命中"
        value={formatCacheHit(summary)}
        detail="LLM 输入 Token"
      />
    </div>
  );
}

function WindowPicker({ value, onChange }) {
  return (
    <div className="obs-window-picker" role="group" aria-label="分析时间窗口">
      {WINDOW_PRESETS.map((preset) => (
        <button
          key={preset.id}
          className={value === preset.id ? 'active' : ''}
          onClick={() => onChange(preset.id)}
          type="button"
        >
          {preset.label}
        </button>
      ))}
    </div>
  );
}

function FilterStrip({
  draftFilters,
  setDraftFilters,
  workspaces,
  onApply,
  disabled,
}) {
  const update = (key, value) =>
    setDraftFilters((current) => ({ ...current, [key]: value }));
  return (
    <section className="obs-filter-strip" aria-label="本地观测筛选条件">
      <label>
        <span>工作空间</span>
        <input
          list="obs-workspaces"
          value={draftFilters.workspace_id}
          onChange={(event) => update('workspace_id', event.target.value)}
          placeholder="全部工作空间"
        />
        <datalist id="obs-workspaces">
          {workspaces.map((workspace) => (
            <option key={workspace.id} value={workspace.id}>
              {workspace.name || workspace.id}
            </option>
          ))}
        </datalist>
      </label>
      <label>
        <span>状态</span>
        <select
          value={draftFilters.status}
          onChange={(event) => update('status', event.target.value)}
        >
          <option value="">全部</option>
          <option value="success">成功</option>
          <option value="completed">完成</option>
          <option value="error">失败</option>
          <option value="cancelled">取消</option>
        </select>
      </label>
      <label>
        <span>模型供应商</span>
        <input
          value={draftFilters.model_provider}
          onChange={(event) => update('model_provider', event.target.value)}
          placeholder="例如 glm"
        />
      </label>
      <label>
        <span>Agent 模式</span>
        <input
          value={draftFilters.agent_mode}
          onChange={(event) => update('agent_mode', event.target.value)}
          placeholder="例如 thinking"
        />
      </label>
      <button
        className="obs-button primary"
        onClick={onApply}
        type="button"
        disabled={disabled}
      >
        <Filter size={15} aria-hidden="true" />
        应用筛选
      </button>
    </section>
  );
}

function RunExplorer({
  slowRows,
  recentRows,
  recentCount,
  selectedRunId,
  onSelect,
  pageInfo,
  onPrevious,
  onNext,
  loading,
}) {
  const [mode, setMode] = useState('slow');
  const effectiveMode =
    mode === 'slow' && slowRows.length === 0 && recentRows.length > 0 ?
      'recent' :
      mode;
  const isSlow = effectiveMode === 'slow';
  const rows = isSlow ? slowRows : recentRows;
  return (
    <ObservabilityPanel className="obs-run-explorer">
      <div className="obs-explorer-heading">
        <div>
          <h3>运行定位</h3>
          <p>选择慢运行或最近运行，在下方查看详情。</p>
        </div>
        <div className="obs-segmented" role="tablist" aria-label="运行列表类型">
          <button
            className={isSlow ? 'active' : ''}
            onClick={() => setMode('slow')}
            type="button"
            role="tab"
            aria-selected={isSlow}
          >
            慢运行 <small>{formatMetric(slowRows.length)}</small>
          </button>
          <button
            className={!isSlow ? 'active' : ''}
            onClick={() => setMode('recent')}
            type="button"
            role="tab"
            aria-selected={!isSlow}
          >
            最近运行{' '}
            <small>{formatMetric(recentCount || recentRows.length)}</small>
          </button>
        </div>
      </div>
      <GlobalRunsTable
        rows={rows}
        slowMode={isSlow}
        selectedRunId={selectedRunId}
        onSelect={onSelect}
      />
      {!isSlow && (
        <Pagination
          pageInfo={pageInfo}
          onPrevious={onPrevious}
          onNext={onNext}
          disabled={loading}
        />
      )}
    </ObservabilityPanel>
  );
}

function GlobalRunsTable({ rows, slowMode, selectedRunId, onSelect }) {
  const columns = slowMode ?
    ['运行', '工作空间', '状态', '总耗时', 'LLM', '缓存命中', '时间'] :
    ['运行', '会话', '模式', '总耗时', '缓存命中', '时间'];
  const activate = (event, runId) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onSelect(runId);
    }
  };
  return (
    <DataTable
      columns={columns}
      emptyText={
        slowMode ? '当前窗口没有慢运行摘要' : '当前筛选条件没有运行摘要'
      }
    >
      {rows.map((row) => (
        <tr
          key={row.run_id}
          className={selectedRunId === row.run_id ? 'is-selected' : ''}
          onClick={() => onSelect(row.run_id)}
          onKeyDown={(event) => activate(event, row.run_id)}
          tabIndex={0}
          role="button"
          aria-pressed={selectedRunId === row.run_id}
        >
          <td title={row.run_id}>{shortId(row.run_id)}</td>
          {slowMode ? (
            <>
              <td title={row.workspace_id || '-'}>{row.workspace_id || '-'}</td>
              <td>
                <StatusPill status={row.status} />
              </td>
              <td>{formatDuration(row.total_duration_ms)}</td>
              <td>{formatDuration(row.llm_wall_total_ms)}</td>
              <td>{formatCacheHit(row)}</td>
              <td>{formatTimestamp(row.ts_ms)}</td>
            </>
          ) : (
            <>
              <td title={row.session_id || '-'}>{shortId(row.session_id)}</td>
              <td title={row.agent_mode || '-'}>
                {formatPerfAgentModeLabel(row.agent_mode)}
              </td>
              <td>{formatDuration(row.total_duration_ms)}</td>
              <td>{formatCacheHit(row)}</td>
              <td>{formatTimestamp(row.ts_ms)}</td>
            </>
          )}
        </tr>
      ))}
    </DataTable>
  );
}

function Rankings({ tools, phases }) {
  return (
    <div className="obs-rankings">
      <RankTable
        title="工具耗时排行"
        emptyText="没有工具耗时记录"
        columns={['工具', '次数', '累计', '平均', '最大']}
        rows={tools.slice(0, 8).map((row) => (
          <tr key={row.tool_name}>
            <td title={row.tool_name}>{formatPerfToolLabel(row.tool_name)}</td>
            <td>{formatMetric(row.count)}</td>
            <td>{formatDuration(row.total_ms)}</td>
            <td>{formatDuration(row.avg_ms)}</td>
            <td>{formatDuration(row.max_ms)}</td>
          </tr>
        ))}
      />
      <RankTable
        title="运行阶段排行"
        emptyText="没有运行阶段记录"
        columns={['组件 / 阶段', '次数', '累计', '平均', '最大']}
        rows={phases.slice(0, 8).map((row) => (
          <tr key={`${row.component}-${row.phase_name}`}>
            <td title={`${row.component || '-'} / ${row.phase_name || '-'}`}>
              {formatPerfComponentLabel(row.component)} /{' '}
              {formatPerfPhaseLabel(row.phase_name)}
            </td>
            <td>{formatMetric(row.count)}</td>
            <td>{formatDuration(row.total_ms)}</td>
            <td>{formatDuration(row.avg_ms)}</td>
            <td>{formatDuration(row.max_ms)}</td>
          </tr>
        ))}
      />
    </div>
  );
}

function RankTable({ title, columns, rows, emptyText }) {
  return (
    <section className="obs-rank-table">
      <h4>{title}</h4>
      <DataTable columns={columns} emptyText={emptyText}>
        {rows}
      </DataTable>
    </section>
  );
}
