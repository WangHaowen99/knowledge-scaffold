import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import test from 'node:test';

/**
 * 用户可见文案的术语守卫。
 *
 * 这个仓库已经两次出现同一个东西两种叫法：先是「受控修复 / 逐项修复」靠前端 replaceAll
 * 白名单硬盖（还漏了 run 的 intent），后来我自己加「阶段」时又和存量的「里程碑」并存。
 * 统一一次不难，难的是不退化——所以用测试盯着，而不是靠约定。
 */

/** 旧词 → 应该用的说法。只管用户读得到的字符串 */
const BANNED: Array<[RegExp, string]> = [
  [/里程碑/, '阶段'],
  [/阻断错误|阻断问题/, '必须修复的错误 / 必须修复的问题'],
  [/非阻断警告/, '可暂缓的提醒'],
  [/受控修复/, '逐项修复'],
  [/候选修订/, '待确认的修改'],
  [/\boperation\b/, '业务操作'],
];

/**
 * 发给模型的提示词不在管辖范围：那里的 operation / rule / metric 是 schema 术语，
 * 改掉会影响模型行为。用这些标记识别提示词所在行。
 */
const PROMPT_MARKERS = [
  'add_', 'modify_', 'submit_', 'kind 取值', '你是知识包', '你是评测', '你是知识材料',
  '判定规则：', 'boundOperation', 'operations/index.md', 'business_data_search',
  'competencyQuestionId', 'ontology_context',
];

const SCAN_DIRS = [
  'src/ai-construction', 'src/authoring', 'frontend/src',
];
/** 这些文件整体是提示词或纯内部实现，不面向用户 */
const SKIP_FILES = new Set(['deterministic-capability.ts']);

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) { out.push(...sourceFiles(full)); continue; }
    if (!/\.(ts|js|jsx)$/.test(entry) || entry.includes('.test.')) continue;
    out.push(full);
  }
  return out;
}

/** 抓行内的字符串字面量；注释行整行跳过 */
function userFacingStrings(line: string): string[] {
  const trimmed = line.trim();
  if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) return [];
  if (PROMPT_MARKERS.some((marker) => line.includes(marker))) return [];
  // 兼容历史消息的改写调用必须写出旧词才能替换掉它，豁免
  if (line.includes('.replaceAll(')) return [];
  return [...line.matchAll(/'([^']{2,})'|"([^"]{2,})"/g)]
    .map((match) => match[1] ?? match[2])
    .filter((value) => /[一-龥]/.test(value));
}

test('用户可见文案不使用已经统一掉的旧术语', () => {
  const offenders: string[] = [];
  for (const dir of SCAN_DIRS) {
    for (const file of sourceFiles(dir)) {
      if (SKIP_FILES.has(file.split('/').pop()!)) continue;
      readFileSync(file, 'utf8').split('\n').forEach((line, index) => {
        for (const value of userFacingStrings(line)) {
          for (const [pattern, suggestion] of BANNED) {
            if (pattern.test(value)) {
              offenders.push(`${file}:${index + 1}  「${value.slice(0, 46)}」→ 应使用「${suggestion}」`);
            }
          }
        }
      });
    }
  }
  assert.deepEqual(offenders, [], `以下用户可见文案仍在使用旧术语：\n${offenders.join('\n')}`);
});

test('术语表本身不自相矛盾——新词不能又是别的条目的旧词', () => {
  const replacements = BANNED.map(([, suggestion]) => suggestion).join(' ');
  for (const [pattern] of BANNED) {
    assert.equal(pattern.test(replacements), false,
      `建议用词里出现了被禁的旧词：${pattern}`);
  }
});
