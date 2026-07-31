import assert from 'node:assert/strict';
import test from 'node:test';

import type { PackageSpec } from '../src/types.js';
import { AgentRunTimeoutError } from '../src/workflow/agent.js';
import {
  applyAtomicResourcePatch,
  buildAtomicPatchParameters,
  buildAtomicChangeContext,
  generateAtomicResourcePatchInBatches,
  normalizeChangePlan,
  nextPackageChatVersion,
  partitionAtomicChangePlan,
  PackageCandidateRejectedError,
  PackageCandidateValidationError,
  resolveChangePlanTargets,
  resolvePackageApplyTimeoutMs,
  runCandidateQualityLoop,
  validatePackageApplyCandidate,
  type AtomicResourcePatch,
  type ChangePlan,
} from '../src/workflow/package-chat.js';

test('同一资源的多项同类修改会在确认和执行前合并', () => {
  const normalized = normalizeChangePlan({
    summary: '修复两个脚本的三个问题',
    changes: [
      {
        targetKind: 'script', targetKey: 'LoadCheck', action: 'modify',
        description: '修正 normalize_yield_percent 的阈值边界',
      },
      {
        targetKind: 'script', targetKey: 'TankCheck', action: 'modify',
        description: '同步修正 normalize_yield_percent 的阈值边界',
      },
      {
        targetKind: 'script', targetKey: 'LoadCheck', action: 'modify',
        description: '澄清负荷校验脚本的返回变量名',
      },
      {
        targetKind: 'script', targetKey: 'LoadCheck', action: 'modify',
        description: '统一减一线查询逻辑',
      },
    ],
    risks: ['需要重新评测'],
  });

  assert.equal(normalized.changes.length, 2);
  assert.deepEqual(normalized.changes.map((change) => `${change.targetKind}:${change.targetKey}`), [
    'script:LoadCheck',
    'script:TankCheck',
  ]);
  assert.equal(
    normalized.changes[0].description,
    '修正 normalize_yield_percent 的阈值边界；澄清负荷校验脚本的返回变量名；统一减一线查询逻辑',
  );
  assert.deepEqual(normalized.risks, ['需要重新评测']);
});

test('同一资源存在冲突动作时拒绝静默合并', () => {
  assert.throws(() => normalizeChangePlan({
    summary: '冲突计划',
    changes: [
      {
        targetKind: 'script', targetKey: 'LoadCheck', action: 'modify',
        description: '修正阈值',
      },
      {
        targetKind: 'script', targetKey: 'LoadCheck', action: 'remove',
        description: '删除脚本',
      },
    ],
  }), /同时安排了“修改”和“删除”/u);
});

test('原子补丁工具只暴露当前批次资源且要求完整提交', () => {
  const plan: ChangePlan = {
    summary: '新增脚本并绑定操作',
    changes: [
      {
        targetKind: 'script', targetKey: 'CalculatePlan', action: 'add',
        description: '新增确定性计算脚本',
      },
      {
        targetKind: 'operation', targetKey: 'PlanOperation', action: 'modify',
        description: '绑定确定性脚本',
      },
    ],
  };

  const scriptSchema = buildAtomicPatchParameters({ ...plan, changes: [plan.changes[0]] }) as any;
  assert.deepEqual(scriptSchema.required, ['scripts']);
  assert.deepEqual(Object.keys(scriptSchema.properties), ['scripts']);
  assert.equal(scriptSchema.properties.scripts.minItems, 1);
  assert.equal(scriptSchema.properties.scripts.maxItems, 1);

  const operationSchema = buildAtomicPatchParameters({ ...plan, changes: [plan.changes[1]] }) as any;
  assert.deepEqual(operationSchema.required, ['operations']);
  assert.deepEqual(Object.keys(operationSchema.properties), ['operations']);
});

test('知识包应用超时默认十分钟且支持毫秒环境变量覆盖', () => {
  assert.equal(resolvePackageApplyTimeoutMs(undefined), 600_000);
  assert.equal(resolvePackageApplyTimeoutMs('900000'), 900_000);
});

