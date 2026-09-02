import { createHash } from 'node:crypto';

import { parser as pythonParser } from '@lezer/python';

export type PythonSourceEdit =
  | { kind: 'replace_function'; symbol: string; source: string; expectedNodeHash?: string }
  | { kind: 'replace_assignment'; symbol: string; source: string; expectedNodeHash?: string }
  | { kind: 'delete_function'; symbol: string; expectedNodeHash?: string }
  | { kind: 'delete_assignment'; symbol: string; expectedNodeHash?: string }
  | { kind: 'insert_before_function'; symbol: string; source: string; expectedNodeHash?: string };

export type PythonSourceSymbol = {
  kind: 'function' | 'assignment';
  qualifiedName: string;
  nodeHash: string;
  source: string;
  dependencies: string[];
  from: number;
  to: number;
};

export type PythonSourceIndex = {
  baseSourceHash: string;
  canonicalSourceHash: string;
  imports: string[];
  symbols: PythonSourceSymbol[];
};

type SourceNode = {
  kind: 'function' | 'assignment';
  symbol: string;
  from: number;
  to: number;
};

function normalizeSource(source: string): string {
  return `${source.replace(/\r\n?/gu, '\n').replace(/\n*$/u, '')}\n`;
}

export function pythonSourceHash(source: string): string {
  return digest(normalizeSource(source));
}

export function pythonPatchBaseHash(source: string): string {
  return digest(source);
}

function digest(value: string): string {
  return `sha256:${createHash('sha256').update(value).digest('hex')}`;
}

function directVariableName(node: any): any | undefined {
  let child = node.firstChild;
  while (child) {
    if (child.name === 'VariableName') return child;
    child = child.nextSibling;
  }
  return undefined;
}

function topLevelNodes(source: string): SourceNode[] {
  const tree = pythonParser.parse(source);
  const cursor = tree.cursor();
  const nodes: SourceNode[] = [];
  if (!cursor.firstChild()) return nodes;
  do {
    const kind = cursor.name === 'FunctionDefinition'
      ? 'function'
      : cursor.name === 'AssignStatement'
        ? 'assignment'
        : undefined;
    if (!kind) continue;
    const nameNode = directVariableName(cursor.node);
    if (!nameNode) continue;
    nodes.push({
      kind,
      symbol: source.slice(nameNode.from, nameNode.to),
      from: cursor.from,
      to: cursor.to,
    });
  } while (cursor.nextSibling());
  return nodes;
}

function topLevelImports(source: string): string[] {
  const cursor = pythonParser.parse(source).cursor();
  const imports: string[] = [];
  if (!cursor.firstChild()) return imports;
  do {
    if (cursor.name === 'ImportStatement') imports.push(source.slice(cursor.from, cursor.to));
  } while (cursor.nextSibling());
  return imports;
}

function variableNames(source: string, from: number, to: number): Set<string> {
  const names = new Set<string>();
  const tree = pythonParser.parse(source);
  tree.iterate({
    from,
    to,
    enter(node) {
      if (node.name === 'VariableName') names.add(source.slice(node.from, node.to));
    },
  });
  return names;
}

export function pythonSourceIdentifierNames(source: string): string[] {
  return [...variableNames(source, 0, source.length)].sort();
}

/**
 * Rename parsed Python identifiers while leaving strings and comments intact.
 * Optimizer helper names are assigned by the server, so references in a
 * submitted fragment must be rewritten through the Python syntax tree.
 */
