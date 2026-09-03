import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';

import {
  applyOptimizations,
  fileDataLoaderFunctions,
  groupTargetedOptimizationSuggestions,
  hardCodedRuntimeIdentifierCollections,
  hasReachableActualPeriodAggregation,
  hasReachableDerivedFlowAggregation,
  hasReachableDynamicTankInventory,
  loaderOutputDisconnects,
  planTargetedOptimizationRepairSymbols,
  planTargetedOptimizationSymbols,
  pruneUnreachableNewOptimizerHelpers,
  unreachableNewOptimizerHelpers,
} from '../src/evaluation/optimizer.js';
import {
  applyPythonSourceEdits,
  inspectPythonSource,
  pythonSourceIdentifierNames,
} from '../src/evaluation/python-source-patch.js';
import type { LlmDeps } from '../src/llm/provider.js';
import type { PackageSpec, Project } from '../src/types.js';
import type { OptSuggestion } from '../src/workflow/m4-repo.js';
import { AgentRunTimeoutError } from '../src/workflow/agent.js';

const source = [
  'import argparse',
  'import json',
  '',
  'def calculate(value):',
  '    return float(value)',
  '',
  'def main(argv=None):',
  '    parser = argparse.ArgumentParser()',
  '    parser.add_argument("--value", required=True)',
  '    args = parser.parse_args(argv)',
  '    print(json.dumps({"result": calculate(args.value)}))',
  '',
  'if __name__ == "__main__":',
  '    main()',
].join('\n');

const unrelatedSource = [
  'import argparse',
  'import json',
  'UNRELATED_SCRIPT_BODY = "must-not-enter-targeted-prompt"',
  'parser = argparse.ArgumentParser()',
  'parser.add_argument("--name", required=True)',
  'args = parser.parse_args()',
  'print(json.dumps({"name": args.name}))',
].join('\n');

function fixture() {
  const spec: PackageSpec = {
    id: 'targeted-demo', name: 'targeted-demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [{
      key: 'input_record', title: '输入记录', description: '结构化输入', sourceTable: 'input_record',
      fields: [{ key: 'value', cnName: '数值', sourceColumn: 'value', type: 'number' }],
    }],
    operations: [{
      key: 'calculate_operation', title: '计算', description: '执行确定性计算', inputs: [], outputs: [],
      flow: ['调用 script:calculate_script'], relatedObjects: ['input_record'],
      competencyQuestion: '如何计算？', script: { key: 'calculate_script', required: true },
    }, {
      key: 'unrelated_operation', title: '无关操作', description: 'UNRELATED_SECRET_FLOW', inputs: [], outputs: [],
      flow: ['调用 script:unrelated_script'], relatedObjects: [], competencyQuestion: '无关操作是什么？',
      script: { key: 'unrelated_script', required: true },
    }],
    rules: [], metrics: [],
    scripts: [{
      key: 'calculate_script', title: '计算脚本', description: '计算结构化数值。',
      boundOperation: 'calculate_operation', runtime: 'python', entrypoint: 'calculate.py', timeoutSeconds: 30,
      parameters: [{ name: 'value', flag: '--value', type: 'number', required: true }],
      outputs: [{ key: 'result', type: 'number' }], boundaries: ['非法数值必须明确拒绝。'], sourceCode: source,
    }, {
      key: 'unrelated_script', title: '无关脚本', description: '不应进入目标上下文。',
      boundOperation: 'unrelated_operation', runtime: 'python', entrypoint: 'unrelated.py', timeoutSeconds: 30,
      parameters: [{ name: 'name', flag: '--name', type: 'string', required: true }],
      outputs: [{ key: 'name', type: 'string' }], boundaries: ['名称必填。'], sourceCode: unrelatedSource,
    }],
  };
  const project: Project = {
    id: 'p-targeted', owner: 'tester', name: '目标补丁测试', domain: '通用计算', background: '', stage: 'optimize',
    createdAt: '2026-09-02T00:00:00.000Z', updatedAt: '2026-09-02T00:00:00.000Z',
  };
  const base = {
    projectId: project.id, roundId: 'r-targeted', basedVersion: '1.0.0', status: 'pending' as const,
    createdAt: '2026-09-02T00:00:00.000Z', target: 'calculate_script', kind: 'modify_script',
  };
  const suggestions: OptSuggestion[] = [{
    ...base, id: 's-script-1', title: '统一数值转换', detail: 'calculate 函数必须显式转换数值并拒绝非法输入。',
    generalizationPlan: '对任意结构化数值应用统一转换。', validationPlan: '覆盖非法值和正常值。',
  }, {
    ...base, id: 's-script-2', title: '补充非负校验', detail: 'calculate 函数在输入小于允许范围时返回明确错误。',
    generalizationPlan: '按输入性质校验，不绑定具体样例。', validationPlan: '覆盖边界和越界输入。',
  }, {
    ...base, id: 's-operation', title: '收紧操作流程', detail: '明确脚本是唯一计算路径。',
    kind: 'modify_operation', target: 'calculate_operation',
    generalizationPlan: '所有同类问题只走确定性脚本。', validationPlan: '脚本失败时明确阻断。',
  }];
  return { spec, project, suggestions };
}

test('复合脚本建议按目标资源聚合并通过函数级补丁生成候选', async () => {
  const { spec, project, suggestions } = fixture();
  const scope = groupTargetedOptimizationSuggestions(suggestions, spec);
  assert.equal(scope?.groups.length, 2);
  assert.equal(scope?.changeSets.length, 1);
  assert.deepEqual(scope?.changeSets[0].resourceKeys.sort(), [
    'operation:calculate_operation', 'script:calculate_script',
  ]);
  assert.equal(scope?.groups.find((group) => group.target === 'calculate_script')?.suggestions.length, 2);

  const changedOperation = {
    ...spec.operations[0],
    description: '只通过绑定的确定性脚本执行计算，脚本失败时停止并返回明确错误。',
    flow: ['校验结构化输入', '调用 script:calculate_script', '脚本失败时停止并报告错误'],
  };
  const sourceIndex = inspectPythonSource(source);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const core = createFauxCore({ provider: 'targeted-patch', models: [{ id: 'targeted-patch' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
      scriptKey: 'calculate_script',
      baseSourceHash: sourceIndex.baseSourceHash,
      edits: [{
        kind: 'replace_function',
        symbol: 'calculate',
        expectedNodeHash: calculateNode.nodeHash,
        source: [
          'def calculate(value):',
          '    try:',
          '        number = float(value)',
          '    except (TypeError, ValueError) as exc:',
          '        raise ValueError("invalid value") from exc',
          '    if number < 0:',
          '        raise ValueError("value must be non-negative")',
          '    return number',
        ].join('\n'),
      }],
    })]),
    fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: {
        description: changedOperation.description,
        flow: changedOperation.flow,
      },
    })]),
  ]);
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, suggestions, llm);

  assert.equal(core.state.callCount, 2);
  assert.match(contexts.join('\n'), /submit_targeted_script_shard/u);
  assert.match(contexts.join('\n'), /submit_targeted_operation_patch/u);
  assert.doesNotMatch(contexts.join('\n'), /UNRELATED_SCRIPT_BODY/u);
  assert.doesNotMatch(contexts.join('\n'), /UNRELATED_SECRET_FLOW/u);
  assert.match(revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '', /number < 0/u);
  assert.equal(revised.scripts.find((item) => item.key === 'unrelated_script')?.sourceCode, unrelatedSource);
  assert.equal(revised.operations.find((item) => item.key === 'calculate_operation')?.description, changedOperation.description);
  assert.equal(revised.operations.find((item) => item.key === 'calculate_operation')?.title, '计算');
  assert.deepEqual(revised.operations.find((item) => item.key === 'calculate_operation')?.script, {
    key: 'calculate_script', required: true,
  });
  assert.equal(revised.operations.find((item) => item.key === 'calculate_operation')?.competencyQuestion, '如何计算？');
  assert.equal(revised.operations.find((item) => item.key === 'unrelated_operation')?.description, 'UNRELATED_SECRET_FLOW');
});

test('脚本契约补丁与绑定 operation 的输入输出原子同步', async () => {
  const { spec, project, suggestions } = fixture();
  const sourceIndex = inspectPythonSource(source);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const core = createFauxCore({ provider: 'targeted-contract', models: [{ id: 'targeted-contract' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
      scriptKey: 'calculate_script',
      baseSourceHash: sourceIndex.baseSourceHash,
      edits: [{
        kind: 'replace_function',
        symbol: 'calculate',
        expectedNodeHash: calculateNode.nodeHash,
        source: [
          'def calculate(value):',
          '    number = float(value)',
          '    if number < 0:',
          '        raise ValueError("value must be non-negative")',
          '    return number',
        ].join('\n'),
      }],
      contractPatch: {
        description: '根据结构化输入返回确定性结果。',
        parameters: [{
          name: 'value', flag: '--value', type: 'number', required: true,
          description: '待计算的合成数值。',
        }],
        outputs: [{ key: 'result', type: 'number', description: '确定性计算结果。' }],
        boundaries: ['输入必须可转换为非负数值。'],
      },
    })]),
    fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: { description: '只通过绑定脚本执行确定性计算。' },
    })]),
  ]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, suggestions, llm);
  const script = revised.scripts.find((item) => item.key === 'calculate_script')!;
  const operation = revised.operations.find((item) => item.key === 'calculate_operation')!;

  assert.equal(script.runtime, 'python');
  assert.equal(script.boundOperation, 'calculate_operation');
  assert.equal(script.entrypoint, 'calculate.py');
  assert.equal(script.parameters[0].description, '待计算的合成数值。');
  assert.equal(script.outputs[0].description, '确定性计算结果。');
  assert.deepEqual(operation.inputs, [{
    param: 'value', type: 'number', required: true, note: '待计算的合成数值。',
  }]);
  assert.deepEqual(operation.outputs, [{
    field: 'result', type: 'number', note: '确定性计算结果。',
  }]);
});

