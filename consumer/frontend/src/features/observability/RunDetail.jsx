import { useMemo, useState } from 'react';
import { Activity, Bug, Cpu, GitBranch, Wrench } from 'lucide-react';

import {
  BreakdownBar,
  DataTable,
  DiagnosticsBanner,
  IterationWaterfall,
  ObservabilityPanel,
  ObservabilityState,
  StatusPill,
} from './components.jsx';
import {
  detailCount,
  formatDuration,
  formatMetric,
  formatPercent,
  formatPerfAgentModeLabel,
  formatPerfCallerLabel,
  formatPerfComponentLabel,
  formatPerfEventLabel,
  formatPerfKindLabel,
  formatPerfPhaseLabel,
  formatPerfToolLabel,
  formatPromptSize,
  formatTimestamp,
  llmWallDuration,
} from './model.js';

const DETAIL_TABS = [
  { id: 'callchain', label: '调用链', icon: GitBranch },
  { id: 'iterations', label: '迭代耗时', icon: Activity },
  { id: 'llm', label: 'LLM 明细', icon: Cpu },
  { id: 'tools', label: '工具明细', icon: Wrench },
  { id: 'debug', label: '调试信息', icon: Bug },
];

export default function RunDetail({
  detail,
  loading = false,
  error = '',
  errorStatus = null,
}) {
  const [activeTab, setActiveTab] = useState('callchain');
  const run = detail?.run || null;
  const timelineRows = useMemo(
    () => (detail?.timeline || []).slice(-80).reverse(),
    [detail],
  );
  const tabs = DETAIL_TABS.map((tab) => ({
    ...tab,
    count: detailCount(detail, tab.id),
  }));

  if (loading && !run) {
    return (
      <ObservabilityPanel className="obs-run-detail">
        <ObservabilityState kind="loading" title="正在加载运行详情" />
      </ObservabilityPanel>
    );
  }
  if (error && !run) {
    const isExpired = Number(errorStatus) === 404;
    return (
      <ObservabilityPanel className="obs-run-detail">
        <ObservabilityState
          kind={isExpired ? 'stale' : 'error'}
          title={isExpired ? '运行明细已过期' : '运行详情加载失败'}
          detail={
            isExpired ? '本地明细已清理，会话摘要仍可用于比较轮次。' : error
          }
        />
      </ObservabilityPanel>
    );
  }
  if (!run) {
    return (
      <ObservabilityPanel className="obs-run-detail">
        <ObservabilityState title="选择一次运行查看耗时拆分" />
      </ObservabilityPanel>
    );
  }

  return (
    <ObservabilityPanel
      className="obs-run-detail"
      aria-label={`运行 ${run.run_id} 详情`}
    >
      <div className="obs-run-heading">
        <div>
          <p>运行详情</p>
          <h2 title={run.run_id}>{run.run_id}</h2>
        </div>
        <div className="obs-run-meta">
          <StatusPill status={run.status} />
          <span>{formatDuration(run.total_duration_ms)}</span>
          <span>{formatTimestamp(run.ts_ms)}</span>
        </div>
      </div>

      {error && (
        <div className="obs-banner tone-error">
          刷新运行详情失败，当前保留上次数据：{error}
        </div>
      )}
      {loading && <div className="obs-inline-loading">正在刷新运行详情…</div>}
      <DiagnosticsBanner diagnostics={detail.diagnostics} />

      <div className="obs-run-labels">
        <RunLabel label="会话" value={run.session_id} />
        <RunLabel label="工作空间" value={run.workspace_id} />
        <RunLabel label="模型" value={run.model} />
        <RunLabel label="供应商" value={run.model_provider} />
        <RunLabel
          label="模式"
          value={formatPerfAgentModeLabel(run.agent_mode)}
          rawValue={run.agent_mode}
        />
      </div>

      <BreakdownBar breakdown={detail.breakdown} />

      <div className="obs-detail-section">
        <h3>运行明细</h3>
        <div
          className="obs-detail-tabs"
          role="tablist"
          aria-label="运行详情分组"
        >
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
                aria-controls={`obs-detail-${tab.id}`}
              >
                <TabIcon size={14} aria-hidden="true" />
                <span>{tab.label}</span>
                <small>{tab.count}</small>
              </button>
            );
          })}
        </div>
      </div>

      <div
        className="obs-detail-content"
        id={`obs-detail-${activeTab}`}
        role="tabpanel"
      >
        {activeTab === 'callchain' && (
          <CallChain rows={detail.iteration_calls || []} />
        )}
        {activeTab === 'iterations' && (
          <IterationWaterfall iterations={detail.iterations || []} />
        )}
        {activeTab === 'llm' && <LlmTable rows={detail.llm_calls || []} />}
        {activeTab === 'tools' && <ToolTable rows={detail.tools || []} />}
        {activeTab === 'debug' && (
          <DebugInfo
            phases={detail.runtime_phases || []}
            timeline={timelineRows}
          />
        )}
      </div>
    </ObservabilityPanel>
  );
}

