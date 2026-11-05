// inspect-managed-members / inspect-managed-native-boundaries 的结果 → 标准记录。
// 程序集是模块，类型是 entity，方法是 operation，属性和字段是 field；继承、方法间调用、P/Invoke 是关系。
// 元数据 token 只在一次构建内有意义，不写进记录（否则每次重新编译所有记录的哈希都会变）。
import { endpointName, isHttpUrl, stripQuery } from './urls.mjs';

const TYPE_VISIBILITY = ['internal', 'public', 'public', 'private', 'protected', 'internal', 'private protected', 'protected internal'];
const MEMBER_VISIBILITY = ['private', 'private', 'private protected', 'internal', 'protected', 'protected internal', 'public', 'public'];
const TYPE_INTERFACE = 0x20;
const MEMBER_STATIC = 0x10;
const FIELD_LITERAL = 0x40;
const METHOD_SPECIAL_NAME = 0x800;
const PRIMITIVES = {
  void: 'void', bool: 'bool', char: 'char', i1: 'sbyte', u1: 'byte', i2: 'short', u2: 'ushort', i4: 'int', u4: 'uint',
  i8: 'long', u8: 'ulong', r4: 'float', r8: 'double', string: 'string', object: 'object', 'native-int': 'IntPtr',
  'native-uint': 'UIntPtr',
};

const rowOf = (token) => Number.parseInt(String(token).slice(4), 16);
const tableOf = (token) => String(token).slice(0, 4).toLowerCase();
const generated = (name) => typeof name !== 'string' || name.includes('<');

/** 签名里的 class:N / valuetype:N 是 TypeDefOrRef 编码；包内类型（TypeDef）换成全名，外部引用保留原样。 */
function typeName(raw, typeDefs) {
  const match = /^(class|valuetype):(\d+)$/.exec(String(raw));
  if (match) {
    const coded = Number(match[2]);
    if ((coded & 3) === 0 && typeDefs.has(coded >> 2)) return typeDefs.get(coded >> 2).full_name;
    return String(raw);
  }
  return PRIMITIVES[raw] ?? String(raw);
}

