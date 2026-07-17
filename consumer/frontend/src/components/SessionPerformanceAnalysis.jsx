import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, RefreshCw } from 'lucide-react';

import { authFetch } from '../shared/api/auth';
import PerformanceRunDetail from './PerformanceRunDetail';
import {
  StatusPill,
} from './performance/PerformanceCharts';
import {
  formatDuration,
  formatMetric,
  formatTimestamp,
} from '../utils/performanceMetrics';

function SessionPerformanceAnalysis({ sessionId, onBack }) {
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selectedRunId, setSelectedRunId] = useState('');
  const [runDetail, setRunDetail] = useState(null);
  const [runLoading, setRunLoading] = useState(false);
  const [runError, setRunError] = useState('');

  const loadAnalysis = useCallback(async (signal) => {
    if (!sessionId) return;
    setLoading(true);
    setError('');
    try {
      const response = await authFetch(
        `/agentic/sessions/${encodeURIComponent(sessionId)}/performance`,
        { signal },
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || data.detail || '加载会话性能失败');
      setAnalysis(data);
      setSelectedRunId((current) => {
        if (current && data.runs?.some((run) => run.run_id === current)) return current;
        return data.runs?.[0]?.run_id || '';
      });
    } catch (nextError) {
      if (nextError?.name !== 'AbortError') {
        setError(nextError.message || '加载会话性能失败');
      }
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    const controller = new AbortController();
    loadAnalysis(controller.signal);
    return () => controller.abort();
  }, [loadAnalysis, refreshVersion]);

  useEffect(() => {
    if (!selectedRunId || !sessionId) {
      setRunDetail(null);
      return undefined;
    }
    const controller = new AbortController();
    const loadRun = async () => {
      setRunLoading(true);
      setRunError('');
      try {
        const response = await authFetch(
          `/agentic/performance/runs/${encodeURIComponent(selectedRunId)}?session_id=${encodeURIComponent(sessionId)}`,
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
  }, [selectedRunId, sessionId, refreshVersion]);

  const diagnostics = analysis?.diagnostics || {};

  return (
    <div className="session-performance-page">
      <header className="perf-page-header">
        <div className="perf-page-heading">
          <button onClick={onBack} type="button" aria-label="返回对话" title="返回对话">
            <ArrowLeft size={16} />
          </button>
          <div>
            <h2>会话性能分析</h2>
            <p title={sessionId}>按会话查看本地观测生成的运行耗时与单次运行拆分。</p>
          </div>
        </div>
        <div className="perf-toolbar">
          <button onClick={() => setRefreshVersion((value) => value + 1)} type="button" title="刷新性能数据">
            <RefreshCw size={15} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </header>

      {error && <div className="perf-banner is-error">{error}</div>}
      <Diagnostics diagnostics={diagnostics} />

      <div className="perf-session-grid">
        <section className="perf-surface perf-session-run-panel">
          <div className="perf-runs-panel-heading">
            <h3>运行定位</h3>
            <span>选择一轮查看右侧调用链</span>
          </div>
          <RunsTable
            rows={analysis?.runs || []}
            selectedRunId={selectedRunId}
            onSelect={setSelectedRunId}
            loading={loading}
          />
        </section>
        <PerformanceRunDetail detail={runDetail} loading={runLoading} error={runError} />
      </div>
    </div>
  );
}

function RunsTable({ rows, selectedRunId, onSelect, loading }) {
  if (loading && !rows.length) {
    return <div className="perf-empty">正在读取会话运行摘要...</div>;
  }
  if (!rows.length) {
    return <div className="perf-empty">当前会话没有本地性能运行记录</div>;
  }

  return (
    <div className="perf-table-wrap perf-run-list">
      <table className="perf-table">
        <thead>
          <tr>
            <th>轮次</th>
            <th>运行</th>
            <th>状态</th>
            <th>总耗时</th>
            <th>LLM</th>
            <th>提示词</th>
            <th>缓存命中</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.run_id}
              className={selectedRunId === row.run_id ? 'is-selected' : ''}
              onClick={() => onSelect(row.run_id)}
            >
              <td>{row.turn_id ?? '-'}</td>
              <td title={row.run_id}>{shortId(row.run_id)}</td>
              <td><StatusPill status={row.status} /></td>
              <td>{formatDuration(row.total_duration_ms)}</td>
              <td>{formatDuration(row.llm_wall_total_ms)}</td>
              <td>{row.prompt_tokens_max ? `${formatMetric(row.prompt_tokens_max)} tokens` : formatMetric(row.prompt_chars_max)}</td>
              <td>{formatCacheHit(row)}</td>
              <td>{formatTimestamp(row.ts_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Diagnostics({ diagnostics }) {
  if (!diagnostics?.parse_error_count && !diagnostics?.unknown_schema_count && !diagnostics?.truncated) {
    return null;
  }
  return (
    <div className="perf-banner">
      读取诊断：跳过旧版或不兼容日志 {formatMetric(diagnostics.unknown_schema_count)} 条，
      损坏日志 {formatMetric(diagnostics.parse_error_count)} 条
      {diagnostics.truncated ? '，结果已被截断。' : '。'}
    </div>
  );
}

function shortId(value) {
  const text = String(value || '-');
  return text.length > 16 ? `${text.slice(0, 7)}...${text.slice(-6)}` : text;
}

function formatCacheHit(row = {}) {
  const hitTokens = row.llm_cache_hit_tokens;
  const totalInputTokens = row.llm_cache_total_input_tokens;
  const hitRate = row.llm_cache_hit_rate;
  if (hitTokens == null || totalInputTokens == null || hitRate == null) return '-';
  return `${formatMetric(hitTokens)} / ${formatMetric(totalInputTokens)} · ${formatPercent(hitRate)}`;
}

function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '-';
  return `${(number * 100).toFixed(1)}%`;
}

export default SessionPerformanceAnalysis;
