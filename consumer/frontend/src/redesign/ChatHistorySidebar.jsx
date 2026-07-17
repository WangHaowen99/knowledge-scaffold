import { useMemo, useState } from 'react';
import { Trash2 } from 'lucide-react';
import { groupSessions, formatTime } from './chatUtils';
import { ConfirmDialog, SkeletonState } from './UiFeedback';

// 侧边栏左下固定的历史对话区块：所有页面常驻，标题 + 搜索框固定，列表内部滚动。
export default function ChatHistorySidebar({ chat, onOpen }) {
  const { sessions, currentChatId, deleteChat, loadingSessions, setPinned, activeSessionIds } = chat;
  const [search, setSearch] = useState('');
  const [deleteId, setDeleteId] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const groups = useMemo(() => groupSessions(sessions, search), [sessions, search]);

  return (
    <div className="side-hist">
      <div className="sh-title">历史对话</div>
      <input className="kb-search ch-search" placeholder="搜索历史对话…"
        value={search} onChange={(e) => setSearch(e.target.value)} />
      <div className="ch-list">
        {loadingSessions && <SkeletonState compact rows={4} label="正在加载历史对话" />}
        {!loadingSessions && !sessions.length && <div className="ch-empty">还没有历史对话</div>}
        {groups.map(([label, items]) => (
          <div key={label}>
            <div className="ch-group">{label}</div>
            {items.map((s) => (
              <div key={s.id} className={`ch-item-wrap ${s.id === currentChatId ? 'on' : ''}`}>
                <button type="button" className="ch-item" onClick={() => onOpen(s.id)}>
                  <span className="ch-title">
                    {s.metadata?.pinned && <span title="已固定">★ </span>}
                    {s.title || '未命名对话'}
                  </span>
                  <span className="ch-meta">
                    {(activeSessionIds.includes(s.id) || ['queued', 'running'].includes(s.active_run_status))
                      ? `${s.active_run_status === 'queued' ? '排队中' : '运行中'} · `
                      : ''}
                    {formatTime(s.updated_at || s.created_at)}
                  </span>
                </button>
                <button type="button" className="ch-pin" title={s.metadata?.pinned ? '取消固定' : '固定对话'}
                  aria-label={`${s.metadata?.pinned ? '取消固定' : '固定对话'} ${s.title || '未命名对话'}`}
                  onClick={() => setPinned(s.id, !s.metadata?.pinned)}>{s.metadata?.pinned ? '☆' : '★'}</button>
                <button type="button" className="ch-delete" title="删除对话" aria-label={`删除对话 ${s.title || '未命名对话'}`}
                  onClick={() => setDeleteId(s.id)}><Trash2 size={12} /></button>
              </div>
            ))}
          </div>
        ))}
      </div>
      {deleteError && <div className="form-error" role="alert">{deleteError}</div>}
      <ConfirmDialog open={Boolean(deleteId)} title="删除对话" description="删除后将无法恢复这条对话及其运行记录。"
        confirmLabel="删除对话" danger busy={deleting}
        onConfirm={async () => {
          setDeleting(true);
          setDeleteError('');
          const deleted = await deleteChat(deleteId);
          setDeleting(false);
          if (deleted) setDeleteId('');
          else setDeleteError('删除失败，会话和运行状态已保留，请重试。');
        }}
        onCancel={() => { if (!deleting) setDeleteId(''); }} />
    </div>
  );
}
