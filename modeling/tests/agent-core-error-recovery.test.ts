import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

/* 拒绝类错误必须给出可执行的下一步。
   现场代价：目标草稿分段报「包含未知字段：points」却不说这一段允许哪些字段，
   助手连续 22 次 400 一直在猜；同类的还有 #23 的计划目标标识、#32 的字段哈希。
   信息缺口越小，模型越倾向反复尝试而不是放弃，所以空提示比想象中贵。

   这条测试是棘轮：只允许变好，不允许变差。新增不带提示的抛错点会直接失败。 */

/** 逐个解析 new AgentCoreError(...) 的前三个参数，返回恢复提示为空的行号。 */
function emptyRecoverySites(source: string): number[] {
  const sites: number[] = [];
  const marker = 'new AgentCoreError(';
  for (let index = source.indexOf(marker); index >= 0; index = source.indexOf(marker, index + 1)) {
    const start = index + marker.length;
    let depth = 1;
    let cursor = start;
    while (cursor < source.length && depth > 0) {
      if (source[cursor] === '(') depth += 1;
      else if (source[cursor] === ')') depth -= 1;
      cursor += 1;
    }
    const body = source.slice(start, cursor - 1);
    const parts: string[] = [];
    let nesting = 0;
    let current = '';
    for (const character of body) {
      if ('([{'.includes(character)) nesting += 1;
      else if (')]}'.includes(character)) nesting -= 1;
      if (character === ',' && nesting === 0) { parts.push(current); current = ''; } else current += character;
    }
    parts.push(current);
    if (parts.length < 3) continue;
    if (!['[]', '[ ]'].includes(parts[2].trim())) continue;
    sites.push(source.slice(0, index).split('\n').length);
  }
  return sites;
}

test('拒绝类错误都带可执行的恢复提示，且不允许新增空提示', () => {
  const source = readFileSync('src/agent/core-service.ts', 'utf8');
  const sites = emptyRecoverySites(source);
  assert.deepEqual(
    sites,
    [],
    `以下行的 AgentCoreError 没有恢复提示，模型收到「不对」却不知道下一步：${sites.join('、')}。`
    + '\n请补一条可执行的提示：能穷举取值的列出取值，找不到资源的说明用哪个工具查，'
    + '冲突类说明先重新读取再提交。不要把错误消息重说一遍。',
  );
});

test('恢复提示不是把错误消息重说一遍', () => {
  // 提示的价值在于「下一步做什么」。整条提示与消息完全相同说明没提供新信息。
  const source = readFileSync('src/agent/core-service.ts', 'utf8');
  const duplicated = [...source.matchAll(/new AgentCoreError\(\s*'[A-Z_]+',\s*'([^']+)',\s*\['([^']+)'\]/g)]
    .filter((match) => match[1].trim() === match[2].trim())
    .map((match) => match[1]);
  assert.deepEqual(duplicated, [], `这些提示只是复述了错误消息：${duplicated.join('；')}`);
});
