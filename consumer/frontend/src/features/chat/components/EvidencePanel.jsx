import { useEffect, useState } from 'react';
import {
  Copy,
  Database,
  ExternalLink,
  FileText,
  Loader2,
  ShieldCheck,
  TriangleAlert,
  X,
} from 'lucide-react';

import { authFetch } from '../../../shared/api/auth';
import Markdown from '../../../shared/ui/Markdown';

const RESOURCE_LABELS = {
  root: '知识包入口',
  object: '业务对象',
  operation: '业务操作',
  metric: '业务指标',
  rule: '业务规则',
  script: '业务脚本',
  asset: '受控资产',
};

const compactId = (value) => {
  const normalized = String(value || '');
  return normalized.length > 30 ?
    `${normalized.slice(0, 14)}…${normalized.slice(-10)}` :
    normalized;
};

const shortDigest = (value, size = 14) => {
  const normalized = String(value || '').replace(/^sha256:/, '');
  if (!normalized) {
    return '';
  }
  return `${normalized.slice(0, size)}${normalized.length > size ? '…' : ''}`;
};

const evidenceState = (hit, runtime = false) => {
  if (runtime) {
    return hit.grounding_status === 'grounded' ? 'Grounded' : '待核验';
  }
  return hit.loaded ? '已查回' : '已命中';
};

export function TurnEvidencePanel({ evidence, onSelect }) {
  if (!evidence) {
    return null;
  }
  const grounded = evidence.runtime.filter(
    (hit) => hit.grounding_status === 'grounded',
  ).length;
  const status =
    evidence.ontology.length && evidence.runtime.length ?
      '本体依据与运行证据已分层' :
      evidence.ontology.length ?
        '本体依据已查回' :
        '运行证据已固化';

  return (
    <section className="turn-evidence" aria-label="回答证据">
      <div className="turn-evidence-head">
        <span className="turn-evidence-icon">
          <ShieldCheck size={16} />
        </span>
        <div className="turn-evidence-title">
          <strong>可追溯依据</strong>
          <span>{status}</span>
        </div>
        <div className="turn-evidence-counts">
          <span>
            <b>{evidence.ontology.length}</b> ONT
          </span>
          <span>
            <b>{evidence.runtime.length}</b> RUN
          </span>
        </div>
      </div>
      <div className="turn-evidence-groups">
        <InlineEvidenceGroup
          title="本体依据"
          hits={evidence.ontology}
          onSelect={onSelect}
        />
        <InlineEvidenceGroup
          title="运行证据"
          hits={evidence.runtime}
          onSelect={onSelect}
          runtime
        />
      </div>
      <div className="turn-evidence-foot">
        <span>
          {evidence.ontologyEpoch ?
            `Epoch ${shortDigest(evidence.ontologyEpoch)}` :
            '按当前工作空间隔离'}
        </span>
        {evidence.runtime.length > 0 && (
          <span>
            {grounded}/{evidence.runtime.length} Grounded
          </span>
        )}
        {evidence.previewTruncated && <span>展示前序安全投影</span>}
      </div>
    </section>
  );
}

function InlineEvidenceGroup({ title, hits, onSelect, runtime = false }) {
  const visibleHits = hits.slice(0, 2);
  return (
    <section className="turn-evidence-group">
      <div className="turn-evidence-group-title">
        <span>{title}</span>
        <b>{hits.length}</b>
      </div>
      {visibleHits.length ? (
        visibleHits.map((hit) => (
          <button
            key={hit.evidence_id}
            type="button"
            onClick={() => onSelect?.(hit)}
          >
            <span className={`evidence-kind-mark ${runtime ? 'run' : ''}`}>
              {runtime ? 'RUN' : 'ONT'}
            </span>
            <span className="turn-evidence-copy">
              <strong>{hit.title || hit.key || '未命名证据'}</strong>
              <code>{compactId(hit.evidence_id)}</code>
            </span>
            <span className="turn-evidence-row-state">
              {evidenceState(hit, runtime)}
            </span>
          </button>
        ))
      ) : (
        <div className="turn-evidence-none">本轮未产生{title}</div>
      )}
      {hits.length > 2 && (
        <button
          type="button"
          className="turn-evidence-more"
          onClick={() => onSelect?.(hits[0])}
        >
          查看全部 {hits.length} 条
        </button>
      )}
    </section>
  );
}

