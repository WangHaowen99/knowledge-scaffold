import { Bubble, Sender } from '@ant-design/x';
import {
  CheckOutlined,
  CloseOutlined,
  DownOutlined,
  FileOutlined,
  LoadingOutlined,
  PaperClipOutlined,
} from '@ant-design/icons';
import { Alert, Button, Card, Input, Modal, Space, Tag, Typography, Upload, message } from 'antd';
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

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return '';
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
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
function QuestionCard({ req, resolved, onAnswer, trace = 0 }) {
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
    <Card
      size="small"
      className={`question-card${trace ? ' trace-card' : ''}`}
      title={trace ? '走查分支选择' : '助手向你提问'}
    >
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

export default function ChatPane({ project, onWorkspaceChanged, onPendingChange, registerSend }) {
  const [events, setEvents] = useState([]);
  const [live, setLive] = useState({ text: '', thinking: '' });
  const [input, setInput] = useState('');
  const [state, setState] = useState('idle');
  const [pending, setPending] = useState(null); // 乐观显示的用户消息
  const [metrics, setMetrics] = useState(null);
  const [attachments, setAttachments] = useState([]);
  const [uploads, setUploads] = useState([]); // 发送中的附件卡：{uid,name,size,percent,status}
  const [uploading, setUploading] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [queueInfo, setQueueInfo] = useState(null); // 会话池排队位次
  const [distill, setDistill] = useState(null); // 业务知识确认卡 {name, content}
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
    setQueueInfo(null);
    setUploads([]);
    const source = openEventStream(project.id, (event) => {
      if (event.type === 'queue_position') {
        setQueueInfo({ position: event.position, waiting: event.waiting });
        return;
      }
      if (event.type === 'session_started') setQueueInfo(null);
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
        setUploads([]); // 附件进度卡由历史里的正式消息接棒
        metricsRef.current = { sentTs: event.ts };
        setMetrics(null);
      }
      if (event.type === 'assistant_message' && !event.subagent) {
        liveRef.current = { text: '', thinking: '' };
        setLive({ text: '', thinking: '' });
      }
      if (event.type === 'result') {
        const m = metricsRef.current;
        const usage = event.usage || {};
        const outputTokens = usage.output_tokens;
        const cacheRead = usage.cache_read_input_tokens || 0;
        const inputTokens = (usage.input_tokens || 0) + cacheRead;
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
            cost: event.total_cost_usd ?? null,
            inputTokens: inputTokens || null,
            cacheRate: inputTokens ? cacheRead / inputTokens : null,
            done: true,
          });
        }
      }
      setEvents((prev) => [...prev, event]);
      if (event.type === 'session_state') {
        setState(event.state);
        if (event.state !== 'queued') setQueueInfo(null);
      }
      if (event.type === 'result') onWorkspaceChanged?.();
    });
    return () => source.close();
    // onWorkspaceChanged 变化不应重建 SSE 连接
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id]);

  useEffect(() => {
    const el = historyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events, live, pending, uploads]);

  const send = async (text) => {
    const trimmed = (text || '').trim();
    if (uploading) return;
    if (!trimmed && attachments.length === 0) return;
    const toUpload = attachments;
    setInput('');
    // 附件立即离开输入框，进入对话区变成带进度条的文件卡（并行上传）
    setAttachments([]);
    setPending({ text: trimmed || '（发送资料）' });
    if (toUpload.length) {
      setUploading(true);
      setUploads(
        toUpload.map((att) => ({
          uid: att.uid,
          name: att.name,
          size: att.file.size,
          percent: 0,
          status: 'uploading',
        })),
      );
    }
    const patchUpload = (uid, patch) =>
      setUploads((prev) => prev.map((u) => (u.uid === uid ? { ...u, ...patch } : u)));
    try {
      const results = await Promise.all(
        toUpload.map(async (att) => {
          try {
            const saved = await api.uploadResource(
              project.id,
              resourceSubdir(att.name),
              att.file,
              (ratio) => patchUpload(att.uid, { percent: Math.round(ratio * 100) }),
            );
            patchUpload(att.uid, { percent: 100, status: 'done' });
            return { att, path: saved.path };
          } catch (error) {
            patchUpload(att.uid, { status: 'error' });
            return { att, error };
          }
        }),
      );
      if (results.some((r) => r.path)) onWorkspaceChanged?.();
      const failed = results.filter((r) => r.error);
      if (failed.length) {
        // 失败的附件退回输入框待重试；成功的已入工作区，不重复上传
        setAttachments((prev) => [...failed.map((f) => f.att), ...prev]);
        setPending(null);
        setInput(trimmed);
        message.error(`${failed.length} 个文件上传失败：${failed[0].error.message}`);
        return;
      }
      const uploaded = results.map((r) => r.path);
      const prefix = uploaded.length
        ? `（我通过界面上传了资料，已存入工作区：${uploaded.join('、')}）\n`
        : '';
      const body = trimmed || '请查看我刚上传的资料，说明接下来可以怎么用。';
      await api.sendMessage(project.id, prefix + body);
      setState('running');
    } catch (error) {
      // 走到这里说明附件都已入工作区，只是消息未发出；不退回附件避免重复上传
      setPending(null);
      setUploads([]);
      setInput(trimmed);
      message.error(error.message);
    } finally {
      setUploading(false);
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

  // 挂卡死循环出口（方案风险#4）：明确取消该要求，指示助手不再重复发起
  const cancelRequirement = async (requestId) => {
    try {
      await api.respondPermission(
        project.id,
        requestId,
        'deny',
        '用户已明确取消该要求：请不要再次发起相同的确认或提问，改用其他方式继续，或跳过此步骤并说明。',
      );
      message.success('已取消该要求');
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
    const fingerprints = new Map(); // 相同确认要求跨轮重复计数（防挂卡死循环，方案风险#4）
    let traceCount = 0;
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
        const fingerprint = `${event.tool_name}:${JSON.stringify(event.tool_input ?? {})}`;
        const repeat = (fingerprints.get(fingerprint) || 0) + 1;
        fingerprints.set(fingerprint, repeat);
        const isQuestion = event.tool_name === 'AskUserQuestion';
        const isTrace =
          isQuestion &&
          /分支|走向|走查|下一步怎么走|场景推演/.test(
            JSON.stringify(event.tool_input?.questions ?? ''),
          );
        if (isTrace) traceCount += 1;
        list.push({
          key: event.seq,
          role: isQuestion ? 'question' : 'permission',
          content: event,
          resolved: resolvedPermissions.has(event.request_id),
          repeat,
          trace: isTrace ? traceCount : 0,
        });
      } else if (event.type === 'session_error') {
        // 旧历史里遗留的 SIGTERM（服务重启）报错不再展示
        if (/exit code[:\s]*143/.test(event.error || '')) continue;
        list.push({ key: event.seq, role: 'error', content: event.error });
      }
    }
    return list;
  }, [events, resolvedPermissions, toolResults]);

  const hasLive = live.text || live.thinking;

  // 顶栏"待处理"徽标：尚未答复的确认卡/提问卡数量
  const pendingPermissions = items.filter(
    (item) => (item.role === 'permission' || item.role === 'question') && !item.resolved,
  ).length;
  useEffect(() => {
    onPendingChange?.(pendingPermissions);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingPermissions]);

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
                  {item.trace > 0 && (
                    <div className="trace-badge">
                      场景走查 · 第 {item.trace} 个分支
                    </div>
                  )}
                  {!item.resolved && item.repeat > 1 && (
                    <div className="repeat-banner">
                      该问题已第 {item.repeat} 次出现。
                      <Typography.Link onClick={() => cancelRequirement(item.content.request_id)}>
                        取消该要求
                      </Typography.Link>
                    </div>
                  )}
                  <QuestionCard
                    req={item.content}
                    resolved={item.resolved}
                    trace={item.trace}
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
                  {!item.resolved && item.repeat > 1 && (
                    <div className="repeat-banner">
                      相同的确认已第 {item.repeat} 次出现，助手可能在原地打转。
                      <Typography.Link onClick={() => cancelRequirement(req.request_id)}>
                        取消该要求（不再询问）
                      </Typography.Link>
                    </div>
                  )}
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
                  closable
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
                className={isUser ? 'msg-in msg-right' : 'msg-soft msg-left-wrap assistant-row'}
              >
                <Bubble
                  placement={isUser ? 'end' : 'start'}
                  variant="filled"
                  styles={isUser ? USER_BUBBLE_STYLE : ASSISTANT_BUBBLE_STYLE}
                  content={isUser ? item.content : <Markdown content={item.content} />}
                />
                {!isUser && (
                  <button
                    type="button"
                    className="distill-btn"
                    title="把这条内容沉淀为业务知识"
                    onClick={() =>
                      setDistill({
                        name: item.content.split('\n')[0].replace(/^#+\s*/, '').slice(0, 40),
                        content: item.content,
                      })
                    }
                  >
                    沉淀知识
                  </button>
                )}
              </div>
            );
          })}
          {pending && (
            <div className="msg-in msg-right">
              <Bubble placement="end" variant="filled" styles={USER_BUBBLE_STYLE} content={pending.text} />
            </div>
          )}
          {uploads.length > 0 && (
            <div className="msg-right">
              <div className="upload-stack">
                {uploads.map((up) => (
                  <div key={up.uid} className={`upload-card ${up.status}`}>
                    <div className="upload-icon">
                      {up.status === 'done' ? (
                        <CheckOutlined />
                      ) : up.status === 'error' ? (
                        <CloseOutlined />
                      ) : (
                        <FileOutlined />
                      )}
                    </div>
                    <div className="upload-info">
                      <div className="upload-name" title={up.name}>
                        {up.name}
                      </div>
                      <div className="upload-track">
                        <div className="upload-fill" style={{ width: `${up.percent}%` }} />
                      </div>
                      <div className="upload-status">
                        {up.status === 'uploading' &&
                          `上传中 ${up.percent}% · ${formatSize(up.size)}`}
                        {up.status === 'done' && `已存入工作区 · ${formatSize(up.size)}`}
                        {up.status === 'error' && '上传失败，已退回输入框'}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
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
        {state === 'queued' && (
          <div className="queue-banner">
            当前建模任务较多
            {queueInfo ? `，您排在第 ${queueInfo.position} 位（共 ${queueInfo.waiting} 位等待）` : '，正在排队'}
            ；空出会话位后自动开始，点停止按钮可取消排队。
          </div>
        )}
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
            {metrics.done && metrics.cost != null && ` · $${metrics.cost.toFixed(3)}`}
            {metrics.done && metrics.inputTokens != null && (
              <>
                {' '}
                · 输入 {(metrics.inputTokens / 1000).toFixed(1)}k
                {metrics.cacheRate != null &&
                  `（缓存 ${Math.round(metrics.cacheRate * 100)}%）`}
              </>
            )}
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
          loading={state === 'running' || state === 'queued' || uploading}
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
          onCancel={() => {
            // 上传阶段还没有会话消息可打断，忽略停止按钮
            if (!uploading) api.interrupt(project.id).catch(() => {});
          }}
        />
      </div>
      <Modal
        title="沉淀为业务知识"
        open={!!distill}
        onCancel={() => setDistill(null)}
        okText="确认入库"
        onOk={async () => {
          if (!distill.name.trim()) {
            message.warning('请填写条目名称');
            return;
          }
          try {
            await api.createKnowledge(project.id, distill.name, distill.content);
            setDistill(null);
            onWorkspaceChanged?.();
            message.success('已入库（knowledge/），后续建模会自动应用');
          } catch (error) {
            message.error(error.message);
          }
        }}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          入库后保存在项目 knowledge/ 目录并记录版本；可在中栏「知识」页管理。
        </Typography.Paragraph>
        <Input
          placeholder="条目名称"
          value={distill?.name}
          onChange={(e) => setDistill({ ...distill, name: e.target.value })}
          style={{ marginBottom: 8 }}
        />
        <Input.TextArea
          autoSize={{ minRows: 5, maxRows: 14 }}
          value={distill?.content}
          onChange={(e) => setDistill({ ...distill, content: e.target.value })}
        />
      </Modal>
    </div>
  );
}
