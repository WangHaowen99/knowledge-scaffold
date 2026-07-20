import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';

import { requestObservabilityJson } from './api.js';
import {
  DataTable,
  DiagnosticsBanner,
  MetricTile,
  ObservabilityPageHeader,
  ObservabilityPanel,
  ObservabilityState,
  StatusPill,
} from './components.jsx';
import {
  OBSERVABILITY_VIEWS,
  buildRunDetailUrl,
  buildSessionPerformanceUrl,
  detailForSelectedRun,
  formatCacheHit,
  formatDuration,
  formatMetric,
  formatTimestamp,
  normalizeRunDetail,
  normalizeSessionAnalysis,
  resolveCollectionState,
  selectRunId,
  shortId,
} from './model.js';
import RunDetail from './RunDetail.jsx';

export default function SessionPerformanceView({ sessionId, onBack, request }) {
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedRunId, setSelectedRunId] = useState('');
  const [runDetail, setRunDetail] = useState(null);
  const [runLoading, setRunLoading] = useState(false);
  const [runError, setRunError] = useState('');
  const [runErrorStatus, setRunErrorStatus] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setLoading(true);
      setError('');
      try {
        const payload = await requestObservabilityJson(
          request,
          buildSessionPerformanceUrl(sessionId),
          { signal: controller.signal },
          '加载会话性能失败',
        );
        if (controller.signal.aborted) return;
        const next = normalizeSessionAnalysis(payload);
        setAnalysis(next);
        setSelectedRunId((current) => selectRunId(current, next.runs));
      } catch (nextError) {
        if (nextError?.name !== 'AbortError' && !controller.signal.aborted) {
          setError(nextError.message || '加载会话性能失败');
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    load();
    return () => controller.abort();
  }, [refreshVersion, request, sessionId]);

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
            sessionId,
            view: OBSERVABILITY_VIEWS.SESSION,
          }),
          { signal: controller.signal },
          '加载运行详情失败',
        );
        if (!controller.signal.aborted) setRunDetail(normalizeRunDetail(payload));
      } catch (nextError) {
        if (nextError?.name !== 'AbortError' && !controller.signal.aborted) {
          setRunError(nextError.message || '加载运行详情失败');
          setRunErrorStatus(Number(nextError.status) || null);
        }
      } finally {
        if (!controller.signal.aborted) setRunLoading(false);
      }
    };
    load();
    return () => controller.abort();
  }, [refreshVersion, request, selectedRunId, sessionId]);

  const runs = analysis?.runs || [];
  const summary = analysis?.summary || {};
  const state = resolveCollectionState({ loading, error, itemCount: runs.length });
  const selectedRunDetail = detailForSelectedRun(runDetail, selectedRunId);

  return (
    <div className="obs-view obs-session-view">
      <ObservabilityPageHeader
        eyebrow="SESSION OBSERVABILITY"
        title="会话性能分析"
        description="按会话对齐轮次、运行、LLM 和工具耗时。"
        onBack={onBack}
        actions={(
          <button
            className="obs-button"
            onClick={() => setRefreshVersion((value) => value + 1)}
            type="button"
            disabled={loading}
          >
            <RefreshCw className={loading ? 'obs-spin' : ''} size={15} aria-hidden="true" />
            刷新
          </button>
        )}
      />

      {error && runs.length > 0 && <div className="obs-banner tone-error">刷新失败，当前保留上次数据：{error}</div>}
      <DiagnosticsBanner diagnostics={analysis?.diagnostics} />

      {state === 'loading' && <ObservabilityPanel><ObservabilityState kind="loading" title="正在读取会话运行摘要" /></ObservabilityPanel>}
      {state === 'error' && (
        <ObservabilityPanel>
          <ObservabilityState
            kind="error"
            title="会话性能加载失败"
            detail={error}
            action={<button className="obs-button" type="button" onClick={() => setRefreshVersion((value) => value + 1)}>重试</button>}
          />
        </ObservabilityPanel>
      )}

      {state !== 'loading' && state !== 'error' && (
        <>
          <div className="obs-metric-grid session-metrics">
            <MetricTile label="运行数" value={formatMetric(summary.run_count)} detail={`${formatMetric(summary.turn_count)} 个轮次`} />
            <MetricTile label="成功 / 失败" value={`${formatMetric(summary.success_count)} / ${formatMetric(summary.error_count)}`} detail={`取消 ${formatMetric(summary.cancelled_count)}`} />
            <MetricTile label="总耗时" value={formatDuration(summary.total_duration_ms)} detail="会话内所有运行" />
            <MetricTile label="LLM 墙钟耗时" value={formatDuration(summary.llm_wall_total_ms)} detail="累计耗时" />
          </div>

          <div className="obs-session-grid">
            <ObservabilityPanel title="运行定位" description="选择一轮后查看右侧调用链。" className="obs-run-list-panel">
              {state === 'empty' ? (
                <ObservabilityState
                  title="当前会话没有本地性能运行记录"
                  detail="完成一次运行后可刷新查看。"
                />
              ) : (
                <SessionRunsTable rows={runs} selectedRunId={selectedRunId} onSelect={setSelectedRunId} />
              )}
            </ObservabilityPanel>
            <RunDetail
              key={selectedRunId || 'empty'}
              detail={selectedRunDetail}
              loading={runLoading}
              error={runError}
              errorStatus={runErrorStatus}
            />
          </div>
        </>
      )}
    </div>
  );
}

function SessionRunsTable({ rows, selectedRunId, onSelect }) {
  const activate = (event, runId) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onSelect(runId);
    }
  };
  return (
    <DataTable columns={['轮次', '运行', '状态', '总耗时', 'LLM', '提示词', '缓存命中', '时间']}>
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
          <td>{row.turn_id ?? '-'}</td>
          <td title={row.run_id}>{shortId(row.run_id, 16)}</td>
          <td><StatusPill status={row.status} /></td>
          <td>{formatDuration(row.total_duration_ms)}</td>
          <td>{formatDuration(row.llm_wall_total_ms)}</td>
          <td>{row.prompt_tokens_max ? `${formatMetric(row.prompt_tokens_max)} tokens` : formatMetric(row.prompt_chars_max)}</td>
          <td>{formatCacheHit(row)}</td>
          <td>{formatTimestamp(row.ts_ms)}</td>
        </tr>
      ))}
    </DataTable>
  );
}
