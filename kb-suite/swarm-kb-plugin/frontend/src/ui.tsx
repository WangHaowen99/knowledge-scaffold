import { ChevronDown, X } from 'lucide-react';
import { useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from 'react';
import type { Tone } from './format';

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  size?: 'sm' | 'md';
  icon?: ReactNode;
};

export function Button({ variant = 'secondary', size = 'md', icon, children, className = '', ...rest }: ButtonProps) {
  return (
    <button className={`btn btn-${variant} btn-${size} ${className}`} {...rest}>
      {icon}
      {children ? <span>{children}</span> : null}
    </button>
  );
}

export function Badge({ tone = 'neutral', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return <span className="spinner" style={{ width: size, height: size }} aria-label="加载中" />;
}

export function Empty({ icon, title, hint, action }: { icon?: ReactNode; title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="empty">
      {icon ? <div className="empty-icon">{icon}</div> : null}
      <div className="empty-title">{title}</div>
      {hint ? <div className="empty-hint">{hint}</div> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  footer,
  width = 480,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={{ width }} role="dialog" aria-label={title}>
        <div className="modal-head">
          <div className="modal-title">{title}</div>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-foot">{footer}</div> : null}
      </div>
    </div>
  );
}

export function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string | null; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {error ? <span className="field-hint field-error">{error}</span> : hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  );
}

export function Tabs<T extends string>({
  value,
  items,
  onChange,
}: {
  value: T;
  items: { key: T; label: string; extra?: ReactNode }[];
  onChange: (key: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {items.map((item) => (
        <button
          key={item.key}
          role="tab"
          aria-selected={value === item.key}
          className={`tab ${value === item.key ? 'tab-active' : ''}`}
          onClick={() => onChange(item.key)}
        >
          {item.label}
          {item.extra}
        </button>
      ))}
    </div>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  hint?: string;
  disabled?: boolean;
}) {
  return (
    <div className={`switch-row ${disabled ? 'switch-row-disabled' : ''}`}>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        className={`switch ${checked ? 'switch-on' : ''}`}
        onClick={() => onChange(!checked)}
      >
        <span />
      </button>
      <div>
        <div className="field-label">{label}</div>
        {hint ? <div className="field-hint">{hint}</div> : null}
      </div>
    </div>
  );
}

export function NumberField({
  label,
  hint,
  value,
  onChange,
  min,
  max,
  step = 1,
  disabled,
  error,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
}) {
  return (
    <Field label={label} hint={hint} error={error}>
      <input
        className="input"
        type="number"
        value={Number.isFinite(value) ? value : ''}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value === '' ? Number.NaN : Number(e.target.value))}
      />
    </Field>
  );
}

/** A button that opens a list of choices. */
export function Menu({
  label,
  icon,
  items,
  align = 'left',
  variant = 'secondary',
  size = 'sm',
}: {
  label: string;
  icon?: ReactNode;
  items: { key: string; label: string; title?: string; disabled?: boolean; onSelect: () => void }[];
  align?: 'left' | 'right';
  variant?: ButtonProps['variant'];
  size?: ButtonProps['size'];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);
  return (
    <div
      className="menu"
      ref={ref}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && open) {
          event.stopPropagation();
          setOpen(false);
          ref.current?.querySelector('button')?.focus();
        }
      }}
    >
      <Button type="button" size={size} variant={variant} icon={icon} aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen(!open)}>
        {label}
        <ChevronDown size={12} className="menu-caret" />
      </Button>
      {open ? (
        <div className={`menu-list ${align === 'right' ? 'menu-list-right' : ''}`} role="menu">
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              title={item.title}
              disabled={item.disabled}
              className="menu-item"
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
