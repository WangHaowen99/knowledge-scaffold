// 版本历史分页与发布记录（移植 develop versionHistory 测试；数据全部合成）
import assert from 'node:assert/strict';
import test from 'node:test';
import { createClient, createTokenStore } from '../src/api/client.js';
import { createApi } from '../src/api/endpoints.js';
import { EMPTY_HISTORY, appendPage, blockerView, revertErrorText } from '../src/pages/versions/versionsModel.js';

const v = (n, release = null) => ({ commit: `commit-${n}`, n, version_label: `v0.${n}`, release });
const memory = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, x) => m.set(k, String(x)), removeItem: (k) => m.delete(k) };
};

test('分页按后端顺序累积，发布记录读不到的原因保留第一次的', () => {
  const first = appendPage(EMPTY_HISTORY, { items: [v(3, '0.1.1'), v(2)], next_cursor: 'c-2', release_error: '' });
  assert.deepEqual(first.items.map((x) => x.n), [3, 2]);
  assert.equal(first.next, 'c-2');
  const failed = appendPage(first, { items: [v(1)], next_cursor: 'c-3', release_error: '知识包仓库暂时不可用' });
  const last = appendPage(failed, { items: [], next_cursor: null, release_error: '' });
  assert.deepEqual(last.items.map((x) => x.release), ['0.1.1', null, null]);
  assert.equal(last.next, null);
  assert.equal(last.releaseError, '知识包仓库暂时不可用');
});

test('读页之间有了新版本：同一版本不重复列', () => {
  const first = appendPage(EMPTY_HISTORY, { items: [v(3), v(2)], next_cursor: 'c-2' });
  const shifted = appendPage(first, { items: [v(2), v(1)], next_cursor: null });
  assert.deepEqual(shifted.items.map((x) => x.n), [3, 2, 1]);
});

test('翻页请求把游标原样带回后端', async () => {
  const urls = [];
  const client = createClient({
    tokens: createTokenStore(memory()),
    fetchImpl: async (url) => {
      urls.push(String(url));
      return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    },
  });
  const api = createApi(client);
  await api.versions('project-alpha', { limit: 200 });
  await api.versions('project-alpha', { limit: 200, cursor: '5:off' });
  assert.match(urls[0], /\/api\/v1\/projects\/project-alpha\/versions\?limit=200$/);
  assert.match(urls[1], /\/api\/v1\/projects\/project-alpha\/versions\?limit=200&cursor=5%3Aoff$/);
});

test('回退被挡住的原因：每种都有说法，能处理的给去处', () => {
  assert.equal(blockerView({ code: 'write_scope_locked', details: { state: 'assistant_running' } }).action, 'chat');
  assert.equal(blockerView({ code: 'session_busy' }).action, 'chat');
  assert.equal(blockerView({ code: 'autobuild_running' }).action, 'run');
  const draft = blockerView({ code: 'studio_draft_pending', details: { entry_id: 'object:item' } });
  assert.deepEqual([draft.action, draft.entry], ['studio', 'object:item']);
  assert.equal(blockerView({ code: 'workspace_dirty' }).action, null);
  assert.equal(blockerView({ code: 'unsaved_editor' }).text, '对比里的修改还没保存');
  assert.equal(blockerView({ code: 'other' }), null);
  assert.match(revertErrorText({ code: 'head_moved' }, 'x'), /新版本/);
  assert.equal(revertErrorText({ code: 'studio_draft_pending' }, 'x'), 'Studio 里有没保存的草稿');
});
