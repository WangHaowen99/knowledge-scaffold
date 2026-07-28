import { useEffect, useRef } from 'react';

const engineLabel = (engine) => engine === 'postgresql' ? 'PostgreSQL' : 'MySQL';

export default function ProjectDatabaseCreateModal({
  databaseName,
  engine,
  busy,
  error,
  onCancel,
  onConfirm,
}) {
  const dialogRef = useRef(null);
  useEffect(() => { dialogRef.current?.focus(); }, []);

  const cancel = () => { if (!busy) onCancel(); };
  const handleKeyDown = (event) => {
    if (event.key === 'Escape') {
      if (!busy) onCancel();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(event.currentTarget.querySelectorAll('button:not([disabled])'));
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
      <div ref={dialogRef} className="modal gi-conflict-modal" role="dialog" aria-modal="true"
        aria-labelledby="project-db-create-title" aria-describedby="project-db-create-message"
        aria-busy={busy} tabIndex={-1}>
        <h3 id="project-db-create-title">新建项目数据库</h3>
        <p id="project-db-create-message" className="gi-conflict-message">
          是否使用当前 {engineLabel(engine)} 连接创建本项目唯一的评测数据库？
        </p>
        <div className="project-db-fixed-name">
          <span>固定数据库名</span>
          <code>{databaseName}</code>
        </div>
        <p className="project-db-create-hint">
          该名称由项目 ID 固定生成，不能修改；如果数据库已经存在，将直接复用。当前用户需要具有 CREATE DATABASE 权限。
        </p>
        {error && <div className="lerr">{error}</div>}
        <div className="foot">
          <button className="btn" disabled={busy} onClick={cancel}>取消</button>
          <button className="btn btn-p" disabled={busy} autoFocus onClick={onConfirm}>
            {busy ? '创建中…' : '确认新建'}
          </button>
        </div>
      </div>
    </div>
  );
}
