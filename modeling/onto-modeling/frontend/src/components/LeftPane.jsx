import {
  DeleteOutlined,
  InboxOutlined,
  TagOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Divider,
  Dropdown,
  Input,
  List,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Steps,
  Tag,
  Tooltip,
  Timeline,
  Tree,
  Typography,
  Upload,
  message,
} from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import EvalRunDetail from './EvalRunDetail.jsx';
import EvalWizard from './EvalWizard.jsx';

// 阶段定义：探测产物目录推断完成态（对应 CLI 版 detect-stage），
// actions 是该阶段的操作入口；command 以空格结尾表示"填入输入框待补参数"
// 有产物 = 目录下存在真实文件：排除目录节点本身（尾 '/'，新项目会预建全部目录）
// 与目录索引 index.md
const hasEntry = (paths, ...prefixes) =>
  paths.some(
    (p) =>
      prefixes.some((prefix) => p.startsWith(prefix)) &&
      !p.endsWith('/') &&
      !p.endsWith('/index.md'),
  );

export const STAGES = [
  {
    key: 'research',
    title: '资料分析',
    probe: (paths) => hasEntry(paths, 'research/'),
    actions: [
      { label: '联网资料分析', command: '/ontology-research ' },
      { label: '仅本地资料', command: '/ontology-research 仅基于本地文档 ' },
      { label: '追加分析', command: '/ontology-research 追加' },
    ],
  },
  {
    key: 'flowchart',
    title: '流程图（可选）',
    optional: true,
    probe: (paths) => hasEntry(paths, 'ontology/diagrams/'),
    actions: [
      { label: '创建/打磨流程图', command: '/ontology-flowchart' },
      { label: '全量刷新', command: '/ontology-flowchart 全量刷新' },
      { label: '校验流程图', command: '/ontology-validate-diagrams' },
    ],
  },
  {
    key: 'object',
    title: '对象提取',
    probe: (paths) => hasEntry(paths, 'ontology/object/'),
    actions: [
      { label: '提取对象', command: '/ontology-extract-object' },
      { label: '迁移提取', command: '迁移提取业务对象 ' },
    ],
  },
  {
    key: 'operation',
    title: '操作提取',
    // ontology/logic 为旧目录兼容（范式升级 ADR-0004）
    probe: (paths) => hasEntry(paths, 'ontology/operation/', 'ontology/logic/'),
    actions: [{ label: '提取操作', command: '/ontology-extract-operation' }],
  },
  {
    key: 'rulemetric',
    title: '规则与指标（可选）',
    optional: true,
    probe: (paths) => hasEntry(paths, 'ontology/rule/', 'ontology/metric/'),
    actions: [{ label: '提取规则与指标', command: '/ontology-extract-rule-metric' }],
  },
  {
    key: 'validate',
    title: '质量检查',
    probe: (paths) => hasEntry(paths, 'reports/'),
    actions: [{ label: '运行质量检查', command: '/ontology-validate' }],
  },
  {
    key: 'mock',
    title: '示例数据',
    probe: (paths) => hasEntry(paths, 'ontology/data-mock/'),
    actions: [{ label: '生成示例数据', command: '/ontology-mock' }],
  },
  {
    key: 'trace',
    title: '场景验证',
    probe: (paths) => hasEntry(paths, 'ontology/testcase/'),
    actions: [
      { label: '批量验证', command: '/ontology-trace 全部' },
      { label: '交互走查', command: '/ontology-trace 交互' },
    ],
  },
  {
    key: 'codegen',
    title: '代码生成（可选）',
    optional: true,
    probe: (paths) => hasEntry(paths, 'ontology/scripts/'),
    actions: [
      { label: '扫描候选', command: '/ontology-codegen-scan' },
      { label: '生成脚本', command: '/ontology-codegen ' },
    ],
  },
  {
    key: 'eval',
    title: '数据评测（可选）',
    optional: true,
    // 探针：评测报告文件（由平台编排落盘），非目录占位
    probe: (paths) => paths.some((p) => p.startsWith('reports/eval-')),
    actions: [], // 入口是平台编排（复核 Modal），不走聊天命令
  },
];

