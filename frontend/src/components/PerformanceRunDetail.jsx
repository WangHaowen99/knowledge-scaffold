import { useMemo, useState } from 'react';
import { Activity, Bug, Cpu, GitBranch, Wrench } from 'lucide-react';

import {
  BreakdownBar,
  IterationWaterfall,
  StatusPill,
} from './performance/PerformanceCharts';
import {
  formatDuration,
  formatMetric,
  formatPerfAgentModeLabel,
  formatPerfCallerLabel,
  formatPerfComponentLabel,
  formatPerfEventLabel,
  formatPerfKindLabel,
  formatPerfPhaseLabel,
  formatPerfToolLabel,
  formatTimestamp,
} from '../utils/performanceMetrics';

const DETAIL_TABS = [
  { id: 'callchain', label: '调用链', icon: GitBranch },
  { id: 'iterations', label: '迭代耗时', icon: Activity },
  { id: 'llm', label: 'LLM 明细', icon: Cpu },
  { id: 'tools', label: '工具明细', icon: Wrench },
  { id: 'debug', label: '调试信息', icon: Bug },
];

function PerformanceRunDetail({ detail, loading = false, error = '' }) {
  const [activeTab, setActiveTab] = useState('callchain');
  const run = detail?.run || null;
  const timelineRows = useMemo(() => (detail?.timeline || []).slice(-80).reverse(), [detail]);
  const tabs = useMemo(() => DETAIL_TABS.map((tab) => ({
    ...tab,
    count: detailCount(detail, tab.id),
  })), [detail]);

  if (loading) {
    return <div className="perf-run-detail perf-empty">正在加载运行详情...</div>;
  }

  if (error) {
    return <div className="perf-run-detail perf-empty is-error">{error}</div>;
  }

  if (!run) {
    return <div className="perf-run-detail perf-empty">选择一次运行查看耗时拆分</div>;
  }

  return (
    <section className="perf-run-detail" aria-label={`运行 ${run.run_id} 详情`}>
      <div className="perf-run-heading">
        <div>
          <p>运行详情</p>
          <h2 title={run.run_id}>{run.run_id}</h2>
        </div>
        <div className="perf-run-meta">
          <StatusPill status={run.status} />
          <span>{formatDuration(run.total_duration_ms)}</span>
          <span>{formatTimestamp(run.ts_ms)}</span>
        </div>
      </div>

      <div className="perf-run-labels">
        <Label label="会话" value={run.session_id} />
        <Label label="工作空间" value={run.workspace_id} />
        <Label label="模型" value={run.model} />
        <Label label="供应商" value={run.model_provider} />
        <Label label="模式" value={formatPerfAgentModeLabel(run.agent_mode)} rawValue={run.agent_mode} />
      </div>

      <BreakdownBar breakdown={detail.breakdown} />

      <div className="perf-detail-section">
        <h3>运行明细</h3>
        <div className="perf-detail-tabs" role="tablist" aria-label="运行详情分组">
          {tabs.map((tab) => {
            const TabIcon = tab.icon;
            return (
              <button
                key={tab.id}
                className={activeTab === tab.id ? 'active' : ''}
                onClick={() => setActiveTab(tab.id)}
                type="button"
                role="tab"
                aria-selected={activeTab === tab.id}
              >
                <TabIcon size={14} />
                <span>{tab.label}</span>
                <small>{tab.count}</small>
              </button>
            );
          })}
        </div>
      </div>

      <div className="perf-detail-content">
        {activeTab === 'callchain' && <CallChain rows={detail.iteration_calls || []} />}
        {activeTab === 'iterations' && <IterationWaterfall iterations={detail.iterations || []} />}
        {activeTab === 'llm' && <LlmTable rows={detail.llm_calls || []} />}
        {activeTab === 'tools' && <ToolTable rows={detail.tools || []} />}
        {activeTab === 'debug' && (
          <DebugInfo
            phases={detail.runtime_phases || []}
            timeline={timelineRows}
          />
        )}
      </div>
    </section>
  );
}

