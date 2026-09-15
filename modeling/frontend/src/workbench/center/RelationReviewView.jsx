import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react';

import { store, actions } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import {
  applyObjectRelationReview,
  getObjectRelationReview,
} from '../studio/studio-api.js';
import './relation-review.css';

const PAGE_SIZE = 10;
const selProjectId = (state) => state.projectId;

export default function RelationReviewView() {
  const projectId = useStore(store, selProjectId);
  const [page, setPage] = useState(0);
  const [state, setState] = useState({ phase: 'loading', candidates: [], total: 0, message: '' });
  const [selected, setSelected] = useState(() => new Set());
  const [relations, setRelations] = useState(() => new Map());
  const [errors, setErrors] = useState(() => new Map());
  const reviewRef = useRef('');

  const load = useCallback(async (nextPage = page) => {
    if (!projectId) return;
    setState((current) => ({ ...current, phase: 'loading', message: '' }));
    try {
      const response = await getObjectRelationReview(projectId, {
        offset: nextPage * PAGE_SIZE,
        limit: PAGE_SIZE,
      }, { cache: 'no-cache' });
      // A new review can contain fewer candidates than the page the user was
      // previously viewing. Rewind instead of showing an empty page as if
      // there were no candidates at all.
      const total = Number(response.total) || 0;
      if (nextPage > 0 && total > 0 && nextPage * PAGE_SIZE >= total) {
        setPage(0);
        return;
      }
      if (reviewRef.current !== response.review_id) {
        reviewRef.current = response.review_id || '';
        setSelected(new Set(response.selected_fingerprints || []));
        setRelations(new Map((response.candidates || [])
          .map((candidate) => [candidate.fingerprint, candidate.relation])));
      } else {
        const validFingerprints = new Set(response.all_fingerprints || []);
        setSelected((current) => new Set(
          [...current].filter((fingerprint) => validFingerprints.has(fingerprint)),
        ));
        setRelations((current) => {
          const next = new Map(current);
          for (const fingerprint of next.keys()) {
            if (!validFingerprints.has(fingerprint)) next.delete(fingerprint);
          }
          for (const candidate of response.candidates || []) {
            if (!next.has(candidate.fingerprint)) next.set(candidate.fingerprint, candidate.relation);
          }
          return next;
        });
      }
      setErrors(new Map((response.candidates || [])
        .filter((candidate) => candidate.error)
        .map((candidate) => [candidate.fingerprint, candidate.error])));
      setState({ ...response, phase: 'ready', message: '' });
    } catch (error) {
      setState((current) => ({
        ...current,
        phase: 'error',
        message: error instanceof Error ? error.message : '对象关系暂时无法读取',
      }));
    }
  }, [page, projectId]);

  useEffect(() => { void load(page); }, [load, page]);

  const submit = async () => {
    if (!state.review_id || state.phase === 'submitting') return;
    setState((current) => ({ ...current, phase: 'submitting', message: '' }));
    try {
      const response = await applyObjectRelationReview(projectId, {
        review_id: state.review_id,
        expected_revision: state.working_revision,
        selected_fingerprints: [...selected],
        relation_edits: [...relations].map(([fingerprint, relation]) => ({ fingerprint, relation })),
      });
      setErrors(new Map());
      setState((current) => ({
        ...current,
        ...response,
        phase: 'applied',
        review_status: 'applied',
        message: response.display?.text || '对象关系已经写入工作版本',
      }));
      await actions.refreshSnapshot();
      store.set({
        focus: { kind: 'studio', target: '知识包 Studio', view: 'graph' },
        follow: false,
        pendingFocus: null,
      });
    } catch (error) {
      const relationErrors = Array.isArray(error?.data?.relations) ? error.data.relations : [];
      setErrors(new Map(relationErrors.map((item) => [item.fingerprint, item])));
      if (relationErrors.length) {
        const first = Math.min(...relationErrors.map((item) => Number(item.ordinal) || 0));
        setPage(Math.floor(first / PAGE_SIZE));
      }
      setState((current) => ({
        ...current,
        phase: 'ready',
        message: error instanceof Error ? error.message : '关系写入失败，请调整后重试',
      }));
    }
  };

  const selectAll = () => {
    const fingerprints = Array.isArray(state.all_fingerprints)
      ? state.all_fingerprints
      : (state.candidates || []).map((candidate) => candidate.fingerprint);
    setSelected(new Set(fingerprints));
  };

  const clearAll = () => setSelected(new Set());

  if (state.phase === 'loading' && !state.review_id) {
    return <div className="relation-review-state"><RefreshCw className="spin" size={18} />正在整理对象关系…</div>;
  }
  if (state.phase === 'error' && !state.review_id) {
    return <div className="relation-review-state" role="alert">{state.message || '对象关系暂时无法读取'}</div>;
  }
  if (!state.available) return <div className="relation-review-state">当前计划没有对象关系任务。</div>;
  if (!state.ready && state.review_status !== 'applied') {
    return <div className="relation-review-state">对象或业务操作任务尚未完成，关系审阅将在依赖完成后自动开始。</div>;
  }

  const totalPages = Math.max(1, Math.ceil((state.total || 0) / PAGE_SIZE));
  const selectedCount = selected.size;
  return (
    <section className="relation-review">
      <header>
        <div>
          <h2>确认对象间关系</h2>
          <p>{state.auto_completed
            ? '当前没有关系候选，关系审阅任务已自动完成。'
            : '标记为“默认建议接受”的候选已默认勾选；取消勾选的关系不会写入。'}</p>
        </div>
        <div className="relation-review-count"><b>{selectedCount}</b>/{state.total || 0} 条将写入</div>
      </header>

      {state.message && (
        <div className={`relation-review-message${errors.size ? ' error' : ''}`} role={errors.size ? 'alert' : 'status'}>
          {state.message}
        </div>
      )}

      <div className="relation-review-list">
        {(state.candidates || []).map((candidate) => {
          const checked = selected.has(candidate.fingerprint);
          const failure = errors.get(candidate.fingerprint) || candidate.error;
          return (
            <div className={`relation-review-row${failure ? ' invalid' : ''}`} key={candidate.fingerprint}>
              <input type="checkbox" checked={checked} disabled={state.phase === 'submitting' || state.review_status === 'applied'}
                aria-label={`${candidate.source_object} 到 ${candidate.target_object} 的关系是否写入`}
                onChange={() => setSelected((current) => {
                  const next = new Set(current);
                  if (next.has(candidate.fingerprint)) next.delete(candidate.fingerprint);
                  else next.add(candidate.fingerprint);
                  return next;
                })} />
              <span className="relation-review-main">
                <span className="relation-review-triple">
                  <b>{candidate.source_object}</b><span>→</span>
                  <input className="relation-review-name" value={relations.get(candidate.fingerprint) ?? candidate.relation}
                    aria-label={`${candidate.source_object} 到 ${candidate.target_object} 的关系名称`}
                    disabled={state.phase === 'submitting' || state.review_status === 'applied'}
                    onChange={(event) => setRelations((current) => {
                      const next = new Map(current);
                      next.set(candidate.fingerprint, event.target.value);
                      return next;
                    })} />
                  <span>→</span><b>{candidate.target_object}</b>
                </span>
                <small>{candidate.reason || candidate.note}</small>
                {failure && <em>{failure.message}</em>}
              </span>
              {candidate.default_selected && <span className="relation-review-default"><Check size={12} />默认建议接受</span>}
            </div>
          );
        })}
        {!state.candidates?.length && <div className="relation-review-empty">没有需要确认的对象关系候选。</div>}
      </div>

      <footer>
        <button className="btn btn-sm" disabled={state.phase === 'submitting' || state.review_status === 'applied' || state.auto_completed}
          onClick={selectAll}>
          全部选中
        </button>
        <button className="btn btn-sm" disabled={state.phase === 'submitting' || state.review_status === 'applied' || state.auto_completed}
          onClick={clearAll}>
          全部取消
        </button>
        <span className="spacer" />
        <button className="btn btn-sm" disabled={page <= 0 || state.phase === 'submitting'}
          onClick={() => setPage((value) => Math.max(0, value - 1))}>
          <ChevronLeft size={14} />上一页
        </button>
        <span>第 {page + 1}/{totalPages} 页</span>
        <button className="btn btn-sm" disabled={page + 1 >= totalPages || state.phase === 'submitting'}
          onClick={() => setPage((value) => Math.min(totalPages - 1, value + 1))}>
          下一页<ChevronRight size={14} />
        </button>
        <button className="btn btn-p" disabled={state.phase === 'submitting' || state.review_status === 'applied'}
          onClick={() => void submit()}>
          {state.phase === 'submitting' ? '正在校验并写入…' : state.auto_completed ? '无需确认' : state.review_status === 'applied' ? '已写入工作版本' : '确认并写入'}
        </button>
      </footer>
    </section>
  );
}
