import { Bubble, Sender } from '@ant-design/x';
import {
  CheckOutlined,
  CloseOutlined,
  DownOutlined,
  FileOutlined,
  LoadingOutlined,
  PaperClipOutlined,
} from '@ant-design/icons';
import { Alert, Button, Card, Input, Space, Tag, Typography, Upload, message } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, openEventStream } from '../lib/api.js';
import { Markdown } from './CenterPane.jsx';

const QUICK_PROMPTS = [
  { key: 'start', label: '开始建模', text: '/ontology' },
  { key: 'research', label: '开始调研', text: '/ontology-research ' },
  { key: 'extract-object', label: '提取对象', text: '/ontology-extract-object' },
  { key: 'extract-logic', label: '提取逻辑', text: '/ontology-extract-logic' },
  { key: 'validate', label: '校验', text: '/ontology-validate' },
  { key: 'overview', label: '整体视图', text: '/ontology-overview' },
];

const USER_BUBBLE_STYLE = {
  content: { background: 'var(--color-primary)', color: '#fff' },
};
// QQ 式灰色气泡：浅灰填充、无边框、大圆角
const ASSISTANT_BUBBLE_STYLE = {
  content: {
    background: 'var(--surface-subtle)',
    border: 'none',
    color: 'var(--text-primary)',
    borderRadius: 12,
  },
};

function resourceSubdir(filename) {
  const lower = filename.toLowerCase();
  if (lower.endsWith('.xlsx') || lower.endsWith('.xls')) return 'excel';
  if (lower.endsWith('.drawio')) return 'diagrams';
  return 'others';
}

function fileExt(name) {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toUpperCase() : '文件';
}

