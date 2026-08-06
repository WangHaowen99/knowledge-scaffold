import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test, { afterEach } from 'node:test';

import {
  listCandidateRevisions,
  previewCandidateRevision,
  respondCandidateRevision,
} from '../frontend/src/authoring-api.js';
import CandidateRevisionPanel, {
  candidateRevisionAcceptedNotice,
  candidateRevisionVisibilityKey,
  CandidateRevisionSuccessNotice,
  respondCandidateWithRefresh,
  resolveCandidateGroupSelection,
} from '../frontend/src/CandidateRevisionPanel.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');
const originalFetch = globalThis.fetch;

afterEach(() => { globalThis.fetch = originalFetch; });

const groups = [
  { id: 'operation', summary: '补充审批操作', dependsOn: ['object'], changedPaths: ['operations/approve.md'], validation: { errors: [], warnings: [] } },
  { id: 'object', summary: '补充审批对象', dependsOn: [], changedPaths: ['objects/request.md'], validation: { errors: [], warnings: [] } },
  { id: 'rule', summary: '补充审批规则', dependsOn: ['operation'], changedPaths: ['rules/approve.md'], validation: { errors: [], warnings: ['口径待确认'] } },
];

test('原子变更组选择自动包含传递依赖', () => {
  assert.deepEqual(resolveCandidateGroupSelection(groups, ['rule']), {
    requestedGroupIds: ['rule'],
    selectedGroupIds: ['operation', 'object', 'rule'],
    autoIncludedGroupIds: ['operation', 'object'],
  });
});

test('候选列表或读取错误生成稳定的可见性键', () => {
  assert.equal(candidateRevisionVisibilityKey([], ''), '');
  assert.equal(candidateRevisionVisibilityKey([{ id: 'candidate-a' }], ''), 'candidates:candidate-a');
  assert.equal(
    candidateRevisionVisibilityKey([{ id: 'candidate-a' }, { id: 'candidate-b' }], ''),
    'candidates:candidate-a,candidate-b',
  );
  assert.equal(candidateRevisionVisibilityKey([], '读取失败'), 'error:读取失败');
  assert.equal(
    candidateRevisionVisibilityKey([], '', 'accepted:revision-a'),
    'notice:accepted:revision-a',
  );
  assert.equal(
    candidateRevisionVisibilityKey([{ id: 'candidate-a' }], '', 'accepted:revision-a'),
    'candidates:candidate-a|notice:accepted:revision-a',
  );
});

test('接受候选后生成知识包修订成功提示', () => {
  const notice = candidateRevisionAcceptedNotice({
    currentRevision: 'revision-a',
    packageVersion: '1.0.0-rc.5',
    candidate: { id: 'candidate-a', acceptedRevision: 'revision-a' },
  });
  assert.deepEqual(notice, {
    key: 'accepted:revision-a',
    title: '知识包已更新',
    detail: '所选修改已接受，新版本 v1.0.0-rc.5 已生成并设为当前内容。',
  });
  const html = renderToStaticMarkup(React.createElement(CandidateRevisionSuccessNotice, { notice }));
  assert.match(html, /role="status"/);
  assert.match(html, /知识包已更新/);
  assert.match(html, /新版本 v1\.0\.0-rc\.5 已生成/);
});

test('候选卡片提供预览、接受、退回和拒绝入口', () => {
  const html = renderToStaticMarkup(React.createElement(CandidateRevisionPanel, {
    projectId: 'project-a',
    candidates: [{
      id: 'candidate-a', status: 'pending', summary: '完善审批流程',
      baseRevision: 'revision-a', actor: { displayName: '知识包建模助手' }, groups,
    }],
  }));
  assert.match(html, /完善审批流程/);
  assert.match(html, /查看差异/);
  assert.match(html, /接受所选/);
  assert.match(html, /退回修改/);
  assert.match(html, /拒绝/);
  assert.match(html, /data-candidate-review="candidate-a"/);
});

test('候选接口按需加载预览并提交单一三态响应', async () => {
  const calls: Array<{ url: string; options: RequestInit }> = [];
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ candidates: [], files: [], candidate: { id: 'candidate-a' } }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;

  await listCandidateRevisions('项目/甲', 'pending');
  await previewCandidateRevision('项目/甲', 'candidate-a', ['rule']);
  await respondCandidateRevision('项目/甲', 'candidate-a', {
    action: 'accept', selectedGroupIds: ['rule'], note: '确认采用',
  });

  assert.equal(calls[0].url, '/api/projects/%E9%A1%B9%E7%9B%AE%2F%E7%94%B2/candidate-revisions?status=pending');
  assert.equal(calls[1].url, '/api/projects/%E9%A1%B9%E7%9B%AE%2F%E7%94%B2/candidate-revisions/candidate-a/preview?groups=rule');
  assert.equal(calls[2].options.body, JSON.stringify({
    action: 'accept', selectedGroupIds: ['rule'], note: '确认采用',
  }));
});

test('候选响应返回关闭候选的 409 时仍刷新列表并保留原始错误', async () => {
  let refreshed = 0;
  const conflict = Object.assign(new Error('候选已不再等待确认'), { status: 409 });
  await assert.rejects(
    () => respondCandidateWithRefresh(
      async () => { throw conflict; },
      async () => { refreshed += 1; },
    ),
    (error) => error === conflict,
  );
  assert.equal(refreshed, 1);
});