// 每个阶段的展示状态：可选阶段未完成不阻塞后续（finish/process/wait）
export function stageStatuses(paths) {
  const hits = STAGES.map((stage) => stage.probe(paths));
  let firstPending = -1;
  for (let i = 0; i < STAGES.length; i += 1) {
    if (!hits[i] && !STAGES[i].optional) {
      firstPending = i;
      break;
    }
  }
  return STAGES.map((stage, i) => {
    if (hits[i]) return 'finish';
    if (i === firstPending) return 'process';
    return 'wait';
  });
}

// 资料用途（对齐 modeling 的 material purpose 体系）；schema/term 仅表格类可选
export const MATERIAL_PURPOSES = [
  { value: 'auto', label: '由助手判断' },
  { value: 'goal', label: '建模目标依据' },
  { value: 'schema', label: '数据 Schema', tableOnly: true },
  { value: 'term', label: '术语表', tableOnly: true },
  { value: 'evidence', label: '通用业务依据' },
  { value: 'eval_data', label: '评测数据', tableOnly: true },
];
const isTableFile = (name) => /\.(xlsx?|csv)$/i.test(name);

// 运行中的 /ontology-* 命令 → 所属阶段（左栏活跃高亮用）
export function stageForCommand(text) {
  const cmd = String(text || '').trim().split(/\s/)[0];
  if (!cmd.startsWith('/ontology')) return null;
  for (const stage of STAGES) {
    for (const action of stage.actions) {
      if (action.command.trim().split(/\s/)[0] === cmd) return stage.key;
    }
  }
  return null;
}

const EXTRA_ACTIONS = [
  { label: '整体视图', command: '/ontology-overview' },
  { label: '增量更新', command: '/ontology-update ' },
];

function flattenPaths(nodes, out = []) {
  for (const node of nodes) {
    out.push(node.path + (node.type === 'dir' ? '/' : ''));
    if (node.children) flattenPaths(node.children, out);
  }
  return out;
}

// 供 App 顶栏与概览复用：从产物树推断阶段进度
export function stageProgress(tree) {
  const paths = flattenPaths(tree);
  const statuses = stageStatuses(paths);
  const done = statuses.filter((status) => status === 'finish').length;
  const pending = statuses.indexOf('process');
  const current = pending >= 0 ? STAGES[pending].title : null;
  return { done, total: STAGES.length, current };
}

function toTreeData(nodes, keyword) {
  const kw = (keyword || '').toLowerCase();
  const filter = (list) =>
    list
      .map((node) => {
        const children = node.children ? filter(node.children) : undefined;
        const hit = !kw || node.name.toLowerCase().includes(kw);
        if (node.type === 'dir') {
          if (children && children.length > 0) {
            return { key: node.path, title: node.name, isLeaf: false, children };
          }
          return hit && kw ? { key: node.path, title: node.name, isLeaf: false, children: [] } : null;
        }
        return hit ? { key: node.path, title: node.name, isLeaf: true } : null;
      })
      .filter(Boolean);
  return filter(nodes);
}

function collectResources(nodes) {
  const files = [];
  const walk = (list) => {
    for (const node of list) {
      if (
        node.type === 'file' &&
        node.path.startsWith('resource/') &&
        node.name.toLowerCase() !== 'readme.md' &&
        node.path !== 'resource/materials.json' &&
        // 图例定义供流程图工具链使用（摘要节点分类依赖），不作为用户资料展示
        node.path !== 'resource/diagrams/图示说明定义.drawio'
      )
        files.push(node);
      if (node.children) walk(node.children);
    }
  };
  walk(nodes);
  return files;
}