test('知识包应用超时限制在三十秒到三十分钟且非法值回退默认值', () => {
  assert.equal(resolvePackageApplyTimeoutMs('1'), 30_000);
  assert.equal(resolvePackageApplyTimeoutMs('99999999'), 1_800_000);
  assert.equal(resolvePackageApplyTimeoutMs('not-a-number'), 600_000);
  assert.equal(resolvePackageApplyTimeoutMs(''), 600_000);
});

test('知识包修缮版本号遵循统一生命周期而不依赖历史版本行数', () => {
  assert.equal(nextPackageChatVersion({
    version: '1.4.2', status: 'released',
  }), '1.4.3-rc.1');
  assert.equal(nextPackageChatVersion({
    version: '1.4.3-rc.8', status: 'review',
  }), '1.4.3-rc.9');
  assert.equal(nextPackageChatVersion({
    version: '0.1.0', status: 'draft',
  }), '1.0.0-rc.1');
});

test('互不依赖的多个脚本各自单批生成并在最后生成对应操作', async () => {
  const plan: ChangePlan = {
    summary: '为三个操作增加确定性脚本',
    changes: ['Plan', 'Balance', 'Monitor'].flatMap((operationKey) => {
      const scriptKey = `${operationKey}Script`;
      return [
        {
          targetKind: 'script' as const,
          targetKey: scriptKey,
          action: 'add' as const,
          description: `为 operation:${operationKey} 生成脚本`,
        },
        {
          targetKind: 'operation' as const,
          targetKey: operationKey,
          action: 'modify' as const,
          description: `绑定 script:${scriptKey}`,
        },
      ];
    }),
  };
  const calls: string[][] = [];

  const patch = await generateAtomicResourcePatchInBatches(plan, async (batch) => {
    calls.push(batch.changes.map((change) => `${change.targetKind}:${change.targetKey}`));
    assert.equal(batch.changes.length, 1, '每个脚本和操作都应使用独立的小批次');
    const change = batch.changes[0];
    if (change.targetKind === 'script') {
      const operationKey = change.targetKey.replace(/Script$/, '');
      return {
        scripts: [{
          key: change.targetKey,
          title: change.targetKey,
          description: '确定性脚本',
          boundOperation: operationKey,
          entrypoint: 'main',
          parameters: [], outputs: [], boundaries: [],
          sourceCode: 'print("{}")',
        }],
      };
    }
    const scriptKey = `${change.targetKey}Script`;
    return {
      operations: [{
        key: change.targetKey,
        title: change.targetKey,
        description: '确定性操作',
        inputs: [], outputs: [], flow: [`调用 script:${scriptKey}`], relatedObjects: [],
        script: { key: scriptKey },
      }],
    };
  });

  assert.deepEqual(calls, [
    ['script:PlanScript'],
    ['script:BalanceScript'],
    ['script:MonitorScript'],
    ['operation:Plan'],
    ['operation:Balance'],
    ['operation:Monitor'],
  ]);
  assert.deepEqual(patch.operations?.map((item) => item.key), ['Plan', 'Balance', 'Monitor']);
  assert.deepEqual(patch.scripts?.map((item) => item.key), ['PlanScript', 'BalanceScript', 'MonitorScript']);
});

