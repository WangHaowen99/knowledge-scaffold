import { parseDrawioToDocument } from './drawioConvert.js';

export const REPAIR_NODE_KINDS = ['start', 'end', 'action', 'decision', 'object', 'data'];

function modelsIn(xml) {
  const dom = new DOMParser().parseFromString(xml, 'text/xml');
  if (dom.getElementsByTagName('parsererror').length) throw new Error('drawio XML 解析失败');
  return { dom, models: Array.from(dom.getElementsByTagName('mxGraphModel')) };
}

export function inspectDrawioRepair(xml) {
  const { models } = modelsIn(xml);
  const issues = [];
  models.forEach((model, diagramIndex) => {
    const cells = Array.from(model.getElementsByTagName('mxCell'));
    const nodes = cells.filter((cell) => cell.getAttribute('vertex') === '1'
      && !/(?:^|;)text(?:;|$)/.test(cell.getAttribute('style') || ''));
    const firstById = new Map();
    const allIds = new Set(cells.map((cell) => cell.getAttribute('id')).filter(Boolean));
    const nodeIds = new Set(nodes.map((cell) => cell.getAttribute('id')).filter(Boolean));
    cells.forEach((cell, cellIndex) => {
      if (cell.getAttribute('vertex') === '1' && nodes.includes(cell)) {
        const id = cell.getAttribute('id') || '';
        const first = firstById.get(id);
        const geometry = cell.getElementsByTagName('mxGeometry')[0];
        const location = geometry
          ? `(${geometry.getAttribute('x') || '0'}, ${geometry.getAttribute('y') || '0'})` : '未知位置';
        if (first !== undefined) {
          const firstGeometry = cells[first].getElementsByTagName('mxGeometry')[0];
          const overlap = Boolean(geometry && firstGeometry
            && ['x', 'y', 'width', 'height'].every((key) => geometry.getAttribute(key) === firstGeometry.getAttribute(key)));
          let suffix = 2;
          while (allIds.has(`${id}-${suffix}`)) suffix += 1;
          issues.push({ diagramIndex, cellIndex, type: 'duplicate', id,
            title: `第 ${cellIndex + 1} 个单元格「${cell.getAttribute('value') || id}」与第 ${first + 1} 个共用 ID ${id}，位置 ${location}`,
            suggestion: `${id}-${suffix}`, overlap });
        } else firstById.set(id, cellIndex);
        const kind = cell.getAttribute('data-flowchart-node-kind');
        if (kind && !REPAIR_NODE_KINDS.includes(kind)) {
          issues.push({ diagramIndex, cellIndex, type: 'kind', id,
            title: `节点「${cell.getAttribute('value') || id}」的类型 ${kind} 无效`, suggestion: 'action' });
        }
      } else if (cell.getAttribute('edge') === '1') {
        for (const endpoint of ['source', 'target']) {
          if (!nodeIds.has(cell.getAttribute(endpoint))) {
            const oppositeId = cell.getAttribute(endpoint === 'source' ? 'target' : 'source') || '';
            issues.push({ diagramIndex, cellIndex, type: 'dangling', endpoint,
              missingId: cell.getAttribute(endpoint) || '', oppositeId,
              title: `连线 ${cell.getAttribute('id') || cellIndex} 的${endpoint === 'source' ? '起点' : '终点'}「${cell.getAttribute(endpoint) || '空'}」不存在`,
              options: nodes.map((node) => ({ value: node.getAttribute('id'),
                label: `${node.getAttribute('value') || node.getAttribute('id')} (${node.getAttribute('id')})${node.getAttribute('id') === oppositeId ? ' · 会形成自环' : ''}` })),
            });
          }
        }
      }
    });
  });
  return issues;
}

