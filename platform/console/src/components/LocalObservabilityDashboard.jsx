import { useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowLeft, Filter, RefreshCw } from 'lucide-react';

import { authFetch } from '../utils/auth';
import PerformanceRunDetail from './PerformanceRunDetail';
import {
  DistributionBars,
  MetricTile,
  StatusPill,
  TrendChart,
} from './performance/PerformanceCharts';
import {
  createPerformanceWindowQuery,
  formatDuration,
  formatMetric,
  formatPerfAgentModeLabel,
  formatPerfComponentLabel,
  formatPerfPhaseLabel,
  formatPerfToolLabel,
  formatTimestamp,
} from '../utils/performanceMetrics';
import {
  getLocalObservabilityRuntime,
  getUnknownLocalObservabilityRuntime,
} from '../utils/observabilityStatus';

const EMPTY_FILTERS = {
  workspace_id: '',
  status: '',
  model_provider: '',
  agent_mode: '',
};

const WINDOW_PRESETS = [
  { id: '1h', label: '1 小时' },
  { id: '24h', label: '24 小时' },
  { id: '7d', label: '7 天' },
];

function LocalObservabilityDashboard({ workspaces = [], onBack }) {
  const [windowId, setWindowId] = useState('24h');
  const [windowEndMs, setWindowEndMs] = useState(() => Date.now());
  const [draftFilters, setDraftFilters] = useState(EMPTY_FILTERS);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [overview, setOverview] = useState(null);
  const [runs, setRuns] = useState(null);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [runDetail, setRunDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [runLoading, setRunLoading] = useState(false);
  const [error, setError] = useState('');
  const [runError, setRunError] = useState('');
  const [localObservabilityRuntime, setLocalObservabilityRuntime] = useState(getUnknownLocalObservabilityRuntime);
  const query = useMemo(
    () => appendFilters(createPerformanceWindowQuery(windowId, windowEndMs), filters),
    [windowId, windowEndMs, filters],
  );

  const loadOverview = useCallback(async (signal) => {
    setLoading(true);
    setError('');
    try {
      const [overviewResponse, runsResponse] = await Promise.all([
        authFetch(`/api/local-observability/overview?${query}`, { signal }),
        authFetch(`/api/local-observability/runs?limit=60&${query}`, { signal }),
      ]);
      const [overviewData, runsData] = await Promise.all([
        overviewResponse.json().catch(() => ({})),
        runsResponse.json().catch(() => ({})),
      ]);
      if (!overviewResponse.ok) throw new Error(overviewData.error || overviewData.detail || '加载本地观测总览失败');
      if (!runsResponse.ok) throw new Error(runsData.error || runsData.detail || '加载运行列表失败');
      setOverview(overviewData);
      setRuns(runsData);
      setSelectedRunId((current) => {
        if (current && runsData.runs?.some((run) => run.run_id === current)) return current;
        return overviewData.slow_runs?.[0]?.run_id || runsData.runs?.[0]?.run_id || '';
      });
    } catch (nextError) {
      if (nextError?.name !== 'AbortError') {
        setError(nextError.message || '加载本地观测总览失败');
      }
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();
    loadOverview(controller.signal);
    return () => controller.abort();
  }, [loadOverview]);

  useEffect(() => {
    const controller = new AbortController();
    const loadObservabilityConfig = async () => {
      try {
        const response = await authFetch('/api/system-config', { signal: controller.signal });
        const data = await response.json().catch(() => ({}));
        if (!controller.signal.aborted) {
          setLocalObservabilityRuntime(response.ok
            ? getLocalObservabilityRuntime(data)
            : getUnknownLocalObservabilityRuntime());
        }
      } catch (nextError) {
        if (nextError?.name !== 'AbortError') {
          setLocalObservabilityRuntime(getUnknownLocalObservabilityRuntime());
        }
      }
    };
    loadObservabilityConfig();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!selectedRunId) {
      setRunDetail(null);
      return undefined;
    }
    const controller = new AbortController();
    const loadRun = async () => {
      setRunLoading(true);
      setRunError('');
      try {
        const response = await authFetch(
          `/agentic/performance/runs/${encodeURIComponent(selectedRunId)}?${query}`,
          { signal: controller.signal },
        );
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || data.detail || '加载运行详情失败');
        setRunDetail(data);
      } catch (nextError) {
        if (nextError?.name !== 'AbortError') {
          setRunDetail(null);
          setRunError(nextError.message || '加载运行详情失败');
        }
      } finally {
        setRunLoading(false);
      }
    };
    loadRun();
    return () => controller.abort();
  }, [query, selectedRunId]);

  const summary = overview?.summary || {};
  const totalDuration = summary.total_duration_ms || {};
  const diagnostics = overview?.diagnostics || {};

  return (
    <div className="local-observability-page">
      <header className="perf-page-header">
        <div className="perf-page-heading">
          <button onClick={onBack} type="button" aria-label="返回对话" title="返回对话">
            <ArrowLeft size={16} />
          </button>
          <div>
            <h2>本地观测分析</h2>
            <p>面向管理员聚合本地 perf 记录，按窗口和工作空间定位慢运行。</p>
          </div>
        </div>
        <div className="perf-toolbar">
          <WindowPicker
            value={windowId}
            onChange={(nextWindowId) => {
              setWindowId(nextWindowId);
              setWindowEndMs(Date.now());
            }}
          />
          <button onClick={() => setWindowEndMs(Date.now())} type="button" title="刷新总览">
            <RefreshCw size={15} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </header>

      <section className="perf-filter-strip" aria-label="本地观测筛选条件">
        <label>
          <span>工作空间</span>
          <select
            value={draftFilters.workspace_id}
            onChange={(event) => setDraftFilters((current) => ({ ...current, workspace_id: event.target.value }))}
          >
            <option value="">全部</option>
            {workspaces.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>{workspace.name || workspace.id}</option>
            ))}
          </select>
        </label>
        <label>
          <span>状态</span>
          <select
            value={draftFilters.status}
            onChange={(event) => setDraftFilters((current) => ({ ...current, status: event.target.value }))}
          >
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="error">失败</option>
            <option value="cancelled">取消</option>
          </select>
        </label>
        <label>
          <span>模型供应商</span>
          <input
            value={draftFilters.model_provider}
            onChange={(event) => setDraftFilters((current) => ({ ...current, model_provider: event.target.value }))}
            placeholder="例如 glm"
          />
        </label>
        <label>
          <span>Agent 模式</span>
          <input
            value={draftFilters.agent_mode}
            onChange={(event) => setDraftFilters((current) => ({ ...current, agent_mode: event.target.value }))}
            placeholder="例如 thinking"
          />
        </label>
        <button className="perf-filter-apply" onClick={() => setFilters(normalizeFilters(draftFilters))} type="button">
          <Filter size={15} />
          <span>应用</span>
        </button>
      </section>

      {error && <div className="perf-banner is-error">{error}</div>}
      <div className={`perf-banner observability-mode-banner tone-${localObservabilityRuntime.tone}`}>
        <strong>{localObservabilityRuntime.label}</strong>
        <span>{localObservabilityRuntime.detail}</span>
      </div>
      <Diagnostics diagnostics={diagnostics} />

      <div className="perf-metric-grid">
        <MetricTile label="运行数" value={formatMetric(summary.run_count)} detail={`成功率 ${formatPercent(summary.success_rate)}`} />
        <MetricTile label="P50 / P90" value={`${formatDuration(totalDuration.p50)} / ${formatDuration(totalDuration.p90)}`} detail="总耗时" />
        <MetricTile label="P99" value={formatDuration(totalDuration.p99)} detail="尾部运行总耗时" />
        <MetricTile label="失败 / 取消" value={`${formatMetric(summary.error_count)} / ${formatMetric(summary.cancelled_count)}`} detail={`成功 ${formatMetric(summary.success_count)}`} />
        <MetricTile label="缓存命中" value={formatCacheHit(summary)} detail="LLM 输入 Token" />
      </div>

      <div className="perf-overview-grid">
        <section className="perf-surface">
          <TrendChart
            points={overview?.trends || []}
            valueKey="p90_total_duration_ms"
            secondaryValueKey="run_count"
            label="按小时 P90 总耗时与运行量"
            primaryLabel="P90 总耗时"
            secondaryLabel="运行量"
          />
        </section>
        <section className="perf-surface">
          <DistributionBars rows={overview?.duration_buckets || []} label="总耗时分布" />
        </section>
      </div>

      <div className="perf-admin-grid">
        <RunExplorer
          slowRows={overview?.slow_runs || []}
          recentRows={runs?.runs || []}
          recentCount={runs?.count || 0}
          selectedRunId={selectedRunId}
          onSelect={setSelectedRunId}
        />
        <section className="perf-surface perf-rank-panel">
          <PanelHeading
            title="整体瓶颈"
            detail="按当前窗口聚合工具耗时和内部阶段耗时。"
          />
          <Rankings tools={overview?.tools || []} phases={overview?.runtime_phases || []} />
        </section>
      </div>

      <PerformanceRunDetail detail={runDetail} loading={runLoading} error={runError} />
    </div>
  );
}

