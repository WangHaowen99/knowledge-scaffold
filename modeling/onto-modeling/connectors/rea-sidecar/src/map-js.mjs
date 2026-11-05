// analyze-javascript-application 的结果 → 标准记录。
// 只取模块、类、函数/方法、构造函数里写的字段、IPC handler、contextBridge 暴露的接口、路由、带字面量 URL 的请求；
// 表达式级的节点（binding、call-site、promise……）留在原始结果里，不进记录。
import { binAnchor } from './anchors.mjs';
import { endpointName, httpMethod, stripQuery } from './urls.mjs';

const CALLABLE = /^callable:callable:(class|method|function):\d+:\d+$/;
const OBJECT_WRITE = /^object:write:\d+:\d+$/;
const PROGRAM_LITERAL = /^literal:scope:program:\d+:\d+:binding:([^:]+):/;

const firstObservation = (node) => node?.observations?.[0] ?? {};
const nearestAnchor = (item) => {
  for (let current = item; current; current = current.container) if (current.anchor) return current.anchor;
  return null;
};
const confidenceOf = (state) => (state === 'observed' ? 'observed' : 'inferred');

function graphLocation(node) {
  const value = firstObservation(node).evidence?.location?.value ?? {};
  return { file: value.source ?? value.path ?? null, line: value.start?.line ?? null, column: value.start?.column ?? 0 };
}

const position = (point) => [point?.line ?? 0, point?.column ?? 0];
const before = (a, b) => a[0] < b[0] || (a[0] === b[0] && a[1] <= b[1]);
const containsRange = (outer, inner) => before(outer.start, inner.start) && before(inner.end, outer.end);
const compareText = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

/**
 * 一个模块里的具名可调用体。函数和类的范围只会嵌套或不相交，所以排序后用栈求每项的外层，
 * 查某个位置所在的最内层时二分到「起点不晚于它的最后一项」再沿外层往上找；打包后的大文件也不会退化成平方。
 */
class Scopes {
  constructor() {
    this.byModule = new Map();
  }

  add(item) {
    if (!this.byModule.has(item.module)) this.byModule.set(item.module, []);
    this.byModule.get(item.module).push(item);
  }

  sort() {
    for (const items of this.byModule.values()) {
      items.sort((a, b) => a.start[0] - b.start[0] || a.start[1] - b.start[1] || (before(a.end, b.end) ? 1 : -1));
      const stack = [];
      for (const item of items) {
        while (stack.length && !containsRange(stack.at(-1), item)) stack.pop();
        item.container = stack.at(-1) ?? null;
        stack.push(item);
      }
    }
  }

