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
  { pattern: /\b(?:eval|exec|compile|__import__)\s*\(/, label: '动态执行' },
  { pattern: /\bos\s*\.\s*(?:system|popen|spawn\w*)\s*\(/, label: '系统命令' },
  { pattern: /\bshutil\s*\.\s*rmtree\s*\(/, label: '递归删除' },
  { pattern: /\bopen\s*\([^\n]*,[^\n]*["'][wax+][^"']*["']/, label: '文件写入' },
];

export function validateGeneratedScriptSecurity(script: SpecScript): void {
  if (script.sourceCode.length > 100_000) {
    throw new Error(`脚本安全检查失败（${script.key}）：源码超过 100000 字符`);
  }
  for (const rule of FORBIDDEN) {
    if (rule.pattern.test(script.sourceCode)) {
      throw new Error(`脚本安全检查失败（${script.key}）：禁止使用 ${rule.label}`);
    }
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
  }
}

export function validateGeneratedScriptSafety(script: SpecScript): void {
  validateGeneratedScriptSyntax(script);
  validateGeneratedScriptSecurity(script);
  validateGeneratedScriptExecutionContract(script);
}