test('计算语义门禁拒绝错误候选并驱动第二轮定点修正', async () => {
  const { spec, project, suggestions } = fixture();
  const buildPlanningSource = (mode: 'baseline' | 'invalid' | 'valid') => {
    const fixedDays = mode === 'baseline' ? 17 : 19;
    const processBatch = mode === 'valid' ? [
      'def process_batch(batch):',
      '    process_amount = float(batch["process_amount"])',
      '    split_ratio = float(batch["split_ratio"])',
      '    downstream_amount = process_amount * split_ratio',
      '    return {"downstream_amount": downstream_amount}',
    ] : [
      'def process_batch(batch):',
      '    return {"downstream_amount": float(batch["process_amount"])}',
    ];
    const aggregateMonth = mode === 'valid' ? [
      'def aggregate_month(batches, results, daily_capacity):',
      '    total_period_days = sum(float(batch["period_days"]) for batch in batches)',
      '    monthly_capacity = float(daily_capacity) * total_period_days',
      '    downstream_total = sum(result["downstream_amount"] for result in results)',
      '    downstream_load = downstream_total / monthly_capacity',
      '    return {"load": downstream_load}',
    ] : [
      'def aggregate_month(batches, results, daily_capacity):',
      `    monthly_capacity = float(daily_capacity) * ${fixedDays}`,
      '    downstream_total = sum(float(batch["process_amount"]) for batch in batches)',
      '    return {"load": downstream_total / monthly_capacity}',
    ];
    const checkTanks = mode === 'valid' ? [
      'def check_tanks(tank, batches, connections):',
      '    inventory = float(tank["initial_capacity"])',
      '    for batch in batches:',
      '        inflow = sum(float(flow["amount"]) for flow in connections if flow["batch"] == batch["id"] and flow["direction"] == "in")',
      '        outflow = sum(float(flow["amount"]) for flow in connections if flow["batch"] == batch["id"] and flow["direction"] == "out")',
      '        inventory = inventory + inflow - outflow',
      '    return inventory',
    ] : [
      'def check_tanks(tank, batches, connections):',
      '    inventory = float(tank["initial_capacity"])',
      '    for batch in batches:',
      '        inventory = float(tank["initial_capacity"])',
      '        inflow = sum(float(flow["amount"]) for flow in connections if flow["direction"] == "in")',
      '        outflow = sum(float(flow["amount"]) for flow in connections if flow["direction"] == "out")',
      '        inventory = inventory + inflow - outflow',
      '    return inventory',
    ];
    return [
      'import argparse',
      'import json',
      '',
      ...processBatch,
      '',
      ...aggregateMonth,
      '',
      ...checkTanks,
      '',
      'def calculate(payload):',
      '    batches = payload["batches"]',
      '    results = [process_batch(batch) for batch in batches]',
      '    summary = aggregate_month(batches, results, payload["daily_capacity"])',
      '    summary["ending_inventory"] = check_tanks(payload["tank"], batches, payload["connections"])',
      '    return summary',
      '',
      'def main(argv=None):',
      '    parser = argparse.ArgumentParser()',
      '    parser.add_argument("--payload", required=True)',
      '    args = parser.parse_args(argv)',
      '    print(json.dumps(calculate(json.loads(args.payload))))',
      '',
      'if __name__ == "__main__":',
      '    main()',
    ].join('\n');
  };
  const planningScript = {
    ...spec.scripts![0],
    description: '根据通用结构化计划计算汇总结果。',
    parameters: [{ name: 'payload', flag: '--payload', type: 'json', required: true }],
    outputs: [
      { key: 'load', type: 'number' },
      { key: 'ending_inventory', type: 'number' },
    ],
    sourceCode: buildPlanningSource('baseline'),
  };
  const planningSpec: PackageSpec = {
    ...spec,
    operations: spec.operations.map((operation) => operation.key === 'calculate_operation'
      ? {
          ...operation,
          inputs: [{ param: 'payload', type: 'json', required: true }],
          outputs: [
            { field: 'load', type: 'number' },
            { field: 'ending_inventory', type: 'number' },
          ],
        }
      : operation),
    scripts: [planningScript, spec.scripts![1]],
  };
  const adopted = [{
    ...suggestions[0],
    id: 's-planning-semantics',
    title: '修正汇总计算口径',
    detail: [
      'aggregate_month 必须汇总每批 period_days 形成月度能力分母。',
      'process_batch 必须由加工量乘 split_ratio 形成下游派生产量，aggregate_month 只能累计派生产量，不能直接累计原始加工量。',
      'check_tanks 必须从 initial_capacity 初始化，并逐批以上一期 inventory 按 inflow 和 outflow 更新。',
    ].join(''),
  }];
  const plan = planTargetedOptimizationSymbols(adopted, planningSpec)!;
  let workingSource = planningScript.sourceCode;
  let scriptAttempts = 0;
  const core = createFauxCore({ provider: 'semantic-retry', models: [{ id: 'semantic-retry' }] });
  const respond = (context: unknown) => {
    const serialized = JSON.stringify(context);
    const shard = plan.scriptShards.find((item) => serialized.includes(item.id));
    assert.ok(shard);
    const mode = scriptAttempts < plan.scriptShards.length ? 'invalid' : 'valid';
    const currentIndex = inspectPythonSource(workingSource);
    const desiredIndex = inspectPythonSource(buildPlanningSource(mode));
    const edits = shard.symbols.map((symbol) => {
      const current = currentIndex.symbols.find((item) => item.qualifiedName === symbol)!;
      const desired = desiredIndex.symbols.find((item) => item.qualifiedName === symbol)!;
      return {
        kind: 'replace_function' as const,
        symbol,
        expectedNodeHash: current.nodeHash,
        source: desired.source,
      };
    });
    workingSource = applyPythonSourceEdits(workingSource, edits, {
      expectedBaseSourceHash: currentIndex.baseSourceHash,
      requireNodeHashes: true,
    });
    scriptAttempts += 1;
    return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
      scriptKey: 'calculate_script',
      baseSourceHash: currentIndex.baseSourceHash,
      edits,
    })]);
  };
  core.setResponses(Array.from({ length: plan.scriptShards.length * 2 }, () => respond));
  const progress: any[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(
    project,
    planningSpec,
    adopted,
    llm,
    (event) => { progress.push(event); },
  );

  assert.equal(core.state.callCount, plan.scriptShards.length * 2);
  assert.equal(scriptAttempts, plan.scriptShards.length * 2);
  const firstFailure = progress.find((event) => event.phase === 'validation_failed' && event.attempt === 1);
  assert.match(firstFailure?.error ?? '', /实际批次周期/u);
  assert.match(firstFailure?.error ?? '', /派生负荷/u);
  assert.match(firstFailure?.error ?? '', /跨批动态罐存/u);
  const revisedSource = revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '';
  assert.equal(hasReachableActualPeriodAggregation(revisedSource), true);
  assert.equal(hasReachableDerivedFlowAggregation(revisedSource), true);
  assert.equal(hasReachableDynamicTankInventory(revisedSource), true);
});

test('依赖敏感脚本按顺序生成并应用同一脚本的链式分片', async () => {
  const { spec, project, suggestions } = fixture();
  const multiFunctionSource = source.replace('    return float(value)', '    return summarize(value)').replace('def main(argv=None):', [
    'def summarize(value):',
    '    return float(value)',
    '',
    'def main(argv=None):',
  ].join('\n'));
  const targetedSpec = {
    ...spec,
    scripts: [{ ...spec.scripts![0], sourceCode: multiFunctionSource }, spec.scripts![1]],
  };
  const adopted: OptSuggestion[] = [{
    ...suggestions[0], id: 's-calculate-provider',
    title: '修复 calculate 运行时数据读取',
    detail: 'calculate 必须使用 DataProviderClient 读取运行时数据源，不得依赖固定值。',
  }, {
    ...suggestions[1], id: 's-summarize-provider',
    title: '修复 summarize 动态聚合',
    detail: 'summarize 必须基于 calculate 的 DataProviderClient 运行时结果进行动态聚合。',
  }, suggestions[2]];
  const plannedSymbols = planTargetedOptimizationSymbols(adopted, targetedSpec)!.scriptShards.map((item) => item.symbols[0]);
  let workingSource = multiFunctionSource;
  let scriptAttempts = 0;
  let operationAttempts = 0;
  const submittedHashes: string[] = [];
  const core = createFauxCore({ provider: 'targeted-dependency-chain', models: [{ id: 'targeted-dependency-chain' }] });
  const respond = (context: unknown) => {
    const serialized = JSON.stringify(context);
    if (serialized.includes('submit_targeted_script_shard') && scriptAttempts < 2) {
      scriptAttempts += 1;
      const index = inspectPythonSource(workingSource);
      const symbol = plannedSymbols[scriptAttempts - 1]!;
      submittedHashes.push(index.baseSourceHash);
      const node = index.symbols.find((item) => item.kind === 'function' && item.qualifiedName === symbol)!;
      const replacement = symbol === 'calculate'
        ? 'def calculate(value):\n    number = float(value)\n    return number + 1'
        : 'def summarize(value):\n    return calculate(value) * 2';
      const edit = {
        kind: 'replace_function' as const,
        symbol,
        expectedNodeHash: node.nodeHash,
        source: replacement,
      };
      const nextSource = applyPythonSourceEdits(workingSource, [edit], {
        expectedBaseSourceHash: index.baseSourceHash,
        requireNodeHashes: true,
      });
      workingSource = nextSource;
      return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
        scriptKey: 'calculate_script',
        baseSourceHash: index.baseSourceHash,
        edits: [edit],
      })]);
    }
    operationAttempts += 1;
    return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: { description: '只通过确定性脚本执行动态计算。' },
    })]);
  };
  core.setResponses([respond, respond, respond]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, targetedSpec, adopted, llm);
  const revisedSource = revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '';
  assert.equal(scriptAttempts, 2);
  assert.equal(operationAttempts, 1);
  assert.equal(core.state.callCount, 3);
  assert.notEqual(submittedHashes[0], submittedHashes[1]);
  assert.match(revisedSource, /return number \+ 1/u);
  assert.match(revisedSource, /return calculate\(value\) \* 2/u);
});

