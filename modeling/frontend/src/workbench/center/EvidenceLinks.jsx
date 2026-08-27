import { useState } from 'react';
import { BookOpen, LoaderCircle } from 'lucide-react';

import { actions, store } from '../lib/appStore.js';
import { evidenceLabel, materialEvidence } from '../lib/evidence.js';
import { useStore } from '../lib/store.js';

const selectMaterials = (state) => state.materials;

export default function EvidenceLinks({ references, compact = false }) {
  const materials = useStore(store, selectMaterials);
  const values = (Array.isArray(references) ? references : [references]).filter(Boolean);
  if (!values.length) return <span className="faint">未声明</span>;
  return (
    <div className={`wb-evidence-links${compact ? ' compact' : ''}`} aria-label="业务依据">
      {values.map((reference, index) => (
        <EvidenceLink key={`${index}-${reference}`} reference={reference} materials={materials} />
      ))}
    </div>
  );
}

export function EvidenceLink({ reference, materials, children }) {
  const [status, setStatus] = useState('idle');
  const evidence = materialEvidence(reference, materials);
  const label = children || evidenceLabel(reference, materials);
  if (!evidence?.material) return <span className="wb-evidence-note">{label}</span>;
  return (
    <button type="button" className="wb-evidence-link" disabled={status === 'loading'}
      aria-label={`查看${label}中的原文`} onClick={async () => {
        setStatus('loading');
        const located = await actions.locateEvidence(reference);
        setStatus(located ? 'done' : 'error');
      }}>
      {status === 'loading' ? <LoaderCircle className="spin" size={13} /> : <BookOpen size={13} />}
      <span>{status === 'loading' ? '正在打开原文' : label}</span>
      {status === 'error' && <small>暂时无法定位，请稍后重试</small>}
    </button>
  );
}
