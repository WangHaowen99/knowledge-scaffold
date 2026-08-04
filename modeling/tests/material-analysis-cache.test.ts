import assert from 'node:assert/strict';
import test from 'node:test';
import {
  batchCacheKey, briefFingerprint, clearMaterialAnalysisCache,
  readCachedBatchAnalysis, writeCachedBatchAnalysis,
} from '../src/workflow/material-analysis-cache.js';
import { getDb } from '../src/db/index.js';

const table = (name: string, extra = {}) => ({ name, columns: [], ...extra }) as any;
const doc = (filename: string, textContent = '') => ({ filename, textContent });

test('同一批材料内容不变时键稳定', () => {
  const batch = { tables: [table('t1')], documents: [doc('a.md', 'hello')] };
  assert.equal(batchCacheKey(batch, 'brief-1'), batchCacheKey(batch, 'brief-1'));
});

test('材料内容变了键就变——否则会拿旧结论套新材料', () => {
  const before = { tables: [table('t1')], documents: [doc('a.md', 'hello')] };
  const after = { tables: [table('t1')], documents: [doc('a.md', 'hello world')] };
  assert.notEqual(batchCacheKey(before, 'b'), batchCacheKey(after, 'b'));
});

test('文档修订指纹变化时旧批次分析不能复用', () => {
  const before = { tables: [], documents: [{ ...doc('a.md', 'hello'), revisionFingerprint: 'rev-1' }] };
  const after = { tables: [], documents: [{ ...doc('a.md', 'hello'), revisionFingerprint: 'rev-2' }] };
  assert.notEqual(batchCacheKey(before, 'b'), batchCacheKey(after, 'b'));
});

test('表结构变了键就变，光看表名不够', () => {
  const before = { tables: [table('t1', { columns: [{ name: 'id' }] })], documents: [] };
  const after = { tables: [table('t1', { columns: [{ name: 'id' }, { name: 'amount' }] })], documents: [] };
  assert.notEqual(batchCacheKey(before, 'b'), batchCacheKey(after, 'b'));
});

test('建模目标变了整体作废：同样的材料也不复用', () => {
  const batch = { tables: [], documents: [doc('a.md', 'x')] };
  assert.notEqual(batchCacheKey(batch, briefFingerprint({ goal: '甲' })),
    batchCacheKey(batch, briefFingerprint({ goal: '乙' })));
});

test('批次成员不同不能撞键', () => {
  const merged = { tables: [], documents: [doc('a.md', 'x'), doc('b.md', 'y')] };
  const split = { tables: [], documents: [doc('a.mdx', '')] };
  assert.notEqual(batchCacheKey(merged, 'b'), batchCacheKey(split, 'b'));
});

test('写入后能读回，清空后读不到', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const project = createProject({ owner: 'alice', name: '分析缓存' });
  const payload = { materialAssessments: [{ source: 'a.md' }], candidateArtifacts: [] };
  writeCachedBatchAnalysis(project.id, 'k1', payload);
  assert.deepEqual(readCachedBatchAnalysis(project.id, 'k1'), payload);
  assert.equal(readCachedBatchAnalysis(project.id, 'nope'), null);
  assert.ok(clearMaterialAnalysisCache(project.id) >= 1);
  assert.equal(readCachedBatchAnalysis(project.id, 'k1'), null);
});

test('同一个键重复写入是覆盖而不是报错', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const project = createProject({ owner: 'alice', name: '缓存覆盖' });
  writeCachedBatchAnalysis(project.id, 'k1', { candidateArtifacts: [{ name: '旧' }] });
  writeCachedBatchAnalysis(project.id, 'k1', { candidateArtifacts: [{ name: '新' }] });
  assert.deepEqual(readCachedBatchAnalysis(project.id, 'k1'), { candidateArtifacts: [{ name: '新' }] });
});

test('缓存内容损坏时当作未命中，不能拖垮整轮分析', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const project = createProject({ owner: 'alice', name: '缓存损坏' });
  getDb().prepare(`INSERT INTO material_analysis_batch_cache (project_id, cache_key, payload, created_at)
    VALUES (?, 'broken', '{not json', ?)`).run(project.id, new Date().toISOString());
  assert.equal(readCachedBatchAnalysis(project.id, 'broken'), null);
});
