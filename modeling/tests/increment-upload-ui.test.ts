import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../frontend/src/PackageChat.jsx', import.meta.url), 'utf8');

test('增量资料选中后先写流内进度，再在后台上传', () => {
  const start = source.indexOf('const uploadMaterials = async');
  const end = source.indexOf('const submitPendingInteraction', start);
  const upload = source.slice(start, end);
  const optimistic = upload.indexOf('正在上传 ${filenames.length} 份建模资料');
  const request = upload.indexOf('await uploadModelingMaterials');

  assert.ok(optimistic >= 0 && optimistic < request, '文件选择后必须先给本地可见反馈');
  assert.doesNotMatch(upload, /setLocalBusy\(['"]upload['"]\)/u);
  assert.match(upload, /上传和解析在后台继续，你可以继续输入/u);
  assert.match(upload, /解析在后台继续，完成结果会回到消息流/u);
});
