// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { useMemo, useState } from 'react';
import {
  Clock3,
  Loader2,
  MessageCircleQuestion,
  Search,
  Trash2,
} from 'lucide-react';
import { ConfirmDialog, SkeletonState } from '../../shared/ui/feedback';
import { formatTime, groupSessions } from './lib/chatPresentation';
import { resolveSessionRunIndicator } from './lib/sessionScope';

const RUN_STATUS_PRESENTATION = {
  queued: { label: '排队中', Icon: Clock3 },
  running: { label: '运行中', Icon: Loader2 },
  awaiting_input: { label: '需要输入', Icon: MessageCircleQuestion },
};

// 侧边栏历史对话工作区：标题与搜索固定，列表使用侧栏的剩余高度独立滚动。
// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
export default function ChatHistorySidebar({ chat, onOpen }) {
  const {
    sessions,
    currentChatId,
    deleteChat,
    loadingSessions,
    loadingMoreSessions,
    hasMoreSessions,
    loadMoreSessions,
    setPinned,
    streamStates,
  } = chat;
  const [search, setSearch] = useState('');
  const [deleteId, setDeleteId] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const groups = useMemo(
    () => groupSessions(sessions, search),
    [sessions, search],
  );

  return (
    <div className="side-hist">
      <div className="sh-head">
        <div className="sh-title">历史对话</div>
        {!loadingSessions && (
          <span
            className="sh-count"
            aria-label={`共 ${sessions.length} 条对话`}
          >
            {sessions.length}
          </span>
        )}
      </div>
      <label className="ch-search-wrap">
        <Search size={14} aria-hidden="true" />
        <span className="sr-only">搜索历史对话</span>
        <input
          className="kb-search ch-search"
          placeholder="搜索历史对话…"
          aria-label="搜索历史对话"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </label>
      <div className="ch-list">
        {loadingSessions && (
          <SkeletonState compact rows={4} label="正在加载历史对话" />
        )}
        {!loadingSessions && !sessions.length && (
          <div className="ch-empty">还没有历史对话</div>
        )}
        {!loadingSessions && sessions.length > 0 && groups.length === 0 && (
          <div className="ch-empty">未找到匹配的对话</div>
        )}
        {groups.map(([label, items]) => (
          <div key={label}>
            <div className="ch-group">{label}</div>
            {items.map((s) => {
              const runStatus = resolveSessionRunIndicator(
                s,
                streamStates?.get(s.id),
              );
              const runStatusPresentation = RUN_STATUS_PRESENTATION[runStatus];
              const runStatusLabel = runStatusPresentation?.label || '';
              const RunStatusIcon = runStatusPresentation?.Icon;
              return (
                <div
                  key={s.id}
                  className={`ch-item-wrap ${s.id === currentChatId ? 'on' : ''}`}
                >
                  <button
                    type="button"
                    className="ch-item"
                    onClick={() => onOpen(s.id)}
                  >
                    <span className="ch-title">
                      {s.metadata?.pinned && <span title="已固定">★ </span>}
                      {s.title || '未命名对话'}
                    </span>
                    <span className="ch-meta">
                      {RunStatusIcon && (
                        <span className="ch-run-status" title={runStatusLabel}>
                          <RunStatusIcon
                            size={11}
                            className={
                              runStatus === 'running' ? 'spin' : undefined
                            }
                            aria-label={runStatusLabel}
                          />
                          <span aria-hidden="true">{runStatusLabel} ·</span>
                        </span>
                      )}
                      {formatTime(s.updated_at || s.created_at)}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="ch-pin"
                    title={s.metadata?.pinned ? '取消固定' : '固定对话'}
                    aria-label={`${s.metadata?.pinned ? '取消固定' : '固定对话'} ${s.title || '未命名对话'}`}
                    onClick={() => setPinned(s.id, !s.metadata?.pinned)}
                  >
                    {s.metadata?.pinned ? '☆' : '★'}
                  </button>
                  <button
                    type="button"
                    className="ch-delete"
                    title="删除对话"
                    aria-label={`删除对话 ${s.title || '未命名对话'}`}
                    onClick={() => setDeleteId(s.id)}
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              );
            })}
          </div>
        ))}
        {!loadingSessions && hasMoreSessions && (
          <button
            type="button"
            className="ch-load-more"
            disabled={loadingMoreSessions}
            onClick={loadMoreSessions}
          >
            {loadingMoreSessions && (
              <Loader2 size={13} className="spin" aria-hidden="true" />
            )}
            <span>{loadingMoreSessions ? '正在加载' : '加载更多'}</span>
          </button>
        )}
      </div>
      {deleteError && (
        <div className="form-error" role="alert">
          {deleteError}
        </div>
      )}
      <ConfirmDialog
        open={Boolean(deleteId)}
        title="删除对话"
        description="删除后将无法恢复这条对话及其运行记录。"
        confirmLabel="删除对话"
        danger
        busy={deleting}
        onConfirm={async () => {
          setDeleting(true);
          setDeleteError('');
          const deleted = await deleteChat(deleteId);
          setDeleting(false);
          if (deleted) {
            setDeleteId('');
          } else {
            setDeleteError(
              '删除失败；如有任务正在执行，请等待完成或显式停止。会话和运行状态已保留。',
            );
          }
        }}
        onCancel={() => {
          if (!deleting) {
            setDeleteId('');
          }
        }}
      />
    </div>
  );
}