function toolSummary(block) {
  const input = block.input || {};
  const raw =
    input.file_path ||
    input.path ||
    input.command ||
    input.pattern ||
    input.description ||
    input.url ||
    input.query ||
    '';
  let text = typeof raw === 'string' ? raw : JSON.stringify(raw);
  // 工作区绝对路径裁剪成相对路径，聊天里不暴露服务器目录
  text = text.replace(/^\/[^\s]*workspaces\/[^/]+\//, '');
  if (text.length > 64) text = `${text.slice(0, 64)}…`;
  return text;
}

function ThinkingBlock({ text }) {
  return (
    <details className="thinking-block">
      <summary>思考过程</summary>
      <div className="thinking-body">{text}</div>
    </details>
  );
}

function ToolLine({ tool }) {
  return (
    <div className="tool-line">
      {tool.status === undefined ? (
        <LoadingOutlined className="tool-status running" />
      ) : tool.status ? (
        <CloseOutlined className="tool-status error" />
      ) : (
        <CheckOutlined className="tool-status ok" />
      )}
      <span className="tool-name">{tool.tool}</span>
      {tool.summary && <span className="tool-arg">{tool.summary}</span>}
    </div>
  );
}

// 连续的工具调用收进一个可展开的执行组，避免长任务刷屏
function ToolGroup({ tools }) {
  const [open, setOpen] = useState(false);
  const running = tools.some((t) => t.status === undefined);
  const errors = tools.filter((t) => t.status).length;
  const latest = tools[tools.length - 1];

  return (
    <div className={`tool-group${running ? ' running' : ''}`}>
      <div className="tool-group-header" onClick={() => setOpen(!open)}>
        {running ? (
          <LoadingOutlined className="tool-status running" />
        ) : errors > 0 ? (
          <CloseOutlined className="tool-status error" />
        ) : (
          <CheckOutlined className="tool-status ok" />
        )}
        <span className="tool-group-title">
          {running ? '执行中' : '执行完成'} · {tools.length} 步
        </span>
        {errors > 0 && <span className="tool-group-err">{errors} 步失败</span>}
        <DownOutlined className={`tool-group-caret${open ? ' open' : ''}`} />
      </div>
      {open ? (
        <div className="tool-group-body">
          {tools.map((tool) => (
            <ToolLine key={tool.key} tool={tool} />
          ))}
        </div>
      ) : (
        running && latest && (
          <div className="tool-group-body">
            <ToolLine tool={latest} />
          </div>
        )
      )}
    </div>
  );
}

// AskUserQuestion 专用卡：渲染问题与选项，答案经 answers 回填给工具
function QuestionCard({ req, resolved, onAnswer }) {
  const questions = req.tool_input?.questions || [];
  const [chosen, setChosen] = useState({});
  const [custom, setCustom] = useState('');
  const multi = questions.length > 1 || questions.some((q) => q.multiSelect);

  const submit = (answers) => onAnswer(req.request_id, answers);

  const choose = (q, label) => {
    if (resolved) return;
    if (q.multiSelect) {
      const current = new Set((chosen[q.question] || '').split(',').filter(Boolean));
      current.has(label) ? current.delete(label) : current.add(label);
      setChosen({ ...chosen, [q.question]: [...current].join(',') });
      return;
    }
    const next = { ...chosen, [q.question]: label };
    setChosen(next);
    if (!multi) submit(next);
  };

  const allAnswered = questions.every((q) => chosen[q.question]);

  return (
    <Card size="small" className="question-card" title="助手向你提问">
      {questions.map((q) => (
        <div key={q.question} className="question-item">
          <div className="question-text">
            {q.header && <Tag>{q.header}</Tag>}
            {q.question}
          </div>
          <div className="question-options">
            {(q.options || []).map((opt) => {
              const selected = (chosen[q.question] || '').split(',').includes(opt.label);
              return (
                <div
                  key={opt.label}
                  className={`question-option${selected ? ' selected' : ''}${resolved ? ' disabled' : ''}`}
                  onClick={() => choose(q, opt.label)}
                >
                  <div className="option-label">{opt.label}</div>
                  {opt.description && <div className="option-desc">{opt.description}</div>}
                </div>
              );
            })}
          </div>
        </div>
      ))}
      {!resolved && (
        <Space.Compact style={{ width: '100%', marginTop: 8 }}>
          <Input
            size="small"
            placeholder="或输入自定义回答"
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            onPressEnter={() => {
              if (custom.trim() && questions[0]) {
                submit({ [questions[0].question]: custom.trim() });
              }
            }}
          />
          {multi ? (
            <Button size="small" type="primary" disabled={!allAnswered} onClick={() => submit(chosen)}>
              提交
            </Button>
          ) : (
            <Button
              size="small"
              disabled={!custom.trim()}
              onClick={() => submit({ [questions[0]?.question]: custom.trim() })}
            >
              发送
            </Button>
          )}
        </Space.Compact>
      )}
      {resolved && <Typography.Text type="secondary">已答复</Typography.Text>}
    </Card>
  );
}

export default function ChatPane({ project, onWorkspaceChanged, registerSend }) {
  const [events, setEvents] = useState([]);
  const [live, setLive] = useState({ text: '', thinking: '' });
  const [input, setInput] = useState('');
  const [state, setState] = useState('idle');
  const [pending, setPending] = useState(null); // 乐观显示的用户消息
  const [metrics, setMetrics] = useState(null);
  const [attachments, setAttachments] = useState([]);
  const [dragActive, setDragActive] = useState(false);
  const historyRef = useRef(null);
  const seenRef = useRef(new Set());
  const uidRef = useRef(0);
  const liveRef = useRef({ text: '', thinking: '' });
  const metricsRef = useRef(null);
  const rafRef = useRef(0);

  const flushLive = () => {
    rafRef.current = 0;
    setLive({ ...liveRef.current });
    const m = metricsRef.current;
    if (m?.firstTs) {
      setMetrics({
        ttft: m.firstTs - m.sentTs,
        perToken: m.deltaCount > 1 ? (m.lastTs - m.firstTs) / m.deltaCount : null,
        tokens: null,
        done: false,
      });
    }
  };
  const scheduleFlush = () => {
    if (!rafRef.current) rafRef.current = requestAnimationFrame(flushLive);
  };

  const addFiles = (files) => {
    const next = [...files].map((file) => ({
      uid: `att-${(uidRef.current += 1)}`,
      name: file.name,
      file,
    }));
    if (next.length) setAttachments((prev) => [...prev, ...next]);
  };

  useEffect(() => {
    setEvents([]);
    setLive({ text: '', thinking: '' });
    liveRef.current = { text: '', thinking: '' };
    setPending(null);
    setMetrics(null);
    seenRef.current = new Set();
    const source = openEventStream(project.id, (event) => {
      if (event.type === 'stream_delta') {
        if (event.kind === 'thinking') liveRef.current.thinking += event.text;
        else liveRef.current.text += event.text;
        const m = metricsRef.current;
        if (m) {
          if (!m.firstTs) m.firstTs = event.ts;
          m.lastTs = event.ts;
          m.deltaCount = (m.deltaCount || 0) + 1;
        }
        scheduleFlush();
        return;
      }
      if (event.seq !== undefined) {
        const key = `${event.seq}`;
        if (seenRef.current.has(key)) return;
        seenRef.current.add(key);
      }
      if (event.type === 'user_message') {
        setPending(null);
        metricsRef.current = { sentTs: event.ts };
        setMetrics(null);
      }
      if (event.type === 'assistant_message' && !event.subagent) {
        liveRef.current = { text: '', thinking: '' };
        setLive({ text: '', thinking: '' });
      }
      if (event.type === 'result') {
        const m = metricsRef.current;
        const outputTokens = event.usage?.output_tokens;
        if (m?.firstTs) {
          setMetrics({
            ttft: m.firstTs - m.sentTs,
            perToken:
              outputTokens && m.lastTs > m.firstTs
                ? (m.lastTs - m.firstTs) / outputTokens
                : m.deltaCount > 1
                  ? (m.lastTs - m.firstTs) / m.deltaCount
                  : null,
            tokens: outputTokens || null,
            done: true,
          });
        }
      }
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
  }, [events, live, pending]);

  const send = async (text) => {
    const trimmed = (text || '').trim();
    if (!trimmed && attachments.length === 0) return;
    setInput('');
    setPending({ text: trimmed || '（发送资料）' });
    try {
      const uploaded = [];
      for (const att of attachments) {
        const saved = await api.uploadResource(
          project.id,
          resourceSubdir(att.name),
          att.file,
        );
        uploaded.push(saved.path);
      }
      if (uploaded.length) {
        setAttachments([]);
        onWorkspaceChanged?.();
      }
      const prefix = uploaded.length
        ? `（我通过界面上传了资料，已存入工作区：${uploaded.join('、')}）\n`
        : '';
      const body = trimmed || '请查看我刚上传的资料，说明接下来可以怎么用。';
      await api.sendMessage(project.id, prefix + body);
      setState('running');
    } catch (error) {
      setPending(null);
      message.error(error.message);
    }
  };

  useEffect(() => {
    registerSend?.((command) => {
      if (command.endsWith(' ')) setInput(command);
      else send(command);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id]);

  const respondPermission = async (requestId, behavior, answers = null) => {
    try {
      await api.respondPermission(project.id, requestId, behavior, '', answers);
    } catch (error) {
      message.error(error.message);
    }
  };

  const resolvedPermissions = useMemo(
    () =>
      new Set(
        events.filter((e) => e.type === 'permission_resolved').map((e) => e.request_id),
      ),
    [events],
  );

  // tool_use_id → is_error，用于给工具行标记完成状态
  const toolResults = useMemo(() => {
    const map = new Map();
    for (const event of events) {
      if (event.type === 'tool_results') {
        for (const block of event.blocks) map.set(block.tool_use_id, block.is_error);
      }
    }
    return map;
  }, [events]);

  const items = useMemo(() => {
    const list = [];
    const pushTool = (tool) => {
      const last = list[list.length - 1];
      if (last?.role === 'toolgroup') last.tools.push(tool);
      else list.push({ key: `tg-${tool.key}`, role: 'toolgroup', tools: [tool] });
    };
    for (const event of events) {
      if (event.type === 'user_message') {
        list.push({ key: event.seq, role: 'user', content: event.text });
      } else if (event.type === 'assistant_message') {
        for (const [index, block] of event.blocks.entries()) {
          const key = `${event.seq}-${index}`;
          if (block.kind === 'tool_use') {
            pushTool({
              key,
              tool: block.tool,
              summary: toolSummary(block),
              status: toolResults.get(block.tool_use_id),
            });
          } else if (event.subagent) {
            continue;
          } else if (block.kind === 'text') {
            list.push({ key, role: 'assistant', content: block.text });
          } else if (block.kind === 'thinking' && block.text?.trim()) {
            list.push({ key, role: 'thinking', content: block.text });
          }
        }
      } else if (event.type === 'permission_request') {
        list.push({
          key: event.seq,
          role: event.tool_name === 'AskUserQuestion' ? 'question' : 'permission',
          content: event,
          resolved: resolvedPermissions.has(event.request_id),
        });
      } else if (event.type === 'session_error') {
        list.push({ key: event.seq, role: 'error', content: event.error });
      }
    }
    return list;
  }, [events, resolvedPermissions, toolResults]);

  const hasLive = live.text || live.thinking;

  return (
    <div
      className="chat-root"
      onDragOver={(e) => {
        e.preventDefault();
        setDragActive(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setDragActive(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragActive(false);
        addFiles(e.dataTransfer.files);
      }}
    >
      {dragActive && (
        <div className="drop-overlay">
          <FileOutlined style={{ fontSize: 28 }} />
          <div>松开以添加附件</div>
        </div>
      )}
      <div className="chat-history" ref={historyRef}>
        {items.length === 0 && !hasLive && !pending && (
          <Typography.Paragraph type="secondary" style={{ padding: 12 }}>
            回复下方建议或直接输入，开始建模。
          </Typography.Paragraph>
        )}
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          {items.map((item) => {
            if (item.role === 'question') {
              return (
                <div key={item.key} className="msg-in msg-left">
                  <QuestionCard
                    req={item.content}
                    resolved={item.resolved}
                    onAnswer={(requestId, answers) =>
                      respondPermission(requestId, 'allow', answers)
                    }
                  />
                </div>
              );
            }
            if (item.role === 'permission') {
              const req = item.content;
              return (
                <Card
                  key={item.key}
                  size="small"
                  className="msg-in msg-left"
                  title={req.title || `确认操作：${req.tool_name}`}
                >
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
                    {item.resolved && (
                      <Typography.Text type="secondary">已处理</Typography.Text>
                    )}
                  </Space>
                </Card>
              );
            }
            if (item.role === 'error') {
              return (
                <Alert
                  key={item.key}
                  className="msg-in msg-left"
                  type="error"
                  showIcon
                  message={item.content}
                />
              );
            }
            if (item.role === 'toolgroup') {
              return (
                <div key={item.key} className="msg-soft">
                  <ToolGroup tools={item.tools} />
                </div>
              );
            }
            if (item.role === 'thinking') {
              return (
                <div key={item.key} className="msg-soft">
                  <ThinkingBlock text={item.content} />
                </div>
              );
            }
            const isUser = item.role === 'user';
            return (
              <div
                key={item.key}
                className={isUser ? 'msg-in msg-right' : 'msg-soft msg-left-wrap'}
              >
                <Bubble
                  placement={isUser ? 'end' : 'start'}
                  variant="filled"
                  styles={isUser ? USER_BUBBLE_STYLE : ASSISTANT_BUBBLE_STYLE}
                  content={isUser ? item.content : <Markdown content={item.content} />}
                />
              </div>
            );
          })}
          {pending && (
            <div className="msg-in msg-right">
              <Bubble placement="end" variant="filled" styles={USER_BUBBLE_STYLE} content={pending.text} />
            </div>
          )}
          {hasLive && (
            <div className="msg-soft msg-left-wrap">
              {live.thinking && <ThinkingBlock text={live.thinking} />}
              {live.text && (
                <Bubble
                  placement="start"
                  variant="filled"
                  styles={ASSISTANT_BUBBLE_STYLE}
                  content={<Markdown content={live.text} />}
                />
              )}
            </div>
          )}
          {state === 'running' && !hasLive && (
            <Bubble loading styles={ASSISTANT_BUBBLE_STYLE} variant="filled" content="" />
          )}
        </Space>
      </div>
      <div className="chat-footer">
        {metrics && (
          <div className="metrics-line">
            {metrics.done ? '上次调用' : '生成中'} · 首Token{' '}
            {metrics.ttft >= 0 ? `${metrics.ttft.toFixed(1)}s` : '-'}
            {metrics.perToken != null && (
              <>
                {' '}
                · {Math.round(metrics.perToken * 1000)}ms/token
                {metrics.perToken > 0 && ` (≈${Math.round(1 / metrics.perToken)} tok/s)`}
                {metrics.tokens == null && '（按流式片段估算）'}
              </>
            )}
            {metrics.tokens != null && ` · 输出 ${metrics.tokens} tokens`}
          </div>
        )}
        <div className="quick-row">
          {QUICK_PROMPTS.map((prompt) => (
            <button
              key={prompt.key}
              type="button"
              className="quick-chip"
              onClick={() => {
                if (prompt.text.endsWith(' ')) setInput(prompt.text);
                else send(prompt.text);
              }}
            >
              {prompt.label}
            </button>
          ))}
        </div>
        {attachments.length > 0 && (
          <div className="attach-row">
            {attachments.map((att) => (
              <div key={att.uid} className="attach-chip">
                <FileOutlined className="attach-icon" />
                <div className="attach-meta">
                  <span className="attach-name" title={att.name}>
                    {att.name}
                  </span>
                  <span className="attach-ext">{fileExt(att.name)}</span>
                </div>
                <CloseOutlined
                  className="attach-remove"
                  onClick={() =>
                    setAttachments((prev) => prev.filter((a) => a.uid !== att.uid))
                  }
                />
              </div>
            ))}
          </div>
        )}
        <Sender
          value={input}
          onChange={setInput}
          onSubmit={send}
          loading={state === 'running'}
          prefix={
            <Upload
              multiple
              showUploadList={false}
              beforeUpload={(file) => {
                addFiles([file]);
                return false;
              }}
            >
              <Button type="text" icon={<PaperClipOutlined />} />
            </Upload>
          }
          placeholder="输入消息，可拖入文件作为资料附件"
          onCancel={() => api.interrupt(project.id).catch(() => {})}
        />
      </div>
    </div>
  );
}
