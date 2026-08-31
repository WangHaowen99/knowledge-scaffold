import { parser as pythonParser } from '@lezer/python';

import type { SpecScript } from '../types.js';

export const DATA_PROVIDER_SCRIPT_CONTRACT = [
  'DataProviderClient.query(question) 和 query_sql(sql) 都只接受一个位置字符串参数，不支持 filters、limit 等关键字参数。',
  '两个方法都返回 ProviderResult，脚本必须先检查 result.success，失败时使用 result.error/result.message 报错，',
  '成功时只能从 result.data 解析行数据，不得直接迭代、取下标或对 ProviderResult 做真值判断。',
].join('');

const FORBIDDEN: { pattern: RegExp; label: string }[] = [
  { pattern: /\b(?:import|from)\s+subprocess\b|\bsubprocess\s*\./, label: 'subprocess' },
  { pattern: /\b(?:import|from)\s+(?:ctypes|multiprocessing|pickle|marshal)\b/, label: '高风险模块' },
  { pattern: /\b(?:import|from)\s+(?:requests|httpx|aiohttp|socket)\b|\bfrom\s+urllib\.request\b|\burlopen\s*\(/, label: '网络访问' },
  { pattern: /\bos\s*\.\s*(?:system|popen|spawn\w*)\s*\(/, label: '系统命令' },
  { pattern: /\bshutil\s*\.\s*rmtree\s*\(/, label: '递归删除' },
  { pattern: /\bopen\s*\([^\n]*,[^\n]*["'][wax+][^"']*["']/, label: '文件写入' },
];

const DYNAMIC_EXECUTION_FUNCTIONS = new Set(['eval', 'exec', 'compile', '__import__']);

/**
 * Detect calls to Python's dynamic-execution builtins without confusing
 * ordinary module methods such as re.compile() with the builtin compile().
 * The syntax tree also lets us keep qualified builtins calls blocked.
 */
function containsDynamicExecution(sourceCode: string): boolean {
  let found = false;
  pythonParser.parse(sourceCode).iterate({
    enter(node) {
      if (found) return false;
      if (node.name !== 'CallExpression') return;

      const callee = node.node.firstChild;
      if (!callee) return;
      if (callee.name === 'VariableName') {
        found = DYNAMIC_EXECUTION_FUNCTIONS.has(sourceCode.slice(callee.from, callee.to));
        return;
      }
      if (callee.name !== 'MemberExpression') return;

      const receiver = callee.firstChild;
      const property = callee.lastChild;
      if (receiver?.name !== 'VariableName' || property?.name !== 'PropertyName') return;
      const receiverName = sourceCode.slice(receiver.from, receiver.to);
      const propertyName = sourceCode.slice(property.from, property.to);
      found = (receiverName === 'builtins' || receiverName === '__builtins__')
        && DYNAMIC_EXECUTION_FUNCTIONS.has(propertyName);
    },
  });
  return found;
}

export function validateGeneratedScriptSecurity(script: SpecScript): void {
  if (script.sourceCode.length > 100_000) {
    throw new Error(`脚本安全检查失败（${script.key}）：源码超过 100000 字符`);
  }
  for (const rule of FORBIDDEN) {
    if (rule.pattern.test(script.sourceCode)) {
      throw new Error(`脚本安全检查失败（${script.key}）：禁止使用 ${rule.label}`);
    }
  }
  if (containsDynamicExecution(script.sourceCode)) {
    throw new Error(`脚本安全检查失败（${script.key}）：禁止使用 动态执行`);
  }
}

export function validateGeneratedScriptSyntax(script: SpecScript): void {
  let firstError: { from: number; to: number } | undefined;
  pythonParser.parse(script.sourceCode).iterate({
    enter(node) {
      if (!firstError && node.type.isError) firstError = { from: node.from, to: node.to };
    },
  });
  if (!firstError) return;
  const prefix = script.sourceCode.slice(0, firstError.from);
  const line = prefix.split('\n').length;
  const lastNewline = prefix.lastIndexOf('\n');
  const column = firstError.from - lastNewline;
  throw new Error(`Python 脚本语法检查失败（${script.key}）：第 ${line} 行第 ${column} 列附近存在无效语法`);
}

export function validateGeneratedScriptExecutionContract(script: SpecScript): void {
  const source = script.sourceCode;
  if (/hello[ _-]?world/iu.test(source)
    || /^\s*pass\s*(?:#.*)?$/mu.test(source)
    || /\b(?:TODO|FIXME)\b/u.test(source)) {
    throw new Error(`脚本执行契约检查失败（${script.key}）：包含演示或占位实现`);
  }
  if (/\b(?:raise\s+)?NotImplementedError\b/.test(source)) {
    throw new Error(`脚本执行契约检查失败（${script.key}）：包含 NotImplementedError 占位数据接口`);
  }
  if (!/^\s*(?:import\s+argparse|from\s+argparse\s+import\s+)/m.test(source)
    || !/\bArgumentParser\s*\(/.test(source)
    || !/\.parse_args\s*\(/.test(source)) {
    throw new Error(`脚本执行契约检查失败（${script.key}）：缺少 argparse CLI 入口`);
  }
  if (!/\bjson\.dumps\s*\(/.test(source)) {
    throw new Error(`脚本执行契约检查失败（${script.key}）：必须向标准输出返回结构化 JSON`);
  }
  if (/\bDataProviderClient\b/.test(source) && /\.(?:query|query_sql)\s*\(/.test(source)) {
    if (/\.(?:query|query_sql)\s*\([^)]*\b[A-Za-z_]\w*\s*=/s.test(source)) {
      throw new Error(`脚本执行契约检查失败（${script.key}）：${DATA_PROVIDER_SCRIPT_CONTRACT}`);
    }
    const assignments = [...source.matchAll(
      /\b([A-Za-z_]\w*)\s*=\s*(?:[A-Za-z_]\w*\.)+(?:query|query_sql)\s*\(/g,
    )].map((match) => match[1]);
    if (!assignments.length) {
      throw new Error(`脚本执行契约检查失败（${script.key}）：${DATA_PROVIDER_SCRIPT_CONTRACT}`);
    }
    for (const resultName of assignments) {
      const escaped = resultName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      if (!new RegExp(`\\b${escaped}\\.success\\b`).test(source)
        || !new RegExp(`\\b${escaped}\\.data\\b`).test(source)) {
        throw new Error(`脚本执行契约检查失败（${script.key}）：${DATA_PROVIDER_SCRIPT_CONTRACT}`);
      }
    }
  }
  for (const parameter of script.parameters) {
    const flag = String(parameter.flag || '').trim();
    if (!flag) {
      throw new Error(`脚本执行契约检查失败（${script.key}）：参数 ${parameter.name} 缺少 CLI flag`);
    }
    if (!source.includes(flag)) {
      throw new Error(`脚本执行契约检查失败（${script.key}）：源码未声明参数 ${flag}`);
    }
    const parameterType = String(parameter.type || 'string').trim().toLowerCase();
    const booleanParameter = parameterType === 'bool' || parameterType === 'boolean';
    const action = String(parameter.action || '').trim().toLowerCase();
    if (action && !booleanParameter) {
      throw new Error(`脚本执行契约检查失败（${script.key}）：${flag} 的 action 只适用于 boolean 参数`);
    }
    if (action && parameter.required) {
      throw new Error(`脚本执行契约检查失败（${script.key}）：${flag} 使用 ${action} 时不能设为必填`);
    }
    if (action && parameter.multiple) {
      throw new Error(`脚本执行契约检查失败（${script.key}）：${flag} 使用 ${action} 时不能设为多值`);
    }
    const actionDefault = action === 'store_true' ? false : action === 'store_false' ? true : undefined;
    if (actionDefault !== undefined
      && parameter.defaultValue !== undefined
      && parameter.defaultValue !== actionDefault) {
      throw new Error(
        `脚本执行契约检查失败（${script.key}）：${flag} 的默认值与 ${action} 不一致`,
      );
    }
    const escapedFlag = flag.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const declaration = source.match(new RegExp(
      `\\.add_argument\\s*\\(\\s*["']${escapedFlag}["'][\\s\\S]*?\\)`,
      'u',
    ))?.[0] ?? '';
    if (!declaration) {
      throw new Error(`脚本执行契约检查失败（${script.key}）：${flag} 未通过 argparse 声明`);
    }
    const sourceAction = declaration.match(/\baction\s*=\s*["'](store_true|store_false)["']/u)?.[1] ?? '';
    if (action && sourceAction !== action) {
      throw new Error(
        `脚本执行契约检查失败（${script.key}）：${flag} 的源码 action 与契约 ${action} 不一致`,
      );
    }
    if (!action && booleanParameter && sourceAction) {
      throw new Error(
        `脚本执行契约检查失败（${script.key}）：${flag} 是普通 boolean 契约，源码不能使用 ${sourceAction}`,
      );
    }
  }
  for (const output of script.outputs) {
    if (!source.includes(`"${output.key}"`) && !source.includes(`'${output.key}'`)) {
      throw new Error(
        `脚本执行契约检查失败（${script.key}）：源码没有输出契约字段 ${output.key}`,
      );
    }
  }
}

export function validateGeneratedScriptSafety(script: SpecScript): void {
  validateGeneratedScriptSyntax(script);
  validateGeneratedScriptSecurity(script);
  validateGeneratedScriptExecutionContract(script);
}
