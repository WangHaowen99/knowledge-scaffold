// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { Fragment, useEffect, useState } from 'react';
import {
  AlertTriangle,
  ListChecks,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  Trophy,
} from 'lucide-react';

import { getSolverCandidateResults } from './api.js';
import { normalizeCandidateResults } from './candidateResults.js';
import './solver.css';

const cleanText = (value) => String(value ?? '').trim();

// Artifact metrics are integers the solver multiplied by its own metric scale, so
// the business number is value / scale shown to as many decimals as the scale holds.
const metricText = (value, descriptor) => {
  const fractionDigits = Math.max(0, Math.round(Math.log10(descriptor.scale)));
  const amount = (value / descriptor.scale).toLocaleString('zh-CN', {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
  // Percentages read wrong with a separator; every other unit is a noun.
  return descriptor.unit === '%' ? `${amount}%` : `${amount} ${descriptor.unit}`;
};

// The winner leads so the first row read is the baseline every other row is compared
// against; the rest keep the combination order the artifact already guarantees.
const comparisonOrder = (rows) => [
  ...rows.filter((row) => row.selected),
  ...rows.filter((row) => !row.selected),
];

export function CandidateResultsTable({ normalized }) {
  const metrics = normalized.metricDisplay;
  const metricHeading = metrics.map((descriptor) => descriptor.label).join(' / ');
  return (
    <div className="solver-candidate-table-wrap">
      <table
        className="solver-candidate-table"
        aria-label={`候选方案，共 ${normalized.candidateCount} 种`}
      >
        <colgroup>
          <col className="solver-candidate-col-number" />
          {metrics.length > 0 && <col className="solver-candidate-col-metrics" />}
          <col className="solver-candidate-col-sequence" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col">组合</th>
            {metrics.length > 0 && <th scope="col">{metricHeading}</th>}
            <th scope="col">1—{normalized.itemIds.length} 批路线序列</th>
          </tr>
        </thead>
        <tbody>
          {comparisonOrder(normalized.rows).map((row) => (
            <tr
              className={`solver-candidate-row${row.selected ? ' is-selected' : ''}`}
              key={row.candidateId}
              data-combination-no={row.combinationNo}
              data-selected={row.selected ? 'true' : 'false'}
            >
              <td>
                <span className="solver-combination-number">
                  {row.combinationNo}
                </span>
                {row.selected && (
                  <span className="solver-winner-label">
                    <Trophy size={11} />
                    最优方案
                  </span>
                )}
                {!row.eligible && (
                  <span className="solver-infeasible-label">
                    <ShieldAlert size={11} />
                    不可行
                  </span>
                )}
              </td>
              {metrics.length > 0 && (
                <td>
                  <dl className="solver-candidate-metrics">
                    {metrics.map((descriptor) => {
                      const value = row.metrics[descriptor.key];
                      const adverse = descriptor.sense === 'maximize' ? value < 0 : value > 0;
                      return (
                        <div className={adverse ? 'is-adverse' : undefined} key={descriptor.key}>
                          <dt>{descriptor.label}</dt>
                          <dd>{metricText(value, descriptor)}</dd>
                        </div>
                      );
                    })}
                  </dl>
                </td>
              )}
              <td>
                <div className="solver-route-sequence">
                  {row.assignment.map((option, index) => (
                    <Fragment key={`${row.candidateId}:${index}`}>
                      <span
                        className={`is-${option.toLowerCase().replace('_', '-')}`}
                      >
                        {row.sequenceLabels[index]}
                      </span>
                      {index < row.assignment.length - 1 && (
                        <b aria-hidden="true">→</b>
                      )}
                    </Fragment>
                  ))}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const initialState = Object.freeze({
  requestKey: '',
  kind: 'loading',
  data: null,
  message: '',
});

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
export default function CandidateResultsPanel({ result }) {
  const jobId = cleanText(result?.job_id);
  const artifactRef = cleanText(result?.candidate_results_artifact_ref);
  const artifactDigest = cleanText(result?.candidate_results_artifact_digest);
  const [reloadToken, setReloadToken] = useState(0);
  const [asyncState, setAsyncState] = useState(initialState);
  const requestKey =
    jobId && artifactRef && artifactDigest ?
      `${jobId}:${artifactRef}:${artifactDigest}:${reloadToken}` :
      '';
  let staticState = null;
  if (!artifactRef && !artifactDigest) {
    staticState = {
      kind: 'unavailable',
      data: null,
      message: '历史结果未保存候选明细。',
    };
  } else if (!requestKey) {
    staticState = {
      kind: 'integrity',
      data: null,
      message: '候选明细未通过完整性校验，已停止展示。',
    };
  }
  const state =
    staticState ||
    (asyncState.requestKey === requestKey ? asyncState : initialState);

  useEffect(() => {
    if (!requestKey) {
      return undefined;
    }
    const controller = new AbortController();
    getSolverCandidateResults(jobId, { signal: controller.signal })
      .then((payload) => normalizeCandidateResults(payload))
      .then((data) => {
        if (!controller.signal.aborted) {
          setAsyncState({ requestKey, kind: 'ready', data, message: '' });
        }
      })
      .catch((error) => {
        if (error?.name === 'AbortError' || controller.signal.aborted) {
          return;
        }
        if (error?.code === 'candidate_not_found' && error?.status === 404) {
          setAsyncState({
            requestKey,
            kind: 'unavailable',
            data: null,
            message: '历史结果未保存候选明细。',
          });
          return;
        }
        if (
          error?.code === 'candidate_digest_mismatch' ||
          error?.code === 'candidate_results_invalid'
        ) {
          setAsyncState({
            requestKey,
            kind: 'integrity',
            data: null,
            message: '候选明细未通过完整性校验，已停止展示。',
          });
          return;
        }
        setAsyncState({
          requestKey,
          kind: 'error',
          data: null,
          message: error?.message || '候选明细加载失败。',
        });
      });
    return () => controller.abort();
  }, [jobId, requestKey]);

  return (
    <div className={`solver-candidate-results state-${state.kind}`}>
      <header className="solver-candidate-heading">
        <span>
          <ListChecks size={15} />
          全部候选方案
        </span>
        {state.data && (
          <small>{state.data.candidateCount} 种 · 最优方案置顶</small>
        )}
      </header>
      {state.kind === 'loading' && (
        <div className="solver-candidate-state" role="status">
          <LoaderCircle className="spin" size={15} />
          正在加载候选方案
        </div>
      )}
      {state.kind === 'unavailable' && (
        <div className="solver-candidate-state">
          <AlertTriangle size={15} />
          {state.message}
        </div>
      )}
      {state.kind === 'integrity' && (
        <div className="solver-candidate-state is-error" role="alert">
          <ShieldAlert size={15} />
          {state.message}
        </div>
      )}
      {state.kind === 'error' && (
        <div className="solver-candidate-state is-error" role="alert">
          <AlertTriangle size={15} />
          <span>{state.message}</span>
          <button
            type="button"
            title="重新加载候选明细"
            aria-label="重新加载候选明细"
            onClick={() => setReloadToken((value) => value + 1)}
          >
            <RefreshCw size={14} />
          </button>
        </div>
      )}
      {state.kind === 'ready' && state.data && (
        <CandidateResultsTable normalized={state.data} />
      )}
    </div>
  );
}