export default function EvidenceInspector({
  evidenceTurns = [],
  activeTurn,
  selectedEvidence,
  width,
  onTurnChange,
  onEvidenceSelect,
  onClose,
}) {
  const evidence = activeTurn?.evidence || null;
  return (
    <aside
      className="trace evidence-inspector"
      style={width ? { width } : undefined}
      aria-label="回答证据"
    >
      <div className="tr-head">
        <span className="trace-title">
          <ShieldCheck size={14} />
          回答证据
        </span>
        <span className="trace-caption">{evidence?.all.length || 0} refs</span>
        <button
          className="iconbtn trace-close"
          type="button"
          aria-label="关闭证据面板"
          onClick={onClose}
        >
          <X size={16} />
        </button>
      </div>
      {!evidence ? (
        <div className="evidence-inspector-empty">
          <ShieldCheck size={28} />
          <strong>等待本轮证据</strong>
          <p>涉及本体或业务数据时，静态依据和运行证据会在这里分层展示。</p>
        </div>
      ) : (
        <div className="evidence-inspector-body">
          <div className="evidence-inspector-summary">
            <div>
              <span>当前回答</span>
              <strong>{evidence.all.length} 条可查回证据</strong>
            </div>
            <div>
              <span>
                <b>{evidence.ontology.length}</b> ONT
              </span>
              <span>
                <b>{evidence.runtime.length}</b> RUN
              </span>
            </div>
          </div>
          {evidenceTurns.length > 1 && (
            <label className="evidence-turn-select">
              <span>查看回答轮次</span>
              <select
                value={activeTurn?.key || ''}
                onChange={(event) => onTurnChange?.(event.target.value)}
              >
                {evidenceTurns.map((turn, index) => (
                  <option key={turn.key} value={turn.key}>
                    第 {index + 1} 轮 · {turn.evidence.all.length} 条
                  </option>
                ))}
              </select>
            </label>
          )}
          <InspectorEvidenceGroup
            title="本体依据"
            description={
              evidence.retrievalMode === 'direct_context' ?
                '来自已加载本体详情的不可变来源片段' :
                '来自证据索引的定义、规则和受控资产'
            }
            hits={evidence.ontology}
            selectedId={selectedEvidence?.evidence_id}
            onSelect={onEvidenceSelect}
          />
          <InspectorEvidenceGroup
            title="运行证据"
            description="业务查询结果固化后的 Runtime Fact 回执"
            hits={evidence.runtime}
            selectedId={selectedEvidence?.evidence_id}
            onSelect={onEvidenceSelect}
            runtime
          />
          {selectedEvidence && (
            <EvidenceRecordDetail
              key={selectedEvidence.evidence_id}
              hit={selectedEvidence}
              fallbackEpoch={evidence.ontologyEpoch}
            />
          )}
        </div>
      )}
    </aside>
  );
}

function InspectorEvidenceGroup({
  title,
  description,
  hits,
  selectedId,
  onSelect,
  runtime = false,
}) {
  const Icon = runtime ? Database : FileText;
  return (
    <section className="inspector-evidence-group">
      <div className="inspector-evidence-group-head">
        <div>
          <Icon size={14} />
          <strong>{title}</strong>
          <span>{hits.length}</span>
        </div>
        <p>{description}</p>
      </div>
      {hits.length ? (
        <div className="inspector-evidence-list">
          {hits.map((hit) => (
            <button
              key={hit.evidence_id}
              type="button"
              className={hit.evidence_id === selectedId ? 'on' : ''}
              onClick={() => onSelect?.(hit)}
            >
              <span className={`evidence-kind-mark ${runtime ? 'run' : ''}`}>
                {runtime ? 'RUN' : 'ONT'}
              </span>
              <span>
                <strong>{hit.title || hit.key || '未命名证据'}</strong>
                <code>{compactId(hit.evidence_id)}</code>
              </span>
              <small>{evidenceState(hit, runtime)}</small>
            </button>
          ))}
        </div>
      ) : (
        <div className="inspector-evidence-empty">本轮没有{title}</div>
      )}
    </section>
  );
}

