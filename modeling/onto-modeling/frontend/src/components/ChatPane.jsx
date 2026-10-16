import { Bubble, Sender } from '@ant-design/x';
import {
  ArrowDownOutlined,
  CheckCircleFilled,
  CheckOutlined,
  CloseOutlined,
  DownOutlined,
  FileOutlined,
  LoadingOutlined,
  MoreOutlined,
  PaperClipOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Dropdown,
  Input,
  Modal,
  Popconfirm,
  Popover,
  Space,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, openEventStream } from '../lib/api.js';
import { orderResearchActions } from '../lib/researchGate.js';
import {
  EMPTY_CONTEXT,
  compactionNotice,
  describeContext,
  nextContext,
} from '../lib/contextUsage.js';
import {
  buildSubagentFlow,
  buildSubagentLifecycle,
  isSubagentCardMessage,
} from '../lib/subagentEvents.js';
import { FileChip, Markdown } from './CenterPane.jsx';
import ConversationSwitcher from './ConversationSwitcher.jsx';
import { STAGES, stageForCommand, stageHits } from './LeftPane.jsx';

// 「⋯ 更多」里的全量命令（希克定律：常显建议 ≤4，全集收进菜单）
const ALL_COMMANDS = [
  { key: 'start', label: '开始建模', text: '/ontology' },
  { key: 'research', label: '资料分析', text: '/ontology-research ' },
  { key: 'extract-object', label: '提取对象', text: '/ontology-extract-object' },
  { key: 'extract-operation', label: '提取操作', text: '/ontology-extract-operation' },
  { key: 'extract-rule-metric', label: '提取规则与指标', text: '/ontology-extract-rule-metric' },
  { key: 'validate', label: '质量检查', text: '/ontology-validate' },
  { key: 'mock', label: '生成示例数据', text: '/ontology-mock' },
  { key: 'trace', label: '场景验证', text: '/ontology-trace 全部' },
  { key: 'scan', label: '扫描脚本候选', text: '/ontology-codegen-scan' },
  { key: 'overview', label: '整体视图', text: '/ontology-overview' },
  { key: 'update', label: '增量更新', text: '/ontology-update ' },
];

const HISTORY_TAIL = 400;

function isVisibleChatEvent(event) {
  if (
    [
      'user_message',
      'workspace_changes',
      'session_interrupted',
      'permission_request',
      'context_compacted',
    ].includes(event.type)
  ) {
    return true;
  }
  if (event.type === 'session_error') return !/exit code[:\s]*143/.test(event.error || '');
  if (event.type !== 'assistant_message') return false;
  if (isSubagentCardMessage(event)) return false;
  return (event.blocks || []).some((block) => {
    if (block.kind === 'tool_use') return true;
    if (event.subagent) return false;
    return (
      (block.kind === 'text' || block.kind === 'thinking') &&
      Boolean(block.text?.trim())
    );
  });
}

function flattenTreePaths(nodes, out = []) {
  for (const node of nodes || []) {
    if (node.type === 'file') out.push(node.path);
    if (node.children) flattenTreePaths(node.children, out);
  }
  return out;
}

// 情境化"下一步建议"：优先级取首个主推，总数 ≤4（设计稿 §1 规则表）
function buildSuggestions({ paths, meta, versions, reportErrors, interrupted }) {
  if (interrupted) {
    return [
      { key: 'resume', primary: true, label: '继续刚才的任务', tooltip: '从打断处继续', command: '继续' },
      { key: 'redirect', label: '换个方向', tooltip: '清空输入，重新下达指令', fill: '' },
    ];
  }
  const out = [];
  if (reportErrors) {
    out.push({
      key: 'fix',
      primary: true,
      label: `修复报告错误项（${reportErrors.count}）`,
      tooltip: `检测到 ${reportErrors.path.split('/').pop()} 有 ${reportErrors.count} 个错误项`,
      command: `请阅读 ${reportErrors.path}，逐条修复其中列出的错误项，完成后重新运行校验并汇报结果`,
    });
    out.push({ key: 'revalidate', label: '重新校验', tooltip: '重新运行完整校验', command: '/ontology-validate' });
  } else if (
    (meta?.resource_updated_at || 0) > (versions?.[0]?.time || 0) &&
    // 需存在真实派生产物才谈"刷新"：目录占位 index.md 不算（新项目误报修复）
    paths.some(
      (p) =>
        (p.startsWith('research/') || p.startsWith('ontology/')) &&
        !p.endsWith('/') &&
        !p.endsWith('index.md'),
    )
  ) {
    out.push({
      key: 'refresh',
      primary: true,
      label: '刷新受影响产物',
      tooltip: '资料区有更新，派生产物可能已过期',
      command: '/ontology-update 资料区有新变更，请检查并刷新受影响的产物',
    });
  }
  // 评测失分或出现异常用例 → 主推演进（报告错误修复优先级更高，不并列主推）。
  // 判定在后端（modeling_eval.evolution_hint）：得分 <90、有异常用例、或轮次异常终止但已出报告
  const evolution = meta?.eval_evolution;
  if (!reportErrors && evolution) {
    out.push({
      key: 'evolution',
      primary: out.length === 0,
      label: evolution.errors
        ? `评测优化（${evolution.errors} 条异常）`
        : `评测优化（当前 ${evolution.score} 分）`,
      tooltip: '根据最近一轮评测的失分与异常用例归因并优化知识包',
      command: `/ontology-evolution ${evolution.report.split('/').pop()}`,
    });
  }
  // 下一步 = 第一个未完成阶段；用户已越过的可选阶段不再主推。
  // 资料分析未验收且对象尚未提取时不算完成，不会主推对象提取（issue #362）
  const researchReview = meta?.research_review || null;
  const hits = stageHits(paths, researchReview);
  const lastHit = hits.lastIndexOf(true);
  let nextIndex = -1;
  for (let i = 0; i < STAGES.length; i += 1) {
    if (hits[i]) continue;
    if (STAGES[i].optional && i < lastHit) continue;
    nextIndex = i;
    break;
  }
  const started = paths.some(
    (p) =>
      (p.startsWith('research/') || p.startsWith('ontology/')) &&
      !p.endsWith('/') &&
      !p.endsWith('/index.md'),
  );
  if (!started && !out.length) {
    out.push({ key: 'start', primary: true, label: '开始建模', tooltip: '进入引导式建模主链', command: '/ontology' });
  }
  if (nextIndex >= 0) {
    const next = STAGES[nextIndex];
    // 报告已生成但没通过验收时先核验，而不是先推重新调研
    const actions =
      next.key === 'research' ? orderResearchActions(next.actions, researchReview) : next.actions;
    for (const action of actions) {
      if (out.length >= 4) break;
      out.push({
        key: `st-${next.key}-${action.label}`,
        primary: !out.some((s) => s.primary),
        label: action.label,
        tooltip: `下一阶段：${next.title}`,
        command: action.command,
      });
    }
  } else if (out.length < 4) {
    out.push(
      { key: 'overview', primary: !out.some((s) => s.primary), label: '整体视图', tooltip: '主链已完成，查看整体报告', command: '/ontology-overview' },
      { key: 'scan', label: '扫描脚本候选', tooltip: '识别可代码化的操作', command: '/ontology-codegen-scan' },
    );
  }
  return out.slice(0, 4);
}

