import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  applyPythonSourceEdits,
  inspectPythonSource,
  pythonPatchBaseHash,
  pythonSourceHash,
  pythonSourceIdentifierNames,
  rewritePythonIdentifiers,
  selectPythonSourceSymbols,
} from '../src/evaluation/python-source-patch.js';

const source = [
  'import json',
  '',
  'DEFAULT_RATE = 1.0',
  '',
  'def normalize(value):',
  '    return float(value)',
  '',
  'def main():',
  '    print(json.dumps({"value": normalize(DEFAULT_RATE)}))',
  '',
  'if __name__ == "__main__":',
  '    main()',
].join('\n');

test('Python 增量补丁按语法节点替换函数和模块常量并保留其他源码', () => {
  const revised = applyPythonSourceEdits(source, [
    {
      kind: 'replace_assignment',
      symbol: 'DEFAULT_RATE',
      source: 'DEFAULT_RATE = 2.0',
    },
    {
      kind: 'insert_before_function',
      symbol: 'main',
      source: ['def validate(value):', '    if value < 0:', '        raise ValueError("negative")', '    return value'].join('\n'),
    },
    {
      kind: 'replace_function',
      symbol: 'normalize',
      source: ['def normalize(value):', '    return validate(float(value))'].join('\n'),
    },
  ]);

  assert.match(revised, /DEFAULT_RATE = 2\.0/u);
  assert.match(revised, /def validate\(value\):/u);
  assert.match(revised, /return validate\(float\(value\)\)/u);
  assert.match(revised, /def main\(\):/u);
  assert.match(revised, /if __name__ == "__main__":/u);
});

test('Python 增量补丁允许同一锚点按提交顺序插入多个不重名函数', () => {
  const index = inspectPythonSource(source);
  const normalize = index.symbols.find((item) => item.qualifiedName === 'normalize')!;
  const revised = applyPythonSourceEdits(source, [{
    kind: 'insert_before_function',
    symbol: 'normalize',
    expectedNodeHash: normalize.nodeHash,
    source: 'def first_helper(value):\n    return value',
  }, {
    kind: 'insert_before_function',
    symbol: 'normalize',
    expectedNodeHash: normalize.nodeHash,
    source: 'def second_helper(value):\n    return first_helper(value)',
  }], { requireNodeHashes: true });

  assert.ok(revised.indexOf('def first_helper') < revised.indexOf('def second_helper'));
  assert.ok(revised.indexOf('def second_helper') < revised.indexOf('def normalize'));
});

test('Python 增量补丁拒绝未知符号、重名函数和错误语法', () => {
  assert.throws(() => applyPythonSourceEdits(source, [{
    kind: 'replace_function', symbol: 'missing', source: 'def missing():\n    return 1',
  }]), /找不到顶层 function:missing/u);
  assert.throws(() => applyPythonSourceEdits(source, [{
    kind: 'insert_before_function', symbol: 'main', source: 'def normalize(value):\n    return value',
  }]), /不能重复新增函数 normalize/u);
  assert.throws(() => applyPythonSourceEdits(source, [{
    kind: 'replace_function', symbol: 'normalize', source: 'def normalize(value)\n    return value',
  }]), /语法错误/u);
});

test('Python 源码哈希统一换行编码和文件末尾空行', () => {
  assert.equal(pythonSourceHash('print(1)\r\n\r\n'), pythonSourceHash('print(1)\n'));
  assert.notEqual(pythonPatchBaseHash('print(1)\r\n'), pythonPatchBaseHash('print(1)\n'));
});

test('Python 标识符提取忽略注释与字符串并保留表达式引用', () => {
  const identifiers = pythonSourceIdentifierNames([
    '# DEFAULT_COMMENT 仅用于说明',
    'message = "DEFAULT_LITERAL"',
    'DEFAULT_RATE = 1.0',
    'def describe():',
    '    return f"{message}:{DEFAULT_RATE}"',
  ].join('\n'));

  assert.equal(identifiers.includes('DEFAULT_COMMENT'), false);
  assert.equal(identifiers.includes('DEFAULT_LITERAL'), false);
  assert.equal(identifiers.includes('DEFAULT_RATE'), true);
  assert.equal(identifiers.includes('message'), true);
});