test('单个脚本分片参数超限时只局部重试且保留并发 operation 结果', async () => {
  const { spec, project, suggestions } = fixture();
  const sourceIndex = inspectPythonSource(source);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const core = createFauxCore({ provider: 'targeted-shard-retry', models: [{ id: 'targeted-shard-retry' }] });
  let scriptAttempts = 0;
  let operationAttempts = 0;
  const prompts: string[] = [];
  const respond = (context: unknown) => {
    const serialized = JSON.stringify(context);
    prompts.push(serialized);
    if (serialized.includes('submit_targeted_function_repair')) {
      scriptAttempts += 1;
      return fauxAssistantMessage([fauxToolCall('submit_targeted_function_repair', {
        source: [
          'def calculate(value):',
          '    try:',
          '        number = float(value)',
          '    except (TypeError, ValueError) as exc:',
          '        raise ValueError("invalid value") from exc',
          '    if number < 0:',
          '        raise ValueError("value must be non-negative")',
          '    return number',
        ].join('\n'),
      })]);
    }
    if (serialized.includes('submit_targeted_script_shard')) {
      scriptAttempts += 1;
      const replacement = scriptAttempts === 1
        ? `def calculate(value):\n${'    value = float(value)\n'.repeat(650)}`
        : [
          'def calculate(value):',
          '    try:',
          '        number = float(value)',
          '    except (TypeError, ValueError) as exc:',
          '        raise ValueError("invalid value") from exc',
          '    if number < 0:',
          '        raise ValueError("value must be non-negative")',
          '    return number',
        ].join('\n');
      return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
        scriptKey: 'calculate_script',
        baseSourceHash: sourceIndex.baseSourceHash,
        edits: [{
          kind: 'replace_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash, source: replacement,
        }],
      })]);
    }
    operationAttempts += 1;
    return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: {
        description: '只通过绑定的确定性脚本执行计算，脚本失败时停止并返回明确错误。',
        flow: ['校验结构化输入', '调用 script:calculate_script', '脚本失败时停止并报告错误'],
      },
    })]);
  };
  core.setResponses([respond, respond, respond]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, suggestions, llm);

  assert.equal(core.state.callCount, 3);
  assert.equal(scriptAttempts, 2);
  assert.equal(operationAttempts, 1);
  const retryPrompt = prompts.find((item) => item.includes('submit_targeted_function_repair'))!;
  assert.match(retryPrompt, /单函数修正/u);
  assert.match(retryPrompt, /source 过大/u);
  assert.match(retryPrompt, /fieldOrderContract/u);
  assert.match(retryPrompt, /input_record/u);
  assert.doesNotMatch(retryPrompt, /must match a schema in anyOf/u);
  assert.doesNotMatch(retryPrompt, /UNRELATED_SCRIPT_BODY/u);
  assert.match(revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '', /value must be non-negative/u);
  assert.equal(
    revised.operations.find((item) => item.key === 'calculate_operation')?.description,
    '只通过绑定的确定性脚本执行计算，脚本失败时停止并返回明确错误。',
  );
});

test('脚本分片新增 helper 时由服务端托管独占命名空间', async () => {
  const { spec, project, suggestions } = fixture();
  const sourceIndex = inspectPythonSource(source);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const core = createFauxCore({ provider: 'targeted-helper-namespace', models: [{ id: 'targeted-helper-namespace' }] });
  let scriptAttempts = 0;
  let operationAttempts = 0;
  const prompts: string[] = [];
  const respond = (context: unknown) => {
    const serialized = JSON.stringify(context);
    prompts.push(serialized);
    if (serialized.includes('submit_targeted_script_shard')) {
      scriptAttempts += 1;
      const helperName = '_shared_parse_value';
      return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
        scriptKey: 'calculate_script',
        baseSourceHash: sourceIndex.baseSourceHash,
        edits: [{
          kind: 'insert_before_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: [
            `def ${helperName}(value):`,
            '    try:',
            '        number = float(value)',
            '    except (TypeError, ValueError) as exc:',
            '        raise ValueError("invalid value") from exc',
            '    if number < 0:',
            '        raise ValueError("value must be non-negative")',
            '    return number',
          ].join('\n'),
        }, {
          kind: 'replace_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: `def calculate(value):\n    return ${helperName}(value)`,
        }],
      })]);
    }
    operationAttempts += 1;
    return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: {
        description: '只通过绑定的确定性脚本执行计算，脚本失败时停止并返回明确错误。',
        flow: ['校验结构化输入', '调用 script:calculate_script', '脚本失败时停止并报告错误'],
      },
    })]);
  };
  core.setResponses([respond, respond, respond]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, suggestions, llm);
  const revisedSource = revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '';

  assert.equal(core.state.callCount, 2);
  assert.equal(scriptAttempts, 1);
  assert.equal(operationAttempts, 1);
  assert.match(prompts[0], /helperNamePrefix/u);
  assert.match(prompts[0], /_opt_calculate_/u);
  assert.doesNotMatch(revisedSource, /_shared_parse_value/u);
  const generatedName = revisedSource.match(/def (_opt_calculate_h_[a-f0-9]{10}(?:_\d+)?)[(]/u)?.[1];
  assert.ok(generatedName);
  assert.match(revisedSource, new RegExp(`return ${generatedName}\\(value\\)`));
});

test('不可归一化的脚本分片校验失败会把原因交给外层重试', async () => {
  const { spec, project, suggestions } = fixture();
  const adopted = [{
    ...suggestions[0],
    detail: 'calculate 必须使用 DataProviderClient 读取运行时数据源，不得依赖固定值。',
  }, suggestions[1], suggestions[2]];
  const sourceIndex = inspectPythonSource(source);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  let scriptAttempts = 0;
  let operationAttempts = 0;
  const prompts: string[] = [];
  const progress: any[] = [];
  const core = createFauxCore({ provider: 'targeted-capture-outer-retry', models: [{ id: 'targeted-capture-outer-retry' }] });
  const respond = (context: unknown) => {
    const serialized = JSON.stringify(context);
    prompts.push(serialized);
    if (serialized.includes('submit_targeted_operation_patch')) {
      operationAttempts += 1;
      return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
        operationKey: 'calculate_operation',
        patch: { description: '只通过绑定的确定性脚本执行动态计算。' },
      })]);
    }
    if (serialized.includes('submit_targeted_script_shard')) {
      scriptAttempts += 1;
      const helperName = scriptAttempts <= 2 ? '_shared_parse_value' : '_repaired_parse_value';
      const helperBody = scriptAttempts <= 2
        ? [
          `def ${helperName}(value):`,
          '    return float(value)',
          '',
          `def ${helperName}(value):`,
          '    return float(value) + 1',
        ].join('\n')
        : `def ${helperName}(value):\n    return float(value)`;
      return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
        scriptKey: 'calculate_script',
        baseSourceHash: sourceIndex.baseSourceHash,
        edits: [{
          kind: 'insert_before_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: helperBody,
        }, {
          kind: 'replace_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: `def calculate(value):\n    return ${helperName}(value)`,
        }],
      })]);
    }
    operationAttempts += 1;
    return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: { description: '只通过绑定的确定性脚本执行动态计算。' },
    })]);
  };
  core.setResponses([respond, respond, respond, respond]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, adopted, llm, (event) => {
    progress.push(event);
  });

  assert.equal(scriptAttempts, 3);
  assert.equal(operationAttempts, 1);
  assert.equal(core.state.callCount, 4);
  assert.ok(progress.some((event) => event.phase === 'validation_failed' && event.attempt === 1));
  assert.ok(progress.some((event) => event.phase === 'target_plan_ready' && event.attempt === 2));
  assert.ok(prompts.some((item) => item.includes('辅助函数逻辑名')
    && item.includes('多个源码')));
  assert.match(revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '', /def _opt_calculate_h_/u);
});

test('脚本分片连续校验失败后进入人工处理而不是返回结构化提交 500', async () => {
  const { spec, project, suggestions } = fixture();
  const adopted = [{
    ...suggestions[0],
    detail: 'calculate 必须使用 DataProviderClient 读取运行时数据源，不得依赖固定值。',
  }, suggestions[1], suggestions[2]];
  const sourceIndex = inspectPythonSource(source);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const progress: any[] = [];
  const core = createFauxCore({ provider: 'targeted-capture-exhausted', models: [{ id: 'targeted-capture-exhausted' }] });
  const respond = (context: unknown) => {
    if (JSON.stringify(context).includes('submit_targeted_script_shard')) {
      return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
        scriptKey: 'calculate_script',
        baseSourceHash: sourceIndex.baseSourceHash,
        edits: [{
          kind: 'insert_before_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: [
            'def _shared_parse_value(value):',
            '    return float(value)',
            '',
            'def _shared_parse_value(value):',
            '    return float(value) + 1',
          ].join('\n'),
        }, {
          kind: 'replace_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: 'def calculate(value):\n    return _shared_parse_value(value)',
        }],
      })]);
    }
    return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation', patch: { description: '不应到达 operation 提交。' },
    })]);
  };
  core.setResponses([respond, respond, respond, respond, respond]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  await assert.rejects(
    () => applyOptimizations(project, spec, adopted, llm, (event) => { progress.push(event); }),
    /优化器连续 2 次未能通过校验，需要专家确认/u,
  );
  assert.ok(progress.some((event) => event.phase === 'validation_failed' && event.attempt === 2));
});

