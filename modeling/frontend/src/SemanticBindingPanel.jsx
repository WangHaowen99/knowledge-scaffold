import React, { useEffect, useMemo, useState } from 'react';

import {
  applySemanticBindings,
  clearSemanticBindings,
  getSemanticBindingSuggestions,
  ignoreSemanticBindings,
} from './api.js';
import { safeUiError } from './revision-state.js';

const asArray = (value) => (Array.isArray(value) ? value : []);
const refId = (ref, bindingKey) => `${ref.objectKey}.${ref.fieldKey}->${bindingKey}`;
const BATCH_SIZE = 20;

export default function SemanticBindingPanel({ projectId, packageRevision, disabled = false, onApplied }) {
  const [snapshot, setSnapshot] = useState(null);
  const [selectedBindings, setSelectedBindings] = useState(() => new Set());
  const [selectedRefs, setSelectedRefs] = useState(() => new Set());
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [visibleCount, setVisibleCount] = useState(BATCH_SIZE);

  useEffect(() => {
    let active = true;
    if (!projectId || !packageRevision) return () => { active = false; };
    setLoading(true);
    setError('');
    void getSemanticBindingSuggestions(projectId)
      .then((result) => {
        if (!active) return;
        const suggestions = asArray(result.suggestions);
        const fieldSuggestions = asArray(result.fieldSuggestions);
        setSnapshot({ ...result, suggestions, fieldSuggestions, confirmedBindings: asArray(result.confirmedBindings) });
        setVisibleCount(BATCH_SIZE);
        setSelectedBindings(new Set(suggestions.slice(0, BATCH_SIZE).map((item) => String(item.key))));
        setSelectedRefs(new Set(fieldSuggestions.slice(0, BATCH_SIZE).map((ref) => refId(ref, ref.bindingKey))));
      })
      .catch((cause) => { if (active) setError(safeUiError(cause, '术语绑定推荐读取失败')); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId, packageRevision]);

  const suggestions = snapshot?.suggestions ?? [];
  const fieldSuggestions = snapshot?.fieldSuggestions ?? [];
  const confirmedBindings = snapshot?.confirmedBindings ?? [];
  const hasConfirmedBindings = confirmedBindings.length > 0;
  const bindingLabelByKey = useMemo(() => new Map(confirmedBindings.map((binding) => [
    binding.key,
    `${binding.resource}.${binding.valueField} -> ${binding.resource}.${binding.semanticField}`,
  ])), [confirmedBindings]);
  const selectedRefPayload = useMemo(() => fieldSuggestions
    .filter((ref) => selectedRefs.has(refId(ref, ref.bindingKey)))
    .map((ref) => ({ objectKey: ref.objectKey, fieldKey: ref.fieldKey, bindingKey: ref.bindingKey })),
  [selectedRefs, fieldSuggestions]);
  const working = disabled || loading || busy;
  const selectedBindingKeys = [...selectedBindings];
  const bindingStage = suggestions.length > 0;
  const fieldStage = !bindingStage && fieldSuggestions.length > 0;
  const activeTotal = bindingStage ? suggestions.length : fieldStage ? fieldSuggestions.length : 0;
  const activeVisibleCount = Math.min(visibleCount, activeTotal);
  const visibleSuggestions = suggestions.slice(0, activeVisibleCount);
  const visibleFieldSuggestions = fieldSuggestions.slice(0, activeVisibleCount);
  const hasMore = activeVisibleCount < activeTotal;
  const stageLabel = bindingStage ? '1/2 step' : '2/2 step';
  const title = bindingStage ? '术语绑定推荐' : '业务字段关联术语推荐';

  const toggleBinding = (key) => {
    setSelectedBindings((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };
  const toggleRef = (id) => {
    setSelectedRefs((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const showMore = () => {
    if (!snapshot || working) return;
    const nextVisibleCount = Math.min(visibleCount + BATCH_SIZE, activeTotal);
    if (bindingStage) {
      const newlyVisible = suggestions.slice(visibleCount, nextVisibleCount);
      setSelectedBindings((current) => {
        const next = new Set(current);
        for (const item of newlyVisible) next.add(String(item.key));
        return next;
      });
    } else if (fieldStage) {
      const newlyVisible = fieldSuggestions.slice(visibleCount, nextVisibleCount);
      setSelectedRefs((current) => {
        const next = new Set(current);
        for (const ref of newlyVisible) next.add(refId(ref, ref.bindingKey));
        return next;
      });
    }
    setVisibleCount(nextVisibleCount);
  };
  const submit = async () => {
    if (!snapshot || working) return;
    setBusy(true);
    setError('');
    try {
      const result = await applySemanticBindings(projectId, {
        expectedRevision: snapshot.revision,
        bindingKeys: bindingStage ? selectedBindingKeys : [],
        fieldRefs: bindingStage ? [] : selectedRefPayload,
      });
      const refreshed = await getSemanticBindingSuggestions(projectId);
      const suggestions = asArray(refreshed.suggestions);
      const fieldSuggestions = asArray(refreshed.fieldSuggestions);
      setSnapshot({ ...refreshed, suggestions, fieldSuggestions, confirmedBindings: asArray(refreshed.confirmedBindings) });
      setVisibleCount(BATCH_SIZE);
      setSelectedBindings(new Set(suggestions.slice(0, BATCH_SIZE).map((item) => String(item.key))));
      setSelectedRefs(new Set(fieldSuggestions.slice(0, BATCH_SIZE).map((ref) => refId(ref, ref.bindingKey))));
      onApplied?.(result);
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (cause) {
      setError(safeUiError(cause, '术语绑定确认失败'));
    } finally {
      setBusy(false);
    }
  };
  const ignoreAll = async () => {
    if (!snapshot || working) return;
    setBusy(true);
    setError('');
    try {
      const result = await ignoreSemanticBindings(projectId, {
        expectedRevision: snapshot.revision,
      });
      const suggestions = asArray(result.suggestions);
      const fieldSuggestions = asArray(result.fieldSuggestions);
      setSnapshot({ ...result, suggestions, fieldSuggestions, confirmedBindings: asArray(result.confirmedBindings) });
      setVisibleCount(BATCH_SIZE);
      setSelectedBindings(new Set());
      setSelectedRefs(new Set());
      onApplied?.(result);
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (cause) {
      setError(safeUiError(cause, '术语绑定推荐忽略失败'));
    } finally {
      setBusy(false);
    }
  };
  const clearConfirmed = async () => {
    if (!snapshot || working || !hasConfirmedBindings) return;
    setBusy(true);
    setError('');
    try {
      const result = await clearSemanticBindings(projectId, {
        expectedRevision: snapshot.revision,
      });
      const refreshed = await getSemanticBindingSuggestions(projectId);
      const suggestions = asArray(refreshed.suggestions);
      const fieldSuggestions = asArray(refreshed.fieldSuggestions);
      setSnapshot({ ...refreshed, suggestions, fieldSuggestions, confirmedBindings: asArray(refreshed.confirmedBindings) });
      setVisibleCount(BATCH_SIZE);
      setSelectedBindings(new Set(suggestions.slice(0, BATCH_SIZE).map((item) => String(item.key))));
      setSelectedRefs(new Set(fieldSuggestions.slice(0, BATCH_SIZE).map((ref) => refId(ref, ref.bindingKey))));
      onApplied?.(result);
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (cause) {
      setError(safeUiError(cause, '已确认术语绑定清空失败'));
    } finally {
      setBusy(false);
    }
  };

  if (loading && !snapshot) return <section className="semantic-binding-panel"><div className="empty">正在分析术语绑定候选…</div></section>;
  if (!suggestions.length && !fieldSuggestions.length && !error) return null;

  return (
    <section className="semantic-binding-panel" aria-label="术语绑定批量确认">
      <header className="semantic-binding-head">
        <div>
          <h3>{title}</h3>
          <p>{bindingStage
            ? `${stageLabel} · 发现 ${suggestions.length} 个候选术语绑定，当前显示 ${activeVisibleCount} 个`
            : `${stageLabel} · 发现 ${fieldSuggestions.length} 个业务字段关联候选，当前显示 ${activeVisibleCount} 个`}</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {hasConfirmedBindings && (
            <button type="button" className="btn" disabled={working} onClick={clearConfirmed}>清空已确认</button>
          )}
          {(bindingStage || fieldStage) && (
            <button type="button" className="btn" disabled={working} onClick={ignoreAll}>全部忽略</button>
          )}
          {(bindingStage || fieldStage) && (
            <button type="button" className="btn btn-p" disabled={working || (bindingStage ? !selectedBindings.size : !selectedRefPayload.length)} onClick={submit}>
              {busy ? '确认中...' : bindingStage ? `确认术语绑定 ${selectedBindings.size}` : `确认字段关联 ${selectedRefPayload.length}`}
            </button>
          )}
        </div>
      </header>
      <div className="semantic-binding-list">
        {bindingStage && visibleSuggestions.map((item) => (
          <article key={item.key} className="semantic-binding-item">
            <label className="semantic-binding-main">
              <input type="checkbox" disabled={working}
                checked={selectedBindings.has(item.key)}
                onChange={() => toggleBinding(item.key)} />
              <span>
                <strong>{item.resource}.{item.valueField} {'->'} {item.resource}.{item.semanticField}</strong>
              </span>
            </label>
            <div className="semantic-binding-meta">
              置信度 {Math.round(Number(item.confidence ?? 0) * 100)}%
              {asArray(item.aliasFields).length ? ` · 别名字段：${item.aliasFields.join('、')}` : ''}
            </div>
          </article>
        ))}
        {!bindingStage && visibleFieldSuggestions.map((ref) => {
          const id = refId(ref, ref.bindingKey);
          const hasCoverageCount = Number.isFinite(ref.matchedValueCount) && Number.isFinite(ref.sourceValueCount);
          return (
            <article key={id} className="semantic-binding-item">
              <label className="semantic-binding-main">
                <input type="checkbox" disabled={working}
                  checked={selectedRefs.has(id)}
                  onChange={() => toggleRef(id)} />
                <span>
                  <strong>{ref.objectKey}.{ref.fieldKey}</strong>
                  <em>{bindingLabelByKey.get(ref.bindingKey) || ref.bindingKey}</em>
                </span>
              </label>
              <div className="semantic-binding-meta">
                {ref.sourceSheet ? `${ref.sourceSheet}.` : ''}{ref.sourceColumn} · 覆盖率 {Math.round(Number(ref.coverage ?? 0) * 100)}%
                {hasCoverageCount ? ` · 命中 ${ref.matchedValueCount}/${ref.sourceValueCount} 个取值` : ''}
              </div>
            </article>
          );
        })}
      </div>
      {hasMore && (
        <div className="semantic-binding-more">
          <button type="button" className="btn" disabled={working} onClick={showMore}>
            继续显示 {Math.min(BATCH_SIZE, activeTotal - activeVisibleCount)} 个
          </button>
        </div>
      )}
      {error && <div role="alert" className="semantic-binding-error">{error}</div>}
    </section>
  );
}
