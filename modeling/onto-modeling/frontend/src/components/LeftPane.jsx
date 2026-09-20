import { DeleteOutlined, InboxOutlined, TagOutlined, UploadOutlined } from '@ant-design/icons';
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
        node.path !== 'resource/materials.json'
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
                    {stage.actions.map((action) => (
                      <Button
                        key={action.label}
                        size="small"
                        type={statuses[index] === 'process' ? 'primary' : 'default'}
                        ghost={statuses[index] === 'process'}
                        onClick={() => onCommand(action.command)}
                      >
                        {action.label}
                      </Button>
                    ))}
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
            </Button>
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

    </div>
  );
}
