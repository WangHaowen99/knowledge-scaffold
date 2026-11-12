// 知识包共享与删除项目时一并删除 Hub 包（develop !590，ADR-0098）：文字照 develop，「建模者」改成共享用户；
// mock 的删除预览与共享同步。全部为合成数据。
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register as registerPublish } from '../src/api/mock/routes-publish.js';
import {
  DELETE_TEXT, HUB_CHECKBOX, HUB_CONFIRM_TEXT, HUB_DELETE_BUTTON, HUB_USAGE_CHECKBOX, packageLabel, previewFailed, progressNote,
  unpublishedNote,
} from '../src/pages/projects/projectDeleteModel.js';
import {
  PUBLISH_FAILED_WARNING, SAVE_FAILED_WARNING, SHARING_NOTE, addProblem, pendingRevokes, syncFailed, userSync,
} from '../src/pages/publish/sharingModel.js';

test('删除确认：勾选项、没发布过与预览失败的说明、删除进度两种状态', () => {
  assert.equal(HUB_CHECKBOX, '同时删除本项目发布的知识包');
  assert.equal(unpublishedNote({ published: false }), '本项目尚未发布知识包');
  assert.equal(unpublishedNote(previewFailed('知识包 Hub 需要统一账号登录')), '知识包 Hub 需要统一账号登录');
  assert.equal(unpublishedNote({ published: true }), '');
  assert.equal(progressNote({ published: true, state: 'deleted' }), 'Hub 删除进度：知识包已删除，待完成项目删除');
  assert.equal(progressNote({ published: true, state: 'deleting' }), 'Hub 删除进度：结果待确认，请勾选后重试');
  assert.equal(progressNote({ published: true }), '');
  assert.match(DELETE_TEXT, /永久删除，无法恢复。$/);
});

test('二次确认：知识包名称与本体标识、使用情况确认、按钮文字照 develop', () => {
  assert.equal(packageLabel({ name: '门店补货', ontology_id: 'pkg-demo' }), '门店补货（pkg-demo）');
  assert.match(HUB_CONFIRM_TEXT, /^请确保消费和评测不再使用这些知识包。将物理删除全部正式版本、评测快照及共享资料/);
  assert.match(HUB_CONFIRM_TEXT, /已安装或下载的副本不会删除；系统未自动核实下游使用情况。$/);
  assert.equal(HUB_USAGE_CHECKBOX, '已确认消费和评测不再使用这些知识包');
  assert.equal(HUB_DELETE_BUTTON, '删除项目及知识包');
});

test('共享用户：添加的校验、失败原因与重试按钮的说法、待撤权单列', () => {
  assert.equal(SHARING_NOTE, '共享用户将获得 Hub 全部版本读写、授权管理和整包删除权限。移除会撤销该本体的整体授权，包括其他渠道授予的权限。');
  assert.equal(addProblem('', [], 'owner_a'), '请输入用户名');
  assert.equal(addProblem('a b', [], 'owner_a'), '用户名只能有字母、数字、点、下划线和短横线');
  assert.equal(addProblem('owner_a', [], 'owner_a'), '所有者已有全部权限');
  assert.equal(addProblem('user_b', ['user_b'], 'owner_a'), '已在共享名单里');
  assert.equal(addProblem('user_c', ['user_b'], 'owner_a'), '');
  const hubSync = { status: 'failed', pending: { user_b: 'grant', user_x: 'revoke' }, results: { user_b: { error: '目标账号不存在，无法同步 Hub 授权' } } };
  assert.deepEqual(userSync(hubSync, 'user_b'), { action: 'grant', reason: '目标账号不存在，无法同步 Hub 授权', retry: '重试授权' });
  assert.deepEqual(userSync(hubSync, 'user_x'), { action: 'revoke', reason: '授权变更尚未同步，请重试', retry: '重试撤权' });
  assert.equal(userSync(hubSync, 'user_c'), null);
  assert.deepEqual(pendingRevokes(hubSync, ['user_b']), ['user_x']);
  assert.equal(syncFailed(hubSync), true);
  assert.equal(SAVE_FAILED_WARNING, '共享用户已更新，Hub 授权同步失败；移除的用户可能仍可访问知识包');
  assert.equal(PUBLISH_FAILED_WARNING, '知识包已发布，共享用户授权同步失败，请在知识包共享中重试');
});

function mockServer(sc = {}) {
  const routes = [];
  const cards = F.projectCards();
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db: {}, sc, F, uid: () => '1',
    cardOf: (id) => cards.find((c) => c.id === id),
    scopeOf: () => ({ state: 'none', entries: [], artifacts: [], paths: [] }),
    convsOf: () => [], hub: { history: () => [] }, convKey: () => '', emitProj: () => {},
  };
  registerPublish(ctx);
  return async (method, path, body) => {
    const url = new URL(`http://mock${path}`);
    const route = routes.find((r) => r.method === method && r.re.test(url.pathname));
    const res = await route.handler({ m: url.pathname.match(route.re), query: url.searchParams, body: body || {}, init: {} });
    return { status: res.status, body: await res.json() };
  };
}

test('mock：删除预览与共享同步失败后按用户重试', async () => {
  const call = mockServer({ sharing: 'failed' });
  const P = `/api/v1/projects/${F.MAIN_PID}`;
  const preview = await call('GET', `${P}/hub/delete-preview`);
  assert.equal(preview.body.published, true);
  assert.ok(preview.body.ontology_id);
  const saved = await call('PUT', `${P}/hub/sharing`, { users: ['operator_b', 'operator_c'] });
  assert.deepEqual(saved.body.users, ['operator_b', 'operator_c']);
  assert.deepEqual(saved.body.hub_sync.pending, { operator_c: 'grant' });
  assert.equal(saved.body.hub_sync.status, 'failed');
  const retried = await call('POST', `${P}/hub/sharing/retry?username=operator_c`);
  assert.equal(retried.body.hub_sync.status, 'synced');
});
