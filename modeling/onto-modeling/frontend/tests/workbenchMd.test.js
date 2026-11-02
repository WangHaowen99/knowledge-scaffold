import assert from 'node:assert/strict';
import test from 'node:test';

test('助手正文：条目链接与出处角标的预处理', async () => {
  const { preprocessMd, inlineTokens, stripLinks } = await import('../src/pages/workbench/md.js');
  assert.equal(preprocessMd('按 [[商品品类]] 取值[^2]'), '按 [商品品类](#wl:%E5%95%86%E5%93%81%E5%93%81%E7%B1%BB) 取值[2](#ct:2)');
  assert.deepEqual(inlineTokens('对 [[审批提示]] 生成[^4]。'), [
    { t: 'text', v: '对 ' }, { t: 'link', v: '审批提示' }, { t: 'text', v: ' 生成' }, { t: 'cite', v: 4 }, { t: 'text', v: '。' },
  ]);
  assert.equal(stripLinks('2. 按 [[商品品类]] 的标准'), '2. 按 商品品类 的标准');
});