function RunExplorer({ slowRows, recentRows, recentCount, selectedRunId, onSelect }) {
  const [mode, setMode] = useState('slow');
  const effectiveMode = mode === 'slow' && slowRows.length === 0 && recentRows.length > 0 ? 'recent' : mode;
  const isSlowMode = effectiveMode === 'slow';
  const rows = isSlowMode ? slowRows : recentRows;
  const columns = isSlowMode
    ? ['运行', '工作空间', '状态', '总耗时', 'LLM', '缓存命中', '时间']
    : ['运行', '会话', '模式', '总耗时', '缓存命中', '时间'];

  return (
    <section className="perf-surface perf-run-explorer">
      <div className="perf-panel-heading">
        <PanelHeading
          title="运行定位"
          detail="先找出慢运行或最近运行，再在下方查看单次运行详情。"
        />
        <div className="perf-segmented-control" role="tablist" aria-label="运行列表类型">
          <button
            className={isSlowMode ? 'active' : ''}
            onClick={() => setMode('slow')}
            type="button"
            role="tab"
            aria-selected={isSlowMode}
          >
            慢运行
            <small>{formatMetric(slowRows.length)}</small>
          </button>
          <button
            className={!isSlowMode ? 'active' : ''}
            onClick={() => setMode('recent')}
            type="button"
            role="tab"
            aria-selected={!isSlowMode}
          >
            最近运行
            <small>{formatMetric(recentCount || recentRows.length)}</small>
          </button>
        </div>
      </div>
      <RunExplorerTable
        columns={columns}
        emptyText={isSlowMode ? '当前窗口没有慢运行摘要' : '当前筛选条件没有运行摘要'}
        rows={rows.map((row) => (
          <tr key={row.run_id} className={selectedRunId === row.run_id ? 'is-selected' : ''} onClick={() => onSelect(row.run_id)}>
            <td title={row.run_id}>{shortId(row.run_id)}</td>
            {isSlowMode ? (
              <>
                <td title={row.workspace_id || '-'}>{row.workspace_id || '-'}</td>
                <td><StatusPill status={row.status} /></td>
                <td>{formatDuration(row.total_duration_ms)}</td>
                <td>{formatDuration(row.llm_wall_total_ms)}</td>
                <td>{formatCacheHit(row)}</td>
                <td>{formatTimestamp(row.ts_ms)}</td>
              </>
            ) : (
              <>
                <td title={row.session_id || '-'}>{shortId(row.session_id)}</td>
                <td title={row.agent_mode || '-'}>{formatPerfAgentModeLabel(row.agent_mode)}</td>
                <td>{formatDuration(row.total_duration_ms)}</td>
                <td>{formatCacheHit(row)}</td>
                <td>{formatTimestamp(row.ts_ms)}</td>
              </>
            )}
          </tr>
        ))}
      />
    </section>
  );
}