test('脚本分片模型超时也进入外层重试并在耗尽后等待人工处理', async () => {
  const { spec, project, suggestions } = fixture();
  const adopted = [suggestions[0], suggestions[1], suggestions[2]];
  let calls = 0;
  const progress: any[] = [];
  const core = createFauxCore({ provider: 'targeted-timeout-retry', models: [{ id: 'targeted-timeout-retry' }] });
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: (() => {
      calls += 1;
      throw new AgentRunTimeoutError(1);
    }) as LlmDeps['streamFn'],
  };

  await assert.rejects(
    () => applyOptimizations(project, spec, adopted, llm, (event) => { progress.push(event); }),
    /优化器连续 2 次未能通过校验，需要专家确认/u,
  );
  assert.equal(calls, 2);
  assert.ok(progress.some((event) => event.phase === 'validation_failed' && event.attempt === 1));
  assert.ok(progress.some((event) => event.phase === 'revision_started' && event.attempt === 2));
  assert.ok(progress.some((event) => event.phase === 'validation_failed'
    && event.attempt === 2 && /模型任务执行超时/u.test(event.error)));
});

test('整包校验修正只重跑错误关联的脚本分片', () => {
  const { spec, suggestions } = fixture();
  const multiFunctionSource = source.replace('def main(argv=None):', [
    'def summarize(values):',
    '    return sum(values)',
    '',
    'def main(argv=None):',
  ].join('\n'));
  const targetedSpec = {
    ...spec,
    scripts: [{ ...spec.scripts![0], sourceCode: multiFunctionSource }, spec.scripts![1]],
  };
  const adopted = [{
    ...suggestions[0], detail: 'calculate 必须显式转换 raw_value 并拒绝非法输入。',
  }, {
    ...suggestions[1], detail: 'summarize 必须校验 values 集合后再汇总。',
  }, suggestions[2]];
  const completePlan = planTargetedOptimizationSymbols(adopted, targetedSpec);
  assert.equal(completePlan?.scriptShards.length, 2);
  const repairPlan = planTargetedOptimizationRepairSymbols(
    adopted,
    targetedSpec,
    'script:calculate_script 的 calculate 函数仍将 raw_value 直接参与数值运算',
  );
  assert.equal(repairPlan?.scriptShards.length, 1);
  assert.deepEqual(repairPlan?.scriptShards[0].symbols, ['calculate']);
});

test('整包重试复用既有分片辅助函数而不重复插入同名定义', async () => {
  const { spec, project, suggestions } = fixture();
  const helperSource = source.replace('def calculate(value):\n    return float(value)', [
    'def _opt_calculate_parse_value(value):',
    '    return float(value)',
    '',
    'def calculate(value):',
    '    return _opt_calculate_parse_value(value)',
  ].join('\n'));
  const targetedSpec = {
    ...spec,
    scripts: [{ ...spec.scripts![0], sourceCode: helperSource }, spec.scripts![1]],
  };
  const sourceIndex = inspectPythonSource(helperSource);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const core = createFauxCore({ provider: 'targeted-helper-reuse', models: [{ id: 'targeted-helper-reuse' }] });
  const respond = (context: unknown) => {
    const serialized = JSON.stringify(context);
    if (serialized.includes('submit_targeted_script_shard')) {
      return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
        scriptKey: 'calculate_script',
        baseSourceHash: sourceIndex.baseSourceHash,
        edits: [{
          kind: 'insert_before_function',
          symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: [
            'def _opt_calculate_parse_value(value):',
            '    number = float(value)',
            '    if number < 0:',
            '        raise ValueError("value must be non-negative")',
            '    return number',
          ].join('\n'),
        }, {
          kind: 'replace_function',
          symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: 'def calculate(value):\n    return _opt_calculate_parse_value(value)',
        }],
      })]);
    }
    return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: { description: '只通过确定性脚本校验并计算结构化输入。' },
    })]);
  };
  core.setResponses([respond, respond]);
  const prompts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      prompts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, targetedSpec, suggestions, llm);
  const revisedSource = revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '';
  assert.equal((revisedSource.match(/def _opt_calculate_parse_value/gu) ?? []).length, 1);
  assert.match(revisedSource, /value must be non-negative/u);
  assert.match(prompts.find((item) => item.includes('submit_targeted_script_shard')) ?? '', /existingHelpers/u);
  assert.match(prompts.find((item) => item.includes('submit_targeted_script_shard')) ?? '', /_opt_calculate_parse_value/u);
});

test('多个辅助函数合并超限时按 Python 顶层定义自动拆分且不重试模型', async () => {
  const { spec, project, suggestions } = fixture();
  const sourceIndex = inspectPythonSource(source);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const longHelper = (name: string) => [
    `def ${name}(value):`,
    ...Array.from({ length: 220 }, (_item, index) => `    step_${index} = value`),
    '    return value',
  ].join('\n');
  const firstHelper = longHelper('_opt_calculate_first');
  const secondHelper = longHelper('_opt_calculate_second');
  assert.ok(firstHelper.length < 7_500);
  assert.ok(secondHelper.length < 7_500);
  assert.ok(`${firstHelper}\n\n${secondHelper}`.length > 7_500);

  const core = createFauxCore({ provider: 'targeted-helper-split', models: [{ id: 'targeted-helper-split' }] });
  let scriptAttempts = 0;
  let operationAttempts = 0;
  const respond = (context: unknown) => {
    const serialized = JSON.stringify(context);
    if (serialized.includes('submit_targeted_script_shard')) {
      scriptAttempts += 1;
      return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
        scriptKey: 'calculate_script',
        baseSourceHash: sourceIndex.baseSourceHash,
        edits: [{
          kind: 'insert_before_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: `${firstHelper}\n\n${secondHelper}`,
        }, {
          kind: 'replace_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: [
            'def calculate(value):',
            '    number = float(value)',
            '    if number < 0:',
            '        raise ValueError("value must be non-negative")',
            '    return _opt_calculate_second(_opt_calculate_first(number))',
          ].join('\n'),
        }],
      })]);
    }
    operationAttempts += 1;
    return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: { description: '只通过绑定的确定性脚本执行计算。' },
    })]);
  };
  core.setResponses([respond, respond]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, suggestions, llm);
  const revisedSource = revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '';
  assert.equal(scriptAttempts, 1);
  assert.equal(operationAttempts, 1);
  assert.equal(core.state.callCount, 2);
  assert.ok(revisedSource.indexOf('def _opt_calculate_first')
    < revisedSource.indexOf('def _opt_calculate_second'));
  assert.ok(revisedSource.indexOf('def _opt_calculate_second') < revisedSource.indexOf('def calculate'));
});

test('泛化缺省回退建议覆盖全部定义和引用并对残留函数执行局部修正', async () => {
  const { spec, project, suggestions } = fixture();
  const defaultSource = [
    'import argparse',
    'import json',
    '',
    'DEFAULT_CACHE = {"value": 1}',
    '',
    'def calculate(value):',
    '    raw = DEFAULT_CACHE["value"] if value is None else value',
    '    return float(raw)',
    '',
    'def main(argv=None):',
    '    parser = argparse.ArgumentParser()',
    '    parser.add_argument("--value", required=True)',
    '    args = parser.parse_args(argv)',
    '    print(json.dumps({"result": calculate(args.value)}))',
    '',
    'if __name__ == "__main__":',
    '    main()',
  ].join('\n');
  const targetedSpec = {
    ...spec,
    scripts: [{ ...spec.scripts![0], sourceCode: defaultSource }, spec.scripts![1]],
  };
  const adopted = [{
    ...suggestions[0],
    title: '修复数据装载并禁止静默回退硬编码默认值',
    detail: 'calculate 必须直接校验输入，数据缺失时明确报错，不能继续使用内置缺省基准。',
  }, suggestions[1], suggestions[2]];
  const sourceIndex = inspectPythonSource(defaultSource);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const core = createFauxCore({ provider: 'targeted-default-delete', models: [{ id: 'targeted-default-delete' }] });
  let scriptAttempts = 0;
  const prompts: string[] = [];
  const respond = (context: unknown) => {
    const serialized = JSON.stringify(context);
    prompts.push(serialized);
    if (serialized.includes('submit_targeted_function_repair')) {
      scriptAttempts += 1;
      return fauxAssistantMessage([fauxToolCall('submit_targeted_function_repair', {
        source: [
          'def calculate(value):',
          '    """Do not use DEFAULT_CACHE as a runtime fallback."""',
          '    if value is None:',
          '        raise ValueError("value is required")',
          '    return float(value)',
        ].join('\n'),
      })]);
    }
    if (serialized.includes('submit_targeted_script_shard')) {
      scriptAttempts += 1;
      return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
        scriptKey: 'calculate_script',
        baseSourceHash: sourceIndex.baseSourceHash,
        edits: [{
          kind: 'replace_function',
          symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: 'def calculate(value):\n    return float(DEFAULT_CACHE["value"] if value is None else value)',
        }],
      })]);
    }
    return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: { description: '只通过确定性脚本校验输入并执行计算。' },
    })]);
  };
  core.setResponses([respond, respond, respond]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, targetedSpec, adopted, llm);
  const revisedSource = revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '';
  assert.match(revisedSource, /DEFAULT_CACHE/u);
  assert.equal(pythonSourceIdentifierNames(revisedSource).includes('DEFAULT_CACHE'), false);
  assert.match(revisedSource, /value is required/u);
  assert.equal(scriptAttempts, 2);
  assert.equal(core.state.callCount, 3);
  const initialShardPrompt = prompts.filter((item) => item.includes('submit_targeted_script_shard'))[0];
  assert.equal(initialShardPrompt.includes('\\"kind\\":\\"assignment\\",\\"qualifiedName\\":\\"DEFAULT_CACHE\\"'), false);
  assert.equal(initialShardPrompt.includes('\\"serverManaged\\":{\\"deleteAssignments\\":[\\"DEFAULT_CACHE\\"]}'), true);
  assert.match(initialShardPrompt, /serverManaged\.deleteAssignments\/deleteFunctions 由服务端自动删除/u);
  const repairPrompt = prompts.find((item) => item.includes('submit_targeted_function_repair'))!;
  assert.match(repairPrompt, /DEFAULT_CACHE/u);
  assert.doesNotMatch(repairPrompt, /Received arguments/u);
});

