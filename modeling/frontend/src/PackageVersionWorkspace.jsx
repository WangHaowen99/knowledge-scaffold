import React, { useCallback, useEffect, useRef, useState } from 'react';

import {
  getRevisionDiff,
  listRevisions,
  restoreRevision,
  subscribeProjectEvents,
} from './authoring-api.js';
import { authFetch, exportModelingAuditUrl, requireOkResponse } from './api.js';
import VersionPanel from './VersionPanel.jsx';

export default function PackageVersionWorkspace({ projectId }) {
  const [revisionData, setRevisionData] = useState({ versions: [], currentRevision: '' });
  const [selectedRevision, setSelectedRevision] = useState('');
  const [diff, setDiff] = useState(undefined);
  const [loading, setLoading] = useState(true);
  const [diffLoading, setDiffLoading] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [auditExporting, setAuditExporting] = useState(false);
  const [error, setError] = useState('');
  const [diffError, setDiffError] = useState('');
  const refreshTimerRef = useRef(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setRevisionData(await listRevisions(projectId));
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

  return (
    <div className="wb-b" data-testid="package-version-workspace">
      <div className="package-version-actions">
        <button type="button" className="btn" disabled={auditExporting} onClick={() => { void exportAudit(); }}>
          {auditExporting ? '导出中…' : '导出建模审计包'}
        </button>
      </div>
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