export function rewritePythonIdentifiers(
  source: string,
  replacements: ReadonlyMap<string, string>,
): string {
  if (!replacements.size) return source;
  const changes: Array<{ from: number; to: number; value: string }> = [];
  const parents: Array<{ name: string; from: number }> = [];
  pythonParser.parse(source).iterate({
    enter(node) {
      if (node.name === 'VariableName') {
        const parent = parents[parents.length - 1];
        const current = source.slice(node.from, node.to);
        const replacement = replacements.get(current);
        // A helper name is a function definition or a call target. Avoid
        // renaming unrelated local variables, keyword arguments, attributes,
        // strings, or comments that happen to use the same temporary name.
        const isDefinition = parent?.name === 'FunctionDefinition';
        const isCallTarget = parent?.name === 'CallExpression' && parent.from === node.from;
        if (replacement && replacement !== current && (isDefinition || isCallTarget)) {
          changes.push({ from: node.from, to: node.to, value: replacement });
        }
      }
      parents.push({ name: node.name, from: node.from });
    },
    leave() {
      parents.pop();
    },
  });
  let rewritten = source;
  for (const change of changes.reverse()) {
    rewritten = `${rewritten.slice(0, change.from)}${change.value}${rewritten.slice(change.to)}`;
  }
  return rewritten;
}

function syntaxErrorAt(source: string): number | undefined {
  let offset: number | undefined;
  pythonParser.parse(source).iterate({
    enter(node) {
      if (offset === undefined && node.type.isError) offset = node.from;
    },
  });
  return offset;
}

function assertValidFragment(
  edit: PythonSourceEdit,
  expectedKind?: SourceNode['kind'],
  expectedSymbol?: string,
): string {
  if (!('source' in edit)) {
    throw new Error(`Python 增量补丁 ${edit.kind}:${edit.symbol} 不包含替换源码`);
  }
  const source = normalizeSource(edit.source);
  const errorOffset = syntaxErrorAt(source);
  if (errorOffset !== undefined) {
    throw new Error(`Python 增量补丁语法错误（${edit.kind}:${edit.symbol}，offset=${errorOffset}）`);
  }
  const nodes = topLevelNodes(source);
  if (edit.kind === 'insert_before_function') {
    if (!nodes.length || nodes.some((node) => node.kind !== 'function')) {
      throw new Error(`Python 增量补丁 ${edit.kind}:${edit.symbol} 只能插入顶层函数定义`);
    }
    return source;
  }
  if (nodes.length !== 1 || nodes[0].kind !== expectedKind || nodes[0].symbol !== expectedSymbol) {
    throw new Error(`Python 增量补丁 ${edit.kind}:${edit.symbol} 必须提交同名的单个顶层定义`);
  }
  return source;
}

export function inspectPythonSource(source: string): PythonSourceIndex {
  const errorOffset = syntaxErrorAt(source);
  if (errorOffset !== undefined) {
    throw new Error(`Python 源码语法错误（offset=${errorOffset}）`);
  }
  const nodes = topLevelNodes(source);
  const topLevelNames = new Set(nodes.map((node) => node.symbol));
  return {
    baseSourceHash: pythonPatchBaseHash(source),
    canonicalSourceHash: pythonSourceHash(source),
    imports: topLevelImports(source),
    symbols: nodes.map((node) => {
      const names = variableNames(source, node.from, node.to);
      names.delete(node.symbol);
      return {
        kind: node.kind,
        qualifiedName: node.symbol,
        nodeHash: digest(source.slice(node.from, node.to)),
        source: source.slice(node.from, node.to),
        dependencies: [...names].filter((name) => topLevelNames.has(name)).sort(),
        from: node.from,
        to: node.to,
      };
    }),
  };
}

export function selectPythonSourceSymbols(
  source: string,
  requestedSymbols: string[] = [],
): PythonSourceIndex {
  const index = inspectPythonSource(source);
  if (!requestedSymbols.length) return index;
  const byName = new Map(index.symbols.map((symbol) => [symbol.qualifiedName, symbol]));
  const selected = new Set<string>();
  for (const requested of requestedSymbols) {
    if (!byName.has(requested)) throw new Error(`Python 源码找不到顶层符号：${requested}`);
    selected.add(requested);
    for (const dependency of byName.get(requested)?.dependencies ?? []) selected.add(dependency);
  }
  return { ...index, symbols: [...selected].map((name) => byName.get(name)!) };
}