export function applyDrawioRepair(xml, issue, replacement) {
  const { dom, models } = modelsIn(xml);
  const cell = Array.from(models[issue.diagramIndex]?.getElementsByTagName('mxCell') || [])[issue.cellIndex];
  if (!cell) throw new Error('待修复单元格已变化，请重新读取文件');
  if (issue.type === 'duplicate') {
    const id = String(replacement || '').trim();
    const ids = Array.from(models[issue.diagramIndex].getElementsByTagName('mxCell'))
      .filter((candidate) => candidate !== cell).map((candidate) => candidate.getAttribute('id'));
    if (!id || ids.includes(id)) throw new Error('请输入当前图中未使用的节点 ID');
    cell.setAttribute('id', id);
    if (issue.overlap) {
      const geometry = cell.getElementsByTagName('mxGeometry')[0];
      if (geometry) geometry.setAttribute('x', String(
        (Number(geometry.getAttribute('x')) || 0)
        + Math.max(Number(geometry.getAttribute('width')) || 0, 190) + 60));
    }
  } else if (issue.type === 'dangling') {
    if (replacement?.mode === 'existing') {
      const id = String(replacement.nodeId || '');
      if (!issue.options.some((option) => option.value === id)) throw new Error('请选择现有节点');
      cell.setAttribute(issue.endpoint, id);
    } else if (replacement?.mode === 'create') {
      const model = models[issue.diagramIndex];
      const root = model.getElementsByTagName('root')[0];
      if (!root) throw new Error('流程图缺少根节点');
      const label = String(replacement.label || '').trim();
      if (!label) throw new Error('请输入新节点名称');
      const used = new Set(Array.from(model.getElementsByTagName('mxCell'))
        .map((candidate) => candidate.getAttribute('id')).filter(Boolean));
      let id = issue.missingId;
      if (!id || used.has(id)) {
        let serial = 1;
        while (used.has(`node-${serial}`)) serial += 1;
        id = `node-${serial}`;
      }
      const other = Array.from(model.getElementsByTagName('mxCell'))
        .find((candidate) => candidate.getAttribute('vertex') === '1'
          && candidate.getAttribute('id') === issue.oppositeId);
      const otherGeometry = other?.getElementsByTagName('mxGeometry')[0];
      const x = Number(otherGeometry?.getAttribute('x')) || 40;
      const y = Number(otherGeometry?.getAttribute('y')) || 40;
      const width = Number(otherGeometry?.getAttribute('width')) || 160;
      const vertex = dom.createElement('mxCell');
      vertex.setAttribute('id', id);
      vertex.setAttribute('value', label);
      vertex.setAttribute('style', 'rounded=0;whiteSpace=wrap;html=1;');
      vertex.setAttribute('vertex', '1');
      vertex.setAttribute('parent', cell.getAttribute('parent') || '1');
      vertex.setAttribute('data-flowchart-node-kind', 'action');
      const geometry = dom.createElement('mxGeometry');
      geometry.setAttribute('x', String(issue.endpoint === 'target' ? x + width + 100 : x - 260));
      geometry.setAttribute('y', String(y));
      geometry.setAttribute('width', '160');
      geometry.setAttribute('height', '60');
      geometry.setAttribute('as', 'geometry');
      vertex.appendChild(geometry);
      root.appendChild(vertex);
      cell.setAttribute(issue.endpoint, id);
    } else throw new Error('请选择补建节点或连接到现有节点');
  } else if (issue.type === 'kind') {
    if (!REPAIR_NODE_KINDS.includes(replacement)) throw new Error('请选择有效的节点类型');
    cell.setAttribute('data-flowchart-node-kind', replacement);
  } else throw new Error('不支持的修复操作');
  return new XMLSerializer().serializeToString(dom);
}

export function validateRepairedDrawio(xml) {
  const issues = inspectDrawioRepair(xml);
  if (issues.length) throw new Error(`仍有 ${issues.length} 项结构问题需要修复`);
  return parseDrawioToDocument(xml);
}

