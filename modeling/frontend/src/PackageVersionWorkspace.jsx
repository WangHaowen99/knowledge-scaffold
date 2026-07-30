import React, { useCallback, useEffect, useRef, useState } from 'react';

import {
  getRevisionDiff,
  listRevisions,
  restoreRevision,
  subscribeProjectEvents,
} from './authoring-api.js';
import {
  acceptPackageWarnings,
  authFetch,
  exportModelingAuditUrl,
  getPackageCheck,
  releasePackage,
  requireOkResponse,
} from './api.js';
import VersionPanel from './VersionPanel.jsx';

export default function PackageVersionWorkspace({ projectId }) {
  const [revisionData, setRevisionData] = useState({ versions: [], currentRevision: '' });
  const [selectedRevision, setSelectedRevision] = useState('');
  const [diff, setDiff] = useState(undefined);
  const [loading, setLoading] = useState(true);
  const [diffLoading, setDiffLoading] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [auditExporting, setAuditExporting] = useState(false);
  const [warningReview, setWarningReview] = useState(null);
  const [releaseNote, setReleaseNote] = useState('');
  const [confirmingWarnings, setConfirmingWarnings] = useState(false);
  const [releasing, setReleasing] = useState(false);
  const [error, setError] = useState('');
  const [diffError, setDiffError] = useState('');
  const refreshTimerRef = useRef(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [versions, check] = await Promise.all([
        listRevisions(projectId),
        getPackageCheck(projectId),
      ]);
      setRevisionData(versions);
      setWarningReview(check.warningReview);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '版本读取失败');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    setRevisionData({ versions: [], currentRevision: '' });
    setSelectedRevision('');
    setDiff(undefined);
    void load();
  }, [load]);

  useEffect(() => {
    const unsubscribe = subscribeProjectEvents(projectId, {
      onEvent: (event) => {
        if (event.type !== 'package.revision.created') return;
        clearTimeout(refreshTimerRef.current);
        refreshTimerRef.current = setTimeout(() => {
          setSelectedRevision('');
          setDiff(undefined);
          void load();
        }, 80);
      },
    });
    return () => {
      clearTimeout(refreshTimerRef.current);
      unsubscribe();
    };
  }, [load, projectId]);

  const openRevision = async (id, item) => {
    setSelectedRevision(id);
    setDiffError('');
    if (!item.parentRevision) {
      setDiff({ fromRevision: id, toRevision: id, files: [] });
      return;
    }
    setDiffLoading(true);
    try {
      setDiff(await getRevisionDiff(projectId, item.parentRevision, id));
    } catch (cause) {
      setDiffError(cause instanceof Error ? cause.message : '修改读取失败');
    } finally {
      setDiffLoading(false);
    }
  };

  const restore = async (payload) => {
    setRestoring(true);
    setError('');
    try {
      await restoreRevision(projectId, payload);
      setSelectedRevision('');
      setDiff(undefined);
      await load();
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } finally {
      setRestoring(false);
    }
  };

  const exportAudit = async () => {
    if (auditExporting) return;
    setAuditExporting(true);
    setError('');
    try {
      const response = await requireOkResponse(await authFetch(exportModelingAuditUrl(projectId)));
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = response.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1]
        ?? `${projectId}-modeling-audit.zip`;
      link.click();
      URL.revokeObjectURL(objectUrl);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '建模审计包导出失败');
    } finally {
      setAuditExporting(false);
    }
  };

  const pendingReleaseWarnings = (warningReview?.items ?? []).filter((item) => (
    item.accepted && !item.releaseConfirmed && item.finding?.releaseBlocking !== true
  ));
  const releaseBlockingWarnings = (warningReview?.items ?? []).filter((item) => (
    item.finding?.releaseBlocking === true
  ));

  const confirmReleaseWarnings = async () => {
    if (!warningReview?.revision || !pendingReleaseWarnings.length || confirmingWarnings) return;
    setConfirmingWarnings(true);
    setError('');
    try {
      const result = await acceptPackageWarnings(
        projectId,
        warningReview.revision,
        pendingReleaseWarnings.map((item) => item.fingerprint),
        releaseNote,
        'release',
      );
      setWarningReview(result.warningReview);
      setReleaseNote('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '发布已知限制确认失败');
    } finally {
      setConfirmingWarnings(false);
    }
  };

  const release = async () => {
    if (releasing || !window.confirm('确认将当前验证候选正式发布？发布后会生成稳定版本修订。')) return;
    setReleasing(true);
    setError('');
    try {
      const result = await releasePackage(projectId);
      await load();
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
      window.alert(`已发布知识包 ${result.version}`);
    } catch (cause) {
      const gates = cause?.data?.gates?.filter((gate) => !gate.passed) ?? [];
      const details = gates.map((gate) => `${gate.label}：${gate.detail}`).join('；');
      setError(`${cause instanceof Error ? cause.message : '知识包发布失败'}${details ? `：${details}` : ''}`);
    } finally {
      setReleasing(false);
    }
  };

  return (
    <div className="wb-b" data-testid="package-version-workspace">
      <div className="package-version-actions">
        <button type="button" className="btn" disabled={auditExporting} onClick={() => { void exportAudit(); }}>
          {auditExporting ? '导出中…' : '导出建模审计包'}
        </button>
        <button type="button" className="btn btn-p" disabled={releasing} onClick={() => { void release(); }}>
          {releasing ? '发布中…' : '正式发布当前候选'}
        </button>
      </div>
      {warningReview?.warningTotal > 0 && (
        <div className="package-release-review">
          <strong>发布警告与已知限制</strong>
          {releaseBlockingWarnings.length > 0 && (
            <p className="lerr">{releaseBlockingWarnings.length} 条发布阻断警告必须修复，不能作为已知限制带入版本。</p>
          )}
          {warningReview.pendingValidation > 0 && (
            <p>还有 {warningReview.pendingValidation} 条警告尚未在知识包体检中接受，请先完成验证阶段确认。</p>
          )}
          {pendingReleaseWarnings.length > 0 ? (
            <>
              <p>请再次确认这 {pendingReleaseWarnings.length} 条非阻断警告可以作为本次发布的已知限制。</p>
              <textarea
                rows={2}
                value={releaseNote}
                placeholder="说明发布影响、使用约束和后续处理计划（必填）"
                onChange={(event) => setReleaseNote(event.target.value)}
              />
              <button
                type="button"
                className="btn"
                disabled={confirmingWarnings || releaseNote.trim().length < 4}
                onClick={() => { void confirmReleaseWarnings(); }}
              >
                {confirmingWarnings ? '确认中…' : `确认为 ${pendingReleaseWarnings.length} 条已知限制`}
              </button>
            </>
          ) : warningReview.readyForRelease && (
            <p>当前修订的警告已完成发布确认，会随正式版本写入已知限制记录。</p>
          )}
        </div>
      )}
      <VersionPanel
        versions={revisionData.versions}
        currentVersionId={revisionData.currentRevision}
        selectedVersionId={selectedRevision || undefined}
        diff={diff}
        loading={loading}
        diffLoading={diffLoading}
        restoring={restoring}
        error={error}
        diffError={diffError}
        onSelectVersion={openRevision}
        onRestore={restore}
      />
    </div>
  );
}