test('Python 标识符重写只修改语法标识符而不改字符串和注释', () => {
  const rewritten = rewritePythonIdentifiers([
    '# helper_name should remain in a comment',
    'message = "helper_name"',
    'def helper_name(value):',
    '    return value + 1',
    'def caller(value):',
    '    return helper_name(value)',
    'def local_name(helper_name):',
    '    helper_name = helper_name + 1',
    '    return helper_name',
  ].join('\n'), new Map([['helper_name', '_opt_caller_h_deadbeef01']]));

  assert.match(rewritten, /# helper_name should remain/u);
  assert.match(rewritten, /message = "helper_name"/u);
  assert.match(rewritten, /def _opt_caller_h_deadbeef01\(value\):/u);
  assert.match(rewritten, /return _opt_caller_h_deadbeef01\(value\)/u);
  assert.match(rewritten, /def local_name\(helper_name\):/u);
  assert.match(rewritten, /helper_name = helper_name \+ 1/u);
});

test('Python 符号上下文返回直接依赖并通过双哈希阻止过期补丁', () => {
  const index = inspectPythonSource(source);
  const normalize = index.symbols.find((item) => item.qualifiedName === 'normalize');
  const defaultRate = index.symbols.find((item) => item.qualifiedName === 'DEFAULT_RATE');
  assert.ok(normalize);
  assert.ok(defaultRate);
  const selected = selectPythonSourceSymbols(source, ['main']);
  assert.deepEqual(selected.symbols.map((item) => item.qualifiedName), ['main', 'DEFAULT_RATE', 'normalize']);

  assert.throws(() => applyPythonSourceEdits(source, [{
    kind: 'replace_function',
    symbol: 'normalize',
    expectedNodeHash: normalize.nodeHash,
    source: 'def normalize(value):\n    return float(value) * 2',
  }], {
    expectedBaseSourceHash: pythonPatchBaseHash(`${source}\n`),
    requireNodeHashes: true,
  }), /基线源码已变化/u);

  const changedTarget = source.replace('return float(value)', 'return float(value) + 1');
  assert.throws(() => applyPythonSourceEdits(changedTarget, [{
    kind: 'replace_function',
    symbol: 'normalize',
    expectedNodeHash: normalize.nodeHash,
    source: 'def normalize(value):\n    return float(value) * 2',
  }], {
    expectedBaseSourceHash: pythonPatchBaseHash(changedTarget),
    requireNodeHashes: true,
  }), /目标节点已变化/u);
});

test('Python 符号补丁不格式化未授权源码字节', () => {
  const exactSource = source.replace('import json\n', 'import json  # keep spacing\r\n');
  const index = inspectPythonSource(exactSource);
  const normalize = index.symbols.find((item) => item.qualifiedName === 'normalize');
  assert.ok(normalize);
  const revised = applyPythonSourceEdits(exactSource, [{
    kind: 'replace_function',
    symbol: 'normalize',
    expectedNodeHash: normalize.nodeHash,
    source: 'def normalize(value):\n    return float(value) * 2',
  }], {
    expectedBaseSourceHash: index.baseSourceHash,
    requireNodeHashes: true,
  });
  assert.ok(revised.startsWith('import json  # keep spacing\r\n'));
  assert.equal(revised.slice(revised.indexOf('def main():')), exactSource.slice(exactSource.indexOf('def main():')));
});

test('Python 增量补丁通过节点哈希删除废弃顶层实现', () => {
  const withObsolete = source.replace('DEFAULT_RATE = 1.0', 'DEFAULT_RATE = 1.0\nOBSOLETE_RATE = 30.0');
  const index = inspectPythonSource(withObsolete);
  const obsolete = index.symbols.find((item) => item.qualifiedName === 'OBSOLETE_RATE');
  assert.ok(obsolete);
  const revised = applyPythonSourceEdits(withObsolete, [{
    kind: 'delete_assignment',
    symbol: 'OBSOLETE_RATE',
    expectedNodeHash: obsolete.nodeHash,
  }], {
    expectedBaseSourceHash: index.baseSourceHash,
    requireNodeHashes: true,
  });
  assert.doesNotMatch(revised, /OBSOLETE_RATE/u);
  assert.match(revised, /DEFAULT_RATE = 1\.0/u);
});
