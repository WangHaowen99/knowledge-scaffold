import {
  CheckOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import { Button, Input, Popconfirm, Popover, Spin, Tag, Tooltip, message } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import {
  conversationTime,
  conversationTitle,
  isRunning,
  sortConversations,
} from '../lib/conversations.js';

const BUSY_HINT = '当前会话运行中，结束或打断后才能切换、新建或删除会话';

// 聊天头部的会话下拉：列出项目的全部会话，可切换、新建、重命名、删除。
// 选中哪条由 ChatPane 决定（onSwitch），服务端不记"当前会话"。
export default function ConversationSwitcher({
  projectId,
  conversationId,
  busy,
  refreshKey,
  onSwitch,
}) {
  const [open, setOpen] = useState(false);
  const [conversations, setConversations] = useState([]);
  const [live, setLive] = useState(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(null); // { id, title }
  const requestRef = useRef(0); // 只采纳最后一次请求，避免切项目后旧列表回写

  const load = useCallback(
    async ({ quiet = true } = {}) => {
      const ticket = (requestRef.current += 1);
      setLoading(true);
      try {
        const data = await api.listConversations(projectId);
        if (ticket !== requestRef.current) return;
        setConversations(sortConversations(data.conversations));
        setLive(data.live);
      } catch (error) {
        if (!quiet && ticket === requestRef.current) message.error(error.message);
      } finally {
        if (ticket === requestRef.current) setLoading(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    void load();
  }, [load, conversationId, refreshKey]);

  useEffect(() => {
    if (busy) setOpen(false);
  }, [busy]);

  const select = (id) => {
    setOpen(false);
    if (id !== conversationId) onSwitch(id);
  };

  const create = async () => {
    setCreating(true);
    try {
      const created = await api.createConversation(projectId);
      setOpen(false);
      onSwitch(created.id);
    } catch (error) {
      message.error(error.message);
    } finally {
      setCreating(false);
    }
  };

  const commitRename = async () => {
    const target = editing;
    const title = target?.title.trim();
    if (!title) {
      setEditing(null);
      return;
    }
    try {
      const renamed = await api.renameConversation(projectId, target.id, title);
      setConversations((prev) => prev.map((c) => (c.id === renamed.id ? renamed : c)));
      setEditing(null);
    } catch (error) {
      message.error(error.message);
    }
  };

  const remove = async (id) => {
    try {
      const data = await api.deleteConversation(projectId, id);
      const remaining = sortConversations(data.conversations);
      setConversations(remaining);
      message.success('会话已删除');
      if (id === conversationId) {
        setOpen(false);
        onSwitch(remaining[0].id);
      }
    } catch (error) {
      message.error(error.message);
    }
  };

  const current = conversations.find((c) => c.id === conversationId);
  const stop = (event) => event.stopPropagation();

  const panel = (
    <div className="conv-panel">
      {loading && !conversations.length && (
        <div className="conv-empty">
          <Spin size="small" />
        </div>
      )}
      {conversations.map((c) => {
        const running = isRunning(live, c.id);
        if (editing?.id === c.id) {
          return (
            <div key={c.id} className="conv-item editing">
              <Input
                size="small"
                autoFocus
                maxLength={60}
                value={editing.title}
                onChange={(e) => setEditing({ ...editing, title: e.target.value })}
                onPressEnter={commitRename}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    e.stopPropagation();
                    setEditing(null);
                  }
                }}
              />
              <Button size="small" type="text" icon={<CheckOutlined />} onClick={commitRename} />
            </div>
          );
        }
        return (
          <div
            key={c.id}
            role="button"
            tabIndex={0}
            className={`conv-item${c.id === conversationId ? ' active' : ''}`}
            onClick={() => select(c.id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') select(c.id);
            }}
          >
            <div className="conv-item-main">
              <span className="conv-item-title">{conversationTitle(c)}</span>
              <span className="conv-item-time">{conversationTime(c.updated_at)}</span>
            </div>
            {running && <Tag color="processing">运行中</Tag>}
            <span className="conv-item-ops" onClick={stop} onKeyDown={stop}>
              <Button
                size="small"
                type="text"
                icon={<EditOutlined />}
                title="重命名"
                onClick={() => setEditing({ id: c.id, title: c.title || '' })}
              />
              <Popconfirm
                title="删除这条会话？"
                description="对话记录与模型上下文一并删除，无法恢复"
                okText="删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                onConfirm={() => remove(c.id)}
              >
                <Button
                  size="small"
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  title={running ? '运行中的会话不能删除' : '删除'}
                  disabled={running}
                />
              </Popconfirm>
            </span>
          </div>
        );
      })}
    </div>
  );

  const trigger = (
    <Button size="small" type="text" className="conv-trigger" disabled={busy}>
      <span className="conv-trigger-title">{current ? conversationTitle(current) : '会话'}</span>
      <DownOutlined />
    </Button>
  );

  return (
    <>
      {busy ? (
        <Tooltip title={BUSY_HINT}>{trigger}</Tooltip>
      ) : (
        <Popover
          open={open}
          onOpenChange={(next) => {
            setOpen(next);
            setEditing(null);
            if (next) void load({ quiet: false });
          }}
          trigger="click"
          placement="bottomLeft"
          arrow={false}
          content={panel}
        >
          {trigger}
        </Popover>
      )}
      <Tooltip title={busy ? BUSY_HINT : '新建会话'}>
        <Button
          size="small"
          type="text"
          icon={<PlusOutlined />}
          loading={creating}
          disabled={busy}
          onClick={create}
        />
      </Tooltip>
    </>
  );
}
