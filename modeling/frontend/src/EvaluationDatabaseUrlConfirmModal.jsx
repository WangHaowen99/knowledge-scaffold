import React, { useEffect, useRef, useState } from 'react';
import { evaluationDatabaseUrlSelectionError } from './data-source-form.js';

export default function EvaluationDatabaseUrlConfirmModal({
  title,
  message,
  databaseName,
  configured,
  databaseUrlPreview,
  confirmLabel,
  selectionLocked = false,
  lockedSelection = null,
  busy,
  error,
  onCancel,
  onConfirm,
}) {
  const dialogRef = useRef(null);
  const [mode, setMode] = useState(configured ? 'default' : 'custom');
  const [customUrl, setCustomUrl] = useState('');
  const [localError, setLocalError] = useState('');
  const lockedChoice = selectionLocked
    ? (lockedSelection || (configured ? { mode: 'default', databaseUrl: '' } : null))
    : null;
  const lockedLabel = lockedChoice?.mode === 'custom'
    ? '使用已确认的本次自定义 URL'
    : '使用已确认的服务端 EVAL_DB_URL';

  useEffect(() => {
    setMode(configured ? 'default' : 'custom');
    setCustomUrl('');
    setLocalError('');
    dialogRef.current?.focus();
  }, [configured, databaseName, databaseUrlPreview]);

  const cancel = () => { if (!busy) onCancel(); };
  const confirm = () => {
    if (lockedChoice) {
      setLocalError('');
      onConfirm({
        mode: lockedChoice.mode,
        databaseUrl: lockedChoice.mode === 'custom' ? lockedChoice.databaseUrl : '',
      });
      return;
    }
    const validationError = evaluationDatabaseUrlSelectionError(mode, customUrl, configured);
    if (validationError) { setLocalError(validationError); return; }
    setLocalError('');
    onConfirm({
      mode,
      databaseUrl: mode === 'custom' ? customUrl.trim() : '',
    });
  };
  const handleKeyDown = (event) => {
    if (event.key === 'Escape') {
      if (!busy) onCancel();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(event.currentTarget.querySelectorAll(
      'button:not([disabled]), input:not([disabled])',
    ));
    if (!focusable.length) { event.preventDefault(); return; }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = event.currentTarget.ownerDocument.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault(); first.focus();
    }
  };

  return (
    <div className="modal-ov"
      onClick={(event) => { if (event.target === event.currentTarget) cancel(); }}
      onKeyDown={handleKeyDown}>
      <div ref={dialogRef} className="modal evaluation-db-url-modal" role="dialog" aria-modal="true"
        aria-labelledby="evaluation-db-url-title" aria-describedby="evaluation-db-url-message"
        aria-busy={busy} tabIndex={-1}>
        <h3 id="evaluation-db-url-title">{title}</h3>
        <p id="evaluation-db-url-message" className="gi-conflict-message">{message}</p>

        <div className="project-db-fixed-name">
          <span>目标 Scene 数据库</span>
          <code>{databaseName}</code>
        </div>

        {lockedChoice ? (
          <div className="evaluation-db-url-options">
            <div className="evaluation-db-url-option selected locked">
              <span>
                <b>{lockedLabel}</b>
                <code>{databaseUrlPreview}</code>
              </span>
            </div>
          </div>
        ) : (
          <div className="evaluation-db-url-options">
            <label className={`evaluation-db-url-option ${mode === 'default' ? 'selected' : ''}`}>
              <input type="radio" name="evaluation-database-url" value="default"
                checked={mode === 'default'} disabled={busy || !configured}
                onChange={() => { setMode('default'); setLocalError(''); }} />
              <span>
                <b>使用服务端 EVAL_DB_URL</b>
                <code>{configured ? databaseUrlPreview : '服务端未配置'}</code>
              </span>
            </label>
            <label className={`evaluation-db-url-option ${mode === 'custom' ? 'selected' : ''}`}>
              <input type="radio" name="evaluation-database-url" value="custom"
                checked={mode === 'custom'} disabled={busy}
                onChange={() => { setMode('custom'); setLocalError(''); }} />
              <span><b>本次使用自定义 URL</b></span>
            </label>
            {mode === 'custom' && (
              <div className="field evaluation-db-custom-url">
                <label>MySQL URL</label>
                <input value={customUrl} disabled={busy} autoComplete="off" spellCheck="false"
                  placeholder="mysql://user:password@host:3306"
                  onChange={(event) => { setCustomUrl(event.target.value); setLocalError(''); }} />
              </div>
            )}
          </div>
        )}

        <p className="project-db-create-hint">
          {lockedChoice
            ? '导入将直接使用上方已确认连接；连接预览已隐藏密码。'
            : '请确认所选连接与当前 NL2SQL 服务指向同一 MySQL 服务器。服务端预览已隐藏密码；自定义 URL 只用于当前项目的本次操作，不会保存，也不会修改 EVAL_DB_URL。'}
          目标数据库始终使用上方 Scene，URL 中已有的数据库路径不会覆盖它。
        </p>
        {(localError || error) && <div className="lerr">{localError || error}</div>}
        <div className="foot">
          <button className="btn" disabled={busy} onClick={cancel}>取消</button>
          <button className="btn btn-p" disabled={busy} autoFocus onClick={confirm}>
            {busy ? '处理中…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