export function mapManagedMembers(evidence, records, { file, method = 'rea inspect-managed-members' }) {
  const result = evidence?.normalized_result ?? {};
  const assemblyName = result.assembly?.name ?? result.module?.name ?? file;
  const add = (spec) => records.add({ method, ...spec });
  const relate = (spec) => records.relate({ method, confidence: 'observed', ...spec });
  const loc = { file, line: null };
  const moduleAnchor = add({
    path: 'mod', segments: [assemblyName], type: 'entity', name: assemblyName,
    attrs: { kind: 'module', loc, module: assemblyName },
  });

  const types = (result.types ?? []).filter((type) => typeof type?.token === 'string');
  const typeDefs = new Map(types.map((type) => [rowOf(type.token), type]));
  const byToken = new Map(types.map((type) => [type.token, type]));
  const typeSegments = (type, depth = 0) => {
    const outer = type.enclosing_type_token ? byToken.get(type.enclosing_type_token) : null;
    if (outer && depth < 16) return [...typeSegments(outer, depth + 1), type.name];
    return [...String(type.namespace ?? '').split('.').filter(Boolean), type.name];
  };
  const fields = result.fields ?? [];
  const enumTypes = new Set(fields.filter((field) => field.name === 'value__').map((field) => field.declaring_type_token));

  // 外层类型先建，嵌套类型的 parent 才有锚点
  const typeAnchors = new Map();
  const ordered = types
    .filter((type) => !generated(type.name) && type.name !== '<Module>')
    .map((type) => ({ type, segments: typeSegments(type) }))
    .sort((a, b) => a.segments.length - b.segments.length || rowOf(a.type.token) - rowOf(b.type.token));
  for (const { type, segments } of ordered) {
    const flags = type.flags ?? 0;
    const outer = type.enclosing_type_token ? typeAnchors.get(type.enclosing_type_token) : null;
    const typeKind = enumTypes.has(type.token) ? 'enum' : (flags & TYPE_INTERFACE ? 'interface' : 'class');
    typeAnchors.set(type.token, add({
      path: 'sym', segments, type: 'entity', parent: outer ?? moduleAnchor, name: type.name,
      attrs: {
        kind: 'class', type_kind: typeKind, full_name: type.full_name, visibility: TYPE_VISIBILITY[flags & 7],
        loc, module: assemblyName,
      },
    }));
  }
  const segmentsOf = new Map(ordered.map(({ type, segments }) => [type.token, segments]));

  // 方法；get_/set_ 访问器收成属性，构造函数和编译器生成的不单列
  const methods = (result.methods ?? []).filter((item) => typeAnchors.has(item.declaring_type_token));
  const methodAnchors = new Map();
  const properties = new Map();
  const signatureOf = (item) => {
    const signature = item.signature ?? {};
    const parameters = (signature.parameter_types ?? []).map((raw) => typeName(raw, typeDefs));
    return `${item.name}(${parameters.join(', ')}): ${typeName(signature.return_type ?? 'void', typeDefs)}`;
  };
  for (const item of [...methods].sort((a, b) => rowOf(a.token) - rowOf(b.token))) {
    const flags = item.flags ?? 0;
    const accessor = /^(get|set)_(.+)$/.exec(item.name ?? '');
    if (accessor && flags & METHOD_SPECIAL_NAME) {
      const key = `${item.declaring_type_token}|${accessor[2]}`;
      if (!properties.has(key)) properties.set(key, { type: item.declaring_type_token, name: accessor[2], accessors: [] });
      properties.get(key).accessors.push({ kind: accessor[1], item });
      continue;
    }
    if (generated(item.name) || item.name === '.ctor' || item.name === '.cctor' || flags & METHOD_SPECIAL_NAME) continue;
    methodAnchors.set(item.token, { name: item.name, anchor: add({
      path: 'sym', segments: [...segmentsOf.get(item.declaring_type_token), item.name], type: 'operation',
      parent: typeAnchors.get(item.declaring_type_token), name: item.name,
      attrs: {
        kind: 'method', signature: signatureOf(item), visibility: MEMBER_VISIBILITY[flags & 7],
        static: Boolean(flags & MEMBER_STATIC), loc, module: assemblyName,
      },
    }) });
  }
  const methodAnchor = (token) => methodAnchors.get(token)?.anchor;
  for (const property of properties.values()) {
    const getter = property.accessors.find((accessor) => accessor.kind === 'get')?.item;
    const setter = property.accessors.find((accessor) => accessor.kind === 'set')?.item;
    const valueType = getter
      ? typeName(getter.signature?.return_type ?? '?', typeDefs)
      : typeName(setter?.signature?.parameter_types?.at(-1) ?? '?', typeDefs);
    const flags = (getter ?? setter).flags ?? 0;
    add({
      path: 'sym', segments: [...segmentsOf.get(property.type), property.name], type: 'field',
      parent: typeAnchors.get(property.type), name: property.name,
      attrs: {
        kind: 'property', signature: valueType, visibility: MEMBER_VISIBILITY[flags & 7],
        static: Boolean(flags & MEMBER_STATIC), readonly: !setter, loc, module: assemblyName,
      },
      dedupe: true,
    });
  }
  for (const field of fields) {
    if (!typeAnchors.has(field.declaring_type_token) || generated(field.name) || field.name === 'value__') continue;
    const flags = field.flags ?? 0;
    const isEnum = enumTypes.has(field.declaring_type_token);
    add({
      path: 'sym', segments: [...segmentsOf.get(field.declaring_type_token), field.name], type: 'field',
      parent: typeAnchors.get(field.declaring_type_token), name: field.name,
      attrs: {
        kind: isEnum ? 'enum_value' : (flags & FIELD_LITERAL ? 'constant' : 'field'),
        signature: isEnum ? null : typeName(field.signature?.field_type ?? '?', typeDefs),
        visibility: MEMBER_VISIBILITY[flags & 7], static: Boolean(flags & MEMBER_STATIC), loc, module: assemblyName,
      },
      dedupe: true,
    });
  }

  // 继承（只连包内类型）
  for (const { type } of ordered) {
    const base = type.extends_token ? typeAnchors.get(type.extends_token) : null;
    if (base) relate({ from: typeAnchors.get(type.token), name: 'inherits', to: base });
  }

  // 方法间调用：IL 里的 call/callvirt/newobj；构造函数算到类型上，访问器不连
  const methodByToken = new Map(methods.map((item) => [item.token, item]));
  for (const edge of result.call_edges ?? []) {
    const from = methodAnchor(edge.caller_token);
    if (!from || tableOf(edge.target_token) !== '0x06') continue;
    const target = methodByToken.get(edge.target_token);
    if (!target) continue;
    const to = target.name === '.ctor' ? typeAnchors.get(target.declaring_type_token) : methodAnchor(target.token);
    if (to) relate({ from, name: 'calls', to, attrs: { opcode: edge.opcode ?? null } });
  }

  // ldstr 里的 URL：找出加载它的方法，连一条 requests（只知道用到了这个地址，记 inferred）
  const urlTokens = new Map();
  for (const literal of result.user_strings?.literals ?? []) {
    if (typeof literal?.token === 'string' && isHttpUrl(literal.value)) urlTokens.set(literal.token, stripQuery(literal.value));
  }
  if (urlTokens.size) {
    for (const item of methods) {
      const from = methodAnchor(item.token);
      for (const anchor of item.body?.anchors ?? []) {
        const url = anchor.opcode === 'ldstr' ? urlTokens.get(anchor.operand) : null;
        if (!url) continue;
        const ext = add({
          path: 'ext', segments: [endpointName(null, url)], type: 'entity', name: url,
          attrs: { kind: 'endpoint', url, method: null }, confidence: 'inferred', dedupe: true,
        });
        if (from) relate({ from, name: 'requests', to: ext, attrs: { url, method: null }, confidence: 'inferred' });
      }
    }
  }

  return { moduleAnchor, methodAnchors, limitations: evidence?.limitations ?? [] };
}

/** P/Invoke：每个原生入口一条 entity（kind=native），声明它的方法连 invokes_native。 */
export function mapManagedNative(evidence, records, { methodAnchors = new Map(), method = 'rea inspect-managed-native-boundaries' }) {
  const result = evidence?.normalized_result ?? {};
  for (const item of result.pinvoke_imports ?? []) {
    const library = item.import_scope_name;
    const entry = item.import_name ?? item.member_name;
    if (typeof library !== 'string' || typeof entry !== 'string') continue;
    const native = records.add({
      path: 'native', segments: [library, entry], type: 'entity', name: `${library}!${entry}`, method,
      attrs: { kind: 'native', library, entry, char_set: item.char_set ?? null, call_convention: item.call_convention ?? null },
      dedupe: true,
    });
    // token 只在同一次构建内有意义；再核一遍方法名，防止两份结果来自不同文件时连错
    const declared = methodAnchors.get(item.member_token);
    const from = declared && declared.name === item.member_name ? declared.anchor : null;
    if (from) records.relate({ from, name: 'invokes_native', to: native, method, confidence: 'observed' });
  }
  return { limitations: evidence?.limitations ?? [] };
}
