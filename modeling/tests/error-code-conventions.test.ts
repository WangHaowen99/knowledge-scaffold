import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import test from 'node:test';

import { errorCodesWithNextActions } from '../src/error-next-actions.js';

const ROUTE_FILES = ['src/routes.ts', 'src/authoring/routes.ts'];

/** 抓所有 `code: 'XXX'` 字面量及其所在位置 */
function declaredCodes(): Array<{ code: string; where: string }> {
  const found: Array<{ code: string; where: string }> = [];
  for (const file of ROUTE_FILES) {
    const lines = readFileSync(join(process.cwd(), file), 'utf8').split('\n');
    lines.forEach((line, index) => {
      const match = /code: '([A-Za-z_]+)'/.exec(line);
      if (match) found.push({ code: match[1], where: `${file}:${index + 1}` });
    });
  }
  return found;
}

/**
 * 错误码必须全大写下划线。
 *
 * 出口是按 code 字面量查表挂上去的，大小写差一个字符就静默失效——看代码像是有处理，
 * 实际什么都没挂上。`revision_conflict` 写成小写就这么白白丢了出口，
 * 这类问题不靠约定，靠这条测试。
 */
test('错误码统一大写下划线，避免查表静默失效', () => {
  // 租约错误码来自 ProjectLeaseError，历史上就是小写，已在注册表里按原样登记
  const legacyLowercase = new Set(['busy', 'expired', 'not_owner', 'project_not_found', 'not_found']);
  const offenders = declaredCodes()
    .filter((item) => !legacyLowercase.has(item.code))
    .filter((item) => item.code !== item.code.toUpperCase());
  assert.deepEqual(offenders, [], `这些错误码不是全大写，出口会查不到：\n${
    offenders.map((item) => `  ${item.code} @ ${item.where}`).join('\n')}`);
});

test('同一个概念的两个错误码至少要给同样的出口', async () => {
  const codes = new Set(declaredCodes().map((item) => item.code));
  // PROJECT_BUSY 与 busy 是同一件事的两个码，暂未合并（busy 来自租约层且是小写）。
  // 底线是两者出口一致，否则用户撞上哪个码决定了他有没有路走。
  const { errorNextActions } = await import('../src/error-next-actions.js');
  if (codes.has('PROJECT_BUSY')) {
    assert.deepEqual(
      errorNextActions('PROJECT_BUSY')?.map((action) => action.id),
      errorNextActions('busy')?.map((action) => action.id),
      'PROJECT_BUSY 和 busy 的出口必须一致',
    );
  }
});

test('注册表里的码都实际出现在路由里，不留无人认领的登记', () => {
  const declared = new Set(declaredCodes().map((item) => item.code));
  // 这几个来自 lease / 状态机而不是路由字面量
  const fromOtherLayers = new Set(['busy', 'expired', 'REVISION_CONFLICT', 'PACKAGE_DRAFT_REQUIRES_PUBLISH']);
  const orphans = errorCodesWithNextActions()
    .filter((code) => !declared.has(code) && !fromOtherLayers.has(code));
  assert.deepEqual(orphans, [], `注册了出口但路由里没有该码：${orphans.join('、')}`);
});

test('流程图确认被拒的每种情况都有出口', async () => {
  const { errorNextActions } = await import('../src/error-next-actions.js');
  const { ASSISTANT_ACTION_KINDS } = await import('../src/ai-construction/assistant-actions.js');
  for (const code of [
    'FLOWCHART_DIRECT_EDIT_REQUIRED', 'FLOWCHART_DRAFT_OUTDATED', 'FLOWCHART_EMPTY_DOCUMENT',
    'FLOWCHART_WARNINGS_NOT_ACKNOWLEDGED', 'FLOWCHART_CANDIDATE_UNAVAILABLE',
  ]) {
    const actions = errorNextActions(code);
    assert.ok(actions?.length, `${code} 缺少出口——用户正画着流程图被拒，只给红字等于卡死`);
    assert.equal(actions![0].style, 'primary', `${code} 应有主操作`);
    for (const action of actions!) {
      assert.ok(ASSISTANT_ACTION_KINDS[action.id], `${code} 的出口 ${action.id} 未登记`);
    }
  }
});
