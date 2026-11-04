import React, { useEffect, useId, useRef, useState } from 'react';
import { Icon } from './ui.jsx';

export function UserMenu({ user, onLogout }) {
  const [open, setOpen] = useState(false);
  const container = useRef(null),
    trigger = useRef(null),
    logout = useRef(null),
    id = useId();
  const username = user?.username || '当前账号';
  const initial = user?.username ? Array.from(user.username)[0].toUpperCase() : '?';
  useEffect(() => {
    if (!open) return;
    function outside(event) {
      if (!container.current?.contains(event.target)) setOpen(false);
    }
    function escape(event) {
      if (event.key === 'Escape') {
        setOpen(false);
        trigger.current?.focus();
      }
    }
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);
  return (
    <div
      className="account-menu"
      ref={container}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <button
        type="button"
        ref={trigger}
        id={`${id}-trigger`}
        className={`account-chip ${open ? 'open' : ''}`}
        aria-label="账号菜单"
        title={username}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-haspopup="menu"
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown') {
            event.preventDefault();
            setOpen(true);
            requestAnimationFrame(() => logout.current?.focus());
          }
        }}
      >
        <span className="account-avatar" aria-hidden="true">
          {initial}
        </span>
        <span className="account-name">{username}</span>
        <span className={`account-caret ${open ? 'up' : ''}`} aria-hidden="true">
          ▾
        </span>
      </button>
      {open && (
        <div id={id} className="account-popover" role="menu" aria-labelledby={`${id}-trigger`}>
          <div className="account-menu-header" role="presentation">
            <span className="account-avatar large" aria-hidden="true">
              {initial}
            </span>
            <div>
              <strong>{username}</strong>
              <small>当前登录账号</small>
            </div>
          </div>
          <button
            ref={logout}
            type="button"
            role="menuitem"
            className="account-logout"
            onClick={() => {
              setOpen(false);
              onLogout();
            }}
          >
            <Icon name="logout" />
            退出登录
          </button>
        </div>
      )}
    </div>
  );
}