test('多个脚本共享同一 hub operation 时逐个生成脚本并最后生成 operation', async () => {
  const scriptKeys = ['CalculateYield', 'CalculateCost', 'CalculateBalance'];
  const plan: ChangePlan = {
    summary: '为综合测算操作增加三个确定性脚本',
    changes: [
      {
        targetKind: 'operation',
        targetKey: 'IntegratedCalculation',
        action: 'modify',
        description: `依次调用 ${scriptKeys.map((key) => `script:${key}`).join('、')}`,
      },
      ...scriptKeys.map((scriptKey) => ({
        targetKind: 'script' as const,
        targetKey: scriptKey,
        action: 'add' as const,
        description: `为 operation:IntegratedCalculation 生成 ${scriptKey} 脚本`,
      })),
    ],
  };
  const calls: string[][] = [];

  const patch = await generateAtomicResourcePatchInBatches(plan, async (batch) => {
    calls.push(batch.changes.map((change) => `${change.targetKind}:${change.targetKey}`));
    const script = batch.changes.find((change) => change.targetKind === 'script');
    if (script) {
      return {
        scripts: [{
          key: script.targetKey,
          title: script.targetKey,
          description: '独立确定性脚本',
          boundOperation: 'IntegratedCalculation',
          entrypoint: 'main',
          parameters: [],
          outputs: [],
          boundaries: [],
          sourceCode: 'print("{}")',
        }],
      };
    }
    return {
      operations: [{
        key: 'IntegratedCalculation',
        title: '综合测算',
        description: '按顺序调用三个确定性脚本',
        inputs: [],
        outputs: [],
        flow: scriptKeys.map((key) => `调用 script:${key}`),
        relatedObjects: [],
      }],
    };
  });

  assert.deepEqual(calls, [
    ['script:CalculateYield'],
    ['script:CalculateCost'],
    ['script:CalculateBalance'],
    ['operation:IntegratedCalculation'],
  ]);
  assert.deepEqual(patch.scripts?.map((item) => item.key), scriptKeys);
  assert.deepEqual(patch.operations?.map((item) => item.key), ['IntegratedCalculation']);
});

test('多个脚本和唯一汇总操作按资源结构分批且不依赖描述包含 key', async () => {
  const plan: ChangePlan = {
    summary: '补充多项确定性测算并统一汇总',
    changes: [
      {
        targetKind: 'operation',
        targetKey: 'IntegratedCalculation',
        action: 'modify',
        description: '在业务流程中统一组织各项测算结果',
      },
      {
        targetKind: 'script',
        targetKey: 'CalculateYield',
        action: 'add',
        description: '新增收率测算能力',
      },
      {
        targetKind: 'script',
        targetKey: 'CalculateCost',
        action: 'modify',
        description: '修订成本测算能力',
      },
    ],
  };
  const calls: string[][] = [];

  await generateAtomicResourcePatchInBatches(plan, async (batch) => {
    calls.push(batch.changes.map((change) => `${change.targetKind}:${change.targetKey}`));
    return {
      ...(batch.changes[0].targetKind === 'script' ? {
        scripts: [{
          key: batch.changes[0].targetKey,
          title: '测算脚本',
          description: '确定性测算',
          boundOperation: 'IntegratedCalculation',
          entrypoint: 'main',
          parameters: [],
          outputs: [],
          boundaries: [],
          sourceCode: 'print("{}")',
        }],
      } : {
        operations: [{
          key: 'IntegratedCalculation',
          title: '综合测算',
          description: '汇总确定性测算结果',
          inputs: [],
          outputs: [],
          flow: ['执行测算并汇总'],
          relatedObjects: [],
        }],
      }),
    };
  });

  assert.deepEqual(calls, [
    ['script:CalculateYield'],
    ['script:CalculateCost'],
    ['operation:IntegratedCalculation'],
  ]);
});

test('多个脚本分别对应多个操作时仍逐个单批并将操作排在后面', async () => {
  const plan: ChangePlan = {
    summary: '补充两个独立计算能力',
    changes: [
      {
        targetKind: 'operation', targetKey: 'YieldCalculation', action: 'modify',
        description: '在收率流程中使用确定性计算',
      },
      {
        targetKind: 'script', targetKey: 'YieldScript', action: 'add',
        description: '新增收率计算脚本',
      },
      {
        targetKind: 'operation', targetKey: 'CostCalculation', action: 'modify',
        description: '在成本流程中使用确定性计算',
      },
      {
        targetKind: 'script', targetKey: 'CostScript', action: 'modify',
        description: '修订成本计算脚本',
      },
    ],
  };

  assert.deepEqual(
    partitionAtomicChangePlan(plan).map((batch) => (
      batch.changes.map((change) => `${change.targetKind}:${change.targetKey}`)
    )),
    [
      ['script:YieldScript'],
      ['script:CostScript'],
      ['operation:YieldCalculation'],
      ['operation:CostCalculation'],
    ],
  );
});

