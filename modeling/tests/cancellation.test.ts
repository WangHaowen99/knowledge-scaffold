import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'cancellation-'));

const {
  CANCELLABLE_OPERATIONS, isCancelled, requestCancel, throwIfCancelled, OperationCancelledError,
} = await import('../src/cancellation.js');
const { InFlightOperations } = await import('../src/in-flight-operations.js');
const {
  CANCELLABLE_BUSY_STATES, cancellableOperationOf,
} = await import('../frontend/src/cancellableOperation.js');

test('取消键必须与启动作业时用的键一致，否则置位置到别处去了', () => {
  assert.equal(CANCELLABLE_OPERATIONS.analyze.key('pj_1'), 'pj_1:analyze');
  assert.equal(CANCELLABLE_OPERATIONS.knowledgeGenerate.key('pj_1'), 'pj_1:knowledge-generate');
  assert.equal(CANCELLABLE_OPERATIONS.knowledgeRepair.key('pj_1'), 'pj_1:knowledge-repair');
  assert.equal(CANCELLABLE_OPERATIONS.scriptGeneration.key('pj_1'), 'pj_1:script-generation');
});

test('没有任务在跑时取消返回 false，界面据此如实提示而不是假装成功', () => {
  assert.equal(requestCancel('pj_idle', 'analyze'), false);
  assert.equal(isCancelled('pj_idle', 'analyze'), false);
});

test('任务在检查点看到取消请求后退出，且区分于失败', async () => {
  const inFlight = new InFlightOperations();
  const batches: number[] = [];
  let cancelAfterFirst: () => void = () => {};
  const firstBatchDone = new Promise<void>((resolve) => { cancelAfterFirst = resolve; });

  const running = inFlight.run('pj_run:analyze', 'analyze', async () => {
    for (let batch = 1; batch <= 5; batch += 1) {
      throwIfCancelled('pj_run', 'analyze');
      batches.push(batch);
      if (batch === 1) {
        cancelAfterFirst();
        await new Promise((resolve) => { setTimeout(resolve, 30); });
      }
    }
  });

  await firstBatchDone;
  assert.equal(requestCancel('pj_run', 'analyze'), true);

  await assert.rejects(running, (error: unknown) => error instanceof OperationCancelledError);
  // 已经跑完的批次要保留，取消不是回滚
  assert.deepEqual(batches, [1]);
  // 收尾要清掉登记，否则下一轮起不来
  assert.equal(inFlight.current('pj_run:analyze'), null);
});

test('取消文案说明是按用户要求停的，不能读成系统故障', () => {
  const error = new OperationCancelledError('资料分析');
  assert.match(error.message, /资料分析/);
  assert.match(error.message, /取消/);
  assert.doesNotMatch(error.message, /失败|错误|异常/);
});

test('取消请求只影响本项目本操作', async () => {
  const inFlight = new InFlightOperations();
  const held = new Promise<void>((resolve) => { setTimeout(resolve, 40); });
  const a = inFlight.run('pj_a:analyze', 'analyze', () => held);
  const b = inFlight.run('pj_b:analyze', 'analyze', () => held);
  assert.equal(requestCancel('pj_a', 'analyze'), true);
  assert.equal(isCancelled('pj_a', 'analyze'), true);
  assert.equal(isCancelled('pj_b', 'analyze'), false, '不能波及别的项目');
  await Promise.all([a, b]);
});

test('前端只给真正可取消的忙碌态显示按钮', () => {
  assert.equal(cancellableOperationOf({ busy: 'analyze' }), 'analyze');
  assert.equal(cancellableOperationOf({ busy: 'generate_knowledge' }), 'knowledgeGenerate');
  // 这些是瞬时操作，给取消按钮只会造出点了没用的东西
  assert.equal(cancellableOperationOf({ busy: 'confirm' }), null);
  assert.equal(cancellableOperationOf({ busy: 'upload' }), null);
  assert.equal(cancellableOperationOf({}), null);
});

test('刷新页面后靠服务端 run 的 intent 认出可取消操作', () => {
  assert.equal(
    cancellableOperationOf({ busy: 'server_run', serverRun: { intent: '生成知识包' } }),
    'knowledgeGenerate',
  );
  assert.equal(
    cancellableOperationOf({ busy: 'server_run', serverRun: { intent: '确认建模目标' } }),
    null,
  );
});

test('前端映射的目标都在后端可取消清单里', () => {
  for (const operation of Object.values(CANCELLABLE_BUSY_STATES)) {
    assert.ok(operation in CANCELLABLE_OPERATIONS, `${operation} 未在后端登记，取消会 400`);
  }
});
