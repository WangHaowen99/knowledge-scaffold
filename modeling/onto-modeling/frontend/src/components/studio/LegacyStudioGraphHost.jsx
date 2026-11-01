import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import StudioGraph from './legacy/StudioGraph.jsx';
import { createStudioSession } from './legacy/studio-model.js';
import './legacy/studio-tokens.css';
import './legacy/studio-base.css';
import './legacy/studio.css';

/** 原版 modeling Studio 图的宿主适配层（方案 7.1"整体搬迁"）。
 *
 * 组件、样式、WebGL 力导运行时与 token 全部原样搬自
 * modeling/frontend/src/workbench/studio/，本层只做三件事：
 * 1. 数据映射：ST-02 关系图 → 原版契约（边的样式取 relation，「提及」统一按 references 画）；
 * 2. 受控：搜索词、规则链 / 指标链高亮、选中节点由页面传入，页面自己的工具条、图例、侧栏代替原版的；
 * 3. 回调：单击节点 onSelect(id)，双击 onOpen(id)。
 * 组件不再自己请求接口，数据由页面经 useApp().api 取好传进来。
 */
export default function LegacyStudioGraphHost({ graph, query, highlight = 'all', selectedId, onSelect, onOpen }) {
  const [session, setSession] = useState(() => ({
    ...createStudioSession(),
    kind: '',
    graphHighlight: highlight,
    selectedElementId: selectedId || '',
  }));

  const update = useCallback((patch) => {
    setSession((current) => ({
      ...current,
      ...(typeof patch === 'function' ? patch(current) : patch),
    }));
  }, []);

  const mapped = useMemo(() => {
    const src = graph || { nodes: [], edges: [] };
    const nodes = (src.nodes || []).map((node) => ({ id: node.id, label: node.label || node.id, kind: node.kind }));
    const edges = (src.edges || []).map((edge, index) => ({
      id: edge.id || `e-${index}`,
      source: edge.source,
      target: edge.target,
      kind: edge.kind === 'mention' ? 'references' : edge.relation || 'references',
      label: edge.label || '',
    }));
    const elements = (src.nodes || []).map((node) => ({
      id: node.id, kind: node.kind, title: node.label || node.id, description: node.description || '',
    }));
    return { graph: { nodes, edges }, elements };
  }, [graph]);

  useEffect(() => {
    update({ graph: mapped.graph, elements: mapped.elements, details: {}, workingRevision: graph?.head || '' });
  }, [graph?.head, mapped, update]);

  // 页面传入的选中、高亮、搜索词同步进原版的 session
  const selectedRef = useRef(selectedId || '');
  useEffect(() => {
    selectedRef.current = selectedId || '';
    update({ selectedElementId: selectedId || '' });
  }, [selectedId, update]);
  useEffect(() => { update({ graphHighlight: highlight }); }, [highlight, update]);
  useEffect(() => { update({ query: (query || '').trim(), queryDraft: query || '' }); }, [query, update]);

  // 原版里点了节点 / 空白处：把选中回报给页面
  useEffect(() => {
    if (session.selectedElementId !== selectedRef.current) onSelect?.(session.selectedElementId || '');
  }, [session.selectedElementId, onSelect]);

  const open = useCallback((elementId) => onOpen?.(elementId), [onOpen]);

  return (
    // wb = modeling 工作台的样式作用域根类：原版 studio.css 与 tokens 全部以 .wb 前缀限定
    <div className="legacy-studio-host wb">
      <StudioGraph session={session} update={update} onOpen={open} focus={null} />
    </div>
  );
}
