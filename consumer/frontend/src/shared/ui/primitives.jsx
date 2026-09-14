// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import {
  Children,
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
} from 'react';

export function PageHeader({ title, description, actions, eyebrow }) {
  return (
    <header className="ui-page-header">
      <div>
        {eyebrow && <div className="ui-eyebrow">{eyebrow}</div>}
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="ui-page-actions">{actions}</div>}
    </header>
  );
}

export function SectionCard({
  title,
  description,
  actions,
  children,
  className = '',
  ...props
}) {
  return (
    <section className={`card ui-section-card ${className}`.trim()} {...props}>
      {(title || description || actions) && (
        <div className="ui-section-head">
          <div>
            {title && <h3>{title}</h3>}
            {description && <div className="cd">{description}</div>}
          </div>
          {actions && <div className="ui-section-actions">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function FieldRow({ label, hint, children, className = '' }) {
  const labelId = useId();
  const hintId = useId();
  const labelledChildren = Children.map(children, (child) => {
    if (
      !isValidElement(child) ||
      child.props['aria-label'] ||
      child.props['aria-labelledby']
    ) {
      return child;
    }
    const isNativeControl =
      typeof child.type === 'string' &&
      ['input', 'select', 'textarea', 'button'].includes(child.type);
    const isCustomControl = typeof child.type === 'function';
    if (!isNativeControl && !isCustomControl) {
      return child;
    }
    return cloneElement(child, {
      'aria-labelledby': labelId,
      ...(hint ? { 'aria-describedby': hintId } : {}),
    });
  });
  return (
    <div className={`fieldrow ${className}`.trim()}>
      <div className="fl">
        <div className="n" id={labelId}>
          {label}
        </div>
        {hint && (
          <div className="h" id={hintId}>
            {hint}
          </div>
        )}
      </div>
      {labelledChildren}
    </div>
  );
}

export function Table({ children, className = '', ...props }) {
  return (
    <table className={`dt ${className}`.trim()} {...props}>
      {children}
    </table>
  );
}

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
export function Modal({
  title,
  description,
  children,
  actions,
  onClose,
  wide = false,
  closeOnOverlayClick = true,
  closeOnEscape = true,
}) {
  const dialogRef = useRef(null);
  const closeRef = useRef(onClose);
  const closeOnOverlayClickRef = useRef(closeOnOverlayClick);
  const closeOnEscapeRef = useRef(closeOnEscape);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    closeRef.current = onClose;
    closeOnOverlayClickRef.current = closeOnOverlayClick;
    closeOnEscapeRef.current = closeOnEscape;
  }, [onClose, closeOnOverlayClick, closeOnEscape]);

  useEffect(() => {
    const previouslyFocused = document.activeElement;
    const dialog = dialogRef.current;
    const focusable = () =>
      Array.from(
        dialog?.querySelectorAll(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
            'textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        ) || [],
      );
    const [initialTarget] = focusable();
    (initialTarget || dialog)?.focus();
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        if (!closeOnEscapeRef.current) {
          return;
        }
        event.preventDefault();
        closeRef.current?.();
        return;
      }
      if (event.key !== 'Tab') {
        return;
      }
      const items = focusable();
      if (!items.length) {
        event.preventDefault();
        dialog?.focus();
        return;
      }
      const [first, last] = [items[0], items.at(-1)];
      if (!dialog?.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else {
        return;
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      if (
        previouslyFocused instanceof HTMLElement &&
        previouslyFocused.isConnected
      ) {
        previouslyFocused.focus();
      }
    };
  }, []);

  return (
    <div
      className="ui-modal-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (
          closeOnOverlayClickRef.current &&
          event.target === event.currentTarget
        ) {
          onClose?.();
        }
      }}
    >
      <section
        ref={dialogRef}
        className={`ui-modal ${wide ? 'wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
      >
        <header>
          <div>
            <h3 id={titleId}>{title}</h3>
            {description && <p id={descriptionId}>{description}</p>}
          </div>
          <button
            type="button"
            className="iconbtn"
            aria-label="关闭"
            onClick={onClose}
          >
            ✕
          </button>
        </header>
        <div className="ui-modal-body">{children}</div>
        {actions && <footer>{actions}</footer>}
      </section>
    </div>
  );
}

export function EmptyState({
  title = '暂无数据',
  description,
  action,
  compact = false,
}) {
  return (
    <div className={`empty ui-empty ${compact ? 'compact' : ''}`}>
      <strong>{title}</strong>
      {description && <span>{description}</span>}
      {action}
    </div>
  );
}

const STATUS_CLASS = {
  success: 'tag-ok',
  enabled: 'tag-ok',
  active: 'tag-ok',
  approved: 'tag-ok',
  completed: 'tag-ok',
  finished: 'tag-ok',
  warning: 'tag-draft',
  queued: 'tag-draft',
  pending: 'tag-draft',
  awaiting: 'tag-draft',
  error: 'tag-error',
  failed: 'tag-error',
  rejected: 'tag-error',
  disabled: 'tag-off',
  cancelled: 'tag-off',
};

export function StatusTag({ status, children, className = '' }) {
  const normalized = String(status || '').toLowerCase();
  return (
    <span
      className={`tag ${STATUS_CLASS[normalized] || 'tag-lang'} ${className}`.trim()}
    >
      {children || status || '未知'}
    </span>
  );
}

export function ScopeTabs({ items, value, onChange, ariaLabel = '切换范围' }) {
  return (
    <div className="scope-tabs" role="tablist" aria-label={ariaLabel}>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="tab"
          aria-selected={item.id === value}
          className={item.id === value ? 'on' : ''}
          onClick={() => onChange(item.id)}
        >
          {item.badge && (
            <span className={`scope-badge ${item.badgeClass || ''}`}>
              {item.badge}
            </span>
          )}
          {item.label}
        </button>
      ))}
    </div>
  );
}
