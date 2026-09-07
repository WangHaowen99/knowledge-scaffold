import assert from 'node:assert/strict';
import test from 'node:test';

import { requireOkResponse } from '../frontend/src/api.js';
import { publicApiErrorMessage } from '../frontend/src/error-presentation.js';

test('前端按后端公开字段展示错误并兼容三种响应结构', () => {
  assert.equal(publicApiErrorMessage({ error: '文件不存在' }, 404), '文件不存在');
  assert.equal(publicApiErrorMessage({ reason: '候选修订已处理' }, 409), '候选修订已处理');
  assert.equal(publicApiErrorMessage({
    error: '备用文案',
    reason: '原因文案',
    display: { text: '页面公开文案' },
  }, 409), '页面公开文案');
});

test('前端不展示 5xx 的完整后端报错或 Fastify message', () => {
  const payload = {
    statusCode: 500,
    error: 'Internal Server Error',
    message: 'SQLITE_ERROR: synthetic backend detail\nError: internal stack',
  };
  assert.equal(publicApiErrorMessage(payload, 500), '服务暂时不可用，请稍后重试');
  assert.equal(publicApiErrorMessage({ error: 'upstream private failure' }, 503), '服务暂时不可用，请稍后重试');
});

test('前端为缺少公开文案的常见 HTTP 状态提供简短兜底', () => {
  const expected = new Map([
    [400, '请求内容不正确，请检查后重试'],
    [401, '登录状态已失效，请重新登录'],
    [403, '没有权限执行此操作'],
    [404, '请求的内容不存在'],
    [409, '内容已更新或操作冲突，请刷新后重试'],
    [422, '提交内容未通过校验，请检查后重试'],
    [429, '请求过于频繁，请稍后重试'],
  ]);
  for (const [status, message] of expected) {
    assert.equal(publicApiErrorMessage({}, status), message, String(status));
  }
});

test('前端把多行或过长的 4xx 文案压缩到单行 120 字以内', () => {
  const message = publicApiErrorMessage({ error: `第一行\n${'很长的校验说明'.repeat(30)}` }, 400);
  assert.equal(message.includes('\n'), false);
  assert.ok(message.length <= 120);
  assert.match(message, /\.\.\.$/u);
});

test('二进制接口也通过统一规则提取公开错误', async () => {
  const body = {
    code: 'ARTIFACT_REVISION_NOT_FOUND',
    reason: '找不到这个版本',
    message: 'synthetic backend detail',
  };
  await assert.rejects(
    requireOkResponse({ ok: false, status: 404, json: async () => body }),
    (error: any) => error.status === 404
      && error.data === body
      && error.message === '找不到这个版本',
  );
});