test('存在内部引用的资源始终留在同一生成批次', async () => {
  const plan: ChangePlan = {
    summary: '同步修改操作与指标口径',
    changes: [
      {
        targetKind: 'operation', targetKey: 'CalculateBenefit', action: 'modify',
        description: '改为调用 metric:WeightedBenefit',
      },
      {
        targetKind: 'metric', targetKey: 'WeightedBenefit', action: 'modify',
        description: '为 operation:CalculateBenefit 提供统一计算口径',
      },
      {
        targetKind: 'rule', targetKey: 'IndependentFallback', action: 'modify',
        description: '补充独立兜底说明',
      },
    ],
  };
  const batches: string[][] = [];

  await generateAtomicResourcePatchInBatches(plan, async (batch) => {
    batches.push(batch.changes.map((change) => `${change.targetKind}:${change.targetKey}`));
    return {
      ...(batch.changes.some((change) => change.targetKind === 'operation') ? {
        operations: [{
          key: 'CalculateBenefit', title: '计算效益', description: '统一计算',
          inputs: [], outputs: [], flow: ['调用 metric:WeightedBenefit'], relatedObjects: [],
        }],
        metrics: [{
          key: 'WeightedBenefit', title: '加权效益', description: '统一口径', caliber: '按确认权重计算',
        }],
      } : {}),
      ...(batch.changes.some((change) => change.targetKind === 'rule') ? {
        rules: [{ key: 'IndependentFallback', title: '独立兜底', description: '查询为空时明确返回' }],
      } : {}),
    };
  });

  assert.deepEqual(batches, [
    ['operation:CalculateBenefit', 'metric:WeightedBenefit'],
    ['rule:IndependentFallback'],
  ]);
});

test('重试原子计划时复用已完成批次且只生成缺失批次', async () => {
  const plan: ChangePlan = {
    summary: '新增脚本并绑定操作',
    changes: [
      {
        targetKind: 'script', targetKey: 'CalculatePlan', action: 'add',
        description: '新增确定性计算脚本',
      },
      {
        targetKind: 'operation', targetKey: 'PlanOperation', action: 'modify',
        description: '绑定确定性脚本',
      },
    ],
  };
  const firstBatch = partitionAtomicChangePlan(plan)[0];
  const completed = {
    [firstBatch.changes.map((change) => `${change.targetKind}:${change.targetKey}`).join('|')]: {
      scripts: [{
        key: 'CalculatePlan', title: '计算计划', description: '确定性计算',
        boundOperation: 'PlanOperation', runtime: 'python' as const,
        entrypoint: 'calculate_plan.py', timeoutSeconds: 30,
        parameters: [], outputs: [], boundaries: [], sourceCode: 'print("{}")',
      }],
    },
  };
  const generated: string[] = [];

  const patch = await generateAtomicResourcePatchInBatches(plan, async (batch) => {
    generated.push(batch.changes.map((change) => `${change.targetKind}:${change.targetKey}`).join('|'));
    return {
      operations: [{
        key: 'PlanOperation', title: '计划操作', description: '调用脚本',
        inputs: [], outputs: [], flow: ['调用 script:CalculatePlan'], relatedObjects: [],
        script: { key: 'CalculatePlan' },
      }],
    };
  }, { completed });

  assert.deepEqual(generated, ['operation:PlanOperation']);
  assert.equal(patch.scripts?.[0].key, 'CalculatePlan');
  assert.equal(patch.operations?.[0].key, 'PlanOperation');
});