function Rankings({ tools, phases }) {
  return (
    <div className="perf-rankings">
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
              {formatPerfComponentLabel(row.component)} / {formatPerfPhaseLabel(row.phase_name)}
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

function WindowPicker({ value, onChange }) {
  return (
    <div className="perf-window-picker" role="group" aria-label="分析时间窗口">
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

function PanelHeading({ title, detail }) {
  return (
    <div className="perf-section-heading">
      <h3>{title}</h3>
      {detail && <span>{detail}</span>}
    </div>
  );
}

function RunExplorerTable({ columns, rows, emptyText }) {
  if (!rows.length) {
    return <div className="perf-empty">{emptyText}</div>;
  }

  return (
    <div className="perf-table-wrap">
      <table className="perf-table">
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  );
}

function RankTable({ title, columns, rows, emptyText }) {
  return (
    <section className="perf-rank-table">
      <h3>{title}</h3>
      {rows.length ? (
        <div className="perf-table-wrap">
          <table className="perf-table">
            <thead>
              <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
      ) : (
        <div className="perf-empty">{emptyText}</div>
      )}
    </section>
  );
}

function Diagnostics({ diagnostics }) {
  if (!diagnostics?.parse_error_count && !diagnostics?.unknown_schema_count && !diagnostics?.truncated) {
    return null;
  }
  return (
    <div className="perf-banner">
      扫描文件 {formatMetric(diagnostics.scanned_files)}，
      跳过旧版或不兼容日志 {formatMetric(diagnostics.unknown_schema_count)} 条，
      损坏日志 {formatMetric(diagnostics.parse_error_count)} 条
      {diagnostics.truncated ? '，结果已截断。' : '。'}
    </div>
  );
}

function appendFilters(query, filters) {
  const params = new URLSearchParams(query);
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  return params.toString();
}

function normalizeFilters(filters) {
  return Object.fromEntries(Object.entries(filters).map(([key, value]) => [key, String(value || '').trim()]));
}

function formatPercent(value) {
  return `${Math.round(Number(value || 0) * 1000) / 10}%`;
}

function formatCacheHit(row = {}) {
  const hitTokens = row.llm_cache_hit_tokens;
  const totalInputTokens = row.llm_cache_total_input_tokens;
  const hitRate = row.llm_cache_hit_rate;
  if (hitTokens == null || totalInputTokens == null || hitRate == null) return '-';
  return `${formatMetric(hitTokens)} / ${formatMetric(totalInputTokens)} · ${formatPercent(hitRate)}`;
}

function shortId(value) {
  const text = String(value || '-');
  return text.length > 18 ? `${text.slice(0, 8)}...${text.slice(-6)}` : text;
}

export default LocalObservabilityDashboard;
