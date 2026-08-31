import assert from 'node:assert/strict';
import test from 'node:test';
import { scriptSourceHash } from '../src/agent/core-service.js';

test('脚本测试回执忽略换行编码和文件末尾换行差异', () => {
  const source = 'print("ok")';

  assert.equal(scriptSourceHash(source), scriptSourceHash(`${source}\n`));
  assert.equal(scriptSourceHash(source), scriptSourceHash(`${source}\n\n`));
  assert.equal(
    scriptSourceHash('import json\r\nprint(json.dumps({"ok": True}))\r\n'),
    scriptSourceHash('import json\nprint(json.dumps({"ok": True}))\n'),
  );
});

test('脚本实际代码变化仍会使测试回执失效', () => {
  assert.notEqual(scriptSourceHash('print("ok")'), scriptSourceHash('print("changed")'));
});