test('缺省依赖分片首稿漏掉目标函数时通过单函数工具补齐并合并已有 helper', async () => {
  const { spec, project, suggestions } = fixture();
  const defaultSource = [
    'import argparse',
    'import json',
    '',
    'DEFAULT_CACHE = {"value": 1}',
    '',
    'def calculate(value):',
    '    return float(DEFAULT_CACHE["value"] if value is None else value)',
    '',
    'def main(argv=None):',
    '    parser = argparse.ArgumentParser()',
    '    parser.add_argument("--value", required=True)',
    '    args = parser.parse_args(argv)',
    '    print(json.dumps({"result": calculate(args.value)}))',
    '',
    'if __name__ == "__main__":',
    '    main()',
  ].join('\n');
  const targetedSpec = {
    ...spec,
    scripts: [{ ...spec.scripts![0], sourceCode: defaultSource }, spec.scripts![1]],
  };
  const adopted = [{
    ...suggestions[0],
    title: '删除缺省输入路径',
    detail: 'calculate 必须删除全部 DEFAULT_ 缺省值，输入缺失时明确失败。',
  }, suggestions[1], suggestions[2]];
  const sourceIndex = inspectPythonSource(defaultSource);
  const calculateNode = sourceIndex.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const core = createFauxCore({ provider: 'targeted-missing-function', models: [{ id: 'targeted-missing-function' }] });
  let scriptAttempts = 0;
  let repairAttempts = 0;
  const respond = (context: unknown) => {
    const serialized = JSON.stringify(context);
    if (serialized.includes('submit_targeted_function_repair')) {
      repairAttempts += 1;
      return fauxAssistantMessage([fauxToolCall('submit_targeted_function_repair', {
        source: [
          'def calculate(value):',
          '    return _opt_calculate_required(value)',
        ].join('\n'),
      })]);
    }
    if (serialized.includes('submit_targeted_script_shard')) {
      scriptAttempts += 1;
      return fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
        scriptKey: 'calculate_script',
        baseSourceHash: sourceIndex.baseSourceHash,
        edits: [{
          kind: 'insert_before_function', symbol: 'calculate',
          expectedNodeHash: calculateNode.nodeHash,
          source: [
            'def _opt_calculate_required(value):',
            '    if value is None:',
            '        raise ValueError("value is required")',
            '    return float(value)',
          ].join('\n'),
        }],
      })]);
    }
    return fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: { description: '只通过确定性脚本校验结构化输入。' },
    })]);
  };
  core.setResponses([respond, respond, respond]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, targetedSpec, adopted, llm);
  const revisedSource = revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '';
  assert.equal(scriptAttempts, 1);
  assert.equal(repairAttempts, 1);
  assert.match(revisedSource, /def _opt_calculate_required/u);
  assert.match(revisedSource, /return _opt_calculate_required\(value\)/u);
  assert.equal(pythonSourceIdentifierNames(revisedSource).includes('DEFAULT_CACHE'), false);
});

test('纯缺省常量删除分片由服务端直接执行且不消耗脚本模型调用', async () => {
  const { spec, project, suggestions } = fixture();
  const sourceWithUnusedDefault = [
    'import argparse',
    'import json',
    'from shared_capabilities.ontology_script_sdk import DataProviderClient',
    '',
    'DEFAULT_UNUSED = 1',
    '',
    'def _load_platform_json(path):',
    '    with open(path, "r", encoding="utf-8") as handle:',
    '        return json.load(handle)',
    '',
    'def calculate(value):',
    '    return float(value)',
    '',
    'def main(argv=None):',
    '    parser = argparse.ArgumentParser()',
    '    parser.add_argument("--value", required=True)',
    '    args = parser.parse_args(argv)',
    '    print(json.dumps({"result": calculate(args.value)}))',
    '',
    'if __name__ == "__main__":',
    '    main()',
  ].join('\n');
  const targetedSpec = {
    ...spec,
    scripts: [{ ...spec.scripts![0], sourceCode: sourceWithUnusedDefault }, spec.scripts![1]],
  };
  const adopted = [{
    ...suggestions[0],
    title: '删除未使用的缺省常量',
    detail: '删除全部 DEFAULT_ 缺省值，禁止静默回退硬编码默认值；真实平台数据必须通过 DataProviderClient 读取，不得保留本地文件读取。',
  }, {
    ...suggestions[1],
    title: '禁止缺省数据路径',
    detail: '不得保留任何 DEFAULT_ 常量，数据必须来自结构化输入。',
  }, suggestions[2]];
  const core = createFauxCore({ provider: 'targeted-server-delete', models: [{ id: 'targeted-server-delete' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: { description: '只通过结构化输入执行计算。' },
    })]),
  ]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, targetedSpec, adopted, llm);
  const revisedSource = revised.scripts.find((item) => item.key === 'calculate_script')?.sourceCode ?? '';
  assert.doesNotMatch(revisedSource, /DEFAULT_UNUSED/u);
  assert.doesNotMatch(revisedSource, /def _load_platform_json/u);
  assert.equal(core.state.callCount, 1);
});

test('单条 operation 建议直接进入目标资源增量协议', () => {
  const { spec, suggestions } = fixture();
  const scope = groupTargetedOptimizationSuggestions([suggestions[2]], spec);
  assert.deepEqual(scope?.changeSets, [{
    id: 'operation:calculate_operation',
    resourceKeys: ['operation:calculate_operation'],
    suggestionIds: ['s-operation'],
  }]);
  assert.equal(scope?.scriptKeys.size, 0);
  assert.deepEqual([...scope?.operationKeys ?? []], ['calculate_operation']);
});

test('单条 operation 建议只携带目标资源并生成字段补丁', async () => {
  const { spec, project, suggestions } = fixture();
  const core = createFauxCore({ provider: 'single-operation-targeted', models: [{ id: 'single-operation-targeted' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
    operationKey: 'calculate_operation',
    patch: { description: '脚本是唯一权威计算路径。' },
  })])]);
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestions[2]], llm);

  assert.equal(core.state.callCount, 1);
  assert.equal(revised.operations[0].description, '脚本是唯一权威计算路径。');
  assert.equal(revised.operations[1].description, 'UNRELATED_SECRET_FLOW');
  assert.doesNotMatch(contexts[0], /UNRELATED_SECRET_FLOW/u);
  assert.doesNotMatch(contexts[0], /UNRELATED_SCRIPT_BODY/u);
});

test('单条 script 建议使用符号级补丁且不携带无关脚本', async () => {
  const { spec, project, suggestions } = fixture();
  const index = inspectPythonSource(source);
  const calculate = index.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const core = createFauxCore({ provider: 'single-script-targeted', models: [{ id: 'single-script-targeted' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
    scriptKey: 'calculate_script',
    baseSourceHash: index.baseSourceHash,
    edits: [{
      kind: 'replace_function', symbol: 'calculate', expectedNodeHash: calculate.nodeHash,
      source: 'def calculate(value):\n    number = float(value)\n    if number < 0:\n        raise ValueError("negative")\n    return number',
    }],
  })])]);
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestions[0]], llm);

  assert.equal(core.state.callCount, 1);
  assert.match(revised.scripts[0].sourceCode, /number < 0/u);
  assert.equal(revised.scripts[1].sourceCode, unrelatedSource);
  assert.doesNotMatch(contexts[0], /UNRELATED_SCRIPT_BODY/u);
  assert.doesNotMatch(contexts[0], /UNRELATED_SECRET_FLOW/u);
});

test('无法确定 Python 符号时仅回退提交单个目标 script', async () => {
  const { spec, project, suggestions } = fixture();
  const adopted: OptSuggestion = {
    ...suggestions[0],
    id: 's-script-resource-fallback',
    title: '提升异常处理健壮性',
    detail: '对目标实现增加通用输入合法性检查，并返回明确错误。',
    generalizationPlan: '适用于所有结构化数值输入。',
    validationPlan: '覆盖合法值、非法值和边界值。',
  };
  const changedSource = source.replace(
    '    return float(value)',
    '    number = float(value)\n    if number < 0:\n        raise ValueError("negative")\n    return number',
  );
  const core = createFauxCore({ provider: 'script-resource-fallback', models: [{ id: 'script-resource-fallback' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_optimization_patch', {
    scripts: [{ ...spec.scripts![0], sourceCode: changedSource }],
  })])]);
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [adopted], llm);

  assert.equal(core.state.callCount, 1);
  assert.equal(revised.scripts[0].sourceCode, changedSource);
  assert.equal(revised.scripts[0].runtime, spec.scripts![0].runtime);
  assert.equal(revised.scripts[0].boundOperation, spec.scripts![0].boundOperation);
  assert.equal(revised.scripts[0].entrypoint, spec.scripts![0].entrypoint);
  assert.equal(revised.scripts[1].sourceCode, unrelatedSource);
  assert.doesNotMatch(contexts[0], /UNRELATED_SCRIPT_BODY/u);
  assert.doesNotMatch(contexts[0], /UNRELATED_SECRET_FLOW/u);
  assert.doesNotMatch(contexts[0], /submit_targeted_script_shard/u);
});

