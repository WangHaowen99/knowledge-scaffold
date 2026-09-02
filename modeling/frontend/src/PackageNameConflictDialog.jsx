import { useEffect, useRef, useState } from 'react';

function normalizedName(value) {
  return String(value || '').trim().normalize('NFKC').toLowerCase();
}

export function packageNameSelectionError(name, originalName) {
  const selected = String(name || '').trim();
  if (!selected) return '请输入新的知识包名称';
  if ([...selected].length > 200) return '知识包名称不能超过 200 个字符';
  if (normalizedName(selected) === normalizedName(originalName)) return '新名称不能与原名称相同';
  return '';
}

export default function PackageNameConflictDialog({ conflict, busy, error, onCancel, onConfirm }) {
  const dialogRef = useRef(null);
  const candidates = Array.isArray(conflict?.candidates) ? conflict.candidates : [];
  const [mode, setMode] = useState(candidates.length ? 'candidate' : 'custom');
  const [candidate, setCandidate] = useState(candidates[0] || '');
  const [customName, setCustomName] = useState('');
  const [localError, setLocalError] = useState('');

  useEffect(() => {
    setMode(candidates.length ? 'candidate' : 'custom');
    setCandidate(candidates[0] || '');
    setCustomName('');
    setLocalError('');
    dialogRef.current?.focus();
  }, [conflict]);

  const selectedName = mode === 'candidate' ? candidate : customName;
  const cancel = () => { if (!busy) onCancel(); };
  const submit = (event) => {
    event.preventDefault();
    if (busy) return;
    const validationError = packageNameSelectionError(selectedName, conflict?.originalName);
    if (validationError) { setLocalError(validationError); return; }
    setLocalError('');
    onConfirm(String(selectedName).trim());
  };

  return (
    <div className="modal-ov" onMouseDown={(event) => {
      if (event.target === event.currentTarget) cancel();
    }}>
      <form ref={dialogRef} className="modal package-name-conflict-modal" role="dialog"
        aria-modal="true" aria-labelledby="package-name-conflict-title" tabIndex={-1}
        onSubmit={submit} onKeyDown={(event) => { if (event.key === 'Escape') cancel(); }}>
        <h3 id="package-name-conflict-title">知识包名称已存在</h3>
        <p className="package-name-conflict-summary">
          当前工作空间已有“{conflict?.attemptedName || conflict?.originalName}”。
        </p>

        <div className="package-name-options">
          {candidates.map((name) => (
            <label key={name} className={mode === 'candidate' && candidate === name ? 'selected' : ''}>
              <input type="radio" name="package-name-mode" checked={mode === 'candidate' && candidate === name}
                disabled={busy} onChange={() => { setMode('candidate'); setCandidate(name); setLocalError(''); }} />
              <span>{name}</span>
            </label>
          ))}
          <label className={mode === 'custom' ? 'selected' : ''}>
            <input type="radio" name="package-name-mode" checked={mode === 'custom'} disabled={busy}
              onChange={() => { setMode('custom'); setLocalError(''); }} />
            <span>自定义名称</span>
          </label>
          {mode === 'custom' && <input className="package-name-custom-input" value={customName}
            maxLength={200} disabled={busy} autoFocus placeholder="输入新的知识包名称"
            onChange={(event) => { setCustomName(event.target.value); setLocalError(''); }} />}
        </div>

        {(localError || error) && <div className="lerr" role="alert">{localError || error}</div>}
        <div className="foot">
          <button type="button" className="btn" disabled={busy} onClick={cancel}>取消</button>
          <button type="submit" className="btn btn-p" disabled={busy}>
            {busy ? '导入中…' : '改名并导入'}
          </button>
        </div>
      </form>
    </div>
  );
}
