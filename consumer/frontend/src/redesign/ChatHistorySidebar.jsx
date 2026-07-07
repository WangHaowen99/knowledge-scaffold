import { useMemo, useState } from 'react';
import { groupSessions, formatTime } from './chatUtils';

// 应用侧边栏里的历史对话列表：挂在“新建对话”导航项下方展开。
export default function ChatHistorySidebar({ chat, onOpen }) {
  const { sessions, currentChatId, deleteChat, loadingSessions } = chat;
  const [search, setSearch] = useState('');
  const groups = useMemo(() => groupSessions(sessions, search), [sessions, search]);

  return (
    <div className="chathist">
      <input className="kb-search ch-search" placeholder="搜索历史对话…"
        value={search} onChange={(e) => setSearch(e.target.value)} />
      {loadingSessions && <div className="ch-empty">加载中…</div>}
      {!loadingSessions && !sessions.length && <div className="ch-empty">还没有历史对话</div>}
      {groups.map(([label, items]) => (
        <div key={label}>
          <div className="ch-group">{label}</div>
          {items.map((s) => (
            <div key={s.id} className={`ch-item ${s.id === currentChatId ? 'on' : ''}`} onClick={() => onOpen(s.id)}>
              <div className="ch-title">{s.title || '未命名对话'}</div>
              <div className="ch-meta">
                <span>{formatTime(s.updated_at || s.created_at)}</span>
                <a title="删除对话"
                  onClick={(e) => { e.stopPropagation(); if (window.confirm('删除该对话？')) deleteChat(s.id); }}>✕</a>
              </div>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
