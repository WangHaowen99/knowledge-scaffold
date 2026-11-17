import { Background, Controls, MarkerType, ReactFlow } from '@xyflow/react';
import { Alert, Button, Input, Modal, Select, Space, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { api } from '../../lib/api.js';
import { setWorkspaceDirty } from '../../lib/workspaceDirty.js';
import { FlowNode } from './FlowchartEditor.jsx';
import { applyDrawioRepair, inspectDrawioRepair, keepRepairDrafts, repairIssueKey, repairIssueLabel, REPAIR_NODE_KINDS, repairPreviewGraph, validateRepairedDrawio } from './drawioRepair.js';

const KIND_LABELS = {
  start: '开始', end: '结束', action: '业务步骤', decision: '判断', object: '数据对象', data: '数据',
};
const nodeTypes = { flow: FlowNode };

export default function DrawioRepairPanel({ project, path, content, revision, onDirtyChange, onSaved }) {
  const [modal, modalContext] = Modal.useModal();
  const [xml, setXml] = useState(content);
  const [drafts, setDrafts] = useState({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [activePage, setActivePage] = useState(0);
  const [selectedIssue, setSelectedIssue] = useState('');
  const issues = useMemo(() => inspectDrawioRepair(xml), [xml]);
  const previewPages = useMemo(() => repairPreviewGraph(xml), [xml]);
  const preview = previewPages[activePage] || previewPages[0];
  const highlighted = issues.find((issue) => repairIssueKey(issue) === selectedIssue);
  useEffect(() => {
    setXml(content);
    setDrafts({});
    setError('');
    setActivePage(0);
    setSelectedIssue('');
  }, [content, revision]);
  useEffect(() => {
    setWorkspaceDirty('flowchart-repair', xml !== content || saving);
    onDirtyChange?.(xml !== content || saving);
    return () => setWorkspaceDirty('flowchart-repair', false);
  }, [xml, content, saving, onDirtyChange]);

  const applyRepair = (issue, key, replacement) => {
    try {
      const repaired = applyDrawioRepair(xml, issue, replacement);
      const remaining = inspectDrawioRepair(repaired);
      if (!remaining.length) validateRepairedDrawio(repaired);
      setXml(repaired);
      setDrafts(keepRepairDrafts(xml, issues, remaining, drafts, issue, replacement));
      setSelectedIssue('');
      setError('');
    } catch (failure) { setError(failure.message); }
  };

  const repair = (issue, key, choice) => {
    const replacement = choice || (issue.type === 'duplicate'
      ? drafts[key] || { mode: 'rename', id: issue.suggestion }
      : issue.type === 'dangling' ? drafts[key] : drafts[key] || issue.suggestion);
    if (replacement?.mode !== 'delete') {
      applyRepair(issue, key, replacement);
      return;
    }
    const duplicate = issue.type === 'duplicate';
    modal.confirm({
      centered: true,
      title: duplicate ? '删除重复节点？' : '删除这条连线？',
      content: duplicate
        ? `将删除节点「${issue.label}」。它与「${issue.firstLabel}」共用编号，相关连线原本指向哪个节点无法区分。`
        : `将删除连线「${issue.edgeLabel || issue.edgeId || '未命名连线'}」及其关联的文字标注。`,
      okText: '确定',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => applyRepair(issue, key, replacement),
    });
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
    {modalContext}
    <div className="flowchart-repair-canvas">
      {previewPages.length > 1 && <Select aria-label="流程图页面" value={activePage}
        onChange={setActivePage} options={previewPages.map((page, index) => ({ value: index, label: page.name }))} />}
      <div className="flowchart-editor-canvas" aria-label="修复预览画布">
        {preview && <ReactFlow key={`${activePage}:${xml}`} nodes={preview.nodes.map((node) => ({ ...node,
          className: [node.className, highlighted?.diagramIndex === activePage
            && node.repairCellIndex === highlighted.cellIndex && highlighted.type !== 'dangling'
            ? 'flowchart-repair-highlight-node' : ''].filter(Boolean).join(' '),
        }))}
          edges={preview.edges.map((edge) => ({ ...edge,
            className: [edge.className, highlighted?.diagramIndex === activePage
              && edge.repairCellIndex === highlighted.cellIndex && highlighted.type === 'dangling'
              ? 'flowchart-repair-highlight-edge' : ''].filter(Boolean).join(' '),
            markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
          }))}
          nodeTypes={nodeTypes} fitView nodesDraggable={false} nodesConnectable={false}
          elementsSelectable={false} deleteKeyCode={null} minZoom={0.3} maxZoom={1.8}
          proOptions={{ hideAttribution: true }}>
          <Background color="#dfe4eb" gap={20} size={1} />
          <Controls showInteractive={false} position="bottom-right" />
        </ReactFlow>}
      </div>
      <div className="flowchart-repair-preview-hint">重叠节点在预览中已错开；点击右侧问题可定位。</div>
    </div>
    <div className="flowchart-repair-controls">
      <Alert type="warning" showIcon message="请修复以下结构问题" />
      {issues.map((issue, index) => {
      const key = repairIssueKey(issue);
      const value = drafts[key] ?? issue.suggestion ?? '';
      const duplicateDraft = issue.type === 'duplicate' ? drafts[key] || { mode: 'rename', id: issue.suggestion } : null;
      const title = repairIssueLabel(issue);
      return <div key={key} className={selectedIssue === key ? 'flowchart-repair-selected-issue' : ''} style={{ marginTop: 16 }}>
        <Button type="text" className="flowchart-repair-issue-title" onClick={() => { setActivePage(issue.diagramIndex); setSelectedIssue(key); }}>
          {index + 1}. {title}
        </Button>
        <Space style={{ marginTop: 6 }} wrap>
          {issue.type === 'duplicate'
            ? <>
              <Select aria-label="重复节点修复方式" style={{ minWidth: 180 }}
                value={duplicateDraft.mode}
                onChange={(mode) => setDrafts({ ...drafts, [key]: { ...duplicateDraft, mode } })}
                options={[{ value: 'rename', label: '为此节点重新编号' }, { value: 'delete', label: '删除此重复节点' }]} />
              {duplicateDraft.mode === 'rename' && <Input aria-label="新节点 ID" value={duplicateDraft.id || ''}
                onChange={(event) => setDrafts({ ...drafts, [key]: { ...duplicateDraft, id: event.target.value } })} />}
            </>
            : issue.type === 'kind' ? <Select aria-label="节点类型" style={{ minWidth: 180 }}
              value={value} onChange={(next) => setDrafts({ ...drafts, [key]: next })}
              options={REPAIR_NODE_KINDS.map((kind) => ({ value: kind, label: KIND_LABELS[kind] }))} />
              : <>
                <Select aria-label="修复方式" placeholder="选择修复方式" style={{ minWidth: 180 }}
                  value={drafts[key]?.mode} onChange={(mode) => setDrafts({ ...drafts, [key]: { mode } })}
                  options={[{ value: 'create', label: '补建缺失节点' }, { value: 'existing', label: '连接现有节点' },
                    { value: 'delete', label: '删除这条连线' }]} />
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
      <Button style={{ marginTop: 16 }} type="primary" disabled={Boolean(issues.length) || xml === content || saving}
        loading={saving} onClick={save}>保存修复后的流程图</Button>
    </div>
  </div>;
}
