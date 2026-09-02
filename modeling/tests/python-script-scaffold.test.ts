import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';

import { applyPythonSourceEdits, inspectPythonSource } from '../src/evaluation/python-source-patch.js';
import {
  buildPythonScriptScaffold,
  isCoreManagedPythonScaffold,
} from '../src/workflow/python-script-scaffold.js';
import { validateGeneratedScriptSafety } from '../src/workflow/script-safety.js';
import type { SpecScript } from '../src/types.js';

const contract: Omit<SpecScript, 'sourceCode'> = {
  key: 'synthetic_calculation',
  title: '合成计算',
  description: '根据合成输入返回结构化结果。',
  boundOperation: 'calculate_synthetic_value',
  runtime: 'python',
  entrypoint: 'synthetic_calculation.py',
  timeoutSeconds: 30,
  parameters: [
    { name: 'value', flag: '--value', type: 'decimal', required: true },
    { name: 'enabled', flag: '--enabled', type: 'boolean', required: false, defaultValue: false },
    { name: 'verbose', flag: '--verbose', type: 'boolean', required: false, action: 'store_true' },
  ],
  outputs: [{ key: 'result', type: 'number' }],
  boundaries: ['value 必须是有限数字'],
  evidence: ['material:synthetic#contract'],
};

test('Core 脚手架确定性生成 CLI 和 JSON 外壳，模型只实现业务函数', () => {
  const scaffold = buildPythonScriptScaffold(contract);
  assert.equal(isCoreManagedPythonScaffold(scaffold), true);
  assert.doesNotMatch(scaffold, /^from shared_capabilities\.ontology_script_sdk import DataProviderClient$/mu);
  assert.match(scaffold, /class DataProviderClient:[\s\S]+def __new__\(cls, \*args, \*\*kwargs\):/u);
  assert.match(scaffold, /DataProviderClient as _DataProviderClient/u);
  assert.match(scaffold, /type=parse_boolean/u);
  assert.match(scaffold, /action="store_true"/u);
  assert.match(scaffold, /parser\.add_argument\("--value", dest="value", type=float, required=True\)/u);
  assert.match(scaffold, /REQUIRED_OUTPUT_KEYS = \("result",\)/u);
  assert.match(scaffold, /NotImplementedError/u);

  const index = inspectPythonSource(scaffold);
  const business = index.symbols.find((item) => item.qualifiedName === 'run_business_logic');
  assert.ok(business);
  const sourceCode = applyPythonSourceEdits(scaffold, [{
    kind: 'replace_function',
    symbol: 'run_business_logic',
    expectedNodeHash: business.nodeHash,
    source: [
      'def run_business_logic(inputs):',
      '    value = float(inputs["value"])',
      '    return {"result": value if inputs["enabled"] else 0.0}',
    ].join('\n'),
  }], {
    expectedBaseSourceHash: index.baseSourceHash,
    requireNodeHashes: true,
  });
  validateGeneratedScriptSafety({ ...contract, sourceCode });
  assert.equal(isCoreManagedPythonScaffold(sourceCode), true);
  assert.doesNotMatch(sourceCode, /NotImplementedError/u);
});

test('Core 脚手架允许结构化业务错误，但成功结果仍校验必需输出', () => {
  const scaffold = buildPythonScriptScaffold(contract);
  const index = inspectPythonSource(scaffold);
  const business = index.symbols.find((item) => item.qualifiedName === 'run_business_logic');
  assert.ok(business);
  const sourceCode = applyPythonSourceEdits(scaffold, [{
    kind: 'replace_function',
    symbol: 'run_business_logic',
    expectedNodeHash: business.nodeHash,
    source: [
      'def run_business_logic(inputs):',
      '    if inputs["value"] < 0:',
      '        return {"error": "value 不能小于 0"}',
      '    return {"result": inputs["value"]}',
    ].join('\n'),
  }], {
    expectedBaseSourceHash: index.baseSourceHash,
    requireNodeHashes: true,
  });

  const failure = spawnSync('python3', ['-c', sourceCode, '--value', '-1'], { encoding: 'utf8' });
  assert.equal(failure.status, 0, failure.stderr);
  assert.deepEqual(JSON.parse(failure.stdout), { error: 'value 不能小于 0' });

  const success = spawnSync('python3', ['-c', sourceCode, '--value', '3.5'], { encoding: 'utf8' });
  assert.equal(success.status, 0, success.stderr);
  assert.deepEqual(JSON.parse(success.stdout), { result: 3.5 });
});
