import assert from 'node:assert/strict';
import test from 'node:test';

import type { SpecScript } from '../src/types.js';
import { buildScriptCliArguments } from '../src/workflow/script-cli.js';

type Parameter = SpecScript['parameters'][number];

function build(parameter: Parameter, value: unknown) {
  return buildScriptCliArguments([parameter], { [parameter.name]: value });
}

test('普通 boolean 参数始终使用 flag-value 语义', () => {
  const parameter: Parameter = {
    name: 'enabled', flag: '--enabled', type: 'boolean', required: false,
  };

  assert.deepEqual(build(parameter, true), { args: ['--enabled', 'true'], missing: [], invalid: [] });
  assert.deepEqual(build(parameter, false), { args: ['--enabled', 'false'], missing: [], invalid: [] });
});

test('store_true 和 store_false 使用与 Consumer 一致的 flag-presence 语义', () => {
  const storeTrue: Parameter = {
    name: 'verbose', flag: '--verbose', type: 'boolean', required: false,
    defaultValue: false, action: 'store_true',
  };
  const storeFalse: Parameter = {
    name: 'cache', flag: '--cache', type: 'bool', required: false,
    defaultValue: true, action: 'store_false',
  };

  assert.deepEqual(build(storeTrue, true).args, ['--verbose']);
  assert.deepEqual(build(storeTrue, false).args, []);
  assert.deepEqual(build(storeFalse, true).args, []);
  assert.deepEqual(build(storeFalse, false).args, ['--cache']);
  assert.deepEqual(buildScriptCliArguments([storeTrue, storeFalse], {}).args, []);
});

test('契约默认值、多值和结构化值按生产规则生成 CLI', () => {
  const parameters: Parameter[] = [
    { name: 'mode', flag: '--mode', type: 'string', required: false, defaultValue: 'standard' },
    { name: 'labels', flag: '--label', type: 'string', required: false, multiple: true },
    { name: 'filters', flag: '--filters', type: 'object', required: true },
  ];

  assert.deepEqual(buildScriptCliArguments(parameters, {
    labels: ['a', 'b'], filters: { enabled: true },
  }), {
    args: ['--mode', 'standard', '--label', 'a', '--label', 'b', '--filters', '{"enabled":true}'],
    missing: [],
    invalid: [],
  });
});

test('number 与 integer 使用 Consumer 的 Python 转换文本', () => {
  const result = buildScriptCliArguments([
    { name: 'amount', flag: '--amount', type: 'number', required: true },
    { name: 'count', flag: '--count', type: 'integer', required: true },
  ], { amount: 80, count: '00042' });

  assert.deepEqual(result, {
    args: ['--amount', '80.0', '--count', '42'],
    missing: [],
    invalid: [],
  });
});

test('缺少必填参数、未知参数和非法布尔值不会进入脚本进程', () => {
  const result = buildScriptCliArguments([
    { name: 'count', flag: '--count', type: 'integer', required: true },
    { name: 'enabled', flag: '--enabled', type: 'boolean', required: false },
  ], { enabled: 'sometimes', extra: 1 });

  assert.deepEqual(result.args, []);
  assert.deepEqual(result.missing, ['count']);
  assert.deepEqual(result.invalid, [
    'enabled: expected boolean',
    "Unknown argument 'extra'",
  ]);
});
