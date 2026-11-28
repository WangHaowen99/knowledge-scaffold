import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildQuery, collectAll, createClient, createTokenStore, saveWithRevision, withConfirmation,
} from '../src/api/client.js';
import {
  ApiError, describeConflict, errorText, packActionErrorText,
} from '../src/api/errors.js';

function memoryStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
}

const json = (status, body) => new Response(body == null ? null : JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});

test('buildQuery 跳过空值，多值用逗号', () => {
  assert.equal(buildQuery({ a: 1, b: null, c: '', d: ['x', 'y'] }), '?a=1&d=x%2Cy');
  assert.equal(buildQuery({}), '');
});

test('非 2xx 统一转成 ApiError，带 code、details、trace_id', async () => {
  const tokens = createTokenStore(memoryStorage());
  const client = createClient({
    tokens,
    fetchImpl: async () => json(409, { code: 'project_name_conflict', message: '重名', details: { conflict: { attempted_name: '甲' } }, trace_id: 't1' }),
  });
  await assert.rejects(client.post('/projects', { name: '甲' }), (e) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.status, 409);
    assert.equal(e.code, 'project_name_conflict');
    assert.equal(e.traceId, 't1');
    assert.equal(errorText(e), '你已经有一个叫「甲」的项目');
    return true;
  });
});

test('字节请求体原样发，Content-Type 是 octet-stream', async () => {
  let seen;
  const client = createClient({ tokens: createTokenStore(memoryStorage()), fetchImpl: async (url, init) => { seen = init; return json(200, { received_chunks: 1 }); } });
  const bytes = new Uint8Array([1, 2, 3]);
  await client.put('/uploads/u1/chunks/0', bytes);
  assert.equal(seen.body, bytes);
  assert.equal(seen.headers['Content-Type'], 'application/octet-stream');
  await client.put('/x', { a: 1 });
  assert.equal(seen.body, '{"a":1}');
  assert.equal(seen.headers['Content-Type'], 'application/json');
});

test('响应体不是统一错误体时按状态码兜底', async () => {
  const client = createClient({ tokens: createTokenStore(memoryStorage()), fetchImpl: async () => new Response('<html>bad gateway</html>', { status: 502 }) });
  await assert.rejects(client.get('/projects'), (e) => e.code === 'upstream_unavailable' && errorText(e).includes('连不上'));
});

test('插件操作显示具体兼容原因，其他场景保留全局简短文案', () => {
  const error = new ApiError({
    status: 409,
    code: 'pack_incompatible',
    message: 'deterministic-execution 依赖 codegen >=0.9，本项目未启用',
  });
  assert.equal(errorText(error), '兼容检查没有通过');
  assert.equal(packActionErrorText(error), 'deterministic-execution 依赖 codegen >=0.9，本项目未启用');
});

test('网络断开转成 status 0', async () => {
  const client = createClient({ tokens: createTokenStore(memoryStorage()), fetchImpl: async () => { throw new TypeError('fetch failed'); } });
  await assert.rejects(client.get('/projects'), (e) => e.status === 0 && errorText(e).includes('网络'));
});

test('token_expired 时刷新一次再重试，并发请求只刷新一次', async () => {
  const tokens = createTokenStore(memoryStorage());
  tokens.save({ access_token: 'old', refresh_token: 'r1' });
  let refreshCalls = 0;
  const seen = [];
  const fetchImpl = async (url, init) => {
    if (url.endsWith('/auth/refresh')) {
      refreshCalls += 1;
      await new Promise((r) => setTimeout(r, 5));
      return json(200, { access_token: 'new', refresh_token: 'r2' });
    }
    seen.push(init.headers.Authorization);
    if (init.headers.Authorization === 'Bearer old') return json(401, { code: 'token_expired', message: '过期' });
    return json(200, { ok: true });
  };
  const client = createClient({ tokens, fetchImpl });
  const [a, b] = await Promise.all([client.get('/me'), client.get('/projects')]);
  assert.deepEqual(a, { ok: true });
  assert.deepEqual(b, { ok: true });
  assert.equal(refreshCalls, 1);
  assert.equal(tokens.access, 'new');
  assert.equal(tokens.refresh, 'r2');
});

test('刷新失败时通知重新登录', async () => {
  const tokens = createTokenStore(memoryStorage());
  tokens.save({ access_token: 'old', refresh_token: 'r1' });
  let required = 0;
  const client = createClient({
    tokens,
    onAuthRequired: () => { required += 1; },
    fetchImpl: async (url) => (url.endsWith('/auth/refresh') ? json(401, { code: 'refresh_invalid' }) : json(401, { code: 'token_expired' })),
  });
  await assert.rejects(client.get('/me'), (e) => e.status === 401);
  assert.equal(required, 1);
});

test('维护模式错误交给 onMaintenance', async () => {
  let got = null;
  const client = createClient({
    tokens: createTokenStore(memoryStorage()),
    onMaintenance: (d) => { got = d; },
    fetchImpl: async () => json(503, { code: 'maintenance_read_only', message: 'x', details: { message: '平台升级中', until: null } }),
  });
  await assert.rejects(client.put('/projects/a/files/content', {}), (e) => e.isMaintenance && errorText(e) === '平台升级中');
  assert.equal(got.message, '平台升级中');
});