function spec(): PackageSpec {
  return {
    id: 'pkg',
    name: '通用知识包',
    description: '测试原子修订',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'default',
    objects: [
      {
        key: 'Order',
        title: '订单',
        description: '订单对象',
        fields: [{ key: 'id', cnName: '订单号' }],
        sourceRefs: [{ kind: 'table', table: 'orders', columns: ['id'] }],
      },
      {
        key: 'Customer',
        title: '客户',
        description: '客户对象必须原样保留',
        fields: [{ key: 'id', cnName: '客户号' }],
        sourceRefs: [{ kind: 'table', table: 'customers', columns: ['id'] }],
      },
    ],
    operations: [{
      key: 'FindOrder',
      title: '查询订单',
      description: '查询指定订单',
      inputs: [{ param: 'id', type: 'string', required: true }],
      outputs: [{ field: 'order', type: 'Order' }],
      flow: ['按订单号查询'],
      relatedObjects: ['Order'],
      evidence: ['diagram:order'],
    }],
    rules: [],
    metrics: [],
    scripts: [],
  };
}

test('候选删除仍被引用的对象时将完整性问题归为专家可调整的校验失败', () => {
  const current = spec();
  const dangling: PackageSpec = {
    ...current,
    objects: current.objects
      .filter((object) => object.key !== 'Order')
      .map((object) => ({ ...object, sourceRefs: undefined })),
  };

  assert.throws(
    () => validatePackageApplyCandidate(dangling, []),
    (error: unknown) => error instanceof PackageCandidateValidationError
      && /操作 FindOrder 引用了不存在的对象 Order/u.test(error.message),
  );
});

test('修改计划可用资源标题解析到稳定 key', () => {
  const resolved = resolveChangePlanTargets(spec(), {
    summary: '补充查询订单流程',
    changes: [{
      targetKind: 'operation', targetKey: '查询订单', action: 'modify', description: '补充异常分支',
    }],
  });

  assert.equal(resolved.changes[0].targetKey, 'FindOrder');
});

test('修改计划支持带资源类型前缀的标题并在归一后合并重复项', () => {
  const resolved = resolveChangePlanTargets(spec(), {
    summary: '补充订单对象',
    changes: [
      { targetKind: 'object', targetKey: 'object:订单', action: 'modify', description: '补充状态' },
      { targetKind: 'object', targetKey: 'Order', action: 'modify', description: '补充来源' },
    ],
  });

  assert.deepEqual(resolved.changes, [{
    targetKind: 'object', targetKey: 'Order', action: 'modify', description: '补充状态；补充来源',
  }]);
});

test('修改计划拒绝无法唯一解析或不存在的资源标题', () => {
  const current = spec();
  current.operations.push({ ...current.operations[0], key: 'FindOrderCopy' });
  assert.throws(() => resolveChangePlanTargets(current, {
    summary: '标题歧义',
    changes: [{ targetKind: 'operation', targetKey: '查询订单', action: 'modify', description: '补充' }],
  }), /修改目标标题不唯一.*查询订单/u);
  assert.throws(() => resolveChangePlanTargets(spec(), {
    summary: '目标不存在',
    changes: [{ targetKind: 'operation', targetKey: '不存在的操作', action: 'modify', description: '补充' }],
  }), /修改目标不存在.*不存在的操作/u);
});

test('建模方案在创建交互前拒绝超出展示上限的变更说明', () => {
  assert.throws(() => normalizeChangePlan({
    summary: '说明过长',
    changes: [{
      targetKind: 'operation', targetKey: 'FindOrder', action: 'modify',
      description: '修改'.repeat(3_001),
    }],
  }), /变更说明不能为空且不能超过 6000 字/u);
});

