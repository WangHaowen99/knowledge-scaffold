import React, { useEffect, useMemo, useState } from 'react';
import ArtifactDiff from './ArtifactDiff.jsx';
import {
  makeRestoreRequest,
  normalizeVersions,
  safeUiError,
  versionActor,
  versionSummary,
  versionTitle,
} from './revision-state.js';

const styles = {
  shell: { minWidth: 0 },
  header: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 },
  count: { color: '#667085', fontSize: 12 },
  list: { borderTop: '1px solid #eaecf0' },
  row: { display: 'flex', flexWrap: 'wrap', gap: '8px 12px', alignItems: 'center', padding: '10px 4px', borderBottom: '1px solid #eaecf0' },
  versionButton: { flex: '1 1 110px', border: 0, padding: 0, background: 'transparent', textAlign: 'left', cursor: 'pointer', minWidth: 0 },
  titleLine: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' },
  title: { color: '#101828', fontSize: 13, fontWeight: 700 },
  current: { padding: '1px 5px', borderRadius: 4, background: '#ecfdf3', color: '#067647', fontSize: 11, fontWeight: 650 },
  package: { color: '#475467', fontSize: 11.5 },
  summary: { flex: '2 1 160px', minWidth: 0, color: '#344054', fontSize: 12.5, overflowWrap: 'anywhere' },
  meta: { display: 'block', marginTop: 3, color: '#667085', fontSize: 11.5 },
  actions: { display: 'flex', flex: '0 1 auto', gap: 6, marginLeft: 'auto', justifyContent: 'flex-end', flexWrap: 'wrap' },
  button: { padding: '4px 10px', fontSize: 11.5 },
  selected: { background: '#f8fbff' },
  confirm: { margin: '10px 0 14px', padding: 12, border: '1px solid #fdb022', borderRadius: 8, background: '#fffaeb' },
  confirmTitle: { marginBottom: 4, color: '#7a2e0e', fontSize: 13, fontWeight: 700 },
  confirmText: { color: '#854a0e', fontSize: 12.5, lineHeight: 1.55 },
  confirmActions: { display: 'flex', gap: 8, marginTop: 10 },
  state: { padding: '24px 8px', textAlign: 'center', color: '#667085', fontSize: 13 },
  error: { marginBottom: 10, padding: '8px 10px', color: '#b42318', background: '#fef3f2', borderRadius: 6, fontSize: 12.5 },
  diff: { marginTop: 14 },
};

const formatTime = (value) => {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
};

/**
 * Props:
 * - versions: version items returned by the server
 * - currentVersionId: current version id, used for conflict-safe restore
 * - selectedVersionId: optional controlled selection
 * - diff: changes for the selected version; omit to hide the change view
 * - onSelectVersion(id, item): load a version's changes
 * - onRestore({ targetRevision, expectedRevision }): restore callback
 */
export default function VersionPanel({
  versions = [],
  currentVersionId = '',
  selectedVersionId,
  diff,
  loading = false,
  diffLoading = false,
  restoring = false,
  error = '',
  diffError = '',
  onSelectVersion,
  onRestore,
  onOpenFile,
}) {
  const items = useMemo(() => normalizeVersions(versions), [versions]);
  const currentId = currentVersionId || items[0]?.id || '';
  const [localSelectedId, setLocalSelectedId] = useState('');
  const [restoreTarget, setRestoreTarget] = useState(null);
  const [restoreError, setRestoreError] = useState('');
  const [localRestoring, setLocalRestoring] = useState(false);
  const requestedActiveId = selectedVersionId === undefined
    ? (localSelectedId || currentId)
    : selectedVersionId;
  const activeId = items.some((item) => item.id === requestedActiveId)
    ? requestedActiveId
    : (items[0]?.id || '');
  const isRestoring = restoring || localRestoring;

  useEffect(() => {
    setLocalSelectedId('');
    setRestoreTarget(null);
    setRestoreError('');
  }, [currentId]);

  const select = (item) => {
    if (selectedVersionId === undefined) setLocalSelectedId(item.id);
    onSelectVersion?.(item.id, item);
  };

  const confirmRestore = async () => {
    if (!restoreTarget || !onRestore) return;
    setRestoreError('');
    setLocalRestoring(true);
    try {
      await onRestore(makeRestoreRequest(restoreTarget.id, currentId));
      setRestoreTarget(null);
    } catch (cause) {
      setRestoreError(safeUiError(cause, '恢复失败，请刷新后重试'));
    } finally {
      setLocalRestoring(false);
    }
  };

  return (
    <section style={styles.shell} aria-label="版本">
      <div style={styles.header}>
        <strong style={{ fontSize: 14 }}>版本</strong>
        {!loading && <span style={styles.count}>{items.length} 个</span>}
      </div>
      {error && <div role="alert" style={styles.error}>{safeUiError(error, '版本读取失败，请重试')}</div>}
      {loading && <div style={styles.state}>正在读取版本...</div>}
      {!loading && !items.length && <div style={styles.state}>暂无版本</div>}

      {!loading && items.length > 0 && (
        <div style={styles.list}>
          {items.map((item) => {
            const isCurrent = item.id === currentId;
            const isSelected = item.id === activeId;
            const time = formatTime(item.createdAt);
            return (
              <div key={item.id} style={{ ...styles.row, ...(isSelected ? styles.selected : {}) }}>
                <button type="button" style={styles.versionButton} onClick={() => select(item)} aria-current={isSelected ? 'true' : undefined}>
                  <span style={styles.titleLine}>
                    <span style={styles.title}>{versionTitle(item)}</span>
                    {isCurrent && <span style={styles.current}>当前</span>}
                    {item.packageVersion && <span style={styles.package}>v{item.packageVersion.replace(/^v/i, '')}</span>}
                  </span>
                </button>

                <div style={styles.summary}>
                  {versionSummary(item)}
                  <span style={styles.meta}>
                    {[versionActor(item), time, item.changedPaths.length ? `${item.changedPaths.length} 个文件` : ''].filter(Boolean).join(' · ')}
                  </span>
                </div>

                <div style={styles.actions}>
                  <button type="button" className="btn" style={styles.button} onClick={() => select(item)}>
                    查看修改
                  </button>
                  {!isCurrent && onRestore && (
                    <button type="button" className="btn" style={styles.button} disabled={isRestoring} onClick={() => { setRestoreError(''); setRestoreTarget(item); }}>
                      恢复
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {restoreTarget && (
        <div role="alertdialog" aria-labelledby="restore-title" style={styles.confirm}>
          <div id="restore-title" style={styles.confirmTitle}>恢复到{versionTitle(restoreTarget)}？</div>
          <div style={styles.confirmText}>当前内容会保存为新版本，已有版本不会删除。</div>
          {restoreError && <div role="alert" style={{ ...styles.error, marginTop: 8, marginBottom: 0 }}>{restoreError}</div>}
          <div style={styles.confirmActions}>
            <button type="button" className="btn btn-p" style={styles.button} disabled={isRestoring} onClick={confirmRestore}>
              {isRestoring ? '正在恢复...' : '确认恢复'}
            </button>
            <button type="button" className="btn" style={styles.button} disabled={isRestoring} onClick={() => setRestoreTarget(null)}>
              取消
            </button>
          </div>
        </div>
      )}

      {diff !== undefined && activeId && (
        <div style={styles.diff}>
          <ArtifactDiff diff={diff} loading={diffLoading} error={diffError} onOpenFile={onOpenFile} />
        </div>
      )}
    </section>
  );
}