test('403 password_change_required 交给 onPasswordChangeRequired，不当成登录失效；别的 403 不触发', async () => {
  let pending = 0;
  let relogin = 0;
  let body = { code: 'password_change_required', message: '请先修改初始密码' };
  const client = createClient({
    tokens: createTokenStore(memoryStorage()),
    onAuthRequired: () => { relogin += 1; },
    onPasswordChangeRequired: () => { pending += 1; },
    fetchImpl: async () => json(403, body),
  });
  await assert.rejects(client.get('/projects'), (e) => e.status === 403 && errorText(e) === '请先修改初始密码');
  assert.deepEqual([pending, relogin], [1, 0]);
  body = { code: 'forbidden', message: '没有权限' };
  await assert.rejects(client.get('/admin/users'), (e) => e.code === 'forbidden');
  assert.equal(pending, 1);
});

test('423 写入范围锁：isLocked，提示按两态区分', () => {
  const running = new ApiError({ status: 423, code: 'write_scope_locked', details: { state: 'assistant_running' } });
  const pending = new ApiError({ status: 423, code: 'write_scope_locked', details: { state: 'pending_confirm' } });
  assert.ok(running.isLocked);
  assert.match(errorText(running), /助手正在改/);
  assert.match(errorText(pending), /有冲突/);
});

test('409 revision_conflict：提示带来源、相对时间和版本', () => {
  const now = Date.parse('2026-11-01T08:52:00.000Z');
  const text = describeConflict({ changed_by: 'assistant', changed_at: '2026-11-01T08:50:00.000Z', version_label: 'v0.13' }, now);
  assert.equal(text, '文件已被助手修改（2 分钟前，版本 v0.13），请刷新对比后再保存');
});

test('saveWithRevision：冲突时按用户选择覆盖或放弃', async () => {
  const calls = [];
  const save = async (rev) => {
    calls.push(rev);
    if (rev === 'r1') throw new ApiError({ status: 409, code: 'revision_conflict', details: { actual: 'r2' } });
    return { revision: 'r3' };
  };
  assert.deepEqual(await saveWithRevision(save, 'r1', async () => 'overwrite'), { revision: 'r3' });
  assert.deepEqual(calls, ['r1', 'r2']);
  await assert.rejects(saveWithRevision(save, 'r1', async () => 'reload'), (e) => e.isConflict);
});

test('withConfirmation：428 时先问用户，确认后带确认结果重试，取消返回 null', async () => {
  const call = async (token) => {
    if (!token) throw new ApiError({ status: 428, code: 'confirmation_required', details: { window: '22:00-06:00' } });
    return { ok: token };
  };
  assert.deepEqual(await withConfirmation(call, async (d) => (d.window ? 'tok' : null)), { ok: 'tok' });
  assert.equal(await withConfirmation(call, async () => null), null);
});

test('collectAll：游标翻页、去重，cursor_stale 时从头拉', async () => {
  let stale = true;
  const pages = { '': { items: [{ id: 1 }, { id: 2 }], next_cursor: 'c2' }, c2: { items: [{ id: 2 }, { id: 3 }], next_cursor: null } };
  const fetchPage = async (q) => {
    if (q.cursor === 'c2' && stale) {
      stale = false;
      throw new ApiError({ status: 409, code: 'cursor_stale' });
    }
    return pages[q.cursor || ''];
  };
  const all = await collectAll(fetchPage, { status: 'all' });
  assert.deepEqual(all.map((x) => x.id), [1, 2, 3]);
});

test('withToken 只给下载和 SSE 地址加 token 参数', () => {
  const tokens = createTokenStore(memoryStorage());
  tokens.save({ access_token: 'abc' });
  const client = createClient({ tokens, fetchImpl: async () => json(200, {}) });
  assert.equal(client.withToken('/api/v1/projects/a/transfer-package'), '/api/v1/projects/a/transfer-package?token=abc');
  assert.equal(client.url('/projects', { q: 'x' }), '/api/v1/projects?q=x');
  assert.equal(client.url('/health', null, { ops: true }), '/api/v1/ops/health');
});

test('超出配额：用服务端的说法（今天 / 本月、千分位），没有说法时数字也带千分位', () => {
  const server = new ApiError({ status: 429, code: 'quota_exceeded', message: '今天的模型用量已到上限（2,000,000 token），明天再试或请管理员调整配额',
    details: { quota: 'daily_tokens', limit: 2000000, used: 3314435 } });
  assert.equal(errorText(server), '今天的模型用量已到上限（2,000,000 token），明天再试或请管理员调整配额');
  const bare = new ApiError({ status: 429, code: 'quota_exceeded', details: { quota: 'daily_tokens', limit: 2000000, used: 3314435 } });
  assert.equal(errorText(bare), '已超出配额（上限 2,000,000，已用 3,314,435）');
});

test('自主构建撤回一步回 409：要先撤后面那一步时照服务端的说法提示，其余用固定说法', () => {
  const later = new ApiError({ status: 409, code: 'slice_not_revertible', message: '先撤回「提取操作」',
    details: { reason: 'revert_later_first', slice_id: 'core.operation', slice_name: '提取操作' } });
  assert.equal(errorText(later), '先撤回「提取操作」');
  const merged = new ApiError({ status: 409, code: 'slice_not_revertible', message: '这一步没有生效过', details: { reason: 'not_merged' } });
  assert.equal(errorText(merged), '这一步不能回退（未生效、已回退过，或后面的修改改到了它）');
});
