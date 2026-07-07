import { useMemo } from 'react';

import { formatDuration, formatMetric, formatPerfCallerLabel } from '../../utils/performanceMetrics';

const STATUS_LABELS = {
  success: '成功',
  completed: '完成',
  error: '失败',
  cancelled: '取消',
  unknown: '未知',
};

export function StatusPill({ status }) {
  const normalized = String(status || 'unknown').toLowerCase();
  return (
    <span className={`perf-status-pill status-${normalized}`}>
      {STATUS_LABELS[normalized] || normalized}
    </span>
  );
}

export function MetricTile({ label, value, detail }) {
  return (
    <div className="perf-metric-tile">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </div>
  );
}

export function TrendChart({
  points = [],
  valueKey,
  secondaryValueKey = null,
  label,
  primaryLabel = '主指标',
  secondaryLabel = '对比指标',
  emptyText = '当前窗口没有趋势数据',
}) {
  const chart = useMemo(() => buildTrend(points, valueKey, secondaryValueKey), [
    points,
    valueKey,
    secondaryValueKey,
  ]);

  if (!chart) {
    return <div className="perf-empty perf-chart-empty">{emptyText}</div>;
  }

  return (
    <figure className="perf-chart">
      <figcaption>{label}</figcaption>
      <svg viewBox="0 0 600 190" role="img" aria-label={label}>
        {chart.gridY.map((y) => (
          <line key={y} x1="42" y1={y} x2="576" y2={y} className="perf-chart-grid" />
        ))}
        <path d={chart.primaryPath} className="perf-chart-line primary" />
        {chart.secondaryPath && <path d={chart.secondaryPath} className="perf-chart-line secondary" />}
        {chart.points.map((point) => (
          <g key={point.key}>
            <circle cx={point.x} cy={point.primaryY} r="3.5" className="perf-chart-dot primary" />
            {point.secondaryY != null && (
              <circle cx={point.x} cy={point.secondaryY} r="3" className="perf-chart-dot secondary" />
            )}
            <text x={point.x} y="178" className="perf-chart-axis-label">
              {point.label}
            </text>
          </g>
        ))}
        {chart.axisLabels.map((axis) => (
          <text key={axis.text} x="36" y={axis.y + 4} className="perf-chart-value-label">
            {axis.text}
          </text>
        ))}
      </svg>
      <div className="perf-chart-legend">
        <span><i className="primary" />{primaryLabel}</span>
        {secondaryValueKey && <span><i className="secondary" />{secondaryLabel}</span>}
      </div>
    </figure>
  );
}