test('原子修订只替换计划内资源并原样保留其余知识', () => {
  const current = spec();
  const plan: ChangePlan = {
    summary: '补充订单说明',
    changes: [{
      targetKind: 'object',
      targetKey: 'Order',
      action: 'modify',
      description: '补充订单状态语义',
    }],
  };
  const patch: AtomicResourcePatch = {
    objects: [{
      ...current.objects[0],
      description: '订单对象，包含状态语义',
    }],
  };

  const revised = applyAtomicResourcePatch(current, plan, patch);

  assert.equal(revised.objects[0].description, '订单对象，包含状态语义');
  assert.deepEqual(revised.objects[1], current.objects[1]);
  assert.deepEqual(revised.operations, current.operations);
  assert.deepEqual(revised.rules, current.rules);
  assert.deepEqual(revised.metrics, current.metrics);
  assert.deepEqual(revised.scripts, current.scripts);
});

test('原子修订拒绝模型提交计划外资源', () => {
  const current = spec();
  const plan: ChangePlan = {
    summary: '补充订单说明',
    changes: [{
      targetKind: 'object', targetKey: 'Order', action: 'modify', description: '补充状态语义',
    }],
  };

  assert.throws(() => applyAtomicResourcePatch(current, plan, {
    objects: [
      { ...current.objects[0], description: '订单对象，包含状态语义' },
      { ...current.objects[1], description: '模型擅自修改了客户' },
    ],
  }), /计划外.*Customer/);
});

test('原子修订拒绝遗漏计划内资源', () => {
  const current = spec();
  const plan: ChangePlan = {
    summary: '补充订单说明',
    changes: [{
      targetKind: 'object', targetKey: 'Order', action: 'modify', description: '补充状态语义',
    }],
  };

  assert.throws(
    () => applyAtomicResourcePatch(current, plan, { objects: [] }),
    /未提交计划内.*Order/,
  );
});

test('原子修订在计划目标已过期时拒绝写入', () => {
  const current = spec();
  const plan: ChangePlan = {
    summary: '修改已不存在的对象',
    changes: [{
      targetKind: 'object', targetKey: 'Missing', action: 'modify', description: '不应执行',
    }],
  };

  assert.throws(() => applyAtomicResourcePatch(current, plan, {
    objects: [{
      key: 'Missing', title: '不存在', description: '不应新增', fields: [],
    }],
  }), /修改目标不存在.*Missing/);
});

test('原子修订只把目标全文和精简引用索引发送给模型', () => {
  const current = spec();
  const plan: ChangePlan = {
    summary: '补充订单说明',
    changes: [{
      targetKind: 'object', targetKey: 'Order', action: 'modify', description: '补充状态语义',
    }],
  };

  const context = JSON.stringify(buildAtomicChangeContext(current, plan));

  assert.match(context, /订单对象/);
  assert.match(context, /FindOrder/);
  assert.doesNotMatch(context, /客户对象必须原样保留/);
});

test('原子修订拒绝没有实际变化的 modify 补丁', () => {
  const current = spec();
  const plan: ChangePlan = {
    summary: '补充订单说明',
    changes: [{
      targetKind: 'object', targetKey: 'Order', action: 'modify', description: '补充状态语义',
    }],
  };

  assert.throws(
    () => applyAtomicResourcePatch(current, plan, { objects: [current.objects[0]] }),
    /Order.*未产生实际变化/,
  );
});

test('原子修订校验 add 和 remove 的当前资源状态', () => {
  const current = spec();
  assert.throws(() => applyAtomicResourcePatch(current, {
    summary: '错误新增',
    changes: [{ targetKind: 'object', targetKey: 'Order', action: 'add', description: '重复' }],
  }, { objects: [current.objects[0]] }), /新增目标已经存在.*Order/);

  assert.throws(() => applyAtomicResourcePatch(current, {
    summary: '错误删除',
    changes: [{ targetKind: 'rule', targetKey: 'MissingRule', action: 'remove', description: '不存在' }],
  }, {}), /删除目标不存在.*MissingRule/);
});

test('原子修订保留未涉及的可选目录缺省状态', () => {
  const current = spec();
  delete current.rules;
  delete current.metrics;
  delete current.scripts;
  const revised = applyAtomicResourcePatch(current, {
    summary: '补充订单说明',
    changes: [{ targetKind: 'object', targetKey: 'Order', action: 'modify', description: '补充' }],
  }, { objects: [{ ...current.objects[0], description: '已补充' }] });

  assert.equal(revised.rules, undefined);
  assert.equal(revised.metrics, undefined);
  assert.equal(revised.scripts, undefined);
});

