import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  InFlightOperations,
  OperationAlreadyRunningError,
} from '../src/in-flight-operations.js';

test('in-flight operation：运行期间暴露操作类型、拒绝重入并在结束后清理', async () => {
  const operations = new InFlightOperations();
  let release!: () => void;
  const running = operations.run('project-1:optimize', 'propose', () => (
    new Promise<void>((resolve) => { release = resolve; })
  ));

  assert.equal(operations.current('project-1:optimize'), 'propose');
  await assert.rejects(
    operations.run('project-1:optimize', 'apply', async () => {}),
    OperationAlreadyRunningError,
  );

  release();
  await running;
  assert.equal(operations.current('project-1:optimize'), null);

  await assert.rejects(
    operations.run('project-1:optimize', 'apply', async () => { throw new Error('boom'); }),
    /boom/,
  );
  assert.equal(operations.current('project-1:optimize'), null);
});

test('optimize panel：只恢复服务端支持的运行操作', async () => {
  const { normalizeOptimizeOperation, shouldPollOptimizeOperation } =
    await import('../frontend/src/optimize-running-state.js');
  assert.equal(typeof shouldPollOptimizeOperation, 'function');
  assert.equal(normalizeOptimizeOperation('propose'), 'propose');
  assert.equal(normalizeOptimizeOperation('apply'), 'apply');
  assert.equal(normalizeOptimizeOperation(null), '');
  assert.equal(normalizeOptimizeOperation('unknown'), '');
  assert.equal(shouldPollOptimizeOperation('propose'), true);
  assert.equal(shouldPollOptimizeOperation('apply'), true);
  assert.equal(shouldPollOptimizeOperation(undefined), true);
  assert.equal(shouldPollOptimizeOperation(''), false);
  assert.equal(shouldPollOptimizeOperation('unknown'), false);
});
