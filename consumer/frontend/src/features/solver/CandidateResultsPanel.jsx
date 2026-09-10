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

const batchLabel = (itemId) => {
  const text = cleanText(itemId);
  const numericSuffix = text.match(/(?:^|[_-])0*(?<suffix>\d+)$/u)?.groups
    .suffix;
  return numericSuffix ? String(Number(numericSuffix)) : text;
};

const batchList = (itemIds) =>
  itemIds.length > 0 ? itemIds.map(batchLabel).join('、') : '无';

const technicalValue = (value) => {
  if (value == null || value === '') {
    return '—';
  }
  if (
    typeof value === 'string' ||
    typeof value === 'number' ||
    typeof value === 'boolean'
  ) {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return '—';
  }
};

function TechnicalDetails({ row }) {
  const metrics = Object.entries(row.metrics).sort(([left], [right]) =>
    left.localeCompare(right),
  );
  return (
    <details className="solver-candidate-technical">
      <summary>技术详情</summary>
      <dl>
        <div>
          <dt>候选 ID</dt>
          <dd>
            <code>{row.candidateId}</code>
          </dd>
        </div>
        <div>
          <dt>校验状态</dt>
          <dd>{row.validationStatus}</dd>
        </div>
        {metrics.map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>
              <code>{technicalValue(value)}</code>
            </dd>
          </div>
        ))}
        {row.rejectionReasons.length > 0 && (
          <div>
            <dt>拒绝原因</dt>
            <dd>{row.rejectionReasons.join('、')}</dd>
          </div>
        )}
      </dl>
    </details>
  );
}

export function CandidateResultsTable({ normalized }) {
  const option0 = normalized.optionLabels.OPTION_0;
  const option1 = normalized.optionLabels.OPTION_1;
  return (
    <div className="solver-candidate-table-wrap">
      <table
        className="solver-candidate-table"
        aria-label={`候选方案，共 ${normalized.candidateCount} 种`}
      >
        <colgroup>
          <col className="solver-candidate-col-number" />
          <col className="solver-candidate-col-route" />
          <col className="solver-candidate-col-route" />
          <col className="solver-candidate-col-sequence" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col">组合</th>
            <th scope="col">
              <span className="solver-route-swatch is-option-0" />
              {option0}批次
            </th>
            <th scope="col">
              <span className="solver-route-swatch is-option-1" />
              {option1}批次
            </th>
            <th scope="col">1—{normalized.itemIds.length} 批路线序列</th>
          </tr>
        </thead>
        <tbody>
          {normalized.rows.map((row) => (
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
                <TechnicalDetails row={row} />
              </td>
              <td className="solver-route-batches is-option-0">
                {batchList(row.optionItemIds.OPTION_0)}
              </td>
              <td className="solver-route-batches is-option-1">
                {batchList(row.optionItemIds.OPTION_1)}
              </td>
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
  const staticState =
    !artifactRef && !artifactDigest ?
      { kind: 'unavailable', data: null, message: '历史结果未保存候选明细。' } :
      !requestKey ?
        {
          kind: 'integrity',
          data: null,
          message: '候选明细未通过完整性校验，已停止展示。',
        } :
        null;
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
          <small>{state.data.candidateCount} 种 · 按组合号排序</small>
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