function EvidenceRecordDetail({ hit, fallbackEpoch }) {
  const [record, setRecord] = useState(null);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    authFetch(`/agentic/evidence/${encodeURIComponent(hit.evidence_id)}`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(
            payload?.error?.message ||
              payload?.detail ||
              payload?.message ||
              '证据查回失败',
          );
        }
        setRecord(payload);
      })
      .catch((lookupError) => {
        if (lookupError.name !== 'AbortError') {
          setError(lookupError.message || '证据查回失败');
        }
      });
    return () => controller.abort();
  }, [hit.evidence_id]);

  const isRuntime = (record?.kind || hit.kind) !== 'ontology_evidence';
  const sourcePath = record?.source?.path || hit.source_path || '';
  const groundingStatus =
    record?.grounding_status || hit.grounding_status || '';
  const validityActive = record?.validity?.active;
  const derivedFrom =
    record?.provenance?.derived_from || hit.derived_from || [];
  const warnings = record?.grounding_warnings || hit.grounding_warnings || [];
  const copyId = async () => {
    await navigator.clipboard?.writeText(hit.evidence_id);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  const openSource = () => {
    if (!sourcePath) {
      return;
    }
    window.dispatchEvent(
      new CustomEvent('mwr-open-kb-source', {
        detail: {
          ontologyId: record?.ontology_id || hit.ontology_id,
          path: sourcePath,
        },
      }),
    );
  };

  return (
    <section className="evidence-record" aria-label="证据详情">
      <div className="evidence-record-title">
        <span className={`evidence-kind-mark ${isRuntime ? 'run' : ''}`}>
          {isRuntime ? 'RUN' : 'ONT'}
        </span>
        <div>
          <span>证据详情</span>
          <strong>
            {record?.title || hit.title || hit.key || '业务运行事实'}
          </strong>
        </div>
      </div>
      <div className="evidence-record-status">
        <span
          className={
            validityActive === false ?
              'inactive' :
              groundingStatus === 'unverified' ?
                'warning' :
                'active'
          }
        >
          {!record ?
            '校验中' :
            isRuntime ?
              groundingStatus === 'grounded' ?
                'Grounded' :
                '待核验' :
              validityActive === false ?
                '已失效' :
                '当前有效'}
        </span>
        <span>
          {isRuntime ?
            'provider_query' :
            RESOURCE_LABELS[record?.resource || hit.resource] ||
              hit.resource ||
              '本体资源'}
        </span>
      </div>
      <dl className="evidence-record-meta">
        <div>
          <dt>证据 ID</dt>
          <dd>
            <code>{hit.evidence_id}</code>
            <button type="button" onClick={copyId}>
              <Copy size={11} />
              {copied ? '已复制' : '复制'}
            </button>
          </dd>
        </div>
        {isRuntime ? (
          <>
            <div>
              <dt>业务对象</dt>
              <dd>{record?.subject || hit.subject || '未声明'}</dd>
            </div>
            <div>
              <dt>Provider</dt>
              <dd>{record?.provenance?.provider || '未声明'}</dd>
            </div>
            <div>
              <dt>读取时间</dt>
              <dd>
                {record?.validity?.read_at || record?.created_at || '未声明'}
              </dd>
            </div>
          </>
        ) : (
          <>
            <div>
              <dt>来源</dt>
              <dd>{sourcePath || '未声明'}</dd>
            </div>
            <div>
              <dt>章节</dt>
              <dd>
                {record?.section ||
                  hit.section ||
                  hit.section_anchor ||
                  '文档正文'}
              </dd>
            </div>
            <div>
              <dt>Epoch</dt>
              <dd title={record?.ontology_epoch || fallbackEpoch}>
                {shortDigest(record?.ontology_epoch || fallbackEpoch, 20) ||
                  '未声明'}
              </dd>
            </div>
          </>
        )}
      </dl>
      {error ? (
        <div className="evidence-record-error">
          <TriangleAlert size={14} />
          {error}
        </div>
      ) : record ? (
        <>
          <div className="evidence-record-content">
            <span>{isRuntime ? '运行事实摘要' : '证据原文'}</span>
            <Markdown>
              {record.content ||
                record.summary ||
                hit.snippet ||
                '暂无可展示内容'}
            </Markdown>
          </div>
          {isRuntime && (derivedFrom.length > 0 || warnings.length > 0) && (
            <div className="evidence-record-relations">
              <strong>有效性与关系</strong>
              {derivedFrom.map((id) => (
                <code key={id}>派生自 {id}</code>
              ))}
              {warnings.map((warning) => (
                <span key={warning}>不确定性：{warning}</span>
              ))}
            </div>
          )}
        </>
      ) : (
        <div className="evidence-record-loading">
          <Loader2 className="spin" size={18} />
          正在查回证据…
        </div>
      )}
      {sourcePath && (
        <button
          type="button"
          className="evidence-open-source"
          onClick={openSource}
        >
          <ExternalLink size={12} />
          在知识包中打开来源
        </button>
      )}
      <p className="evidence-record-footnote">
        Evidence ID 只用于定位，不作为授权凭证；业务原始行不会复制到证据详情。
      </p>
    </section>
  );
}