export function DistributionBars({ rows = [], label }) {
  const maxCount = Math.max(...rows.map((row) => Number(row.count || 0)), 1);
  if (!rows.length) {
    return <div className="perf-empty">暂无分布数据</div>;
  }

  return (
    <section className="perf-distribution" aria-label={label}>
      <h3>{label}</h3>
      <div className="perf-distribution-rows">
        {rows.map((row) => (
          <div className="perf-distribution-row" key={row.label}>
            <span>{row.label}</span>
            <div className="perf-distribution-track">
              <i style={{ width: `${Math.max((Number(row.count || 0) / maxCount) * 100, row.count ? 6 : 0)}%` }} />
            </div>
            <strong>{formatMetric(row.count)}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

export function BreakdownBar({ breakdown = {} }) {
  const items = [
    { key: 'llm_wall_ms', label: 'LLM', className: 'llm', value: Number(breakdown.llm_wall_ms || 0) },
    { key: 'tool_ms', label: '工具', className: 'tool', value: Number(breakdown.tool_ms || 0) },
    { key: 'runtime_phase_ms', label: '运行阶段', className: 'phase', value: Number(breakdown.runtime_phase_ms || 0) },
    { key: 'other_ms', label: '其他', className: 'other', value: Number(breakdown.other_ms || 0) },
  ];
  const total = items.reduce((sum, item) => sum + item.value, 0);

  return (
    <section className="perf-breakdown">
      <div className="perf-breakdown-bar" aria-label="运行耗时拆分">
        {items.map((item) => (
          <span
            key={item.key}
            className={item.className}
            style={{ width: `${total ? (item.value / total) * 100 : 0}%` }}
            title={`${item.label}: ${formatDuration(item.value)}`}
          />
        ))}
      </div>
      <div className="perf-breakdown-legend">
        {items.map((item) => (
          <div key={item.key}>
            <i className={item.className} />
            <span>{item.label}</span>
            <strong>{formatDuration(item.value)}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

export function IterationWaterfall({ iterations = [] }) {
  if (!iterations.length) {
    return <div className="perf-empty">本次运行没有迭代分段记录</div>;
  }

  const origin = Math.min(...iterations.map((item) => Number(item.first_ts_ms || 0)));
  const end = Math.max(...iterations.map((item) => Number(item.last_ts_ms || item.first_ts_ms || 0)));
  const range = Math.max(end - origin, 1);

  return (
    <section className="perf-iteration-waterfall">
      {iterations.map((item) => {
        const start = Number(item.first_ts_ms || origin);
        const finish = Number(item.last_ts_ms || start);
        const left = ((start - origin) / range) * 100;
        const width = Math.max(((finish - start) / range) * 100, 2);
        return (
          <div className="perf-iteration-row" key={`${item.caller}-${item.iteration}`}>
            <div>
              <strong title={item.caller}>{formatPerfCallerLabel(item.caller)}</strong>
              <span>迭代 {item.iteration}</span>
            </div>
            <div className="perf-iteration-track">
              <i style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }} />
            </div>
            <span>{formatDuration(item.metrics?.total_elapsed_ms || finish - start)}</span>
          </div>
        );
      })}
    </section>
  );
}

function buildTrend(points, valueKey, secondaryValueKey) {
  if (!Array.isArray(points) || points.length === 0) return null;
  const values = points.flatMap((point) => [
    Number(point?.[valueKey] || 0),
    secondaryValueKey ? Number(point?.[secondaryValueKey] || 0) : null,
  ]).filter((value) => value != null && Number.isFinite(value));
  const maxValue = Math.max(...values, 1);
  const minX = 52;
  const maxX = 566;
  const minY = 18;
  const maxY = 150;
  const scaleY = (value) => maxY - (Number(value || 0) / maxValue) * (maxY - minY);
  const gap = points.length === 1 ? 0 : (maxX - minX) / (points.length - 1);
  const builtPoints = points.map((point, index) => ({
    key: `${point.run_id || point.bucket_start_ms || point.turn_id || 'point'}-${index}`,
    x: minX + gap * index,
    primaryY: scaleY(point?.[valueKey]),
    secondaryY: secondaryValueKey ? scaleY(point?.[secondaryValueKey]) : null,
    label: pointLabel(point, index, points.length),
  }));
  return {
    points: builtPoints,
    gridY: [18, 62, 106, 150],
    primaryPath: toLinePath(builtPoints, 'primaryY'),
    secondaryPath: secondaryValueKey ? toLinePath(builtPoints, 'secondaryY') : null,
    axisLabels: [
      { y: 18, text: formatChartValue(maxValue) },
      { y: 84, text: formatChartValue(maxValue / 2) },
      { y: 150, text: '0' },
    ],
  };
}

function toLinePath(points, key) {
  return points
    .filter((point) => point[key] != null)
    .map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point[key]}`)
    .join(' ');
}

function pointLabel(point, index, total) {
  if (total > 10 && index !== 0 && index !== total - 1 && index % Math.ceil(total / 6) !== 0) {
    return '';
  }
  if (point.turn_id != null) return `T${point.turn_id}`;
  if (point.bucket_start_ms != null) {
    return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
      .format(new Date(point.bucket_start_ms));
  }
  return `${index + 1}`;
}

function formatChartValue(value) {
  if (value >= 1000) return formatDuration(value);
  return `${Math.round(value)}`;
}
