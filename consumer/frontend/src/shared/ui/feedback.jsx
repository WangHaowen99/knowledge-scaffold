// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { useEffect, useId, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Info, KeyRound, X } from 'lucide-react';

export function SkeletonState({
  rows = 4,
  compact = false,
  label = '正在加载',
}) {
  return (
    <div
      className={`ui-skeleton ${compact ? 'compact' : ''}`}
      role="status"
      aria-label={label}
    >
      <span className="ui-skeleton-title" />
      {Array.from({ length: rows }, (_, index) => (
        <span
          key={index}
          className="ui-skeleton-row"
          style={{ width: `${92 - (index % 3) * 13}%` }}
        />
      ))}
    </div>
  );
}

export function ToastNotice({
  message,
  tone,
  duration = 3200,
  actionLabel,
  onAction,
  actionHref,
}) {
  const [dismissedMessage, setDismissedMessage] = useState('');

  useEffect(() => {
    if (!message) {
      const resetTimer = setTimeout(() => setDismissedMessage(''), 0);
      return () => clearTimeout(resetTimer);
    }
    if (duration <= 0) {
      return undefined;
    }
    const timer = setTimeout(() => setDismissedMessage(message), duration);
    return () => clearTimeout(timer);
  }, [message, duration]);

  if (!message || dismissedMessage === message) {
    return null;
  }
  const resolvedTone =
    tone || (/失败|错误|异常|无法/.test(message) ? 'error' : 'success');
  let Icon = CheckCircle2;
  if (resolvedTone === 'error') {
    Icon = AlertTriangle;
  } else if (resolvedTone === 'info') {
    Icon = Info;
  }
  return (
    <div
      className={`ui-toast ${resolvedTone}`}
      role={resolvedTone === 'error' ? 'alert' : 'status'}
      aria-live={resolvedTone === 'error' ? 'assertive' : 'polite'}
    >
      <Icon size={16} />
      <span>{message}</span>
      {actionLabel && actionHref && (
        <a
          className="ui-toast-action"
          href={actionHref}
          target="_blank"
          rel="noreferrer"
        >
          {actionLabel}
        </a>
      )}
      {actionLabel && !actionHref && (
        <button type="button" className="ui-toast-action" onClick={onAction}>
          {actionLabel}
        </button>
      )}
      <button
        type="button"
        aria-label="关闭提示"
        onClick={() => setDismissedMessage(message)}
      >
        <X size={14} />
      </button>
    </div>
  );
}

function useDialogA11y(open, onCancel, busy) {
  const dialogRef = useRef(null);
  const cancelRef = useRef(onCancel);
  const busyRef = useRef(busy);
  useEffect(() => {
    cancelRef.current = onCancel;
  }, [onCancel]);
  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const previousFocus = document.activeElement;
    const dialog = dialogRef.current;
    const focusable = () => [
      ...(dialog?.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
          'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) || []),
    ];
    const focusTimer = setTimeout(() => {
      const [firstFocusable] = focusable();
      (dialog?.querySelector('[autofocus]') || firstFocusable)?.focus();
    }, 0);
    const onKeyDown = (event) => {
      if (event.key === 'Escape' && !busyRef.current) {
        event.preventDefault();
        cancelRef.current?.();
        return;
      }
      if (event.key !== 'Tab') {
        return;
      }
      const items = focusable();
      if (!items.length) {
        event.preventDefault();
        return;
      }
      const [first, last] = [items[0], items[items.length - 1]];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      clearTimeout(focusTimer);
      document.removeEventListener('keydown', onKeyDown);
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) {
        previousFocus.focus();
      }
    };
  }, [open]);

  return dialogRef;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = '确认',
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useDialogA11y(open, onCancel, busy);
  if (!open) {
    return null;
  }
  return (
    <div
      className="ui-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) {
          onCancel();
        }
      }}
    >
      <div
        ref={dialogRef}
        className="ui-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
      >
        <div className={`ui-dialog-icon ${danger ? 'danger' : ''}`}>
          <AlertTriangle size={20} />
        </div>
        <div className="ui-dialog-copy">
          <h2 id={titleId}>{title}</h2>
          <p id={descriptionId}>{description}</p>
        </div>
        <div className="ui-dialog-actions">
          <button
            className="btn"
            type="button"
            disabled={busy}
            onClick={onCancel}
          >
            取消
          </button>
          <button
            className={`btn ${danger ? 'btn-danger-solid' : 'btn-p'}`}
            type="button"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? '处理中…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function InputDialog({
  open,
  title,
  description,
  value,
  placeholder,
  confirmLabel = '保存',
  busy = false,
  onChange,
  onConfirm,
  onCancel,
}) {
  const titleId = useId();
  const descriptionId = useId();
  const inputId = useId();
  const dialogRef = useDialogA11y(open, onCancel, busy);
  if (!open) {
    return null;
  }
  return (
    <div
      className="ui-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) {
          onCancel();
        }
      }}
    >
      <form
        ref={dialogRef}
        className="ui-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        onSubmit={(event) => {
          event.preventDefault();
          if (value && !busy) {
            onConfirm();
          }
        }}
      >
        <div className="ui-dialog-icon">
          <KeyRound size={20} />
        </div>
        <div className="ui-dialog-copy">
          <h2 id={titleId}>{title}</h2>
          <p id={descriptionId}>{description}</p>
          <label className="sr-only" htmlFor={inputId}>
            新密码
          </label>
          <input
            id={inputId}
            autoFocus
            type="password"
            value={value}
            placeholder={placeholder}
            onChange={(event) => onChange(event.target.value)}
          />
        </div>
        <div className="ui-dialog-actions">
          <button
            className="btn"
            type="button"
            disabled={busy}
            onClick={onCancel}
          >
            取消
          </button>
          <button className="btn btn-p" type="submit" disabled={!value || busy}>
            {busy ? '处理中…' : confirmLabel}
          </button>
        </div>
      </form>
    </div>
  );
}