function relTime(ts) {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  const date = new Date(ts * 1000);
  const sameDay = date.toDateString() === new Date().toDateString();
  return sameDay
    ? date.toTimeString().slice(0, 5)
    : date.toLocaleString([], { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function absTime(ts) {
  return new Date(ts * 1000).toLocaleString();
}

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

// ---- 工具调用的用户视角呈现：中文动作 + 隐藏内部实现（.claude 资产、绝对路径）----

function sanitizeInternal(raw) {
  return String(raw || '')
    // 工作区绝对路径 → 项目相对路径
    .replace(/\/[^\s"']*workspaces\/[^/\s"']+\//g, '')
    // 内置提示词资产：.claude/**/name.ext → 内置·name（不暴露内部目录结构）
    .replace(
      /(?:~\/|\/[^\s"']*\/)?\.claude\/[\w./-]*?([\w-]+)\.(?:py|md|json|yaml|yml|html|template)/g,
      '内置·$1',
    )
    .replace(/(?:~\/|\/[^\s"']*\/)?\.claude\/?/g, '内置资产/')
    // 其余系统绝对路径只留文件名
    .replace(/\/(?:opt|data|usr|home|tmp|var)\/[\w./-]*\/([\w.-]+)/g, '$1');
}

const TOOL_VERBS = {
  Read: '读取文件',
  Write: '写入文件',
  Edit: '修改文件',
  MultiEdit: '修改文件',
  NotebookEdit: '修改笔记',
  Bash: '执行命令',
  BashOutput: '查看命令输出',
  KillShell: '终止命令',
  Grep: '搜索内容',
  Glob: '查找文件',
  LS: '浏览目录',
  WebSearch: '联网搜索',
  WebFetch: '读取网页',
  TodoWrite: '更新任务清单',
  Task: '派遣子任务',
  Agent: '派遣子任务',
  ExitPlanMode: '提交计划',
  AskUserQuestion: '征询确认',
};

function presentTool(block) {
  const name = block.tool || '';
  const input = block.input || {};
  let verb = TOOL_VERBS[name];
  if (!verb && name.startsWith('mcp__')) verb = `调用 ${name.split('__')[1] || 'MCP'} 服务`;
  if (!verb) verb = name || '执行操作';

  let raw;
  if (name === 'TodoWrite') raw = '';
  else if (name === 'Bash') raw = input.description || input.command || '';
  else
    raw =
      input.file_path ||
      input.path ||
      input.pattern ||
      input.command ||
      input.description ||
      input.url ||
      input.query ||
      '';
  let summary = sanitizeInternal(typeof raw === 'string' ? raw : JSON.stringify(raw));
  // 读写内置资产时不展示伪装成项目文件的路径，直接说明语义
  if (['Read', 'Write', 'Edit', 'MultiEdit'].includes(name) && summary.startsWith('内置·')) {
    verb = name === 'Read' ? '查阅内置规范' : '更新内置资产';
    summary = summary.slice('内置·'.length);
  }
  if (summary.length > 64) summary = `${summary.slice(0, 64)}…`;
  return { verb, summary };
}

function formatSubagentDuration(durationMs) {
  if (durationMs == null) return '';
  const seconds = Number(durationMs) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${Math.round(seconds % 60)} 秒`;
}

// 子代理步骤 → ToolLine 契约（复用主对话工具行的中文动作与详情展开）
function subagentToolLine(step) {
  const shown = presentTool({ tool: step.tool, input: step.input || {} });
  const input = step.input && Object.keys(step.input).length ? step.input : null;
  return {
    key: step.key,
    tool: shown.verb,
    summary: shown.summary,
    status: step.status == null ? undefined : step.status === 'error',
    excerpt: step.excerpt,
    input,
  };
}

function ThinkingBlock({ text }) {
  return (
    <details className="thinking-block">
      <summary>思考过程</summary>
      <div className="thinking-body">{text}</div>
    </details>
  );
}

function ToolLine({ tool, expanded, onToggle }) {
  const hasDetail = tool.input != null || tool.excerpt;
  return (
    <div className="tool-item">
      <div
        className={`tool-line${hasDetail ? ' expandable' : ''}`}
        onClick={(e) => {
          e.stopPropagation();
          if (hasDetail) onToggle?.();
        }}
      >
        {tool.status === undefined ? (
          <LoadingOutlined className="tool-status running" />
        ) : tool.status ? (
          <CloseOutlined className="tool-status" />
        ) : (
          <CheckOutlined className="tool-status ok" />
        )}
        {tool.sub && <span className="tool-sub-badge">子代理</span>}
        <span className="tool-name">{tool.tool}</span>
        {tool.summary && <span className="tool-arg">{tool.summary}</span>}
        {hasDetail && <DownOutlined className={`tool-caret${expanded ? ' open' : ''}`} />}
      </div>
      {expanded && (
        <div className="tool-detail msg-soft" onClick={(e) => e.stopPropagation()}>
          {tool.input != null && (
            <div className="tool-detail-block">
              <div className="tool-detail-label">输入</div>
              <pre>{sanitizeInternal(JSON.stringify(tool.input, null, 2)).slice(0, 800)}</pre>
            </div>
          )}
          {tool.excerpt && (
            <div className="tool-detail-block">
              <div className="tool-detail-label">结果摘录</div>
              <pre>{sanitizeInternal(tool.excerpt)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// 连续的工具调用收进一个可展开的执行组，避免长任务刷屏；组内详情手风琴（一次一行）
function ToolGroup({ tools }) {
  const [open, setOpen] = useState(false);
  const [expandedKey, setExpandedKey] = useState(null);
  const running = tools.some((t) => t.status === undefined);
  const latest = tools[tools.length - 1];

  return (
    <div className={`tool-group${running ? ' running' : ''}`}>
      <div className="tool-group-header" onClick={() => setOpen(!open)}>
        {running ? (
          <LoadingOutlined className="tool-status running" />
        ) : (
          <CheckOutlined className="tool-status ok" />
        )}
        <span className="tool-group-title">
          {running ? '执行中' : '执行完成'} · {tools.length} 步
        </span>
        <DownOutlined className={`tool-group-caret${open ? ' open' : ''}`} />
      </div>
      {open ? (
        <div className="tool-group-body">
          {tools.map((tool) => (
            <ToolLine
              key={tool.key}
              tool={tool}
              expanded={expandedKey === tool.key}
              onToggle={() => setExpandedKey(expandedKey === tool.key ? null : tool.key)}
            />
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

// 子任务卡：默认折叠，展开后是这个子代理自己的实时步骤流。
// 展开状态是卡片局部的，派发多个子代理时各自独立。
function SubagentCard({ item }) {
  const [open, setOpen] = useState(false);
  const [expandedKey, setExpandedKey] = useState(null);
  const steps = item.steps || [];
  const stopped = item.stopped && !item.failed;
  const stateText = item.done
    ? item.failed
      ? '失败'
      : stopped
        ? '已停止'
        : '完成'
    : '运行中';
  const duration = item.done ? formatSubagentDuration(item.durationMs) : '';
  const hasDetail = steps.length > 0 || !!item.excerpt;

  let preview = '';
  if (item.done) {
    preview = item.summary || '';
  } else {
    const latest = steps[steps.length - 1];
    if (!latest) preview = '已启动，等待子代理反馈';
    else if (latest.kind === 'text')
      preview = latest.text.length > 60 ? `${latest.text.slice(0, 60)}…` : latest.text;
    else {
      const shown = presentTool({ tool: latest.tool, input: latest.input || {} });
      preview = `正在${shown.verb}${shown.summary ? ` ${shown.summary}` : ''}`;
    }
  }

  return (
    <div
      className={`subagent-card${
        item.done ? (item.failed ? ' failed' : stopped ? ' stopped' : ' done') : ' running'
      }`}
    >
      <div className="subagent-head">
        <span className="subagent-icon">
          {item.done ? (
            item.failed || stopped ? (
              <CloseOutlined />
            ) : (
              <CheckOutlined />
            )
          ) : (
            <LoadingOutlined />
          )}
        </span>
        <span className="subagent-main">
          <span className="subagent-title">
            子任务{stateText} · {item.description}
            {duration && ` · 耗时 ${duration}`}
          </span>
          <span className="subagent-sub">
            {item.agentType && <span className="subagent-type">{item.agentType}</span>}
            {preview && <span className="subagent-hint">{preview}</span>}
          </span>
        </span>
        {hasDetail && (
          <button
            type="button"
            className="subagent-toggle"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            {open ? '收起' : '展开'}
            <DownOutlined className={`subagent-caret${open ? ' open' : ''}`} />
          </button>
        )}
      </div>
      {open && hasDetail && (
        <div className="subagent-steps">
          {steps.map((step) =>
            step.kind === 'text' ? (
              <div key={step.key} className="subagent-say">
                {sanitizeInternal(step.text)}
              </div>
            ) : (
              <ToolLine
                key={step.key}
                tool={subagentToolLine(step)}
                expanded={expandedKey === step.key}
                onToggle={() => setExpandedKey(expandedKey === step.key ? null : step.key)}
              />
            ),
          )}
          {item.excerpt && (
            <div className="subagent-result">
              <div className="tool-detail-label">结果摘录</div>
              <pre>{sanitizeInternal(item.excerpt)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// AskUserQuestion 专用卡：渲染问题与选项，答案经 answers 回填给工具
function QuestionCard({ req, resolved, onAnswer, trace = 0 }) {
  const questions = req.tool_input?.questions || [];
  const [chosen, setChosen] = useState({});
  const [custom, setCustom] = useState('');
  const [submitted, setSubmitted] = useState(false); // 乐观：等确认事件期间即时反馈
  const multi = questions.length > 1 || questions.some((q) => q.multiSelect);

  const submit = (answers) => {
    setSubmitted(true);
    Promise.resolve(onAnswer(req.request_id, answers)).catch(() => setSubmitted(false));
  };

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
    <div className={`question-card${trace ? ' trace-card' : ''}${resolved ? ' resolved' : ''}`}>
      <div className="qc-head">
        <span className="qc-glyph">?</span>
        <span className="qc-title">{trace ? '走查分支选择' : '请专家确认'}</span>
        {resolved ? (
          <span className="qc-state done">
            <CheckCircleFilled /> 已答复
          </span>
        ) : submitted ? (
          <span className="qc-state done">
            <CheckCircleFilled /> 已提交
          </span>
        ) : (
          <span className="qc-state live">
            <span className="qc-dot" />
            等待你选择
          </span>
        )}
      </div>
      {questions.map((q) => (
        <div key={q.question} className="question-item">
          <div className="question-text">
            {q.header && <span className="qc-topic">{q.header}</span>}
            <span>{q.question}</span>
          </div>
          <div className="question-options" role="radiogroup" aria-label={q.question}>
            {(q.options || []).map((opt, index) => {
              const selected = (chosen[q.question] || '').split(',').includes(opt.label);
              return (
                <button
                  type="button"
                  key={opt.label}
                  role={q.multiSelect ? 'checkbox' : 'radio'}
                  aria-checked={selected}
                  className={`question-option${selected ? ' selected' : ''}`}
                  disabled={resolved}
                  style={{ animationDelay: `${Math.min(index * 45, 220)}ms` }}
                  onClick={() => choose(q, opt.label)}
                >
                  <span className="option-index">{index + 1}</span>
                  <span className="option-main">
                    <span className="option-label">{opt.label}</span>
                    {opt.description && <span className="option-desc">{opt.description}</span>}
                  </span>
                  <span className="option-check">
                    <CheckOutlined />
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      ))}
      {!resolved && (
        <div className="qc-custom">
          <Input
            placeholder="或输入自定义回答…"
            variant="borderless"
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            onPressEnter={() => {
              if (custom.trim() && questions[0]) {
                submit({ [questions[0].question]: custom.trim() });
              }
            }}
          />
          {multi ? (
            <Button size="small" type="primary" shape="round" disabled={!allAnswered} onClick={() => submit(chosen)}>
              提交
            </Button>
          ) : (
            <Button
              size="small"
              type="primary"
              shape="round"
              disabled={!custom.trim() && !questions.every((q) => chosen[q.question])}
              onClick={() => {
                if (custom.trim()) {
                  submit({ [questions[0]?.question]: custom.trim() });
                } else {
                  submit(chosen);
                }
              }}
            >
              发送
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

// 存量历史里 git 八进制转义的中文路径（core.quotepath 修复前落盘）解码兜底
function decodeGitPath(path) {
  if (!/\\\d{3}/.test(path)) return path.replace(/^"|"$/g, '');
  try {
    const bytes = [];
    const encoder = new TextEncoder();
    let i = 0;
    while (i < path.length) {
      if (path[i] === '\\' && /^\d{3}$/.test(path.slice(i + 1, i + 4))) {
        bytes.push(parseInt(path.slice(i + 1, i + 4), 8));
        i += 4;
      } else {
        bytes.push(...encoder.encode(path[i]));
        i += 1;
      }
    }
    return new TextDecoder().decode(new Uint8Array(bytes)).replace(/^"|"$/g, '');
  } catch {
    return path;
  }
}

// 每轮产物变更卡：让"助手改了什么"零成本可见（设计稿 §3）
function ChangesCard({ added, modified, deleted, total, knownPaths, onOpenFile, onOpenDiff }) {
  const [expanded, setExpanded] = useState(false);
  const entries = [
    ...added.map((p) => ({ path: decodeGitPath(p), status: 'added' })),
    ...modified.map((p) => ({ path: decodeGitPath(p), status: 'modified' })),
    ...deleted.map((p) => ({ path: decodeGitPath(p), status: 'deleted' })),
  ];
  const shown = expanded ? entries : entries.slice(0, 3);
  return (
    <div className="changes-card msg-soft">
      <div className="changes-head">
        <span className="changes-title">
          本轮产物变更
          {added.length > 0 && ` · 新增 ${added.length}`}
          {modified.length > 0 && ` · 修改 ${modified.length}`}
          {deleted.length > 0 && ` · 删除 ${deleted.length}`}
        </span>
        <button type="button" className="changes-diff" onClick={onOpenDiff}>
          查看对比
        </button>
      </div>
      <div className="changes-files">
        {shown.map((entry) => (
          <FileChip
            key={entry.path}
            path={entry.path}
            status={entry.status}
            exists={entry.status !== 'deleted' && (!knownPaths || knownPaths.has(entry.path))}
            onOpen={onOpenFile}
          />
        ))}
        {entries.length > 3 && (
          <button
            type="button"
            className="changes-more"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? '收起' : `展开全部（${total}）`}
          </button>
        )}
      </div>
    </div>
  );
}

// 复制按钮：自身切换"已复制"1.6s，不弹 toast 打扰
function CopyOpButton({ text }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef(0);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          clearTimeout(timerRef.current);
          timerRef.current = setTimeout(() => setCopied(false), 1600);
        });
      }}
    >
      {copied ? '✓ 已复制' : '复制'}
    </button>
  );
}

export default function ChatPane({
  project,
  initialFiles,
  onInitialFilesConsumed,
  tree,
  meta,
  versions,
  usage,
  onWorkspaceChanged,
  onPendingChange,
  onOpenFile,
  onOpenDiff,
  registerSend,
  onActiveStage,
}) {
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
  const [newCount, setNewCount] = useState(0); // 未读新消息（滚动锚定用）
  const [skippedEvents, setSkippedEvents] = useState(0); // 历史截断：跳过的较早事件数
  const [historyEpoch, setHistoryEpoch] = useState(0); // 触发全量重放
  // 用户选中的会话；按项目区分，切项目时回到服务端默认（null = 引擎绑定的或最近更新的）
  const [selection, setSelection] = useState({ projectId: project.id, id: null });
  const requestedConversation = selection.projectId === project.id ? selection.id : null;
  const [conversationId, setConversationId] = useState(null); // 服务端解析后的当前会话
  const conversationIdRef = useRef(null); // send() 等异步流程读最新值
  const [conversationsEpoch, setConversationsEpoch] = useState(0); // 发消息后刷新会话标题
  const fullHistoryRef = useRef(false);
  const viewKeyRef = useRef('');
  const historyEndRef = useRef(null);
  const pendingNewCountRef = useRef(0);
  const [ctx, setCtx] = useState(EMPTY_CONTEXT); // 上下文用量：已用/窗口/压缩触发点
  const [reportErrors, setReportErrors] = useState(null); // 最新校验报告的错误项统计
  const historyRef = useRef(null);
  const seenRef = useRef(new Set());
  const uidRef = useRef(0);
  const liveRef = useRef({ text: '', thinking: '' });
  const metricsRef = useRef(null);
  const rafRef = useRef(0);
  const nearBottomRef = useRef(true);
  const recallIndexRef = useRef(-1); // ↑ 召回历史输入的位置

  const paths = useMemo(() => flattenTreePaths(tree), [tree]);
  const knownPaths = useMemo(() => new Set(paths), [paths]);

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
      // 图片附件（含粘贴截图）显示缩略图而非类型图标（识别优于回忆）
      preview: file.type?.startsWith('image/') ? URL.createObjectURL(file) : null,
    }));
    if (next.length) setAttachments((prev) => [...prev, ...next]);
  };

  // 粘贴截图直接进附件（业务用户贴流程/表格截图高频）
  const onPaste = (event) => {
    const images = [...(event.clipboardData?.items || [])]
      .filter((item) => item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (!images.length) return; // 非图片剪贴板不拦截默认粘贴
    event.preventDefault();
    const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
    addFiles(
      images.map(
        (file, index) =>
          new File([file], `screenshot-${stamp}${index ? `-${index}` : ''}.png`, {
            type: file.type,
          }),
      ),
    );
  };

  const removeAttachment = (uid) => {
    setAttachments((prev) => {
      const target = prev.find((a) => a.uid === uid);
      if (target?.preview) URL.revokeObjectURL(target.preview);
      return prev.filter((a) => a.uid !== uid);
    });
  };

  // 最新校验/追踪报告的错误项统计（动态建议的数据源；codegen 候选报告除外）
  useEffect(() => {
    const reports = paths
      .filter((p) => p.startsWith('reports/') && p.endsWith('.md') && !p.includes('codegen-candidates'))
      .sort();
    const latest = reports[reports.length - 1];
    if (!latest) {
      setReportErrors(null);
      return undefined;
    }
    let active = true;
    api
      .reportItems(project.id, latest)
      .then((data) => {
        if (!active) return;
        const count = (data.items || []).filter((i) => i.severity === 'error').length;
        setReportErrors(count > 0 ? { path: latest, count } : null);
      })
      .catch(() => active && setReportErrors(null));
    return () => {
      active = false;
    };
  }, [project.id, paths]);

  useEffect(() => {
    const viewKey = `${project.id}/${requestedConversation || ''}`;
    if (viewKeyRef.current !== viewKey) {
      // 换了项目或会话：上一条会话的状态与指标都不再适用（上下文用量随历史回放重建）
      viewKeyRef.current = viewKey;
      fullHistoryRef.current = false;
      conversationIdRef.current = requestedConversation;
      setConversationId(requestedConversation);
      setState('idle');
      setLocalResolved(new Set());
      metricsRef.current = null;
      recallIndexRef.current = -1;
    }
    let resolved = requestedConversation;
    // 选中的会话已被删除（如在另一个标签页）：回到服务端默认会话
    const fallBackIfMissing = (error) => {
      if (error?.status !== 404 || !requestedConversation) return false;
      setSelection({ projectId: project.id, id: null });
      return true;
    };
    setEvents([]);
    setLive({ text: '', thinking: '' });
    liveRef.current = { text: '', thinking: '' };
    setPending(null);
    setMetrics(null);
    setCtx(EMPTY_CONTEXT); // 由历史回放重建
    seenRef.current = new Set();
    setQueueInfo(null);
    setUploads([]);
    setSkippedEvents(0);
    setNewCount(0);
    historyEndRef.current = null;
    pendingNewCountRef.current = 0;
    nearBottomRef.current = true;
    let source = null;
    let cancelled = false;
    let retryTimer = 0;
    let retryDelay = 2000;
    const lastMsgTsRef = { current: Date.now() };
    const nextAfterRef = { current: 0 };
    const handleEvent = (event) => {
      if (cancelled) return;
      lastMsgTsRef.current = Date.now();
      if (event.type === 'ping') return; // 心跳仅用于连接活性判断
      if (event.seq !== undefined) {
        nextAfterRef.current = Math.max(nextAfterRef.current, event.seq + 1);
      }
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
      const isHistoryEvent =
        event.seq !== undefined &&
        historyEndRef.current !== null &&
        event.seq < historyEndRef.current;
      if (event.type === 'system') return;
      setCtx((prev) => nextContext(prev, event));
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
            inputTokens: inputTokens || null,
            cacheRate: inputTokens ? cacheRead / inputTokens : null,
            done: true,
          });
        }
      }
      if (!isHistoryEvent && isVisibleChatEvent(event)) {
        pendingNewCountRef.current += 1;
      }
      setEvents((prev) => [...prev, event]);
      if (event.type === 'session_state') {
        setState(event.state);
        if (event.state !== 'queued') setQueueInfo(null);
      }
      if (event.type === 'result') onWorkspaceChanged?.();
    };
    // SSE 断线自愈：URL 内嵌的 access token 只有约 15 分钟有效期，浏览器
    // 自动重连一旦撞上过期 token（401）EventSource 就永久关闭。这里统一
    // 接管重连：先走一次 REST（401 会自动刷新 token）再按已收水位续传。
    const connect = (after) => {
      if (cancelled) return;
      source?.close();
      source = openEventStream(project.id, handleEvent, after, resolved);
      source.onopen = () => {
        retryDelay = 2000;
        lastMsgTsRef.current = Date.now();
      };
      source.onerror = () => {
        if (cancelled) return;
        // CONNECTING 是浏览器内建重试（同 URL）；CLOSED 才需要换 token 重建
        if (source.readyState === EventSource.CLOSED) scheduleReconnect();
      };
    };
    const scheduleReconnect = () => {
      if (cancelled) return;
      clearTimeout(retryTimer);
      retryTimer = setTimeout(async () => {
        if (cancelled) return;
        try {
          await api.sessionState(project.id, resolved); // request() 遇 401 会刷新 token
        } catch (error) {
          if (fallBackIfMissing(error)) return;
          /* 服务暂不可达，下轮再试 */
        }
        connect(nextAfterRef.current);
      }, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    };

    api
      .sessionState(project.id, requestedConversation)
      .then((info) => {
        if (cancelled) return;
        resolved = info.conversation_id || requestedConversation;
        conversationIdRef.current = resolved;
        setConversationId(resolved);
        setState(info.state || 'idle');
        const total = info.events || 0;
        historyEndRef.current = total;
        const historyStart = Number.isInteger(info.history_start)
          ? info.history_start
          : Math.max(0, total - HISTORY_TAIL);
        const skip = fullHistoryRef.current ? 0 : Math.max(0, historyStart);
        setSkippedEvents(skip);
        nextAfterRef.current = skip;
        connect(skip);
      })
      .catch((error) => {
        if (cancelled || fallBackIfMissing(error)) return;
        historyEndRef.current = 0;
        connect(0);
      });

    // 看门狗：后端每 15s 发一次 ping，40s 静默视为连接僵死（半开），强制重建
    const watchdog = setInterval(() => {
      if (cancelled) return;
      if (Date.now() - lastMsgTsRef.current > 40000) {
        lastMsgTsRef.current = Date.now();
        scheduleReconnect();
      }
    }, 10000);

    return () => {
      cancelled = true;
      clearTimeout(retryTimer);
      clearInterval(watchdog);
      source?.close();
    };
    // onWorkspaceChanged 变化不应重建 SSE 连接
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id, requestedConversation, historyEpoch]);

  // 滚动锚定：贴底（<80px）才自动滚；上翻阅读时不打扰，浮出"新消息"按钮
  const reducedMotion =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const scrollToBottom = (smooth = true) => {
    const el = historyRef.current;
    if (el) {
      el.scrollTo({
        top: el.scrollHeight,
        behavior: smooth && !reducedMotion ? 'smooth' : 'auto',
      });
    }
    setNewCount(0);
  };
  const onHistoryScroll = () => {
    const el = historyRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    nearBottomRef.current = near;
    if (near) setNewCount(0);
  };
  useEffect(() => {
    if (nearBottomRef.current) scrollToBottom(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, pending, uploads]);
  const eventCount = events.length;
  useEffect(() => {
    const count = pendingNewCountRef.current;
    pendingNewCountRef.current = 0;
    if (count === 0) return;
    if (nearBottomRef.current) scrollToBottom(false);
    else setNewCount((current) => current + count);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventCount]);

  const send = async (text, explicitAttachments = null) => {
    const trimmed = (text || '').trim();
    if (uploading) return;
    const toUpload = explicitAttachments ?? attachments;
    if (!trimmed && toUpload.length === 0) return;
    // 上传可能持续数秒：消息发往点发送时所在的会话
    const targetConversation = conversationIdRef.current;
    setInput('');
    recallIndexRef.current = -1;
    // 附件立即离开输入框，进入对话区变成带进度条的文件卡（并行上传）
    setAttachments([]);
    for (const att of toUpload) if (att.preview) URL.revokeObjectURL(att.preview);
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
              att.purpose,
              att.readingMode,
            );
            patchUpload(att.uid, { percent: 100, status: 'done' });
            return { att, path: saved.path };
          } catch (error) {
            patchUpload(att.uid, { status: 'error', error: error.message });
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
      await api.sendMessage(project.id, prefix + body, targetConversation);
      setState('running');
      setConversationsEpoch((epoch) => epoch + 1);
    } catch (error) {
      // 走到这里说明附件都已入工作区，只是消息未发出；不退回附件避免重复上传
      // 409：引擎正被另一条会话占用；404：会话已被删除，回到默认会话
      setPending(null);
      setUploads([]);
      setInput(trimmed);
      message.error(error.message);
      if (error.status === 404 && targetConversation) {
        setSelection({ projectId: project.id, id: null });
      }
    } finally {
      setUploading(false);
    }
  };

  useEffect(() => {
    if (!initialFiles?.length) return;
    const atts = initialFiles.map((f, i) => ({
      uid: `init-${Date.now()}-${i}`,
      name: f.name,
      file: f.file,
      preview: null,
      purpose: f.purpose,
      readingMode: f.readingMode,
    }));
    onInitialFilesConsumed?.();
    void send('这是本项目的初始领域资料，请接收并简要浏览，等待我的下一步指令。', atts);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFiles]);

  // 外部入口（左栏命令等）只注册一次，经 ref 调最新的 send，避免读到旧的上传态与附件
  const sendRef = useRef(send);
  sendRef.current = send;
  useEffect(() => {
    registerSend?.((command) => {
      if (command.endsWith(' ')) setInput(command);
      else sendRef.current(command);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id]);

  const [localResolved, setLocalResolved] = useState(() => new Set());

  const respondPermission = async (requestId, behavior, answers = null) => {
    try {
      await api.respondPermission(project.id, requestId, behavior, '', answers);
    } catch (error) {
      // 会话重启后的失效确认卡：答案降级为普通消息续接（resume 会自动接上上下文）
      if ((error.status === 410 || error.status === 404) && behavior === 'allow' && answers) {
        setLocalResolved((prev) => new Set(prev).add(requestId));
        const text = Object.entries(answers)
          .map(([question, answer]) => `${question}\n我的选择：${answer}`)
          .join('\n\n');
        message.info('该确认来自上次会话，已把你的选择作为消息发给助手继续');
        await send(text);
        return;
      }
      if (error.status === 410 || error.status === 404) {
        setLocalResolved((prev) => new Set(prev).add(requestId));
        message.info('该确认已失效（会话已重启），如需继续请直接发消息');
        return;
      }
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
      new Set([
        ...events.filter((e) => e.type === 'permission_resolved').map((e) => e.request_id),
        ...localResolved,
      ]),
    [events, localResolved],
  );

  // tool_use_id → {isError, excerpt}，用于工具行状态与详情展开
  const toolResults = useMemo(() => {
    const map = new Map();
    for (const event of events) {
      if (event.type === 'tool_results') {
        for (const block of event.blocks) {
          map.set(block.tool_use_id, {
            isError: block.is_error,
            excerpt: block.excerpt || '',
          });
        }
      }
    }
    return map;
  }, [events]);

  // 子代理生命周期（开始/终态）与实时步骤流：归并规则在 lib/subagentEvents.js
  const subagentLifecycle = useMemo(() => buildSubagentLifecycle(events), [events]);
  const subagentFlow = useMemo(() => buildSubagentFlow(events, toolResults), [events, toolResults]);

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
        list.push({ key: event.seq, role: 'user', content: event.text, ts: event.ts });
      } else if (event.type === 'assistant_message') {
        // 已归到子任务卡的子代理消息不再进主对话流
        if (subagentFlow.cards.has(event.parent_tool_use_id)) continue;
        for (const [index, block] of event.blocks.entries()) {
          const key = `${event.seq}-${index}`;
          if (block.kind === 'tool_use') {
            const result = toolResults.get(block.tool_use_id);
            if (['Task', 'Agent'].includes(block.tool) && !event.subagent) {
              // 子代理派遣：独立卡片呈现运行状态与可展开的执行过程
              const progress = subagentLifecycle.get(block.tool_use_id);
              const terminalStatus = progress?.status;
              list.push({
                key,
                role: 'subagent',
                description: progress?.description || block.input?.description || '子任务',
                agentType: block.input?.subagent_type || progress?.agentType || '',
                done: result !== undefined || progress?.done === true,
                failed: result?.isError || terminalStatus === 'failed',
                stopped: terminalStatus === 'stopped' || terminalStatus === 'killed',
                excerpt: result?.excerpt,
                summary: progress?.summary || '',
                durationMs: progress?.durationMs ?? null,
                steps: subagentFlow.steps.get(block.tool_use_id) || [],
              });
              continue;
            }
            const shown = presentTool(block);
            pushTool({
              key,
              tool: shown.verb,
              summary: shown.summary,
              status: result?.isError,
              excerpt: result?.excerpt,
              input: block.input,
              sub: !!event.subagent,
            });
          } else if (event.subagent) {
            continue;
          } else if (block.kind === 'text') {
            list.push({ key, role: 'assistant', content: block.text, ts: event.ts });
          } else if (block.kind === 'thinking' && block.text?.trim()) {
            list.push({ key, role: 'thinking', content: block.text });
          }
        }
      } else if (event.type === 'workspace_changes') {
        list.push({ key: event.seq, role: 'changes', content: event });
      } else if (event.type === 'session_interrupted') {
        list.push({ key: event.seq, role: 'system', content: '已打断' });
      } else if (event.type === 'context_compacted') {
        list.push({ key: event.seq, role: 'system', content: compactionNotice(event) });
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
  }, [events, resolvedPermissions, subagentFlow, subagentLifecycle, toolResults]);

  const hasLive = live.text || live.thinking;

  // 打断后且尚未发新消息 → 建议区给出续接出口
  const interrupted = useMemo(() => {
    if (state === 'running' || state === 'queued') return false;
    let lastInterrupt = -1;
    let lastUser = -1;
    events.forEach((event, index) => {
      if (event.type === 'session_interrupted') lastInterrupt = index;
      if (event.type === 'user_message') lastUser = index;
    });
    return lastInterrupt > lastUser;
  }, [events, state]);

  const suggestions = useMemo(
    () => buildSuggestions({ paths, meta, versions, reportErrors, interrupted }),
    [paths, meta, versions, reportErrors, interrupted],
  );

  // 正在执行的 /ontology-* 命令 → 左栏对应阶段进入"进行中"动效
  useEffect(() => {
    if (!onActiveStage) return;
    if (state !== 'running' && state !== 'queued') {
      onActiveStage(null);
      return;
    }
    let lastUserText = '';
    for (let i = events.length - 1; i >= 0; i -= 1) {
      if (events[i].type === 'user_message') {
        lastUserText = events[i].text || '';
        break;
      }
    }
    onActiveStage(stageForCommand(lastUserText));
  }, [state, events, onActiveStage]);

  // 会话状态（右栏头部常显）：空闲/生成中/执行工具/排队/出错
  const lastItem = items[items.length - 1];
  const toolRunning =
    lastItem?.role === 'toolgroup' && lastItem.tools.some((t) => t.status === undefined);
  const statusInfo =
    state === 'queued'
      ? { cls: 'queued', text: queueInfo ? `排队第 ${queueInfo.position} 位` : '排队中' }
      : state === 'running'
        ? toolRunning
          ? { cls: 'running', text: '执行工具' }
          : { cls: 'running breathing', text: '生成中' }
        : state === 'error'
          ? { cls: 'error', text: '出错了' }
          : { cls: 'idle', text: '空闲' };
  const ctxInfo = describeContext(ctx);
  // 运行、排队、上传或消息未落定时不能换会话：引擎同一时刻只绑定一条会话
  const conversationBusy = state === 'running' || state === 'queued' || uploading || Boolean(pending);

  // ↑ 键召回历史输入（仅空输入框且非运行中，不抢正常光标行为）
  const userMessages = useMemo(
    () => items.filter((i) => i.role === 'user').map((i) => i.content),
    [items],
  );
  const onRecallKey = (event) => {
    if (state === 'running' || state === 'queued') return;
    if (event.key === 'Escape' && recallIndexRef.current !== -1) {
      recallIndexRef.current = -1;
      setInput('');
      return;
    }
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    if (event.key === 'ArrowUp' && input !== '' && recallIndexRef.current === -1) return;
    if (!userMessages.length) return;
    event.preventDefault();
    let index = recallIndexRef.current;
    if (event.key === 'ArrowUp') {
      index = index === -1 ? userMessages.length - 1 : Math.max(0, index - 1);
    } else {
      if (index === -1) return;
      index += 1;
      if (index >= userMessages.length) {
        recallIndexRef.current = -1;
        setInput('');
        return;
      }
    }
    recallIndexRef.current = index;
    setInput(userMessages[index]);
  };


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
      onPaste={onPaste}
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
      <div className="chat-header">
        <span className={`status-dot ${statusInfo.cls}`} />
        <span className="status-text">{statusInfo.text}</span>
        <ConversationSwitcher
          projectId={project.id}
          conversationId={conversationId}
          busy={conversationBusy}
          refreshKey={conversationsEpoch}
          onSwitch={(id) => setSelection({ projectId: project.id, id })}
        />
        <span style={{ flex: 1 }} />
        {usage && usage.calls > 0 && (
          <Popover
            placement="bottomRight"
            content={
              <div className="usage-pop">
                <div>调用 {usage.calls} 次</div>
                <div>输入 {(usage.input_tokens / 1000).toFixed(1)}k tokens</div>
                <div>输出 {(usage.output_tokens / 1000).toFixed(1)}k tokens</div>
                {metrics?.done && metrics.cacheRate != null && (
                  <div>最近一轮缓存命中 {Math.round(metrics.cacheRate * 100)}%</div>
                )}
              </div>
            }
          >
            <span className="chat-cost">
              {(usage.output_tokens / 1000).toFixed(1)}k tok
            </span>
          </Popover>
        )}
        {ctxInfo && (
          <Tooltip title={ctxInfo.text}>
            <svg className={`ctx-ring${ctxInfo.level ? ` ${ctxInfo.level}` : ''}`} viewBox="0 0 20 20">
              <circle className="ctx-ring-bg" cx="10" cy="10" r="8" />
              <circle
                className="ctx-ring-fg"
                cx="10"
                cy="10"
                r="8"
                strokeDasharray={`${(ctxInfo.pct / 100) * 50.27} 50.27`}
                transform="rotate(-90 10 10)"
              />
            </svg>
          </Tooltip>
        )}
        <Popconfirm
          title="重启会话？"
          description="对话记录保留，按最新配置重连模型；稳定引擎会清空模型上下文。要从空白上下文开始，请新建会话"
          onConfirm={async () => {
            await api.closeSession(project.id, conversationIdRef.current).catch(() => {});
            setCtx(EMPTY_CONTEXT);
            message.success('会话已重置，下次发消息生效');
          }}
        >
          <Button size="small" type="text" icon={<ReloadOutlined />} title="重启会话" />
        </Popconfirm>
      </div>
      <div className="chat-history" ref={historyRef} onScroll={onHistoryScroll}>
        {skippedEvents > 0 && (
          <button
            type="button"
            className="history-gap"
            onClick={() => {
              fullHistoryRef.current = true;
              setHistoryEpoch((epoch) => epoch + 1);
            }}
          >
            已折叠较早的对话记录 · 点击加载全部
          </button>
        )}
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
            if (item.role === 'changes') {
              const { added = [], modified = [], deleted = [] } = item.content;
              const total = added.length + modified.length + deleted.length;
              return (
                <ChangesCard
                  key={item.key}
                  added={added}
                  modified={modified}
                  deleted={deleted}
                  total={total}
                  knownPaths={knownPaths}
                  onOpenFile={onOpenFile}
                  onOpenDiff={() =>
                    onOpenDiff?.(item.content.from, [...(added || []), ...(modified || [])])
                  }
                />
              );
            }
            if (item.role === 'system') {
              return (
                <div key={item.key} className="system-line">
                  — {item.content} —
                </div>
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
            if (item.role === 'subagent') {
              return <SubagentCard key={item.key} item={item} />;
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
            if (isUser) {
              return (
                <div key={item.key}>
                  {item.ts && (
                    <div className="round-divider">
                      <span title={absTime(item.ts)}>{relTime(item.ts)}</span>
                    </div>
                  )}
                  <div className="msg-in msg-right has-time">
                    <Bubble
                      placement="end"
                      variant="filled"
                      styles={USER_BUBBLE_STYLE}
                      content={item.content}
                    />
                    {item.ts && <span className="msg-time">{absTime(item.ts)}</span>}
                  </div>
                </div>
              );
            }
            return (
              <div key={item.key} className="msg-soft assistant-row">
                <Bubble
                  placement="start"
                  variant="filled"
                  styles={ASSISTANT_BUBBLE_STYLE}
                  content={
                    <Markdown
                      content={item.content}
                      onOpenFile={onOpenFile}
                      knownPaths={knownPaths}
                    />
                  }
                />
                <div className="msg-ops">
                  {item.ts && <span className="msg-ops-time">{relTime(item.ts)}</span>}
                  <CopyOpButton text={item.content} />
                  <button
                    type="button"
                    onClick={() => {
                      const quoted = item.content.slice(0, 120).replace(/\n+/g, ' ');
                      setInput(`> ${quoted}${item.content.length > 120 ? '…' : ''}\n\n`);
                      recallIndexRef.current = -1;
                    }}
                  >
                    引用
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      setDistill({
                        name: item.content.split('\n')[0].replace(/^#+\s*/, '').slice(0, 40),
                        content: item.content,
                      })
                    }
                  >
                    沉淀知识
                  </button>
                </div>
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
                        {up.status === 'error' && `上传失败：${up.error || '未知错误'}，已退回输入框`}
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
                  content={
                    <Markdown content={live.text} onOpenFile={onOpenFile} knownPaths={knownPaths} />
                  }
                />
              )}
            </div>
          )}
          {state === 'running' && !hasLive && (
            <Bubble loading styles={ASSISTANT_BUBBLE_STYLE} variant="filled" content="" />
          )}
        </Space>
      </div>
      {newCount > 0 && (
        <button type="button" className="new-msg-pill" onClick={() => scrollToBottom()}>
          <ArrowDownOutlined /> 新消息 {newCount}
        </button>
      )}
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
        {state !== 'running' && state !== 'queued' && !uploading && (
          <div className="quick-row msg-soft">
            {suggestions.map((sug) => (
              <Tooltip key={sug.key} title={sug.tooltip} mouseEnterDelay={0.4}>
                <button
                  type="button"
                  className={`quick-chip${sug.primary ? ' primary' : ''}`}
                  onClick={() => {
                    if (sug.fill !== undefined) setInput(sug.fill);
                    else if (sug.command.endsWith(' ')) setInput(sug.command);
                    else send(sug.command);
                  }}
                >
                  {sug.primary && <span className="chip-arrow">→</span>}
                  {sug.label}
                </button>
              </Tooltip>
            ))}
            <Dropdown
              menu={{
                items: ALL_COMMANDS.map((c) => ({ key: c.key, label: c.label })),
                onClick: ({ key }) => {
                  const cmd = ALL_COMMANDS.find((c) => c.key === key);
                  if (!cmd) return;
                  if (cmd.text.endsWith(' ')) setInput(cmd.text);
                  else send(cmd.text);
                },
              }}
            >
              <button type="button" className="quick-chip more" title="全部命令">
                <MoreOutlined />
              </button>
            </Dropdown>
          </div>
        )}
        {attachments.length > 0 && (
          <div className="attach-row">
            {attachments.map((att) => (
              <div key={att.uid} className="attach-chip">
                {att.preview ? (
                  <img className="attach-thumb" src={att.preview} alt="" />
                ) : (
                  <FileOutlined className="attach-icon" />
                )}
                <div className="attach-meta">
                  <span className="attach-name" title={att.name}>
                    {att.name}
                  </span>
                  <span className="attach-ext">{fileExt(att.name)}</span>
                </div>
                <CloseOutlined
                  className="attach-remove"
                  onClick={() => removeAttachment(att.uid)}
                />
              </div>
            ))}
          </div>
        )}
        <div onKeyDownCapture={onRecallKey}>
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
          placeholder="输入消息，可拖入文件或直接粘贴截图作为资料附件"
          onCancel={() => {
            // 上传阶段还没有会话消息可打断，忽略停止按钮
            if (!uploading)
              api
                .interrupt(project.id, conversationIdRef.current)
                .then((r) => {
                  // 幽灵运行态（无活跃会话）：后端只补写了历史，SSE 无推送
                  // 通道，重建流拉取补写的中断标记与 idle 终态
                  if (r?.reconciled) setHistoryEpoch((e) => e + 1);
                })
                .catch(() => {});
          }}
        />
        </div>
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
