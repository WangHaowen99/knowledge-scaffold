import {
  CheckCircleFilled,
  CloseCircleFilled,
  DeleteOutlined,
  ExclamationCircleFilled,
  InboxOutlined,
  StopFilled,
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
  Progress,
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
import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api.js';

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

  // ---- 数据评测（方案 P2）：状态轮询 + 用例复核 Modal ----
  const [evalInfo, setEvalInfo] = useState(null); // {enabled, reachable, state}
  const [evalDetailOpen, setEvalDetailOpen] = useState(false);
  const [evalExpanded, setEvalExpanded] = useState(null);
  const [evalWiz, setEvalWiz] = useState(null); // {cases:[{question,groundTruth}]}
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


  const openEvalWizard = async () => {
    try {
      const [data, ds] = await Promise.all([
        api.evalCases(project.id),
        api.evalDataSourceOptions(project.id).catch(() => ({ eval_data: [], mock_tables: 0 })),
      ]);
      if (!data.cases?.length) {
        message.info('没有可转换的场景用例——先完成「场景验证」阶段，或在弹窗中手工添加');
      }
      setEvalWiz({
        step: 0,
        cases: data.cases || [],
        options: ds,
        dsMode: 'none', // none | mock | dataset
        selected: (ds.eval_data || []).map((f) => f.path), // dataset 模式默认全选
      });
    } catch (error) {
      message.error(error.message);
    }
  };

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
                              onClick={openEvalWizard}
                            >
                              {evalInfo?.state?.status === 'running' ? '评测运行中…' : '运行评测'}
                            </Button>
                          </Tooltip>
                          {evalInfo?.state?.status === 'running' && (
                            <Typography.Link
                              style={{ fontSize: 12 }}
                              onClick={() => {
                                setEvalExpanded(null);
                                setEvalDetailOpen(true);
                              }}
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
                                上次评测失败
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

    
      <Modal
        title="数据评测 · 执行详情"
        open={evalDetailOpen}
        onCancel={() => setEvalDetailOpen(false)}
        footer={null}
        width={620}
      >
        {(() => {
          const st = evalInfo?.state || {};
          const cases = st.cases || [];
          const finished = cases.filter((c) =>
            ['pass', 'partial', 'fail', 'error'].includes(c.verdict),
          );
          const counts = { pass: 0, partial: 0, fail: 0, error: 0 };
          finished.forEach((c) => {
            counts[c.verdict] += 1;
          });
          const percent = cases.length
            ? Math.round((finished.length * 100) / cases.length)
            : 0;
          const running = st.status === 'running';
          const PHASE_STEPS = [
            ['package', '打包'],
            ['import', '导入'],
            ['ds', '数据源'],
            ['cases', '用例'],
            ['evaluate', '执行'],
          ];
          const phaseKey = ['data_import', 'data_source'].includes(st.phase) ? 'ds' : st.phase;
          const phaseIdx =
            st.status === 'done'
              ? PHASE_STEPS.length
              : Math.max(0, PHASE_STEPS.findIndex(([k]) => k === phaseKey));
          const VERDICT_META = {
            pass: { icon: <CheckCircleFilled />, text: '通过', cls: 'pass' },
            partial: { icon: <ExclamationCircleFilled />, text: '部分', cls: 'partial' },
            fail: { icon: <CloseCircleFilled />, text: '失败', cls: 'fail' },
            error: { icon: <StopFilled />, text: '异常', cls: 'error' },
            running: { icon: <span className="eval-run-pulse" />, text: '执行中', cls: 'running' },
            pending: { icon: <span className="eval-run-hollow" />, text: '待执行', cls: 'pending' },
          };
          return (
            <div className="eval-run-view">
              <div className="eval-phase-strip">
                {PHASE_STEPS.map(([key, label], i) => (
                  <span key={key} className="eval-phase-item">
                    <span
                      className={`eval-phase-chip${
                        i < phaseIdx ? ' done' : i === phaseIdx && running ? ' current' : ''
                      }${st.status === 'failed' && i === phaseIdx ? ' failed' : ''}`}
                    >
                      {i < phaseIdx ? '✓ ' : ''}
                      {label}
                    </span>
                    {i < PHASE_STEPS.length - 1 && <span className="eval-phase-link" />}
                  </span>
                ))}
              </div>
              <div className="eval-run-summary">
                <Progress
                  percent={percent}
                  status={running ? 'active' : st.status === 'failed' ? 'exception' : 'normal'}
                  format={() => `${finished.length}/${cases.length}`}
                />
                <div className="eval-run-chips">
                  <span className="eval-chip pass">✓ 通过 {counts.pass}</span>
                  <span className="eval-chip partial">⚠ 部分 {counts.partial}</span>
                  <span className="eval-chip fail">✗ 失败 {counts.fail}</span>
                  <span className="eval-chip error">⊘ 异常 {counts.error}</span>
                  {st.summary && (
                    <span className="eval-chip score">得分 {st.summary.score}</span>
                  )}
                </div>
              </div>
              <div className="eval-run-list">
                {cases.map((c, index) => {
                  const meta = VERDICT_META[c.verdict] || VERDICT_META.pending;
                  const expandable = !!(c.answer || c.reason);
                  const expanded = evalExpanded === index;
                  return (
                    <div
                      key={index}
                      className={`eval-run-row ${meta.cls}${expanded ? ' expanded' : ''}`}
                      style={{ animationDelay: `${Math.min(index * 40, 400)}ms` }}
                      onClick={() => expandable && setEvalExpanded(expanded ? null : index)}
                    >
                      <div className="eval-run-head">
                        <span className={`eval-run-icon ${meta.cls}`}>{meta.icon}</span>
                        <span className="eval-run-index">{index + 1}</span>
                        <span className="eval-run-question" title={c.question}>
                          {c.question}
                        </span>
                        <span className={`eval-run-state ${meta.cls}`}>{meta.text}</span>
                      </div>
                      {expanded && expandable && (
                        <div className="eval-run-detail">
                          {c.answer && (
                            <div className="eval-run-field">
                              <span className="eval-run-label">回答</span>
                              <span>{c.answer}</span>
                            </div>
                          )}
                          {c.reason && (
                            <div className="eval-run-field">
                              <span className="eval-run-label">判分</span>
                              <span>{c.reason}</span>
                            </div>
                          )}
                          {st.report && (
                            <Typography.Link
                              style={{ fontSize: 12 }}
                              onClick={(e) => {
                                e.stopPropagation();
                                setEvalDetailOpen(false);
                                onSelectFile(st.report);
                              }}
                            >
                              查看完整报告 →
                            </Typography.Link>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
                {!cases.length && (
                  <Typography.Text type="secondary">
                    暂无用例执行数据——启动评测后此处实时展示每条用例的执行状态
                  </Typography.Text>
                )}
              </div>
            </div>
          );
        })()}
      </Modal>
      <Modal
        title={evalWiz?.step === 0 ? '数据评测 · 第 1 步：选择数据源' : '数据评测 · 第 2 步：用例复核'}
        open={!!evalWiz}
        onCancel={() => setEvalWiz(null)}
        width={720}
        footer={
          evalWiz?.step === 0
            ? [
                <Button key="cancel" onClick={() => setEvalWiz(null)}>取消</Button>,
                <Button
                  key="next"
                  type="primary"
                  disabled={
                    (evalWiz?.dsMode === 'dataset' && !evalWiz?.selected?.length) ||
                    (evalWiz?.dsMode === 'mock' && !evalWiz?.options?.mock_tables)
                  }
                  onClick={() => setEvalWiz({ ...evalWiz, step: 1 })}
                >
                  下一步：复核用例 →
                </Button>,
              ]
            : [
                <Button key="prev" onClick={() => setEvalWiz({ ...evalWiz, step: 0 })}>
                  ← 上一步
                </Button>,
                <Button
                  key="run"
                  type="primary"
                  disabled={!evalWiz?.cases?.length}
                  onClick={async () => {
                    try {
                      const ds =
                        evalWiz.dsMode === 'none'
                          ? { mode: 'none' }
                          : {
                              mode: 'dataset',
                              resources: evalWiz.dsMode === 'dataset' ? evalWiz.selected : [],
                              include_mock: evalWiz.dsMode === 'mock',
                            };
                      await api.evalRun(project.id, evalWiz.cases, ds);
                      message.success('评测已启动，进度见「数据评测」阶段');
                      setEvalWiz(null);
                      setEvalExpanded(null);
                      setEvalDetailOpen(true);
                      const data = await api.evalStatus(project.id);
                      setEvalInfo(data);
                    } catch (error) {
                      message.error(error.message);
                    }
                  }}
                >
                  运行评测（{evalWiz?.cases?.length || 0} 条）
                </Button>,
              ]
        }
      >
        {evalWiz?.step === 0 ? (
          <div className="eval-ds-view">
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 12 }}>
              评测时 consumer 智能体挂载知识包作答。接入数据源后，涉及具体数据的问题
              可实际查询评测库（数据表 → 评测库 → NL2SQL 查询）；不接则仅按知识包语义作答。
            </Typography.Paragraph>
            <div
              className={`eval-ds-card${evalWiz.dsMode === 'none' ? ' selected' : ''}`}
              onClick={() => setEvalWiz({ ...evalWiz, dsMode: 'none' })}
            >
              <div className="eval-ds-head">
                <span className="eval-ds-icon">📖</span>
                <span className="eval-ds-title">无数据库（默认）</span>
                <span className="eval-ds-badge">零配置</span>
              </div>
              <div className="eval-ds-desc">纯知识包问答——验证本体结构与业务语义，不查询具体数据。</div>
            </div>
            <div
              className={`eval-ds-card${evalWiz.dsMode === 'mock' ? ' selected' : ''}${
                evalWiz.options?.mock_tables ? '' : ' disabled'
              }`}
              onClick={() =>
                evalWiz.options?.mock_tables && setEvalWiz({ ...evalWiz, dsMode: 'mock' })
              }
            >
              <div className="eval-ds-head">
                <span className="eval-ds-icon">🧪</span>
                <span className="eval-ds-title">示例数据种子</span>
                <span className="eval-ds-badge">
                  {evalWiz.options?.mock_tables
                    ? `${evalWiz.options.mock_tables} 张示例表`
                    : '需先完成「示例数据」阶段'}
                </span>
              </div>
              <div className="eval-ds-desc">
                用「示例数据」阶段的 mock 产物生成种子库——没有真实数据也能验证数据问答链路。
              </div>
            </div>
            <div
              className={`eval-ds-card${evalWiz.dsMode === 'dataset' ? ' selected' : ''}${
                evalWiz.options?.eval_data?.length ? '' : ' disabled'
              }`}
              onClick={() =>
                evalWiz.options?.eval_data?.length && setEvalWiz({ ...evalWiz, dsMode: 'dataset' })
              }
            >
              <div className="eval-ds-head">
                <span className="eval-ds-icon">🗄️</span>
                <span className="eval-ds-title">业务数据表</span>
                <span className="eval-ds-badge">
                  {evalWiz.options?.eval_data?.length
                    ? `${evalWiz.options.eval_data.length} 份评测数据`
                    : '资料区暂无「评测数据」'}
                </span>
              </div>
              <div className="eval-ds-desc">
                导入打了「评测数据」用途标签的业务数据表（在左栏「资料」页签上传并打标）。
              </div>
              {evalWiz.dsMode === 'dataset' && (
                <div className="eval-ds-files" onClick={(e) => e.stopPropagation()}>
                  {(evalWiz.options?.eval_data || []).map((f) => (
                    <label key={f.path} className="eval-ds-file">
                      <input
                        type="checkbox"
                        checked={evalWiz.selected.includes(f.path)}
                        onChange={(e) =>
                          setEvalWiz({
                            ...evalWiz,
                            selected: e.target.checked
                              ? [...evalWiz.selected, f.path]
                              : evalWiz.selected.filter((x) => x !== f.path),
                          })
                        }
                      />
                      <span className="eval-ds-file-name">{f.name}</span>
                      <span className="eval-ds-file-size">{Math.round(f.size / 1024)} KB</span>
                    </label>
                  ))}
                </div>
              )}
            </div>
            {evalWiz.dsMode !== 'none' && (
              <div className="eval-ds-flow">
                <span>数据表</span>
                <span className="eval-ds-arrow">→</span>
                <span>评测库</span>
                <span className="eval-ds-arrow">→</span>
                <span>NL2SQL 绑定</span>
                <span className="eval-ds-arrow">→</span>
                <span>consumer 查询作答</span>
              </div>
            )}
          </div>
        ) : (
          <>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
              用例由「场景验证」的 trace 场景自动转换（问题 → consumer 智能体作答 → Judge
              按标准答案判分）。运行前请复核标准答案是否准确，可直接修改或删除。
            </Typography.Paragraph>
            <div style={{ maxHeight: 380, overflow: 'auto' }}>
              {(evalWiz?.cases || []).map((item, index) => (
                <div key={index} className="eval-case-row">
                  <div className="eval-case-head">
                    <Typography.Text strong style={{ fontSize: 12 }}>
                      用例 {index + 1}
                    </Typography.Text>
                    <Button
                      size="small"
                      type="text"
                      danger
                      onClick={() =>
                        setEvalWiz({ ...evalWiz, cases: evalWiz.cases.filter((_, i) => i !== index) })
                      }
                    >
                      删除
                    </Button>
                  </div>
                  <Input.TextArea
                    autoSize={{ minRows: 1, maxRows: 3 }}
                    value={item.question}
                    onChange={(e) => {
                      const cases = [...evalWiz.cases];
                      cases[index] = { ...item, question: e.target.value };
                      setEvalWiz({ ...evalWiz, cases });
                    }}
                  />
                  <Input.TextArea
                    autoSize={{ minRows: 1, maxRows: 3 }}
                    placeholder="标准答案"
                    style={{ marginTop: 4 }}
                    value={item.groundTruth}
                    onChange={(e) => {
                      const cases = [...evalWiz.cases];
                      cases[index] = { ...item, groundTruth: e.target.value };
                      setEvalWiz({ ...evalWiz, cases });
                    }}
                  />
                </div>
              ))}
            </div>
            <Space style={{ marginTop: 8 }}>
              <Button
                size="small"
                onClick={() =>
                  setEvalWiz({
                    ...evalWiz,
                    cases: [...(evalWiz?.cases || []), { question: '', groundTruth: '' }],
                  })
                }
              >
                + 手工添加用例
              </Button>
              <Upload
                accept=".json,application/json"
                showUploadList={false}
                beforeUpload={async (file) => {
                  try {
                    const data = await api.evalCasesImport(project.id, file);
                    setEvalWiz({ ...evalWiz, cases: [...(evalWiz?.cases || []), ...data.cases] });
                    message.success(`已导入 ${data.cases.length} 条用例`);
                  } catch (error) {
                    message.error(error.message);
                  }
                  return false; // 阻止默认上传行为
                }}
              >
                <Button size="small">批量导入 JSON</Button>
              </Upload>
              <Button
                size="small"
                type="link"
                onClick={() => window.open(api.evalCasesTemplateUrl(project.id))}
              >
                下载模板
              </Button>
            </Space>
          </>
        )}
      </Modal>
    </div>
  );
}
