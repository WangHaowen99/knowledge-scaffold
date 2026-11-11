import React, { useEffect, useId, useRef, useState } from 'react';

const paths = {
  grid: (
    <>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </>
  ),
  task: (
    <>
      <rect x="4" y="3" width="16" height="18" rx="3" />
      <path d="m8 8 1 1 2-2m2 1h3M8 14h8m-8 3h5" />
    </>
  ),
  db: (
    <>
      <ellipse cx="12" cy="5" rx="8" ry="3" />
      <path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0" />
    </>
  ),
  set: <path d="M5 3h9l5 5v13H5zM14 3v6h5M8 13h8M8 17h6" />,
  settings: (
    <>
      <path d="m9 3-1 3-3 1 1 4-2 2 2 3 3-1 2 4 4-1 1-3 3-1-1-4 2-2-2-3-3 1-2-4z" />
      <circle cx="12" cy="11" r="3" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  refresh: <path d="M20 6v5h-5M4 18v-5h5M5 8a8 8 0 0 1 14-2M19 16A8 8 0 0 1 5 18" />,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  back: <path d="M20 12H4m6-6-6 6 6 6" />,
  check: <path d="m5 12 4 4 10-10" />,
  close: <path d="m6 6 12 12M18 6 6 18" />,
  search: (
    <>
      <circle cx="10" cy="10" r="6.5" />
      <path d="m15 15 5 5" />
    </>
  ),
  link: <path d="m10 7 3-3a5 5 0 0 1 7 7l-3 3M14 17l-3 3a5 5 0 0 1-7-7l3-3m1 6 8-8" />,
  upload: <path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 10v7m0-11v1" />
    </>
  ),
  shield: (
    <>
      <path d="m12 3 8 3v7c0 4-8 8-8 8s-8-4-8-8V6z" />
      <path d="m8 11 3 3 5-5" />
    </>
  ),
  stop: <rect x="5" y="5" width="14" height="14" rx="2" />,
  logout: <path d="M9 4H4v16h5m5-8h7m-4-4 4 4-4 4" />,
  box: <path d="m12 3 9 5-9 5-9-5zM3 8v10l9 4 9-4V8M12 13v9M7 6l9 5" />,
  message: <path d="M4 5h16v11H10l-5 4v-4H4zM8 9h8M8 12h5" />,
};
export function Icon({ name, ...props }) {
  return (
    <svg
      className="icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {paths[name] || paths.info}
    </svg>
  );
}
export function Button({ children, icon, variant = '', busy, ...props }) {
  return (
    <button type="button" className={`button ${variant}`} {...props} disabled={props.disabled || busy}>
      {(busy || icon) && <Icon name={busy ? 'refresh' : icon} className={`icon ${busy ? 'spin' : ''}`} />}
      {children}
    </button>
  );
}
export function Badge({ children, tone = 'neutral' }) {
  return (
    <span className={`badge ${tone}`}>
      <i />
      {children}
    </span>
  );
}
export function Breadcrumb({ parent, current, onBack, disabled }) {
  return (
    <div className="breadcrumb">
      <Button
        icon="back"
        variant="icon-only ghost"
        aria-label={`返回${parent}`}
        title={`返回${parent}`}
        onClick={onBack}
        disabled={disabled}
      />
      <button type="button" className="text-link" onClick={onBack} disabled={disabled}>
        {parent}
      </button>
      <span aria-hidden="true">/</span>
      <span aria-current="page">{current}</span>
    </div>
  );
}
export function Alert({ children, title, tone = 'blue', action }) {
  return (
    <div className={`notice ${tone}`} role={tone === 'red' ? 'alert' : 'status'}>
      <Icon name="info" />
      <div>
        {title && <strong>{title}</strong>}
        {children && <p>{children}</p>}
      </div>
      {action && <div className="notice-action">{action}</div>}
    </div>
  );
}
export function Empty({ title, children, action, icon = 'task' }) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon name={icon} />
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function Heading({ title, children, actions, breadcrumb }) {
  return (
    <>
      {breadcrumb && <div className="breadcrumb">{breadcrumb}</div>}
      <div className="page-heading">
        <div>
          <h1>{title}</h1>
          {children && <p>{children}</p>}
        </div>
        <div className="actions">{actions}</div>
      </div>
    </>
  );
}
export function Field({ label, hint, error, children }) {
  const id = useId();
  return (
    <label className={`field ${error ? 'invalid' : ''}`} htmlFor={id}>
      <span id={`${id}-label`}>{label}</span>
      {React.isValidElement(children)
        ? React.cloneElement(children, { id, 'aria-labelledby': `${id}-label`, 'aria-invalid': !!error })
        : children}
      {error ? <small className="field-error">{error}</small> : hint && <small>{hint}</small>}
    </label>
  );
}
export function Progress({ value = 0, label }) {
  return (
    <div
      className="progress"
      role="progressbar"
      aria-label={label || '判分进度'}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(value * 100)}
    >
      <span style={{ width: `${Math.min(100, Math.max(0, value * 100))}%` }} />
    </div>
  );
}
export function Skeleton() {
  return (
    <div className="skeleton" role="status" aria-label="正在加载">
      <i />
      <i />
      <i />
    </div>
  );
}
export function Modal({ title, children, onClose, footer, drawer = false }) {
  const ref = useRef(null),
    id = useId();
  useEffect(() => {
    const previous = document.activeElement;
    ref.current?.showModal();
    return () => {
      previous?.focus?.();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={drawer ? 'dialog drawer' : 'dialog'}
      aria-labelledby={id}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="dialog-layout">
        <header className="dialog-header">
          <h2 id={id}>{title}</h2>
          <Button icon="close" variant="icon-only ghost" aria-label="关闭" onClick={onClose} />
        </header>
        <div className="dialog-body">{children}</div>
        {footer && <footer className="dialog-footer">{footer}</footer>}
      </div>
    </dialog>
  );
}
export function useAction() {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [details, setDetails] = useState(null);
  const locked = useRef(false);
  async function act(work) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError('');
    setDetails(null);
    try {
      return await work();
    } catch (e) {
      setError(e.message);
      // Structured detail of the error, such as the objects that conflict with the data tables.
      setDetails(e.details || null);
      return undefined;
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  return { busy, error, setError, details, act };
}
export function Confirm({ title, children, onConfirm, onClose, label = '确认', danger = false }) {
  const { busy, error, act } = useAction();
  return (
    <Modal
      title={title}
      onClose={() => {
        if (!busy) onClose();
      }}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button
            busy={busy}
            variant={danger ? 'danger-fill' : 'primary'}
            onClick={() =>
              act(async () => {
                await onConfirm();
                onClose();
              })
            }
          >
            {label}
          </Button>
        </>
      }
    >
      {children}
      {error && <Alert tone="red">{error}</Alert>}
    </Modal>
  );
}
