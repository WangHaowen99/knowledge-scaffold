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
import { useMemo, useState } from 'react';
import { api } from '../lib/api.js';

// 阶段定义：探测产物目录推断完成态（对应 CLI 版 detect-stage），
// actions 是该阶段的操作入口；command 以空格结尾表示"填入输入框待补参数"
export const STAGES = [
  {
    key: 'research',
    title: '调研',
    probe: (paths) => paths.some((p) => p.startsWith('research/')),
    actions: [
      { label: '联网调研', command: '/ontology-research ' },
      { label: '仅本地资料', command: '/ontology-research 仅基于本地文档 ' },
      { label: '追加调研', command: '/ontology-research 追加' },
    ],
  },
  {
    key: 'flowchart',
    title: '流程图（可选）',
    probe: (paths) => paths.some((p) => p.startsWith('ontology/diagrams/')),
    actions: [
      { label: '创建/打磨流程图', command: '/ontology-flowchart' },
      { label: '全量刷新', command: '/ontology-flowchart 全量刷新' },
      { label: '校验流程图', command: '/ontology-validate-diagrams' },
    ],
  },
  {
    key: 'object',
    title: '对象提取',
    probe: (paths) => paths.some((p) => p.startsWith('ontology/object/')),
    actions: [
      { label: '提取对象', command: '/ontology-extract-object' },
      { label: '迁移提取', command: '迁移提取业务对象 ' },
    ],
  },
  {
    key: 'operation',
    title: '操作提取',
    // ontology/logic 为旧目录兼容（范式升级 ADR-0004）
    probe: (paths) =>
      paths.some((p) => p.startsWith('ontology/operation/') || p.startsWith('ontology/logic/')),
    actions: [{ label: '提取操作', command: '/ontology-extract-operation' }],
  },
  {
    key: 'validate',
    title: '校验',
    probe: (paths) => paths.some((p) => p.startsWith('reports/')),
    actions: [{ label: '运行校验', command: '/ontology-validate' }],
  },
  {
    key: 'mock',
    title: 'Mock 数据',
    probe: (paths) => paths.some((p) => p.startsWith('ontology/data-mock/')),
    actions: [{ label: '生成 Mock', command: '/ontology-mock' }],
  },
  {
    key: 'trace',
    title: '场景追踪',
    probe: (paths) => paths.some((p) => p.startsWith('ontology/testcase/')),
    actions: [
      { label: '批量追踪', command: '/ontology-trace 全部' },
      { label: '交互走查', command: '/ontology-trace 交互' },
    ],
  },
  {
    key: 'codegen',
    title: '代码生成（可选）',
    probe: (paths) => paths.some((p) => p.startsWith('ontology/scripts/')),
    actions: [
      { label: '扫描候选', command: '/ontology-codegen-scan' },
      { label: '生成脚本', command: '/ontology-codegen ' },
    ],
  },
];

const EXTRA_ACTIONS = [
  { label: '整体视图', command: '/ontology-overview' },
  { label: '规则与指标', command: '/ontology-extract-rule-metric' },
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
  let done = 0;
  for (const stage of STAGES) {
    if (stage.probe(paths)) done += 1;
    else break;
  }
  const current = done < STAGES.length ? STAGES[done].title : null;
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
      if (node.type === 'file' && node.path.startsWith('resource/')) files.push(node);
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
  onSelectFile,
  onRefresh,
  onCommand,
  onDiff,
  onQuery,
  onKnowledge,
}) {
  const [section, setSection] = useState('progress');
  const [expandedStage, setExpandedStage] = useState(null);
  const [treeKeyword, setTreeKeyword] = useState('');
  const [uploadSubdir, setUploadSubdir] = useState('auto');
  const [tagging, setTagging] = useState(null); // {commit, label}

  const paths = useMemo(() => flattenPaths(tree), [tree]);
  const doneCount = useMemo(() => {
    let count = 0;
    for (const stage of STAGES) {
      if (stage.probe(paths)) count += 1;
      else break;
    }
    return count;
  }, [paths]);
  const resources = useMemo(() => collectResources(tree), [tree]);
  const [deleting, setDeleting] = useState(null);

  // 资料变更提醒：资料上传/删除时间晚于最近版本 → 派生产物可能过期
  const latestVersionTime = versions[0]?.time || 0;
  const resourceStale =
    (meta?.resource_updated_at || 0) > latestVersionTime && paths.some((p) => p.startsWith('research/'));

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
            current={doneCount}
            items={STAGES.map((stage, index) => ({
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
                        type={index === doneCount ? 'primary' : 'default'}
                        ghost={index === doneCount}
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
          <Space wrap size={4}>
            {EXTRA_ACTIONS.map((action) => (
              <Button key={action.label} size="small" onClick={() => onCommand(action.command)}>
                {action.label}
              </Button>
            ))}
            <Button size="small" onClick={() => onQuery?.('orphans', '')}>
              反查面板
            </Button>
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
                        { type: 'divider' },
                        { key: 'used_by', label: '谁引用了它' },
                        { key: 'search', label: '全文搜索该名称' },
                        { key: 'knowledge', label: '相关业务知识' },
                      ],
                      onClick: ({ key, domEvent }) => {
                        domEvent.stopPropagation();
                        const name = String(node.title).replace(/\.md$/i, '');
                        if (key === 'open') onSelectFile(node.key);
                        else if (key === 'impact') onCommand(`/ontology-impact ${node.key}`);
                        else onQuery?.(key, name);
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
              暂无知识包，先在「进度」里开始调研
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
                  <Typography.Text
                    style={{ cursor: 'pointer' }}
                    onClick={() => onSelectFile(item.path)}
                    ellipsis
                  >
                    {item.path.split('/').pop()}
                  </Typography.Text>
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
                      <Button size="small" type="text" onClick={() => onDiff?.(v.commit)}>
                        对比
                      </Button>
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
