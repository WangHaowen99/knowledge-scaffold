import { Background, Controls, MarkerType, ReactFlow } from '@xyflow/react';
import { Alert, Button, Input, Select, Space, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { api } from '../../lib/api.js';
import { setWorkspaceDirty } from '../../lib/workspaceDirty.js';
import { FlowNode } from './FlowchartEditor.jsx';
import { applyDrawioRepair, inspectDrawioRepair, REPAIR_NODE_KINDS, repairPreviewGraph, validateRepairedDrawio } from './drawioRepair.js';

const KIND_LABELS = {
  start: '开始', end: '结束', action: '业务步骤', decision: '判断', object: '数据对象', data: '数据',
};
const nodeTypes = { flow: FlowNode };

export default function DrawioRepairPanel({ project, path, content, revision, onDirtyChange, onSaved }) {
  const [xml, setXml] = useState(content);
  const [drafts, setDrafts] = useState({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [activePage, setActivePage] = useState(0);
  const issues = useMemo(() => inspectDrawioRepair(xml), [xml]);
  const previewPages = useMemo(() => repairPreviewGraph(xml), [xml]);
  const preview = previewPages[activePage] || previewPages[0];
  useEffect(() => {
    setXml(content);
    setDrafts({});
    setError('');
    setActivePage(0);
  }, [content, revision]);
  useEffect(() => {
    setWorkspaceDirty('flowchart-repair', xml !== content || saving);
    onDirtyChange?.(xml !== content || saving);
    return () => setWorkspaceDirty('flowchart-repair', false);
  }, [xml, content, saving, onDirtyChange]);

  const repair = (issue, key) => {
    try {
      const replacement = issue.type === 'dangling'
        ? drafts[key] : drafts[key] || issue.suggestion;
      setXml(applyDrawioRepair(xml, issue, replacement));
      setDrafts({});
      setError('');
    } catch (failure) { setError(failure.message); }
  };

  const save = async () => {
    try {
      validateRepairedDrawio(xml);
      setSaving(true);
      await api.writeFile(project.id, path, xml, revision);
      message.success('结构已修复并保存');
      onSaved();
    } catch (failure) { setError(failure.message); }
    finally { setSaving(false); }
  };

  return <div className="flowchart-repair-panel">
    <div className="flowchart-repair-canvas">
      {previewPages.length > 1 && <Select aria-label="流程图页面" value={activePage}
        onChange={setActivePage} options={previewPages.map((page, index) => ({ value: index, label: page.name }))} />}
      <div className="flowchart-editor-canvas" aria-label="修复预览画布">
        {preview && <ReactFlow key={`${activePage}:${xml}`} nodes={preview.nodes}
          edges={preview.edges.map((edge) => ({ ...edge,
            markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
          }))}
          nodeTypes={nodeTypes} fitView nodesDraggable={false} nodesConnectable={false}
          elementsSelectable={false} deleteKeyCode={null} minZoom={0.3} maxZoom={1.8}
          proOptions={{ hideAttribution: true }}>
          <Background color="#dfe4eb" gap={20} size={1} />
          <Controls showInteractive={false} position="bottom-right" />
        </ReactFlow>}
      </div>
      <div className="flowchart-repair-preview-hint">预览已将重叠节点临时错开，并标出缺失节点。保存内容以右侧修复操作为准。</div>
    </div>
    <div className="flowchart-repair-controls">
      <Alert type="warning" showIcon message="流程图结构需要修复"
        description="请逐项修复后保存。点击应用修复后，左侧画框会同步更新。" />
      {issues.map((issue, index) => {
      const key = `${issue.diagramIndex}:${issue.cellIndex}:${issue.type}:${issue.endpoint || ''}`;
      const value = drafts[key] ?? issue.suggestion ?? '';
      return <div key={key} style={{ marginTop: 16 }}>
        <div>{index + 1}. {issue.title}</div>
        <Space style={{ marginTop: 6 }} wrap>
          {issue.type === 'duplicate'
            ? <Input aria-label="新节点 ID" value={value} onChange={(event) => setDrafts({ ...drafts, [key]: event.target.value })} />
            : issue.type === 'kind' ? <Select aria-label="节点类型" style={{ minWidth: 180 }}
              value={value} onChange={(next) => setDrafts({ ...drafts, [key]: next })}
              options={REPAIR_NODE_KINDS.map((kind) => ({ value: kind, label: KIND_LABELS[kind] }))} />
              : <>
                <Select aria-label="修复方式" placeholder="选择修复方式" style={{ minWidth: 180 }}
                  value={drafts[key]?.mode} onChange={(mode) => setDrafts({ ...drafts, [key]: { mode } })}
                  options={[{ value: 'create', label: '补建缺失节点' }, { value: 'existing', label: '连接现有节点' }]} />
                {drafts[key]?.mode === 'create' && <Input aria-label="新节点名称" placeholder="输入新节点名称"
                  value={drafts[key]?.label || ''}
                  onChange={(event) => setDrafts({ ...drafts, [key]: { ...drafts[key], label: event.target.value } })} />}
                {drafts[key]?.mode === 'existing' && <Select aria-label="连线端点" placeholder="选择现有节点"
                  style={{ minWidth: 180 }} value={drafts[key]?.nodeId}
                  onChange={(nodeId) => setDrafts({ ...drafts, [key]: { ...drafts[key], nodeId } })}
                  options={issue.options} />}
              </>}
          <Button onClick={() => repair(issue, key)}>应用修复</Button>
        </Space>
      </div>;
      })}
      {error && <Alert style={{ marginTop: 16 }} type="error" showIcon message={error} />}
      {!issues.length && <Alert style={{ marginTop: 16 }} type="success" showIcon message="结构问题已修复，可以保存" />}
      <Button style={{ marginTop: 16 }} type="primary" disabled={Boolean(issues.length) || xml === content || saving}
        loading={saving} onClick={save}>保存修复后的流程图</Button>
    </div>
  </div>;
}
