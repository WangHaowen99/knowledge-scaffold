import { DeleteOutlined, InboxOutlined } from '@ant-design/icons';
import {
  Button,
  Divider,
  List,
  Popconfirm,
  Segmented,
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
const STAGES = [
  {
    key: 'research',
    title: '调研',
    probe: (paths) => paths.some((p) => p.startsWith('research/')),
    actions: [
      { label: '开始调研', command: '/ontology-research ' },
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
    key: 'logic',
    title: '逻辑提取',
    probe: (paths) => paths.some((p) => p.startsWith('ontology/logic/')),
    actions: [{ label: '提取逻辑', command: '/ontology-extract-logic' }],
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
  { label: '增量更新', command: '/ontology-update ' },
  { label: '反查', command: '/ontology-query ' },
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

function toTreeData(nodes) {
  return nodes.map((node) => ({
    key: node.path,
    title: node.name,
    isLeaf: node.type === 'file',
    children: node.children ? toTreeData(node.children) : undefined,
  }));
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
  tree,
  versions,
  onSelectFile,
  onRefresh,
  onCommand,
  onCompare,
}) {
  const [section, setSection] = useState('progress');
  const [expandedStage, setExpandedStage] = useState(null);

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

  const upload = async (file) => {
    try {
      const lower = file.name.toLowerCase();
      const subdir = lower.endsWith('.xlsx') || lower.endsWith('.xls')
        ? 'excel'
        : lower.endsWith('.drawio')
          ? 'diagrams'
          : 'others';
      await api.uploadResource(project.id, subdir, file);
      message.success(`已上传 ${file.name}`);
      onRefresh();
    } catch (error) {
      message.error(error.message);
    }
    return false;
  };

  const removeResource = async (path) => {
    try {
      await api.deleteResource(project.id, path);
      message.success('已删除');
      onRefresh();
    } catch (error) {
      message.error(error.message);
    }
  };

  const revert = async (commit) => {
    try {
      await api.revert(project.id, commit);
      message.success('已回退，生成了新版本');
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
          { label: '产物', value: 'files' },
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
          </Space>
        </>
      )}

      {section === 'files' &&
        (tree.length > 0 ? (
          <Tree.DirectoryTree
            treeData={toTreeData(tree)}
            onSelect={(keys, info) => {
              if (info.node.isLeaf) onSelectFile(String(keys[0]));
            }}
          />
        ) : (
          <Typography.Paragraph type="secondary">
            暂无产物，先在「进度」里开始调研
          </Typography.Paragraph>
        ))}

      {section === 'resources' && (
        <>
          <Upload.Dragger multiple showUploadList={false} beforeUpload={upload}>
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">上传领域资料</p>
            <p className="ant-upload-hint">按扩展名归入 diagrams / excel / others</p>
          </Upload.Dragger>
          <List
            size="small"
            style={{ marginTop: 8 }}
            dataSource={resources}
            locale={{ emptyText: '暂无资料' }}
            renderItem={(item) => (
              <List.Item
                actions={[
                  <Popconfirm
                    key="del"
                    title="删除该资料？"
                    onConfirm={() => removeResource(item.path)}
                  >
                    <Button size="small" type="text" icon={<DeleteOutlined />} />
                  </Popconfirm>,
                ]}
              >
                <Typography.Text
                  style={{ cursor: 'pointer' }}
                  onClick={() => onSelectFile(item.path)}
                  ellipsis
                >
                  {item.path.replace('resource/', '')}
                </Typography.Text>
              </List.Item>
            )}
          />
        </>
      )}

      {section === 'versions' && (
        <Timeline
          items={versions.map((entry, index) => ({
            children: (
              <>
                <Space size={6}>
                  <Typography.Text strong>版本 {entry.version}</Typography.Text>
                  {index === 0 && <Tag color="blue">当前</Tag>}
                </Space>
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {new Date(entry.time * 1000).toLocaleString()}
                  </Typography.Text>
                </div>
                <div style={{ margin: '2px 0 4px' }}>{entry.message}</div>
                <Space size={4}>
                  <Button size="small" onClick={() => onCompare(entry.commit)}>
                    对比
                  </Button>
                  {index > 0 && (
                    <Popconfirm
                      title={`回退到版本 ${entry.version}？将生成一个新版本`}
                      onConfirm={() => revert(entry.commit)}
                    >
                      <Button size="small" danger>
                        回退
                      </Button>
                    </Popconfirm>
                  )}
                </Space>
              </>
            ),
          }))}
        />
      )}
    </div>
  );
}
