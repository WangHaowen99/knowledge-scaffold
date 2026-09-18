import { InboxOutlined } from '@ant-design/icons';
import { Divider, Steps, Tree, Typography, Upload, message } from 'antd';
import { useMemo } from 'react';
import { api } from '../lib/api.js';

// 从产物目录推断建模阶段（对应 CLI 版 state_helper.py 的 detect-stage）
const STAGES = [
  { key: 'research', title: '调研', probe: (paths) => paths.some((p) => p.startsWith('research/')) },
  { key: 'flowchart', title: '流程图', probe: (paths) => paths.some((p) => p.startsWith('ontology/diagrams/')) },
  { key: 'object', title: '对象提取', probe: (paths) => paths.some((p) => p.startsWith('ontology/object/')) },
  { key: 'logic', title: '逻辑提取', probe: (paths) => paths.some((p) => p.startsWith('ontology/logic/')) },
  { key: 'validate', title: '校验', probe: (paths) => paths.some((p) => p.startsWith('reports/')) },
  { key: 'mock', title: 'Mock 数据', probe: (paths) => paths.some((p) => p.startsWith('ontology/data-mock/')) },
];

function flattenPaths(nodes, out = []) {
  for (const node of nodes) {
    out.push(node.path + (node.type === 'dir' ? '/' : ''));
    if (node.children) flattenPaths(node.children, out);
  }
  return out;
}

function toTreeData(nodes) {
  return nodes.map((node) => ({
    key: node.path,
    title: node.name,
    isLeaf: node.type === 'file',
    children: node.children ? toTreeData(node.children) : undefined,
  }));
}

export default function LeftPane({ project, tree, onSelectFile, onRefresh }) {
  const paths = useMemo(() => flattenPaths(tree), [tree]);
  const doneCount = useMemo(() => {
    let count = 0;
    for (const stage of STAGES) {
      if (stage.probe(paths)) count += 1;
      else break; // 阶段是顺序推进的，遇到第一个未完成就停
    }
    return count;
  }, [paths]);

  const upload = async (file) => {
    try {
      await api.uploadResource(project.id, 'others', file);
      message.success(`已上传 ${file.name}`);
      onRefresh();
    } catch (error) {
      message.error(error.message);
    }
    return false; // 阻止 antd 默认上传
  };

  return (
    <div>
      <Typography.Text strong>建模进度</Typography.Text>
      <Steps
        style={{ marginTop: 8 }}
        direction="vertical"
        size="small"
        current={doneCount}
        items={STAGES.map((stage) => ({ title: stage.title }))}
      />
      <Divider style={{ margin: '12px 0' }} />
      <Typography.Text strong>产物</Typography.Text>
      {tree.length > 0 ? (
        <Tree.DirectoryTree
          style={{ marginTop: 8 }}
          treeData={toTreeData(tree)}
          onSelect={(keys, info) => {
            if (info.node.isLeaf) onSelectFile(String(keys[0]));
          }}
        />
      ) : (
        <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
          暂无产物，先在右侧对话开始调研
        </Typography.Paragraph>
      )}
      <Divider style={{ margin: '12px 0' }} />
      <Upload.Dragger multiple showUploadList={false} beforeUpload={upload}>
        <p className="ant-upload-drag-icon">
          <InboxOutlined />
        </p>
        <p className="ant-upload-text">上传领域资料</p>
        <p className="ant-upload-hint">存入 resource/others/，模型只读</p>
      </Upload.Dragger>
    </div>
  );
}
