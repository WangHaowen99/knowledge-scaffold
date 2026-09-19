import { useCallback, useEffect, useMemo, useState } from 'react';
import StudioGraph from './legacy/StudioGraph.jsx';
import { createStudioSession } from './legacy/studio-model.js';
import './legacy/studio-tokens.css';
import './legacy/studio.css';

/** 原版 modeling Studio 图的宿主适配层（方案 7.1"整体搬迁"）。
 *
 * 组件、样式、WebGL 力导运行时与 token 全部原样搬自
 * modeling/frontend/src/workbench/studio/，本层只做两件事：
 * 1. 数据映射：onto 本体图（object/logic）→ 原版契约（logic 映射为 operation，
 *    与设计"逻辑沿用操作的视觉"一致）；
 * 2. 提供原版所需的 session 状态容器与 update/onOpen 回调。
 */
export default function LegacyStudioGraphHost({ project, graph, query, onOpenEntry }) {
  const [session, setSession] = useState(() => ({
    ...createStudioSession(),
    kind: '',
    graphHighlight: 'all',
  }));

  const update = useCallback((patch, _label) => {
    setSession((current) => ({
      ...current,
      ...(typeof patch === 'function' ? patch(current) : patch),
    }));
  }, []);

  const mapped = useMemo(() => {
    const nodes = (graph.nodes || []).map((node) => ({
      id: node.id,
      label: node.id,
      kind: node.kind === 'logic' ? 'operation' : node.kind,
    }));
    const edges = (graph.edges || []).map((edge, index) => ({
      id: `e-${index}`,
      source: edge.source,
      target: edge.target,
    }));
    const elements = nodes.map((node) => ({ id: node.id, title: node.id, description: '' }));
    const pathById = new Map((graph.nodes || []).map((node) => [node.id, node.path]));
    return { graph: { nodes, edges }, elements, pathById };
  }, [graph]);

  useEffect(() => {
    update({ graph: mapped.graph, elements: mapped.elements });
  }, [mapped, update]);

  // 外层工具条搜索同步进原版的图过滤
  useEffect(() => {
    update({ query: (query || '').trim(), queryDraft: query || '' });
  }, [query, update]);

  const onOpen = useCallback(
    (elementId) => {
      const path = mapped.pathById.get(elementId);
      if (path) onOpenEntry?.(path);
    },
    [mapped.pathById, onOpenEntry],
  );

  return (
    // wb = modeling 工作台的样式作用域根类：原版 studio.css 与 tokens 全部以 .wb 前缀限定
    <div className="legacy-studio-host wb">
      <StudioGraph
        projectId={project.id}
        session={session}
        update={update}
        onOpen={onOpen}
        focus={null}
      />
    </div>
  );
}