test('单条 operation 输出截断后使用干净目标上下文重试', async () => {
  const { spec, project, suggestions } = fixture();
  const core = createFauxCore({ provider: 'operation-length-recovery', models: [{ id: 'operation-length-recovery' }] });
  core.setResponses([
    fauxAssistantMessage('TRUNCATED_OPERATION_RESPONSE_MARKER', { stopReason: 'length' }),
    fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
      operationKey: 'calculate_operation',
      patch: { description: '截断恢复后仍以脚本作为唯一计算路径。' },
    })]),
  ]);
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestions[2]], llm);

  assert.equal(core.state.callCount, 2);
  assert.equal(revised.operations[0].description, '截断恢复后仍以脚本作为唯一计算路径。');
  assert.doesNotMatch(contexts[1], /TRUNCATED_OPERATION_RESPONSE_MARKER/u);
  assert.doesNotMatch(contexts[1], /UNRELATED_SECRET_FLOW/u);
});

test('25 项资源中的单条建议不会携带无关资源正文和长脚本', async () => {
  const { spec, project, suggestions } = fixture();
  const extraObjects = Array.from({ length: 20 }, (_, index) => ({
    key: `extra_object_${index + 1}`,
    title: `扩展对象 ${index + 1}`,
    description: `UNRELATED_RESOURCE_BODY_${index + 1}`,
    fields: [],
  }));
  const largeSpec: PackageSpec = {
    ...spec,
    objects: [...spec.objects, ...extraObjects],
    scripts: [spec.scripts![0], {
      ...spec.scripts![1],
      sourceCode: `UNRELATED_LONG_SCRIPT_MARKER = "${'x'.repeat(20_000)}"\n${unrelatedSource}`,
    }],
  };
  const resourceCount = largeSpec.objects.length + largeSpec.operations.length
    + (largeSpec.rules?.length ?? 0) + (largeSpec.metrics?.length ?? 0) + (largeSpec.scripts?.length ?? 0);
  assert.equal(resourceCount, 25);
  const core = createFauxCore({ provider: 'large-package-targeted', models: [{ id: 'large-package-targeted' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_targeted_operation_patch', {
    operationKey: 'calculate_operation',
    patch: { description: '在大型知识包中仍只修改目标操作。' },
  })])]);
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, largeSpec, [suggestions[2]], llm);

  assert.equal(core.state.callCount, 1);
  assert.equal(revised.operations[0].description, '在大型知识包中仍只修改目标操作。');
  assert.doesNotMatch(contexts[0], /UNRELATED_RESOURCE_BODY_/u);
  assert.doesNotMatch(contexts[0], /UNRELATED_LONG_SCRIPT_MARKER/u);
  assert.ok(contexts[0].length < JSON.stringify(largeSpec).length / 2);
});

test('单条 script 输出截断后使用新 Agent 重试当前符号分片', async () => {
  const { spec, project, suggestions } = fixture();
  const index = inspectPythonSource(source);
  const calculate = index.symbols.find((item) => item.qualifiedName === 'calculate')!;
  const core = createFauxCore({ provider: 'script-length-recovery', models: [{ id: 'script-length-recovery' }] });
  core.setResponses([
    fauxAssistantMessage('TRUNCATED_SCRIPT_RESPONSE_MARKER', { stopReason: 'length' }),
    fauxAssistantMessage([fauxToolCall('submit_targeted_script_shard', {
      scriptKey: 'calculate_script',
      baseSourceHash: index.baseSourceHash,
      edits: [{
        kind: 'replace_function', symbol: 'calculate', expectedNodeHash: calculate.nodeHash,
        source: 'def calculate(value):\n    number = float(value)\n    if number < 0:\n        raise ValueError("negative")\n    return number',
      }],
    })]),
  ]);
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestions[0]], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /number < 0/u);
  assert.doesNotMatch(contexts[1], /TRUNCATED_SCRIPT_RESPONSE_MARKER/u);
  assert.doesNotMatch(contexts[1], /UNRELATED_SCRIPT_BODY/u);
});

test('Object Rule Metric 建议通过一次紧凑目标资源调用合并', async () => {
  const { spec, project } = fixture();
  const extended: PackageSpec = {
    ...spec,
    objects: [...spec.objects, {
      key: 'unrelated_object', title: '无关对象', description: 'UNRELATED_OBJECT_MARKER', fields: [],
    }],
    rules: [{
      key: 'capacity_rule', title: '容量规则', content: '容量必须大于零。', evidence: ['document:rule-source'],
    }],
    metrics: [{
      key: 'profit_metric', title: '利润指标', caliber: '收入减成本。', boundaries: ['仅统计有效记录。'],
      evidence: ['document:metric-source'],
    }],
  };
  const base = {
    projectId: project.id, roundId: 'r-targeted', basedVersion: '1.0.0', status: 'pending' as const,
    createdAt: '2026-09-02T00:00:00.000Z',
  };
  const adopted: OptSuggestion[] = [{
    ...base, id: 's-object', kind: 'modify_object', target: 'input_record', title: '明确输入对象',
    detail: '说明输入记录只接受结构化数值。', generalizationPlan: '适用于所有输入记录。', validationPlan: '检查描述。',
  }, {
    ...base, id: 's-rule', kind: 'modify_rule', target: 'capacity_rule', title: '明确容量规则',
    detail: '容量必须是有效正数。', generalizationPlan: '适用于所有容量值。', validationPlan: '覆盖非法容量。',
  }, {
    ...base, id: 's-metric', kind: 'modify_metric', target: 'profit_metric', title: '明确利润口径',
    detail: '利润按有效收入减去有效成本。', generalizationPlan: '适用于所有有效记录。', validationPlan: '覆盖缺失值。',
  }];
  const core = createFauxCore({ provider: 'standalone-resource-targeted', models: [{ id: 'standalone-resource-targeted' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_optimization_patch', {
    objects: [{ ...extended.objects[0], description: '只接受经过校验的结构化数值。' }],
    rules: [{ ...extended.rules![0], content: '容量必须是经过校验的有效正数。', evidence: [] }],
    metrics: [{ ...extended.metrics![0], caliber: '有效收入减去有效成本。', evidence: [] }],
  })])]);
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, extended, adopted, llm);

  assert.equal(core.state.callCount, 1);
  assert.equal(revised.objects[0].description, '只接受经过校验的结构化数值。');
  assert.equal(revised.objects[1].description, 'UNRELATED_OBJECT_MARKER');
  assert.equal(revised.rules[0].content, '容量必须是经过校验的有效正数。');
  assert.equal(revised.rules[0].evidence?.includes('document:rule-source'), true);
  assert.equal(revised.metrics[0].caliber, '有效收入减去有效成本。');
  assert.equal(revised.metrics[0].evidence?.includes('document:metric-source'), true);
  assert.doesNotMatch(contexts[0], /UNRELATED_OBJECT_MARKER/u);
  assert.doesNotMatch(contexts[0], /UNRELATED_SCRIPT_BODY/u);
});

test('目标资源连续两次输出截断后进入人工处理且不产生候选内容', async () => {
  const { spec, project } = fixture();
  const adopted: OptSuggestion[] = [{
    projectId: project.id, roundId: 'r-targeted', basedVersion: '1.0.0', status: 'pending',
    createdAt: '2026-09-02T00:00:00.000Z', id: 's-object-length', kind: 'modify_object',
    target: 'input_record', title: '明确输入对象', detail: '说明输入对象必须经过校验。',
    generalizationPlan: '适用于所有结构化输入。', validationPlan: '检查输入边界。',
  }];
  const core = createFauxCore({ provider: 'resource-length-exhausted', models: [{ id: 'resource-length-exhausted' }] });
  core.setResponses([
    fauxAssistantMessage('FIRST_TRUNCATED_RESOURCE', { stopReason: 'length' }),
    fauxAssistantMessage('SECOND_TRUNCATED_RESOURCE', { stopReason: 'length' }),
  ]);
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux', streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  await assert.rejects(
    () => applyOptimizations(project, spec, adopted, llm),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.name, 'OptimizationExpertReviewRequiredError');
      assert.match(error.message, /模型输出达到长度上限/u);
      assert.match(error.message, /已自动尝试 2 次/u);
      return true;
    },
  );
  assert.equal(core.state.callCount, 2);
});

