import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  RefreshCw,
  ThumbsUp,
  Ban,
  Edit3,
  Check,
  X,
  Inbox,
  AlertCircle,
  Loader2,
  ArrowLeft,
  Download,
  PlayCircle,
  Layers,
  FileText,
} from 'lucide-react';
import { authFetch } from '../shared/api/auth';
import OntologySelector from './OntologySelector';

function safeFileSegment(value) {
  return String(value || 'export')
    .trim()
    .replace(/[\\/:*?"<>|\s]+/g, '-')
    .replace(/^-+|-+$/g, '')
    || 'export';
}

function downloadBlob(blob, filename) {
  const objectUrl = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(objectUrl);
}

function downloadZip(files, filename) {
  const blob = new Blob(
    [buildZip(files)],
    { type: 'application/zip' },
  );
  downloadBlob(blob, filename);
}

const textEncoder = new TextEncoder();
let crcTable = null;

function getCrcTable() {
  if (crcTable) return crcTable;
  crcTable = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) {
      c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    }
    crcTable[n] = c >>> 0;
  }
  return crcTable;
}

function crc32(bytes) {
  const table = getCrcTable();
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) {
    crc = table[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function writeUint16(bytes, offset, value) {
  bytes[offset] = value & 0xff;
  bytes[offset + 1] = (value >>> 8) & 0xff;
}

function writeUint32(bytes, offset, value) {
  bytes[offset] = value & 0xff;
  bytes[offset + 1] = (value >>> 8) & 0xff;
  bytes[offset + 2] = (value >>> 16) & 0xff;
  bytes[offset + 3] = (value >>> 24) & 0xff;
}

function concatBytes(chunks) {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

function buildZip(files) {
  const localChunks = [];
  const centralChunks = [];
  let offset = 0;

  files.forEach((file) => {
    const nameBytes = textEncoder.encode(file.name);
    const dataBytes = textEncoder.encode(file.content);
    const crc = crc32(dataBytes);

    const localHeader = new Uint8Array(30);
    writeUint32(localHeader, 0, 0x04034b50);
    writeUint16(localHeader, 4, 20);
    writeUint16(localHeader, 6, 0x0800);
    writeUint16(localHeader, 8, 0);
    writeUint16(localHeader, 10, 0);
    writeUint16(localHeader, 12, 0);
    writeUint32(localHeader, 14, crc);
    writeUint32(localHeader, 18, dataBytes.length);
    writeUint32(localHeader, 22, dataBytes.length);
    writeUint16(localHeader, 26, nameBytes.length);
    writeUint16(localHeader, 28, 0);
    localChunks.push(localHeader, nameBytes, dataBytes);

    const centralHeader = new Uint8Array(46);
    writeUint32(centralHeader, 0, 0x02014b50);
    writeUint16(centralHeader, 4, 20);
    writeUint16(centralHeader, 6, 20);
    writeUint16(centralHeader, 8, 0x0800);
    writeUint16(centralHeader, 10, 0);
    writeUint16(centralHeader, 12, 0);
    writeUint16(centralHeader, 14, 0);
    writeUint32(centralHeader, 16, crc);
    writeUint32(centralHeader, 20, dataBytes.length);
    writeUint32(centralHeader, 24, dataBytes.length);
    writeUint16(centralHeader, 28, nameBytes.length);
    writeUint16(centralHeader, 30, 0);
    writeUint16(centralHeader, 32, 0);
    writeUint16(centralHeader, 34, 0);
    writeUint16(centralHeader, 36, 0);
    writeUint32(centralHeader, 38, 0);
    writeUint32(centralHeader, 42, offset);
    centralChunks.push(centralHeader, nameBytes);

    offset += localHeader.length + nameBytes.length + dataBytes.length;
  });

  const centralStart = offset;
  const centralDirectory = concatBytes(centralChunks);
  const endRecord = new Uint8Array(22);
  writeUint32(endRecord, 0, 0x06054b50);
  writeUint16(endRecord, 4, 0);
  writeUint16(endRecord, 6, 0);
  writeUint16(endRecord, 8, files.length);
  writeUint16(endRecord, 10, files.length);
  writeUint32(endRecord, 12, centralDirectory.length);
  writeUint32(endRecord, 16, centralStart);
  writeUint16(endRecord, 20, 0);

  return concatBytes([...localChunks, centralDirectory, endRecord]);
}

function yamlLine(key, value) {
  if (value === undefined || value === null || value === '') return null;
  return `${key}: ${JSON.stringify(value)}`;
}

function proposalTimestamp(value) {
  if (!value) return null;
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return null;
  return new Date(seconds * 1000).toISOString();
}

function buildProposalMarkdown({ operation, aggregate, record, exportedAt }) {
  const header = [
    '---',
    yamlLine('proposal_type', 'operation_evolution'),
    yamlLine('schema', 'operation-evolution-raw-proposal.v1'),
    yamlLine('ontology_id', operation.summary?.ontology_id),
    yamlLine('operation_key', operation.summary?.operation_key),
    yamlLine('fingerprint', record.fingerprint),
    yamlLine('proposal_id', record.id),
    yamlLine('source', record.source),
    yamlLine('user_id', record.user_id),
    yamlLine('run_id', record.run_id),
    yamlLine('created_at', proposalTimestamp(record.ts)),
    yamlLine('exported_at', exportedAt),
    yamlLine('kind', aggregate?.kind),
    yamlLine('status', aggregate?.status),
    yamlLine('entity_key', aggregate?.entity_key),
    yamlLine('aggregate_occurrences', aggregate?.occurrences),
    yamlLine('aggregate_applied_count', aggregate?.applied_count),
    yamlLine('aggregate_active_endorsements', aggregate?.active_endorsements),
    yamlLine('aggregate_rejection_count', aggregate?.rejection_count),
    yamlLine('rationale', record.rationale),
    '---',
  ].filter((line) => line !== null);

  return `${header.join('\n')}\n\n${String(record.content || '').trim()}\n`;
}

function buildAllRawProposalFiles(exportBody, ontologyId = '') {
  const exportedAt = new Date().toISOString();
  const files = [];
  const usedNames = new Set();

  for (const operation of exportBody.operations || []) {
    const operationOntologyId = operation.summary?.ontology_id || '';
    if (ontologyId && operationOntologyId !== ontologyId) continue;
    const aggregatesByFingerprint = {};
    for (const aggregate of operation.proposals || []) {
      if (aggregate.fingerprint) aggregatesByFingerprint[aggregate.fingerprint] = aggregate;
    }
    (operation.proposal_records || []).forEach((record, index) => {
      const aggregate = aggregatesByFingerprint[record.fingerprint] || null;
      const baseName = [
        operation.summary?.ontology_id,
        operation.summary?.operation_key,
        record.source,
        record.id || record.fingerprint || index + 1,
      ].map(safeFileSegment).join('__');
      let name = `${baseName}.md`;
      let suffix = 2;
      while (usedNames.has(name)) {
        name = `${baseName}-${suffix}.md`;
        suffix += 1;
      }
      usedNames.add(name);
      files.push({
        name,
        content: buildProposalMarkdown({
          operation,
          aggregate,
          record,
          exportedAt,
        }),
      });
    });
  }

  return files;
}

/**
 * Operation Evolution console.
 *
 * Three views, one component (avoids new routes):
 *  - "list"   — staged patches + proposal aggregates across all operations
 *  - "ops"    — proposals for one (ontology, op): endorse / reject / edit / rescan
 *  - "staged" — markdown diff view + admin apply/reject
 */
function OperationEvolution({
  currentUser,
  ontologyId = '',
  ontologyOptions = [],
  onOntologyIdChange,
}) {
  const isAdmin = currentUser?.role === 'admin';

  const [view, setView] = useState('list'); // 'list' | 'ops' | 'staged'
  const [selected, setSelected] = useState(null); // { ontology_id, operation_key }
  const [pending, setPending] = useState({ count: 0, items: [] });
  const [operationOverview, setOperationOverview] = useState({ count: 0, aggregate_count: 0, items: [] });
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState(null);

  const refreshOverview = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [pendingRes, proposalsRes] = await Promise.all([
        authFetch('/agentic/operations/pending'),
        authFetch('/agentic/operations/proposals?status=all'),
      ]);
      if (!pendingRes.ok) throw new Error(`pending HTTP ${pendingRes.status}`);
      if (!proposalsRes.ok) throw new Error(`proposals HTTP ${proposalsRes.status}`);
      const pendingBody = await pendingRes.json();
      const proposalsBody = await proposalsRes.json();
      setPending(pendingBody || { count: 0, items: [] });
      setOperationOverview(proposalsBody || { count: 0, aggregate_count: 0, items: [] });
    } catch (e) {
      setError(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshOverview();
  }, [refreshOverview]);

  useEffect(() => {
    if (view === 'list' || !selected?.ontology_id || !ontologyId) return;
    if (selected.ontology_id !== ontologyId) {
      setSelected(null);
      setView('list');
    }
  }, [ontologyId, selected?.ontology_id, view]);

  const filteredPendingItems = useMemo(
    () => (pending.items || []).filter((item) => ontologyId && item.ontology_id === ontologyId),
    [ontologyId, pending.items],
  );
  const filteredOperationItems = useMemo(
    () => (operationOverview.items || []).filter((item) => ontologyId && item.ontology_id === ontologyId),
    [ontologyId, operationOverview.items],
  );
  const filteredAggregateCount = useMemo(
    () => filteredOperationItems.reduce((sum, item) => sum + Number(item.aggregate_count || 0), 0),
    [filteredOperationItems],
  );
  const currentOntology = useMemo(
    () => ontologyOptions.find((item) => item.id === ontologyId) || null,
    [ontologyId, ontologyOptions],
  );

  const handleOpenOps = useCallback((ontologyId, operationKey) => {
    setSelected({ ontology_id: ontologyId, operation_key: operationKey });
    setView('ops');
  }, []);

  const handleOpenStaged = useCallback((ontologyId, operationKey) => {
    setSelected({ ontology_id: ontologyId, operation_key: operationKey });
    setView('staged');
  }, []);

  const handleBack = useCallback(() => {
    setSelected(null);
    setView('list');
    refreshOverview();
  }, [refreshOverview]);

  const handleExportAll = useCallback(async () => {
    setExporting(true);
    setError(null);
    try {
      const res = await authFetch('/agentic/operations/proposals/export?status=all&records_limit=10000');
      if (!res.ok) throw new Error(`export HTTP ${res.status}`);
      const body = await res.json();
      const files = buildAllRawProposalFiles(body, ontologyId);
      if (!files.length) throw new Error('没有可导出的原始 proposal');
      const stamp = new Date().toISOString().replace(/[:.]/g, '-');
      const scope = safeFileSegment(ontologyId || body?.workspace_id || 'workspace');
      downloadZip(files, `operation-evolution-raw-proposals-${scope}-${stamp}.zip`);
    } catch (e) {
      setError(e.message || '导出失败');
    } finally {
      setExporting(false);
    }
  }, [ontologyId]);

  return (
    <div className="op-evolution">
      <header className="op-evolution-header">
        <div className="op-evolution-title-row">
          {view !== 'list' && (
            <button type="button" className="icon-btn" onClick={handleBack} title="返回">
              <ArrowLeft size={16} />
            </button>
          )}
          <h2>运算进化</h2>
          {view === 'list' && (
            <>
              <OntologySelector
                ontologyId={ontologyId}
                ontologyOptions={ontologyOptions}
                onOntologyIdChange={onOntologyIdChange}
              />
              <button
                type="button"
                className="icon-btn"
                onClick={refreshOverview}
                disabled={loading}
                title="刷新"
              >
                {loading ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
              </button>
              <button
                type="button"
                className="btn-secondary"
                onClick={handleExportAll}
                disabled={exporting || !ontologyId}
                title="导出当前本体全部原始 proposal ZIP"
              >
                {exporting ? <Loader2 size={16} className="spin" /> : <Download size={16} />}
                导出当前本体全部原始 proposal
              </button>
            </>
          )}
        </div>
        {view === 'list' && (
          <p className="op-evolution-subtitle">
            {currentOntology ? `${currentOntology.name || currentOntology.id} / ` : ''}
            staged 待审 {filteredPendingItems.length} 条，proposal operation {filteredOperationItems.length} 个，
            aggregate {filteredAggregateCount} 条。
          </p>
        )}
        {view !== 'list' && selected && (
          <p className="op-evolution-subtitle">
            {selected.ontology_id} / <strong>{selected.operation_key}</strong>
          </p>
        )}
      </header>

      {error && (
        <div className="op-evolution-error">
          <AlertCircle size={14} /> {error}
        </div>
      )}

      {view === 'list' && (
        <OverviewList
          loading={loading}
          pendingItems={filteredPendingItems}
          operationItems={filteredOperationItems}
          ontologyId={ontologyId}
          onReview={handleOpenStaged}
          onProposals={handleOpenOps}
        />
      )}

      {view === 'ops' && selected && (
        <ProposalsPanel
          ontologyId={selected.ontology_id}
          operationKey={selected.operation_key}
          onOpenStaged={() => handleOpenStaged(selected.ontology_id, selected.operation_key)}
        />
      )}

      {view === 'staged' && selected && (
        <StagedPanel
          ontologyId={selected.ontology_id}
          operationKey={selected.operation_key}
          isAdmin={isAdmin}
          onResolved={handleBack}
          onOpenProposals={() => handleOpenOps(selected.ontology_id, selected.operation_key)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function OverviewList({ loading, pendingItems, operationItems, ontologyId, onReview, onProposals }) {
  const hasPending = pendingItems.length > 0;
  const hasOperations = operationItems.length > 0;

  if (loading && !hasPending && !hasOperations) {
    return <div className="op-evolution-loading"><Loader2 className="spin" /> 加载中...</div>;
  }

  if (!hasPending && !hasOperations) {
    return (
      <div className="op-evolution-empty">
        <Inbox size={28} />
        <p>{ontologyId ? '当前本体没有 operation evolution proposal。' : '请先选择本体。'}</p>
        <p className="hint">chat 主动建议或反思采集后，会在这里出现 proposal 与 aggregate 指标。</p>
      </div>
    );
  }

  return (
    <div className="op-evolution-overview">
      {hasPending && (
        <div className="op-evolution-section">
          <div className="op-evolution-panel-title">
            <FileText size={15} />
            <h3>待审批 staged <span className="count">({pendingItems.length})</span></h3>
          </div>
          <PendingList
            items={pendingItems}
            onReview={onReview}
            onProposals={onProposals}
          />
        </div>
      )}

      <div className="op-evolution-section">
        <div className="op-evolution-panel-title">
          <Layers size={15} />
          <h3>Proposal / Aggregation <span className="count">({operationItems.length})</span></h3>
        </div>
        {hasOperations ? (
          <OperationAggregateList
            items={operationItems}
            onReview={onReview}
            onProposals={onProposals}
          />
        ) : (
          <div className="op-evolution-empty compact">
            <p>还没有 aggregate 快照。</p>
          </div>
        )}
      </div>
    </div>
  );
}

function formatCounts(counts = {}) {
  return Object.entries(counts)
    .filter(([, value]) => Number(value) > 0)
    .map(([key, value]) => `${key}:${value}`)
    .join('  ');
}

function formatTime(seconds) {
  if (!seconds) return '';
  return new Date(seconds * 1000).toLocaleString();
}

function OperationAggregateList({ items, onReview, onProposals }) {
  return (
    <ul className="op-evolution-list">
      {items.map((item) => (
        <li key={`${item.ontology_id}/${item.operation_key}`} className="op-evolution-row">
          <div className="op-evolution-row-main">
            <div className="op-evolution-row-title">
              <strong>{item.operation_key}</strong>
              <span className="op-evolution-row-onto">{item.ontology_id}</span>
              {item.has_staged && <span className="op-evolution-status-chip">staged</span>}
            </div>
            {item.top_content && (
              <p className="op-evolution-row-summary">{item.top_content}</p>
            )}
            <div className="op-evolution-row-meta">
              <span>{item.aggregate_count} aggregates</span>
              <span>occ={item.occurrences}</span>
              <span>applied={item.applied_count}</span>
              <span>endorsed={item.active_endorsements}</span>
              {item.rejection_count > 0 && <span>rejected={item.rejection_count}</span>}
              {item.last_seen && <span>updated {formatTime(item.last_seen)}</span>}
            </div>
            <div className="op-evolution-row-meta">
              {formatCounts(item.status_counts) && <span>status: {formatCounts(item.status_counts)}</span>}
              {formatCounts(item.kind_counts) && <span>kind: {formatCounts(item.kind_counts)}</span>}
              {formatCounts(item.source_counts) && <span>source: {formatCounts(item.source_counts)}</span>}
            </div>
          </div>
          <div className="op-evolution-row-actions">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => onProposals(item.ontology_id, item.operation_key)}
            >
              查看 proposal
            </button>
            {item.has_staged && (
              <button
                type="button"
                className="btn-primary"
                onClick={() => onReview(item.ontology_id, item.operation_key)}
              >
                审批 staged
              </button>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------

function PendingList({ items, onReview, onProposals }) {
  if (!items.length) {
    return (
      <div className="op-evolution-empty">
        <Inbox size={28} />
        <p>当前没有待审 staged 补丁。</p>
        <p className="hint">通过 chat 记忆入口沉淀经验，或等待反思采集累积。</p>
      </div>
    );
  }
  return (
    <ul className="op-evolution-list">
      {items.map((item) => (
        <li key={`${item.ontology_id}/${item.operation_key}`} className="op-evolution-row">
          <div className="op-evolution-row-main">
            <div className="op-evolution-row-title">
              <strong>{item.operation_key}</strong>
              <span className="op-evolution-row-onto">{item.ontology_id}</span>
            </div>
            {item.change_summary && (
              <p className="op-evolution-row-summary">{item.change_summary}</p>
            )}
            <div className="op-evolution-row-meta">
              {Array.isArray(item.fingerprints) && (
                <span>{item.fingerprints.length} 条 proposal</span>
              )}
              {item.created_at && (
                <span>· {new Date(item.created_at * 1000).toLocaleString()}</span>
              )}
            </div>
          </div>
          <div className="op-evolution-row-actions">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => onProposals(item.ontology_id, item.operation_key)}
            >
              查看 proposal
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => onReview(item.ontology_id, item.operation_key)}
            >
              审批 staged
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------

function ProposalsPanel({ ontologyId, operationKey, onOpenStaged }) {
  const [aggregates, setAggregates] = useState([]);
  const [recordsByFingerprint, setRecordsByFingerprint] = useState({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null); // fingerprint
  const [draft, setDraft] = useState('');
  const [hasStaged, setHasStaged] = useState(false);
  const [rescanning, setRescanning] = useState(false);

  const apiBase = `/agentic/operations/${encodeURIComponent(ontologyId)}/${encodeURIComponent(operationKey)}`;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await authFetch(`${apiBase}/proposals?status=all&include_records=1`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const proposals = body.proposals || [];
      const records = body.proposal_records || [];
      const groupedRecords = {};
      for (const record of records) {
        const fp = record.fingerprint;
        if (!fp) continue;
        if (!groupedRecords[fp]) groupedRecords[fp] = [];
        groupedRecords[fp].push(record);
      }
      setAggregates(proposals);
      setRecordsByFingerprint(groupedRecords);
    } catch (e) {
      setError(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
    // detect staged
    try {
      const stagedRes = await authFetch(`${apiBase}/staged`);
      setHasStaged(stagedRes.ok);
    } catch {
      setHasStaged(false);
    }
  }, [apiBase]);

  useEffect(() => { load(); }, [load]);

  const handleEndorse = async (fp) => {
    await authFetch(`${apiBase}/proposals/${encodeURIComponent(fp)}/endorse`, { method: 'POST' });
    load();
  };
  const handleReject = async (fp) => {
    await authFetch(`${apiBase}/proposals/${encodeURIComponent(fp)}/reject`, { method: 'POST' });
    load();
  };
  const handleEditSave = async () => {
    if (!editing || draft.trim().length < 4) return;
    const res = await authFetch(`${apiBase}/proposals/${encodeURIComponent(editing)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: draft.trim() }),
    });
    if (res.ok) {
      setEditing(null);
      setDraft('');
      load();
    }
  };
  const handleRescan = async () => {
    setRescanning(true);
    try {
      await authFetch(`${apiBase}/rescan`, { method: 'POST' });
      // backend is async; give it a beat then refresh
      setTimeout(() => { load(); setRescanning(false); }, 1500);
    } catch {
      setRescanning(false);
    }
  };

  const grouped = useMemo(() => {
    const buckets = { active_chat: [], pending: [], promoted: [], applied: [], rejected: [] };
    for (const a of aggregates) {
      const isActive = (a.sources || {}).active_chat > 0;
      const k = a.status === 'rejected' ? 'rejected'
        : a.status === 'applied' ? 'applied'
        : a.status === 'promoted' ? 'promoted'
        : isActive ? 'active_chat'
        : 'pending';
      buckets[k].push(a);
    }
    return buckets;
  }, [aggregates]);

  const summary = useMemo(() => {
    const totals = {
      aggregates: aggregates.length,
      occurrences: 0,
      activeEndorsements: 0,
      appliedCount: 0,
      rejectedCount: 0,
      rawRecords: 0,
    };
    for (const a of aggregates) {
      const occ = Number(a.occurrences || 0);
      totals.occurrences += occ;
      totals.activeEndorsements += Number(a.active_endorsements || 0);
      totals.appliedCount += Number(a.applied_count || 0);
      totals.rejectedCount += Number(a.rejection_count || 0);
      totals.rawRecords += (recordsByFingerprint[a.fingerprint] || []).length;
    }
    return totals;
  }, [aggregates, recordsByFingerprint]);

  return (
    <div className="op-evolution-proposals">
      {hasStaged && (
        <div className="op-evolution-staged-banner">
          ⚠️ 当前 op 有待审 staged 补丁。
          <button type="button" className="btn-link" onClick={onOpenStaged}>查看 diff →</button>
        </div>
      )}

      <div className="op-evolution-toolbar">
        <button type="button" className="btn-secondary" onClick={load} disabled={loading}>
          {loading ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} 刷新
        </button>
        <button type="button" className="btn-secondary" onClick={handleRescan} disabled={rescanning}>
          {rescanning ? <Loader2 size={14} className="spin" /> : <PlayCircle size={14} />} 立刻重新评估
        </button>
      </div>

      {error && <div className="op-evolution-error"><AlertCircle size={14} /> {error}</div>}

      <div className="op-evolution-summary-strip">
        <span><strong>{summary.aggregates}</strong> aggregates</span>
        <span><strong>{summary.rawRecords}</strong> raw proposals</span>
        <span>occ={summary.occurrences}</span>
        <span>applied={summary.appliedCount}</span>
        <span>endorsed={summary.activeEndorsements}</span>
        {summary.rejectedCount > 0 && <span>rejected={summary.rejectedCount}</span>}
      </div>

      {!loading && aggregates.length === 0 && (
        <div className="op-evolution-empty compact">
          <p>这个 operation 还没有 proposal aggregate。</p>
        </div>
      )}

      <Section title="主动建议（active_chat 直通）" items={grouped.active_chat}
        editing={editing} draft={draft} setEditing={setEditing} setDraft={setDraft}
        onEndorse={handleEndorse} onReject={handleReject} onSave={handleEditSave}
        recordsByFingerprint={recordsByFingerprint}
        emphasizeUnapplied />
      <Section title="待累计（passive 反思）" items={grouped.pending}
        editing={editing} draft={draft} setEditing={setEditing} setDraft={setDraft}
        onEndorse={handleEndorse} onReject={handleReject} onSave={handleEditSave}
        recordsByFingerprint={recordsByFingerprint} />
      <Section title="已纳入 staged（待审批）" items={grouped.promoted}
        editing={editing} draft={draft} setEditing={setEditing} setDraft={setDraft}
        onEndorse={handleEndorse} onReject={handleReject} onSave={handleEditSave}
        recordsByFingerprint={recordsByFingerprint} readonly />
      <Section title="已应用" items={grouped.applied}
        editing={editing} draft={draft} setEditing={setEditing} setDraft={setDraft}
        onEndorse={handleEndorse} onReject={handleReject} onSave={handleEditSave}
        recordsByFingerprint={recordsByFingerprint} readonly />
      <Section title="已拒绝" items={grouped.rejected}
        editing={editing} draft={draft} setEditing={setEditing} setDraft={setDraft}
        onEndorse={handleEndorse} onReject={handleReject} onSave={handleEditSave}
        recordsByFingerprint={recordsByFingerprint} readonly />
    </div>
  );
}

function Section({ title, items, editing, draft, setEditing, setDraft, onEndorse, onReject, onSave, recordsByFingerprint, readonly, emphasizeUnapplied }) {
  if (!items.length) return null;
  return (
    <div className="op-evolution-section">
      <h3>{title} <span className="count">({items.length})</span></h3>
      <ul>
        {items.map((a) => {
          const isActive = (a.sources || {}).active_chat > 0;
          const flagUnproven = emphasizeUnapplied && isActive && a.applied_count === 0;
          const isEditing = editing === a.fingerprint;
          const records = recordsByFingerprint?.[a.fingerprint] || [];
          return (
            <li key={a.fingerprint} className={`op-evolution-proposal ${flagUnproven ? 'unproven' : ''}`}>
              <div className="op-evolution-proposal-meta">
                <span className={`badge badge-${a.kind}`}>{a.kind}</span>
                <span className="op-evolution-fingerprint">{a.fingerprint.slice(0, 10)}</span>
                {a.entity_key && <span className="entity">实体: {a.entity_key}</span>}
                <span>occ={a.occurrences}</span>
                <span>applied={a.applied_count}</span>
                <span>endorsed={a.active_endorsements}</span>
                <span>raw={records.length || (a.proposal_ids || []).length}</span>
                {a.rejection_count > 0 && <span>rejected={a.rejection_count}</span>}
                {flagUnproven && <span className="warn">未经验证</span>}
              </div>
              {isEditing ? (
                <div className="op-evolution-edit">
                  <textarea
                    rows={4}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    autoFocus
                  />
                  <div className="op-evolution-edit-actions">
                    <button type="button" className="btn-primary" onClick={onSave}>
                      <Check size={14} /> 保存
                    </button>
                    <button type="button" className="btn-secondary" onClick={() => { setEditing(null); setDraft(''); }}>
                      <X size={14} /> 取消
                    </button>
                  </div>
                </div>
              ) : (
                <p className="op-evolution-proposal-content">{a.canonical_content}</p>
              )}
              {!readonly && !isEditing && (
                <div className="op-evolution-proposal-actions">
                  <button type="button" className="icon-btn" onClick={() => onEndorse(a.fingerprint)} title="👍">
                    <ThumbsUp size={14} />
                  </button>
                  <button type="button" className="icon-btn" onClick={() => onReject(a.fingerprint)} title="拒绝">
                    <Ban size={14} />
                  </button>
                  <button type="button" className="icon-btn" onClick={() => { setEditing(a.fingerprint); setDraft(a.canonical_content || ''); }} title="编辑">
                    <Edit3 size={14} />
                  </button>
                </div>
              )}
              {records.length > 0 && (
                <details className="op-evolution-records">
                  <summary>原始 proposal 记录 ({records.length})</summary>
                  {records.map((record) => (
                    <div key={record.id} className="op-evolution-record">
                      <div className="op-evolution-record-meta">
                        <span>{record.source}</span>
                        {record.user_id && <span>user={record.user_id}</span>}
                        {record.run_id && <span>run={record.run_id}</span>}
                        {record.ts && <span>{formatTime(record.ts)}</span>}
                      </div>
                      <p>{record.content}</p>
                      {record.rationale && <p className="op-evolution-record-rationale">{record.rationale}</p>}
                    </div>
                  ))}
                </details>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------

function StagedPanel({ ontologyId, operationKey, isAdmin, onResolved, onOpenProposals }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const apiBase = `/agentic/operations/${encodeURIComponent(ontologyId)}/${encodeURIComponent(operationKey)}`;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    authFetch(`${apiBase}/staged`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        if (!cancelled) setData(body);
      })
      .catch((e) => { if (!cancelled) setError(e.message || '加载失败'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [apiBase]);

  const handleApply = async () => {
    if (!isAdmin) return;
    setBusy(true);
    try {
      const res = await authFetch(`${apiBase}/staged/apply`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onResolved();
    } catch (e) {
      setError(e.message || '应用失败');
      setBusy(false);
    }
  };
  const handleReject = async (rejectProposals) => {
    if (!isAdmin) return;
    setBusy(true);
    try {
      const res = await authFetch(`${apiBase}/staged/reject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reject_proposals: rejectProposals }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onResolved();
    } catch (e) {
      setError(e.message || '拒绝失败');
      setBusy(false);
    }
  };

  if (loading) {
    return <div className="op-evolution-loading"><Loader2 className="spin" /> 加载中...</div>;
  }
  if (error) {
    return <div className="op-evolution-error"><AlertCircle size={14} /> {error}</div>;
  }
  if (!data) return null;

  return (
    <div className="op-evolution-staged">
      {data.meta?.change_summary && (
        <div className="op-evolution-summary-card">
          <strong>变更摘要：</strong> {data.meta.change_summary}
        </div>
      )}
      <div className="op-evolution-staged-actions">
        {isAdmin ? (
          <>
            <button type="button" className="btn-primary" onClick={handleApply} disabled={busy}>
              {busy ? <Loader2 size={14} className="spin" /> : <Check size={14} />} 应用到 .md
            </button>
            <button type="button" className="btn-secondary" onClick={() => handleReject(true)} disabled={busy}>
              <Ban size={14} /> 拒绝并标记为 rejected
            </button>
            <button type="button" className="btn-secondary" onClick={() => handleReject(false)} disabled={busy}>
              <X size={14} /> 仅丢弃 staged（保留 pending）
            </button>
          </>
        ) : (
          <span className="hint">仅管理员可应用或拒绝。</span>
        )}
        <button type="button" className="btn-link" onClick={onOpenProposals}>
          查看源 proposal
        </button>
      </div>
      <div className="op-evolution-diff">
        <div className="op-evolution-diff-col">
          <h4>原版本 (.md)</h4>
          <pre>{data.original}</pre>
        </div>
        <div className="op-evolution-diff-col">
          <h4>staged 提案</h4>
          <pre>{data.staged}</pre>
        </div>
      </div>
    </div>
  );
}

export default OperationEvolution;
