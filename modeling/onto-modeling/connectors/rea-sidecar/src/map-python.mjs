// inspect-python-pyc 的结果 → 标准记录：模块、类、函数/方法，以及代码里的 URL 字面量。
// REA 不反编译字节码，拿不到真实调用；字段按 __init__ 引用的属性名推测，URL 只知道出现在哪个函数的常量里，都记 inferred。
import { endpointName, isHttpUrl, stripQuery } from './urls.mjs';

const CO_NEWLOCALS = 0x2;
const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;
// __init__ 的 names 里除了 self.x 的属性名，还有用到的全局名和内置函数，这些不算字段
const BUILTINS = new Set([
  'print', 'len', 'super', 'isinstance', 'issubclass', 'str', 'int', 'float', 'bool', 'bytes', 'dict', 'list', 'set',
  'tuple', 'range', 'enumerate', 'zip', 'map', 'filter', 'getattr', 'setattr', 'hasattr', 'open', 'type', 'object',
  'min', 'max', 'sum', 'sorted', 'any', 'all', 'abs', 'round', 'repr', 'iter', 'next', 'Exception', 'ValueError',
  'TypeError', 'KeyError', 'RuntimeError', 'NotImplementedError', 'property', 'staticmethod', 'classmethod',
]);

/** 嵌套的 code object 按 nested_code 下标挂在父对象下面；`<module>` 是根。 */
function walk(codeObjects, visit) {
  const root = codeObjects.findIndex((code) => code?.name === '<module>');
  if (root < 0) return;
  const seen = new Set();
  const step = (index, parent) => {
    if (seen.has(index) || !codeObjects[index]) return;
    seen.add(index);
    const context = visit(codeObjects[index], parent);
    if (context === null) return;
    for (const child of codeObjects[index].nested_code ?? []) step(child, context);
  };
  step(root, null);
}

/**
 * module：点分模块名（PyInstaller 主脚本就是脚本名）；file：清单里的相对路径。
 * 返回模块锚点和模块顶层引用的名字（调用方据此在已分析的模块之间连 imports）。
 */
export function mapPythonPyc(evidence, records, { module, file, method = 'rea inspect-python-pyc' }) {
  const result = evidence?.normalized_result ?? {};
  const codeObjects = result.code_objects ?? [];
  const segments = module.split('.').filter(Boolean);
  const add = (spec) => records.add({ method, ...spec });
  let moduleAnchor = null;
  let names = [];
  const addUrls = (code, context) => {
    for (const constant of code.consts ?? []) {
      if (constant?.kind !== 'str' || !isHttpUrl(constant.value)) continue;
      const url = stripQuery(constant.value);
      const ext = add({
        path: 'ext', segments: [endpointName(null, url)], type: 'entity', name: url,
        attrs: { kind: 'endpoint', url, method: null }, confidence: 'inferred', dedupe: true,
      });
      records.relate({ from: context.anchor, name: 'requests', to: ext, attrs: { url, method: null }, method });
    }
    return context;
  };

  walk(codeObjects, (code, parent) => {
    const sourceFile = typeof code.filename === 'string' ? code.filename : file;
    let context;
    if (!parent) {
      moduleAnchor = add({
        path: 'mod', segments, type: 'entity', name: module,
        attrs: { kind: 'module', loc: { file: sourceFile, line: 1 }, module, pyc: file },
      });
      names = (code.names ?? []).filter((name) => typeof name === 'string');
      context = { anchor: moduleAnchor, kind: 'module', path: [] };
    } else {
      // lambda、推导式、<locals> 里的闭包不单列
      if (typeof code.name !== 'string' || code.name.startsWith('<') || String(code.qualname).includes('<')) return null;
      const isFunction = (code.flags & CO_NEWLOCALS) !== 0;
      const path = [...parent.path, code.name];
      const kind = isFunction ? (parent.kind === 'class' ? 'method' : 'function') : 'class';
      if (kind === 'method' && code.name === '__init__') {
        // 构造函数不单列，它引用的属性名当作类的字段
        const skip = new Set([...names, ...(parent.names ?? [])]);
        for (const name of code.names ?? []) {
          if (typeof name !== 'string' || !IDENTIFIER.test(name) || name.startsWith('__') || skip.has(name)
            || BUILTINS.has(name)) continue;
          add({
            path: 'sym', segments: [...segments, ...parent.path, name], type: 'field', parent: parent.anchor, name,
            attrs: { kind: 'property', loc: { file: sourceFile, line: code.firstlineno ?? null }, module },
            confidence: 'inferred', dedupe: true,
          });
        }
        context = { ...parent, path };
        return addUrls(code, context);
      }
      const anchor = add({
        path: 'sym', segments: [...segments, ...path], type: isFunction ? 'operation' : 'entity', parent: parent.anchor,
        name: code.name,
        attrs: { kind, loc: { file: sourceFile, line: Number.isSafeInteger(code.firstlineno) ? code.firstlineno : null }, module },
      });
      context = { anchor, kind, path, names: (code.names ?? []).filter((name) => typeof name === 'string') };
    }
    return addUrls(code, context);
  });

  return { anchor: moduleAnchor, names, limitations: evidence?.limitations ?? [] };
}

/** 松散 pyc 的模块名：去掉 __pycache__ 和 .cpython-311 这类标签，__init__ 归到包名。 */
export function pycModuleName(rel) {
  const parts = rel.split('/').filter((part) => part && part !== '__pycache__');
  const last = parts.pop().replace(/\.pyc$/i, '').replace(/\.[a-z]+-\d+(\.opt-\d+)?$/i, '');
  if (last !== '__init__') parts.push(last);
  return parts.join('.') || last;
}