test('八个目标资源截断后按 8-4-2-1 动态拆批并完整合并', async () => {
  const { spec, project } = fixture();
  const objects = Array.from({ length: 8 }, (_, index) => ({
    key: `batch_object_${index + 1}`,
    title: `批次对象 ${index + 1}`,
    description: `原始描述 ${index + 1}`,
    fields: [],
  }));
  const largeSpec: PackageSpec = { ...spec, objects: [...spec.objects, ...objects] };
  const adopted: OptSuggestion[] = objects.map((object, index) => ({
    projectId: project.id, roundId: 'r-batch-split', basedVersion: '1.0.0', status: 'pending',
    createdAt: '2026-09-02T00:00:00.000Z', id: `s-batch-${index + 1}`, kind: 'modify_object',
    target: object.key, title: `更新 ${object.title}`, detail: `将描述更新为批次结果 ${index + 1}`,
  }));
  const changed = objects.map((object, index) => ({ ...object, description: `批次结果 ${index + 1}` }));
  const submit = (items: typeof changed) => fauxAssistantMessage([
    fauxToolCall('submit_optimization_patch', { objects: items }),
  ]);
  const core = createFauxCore({ provider: 'resource-batch-split', models: [{ id: 'resource-batch-split' }] });
  core.setResponses([
    fauxAssistantMessage('TRUNCATED_BATCH_8', { stopReason: 'length' }),
    fauxAssistantMessage('TRUNCATED_BATCH_4', { stopReason: 'length' }),
    fauxAssistantMessage('TRUNCATED_BATCH_2', { stopReason: 'length' }),
    submit(changed.slice(0, 1)),
    submit(changed.slice(1, 2)),
    submit(changed.slice(2, 4)),
    submit(changed.slice(4, 8)),
  ]);
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, largeSpec, adopted, llm);

  assert.equal(core.state.callCount, 7);
  changed.forEach((object) => {
    assert.equal(revised.objects.find((item) => item.key === object.key)?.description, object.description);
  });
  assert.doesNotMatch(contexts[1], /TRUNCATED_BATCH_8/u);
  assert.ok(contexts[3].length < contexts[0].length);
});

test('有明确 key 的新增 Object Rule Metric 使用目标补丁而非完整规格', async () => {
  const { spec, project } = fixture();
  const addedObject = { key: 'new_record', title: '新增记录', description: '新增结构化记录。', fields: [] };
  const addedRule = {
    key: 'new_rule', title: '新增规则', content: '新增记录必须满足通用完整性约束。',
    appliesToObjects: ['new_record'],
  };
  const addedMetric = {
    key: 'new_metric', title: '新增指标', caliber: '按有效新增记录计数。',
    boundaries: ['仅统计字段完整的新增记录。'], usesObjects: ['new_record'],
  };
  const common = {
    projectId: project.id, roundId: 'r-add-targeted', basedVersion: '1.0.0', status: 'pending' as const,
    createdAt: '2026-09-02T00:00:00.000Z',
  };
  const adopted: OptSuggestion[] = [
    { ...common, id: 's-add-object', kind: 'add_object', target: addedObject.key, title: '新增记录对象', detail: '新增通用记录对象。' },
    { ...common, id: 's-add-rule', kind: 'add_rule', target: addedRule.key, title: '新增完整性规则', detail: '新增通用完整性约束。' },
    { ...common, id: 's-add-metric', kind: 'add_metric', target: addedMetric.key, title: '新增记录指标', detail: '新增通用计数指标。' },
  ];
  const core = createFauxCore({ provider: 'targeted-additions', models: [{ id: 'targeted-additions' }] });
  core.setResponses(Array.from({ length: 2 }, () => fauxAssistantMessage([fauxToolCall('submit_optimization_patch', {
    objects: [addedObject], rules: [addedRule], metrics: [addedMetric],
  })])));
  const contexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: unknown, options: unknown) => {
      contexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context as any, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, adopted, llm);

  assert.ok(core.state.callCount <= 2);
  assert.ok(revised.objects.some((item) => item.key === addedObject.key));
  assert.ok(revised.rules.some((item) => item.key === addedRule.key));
  assert.ok(revised.metrics.some((item) => item.key === addedMetric.key));
  assert.doesNotMatch(contexts[0], /submit_revised_spec/u);
});

test('无法定位符号的超长 Script 不要求模型完整重写并转为人工闭环', async () => {
  const { spec, project } = fixture();
  const longSource = `payload = "${'x'.repeat(25_000)}"\nprint(payload)`;
  const longSpec: PackageSpec = {
    ...spec,
    scripts: [{ ...spec.scripts![0], sourceCode: longSource }, spec.scripts![1]],
  };
  const adopted: OptSuggestion = {
    projectId: project.id, roundId: 'r-long-script', basedVersion: '1.0.0', status: 'pending',
    createdAt: '2026-09-02T00:00:00.000Z', id: 's-long-script', kind: 'modify_script',
    target: 'calculate_script', title: '修改超长脚本', detail: '调整无法定位到现有符号的顶层执行逻辑。',
  };
  const core = createFauxCore({ provider: 'long-script-manual', models: [{ id: 'long-script-manual' }] });
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux', streamFn: core.streamSimple as LlmDeps['streamFn'],
  };

  await assert.rejects(
    () => applyOptimizations(project, longSpec, [adopted], llm),
    /无法安全定位修改符号.*已转为人工修订/u,
  );
  assert.equal(core.state.callCount, 0);
});

test('复合脚本建议按显式符号和语义注释确定性拆分且不带入无关函数', () => {
  const { spec, suggestions } = fixture();
  const planningSource = [
    'DEFAULT_TANKS = []',
    'DEFAULT_PRICES = {}',
    'DEFAULT_OPTIMIZE = {"a": 1, "b": 1}',
    'DEFAULT_UNIT_CAPACITY = {}',
    'DEFAULT_ENERGY = []',
    'DEFAULT_CONNECTIONS = []',
    'DEFAULT_FLOWS = []',
    'BASE_YIELDS = {}',
    '',
    'def load_reference_data():',
    '    """装载收率、装置能力、罐参数和价格；数据缺失时必须终止。"""',
    '    return {}',
    '',
    'def process_batch(batch, data):',
    '    """逐批计算侧线产量、路线收入、能源成本和装置输入。"""',
    '    return {}',
    '',
    'def aggregate_month(batches, results):',
    '    """聚合月度装置负荷、实际天数和总利润。"""',
    '    return {}',
    '',
    'def check_tanks(tanks, batch):',
    '    """逐批模拟罐库存并判定罐容风险。"""',
    '    return False',
    '',
    'def monthly_profit(routes, data):',
    '    """按实际产品收率、价格和能耗核算路线收益。"""',
    '    return 0',
    '',
    'def resolve_crude_type(name, mappings):',
    '    """解析组合型原油名称并使用显式映射。"""',
    '    return name',
    '',
    'def unrelated_audit():',
    '    return "must stay untouched"',
  ].join('\n');
  const targetedSpec = {
    ...spec,
    scripts: [{ ...spec.scripts![0], sourceCode: planningSource }, spec.scripts![1]],
  };
  const base = suggestions[0];
  const actualSuggestions: OptSuggestion[] = [{
    ...base, id: 's-load', title: '修复数据装载',
    detail: 'load_reference_data 必须读取真实 schema，删除全部 DEFAULT_ 缺省值。',
    validationPlan: '验证 aggregate_month 的下游结果没有再使用缺省数据。',
  }, {
    ...base, id: 's-load-rate', title: '修复装置负荷',
    detail: 'process_batch 计算侧线产量，aggregate_month 按侧线产量和实际天数累计装置负荷，不得直接累加整批加工量。',
  }, {
    ...base, id: 's-tank', title: '修复罐容动态模拟',
    detail: 'check_tanks 逐批更新库存，不得使用 DEFAULT_TANKS。',
  }, {
    ...base, id: 's-profit', title: '修复经济效益',
    detail: 'monthly_profit 按源表收率与价格分路线核算，删除 BASE_YIELDS 和 DEFAULT_PRICES。',
  }, {
    ...base, id: 's-crude', title: '修复组合原油映射',
    detail: 'resolve_crude_type 必须按映射表解析组合型原油名称，无法解析时明确报错。',
  }, suggestions[2]];

  const plan = planTargetedOptimizationSymbols(actualSuggestions, targetedSpec)!;
  const symbols = new Set(plan.scriptShards.flatMap((item) => item.symbols));
  for (const required of [
    'load_reference_data', 'process_batch', 'aggregate_month', 'check_tanks',
    'monthly_profit', 'resolve_crude_type',
    'DEFAULT_TANKS', 'DEFAULT_PRICES', 'DEFAULT_OPTIMIZE', 'DEFAULT_UNIT_CAPACITY',
    'DEFAULT_ENERGY', 'DEFAULT_CONNECTIONS', 'DEFAULT_FLOWS', 'BASE_YIELDS',
  ]) assert.equal(symbols.has(required), true, required);
  assert.equal(symbols.has('unrelated_audit'), false);
  const loadShard = plan.scriptShards.find((item) => item.symbols.includes('load_reference_data'))!;
  assert.equal(loadShard.symbols.includes('aggregate_month'), false);
  assert.equal(loadShard.symbols.includes('process_batch'), false);
  const loadRateShard = plan.scriptShards.find((item) => item.symbols.includes('process_batch'))!;
  assert.equal(loadRateShard.symbols.includes('aggregate_month'), false);
  assert.equal(loadRateShard.suggestionIds.includes('s-load-rate'), true);
  assert.equal(loadRateShard.suggestionIds.includes('s-profit'), false);
  assert.equal(loadRateShard.suggestionIds.includes('s-tank'), false);
  const aggregateShard = plan.scriptShards.find((item) => item.symbols.includes('aggregate_month'))!;
  assert.equal(aggregateShard.suggestionIds.includes('s-load-rate'), true);
  const functionNames = new Set([
    'load_reference_data', 'process_batch', 'aggregate_month', 'check_tanks',
    'monthly_profit', 'resolve_crude_type',
  ]);
  assert.ok(plan.scriptShards.every((item) => item.symbols.filter((symbol) => functionNames.has(symbol)).length <= 1));
  assert.equal(plan.scriptShards.length, 6);
  assert.ok(plan.scriptShards.length <= 8);
});

