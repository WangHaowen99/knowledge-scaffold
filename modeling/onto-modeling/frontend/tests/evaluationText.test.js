// 评测页面的纯函数（照 develop 的 CenterPane 报告预览、SolverModelPanel）；数据全部为合成
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  ANSWER_LINK, linkifyPaths, shortRevision, solverValidationText,
} from '../src/pages/evaluation/evaluationText.js';

test('workspace paths in report text become file links, code stays as is', () => {
  assert.equal(linkifyPaths('见 ontology/object/订单.md。'), '见 [ontology/object/订单.md](#file:ontology%2Fobject%2F%E8%AE%A2%E5%8D%95.md)。');
  assert.equal(linkifyPaths('`ontology/object/订单.md`'), '`ontology/object/订单.md`');
  assert.equal(linkifyPaths('```\nontology/object/订单.md\n```'), '```\nontology/object/订单.md\n```');
  assert.equal(linkifyPaths('[链接](ontology/object/订单.md)'), '[链接](ontology/object/订单.md)');
});

test('full answer links of a report carry the attachment and the result id', () => {
  assert.deepEqual([...ANSWER_LINK.exec('reports/eval-20261001-090000.answers.json#case-3')].slice(1),
    ['reports/eval-20261001-090000.answers.json', 'case-3']);
  assert.equal(ANSWER_LINK.exec('https://example.com/a.answers.json#x'), null);
});

test('the solver validation card says where the current revision stands', () => {
  const status = { configured: true, current_release_id: 'release-a', current_version: '0.1.0', solver_revisions: { 'release-a': 'rev-a' },
    solver_releases: { 'release-a': { status: 'completed', version: '0.1.0', solver_summary: { total: 1, passed: 1, failed: 0 } } } };
  assert.equal(solverValidationText(null, 'rev-a', false), '正在读取求解验证状态。');
  assert.equal(solverValidationText(status, 'rev-a', true), '有未保存的修改：保存并发布新版本后，再对新版本发起求解验证。');
  assert.equal(solverValidationText({ configured: false }, 'rev-a', false), '评测服务不可用或未配置，暂时读不到求解验证状态。');
  assert.equal(solverValidationText(status, 'rev-a', false), '当前修订已在 0.1.0 通过求解验证。');
  assert.equal(solverValidationText({ ...status, solver_releases: {} }, 'rev-a', false), '当前修订已发布，尚未通过求解验证：在评测页面对该版本发起求解验证。');
  assert.equal(solverValidationText(status, 'rev-b', false), '当前修改尚未发布和验证：发布新版本后，在发布页「已发布版本」里发起求解验证。');
  assert.equal(shortRevision('sha256:0123456789abcdef'), '0123456789');
  assert.equal(shortRevision(''), '—');
});