test('原子修订拒绝同一资源的重复补丁', () => {
  const current = spec();
  assert.throws(() => applyAtomicResourcePatch(current, {
    summary: '补充订单说明',
    changes: [{ targetKind: 'object', targetKey: 'Order', action: 'modify', description: '补充' }],
  }, {
    objects: [
      { ...current.objects[0], description: '第一次补充' },
      { ...current.objects[0], description: '第二次补充' },
    ],
  }), /重复提交.*Order/);
});

test('单一 modify 目标把唯一同类型补丁归一到既有资源 key', () => {
  const current = spec();
  current.metrics = [{
    key: 'LegacyMetric',
    title: '原指标',
    description: '原指标只覆盖旧口径',
    caliber: '按旧对象计算',
    boundaries: ['仅覆盖旧对象'],
  }];
  const revised = applyAtomicResourcePatch(current, {
    summary: '扩展既有指标的适用口径',
    changes: [{
      targetKind: 'metric',
      targetKey: 'LegacyMetric',
      action: 'modify',
      description: '保留资源身份，只扩展标题、说明和计算口径',
    }],
  }, {
    metrics: [{
      key: 'GeneralMetric',
      title: '通用指标',
      description: '覆盖新旧两类对象',
      caliber: '根据对象类型选择对应字段后计算',
      boundaries: ['仅使用已确认对象字段'],
    }],
  });

  assert.deepEqual(revised.metrics?.map((metric) => metric.key), ['LegacyMetric']);
  assert.equal(revised.metrics?.[0].title, '通用指标');
  assert.equal(revised.metrics?.[0].caliber, '根据对象类型选择对应字段后计算');
});

test('单一 modify 不会把另一个既有资源误认成目标补丁', () => {
  const current = spec();
  assert.throws(() => applyAtomicResourcePatch(current, {
    summary: '补充订单说明',
    changes: [{
      targetKind: 'object', targetKey: 'Order', action: 'modify', description: '补充状态语义',
    }],
  }, {
    objects: [{ ...current.objects[1], description: '误提交了另一个既有对象' }],
  }), /计划外.*Customer/);
});

test('候选修订未改善时把校验反馈交给下一次生成且不返回退化版本', async () => {
  const feedbacks: string[] = [];
  const accepted = await runCandidateQualityLoop({
    maxAttempts: 3,
    generate: async (_attempt, feedback) => {
      feedbacks.push(feedback);
      return feedbacks.length === 1 ? '退化候选' : '改善候选';
    },
    validate: async (candidate) => candidate === '改善候选'
      ? { accepted: true, feedback: '阻断错误减少' }
      : { accepted: false, feedback: '新增了未经确认的业务阈值' },
  });

  assert.equal(accepted.status, 'accepted');
  assert.equal(accepted.candidate, '改善候选');
  assert.deepEqual(feedbacks, ['', '新增了未经确认的业务阈值']);
});

test('候选修订循环持续报告生成、校验和重试阶段', async () => {
  const progress: Array<{ phase: string; attempt: number; eventKey: string }> = [];
  const accepted = await runCandidateQualityLoop({
    maxAttempts: 2,
    onProgress: async (event) => {
      progress.push({ phase: event.phase, attempt: event.attempt, eventKey: event.eventKey });
    },
    generate: async (attempt) => `候选-${attempt}`,
    validate: async (candidate) => candidate === '候选-2'
      ? { accepted: true, feedback: '已改善' }
      : { accepted: false, feedback: '仍有语义冲突' },
  });

  assert.equal(accepted.status, 'accepted');
  assert.deepEqual(progress, [
    { phase: 'generating', attempt: 1, eventKey: 'attempt-1-generating' },
    { phase: 'validating', attempt: 1, eventKey: 'attempt-1-validating' },
    { phase: 'retrying', attempt: 1, eventKey: 'attempt-1-retrying' },
    { phase: 'generating', attempt: 2, eventKey: 'attempt-2-generating' },
    { phase: 'validating', attempt: 2, eventKey: 'attempt-2-validating' },
  ]);
});