function RunLabel({ label, value, rawValue = value }) {
  return (
    <div>
      <span>{label}</span>
      <strong title={rawValue || value || '-'}>{value || '-'}</strong>
    </div>
  );
}

function CallChain({ rows }) {
  if (!rows.length) {
    return <ObservabilityState compact title="本次运行没有可展示调用链" />;
  }
  return (
    <div className="obs-call-chain">
      {rows.map((row) => {
        const [llm = null] = Array.isArray(row.llm_calls) ? row.llm_calls : [];
        const tools = row.tools || [];
        return (
          <section
            className="obs-call-chain-group"
            key={`${row.caller}-${row.iteration}`}
          >
            <div className="obs-call-chain-heading">
              <div>
                <strong>{formatPerfCallerLabel(row.caller)}</strong>
                <span>迭代 {row.iteration}</span>
              </div>
              <div>
                <span>LLM {formatDuration(row.summary?.llm_wall_ms)}</span>
                <span>{formatMetric(row.summary?.tool_count)} 个工具</span>
              </div>
            </div>
            <div className="obs-call-chain-body">
              <div className="obs-call-chain-llm">
                <span>LLM</span>
                {llm ? (
                  <dl>
                    <Definition
                      label="时间"
                      value={formatTimestamp(llm.ts_ms)}
                    />
                    <Definition
                      label="供应商"
                      value={llm.labels?.model_provider || '-'}
                    />
                    <Definition
                      label="耗时"
                      value={formatDuration(llmWallDuration(llm.metrics))}
                    />
                    <Definition
                      label="提示词"
                      value={formatPromptSize(llm.metrics)}
                    />
                    <Definition
                      label="缓存命中"
                      value={formatLlmCacheHit(llm.metrics)}
                    />
                    <Definition
                      label="工具请求"
                      value={formatMetric(llm.metrics?.tool_calls_count)}
                    />
                    <Definition
                      label="结束原因"
                      value={llm.labels?.finish_reason || '-'}
                    />
                  </dl>
                ) : (
                  <div className="obs-chain-empty">没有 LLM 完成记录</div>
                )}
              </div>
              <div className="obs-call-chain-tools">
                <span>工具调用</span>
                {tools.length ? (
                  <div className="obs-chain-tool-list">
                    {tools.map((tool, index) => (
                      <div
                        className="obs-chain-tool-row"
                        key={`${tool.labels?.tool_call_id || tool.ts_ms}-${index}`}
                      >
                        <strong title={tool.labels?.tool_name || '-'}>
                          {formatPerfToolLabel(tool.labels?.tool_name)}
                        </strong>
                        <StatusPill status={tool.status} />
                        <span>
                          {formatDuration(
                            tool.metrics?.elapsed_ms ||
                              tool.metrics?.duration_ms,
                          )}
                        </span>
                        <em title={tool.labels?.tool_call_id || '-'}>
                          {tool.labels?.tool_call_id || '-'}
                        </em>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="obs-chain-empty">本轮没有工具调用</div>
                )}
              </div>
            </div>
          </section>
        );
      })}
    </div>
  );
}

function Definition({ label, value }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function LlmTable({ rows }) {
  return (
    <DataTable
      emptyText="本次运行没有 LLM 完成记录"
      columns={[
        '时间',
        '调用方',
        '供应商',
        '墙钟耗时',
        '流式耗时',
        '提示词规模',
        '缓存命中',
      ]}
    >
      {rows.map((row, index) => (
        <tr key={`${row.ts_ms}-${index}`}>
          <td>{formatTimestamp(row.ts_ms)}</td>
          <td title={row.caller || '-'}>{formatPerfCallerLabel(row.caller)}</td>
          <td>{row.labels?.model_provider || '-'}</td>
          <td>{formatDuration(llmWallDuration(row.metrics))}</td>
          <td>
            {formatDuration(
              row.metrics?.stream_duration_ms || row.metrics?.duration_ms,
            )}
          </td>
          <td>{formatPromptSize(row.metrics)}</td>
          <td>{formatLlmCacheHit(row.metrics)}</td>
        </tr>
      ))}
    </DataTable>
  );
}

function ToolTable({ rows }) {
  return (
    <DataTable
      emptyText="本次运行没有工具完成记录"
      columns={['时间', '工具', '调用方', '状态', '耗时', '调用 ID']}
    >
      {rows.map((row, index) => (
        <tr key={`${row.labels?.tool_call_id || row.ts_ms}-${index}`}>
          <td>{formatTimestamp(row.ts_ms)}</td>
          <td title={row.labels?.tool_name || '-'}>
            {formatPerfToolLabel(row.labels?.tool_name)}
          </td>
          <td title={row.caller || '-'}>{formatPerfCallerLabel(row.caller)}</td>
          <td>
            <StatusPill status={row.status} />
          </td>
          <td>
            {formatDuration(
              row.metrics?.elapsed_ms || row.metrics?.duration_ms,
            )}
          </td>
          <td title={row.labels?.tool_call_id || '-'}>
            {row.labels?.tool_call_id || '-'}
          </td>
        </tr>
      ))}
    </DataTable>
  );
}

function DebugInfo({ phases, timeline }) {
  return (
    <div className="obs-debug-info">
      <section>
        <div className="obs-debug-heading">
          <h4>内部阶段</h4>
          <span>保存、压缩和收尾等阶段耗时。</span>
        </div>
        <PhaseTable rows={phases} />
      </section>
      <section>
        <div className="obs-debug-heading">
          <h4>原始事件</h4>
          <span>最新 80 条事件，按时间倒序展示。</span>
        </div>
        <Timeline rows={timeline} />
      </section>
    </div>
  );
}

function PhaseTable({ rows }) {
  return (
    <DataTable
      emptyText="本次运行没有运行阶段记录"
      columns={['时间', '组件', '阶段', '事件', '耗时']}
    >
      {rows.map((row, index) => (
        <tr key={`${row.labels?.phase_name || row.event}-${index}`}>
          <td>{formatTimestamp(row.ts_ms)}</td>
          <td title={row.labels?.component || '-'}>
            {formatPerfComponentLabel(row.labels?.component)}
          </td>
          <td title={row.labels?.phase_name || '-'}>
            {formatPerfPhaseLabel(row.labels?.phase_name)}
          </td>
          <td title={row.event || '-'}>{formatPerfEventLabel(row.event)}</td>
          <td>{formatDuration(row.metrics?.duration_ms)}</td>
        </tr>
      ))}
    </DataTable>
  );
}

function Timeline({ rows }) {
  if (!rows.length) {
    return <ObservabilityState compact title="本次运行没有可展示事件" />;
  }
  return (
    <ol className="obs-timeline">
      {rows.map((row, index) => (
        <li key={`${row.kind}-${row.event}-${row.ts_ms}-${index}`}>
          <time>{formatTimestamp(row.ts_ms)}</time>
          <strong title={row.event || row.kind}>
            {row.event ?
              formatPerfEventLabel(row.event) :
              formatPerfKindLabel(row.kind)}
          </strong>
          <span title={row.kind || '-'}>{formatPerfKindLabel(row.kind)}</span>
          {row.metrics?.duration_ms != null && (
            <em>{formatDuration(row.metrics.duration_ms)}</em>
          )}
        </li>
      ))}
    </ol>
  );
}

function formatLlmCacheHit(metrics = {}) {
  if (
    metrics.cache_hit_tokens == null ||
    metrics.cache_total_input_tokens == null ||
    metrics.cache_hit_rate == null
  ) {
    return '-';
  }
  return `${formatMetric(metrics.cache_hit_tokens)} / ${formatMetric(metrics.cache_total_input_tokens)} · ${formatPercent(metrics.cache_hit_rate)}`;
}
