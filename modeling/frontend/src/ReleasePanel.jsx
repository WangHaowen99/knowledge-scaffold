import React, { useEffect, useMemo, useState } from 'react';
import {
  acceptPackageWarnings,
  authFetch,
  exportModelingAuditUrl,
  releaseProject,
} from './api.js';

const FALLBACK_GATES = [
  ['package_check', 'validate', '内容检查'],
  ['flowchart', 'flowchart', '流程图确认'],
  ['evaluation', 'evaluate', '评测'],
];

const styles = {
  panel: { maxWidth: 560, margin: '8px auto', padding: '8px 0' },
  head: { display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12, marginBottom: 14 },
  title: { margin: 0, fontSize: 16 },
  version: { color: 'var(--muted)', fontSize: 12 },
  list: { listStyle: 'none', padding: 0, margin: '0 0 18px', borderTop: '1px solid #e6e9f0' },
  row: { display: 'grid', gridTemplateColumns: '22px minmax(80px, 1fr) auto', alignItems: 'center', gap: 8, minHeight: 46, borderBottom: '1px solid #e6e9f0' },
  mark: { fontWeight: 800, textAlign: 'center' },
  label: { fontSize: 13, fontWeight: 650 },
  detail: { color: 'var(--muted)', fontSize: 12, textAlign: 'right' },
  action: { display: 'flex', alignItems: 'center', gap: 12 },
  message: { color: 'var(--muted)', fontSize: 12 },
  error: { color: 'var(--bad, #b42318)', fontSize: 12.5, marginTop: 10 },
  limits: { margin: '0 0 18px', padding: 12, border: '1px solid #ead9ae', borderRadius: 10, background: '#fffbef' },
  limitItem: { margin: '6px 0', color: '#66552c', fontSize: 12, lineHeight: 1.55 },
  textarea: { width: '100%', boxSizing: 'border-box', resize: 'vertical', margin: '8px 0', border: '1px solid #d6c28e', borderRadius: 7, padding: 8, font: 'inherit' },
};

export function releaseGateItems(release, steps = []) {
  if (Array.isArray(release?.gates) && release.gates.length) return release.gates;
  const byId = new Map((steps ?? []).map((step) => [step.id, step]));
  return FALLBACK_GATES.map(([id, stepId, label]) => {
    const passed = byId.get(stepId)?.status === 'completed';
    return { id, label, passed, detail: passed ? '已完成' : '未完成' };
  });
}

export function releaseIsReady(release, steps = []) {
  if (release?.released) return false;
  if (release?.available === false) return false;
  if (typeof release?.ready === 'boolean') return release.ready;
  const gates = releaseGateItems(release, steps);
  return gates.length > 0 && gates.every((gate) => gate.passed);
}