// Preview-only copy: give overlapping duplicate cells distinct identities and
// represent missing edge endpoints so the canvas can show the whole problem.
export function repairPreviewXml(xml) {
  const { dom, models } = modelsIn(xml);
  models.forEach((model) => {
    const cells = Array.from(model.getElementsByTagName('mxCell'));
    const root = model.getElementsByTagName('root')[0];
    const used = new Set(cells.map((cell) => cell.getAttribute('id')).filter(Boolean));
    const firstById = new Map();
    let serial = 1;
    const uniqueId = () => {
      while (used.has(`repair-preview-${serial}`)) serial += 1;
      const id = `repair-preview-${serial++}`;
      used.add(id);
      return id;
    };
    const vertices = cells.filter((cell) => cell.getAttribute('vertex') === '1');
    vertices.forEach((cell) => {
      const id = cell.getAttribute('id');
      const first = firstById.get(id);
      if (!first) {
        firstById.set(id, cell);
        return;
      }
      cell.setAttribute('id', uniqueId());
      const geometry = cell.getElementsByTagName('mxGeometry')[0];
      const firstGeometry = first.getElementsByTagName('mxGeometry')[0];
      if (geometry && firstGeometry
        && ['x', 'y', 'width', 'height'].every((key) => geometry.getAttribute(key) === firstGeometry.getAttribute(key))) {
        geometry.setAttribute('x', String((Number(geometry.getAttribute('x')) || 0)
          + Math.max(Number(geometry.getAttribute('width')) || 0, 190) + 60));
      }
    });
    const positions = new Map(vertices.map((cell) => {
      const geometry = cell.getElementsByTagName('mxGeometry')[0];
      return [cell.getAttribute('id'), {
        x: Number(geometry?.getAttribute('x')) || 0,
        y: Number(geometry?.getAttribute('y')) || 0,
        width: Number(geometry?.getAttribute('width')) || 160,
      }];
    }));
    const missing = new Map();
    let placeholderIndex = 0;
    cells.filter((cell) => cell.getAttribute('edge') === '1').forEach((edge) => {
      for (const endpoint of ['source', 'target']) {
        const missingId = edge.getAttribute(endpoint) || '空';
        if (positions.has(missingId)) continue;
        if (!missing.has(missingId)) {
          const source = positions.get(edge.getAttribute('source'));
          const target = positions.get(edge.getAttribute('target'));
          const near = source || target || { x: 40, y: 40, width: 160 };
          const placeholderId = uniqueId();
          const placeholder = dom.createElement('mxCell');
          placeholder.setAttribute('id', placeholderId);
          placeholder.setAttribute('value', `缺失节点：${missingId}`);
          placeholder.setAttribute('style', 'rounded=1;whiteSpace=wrap;html=1;dashed=1;strokeColor=#d4380d;fillColor=#fff2e8;');
          placeholder.setAttribute('vertex', '1');
          placeholder.setAttribute('parent', '1');
          const geometry = dom.createElement('mxGeometry');
          geometry.setAttribute('x', String(near.x + near.width + 100));
          geometry.setAttribute('y', String(near.y + placeholderIndex * 90));
          geometry.setAttribute('width', '150');
          geometry.setAttribute('height', '50');
          geometry.setAttribute('as', 'geometry');
          placeholder.appendChild(geometry);
          root.appendChild(placeholder);
          missing.set(missingId, placeholderId);
          placeholderIndex += 1;
        }
        edge.setAttribute(endpoint, missing.get(missingId));
      }
    });
  });
  return new XMLSerializer().serializeToString(dom);
}

export function repairPreviewGraph(xml) {
  const { models } = modelsIn(repairPreviewXml(xml));
  return models.map((model, index) => {
    const cells = Array.from(model.getElementsByTagName('mxCell'));
    const nodes = cells.filter((cell) => cell.getAttribute('vertex') === '1'
      && !/(?:^|;)text(?:;|$)/.test(cell.getAttribute('style') || ''))
      .map((cell) => {
        const geometry = cell.getElementsByTagName('mxGeometry')[0];
        const rawKind = cell.getAttribute('data-flowchart-node-kind') || 'action';
        const missing = (cell.getAttribute('value') || '').startsWith('缺失节点：')
          && (cell.getAttribute('id') || '').startsWith('repair-preview-');
        return {
          id: cell.getAttribute('id'),
          type: 'flow',
          position: {
            x: Number(geometry?.getAttribute('x')) || 0,
            y: Number(geometry?.getAttribute('y')) || 0,
          },
          data: {
            kind: REPAIR_NODE_KINDS.includes(rawKind) ? rawKind : 'action',
            label: cell.getAttribute('value') || '',
            disabled: true,
          },
          className: missing ? 'flowchart-repair-missing-node'
            : REPAIR_NODE_KINDS.includes(rawKind) ? '' : 'flowchart-repair-invalid-node',
        };
      });
    const nodeIds = new Set(nodes.map((node) => node.id));
    const edges = cells.filter((cell) => cell.getAttribute('edge') === '1'
      && nodeIds.has(cell.getAttribute('source')) && nodeIds.has(cell.getAttribute('target')))
      .map((cell) => ({
        id: cell.getAttribute('id'),
        source: cell.getAttribute('source'),
        target: cell.getAttribute('target'),
        label: cell.getAttribute('value') || '',
        type: 'smoothstep',
        className: 'flowchart-editor-edge',
      }));
    return {
      name: model.parentNode?.getAttribute('name') || `第 ${index + 1} 页`,
      nodes,
      edges,
    };
  });
}
