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

export function SectionCard({ title, description, actions, children, className = '', ...props }) {
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
  return (
    <div className={`fieldrow ${className}`.trim()}>
      <div className="fl">
        <div className="n">{label}</div>
        {hint && <div className="h">{hint}</div>}
      </div>
      {children}
    </div>
  );
}

export function Table({ children, className = '', ...props }) {
  return <table className={`dt ${className}`.trim()} {...props}>{children}</table>;
}

export function Modal({ title, description, children, actions, onClose, wide = false }) {
  return (
    <div className="ui-modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose?.();
    }}>
      <section className={`ui-modal ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
        <header>
          <div>
            <h3>{title}</h3>
            {description && <p>{description}</p>}
          </div>
          <button type="button" className="iconbtn" aria-label="关闭" onClick={onClose}>✕</button>
        </header>
        <div className="ui-modal-body">{children}</div>
        {actions && <footer>{actions}</footer>}
      </section>
    </div>
  );
}

export function EmptyState({ title = '暂无数据', description, action, compact = false }) {
  return (
    <div className={`empty ui-empty ${compact ? 'compact' : ''}`}>
      <strong>{title}</strong>
      {description && <span>{description}</span>}
      {action}
    </div>
  );
}

const STATUS_CLASS = {
  success: 'tag-ok', enabled: 'tag-ok', active: 'tag-ok', approved: 'tag-ok', completed: 'tag-ok', finished: 'tag-ok',
  warning: 'tag-draft', queued: 'tag-draft', pending: 'tag-draft', awaiting: 'tag-draft',
  error: 'tag-error', failed: 'tag-error', rejected: 'tag-error', disabled: 'tag-off', cancelled: 'tag-off',
};

export function StatusTag({ status, children, className = '' }) {
  const normalized = String(status || '').toLowerCase();
  return (
    <span className={`tag ${STATUS_CLASS[normalized] || 'tag-lang'} ${className}`.trim()}>
      {children || status || '未知'}
    </span>
  );
}

export function ScopeTabs({ items, value, onChange, ariaLabel = '切换范围' }) {
  return (
    <div className="scope-tabs" role="tablist" aria-label={ariaLabel}>
      {items.map((item) => (
        <button key={item.id} type="button" role="tab" aria-selected={item.id === value}
          className={item.id === value ? 'on' : ''} onClick={() => onChange(item.id)}>
          {item.badge && <span className={`scope-badge ${item.badgeClass || ''}`}>{item.badge}</span>}
          {item.label}
        </button>
      ))}
    </div>
  );
}
