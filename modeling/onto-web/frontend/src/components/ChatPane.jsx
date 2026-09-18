import { Bubble, Prompts, Sender } from '@ant-design/x';
import { Alert, Button, Card, Space, Typography, message } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, openEventStream } from '../lib/api.js';
import { Markdown } from './CenterPane.jsx';

const QUICK_PROMPTS = [
  { key: 'start', label: '开始建模', description: '检测进度并引导下一步', text: '/ontology' },
  { key: 'research', label: '开始调研', description: '对领域做系统调研', text: '/ontology-research ' },
  { key: 'extract-object', label: '提取对象', description: '识别业务数据实体', text: '/ontology-extract-object' },
  { key: 'extract-logic', label: '提取逻辑', description: '提取业务流程', text: '/ontology-extract-logic' },
  { key: 'validate', label: '校验', description: '规则检查 + 语义检查', text: '/ontology-validate' },
];

export default function ChatPane({ project, onWorkspaceChanged }) {
  const [events, setEvents] = useState([]);
  const [input, setInput] = useState('');
  const [state, setState] = useState('idle');
  const historyRef = useRef(null);
  const seenRef = useRef(new Set());

  useEffect(() => {
    setEvents([]);
    seenRef.current = new Set();
    const source = openEventStream(project.id, (event) => {
      const key = `${event.seq}`;
      if (seenRef.current.has(key)) return;
      seenRef.current.add(key);
      setEvents((prev) => [...prev, event]);
      if (event.type === 'session_state') setState(event.state);
      if (event.type === 'result') onWorkspaceChanged?.();
    });
    return () => source.close();
    // onWorkspaceChanged 变化不应重建 SSE 连接
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id]);

  useEffect(() => {
    const el = historyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events]);

  const send = async (text) => {
    if (!text.trim()) return;
    setInput('');
    try {
      await api.sendMessage(project.id, text);
      setState('running');
    } catch (error) {
      message.error(error.message);
    }
  };

  const respondPermission = async (requestId, behavior) => {
    try {
      await api.respondPermission(project.id, requestId, behavior);
    } catch (error) {
      message.error(error.message);
    }
  };

  const resolvedPermissions = useMemo(
    () =>
      new Set(
        events
          .filter((e) => e.type === 'permission_resolved')
          .map((e) => e.request_id),
      ),
    [events],
  );

  const items = useMemo(() => {
    const list = [];
    for (const event of events) {
      if (event.type === 'user_message') {
        list.push({ key: event.seq, role: 'user', content: event.text });
      } else if (event.type === 'assistant_message') {
        for (const [index, block] of event.blocks.entries()) {
          const key = `${event.seq}-${index}`;
          if (block.kind === 'text') {
            list.push({ key, role: 'assistant', content: block.text });
          } else if (block.kind === 'tool_use') {
            list.push({ key, role: 'tool', content: `正在执行：${block.tool}` });
          }
        }
      } else if (event.type === 'permission_request') {
        list.push({
          key: event.seq,
          role: 'permission',
          content: event,
          resolved: resolvedPermissions.has(event.request_id),
        });
      } else if (event.type === 'session_error') {
        list.push({ key: event.seq, role: 'error', content: event.error });
      }
    }
    return list;
  }, [events, resolvedPermissions]);

  return (
    <>
      <div className="chat-history" ref={historyRef}>
        {items.length === 0 && (
          <Typography.Paragraph type="secondary" style={{ padding: 12 }}>
            回复下方建议或直接输入，开始建模。
          </Typography.Paragraph>
        )}
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          {items.map((item) => {
            if (item.role === 'permission') {
              const req = item.content;
              return (
                <Card key={item.key} size="small" title={req.title || `确认操作：${req.tool_name}`}>
                  {req.description && (
                    <Typography.Paragraph type="secondary">
                      {req.description}
                    </Typography.Paragraph>
                  )}
                  <Space>
                    <Button
                      type="primary"
                      size="small"
                      disabled={item.resolved}
                      onClick={() => respondPermission(req.request_id, 'allow')}
                    >
                      允许
                    </Button>
                    <Button
                      size="small"
                      disabled={item.resolved}
                      onClick={() => respondPermission(req.request_id, 'deny')}
                    >
                      拒绝
                    </Button>
                    {item.resolved && <Typography.Text type="secondary">已处理</Typography.Text>}
                  </Space>
                </Card>
              );
            }
            if (item.role === 'error') {
              return <Alert key={item.key} type="error" showIcon message={item.content} />;
            }
            if (item.role === 'tool') {
              return (
                <div key={item.key} className="tool-line">
                  {item.content}
                </div>
              );
            }
            return (
              <Bubble
                key={item.key}
                placement={item.role === 'user' ? 'end' : 'start'}
                variant={item.role === 'user' ? 'filled' : 'outlined'}
                content={
                  item.role === 'assistant' ? (
                    <Markdown content={item.content} />
                  ) : (
                    item.content
                  )
                }
              />
            );
          })}
          {state === 'running' && <Bubble loading content="" />}
        </Space>
      </div>
      <div className="chat-footer">
        <Prompts
          items={QUICK_PROMPTS.map(({ key, label, description }) => ({ key, label, description }))}
          onItemClick={(info) => {
            const prompt = QUICK_PROMPTS.find((p) => p.key === info.data.key);
            if (!prompt) return;
            if (prompt.text.endsWith(' ')) setInput(prompt.text);
            else send(prompt.text);
          }}
          style={{ marginBottom: 8 }}
        />
        <Sender
          value={input}
          onChange={setInput}
          onSubmit={send}
          loading={state === 'running'}
          placeholder="输入消息，或回复 确认 / 继续 等关键词"
          onCancel={() => api.interrupt(project.id).catch(() => {})}
        />
      </div>
    </>
  );
}