export default function ReleasePanel({ projectId, release, steps = [], onReleased }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [serverGates, setServerGates] = useState(null);
  const [warningReview, setWarningReview] = useState(release?.warningReview ?? null);
  const [warningNote, setWarningNote] = useState('');
  const [warningBusy, setWarningBusy] = useState(false);
  const [auditBusy, setAuditBusy] = useState(false);
  useEffect(() => { setWarningReview(release?.warningReview ?? null); }, [release?.revision, release?.warningReview]);
  const warningGatePassed = warningReview?.readyForRelease === true;
  const sourceGates = serverGates ?? release?.gates;
  const localGates = Array.isArray(sourceGates) ? sourceGates.map((gate) => gate.id === 'known_limitations'
    ? { ...gate, passed: warningGatePassed, detail: warningGatePassed
      ? `${warningReview.warningTotal} 条非阻断警告已复核`
      : gate.detail }
    : gate) : sourceGates;
  const view = { ...release, warningReview, ...(localGates ? { gates: localGates } : {}), ready: undefined };
  const gates = useMemo(() => releaseGateItems(view, steps), [view, steps]);
  const released = Boolean(result || release?.released);
  const ready = !released && releaseIsReady(view, steps);
  const version = result?.version || release?.version;
  const releasableWarnings = (warningReview?.items ?? []).filter((item) => (
    item.accepted && !item.releaseConfirmed && item.finding?.releaseBlocking !== true
  ));

  const confirmWarnings = async () => {
    if (!release?.revision || !releasableWarnings.length || warningBusy) return;
    setWarningBusy(true); setError('');
    try {
      const response = await acceptPackageWarnings(
        projectId, release.revision, releasableWarnings.map((item) => item.fingerprint), warningNote, 'release',
      );
      setWarningReview(response.warningReview);
      setServerGates((current) => (current ?? release?.gates ?? []).map((gate) => gate.id === 'known_limitations'
        ? { ...gate, passed: true, detail: `${response.warningReview.warningTotal} 条非阻断警告已复核` }
        : gate));
      setWarningNote('');
    } catch (err) {
      setError(err instanceof Error ? err.message : '已知限制确认失败');
    } finally { setWarningBusy(false); }
  };

  const publish = async () => {
    if (busy) return;
    if (!window.confirm('确认发布正式版本吗？发布后将生成不可修改的交付版本。')) return;
    setBusy(true);
    setError('');
    try {
      const next = await releaseProject(projectId);
      setResult(next);
      await onReleased?.(next);
    } catch (err) {
      if (Array.isArray(err?.data?.gates)) setServerGates(err.data.gates);
      setError(err instanceof Error ? err.message : '发布失败');
    } finally {
      setBusy(false);
    }
  };

  const downloadAudit = async () => {
    if (auditBusy) return;
    setAuditBusy(true); setError('');
    try {
      const response = await authFetch(exportModelingAuditUrl(projectId));
      if (!response.ok) throw new Error(`审计包导出失败（${response.status}）`);
      const blob = await response.blob();
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = response.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1]
        ?? `${projectId}-modeling-audit.zip`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (err) {
      setError(err instanceof Error ? err.message : '审计包导出失败');
    } finally { setAuditBusy(false); }
  };

  return (
    <section style={styles.panel} aria-label="发布交付">
      <div style={styles.head}>
        <h3 style={styles.title}>发布检查</h3>
        {version && <span style={styles.version}>{released ? '版本' : '当前'} {version}</span>}
      </div>
      <ul style={styles.list}>
        {gates.map((gate) => (
          <li key={gate.id} style={styles.row}>
            <span style={{ ...styles.mark, color: gate.passed ? 'var(--good, #0e6b4a)' : 'var(--muted)' }}>
              {gate.passed ? '✓' : '·'}
            </span>
            <span style={styles.label}>{gate.label}</span>
            <span style={styles.detail}>{gate.detail}</span>
          </li>
        ))}
      </ul>
      {warningReview?.warningTotal > 0 && (
        <div style={styles.limits}>
          <strong>发布已知限制</strong>
          {(warningReview.items ?? []).map((item) => (
            <div key={item.fingerprint} style={styles.limitItem}>
              {item.finding.releaseBlocking ? '必须修复' : item.releaseConfirmed ? '已确认' : '待确认'} · {item.finding.category} · {item.finding.target}<br />
              {item.finding.message}
            </div>
          ))}
          {releasableWarnings.length > 0 && <>
            <textarea style={styles.textarea} rows={2} value={warningNote}
              placeholder="说明为什么可以作为本次发布的已知限制（必填）"
              onChange={(event) => setWarningNote(event.target.value)} />
            <button type="button" className="btn" disabled={warningBusy || warningNote.trim().length < 4}
              onClick={confirmWarnings}>{warningBusy ? '确认中…' : '确认已知限制'}</button>
          </>}
        </div>
      )}
      <div style={styles.action}>
        <button type="button" className="btn btn-p" disabled={!ready || busy} onClick={publish}>
          {busy ? '发布中…' : released ? '已发布' : ready ? '发布' : '暂不可发布'}
        </button>
        <button type="button" className="btn" disabled={auditBusy} onClick={downloadAudit}>
          {auditBusy ? '导出中…' : '导出建模审计包'}
        </button>
        {released && version && <span style={styles.message}>已生成 {version}</span>}
      </div>
      {error && <div style={styles.error}>{error}</div>}
    </section>
  );
}
