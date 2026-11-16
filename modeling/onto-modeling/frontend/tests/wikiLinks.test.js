import test from 'node:test';
import assert from 'node:assert/strict';
import { fromMarkdown } from 'mdast-util-from-markdown';
import { remarkWikiLinks } from '../src/lib/wikiLinks.js';

function transform(markdown) {
  const tree = fromMarkdown(markdown);
  remarkWikiLinks()(tree);
  return tree;
}

function links(tree) {
  const found = [];
  function visit(node) {
    if (node.type === 'link') found.push(node);
    for (const child of node.children || []) visit(child);
  }
  visit(tree);
  return found;
}

function visibleText(tree) {
  const values = [];
  function visit(node) {
    if (node.type === 'text') values.push(node.value);
    for (const child of node.children || []) visit(child);
  }
  visit(tree);
  return values.join('');
}

test('converts Chinese Wiki markup without exposing the encoded target as text', () => {
  const tree = transform('- 合成对象档案（2） → [[合成对象档案查询]]');
  const [link] = links(tree);
  assert.equal(link.url, `#wiki:${encodeURIComponent('合成对象档案查询')}`);
  assert.equal(link.children[0].value, '合成对象档案查询');
  assert.doesNotMatch(visibleText(tree), /%[0-9a-f]{2}|#wiki:/i);
});

test('repairs historical Wiki links split by whitespace or a line break', () => {
  for (const separator of [' ', '\n']) {
    const tree = transform(`[合成对象问题处理]${separator}(#wiki:${encodeURIComponent('合成对象问题处理')})`);
    const [link] = links(tree);
    assert.equal(link.children[0].value, '合成对象问题处理');
    assert.equal(decodeURIComponent(link.url.slice('#wiki:'.length)), '合成对象问题处理');
  }
});

test('keeps Wiki-like text in code and existing Markdown links unchanged', () => {
  const tree = transform('`[[代码示例]]` [已有链接](#wiki:operation)');
  const [link] = links(tree);
  assert.equal(link.url, '#wiki:operation');
  assert.equal(tree.children[0].children[0].type, 'inlineCode');
});

test('leaves incomplete streaming markup as plain text until it is closed', () => {
  const tree = transform('正在生成 [[合成对象档案');
  assert.equal(links(tree).length, 0);
  assert.equal(tree.children[0].children[0].value, '正在生成 [[合成对象档案');
});