test('候选补丁越过计划边界时把失败原因交给下一次生成', async () => {
  const feedbacks: string[] = [];
  let calls = 0;
  const accepted = await runCandidateQualityLoop({
    maxAttempts: 3,
    generate: async (_attempt, feedback) => {
      calls += 1;
      feedbacks.push(feedback);
      if (calls === 1) throw new Error('模型提交了计划外的 metric:WeightedRevenue，已拒绝应用');
      return '只修改计划内资源的候选';
    },
    validate: async () => ({ accepted: true, feedback: '阻断错误减少' }),
  });

  assert.equal(accepted.status, 'accepted');
  assert.equal(accepted.candidate, '只修改计划内资源的候选');
  assert.equal(calls, 2);
  assert.match(feedbacks[1], /计划外.*WeightedRevenue/);
});

test('候选连续生成失败时保留可反馈给专家的校验原因', async () => {
  await assert.rejects(
    () => runCandidateQualityLoop({
      maxAttempts: 2,
      generate: async () => {
        throw new PackageCandidateValidationError(
          '对象「跨境业务事件」主键「cust_id」不属于对象字段',
        );
      },
      validate: async () => ({ accepted: true, feedback: '' }),
    }),
    (error: unknown) => error instanceof PackageCandidateRejectedError
      && error.code === 'PACKAGE_CANDIDATE_REJECTED'
      && /cust_id.*不属于对象字段/u.test(error.feedback),
  );
});

test('模型服务异常不会被误报为专家方案校验失败', async () => {
  await assert.rejects(
    () => runCandidateQualityLoop({
      maxAttempts: 1,
      generate: async () => {
        throw new Error('模型服务不可用');
      },
      validate: async () => ({ accepted: true, feedback: '' }),
    }),
    (error: unknown) => error instanceof Error
      && !(error instanceof PackageCandidateRejectedError)
      && /模型服务不可用/u.test(error.message),
  );
});

test('候选连续未通过质量门禁时保留最后一次校验反馈', async () => {
  await assert.rejects(
    () => runCandidateQualityLoop({
      maxAttempts: 2,
      generate: async (attempt) => `候选-${attempt}`,
      validate: async () => ({ accepted: false, feedback: '主键必须引用 fields[].key' }),
    }),
    (error: unknown) => error instanceof PackageCandidateRejectedError
      && error.feedback === '主键必须引用 fields[].key',
  );
});

test('语义质量门禁多次仍有争议时保留最佳候选供专家复核', async () => {
  const result = await runCandidateQualityLoop({
    maxAttempts: 3,
    generate: async (attempt) => `候选-${attempt}`,
    validate: async (candidate) => ({
      accepted: false,
      disposition: 'expert_review' as const,
      feedback: `${candidate} 仍有语义争议`,
      reviewScore: candidate === '候选-2' ? 1 : 2,
    }),
  });

  assert.deepEqual(result, {
    status: 'expert_review',
    candidate: '候选-2',
    decision: {
      accepted: false,
      disposition: 'expert_review',
      feedback: '候选-2 仍有语义争议',
      reviewScore: 1,
    },
    attempts: 3,
  });
});

test('知识包应用达到绝对超时后立即失败且不自动重试', async () => {
  let calls = 0;

  await assert.rejects(
    () => runCandidateQualityLoop({
      maxAttempts: 3,
      generate: async () => {
        calls += 1;
        throw new AgentRunTimeoutError(600_000);
      },
      validate: async () => ({ accepted: true, feedback: '' }),
    }),
    (error: unknown) => error instanceof AgentRunTimeoutError && error.timeoutMs === 600_000,
  );
  assert.equal(calls, 1);
});
