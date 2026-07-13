import { useMemo, useState } from 'react';
import { groupSessions, formatTime } from './chatUtils';

// 侧边栏左下固定的历史对话区块：所有页面常驻，标题 + 搜索框固定，列表内部滚动。
export default function ChatHistorySidebar({ chat, onOpen }) {
  const { sessions, currentChatId, deleteChat, loadingSessions, setPinned } = chat;
  const [search, setSearch] = useState('');
  const groups = useMemo(() => groupSessions(sessions, search), [sessions, search]);

  return (
    <div className="side-hist">
      <div className="sh-title">历史对话</div>
      <input className="kb-search ch-search" placeholder="搜索历史对话…"
        value={search} onChange={(e) => setSearch(e.target.value)} />
      <div className="ch-list">
        {loadingSessions && <div className="ch-empty">加载中…</div>}
        {!loadingSessions && !sessions.length && <div className="ch-empty">还没有历史对话</div>}
        {groups.map(([label, items]) => (
          <div key={label}>
            <div className="ch-group">{label}</div>
            {items.map((s) => (
              <div key={s.id} className={`ch-item ${s.id === currentChatId ? 'on' : ''}`} onClick={() => onOpen(s.id)}>
                <div className="ch-title">
                  {s.metadata?.pinned && <span title="已固定">★ </span>}
                  {s.title || '未命名对话'}
                </div>
                <div className="ch-meta">
                  <span>
                    {['queued', 'running'].includes(s.active_run_status)
                      ? `${s.active_run_status === 'queued' ? '排队中' : '运行中'} · `
                      : ''}
                    {formatTime(s.updated_at || s.created_at)}
                  </span>
                  <span className="ch-actions">
                    <button type="button" title={s.metadata?.pinned ? '取消固定' : '固定对话'}
                      onClick={(e) => { e.stopPropagation(); setPinned(s.id, !s.metadata?.pinned); }}>
                      {s.metadata?.pinned ? '☆' : '★'}
                    </button>
                    <button type="button" title="删除对话"
                      onClick={(e) => { e.stopPropagation(); if (window.confirm('删除该对话？')) deleteChat(s.id); }}>✕</button>
                  </span>
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
