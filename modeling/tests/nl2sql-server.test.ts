import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  canonicalizeSqlTableIdentifiers,
  createNl2SqlServer,
  prepareNl2SqlSelect,
} from '../src/nl2sql/server.js';

test('NL2SQL 按真实 schema 纠正模型生成的表名大小写', () => {
  const tables = ['RefineryUnit', 'VGOHTUnit', 'Product'];
  assert.equal(
    canonicalizeSqlTableIdentifiers(
      'SELECT r.ProductCapacity_TonsPerDay FROM refineryunit r JOIN `vgOhtunit` v ON v.VGOUnitID = r.RefineryUnitID',
      tables,
    ),
    'SELECT r.ProductCapacity_TonsPerDay FROM `RefineryUnit` r JOIN `VGOHTUnit` v ON v.VGOUnitID = r.RefineryUnitID',
  );
  assert.equal(
    canonicalizeSqlTableIdentifiers(
      'SELECT * FROM mid_stack_device_plan.product',
      tables,
    ),
    'SELECT * FROM mid_stack_device_plan.`Product`',
  );
});

test('NL2SQL 连接探测允许 Scene 数据库尚无数据表', async () => {
  let schemaReadCount = 0;
  const loadEmptySchema = async () => {
    schemaReadCount += 1;
    throw new Error('scene（数据库）不存在或为空：mv_shadow_project');
  };
  const sql = await prepareNl2SqlSelect('SELECT 1 AS ok', loadEmptySchema);

  assert.equal(sql, 'SELECT 1 AS ok LIMIT 200');
  assert.equal(schemaReadCount, 0);
  await assert.rejects(
    prepareNl2SqlSelect('SELECT * FROM orders', loadEmptySchema),
    /scene（数据库）不存在或为空/,
  );
  assert.equal(schemaReadCount, 1);
});

test('NL2SQL provider 服务提供鉴权查询和 Consumer 兼容结果', async () => {
  const calls: { question?: string; scene?: string; sql?: string } = {};
  const server = createNl2SqlServer({
    token: 'provider-token',
    defaultScene: 'default_scene',
    async generateSql(question, scene) {
      calls.question = question; calls.scene = scene;
      return 'SELECT location_id FROM warehouse_location';
    },
    async runSelect(sql, scene) {
      calls.sql = sql; calls.scene = scene;
      return { rows: [{ yard_id: 'Y01' }], sql: `${sql} LIMIT 200` };
    },
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  const base = `http://127.0.0.1:${address.port}`;

  try {
    assert.equal((await fetch(`${base}/health`)).status, 200);
    assert.equal((await fetch(`${base}/api/v1/query`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: '查询仓储位置' }),
    })).status, 401);

    const response = await fetch(`${base}/api/v1/query`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer provider-token' },
      body: JSON.stringify({ question: '查询仓储位置', scene: 'mw_proj_demo' }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {
      success: true,
      data: [{ yard_id: 'Y01' }],
      row_count: 1,
      sql: 'SELECT location_id FROM warehouse_location LIMIT 200',
    });
    assert.deepEqual(calls, {
      question: '查询仓储位置',
      scene: 'mw_proj_demo',
      sql: 'SELECT location_id FROM warehouse_location',
    });
  } finally {
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