export function applyPythonSourceEdits(
  source: string,
  edits: PythonSourceEdit[],
  options: { expectedBaseSourceHash?: string; requireNodeHashes?: boolean } = {},
): string {
  if (!edits.length) throw new Error('Python 增量补丁没有包含任何编辑');
  const actualBaseSourceHash = pythonPatchBaseHash(source);
  if (options.expectedBaseSourceHash && options.expectedBaseSourceHash !== actualBaseSourceHash) {
    throw new Error(`Python 增量补丁基线源码已变化（expected=${options.expectedBaseSourceHash}, actual=${actualBaseSourceHash}）`);
  }
  const errorOffset = syntaxErrorAt(source);
  if (errorOffset !== undefined) throw new Error(`Python 源码语法错误（offset=${errorOffset}）`);
  const nodes = topLevelNodes(source);
  const existingFunctions = new Set(nodes.filter((node) => node.kind === 'function').map((node) => node.symbol));
  const occupied = new Set<string>();
  const replacements: Array<{
    from: number; to: number; source: string; insertion: boolean; order: number;
  }> = [];

  for (const [editOrder, edit] of edits.entries()) {
    const targetKind = edit.kind === 'replace_assignment' || edit.kind === 'delete_assignment'
      ? 'assignment'
      : 'function';
    const targets = nodes.filter((node) => node.kind === targetKind && node.symbol === edit.symbol);
    if (!targets.length) throw new Error(`Python 增量补丁找不到顶层 ${targetKind}:${edit.symbol}`);
    if (targets.length > 1) throw new Error(`Python 增量补丁目标不唯一 ${targetKind}:${edit.symbol}`);
    const target = targets[0];
    if (options.requireNodeHashes && !edit.expectedNodeHash) {
      throw new Error(`Python 增量补丁缺少目标节点哈希 ${targetKind}:${edit.symbol}`);
    }
    const actualNodeHash = digest(source.slice(target.from, target.to));
    if (edit.expectedNodeHash && edit.expectedNodeHash !== actualNodeHash) {
      throw new Error(`Python 增量补丁目标节点已变化 ${targetKind}:${edit.symbol}`);
    }
    if (edit.kind !== 'insert_before_function') {
      const targetId = `${targetKind}:${edit.symbol}`;
      if (occupied.has(targetId)) throw new Error(`Python 增量补丁重复编辑 ${targetId}`);
      occupied.add(targetId);
    }

    if (edit.kind === 'insert_before_function') {
      const fragment = assertValidFragment(edit);
      for (const node of topLevelNodes(fragment)) {
        if (existingFunctions.has(node.symbol)) {
          throw new Error(`Python 增量补丁不能重复新增函数 ${node.symbol}`);
        }
        existingFunctions.add(node.symbol);
      }
      replacements.push({
        from: target.from, to: target.from, source: `${fragment}\n`, insertion: true, order: editOrder,
      });
      continue;
    }

    if (edit.kind === 'delete_function' || edit.kind === 'delete_assignment') {
      replacements.push({
        from: target.from, to: target.to, source: '', insertion: false, order: editOrder,
      });
      continue;
    }

    replacements.push({
      from: target.from,
      to: target.to,
      source: `${assertValidFragment(edit, targetKind, edit.symbol).trimEnd()}\n`,
      insertion: false,
      order: editOrder,
    });
  }

  const ranged = replacements.filter((item) => item.to > item.from).sort((a, b) => a.from - b.from);
  for (let index = 1; index < ranged.length; index += 1) {
    if (ranged[index].from < ranged[index - 1].to) {
      throw new Error('Python 增量补丁包含重叠编辑');
    }
  }

  let result = source;
  replacements.sort((a, b) => b.from - a.from
    || Number(a.insertion) - Number(b.insertion)
    || (a.insertion && b.insertion ? b.order - a.order : 0));
  for (const replacement of replacements) {
    result = `${result.slice(0, replacement.from)}${replacement.source}${result.slice(replacement.to)}`;
  }
  const resultErrorOffset = syntaxErrorAt(result);
  if (resultErrorOffset !== undefined) {
    throw new Error(`Python 增量补丁应用后语法错误（offset=${resultErrorOffset}）`);
  }
  return result;
}