test('正式契约确认的脚本缺省值不被评测派生建议机械删除', () => {
  const { spec, suggestions } = fixture();
  const contractSource = [
    'DEFAULT_PRICES = {"jet": 5000.0, "fuel": 3500.0, "naphtha": 4200.0}',
    'DEFAULT_OPTIMIZE = {"a": 1.0, "b": 1.0}',
    'DEFAULT_TANKS = []',
    '',
    'def load_reference_data():',
    '    return {"prices": DEFAULT_PRICES, "optimize": DEFAULT_OPTIMIZE, "tanks": DEFAULT_TANKS}',
    '',
    'def monthly_profit(amounts):',
    '    return sum(amounts.get(key, 0) * price for key, price in DEFAULT_PRICES.items())',
    '',
    'def main():',
    '    print(load_reference_data())',
  ].join('\n');
  const targetedSpec: PackageSpec = {
    ...spec,
    objects: [...spec.objects, {
      key: 'devices_tanks', title: '储罐', description: '运行时储罐数据', sourceTable: 'devices_tanks',
      fields: [{ key: 'tank_id', cnName: '储罐编号', sourceColumn: 'tank_id', type: 'string' }],
    }],
    operations: spec.operations.map((operation) => operation.key === 'calculate_operation'
      ? {
          ...operation,
          relatedObjects: [...operation.relatedObjects, 'devices_tanks'],
          usesRules: ['optimize_coefficient_rule'],
          usesMetrics: ['product_revenue_metric'],
          notes: ['产品价格缺失时按已确认默认价格表处理。'],
        }
      : operation),
    rules: [{
      key: 'optimize_coefficient_rule', title: '优化系数规则', description: '优化系数处理',
      content: '优化系数缺失时按 A=B=1 处理。', evidence: [], conditions: [], exceptions: [],
      appliesToObjects: [], appliesToOperations: ['calculate_operation'],
    }],
    metrics: [{
      key: 'product_revenue_metric', title: '产品收入', description: '产品收入核算',
      caliber: '产品产量乘以产品单价', boundaries: ['产品单价采用已确认默认单价表。'], evidence: [],
      unit: '元', grain: '月', nullPolicy: [], dimensions: [], usesObjects: [],
    }],
    scripts: spec.scripts!.map((script) => script.key === 'calculate_script'
      ? {
          ...script,
          sourceCode: contractSource,
          boundaries: [
            ...script.boundaries,
            '产品单价默认：航煤5000、燃料油3500、石脑油4200元/吨。',
          ],
        }
      : script),
  };
  const base = suggestions[0];
  const adopted: OptSuggestion[] = [{
    ...base,
    id: 's-defaults',
    title: '修复数据装载',
    detail: 'load_reference_data 必须读取真实字段，删除全部 DEFAULT_ 静默回退。',
  }, {
    ...base,
    id: 's-profit',
    title: '修复产品收入',
    detail: 'monthly_profit 按契约核算产品收入并删除 DEFAULT_PRICES。',
  }, suggestions[2]];

  const plan = planTargetedOptimizationSymbols(adopted, targetedSpec)!;
  const symbols = new Set(plan.scriptShards.flatMap((item) => item.symbols));
  assert.equal(symbols.has('DEFAULT_PRICES'), false);
  assert.equal(symbols.has('DEFAULT_OPTIMIZE'), false);
  assert.equal(symbols.has('DEFAULT_TANKS'), true);
  assert.equal(symbols.has('load_reference_data'), true);
  assert.equal(symbols.has('monthly_profit'), true);
});

test('候选结构门禁识别未接入 helper、装载键断链和残留文件读取', () => {
  const before = [
    'def load_reference_data():',
    '    return {"prices": {}}',
    '',
    'def process_batch(data):',
    '    return data.get("prices", {})',
  ].join('\n');
  const after = [
    'from shared_capabilities.ontology_script_sdk import DataProviderClient',
    '',
    'def _load_platform_json(path):',
    '    with open(path, "r", encoding="utf-8") as handle:',
    '        return json.load(handle)',
    '',
    'def _opt_process_batch_build_side_lines(rows):',
    '    return {row["id"]: row for row in rows}',
    '',
    'def load_reference_data():',
    '    with DataProviderClient() as client:',
    '        prices = client.query("prices").data',
    '    return {"prices": prices}',
    '',
    'def process_batch(data):',
    '    return data.get("side_lines_map", {})',
    '',
    'def aggregate_routes(rows):',
    '    diesel_sids = ["cjy_01_1", "cjy_01_2", "cjy_01_3"]',
    '    return [row for row in rows if row["side_line_id"] in diesel_sids]',
  ].join('\n');

  assert.deepEqual(unreachableNewOptimizerHelpers(before, after), [{
    helper: '_opt_process_batch_build_side_lines', owner: 'process_batch',
  }]);
  assert.deepEqual(loaderOutputDisconnects(after), ['side_lines_map']);
  assert.deepEqual(fileDataLoaderFunctions(after), ['_load_platform_json']);
  assert.deepEqual(hardCodedRuntimeIdentifierCollections(after), ['diesel_sids']);
  assert.doesNotMatch(pruneUnreachableNewOptimizerHelpers(before, after), /def _opt_process_batch_build_side_lines/u);
  assert.equal(hasReachableDynamicTankInventory(after), false);

  const dynamicTankSource = [
    'def _opt_check_tanks_update(initial_capacity, inflow, outflow):',
    '    inventory = initial_capacity + inflow - outflow',
    '    return inventory',
    '',
    'def check_tanks(data):',
    '    initial_capacity = data["initial_capacity"]',
    '    connections = data["connections"]',
    '    inflow = sum(item["amount"] for item in connections if item["direction"] == "in")',
    '    outflow = sum(item["amount"] for item in connections if item["direction"] == "out")',
    '    return _opt_check_tanks_update(initial_capacity, inflow, outflow)',
  ].join('\n');
  assert.equal(hasReachableDynamicTankInventory(dynamicTankSource), false);
});

test('计算语义门禁区分固定周期、原始加工量和跨批状态', () => {
  const fixedPeriodSource = [
    'def aggregate_month(batches, results, daily_capacity):',
    '    total_amount = sum(item["process_amount"] for item in batches)',
    '    monthly_capacity = daily_capacity * 17',
    '    return total_amount / monthly_capacity',
  ].join('\n');
  const actualPeriodSource = [
    'def aggregate_month(batches, results, daily_capacity):',
    '    total_amount = sum(item["process_amount"] for item in batches)',
    '    total_period_days = sum(float(item["period_days"]) for item in batches)',
    '    monthly_capacity = daily_capacity * total_period_days',
    '    return total_amount / monthly_capacity',
  ].join('\n');
  const disconnectedPeriodSource = [
    'def aggregate_month(batches, results, daily_capacity):',
    '    total_period_days = sum(float(item["period_days"]) for item in batches)',
    '    monthly_capacity = daily_capacity * total_period_days',
    '    return sum(item["process_amount"] for item in batches) / (daily_capacity * 17)',
  ].join('\n');
  const unreachablePeriodSource = `${actualPeriodSource}\n\ndef main():\n    return 0`;
  assert.equal(hasReachableActualPeriodAggregation(fixedPeriodSource), false);
  assert.equal(hasReachableActualPeriodAggregation(actualPeriodSource), true);
  assert.equal(hasReachableActualPeriodAggregation(disconnectedPeriodSource), false);
  assert.equal(hasReachableActualPeriodAggregation(unreachablePeriodSource), false);

  const rawFlowSource = [
    'def process_batch(batch):',
    '    return {"downstream_amount": batch["process_amount"]}',
    '',
    'def aggregate_month(batches, results):',
    '    return sum(item["process_amount"] for item in batches)',
  ].join('\n');
  const derivedFlowSource = [
    'def process_batch(batch):',
    '    downstream_amount = batch["process_amount"] * batch["split_ratio"]',
    '    return {"downstream_amount": downstream_amount}',
    '',
    'def aggregate_month(batches, results):',
    '    downstream_total = sum(item["downstream_amount"] for item in results)',
    '    return downstream_total',
  ].join('\n');
  const disconnectedDerivedFlowSource = [
    'def process_batch(batch):',
    '    downstream_amount = batch["process_amount"] * batch["split_ratio"]',
    '    return {"downstream_amount": batch["process_amount"]}',
    '',
    'def aggregate_month(batches, results):',
    '    return sum(item["downstream_amount"] for item in results)',
  ].join('\n');
  assert.equal(hasReachableDerivedFlowAggregation(rawFlowSource), false);
  assert.equal(hasReachableDerivedFlowAggregation(derivedFlowSource), true);
  assert.equal(hasReachableDerivedFlowAggregation(disconnectedDerivedFlowSource), false);

  const statefulTankSource = [
    'def check_tanks(tanks, batches, connections):',
    '    inventory = float(tanks["initial_capacity"])',
    '    history = []',
    '    for batch in batches:',
    '        inflow = sum(flow["amount"] for flow in connections if flow["batch"] == batch["id"] and flow["direction"] == "in")',
    '        outflow = sum(flow["amount"] for flow in connections if flow["batch"] == batch["id"] and flow["direction"] == "out")',
    '        inventory = inventory + inflow - outflow',
    '        history.append(inventory)',
    '    return history',
  ].join('\n');
  assert.equal(hasReachableDynamicTankInventory(statefulTankSource), true);

  const resetEachBatchSource = [
    'def check_tanks(tanks, batches, connections):',
    '    inventory = float(tanks["initial_capacity"])',
    '    for batch in batches:',
    '        inventory = float(tanks["initial_capacity"])',
    '        inflow = sum(flow["amount"] for flow in connections if flow["direction"] == "in")',
    '        outflow = sum(flow["amount"] for flow in connections if flow["direction"] == "out")',
    '        inventory = inventory + inflow - outflow',
    '    return inventory',
  ].join('\n');
  assert.equal(hasReachableDynamicTankInventory(resetEachBatchSource), false);
});