export default function LeftPane({
  project,
  meta,
  tree,
  versions = [],
  activeStage = null,
  onSelectFile,
  onRefresh,
  onCommand,
  onKnowledge,
}) {
  const [section, setSection] = useState('progress');
  const [expandedStage, setExpandedStage] = useState(null);
  const [treeKeyword, setTreeKeyword] = useState('');
  const [uploadSubdir, setUploadSubdir] = useState('auto');
  const [tagging, setTagging] = useState(null); // {commit, label}

  const paths = useMemo(() => flattenPaths(tree), [tree]);
  const statuses = useMemo(() => {
    const base = stageStatuses(paths);
    // 会话正在执行某阶段命令时，进行中的高亮跟随实际执行的阶段
    const activeIndex = activeStage ? STAGES.findIndex((s) => s.key === activeStage) : -1;
    if (activeIndex < 0) return base;
    return base.map((status, index) =>
      index === activeIndex ? 'process' : status === 'process' ? 'wait' : status,
    );
  }, [paths, activeStage]);
  const resources = useMemo(() => collectResources(tree), [tree]);
  const [deleting, setDeleting] = useState(null);
  const [directives, setDirectives] = useState({});

  useEffect(() => {
    if (section !== 'resources') return;
    api
      .resourceDirectives(project.id)
      .then((data) => setDirectives(data.directives || {}))
      .catch(() => {});
  }, [section, project.id]);

  // ---- 数据评测（方案 P2）：状态轮询 + 评测向导 ----
  const [evalInfo, setEvalInfo] = useState(null); // {enabled, reachable, state, evolution}
  const [evalDetailOpen, setEvalDetailOpen] = useState(false);
  const [evalWizardOpen, setEvalWizardOpen] = useState(false);
  useEffect(() => {
    if (section !== 'progress') return undefined;
    let stop = false;
    const pull = () =>
      api.evalStatus(project.id).then((d) => {
        if (!stop) setEvalInfo(d);
      }).catch(() => {});
    pull();
    const timer = setInterval(() => {
      // 运行中密集轮询，空闲时低频
      if (evalInfo?.state?.status === 'running' || evalDetailOpen) pull();
    }, 8000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section, project.id, evalInfo?.state?.status, evalDetailOpen]);

  // 评测进入终态：刷新文件树与项目 meta——报告出现在树里、阶段点亮，
  // 聊天区的「评测优化」建议（依赖 meta.eval_evolution）才能及时出现
  const evalStatusRef = useRef(null);
  useEffect(() => {
    const status = evalInfo?.state?.status;
    if (evalStatusRef.current === 'running' && status && status !== 'running') onRefresh?.();
    evalStatusRef.current = status;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [evalInfo?.state?.status]);

  const [evalActing, setEvalActing] = useState(false); // 停止/续接请求进行中
  const refreshEvalStatus = () =>
    api.evalStatus(project.id).then(setEvalInfo).catch(() => {});

  const stopEval = async () => {
    setEvalActing(true);
    try {
      const res = await api.evalStop(project.id);
      message.success(
        res.cancelled ? '已停止评测，modeling 侧轮次已取消' : '已停止评测',
      );
    } catch (err) {
      message.error(err.message || '停止评测失败');
    } finally {
      setEvalActing(false);
      refreshEvalStatus();
    }
  };

  const resumeEval = async () => {
    setEvalActing(true);
    try {
      await api.evalResume(project.id);
      message.info('正在续接上一轮：收取 modeling 侧已有结果，不重跑用例');
      // 乐观置为运行中，让密集轮询立刻生效；随后的真实状态会覆盖它
      setEvalInfo((prev) =>
        prev
          ? { ...prev, state: { ...prev.state, status: 'running', message: '续接上一轮…' } }
          : prev,
      );
    } catch (err) {
      message.error(err.message || '续接失败');
      refreshEvalStatus();
    } finally {
      setEvalActing(false);
    }
  };

  const onEvalStarted = async () => {
    setEvalWizardOpen(false);
    setEvalDetailOpen(true);
    refreshEvalStatus();
  };

  // 失分/异常用例 → /ontology-evolution 归因修订（与聊天区建议同一判定）
  const evolution = evalInfo?.evolution;
  const runEvolution = () =>
    onCommand(`/ontology-evolution ${evolution.report.split('/').pop()}`);

  const [alignment, setAlignment] = useState(null); // eval_data 三方对账结果
  useEffect(() => {
    if (section !== 'resources') return;
    const hasEvalData = Object.values(directives).some((d) => d?.purpose === 'eval_data');
    if (!hasEvalData) {
      setAlignment(null);
      return;
    }
    api.evalDataAlignment(project.id).then(setAlignment).catch(() => setAlignment(null));
  }, [section, directives, project.id]);

  const alignBadge = (path) => {
    if (directives[path]?.purpose !== 'eval_data') return null;
    const entry = alignment?.files?.find((f) => f.path === path);
    if (!entry) {
      if (alignment?.status === 'no_schema')
        return <Tag color="default">无 schema 可对账</Tag>;
      return null;
    }
    if (entry.status === 'ok') return <Tag color="green">对账通过</Tag>;
    const issues = entry.sheets.flatMap((sh) => [
      ...sh.data_only.map((c) => `「${sh.name}」列 ${c} 不在 schema 中`),
      ...sh.attr_unmatched.map((c) => `「${sh.name}」列 ${c} 未映射到对象属性`),
    ]);
    return (
      <Tooltip title={issues.slice(0, 8).join('；')}>
        <Tag color="orange">对账差异 {issues.length} 处</Tag>
      </Tooltip>
    );
  };

  const changeDirective = async (path, purpose, readingMode) => {
    try {
      await api.setResourceDirective(project.id, path, purpose, readingMode);
      setDirectives((prev) => ({ ...prev, [path]: { purpose, reading_mode: readingMode } }));
    } catch (error) {
      message.error(error.message);
    }
  };

  // 资料变更提醒：资料上传/删除时间晚于最近版本 → 派生产物可能过期
  const latestVersionTime = versions[0]?.time || 0;
  const resourceStale =
    (meta?.resource_updated_at || 0) > latestVersionTime && hasEntry(paths, 'research/');

  const autoSubdir = (name) => {
    const lower = name.toLowerCase();
    if (lower.endsWith('.xlsx') || lower.endsWith('.xls')) return 'excel';
    if (lower.endsWith('.drawio')) return 'diagrams';
    return 'others';
  };

  const upload = async (file, forcedSubdir = null) => {
    try {
      const subdir =
        forcedSubdir || (uploadSubdir === 'auto' ? autoSubdir(file.name) : uploadSubdir);
      const saved = await api.uploadResource(project.id, subdir, file);
      message.success(saved.replaced ? `已替换 ${file.name}` : `已上传 ${file.name}`);
      onRefresh();
    } catch (error) {
      message.error(error.message);
    }
    return false;
  };

  const removeResource = async (path) => {
    try {
      setDeleting(path);
      await api.deleteResource(project.id, path);
      message.success('已删除');
      onRefresh();
    } catch (error) {
      message.error(error.message);
    } finally {
      setDeleting(null);
    }
  };

  const revertTo = async (commit) => {
    try {
      await api.revert(project.id, commit);
      message.success('已回退并生成新版本');
      onRefresh();
    } catch (error) {
      message.error(error.message);
    }
  };

  const saveTag = async () => {
    if (!tagging?.label?.trim()) return;
    try {
      await api.tagVersion(project.id, tagging.commit, tagging.label.trim());
      message.success('已标记');
      setTagging(null);
      onRefresh();
    } catch (error) {
      message.error(error.message);
    }
  };

  return (
    <div>
      <Segmented
        block
        size="small"
        value={section}
        onChange={setSection}
        options={[
          { label: '进度', value: 'progress' },
          { label: '知识包', value: 'files' },
          { label: '资料', value: 'resources' },
          { label: '版本', value: 'versions' },
        ]}
        style={{ marginBottom: 12 }}
      />

      {section === 'progress' && (
        <>
          <Steps
            direction="vertical"
            size="small"
            className="stage-steps"
            items={STAGES.map((stage, index) => ({
              status: statuses[index],
              title: (
                <span
                  style={{ cursor: 'pointer' }}
                  onClick={() =>
                    setExpandedStage(expandedStage === stage.key ? null : stage.key)
                  }
                >
                  {stage.title}
                </span>
              ),
              description:
                expandedStage === stage.key ? (
                  <Space wrap size={4} style={{ marginTop: 4 }}>
                    {stage.key === 'eval' ? (
                      !evalInfo?.enabled ? (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          未配置 modeling 评测服务
                        </Typography.Text>
                      ) : (
                        <>
                          <Tooltip title={evalInfo?.reachable ? '' : 'modeling 评测服务不可达，请先启动'}>
                            <Button
                              size="small"
                              type="primary"
                              ghost
                              disabled={!evalInfo?.reachable || evalInfo?.state?.status === 'running'}
                              onClick={() => setEvalWizardOpen(true)}
                            >
                              {evalInfo?.state?.status === 'running' ? '评测运行中…' : '运行评测'}
                            </Button>
                          </Tooltip>
                          {evalInfo?.state?.status === 'running' && (
                            <Popconfirm
                              title="停止本轮评测？"
                              description="modeling 侧轮次会被取消，已完成用例的判分结果保留；停止后可重新运行。"
                              okText="停止"
                              okButtonProps={{ danger: true }}
                              cancelText="继续评测"
                              onConfirm={stopEval}
                            >
                              <Button size="small" danger loading={evalActing}>
                                停止评测
                              </Button>
                            </Popconfirm>
                          )}
                          {evalInfo?.state?.resumable && (
                            <Tooltip title="只收取 modeling 侧已有轮次的结果，不重跑用例、不重复计费">
                              <Button size="small" loading={evalActing} onClick={resumeEval}>
                                续接上一轮
                              </Button>
                            </Tooltip>
                          )}
                          {evalInfo?.state?.status === 'running' && (
                            <Typography.Link
                              style={{ fontSize: 12 }}
                              onClick={() => setEvalDetailOpen(true)}
                            >
                              <span className="eval-live-dot" />
                              {evalInfo.state.message}
                            </Typography.Link>
                          )}
                          {evalInfo?.state?.status === 'done' && evalInfo.state.summary && (
                            <Tag
                              color={evalInfo.state.summary.score >= 80 ? 'green' : 'orange'}
                              style={{ cursor: 'pointer' }}
                              onClick={() => onSelectFile(evalInfo.state.report)}
                            >
                              最近得分 {evalInfo.state.summary.score}（{evalInfo.state.summary.pass}✓/
                              {evalInfo.state.summary.fail}✗）
                            </Tag>
                          )}
                          {evalInfo?.state?.status === 'failed' && (
                            <Tooltip title={evalInfo.state.message}>
                              <Tag
                                color="red"
                                style={{ cursor: evalInfo.state.cases?.length ? 'pointer' : undefined }}
                                onClick={() => evalInfo.state.cases?.length && setEvalDetailOpen(true)}
                              >
                                {evolution?.round_failed
                                  ? `上次评测异常终止（${evolution.errors} 条异常）`
                                  : '上次评测失败'}
                              </Tag>
                            </Tooltip>
                          )}
                          {evolution && evalInfo?.state?.status !== 'running' && (
                            <Tooltip title="根据最近一轮评测的失分与异常用例归因并优化知识包">
                              <Button size="small" onClick={runEvolution}>
                                评测优化
                              </Button>
                            </Tooltip>
                          )}
                          {(evalInfo?.state?.status === 'interrupted'
                            || evalInfo?.state?.status === 'stopped') && (
                            <Tooltip title={evalInfo.state.message}>
                              <Tag color={evalInfo.state.status === 'stopped' ? 'default' : 'gold'}>
                                {evalInfo.state.status === 'stopped' ? '上次评测已停止' : '上次评测被中断'}
                              </Tag>
                            </Tooltip>
                          )}
                          {evalInfo?.state?.status !== 'running' && evalInfo?.state?.cases?.length > 0 && (
                            <Typography.Link
                              style={{ fontSize: 12 }}
                              onClick={() => setEvalDetailOpen(true)}
                            >
                              执行详情
                            </Typography.Link>
                          )}
                        </>
                      )
                    ) : (
                      stage.actions.map((action) => (
                        <Button
                          key={action.label}
                          size="small"
                          type={statuses[index] === 'process' ? 'primary' : 'default'}
                          ghost={statuses[index] === 'process'}
                          onClick={() => onCommand(action.command)}
                        >
                          {action.label}
                        </Button>
                      ))
                    )}
                  </Space>
                ) : undefined,
            }))}
          />
          <Divider style={{ margin: '8px 0' }} />
          {statuses.every((st, i) => st === 'finish' || STAGES[i].optional) && (
            <Button
              type="primary"
              block
              className="export-package-btn"
              style={{ marginBottom: 8 }}
              onClick={() => window.open(api.exportUrl(project.id, 'package'))}
            >
              导出知识包
              {evalInfo?.state?.summary != null && (
                <span className="export-eval-score">评测 {evalInfo.state.summary.score}</span>
              )}
            </Button>
          )}
          {statuses.every((st, i) => st === 'finish' || STAGES[i].optional) &&
            evalInfo?.enabled &&
            evalInfo?.state?.status !== 'done' && (
              <Alert
                type="warning"
                showIcon
                className="export-eval-hint"
                message="尚未数据评测——建议发布前在「数据评测」阶段跑一轮，验证知识包问答质量"
                style={{ marginBottom: 8, fontSize: 12, padding: '4px 10px' }}
              />
            )}
          <Space wrap size={4}>
            {EXTRA_ACTIONS.map((action) => (
              <Button key={action.label} size="small" onClick={() => onCommand(action.command)}>
                {action.label}
              </Button>
            ))}
            <Button size="small" onClick={() => onKnowledge?.()}>
              知识库
            </Button>
          </Space>
        </>
      )}

      {section === 'files' && (
        <>
          <Input.Search
            allowClear
            size="small"
            placeholder="在产物树内搜索文件名"
            value={treeKeyword}
            onChange={(e) => setTreeKeyword(e.target.value)}
            style={{ marginBottom: 8 }}
          />
          {tree.length > 0 ? (
            <Tree.DirectoryTree
              treeData={toTreeData(tree, treeKeyword)}
              defaultExpandAll={!!treeKeyword}
              key={treeKeyword ? 'kw' : 'all'}
              onSelect={(keys, info) => {
                if (info.node.isLeaf) onSelectFile(String(keys[0]));
              }}
              titleRender={(node) =>
                node.isLeaf ? (
                  <Dropdown
                    trigger={['contextMenu']}
                    menu={{
                      items: [
                        { key: 'open', label: '打开' },
                        { key: 'impact', label: '影响分析' },
                      ],
                      onClick: ({ key, domEvent }) => {
                        domEvent.stopPropagation();
                        if (key === 'open') onSelectFile(node.key);
                        else if (key === 'impact') onCommand(`/ontology-impact ${node.key}`);
                      },
                    }}
                  >
                    <span>{node.title}</span>
                  </Dropdown>
                ) : (
                  node.title
                )
              }
            />
          ) : (
            <Typography.Paragraph type="secondary">
              暂无知识包，先在「进度」里开始资料分析
            </Typography.Paragraph>
          )}
        </>
      )}

      {section === 'resources' && (
        <>
          {resourceStale && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 8 }}
              message="资料区有更新"
              description="上传/删除时间晚于最近版本，派生产物可能已过期。"
              action={
                <Button
                  size="small"
                  onClick={() =>
                    onCommand('/ontology-update 资料区有新变更，请检查并刷新受影响的产物')
                  }
                >
                  让助手刷新
                </Button>
              }
            />
          )}
          <Space.Compact block style={{ marginBottom: 8 }}>
            <Select
              size="small"
              value={uploadSubdir}
              onChange={setUploadSubdir}
              style={{ width: '100%' }}
              options={[
                { value: 'auto', label: '按类型自动归类' },
                { value: 'diagrams', label: '流程图（diagrams）' },
                { value: 'excel', label: '表格（excel）' },
                { value: 'others', label: '其他资料（others）' },
              ]}
            />
          </Space.Compact>
          <Upload.Dragger multiple showUploadList={false} beforeUpload={(f) => upload(f)}>
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">上传领域资料</p>
            <p className="ant-upload-hint">支持 Markdown、Word、Excel、PDF、Drawio、TXT；单个文件不超过 20 MB</p>
          </Upload.Dragger>
          <List
            size="small"
            style={{ marginTop: 8 }}
            dataSource={resources}
            locale={{ emptyText: '暂无资料' }}
            renderItem={(item) => {
              const itemSubdir = item.path.split('/')[1];
              return (
                <List.Item
                  className={deleting === item.path ? 'resource-item deleting' : 'resource-item'}
                  actions={[
                    <Upload
                      key="replace"
                      showUploadList={false}
                      beforeUpload={(f) => upload(f, itemSubdir)}
                    >
                      <Button size="small" type="text" title="替换（上传同名文件覆盖）" icon={<UploadOutlined />} />
                    </Upload>,
                    <Popconfirm
                      key="del"
                      title="删除该资料？"
                      onConfirm={() => removeResource(item.path)}
                    >
                      <Button size="small" type="text" loading={deleting === item.path} icon={<DeleteOutlined />} />
                    </Popconfirm>,
                  ]}
                >
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <Typography.Text
                      style={{ cursor: 'pointer', display: 'block' }}
                      onClick={() => onSelectFile(item.path)}
                      ellipsis
                    >
                      {item.path.split('/').pop()}
                    </Typography.Text>
                    <div className="resource-directive">
                      <Select
                        size="small"
                        variant="borderless"
                        popupMatchSelectWidth={false}
                        value={directives[item.path]?.purpose || 'auto'}
                        onChange={(purpose) =>
                          changeDirective(
                            item.path,
                            purpose,
                            directives[item.path]?.reading_mode || 'on_demand',
                          )
                        }
                        options={MATERIAL_PURPOSES.map((opt) => ({
                          value: opt.value,
                          label: opt.label,
                          disabled: opt.tableOnly && !isTableFile(item.path),
                        }))}
                      />
                      {alignBadge(item.path)}
                      <label className="resource-reading">
                        <input
                          type="checkbox"
                          checked={directives[item.path]?.reading_mode === 'full'}
                          onChange={(e) =>
                            changeDirective(
                              item.path,
                              directives[item.path]?.purpose || 'auto',
                              e.target.checked ? 'full' : 'on_demand',
                            )
                          }
                        />
                        全文精读
                      </label>
                    </div>
                  </div>
                </List.Item>
              );
            }}
          />
        </>
      )}

      {section === 'versions' && (
        <>
          {versions.length === 0 ? (
            <Typography.Paragraph type="secondary">还没有版本记录</Typography.Paragraph>
          ) : (
            <Timeline
              items={versions.map((v) => ({
                key: v.commit,
                color: v.tags?.length ? 'green' : 'blue',
                children: (
                  <div className="version-item">
                    <div>
                      <Typography.Text strong>版本 {v.version}</Typography.Text>{' '}
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {new Date(v.time * 1000).toLocaleString()}
                      </Typography.Text>
                    </div>
                    <div className="version-msg">{v.message}</div>
                    {v.tags?.map((t) => (
                      <Tag
                        key={t.name}
                        color="green"
                        closable
                        onClose={(e) => {
                          e.preventDefault();
                          api.deleteTag(project.id, t.name).then(onRefresh).catch((err) =>
                            message.error(err.message),
                          );
                        }}
                        icon={<TagOutlined />}
                      >
                        {t.label}
                      </Tag>
                    ))}
                    <Space size={4} style={{ marginTop: 2 }}>
                      <Button
                        size="small"
                        type="text"
                        onClick={() => setTagging({ commit: v.commit, label: '' })}
                      >
                        标记
                      </Button>
                      <Popconfirm
                        title={`回退到版本 ${v.version}？`}
                        description="回退会生成新版本，历史保留可再回退"
                        onConfirm={() => revertTo(v.commit)}
                      >
                        <Button size="small" type="text" danger>
                          回退
                        </Button>
                      </Popconfirm>
                    </Space>
                  </div>
                ),
              }))}
            />
          )}
          <Modal
            title="标记版本"
            open={!!tagging}
            onCancel={() => setTagging(null)}
            onOk={saveTag}
            okText="标记"
          >
            <Input
              placeholder="如：校验通过的里程碑"
              value={tagging?.label}
              onChange={(e) => setTagging({ ...tagging, label: e.target.value })}
              onPressEnter={saveTag}
              autoFocus
            />
          </Modal>
        </>
      )}

      <EvalRunDetail
        open={evalDetailOpen}
        state={evalInfo?.state}
        evolution={evolution}
        onClose={() => setEvalDetailOpen(false)}
        onOpenReport={(path) => {
          setEvalDetailOpen(false);
          onSelectFile(path);
        }}
        onEvolution={() => {
          setEvalDetailOpen(false);
          runEvolution();
        }}
      />
      <EvalWizard
        project={project}
        open={evalWizardOpen}
        onClose={() => setEvalWizardOpen(false)}
        onStarted={onEvalStarted}
      />
    </div>
  );
}