  innermost(module, range, predicate = () => true) {
    const items = this.byModule.get(module);
    if (!items?.length) return null;
    const target = { start: position(range.start), end: position(range.end) };
    let low = 0;
    let high = items.length - 1;
    let index = -1;
    while (low <= high) {
      const middle = (low + high) >> 1;
      if (before(items[middle].start, target.start)) {
        index = middle;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    for (let item = items[index]; item; item = item.container) {
      if (item.range !== range && containsRange(item, target) && predicate(item)) return item;
    }
    return null;
  }
}

export function mapJavascript(evidence, records, { method = 'rea analyze-javascript-application' } = {}) {
  const result = evidence?.normalized_result ?? {};
  const graphNodes = result.graph?.nodes ?? [];
  const graphEdges = result.graph?.edges ?? [];
  const semantic = result.semantic_graph ?? {};
  const semanticNodes = semantic.nodes ?? [];
  const add = (spec) => records.add({ method, ...spec });
  const relate = (spec) => records.relate({ method, ...spec });

  // 模块
  const modules = new Map();
  const moduleNodes = new Map();
  const modulePaths = new Set();
  for (const node of graphNodes) {
    const props = firstObservation(node).properties ?? {};
    if (node.kind === 'javascript-module' && props.semantic_role === 'source-module') {
      const path = props.file_occurrence?.path ?? firstObservation(node).label;
      if (path) {
        modulePaths.add(path);
        moduleNodes.set(node.node_id, path);
      }
    }
  }
  for (const node of semanticNodes) {
    if (node.kind === 'module' && node.identity?.module_path) modulePaths.add(node.identity.module_path);
  }
  for (const path of [...modulePaths].sort()) {
    modules.set(path, add({
      path: 'mod', segments: [path], type: 'entity', name: path,
      attrs: { kind: 'module', loc: { file: path, line: 1 }, module: path },
    }));
  }
  const moduleAnchor = (path) => (path ? modules.get(path) ?? null : null);

  // 类、方法、函数：按源码位置排，外层先建，里层的 parent 才有锚点
  const scopes = new Scopes();
  const callables = [];
  for (const node of semanticNodes) {
    const match = node.kind === 'function' && CALLABLE.exec(node.identity?.role_key ?? '');
    const range = node.identity?.source_range;
    const name = node.properties?.name ?? null;
    if (!match || !range || !name || !node.identity.module_path) continue;
    const item = {
      node, kind: match[1], name, module: node.identity.module_path,
      start: position(range.start), end: position(range.end), range,
    };
    scopes.add(item);
    callables.push(item);
  }
  scopes.sort();
  const anchorByNode = new Map();
  for (const items of scopes.byModule.values()) {
    for (const item of items) {
      const { container } = item;
      item.path = container ? [...container.path, item.name] : [item.name];
      if (item.kind === 'method' && item.name === 'constructor') continue;
      const isClass = item.kind === 'class';
      const kind = isClass ? 'class' : (item.kind === 'method' && container?.kind === 'class' ? 'method' : 'function');
      item.anchor = add({
        path: 'sym', segments: [item.module, ...item.path], type: isClass ? 'entity' : 'operation',
        parent: nearestAnchor(container) ?? moduleAnchor(item.module), name: item.name,
        attrs: { kind, loc: { file: item.module, line: item.start[0] }, module: item.module },
      });
      anchorByNode.set(item.node.node_id, item.anchor);
    }
  }
  const ownerAnchor = (module, range) => {
    const owner = scopes.innermost(module, range, (item) => item.anchor);
    return owner?.anchor ?? moduleAnchor(module);
  };

  // 字段：类构造函数里 `this.x = …` 一类的写入；REA 不区分写入对象是不是 this，所以记 inferred
  for (const node of semanticNodes) {
    const props = node.properties ?? {};
    const range = node.identity?.source_range;
    if (node.kind !== 'expression' || !OBJECT_WRITE.test(node.identity?.role_key ?? '') || !range) continue;
    if (props.property_path?.length !== 1) continue;
    const module = node.identity.module_path;
    const ctor = scopes.innermost(module, range, (item) => item.kind === 'method');
    if (!ctor || ctor.name !== 'constructor' || ctor.container?.kind !== 'class' || !ctor.container.anchor) continue;
    const name = props.property_path[0];
    add({
      path: 'sym', segments: [module, ...ctor.container.path, name], type: 'field', parent: ctor.container.anchor, name,
      attrs: { kind: 'property', loc: { file: module, line: range.start.line }, module },
      confidence: 'inferred', dedupe: true,
    });
  }

  // 同一模块顶层字符串常量，用来补全 `${BASE}/orders` 这类模板
  const literals = new Map();
  for (const node of semanticNodes) {
    const match = node.kind === 'literal' && PROGRAM_LITERAL.exec(node.identity?.role_key ?? '');
    if (!match || typeof node.properties?.value !== 'string') continue;
    const module = node.identity.module_path;
    if (!literals.has(module)) literals.set(module, new Map());
    if (!literals.get(module).has(match[1])) literals.get(module).set(match[1], node.properties.value);
  }

  // IPC：主进程一侧的 handler，按通道去重
  let electron = false;
  const sorted = [...graphNodes].sort((a, b) => {
    const la = graphLocation(a);
    const lb = graphLocation(b);
    return compareText(String(la.file), String(lb.file)) || (la.line ?? 0) - (lb.line ?? 0) || la.column - lb.column;
  });
  for (const node of sorted) {
    const observation = firstObservation(node);
    const props = observation.properties ?? {};
    if (/^(electron-|ipc-|context-bridge-api|browser-window)/.test(node.kind)) electron = true;
    if (node.kind !== 'ipc-handler' || props.side !== 'main' || typeof props.channel !== 'string') continue;
    const loc = graphLocation(node);
    add({
      path: 'ipc', segments: [props.channel], type: 'operation', parent: moduleAnchor(loc.file), name: props.channel,
      attrs: { kind: 'ipc', channel: props.channel, mode: props.mode ?? null, loc: { file: loc.file, line: loc.line }, module: loc.file },
      confidence: confidenceOf(observation.evidence?.state), dedupe: true,
    });
  }

  // contextBridge：每个暴露的成员一条，能追到 IPC 通道的连一条 calls
  const bridgeChannels = new Map();
  for (const node of graphNodes) {
    const props = firstObservation(node).properties ?? {};
    if (props.entry_kind === 'bridge-member' && typeof props.entry === 'string' && typeof props.channel === 'string') {
      if (!bridgeChannels.has(props.entry)) bridgeChannels.set(props.entry, props.channel);
    }
  }
  for (const node of sorted) {
    const observation = firstObservation(node);
    const props = observation.properties ?? {};
    if (node.kind !== 'context-bridge-api' || typeof props.api_key !== 'string') continue;
    const loc = graphLocation(node);
    for (const member of [...(props.members ?? [])].sort()) {
      if (typeof member !== 'string') continue;
      const channel = bridgeChannels.get(`${props.api_key}.${member}`) ?? null;
      const anchor = add({
        path: 'bridge', segments: [props.api_key, member], type: 'operation', parent: moduleAnchor(loc.file),
        name: `${props.api_key}.${member}`,
        attrs: { kind: 'bridge', api: props.api_key, channel, loc: { file: loc.file, line: loc.line }, module: loc.file },
        confidence: confidenceOf(observation.evidence?.state), dedupe: true,
      });
      const ipc = channel ? binAnchor(records.source, 'ipc', channel) : null;
      if (ipc && records.has(ipc)) relate({ from: anchor, name: 'calls', to: ipc, attrs: { channel } });
    }
  }

  // 路由与外部接口
  for (const node of sorted) {
    const observation = firstObservation(node);
    const props = observation.properties ?? {};
    if (node.kind !== 'endpoint' || typeof props.value !== 'string') continue;
    const loc = graphLocation(node);
    const verb = httpMethod(props.mechanism);
    const candidate = props.interpretation === 'candidate' || props.resolution === 'partial'
      || observation.evidence?.state !== 'observed';
    if (props.endpoint_kind === 'route') {
      const path = stripQuery(props.value);
      add({
        path: 'route', segments: [endpointName(verb, path)], type: 'operation', parent: moduleAnchor(loc.file),
        name: endpointName(verb, path),
        attrs: { kind: 'route', path, method: verb, loc: { file: loc.file, line: loc.line }, module: loc.file },
        confidence: candidate ? 'inferred' : 'observed', dedupe: true,
      });
    } else if (props.endpoint_kind === 'network') {
      const { url, substituted } = resolveTemplate(props.value, literals.get(loc.file));
      const name = endpointName(verb, url);
      const ext = add({
        path: 'ext', segments: [name], type: 'entity', name,
        attrs: { kind: 'endpoint', url, method: verb },
        confidence: candidate || substituted ? 'inferred' : 'observed', dedupe: true,
      });
      const at = { start: { line: loc.line, column: loc.column }, end: { line: loc.line, column: loc.column } };
      relate({
        from: ownerAnchor(loc.file, at), name: 'requests', to: ext,
        attrs: { url, method: verb, loc: { file: loc.file, line: loc.line } },
      });
    }
  }
  for (const route of result.route_inventory?.routes ?? []) {
    if (typeof route.path !== 'string') continue;
    const path = stripQuery(route.path);
    add({
      path: 'route', segments: [path], type: 'operation', name: path,
      attrs: { kind: 'route', path, method: null }, confidence: 'inferred', dedupe: true,
    });
  }

  // 调用：call-site → 已建记录的函数或类；调用方取 call-site 所在的最内层具名函数
  const semanticById = new Map(semanticNodes.map((node) => [node.node_id, node]));
  for (const relation of semantic.relations ?? []) {
    if (relation.relation !== 'calls') continue;
    const to = anchorByNode.get(relation.target_node_id);
    const site = semanticById.get(relation.source_node_id);
    const range = site?.identity?.source_range;
    if (!to || site?.kind !== 'call-site' || !range) continue;
    relate({
      from: ownerAnchor(site.identity.module_path, range), name: 'calls', to,
      attrs: { loc: { file: site.identity.module_path, line: range.start.line } },
      confidence: relation.resolution === 'resolved' ? 'observed' : 'inferred',
    });
  }

  // 模块之间的 require/import（只连包里有的模块）
  const assetPaths = new Map();
  for (const node of graphNodes) {
    const props = firstObservation(node).properties ?? {};
    if (node.kind === 'javascript-asset' && typeof props.path === 'string') assetPaths.set(node.node_id, props.path);
  }
  for (const edge of graphEdges) {
    if (edge.relation !== 'imports') continue;
    const from = moduleAnchor(moduleNodes.get(edge.source_node_id));
    const to = moduleAnchor(moduleNodes.get(edge.target_node_id) ?? assetPaths.get(edge.target_node_id));
    if (from && to) relate({ from, name: 'imports', to, confidence: confidenceOf(edge.evidence?.state) });
  }

  return {
    detected: electron ? 'electron' : 'javascript',
    unknowns: (semantic.unknowns ?? []).length,
    limitations: evidence?.limitations ?? [],
  };
}

/** 去掉模板/字符串的引号；`${NAME}` 能在同模块顶层常量里找到的就代入，找不到的写成 `{NAME}`。 */
export function resolveTemplate(raw, moduleLiterals = new Map()) {
  let text = String(raw).trim();
  if (/^`[\s\S]*`$/.test(text) || /^'[\s\S]*'$/.test(text) || /^"[\s\S]*"$/.test(text)) text = text.slice(1, -1);
  let substituted = false;
  text = text.replace(/\$\{\s*([^}]*?)\s*\}/g, (_whole, expression) => {
    substituted = true;
    return moduleLiterals.has(expression) ? moduleLiterals.get(expression) : `{${expression}}`;
  });
  return { url: stripQuery(text), substituted };
}