function CallChain({ rows }) {
  if (!rows.length) {
    return <div className="perf-empty">本次运行没有可展示调用链</div>;
  }

  return (
    <div className="perf-call-chain">
      {rows.map((row) => {
        const llm = row.llm_calls?.[0] || null;
        const tools = row.tools || [];
        return (
          <section className="perf-call-chain-group" key={`${row.caller}-${row.iteration}`}>
            <div className="perf-call-chain-heading">
              <div>
                <strong>{formatPerfCallerLabel(row.caller)}</strong>
                <span>迭代 {row.iteration}</span>
              </div>
              <div>
                <span>LLM {formatDuration(row.summary?.llm_wall_ms)}</span>
                <span>{formatMetric(row.summary?.tool_count)} 个工具</span>
              </div>
            </div>

            <div className="perf-call-chain-body">
              <div className="perf-call-chain-llm">
                <span>LLM</span>
                {llm ? (
                  <dl>
                    <div>
                      <dt>时间</dt>
                      <dd>{formatTimestamp(llm.ts_ms)}</dd>
                    </div>
                    <div>
                      <dt>供应商</dt>
                      <dd>{llm.labels?.model_provider || '-'}</dd>
                    </div>
                    <div>
                      <dt>耗时</dt>
                      <dd>{formatDuration(llmWallDuration(llm.metrics))}</dd>
                    </div>
                    <div>
                      <dt>提示词</dt>
                      <dd>{formatPromptSize(llm.metrics)}</dd>
                    </div>
                    <div>
                      <dt>缓存命中</dt>
                      <dd>{formatCacheHit(llm.metrics)}</dd>
                    </div>
                    <div>
                      <dt>工具请求</dt>
                      <dd>{formatMetric(llm.metrics?.tool_calls_count)}</dd>
                    </div>
                    <div>
                      <dt>结束原因</dt>
                      <dd>{llm.labels?.finish_reason || '-'}</dd>
                    </div>
                  </dl>
                ) : (
                  <div className="perf-chain-empty">没有 LLM 完成记录</div>
                )}
              </div>

              <div className="perf-call-chain-tools">
                <span>工具调用</span>
                {tools.length ? (
                  <div className="perf-chain-tool-list">
                    {tools.map((tool, index) => (
                      <div className="perf-chain-tool-row" key={`${tool.labels?.tool_call_id || tool.ts_ms}-${index}`}>
                        <strong title={tool.labels?.tool_name || '-'}>
                          {formatPerfToolLabel(tool.labels?.tool_name)}
                        </strong>
                        <StatusPill status={tool.status} />
                        <span>{formatDuration(tool.metrics?.elapsed_ms || tool.metrics?.duration_ms)}</span>
                        <em title={tool.labels?.tool_call_id || '-'}>{tool.labels?.tool_call_id || '-'}</em>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="perf-chain-empty">本轮没有工具调用</div>
                )}
              </div>
            </div>
          </section>
        );
      })}
    </div>
  );
}

function Label({ label, value, rawValue = value }) {
  return (
    <div>
      <span>{label}</span>
      <strong title={rawValue || value || '-'}>{value || '-'}</strong>
    </div>
  );
}

function LlmTable({ rows }) {
  return (
    <PerfTable
      emptyText="本次运行没有 LLM 完成记录"
      columns={['时间', '调用方', '供应商', '墙钟耗时', '流式耗时', '提示词规模', '缓存命中']}
      rows={rows.map((row, index) => (
        <tr key={`${row.ts_ms}-${index}`}>
          <td>{formatTimestamp(row.ts_ms)}</td>
          <td title={row.caller || '-'}>{formatPerfCallerLabel(row.caller)}</td>
          <td>{row.labels?.model_provider || '-'}</td>
          <td>{formatDuration(llmWallDuration(row.metrics))}</td>
          <td>{formatDuration(row.metrics?.stream_duration_ms || row.metrics?.duration_ms)}</td>
          <td>{formatPromptSize(row.metrics)}</td>
          <td>{formatCacheHit(row.metrics)}</td>
        </tr>
      ))}
    />
  );
}

function ToolTable({ rows }) {
  return (
    <PerfTable
      emptyText="本次运行没有工具完成记录"
      columns={['时间', '工具', '调用方', '状态', '耗时', '调用 ID']}
      rows={rows.map((row, index) => (
        <tr key={`${row.labels?.tool_call_id || row.ts_ms}-${index}`}>
          <td>{formatTimestamp(row.ts_ms)}</td>
          <td title={row.labels?.tool_name || '-'}>{formatPerfToolLabel(row.labels?.tool_name)}</td>
          <td title={row.caller || '-'}>{formatPerfCallerLabel(row.caller)}</td>
          <td><StatusPill status={row.status} /></td>
          <td>{formatDuration(row.metrics?.elapsed_ms || row.metrics?.duration_ms)}</td>
          <td title={row.labels?.tool_call_id || '-'}>{row.labels?.tool_call_id || '-'}</td>
        </tr>
      ))}
    />
  );
}

function PhaseTable({ rows }) {
  return (
    <PerfTable
      emptyText="本次运行没有运行阶段记录"
      columns={['时间', '组件', '阶段', '事件', '耗时']}
      rows={rows.map((row, index) => (
        <tr key={`${row.labels?.phase_name || row.event}-${index}`}>
          <td>{formatTimestamp(row.ts_ms)}</td>
          <td title={row.labels?.component || '-'}>{formatPerfComponentLabel(row.labels?.component)}</td>
          <td title={row.labels?.phase_name || '-'}>{formatPerfPhaseLabel(row.labels?.phase_name)}</td>
          <td title={row.event || '-'}>{formatPerfEventLabel(row.event)}</td>
          <td>{formatDuration(row.metrics?.duration_ms)}</td>
        </tr>
      ))}
    />
  );
}

function DebugInfo({ phases, timeline }) {
  return (
    <div className="perf-debug-info">
      <section>
        <div className="perf-debug-heading">
          <h4>内部阶段</h4>
          <span>系统内部保存、压缩、收尾等阶段耗时。</span>
        </div>
        <PhaseTable rows={phases} />
      </section>
      <section>
        <div className="perf-debug-heading">
          <h4>原始事件</h4>
          <span>按时间倒序展示本地观测事件，主要用于排查打点顺序。</span>
        </div>
        <Timeline rows={timeline} />
      </section>
    </div>
  );
}

function Timeline({ rows }) {
  if (!rows.length) {
    return <div className="perf-empty">本次运行没有可展示事件</div>;
  }

  return (
    <ol className="perf-timeline">
      {rows.map((row, index) => (
        <li key={`${row.kind}-${row.event}-${row.ts_ms}-${index}`}>
          <time>{formatTimestamp(row.ts_ms)}</time>
          <strong title={row.event || row.kind}>
            {row.event ? formatPerfEventLabel(row.event) : formatPerfKindLabel(row.kind)}
          </strong>
          <span title={row.kind || '-'}>{formatPerfKindLabel(row.kind)}</span>
          {row.metrics?.duration_ms != null && <em>{formatDuration(row.metrics.duration_ms)}</em>}
        </li>
      ))}
    </ol>
  );
}

function PerfTable({ columns, rows, emptyText }) {
  if (!rows.length) {
    return <div className="perf-empty">{emptyText}</div>;
  }

  return (
    <div className="perf-table-wrap">
      <table className="perf-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  );
}

function formatPromptSize(metrics = {}) {
  const tokens = Number(metrics.prompt_tokens || 0);
  const chars = Number(metrics.prompt_chars || 0);
  if (tokens) return `${formatMetric(tokens)} tokens`;
  if (chars) return `${formatMetric(chars)} chars`;
  return '-';
}

function formatCacheHit(metrics = {}) {
  const hitTokens = metrics.cache_hit_tokens;
  const totalInputTokens = metrics.cache_total_input_tokens;
  const hitRate = metrics.cache_hit_rate;
  if (hitTokens == null || totalInputTokens == null || hitRate == null) return '-';
  return `${formatMetric(hitTokens)} / ${formatMetric(totalInputTokens)} · ${formatPercent(hitRate)}`;
}

function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '-';
  return `${(number * 100).toFixed(1)}%`;
}

function llmWallDuration(metrics = {}) {
  return Number(metrics.wall_duration_ms || 0)
    || Number(metrics.duration_ms || 0)
    || Number(metrics.stream_duration_ms || 0);
}

function detailCount(detail, tabId) {
  if (!detail) return 0;
  if (tabId === 'callchain') return detail.iteration_calls?.length || 0;
  if (tabId === 'iterations') return detail.iterations?.length || 0;
  if (tabId === 'llm') return detail.llm_calls?.length || 0;
  if (tabId === 'tools') return detail.tools?.length || 0;
  if (tabId === 'debug') {
    return (detail.runtime_phases?.length || 0) + (detail.timeline?.length || 0);
  }
  return 0;
}

export default PerformanceRunDetail;
