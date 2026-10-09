import { useEffect, useRef, useState } from 'react';

export const MAX_NAME_LENGTH = 200;

function normalizedName(value) {
  return String(value || '').trim().normalize('NFKC').toLowerCase();
}

export function nameSelectionError(name, originalName, subject = '知识包') {
  const selected = String(name || '').trim();
  if (!selected) return `请输入新的${subject}名称`;
  if ([...selected].length > MAX_NAME_LENGTH) return `${subject}名称不能超过 ${MAX_NAME_LENGTH} 个字符`;
  if (normalizedName(selected) === normalizedName(originalName)) return '新名称不能与原名称相同';
  return '';
}

export default function NameConflictDialog({
  conflict, busy, error, subject = '知识包', onCancel, onConfirm,
}) {
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
    const validationError = nameSelectionError(selectedName, conflict?.originalName, subject);
    if (validationError) { setLocalError(validationError); return; }
    setLocalError('');
    onConfirm(String(selectedName).trim());
  };

  return (
    <div className="modal-ov" onMouseDown={(event) => {
      if (event.target === event.currentTarget) cancel();
    }}>
      <form ref={dialogRef} className="modal name-conflict-modal" role="dialog"
        aria-modal="true" aria-labelledby="name-conflict-title" tabIndex={-1}
        onSubmit={submit} onKeyDown={(event) => { if (event.key === 'Escape') cancel(); }}>
        <h3 id="name-conflict-title">{subject}名称已存在</h3>
        <p className="name-conflict-summary">
          当前工作空间已有“{conflict?.attemptedName || conflict?.originalName}”。
        </p>

        <div className="name-conflict-options">
          {candidates.map((name) => (
            <label key={name} className={mode === 'candidate' && candidate === name ? 'selected' : ''}>
              <input type="radio" name="name-conflict-mode" checked={mode === 'candidate' && candidate === name}
                disabled={busy} onChange={() => { setMode('candidate'); setCandidate(name); setLocalError(''); }} />
              <span>{name}</span>
            </label>
          ))}
          <label className={mode === 'custom' ? 'selected' : ''}>
            <input type="radio" name="name-conflict-mode" checked={mode === 'custom'} disabled={busy}
              onChange={() => { setMode('custom'); setLocalError(''); }} />
            <span>自定义名称</span>
          </label>
          {mode === 'custom' && <input className="name-conflict-custom-input" value={customName}
            maxLength={MAX_NAME_LENGTH} disabled={busy} autoFocus placeholder={`输入新的${subject}名称`}
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
