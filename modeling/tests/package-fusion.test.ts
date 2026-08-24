import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  fuseKnowledgePackages,
  packageFusionDigest,
  type PackageFusionInput,
} from '../src/workflow/package-fusion.js';
import { renderDiagramFiles } from '../src/flowchart/index.js';
import { zipPackage } from '../src/workflow/export.js';
import { parsePackageArchive } from '../src/workflow/package-import.js';
import { renderPackage } from '../src/workflow/package-render.js';
import type { PackageSpec } from '../src/types.js';

interface RenderedMappingDocument {
  objects: Record<string, { provider_alias: string; resource: string }>;
}

interface RenderedFlowDocument {
  diagrams: Array<{ id: string; nodes: Array<{ id: string }> }>;
}

function source(sourceId: string, objectKey: string): PackageFusionInput {
  const operationKey = 'inspect';
  const spec: PackageSpec = {
    id: `source-${sourceId}`,
    name: `来源包 ${sourceId}`,
    description: `${sourceId} 场景知识包`,
    version: '1.0.0',
    language: 'zh-CN',
    status: 'released',
    defaultProviderAlias: 'pkg',
    objects: [{
      key: objectKey,
      title: '共享对象',
      description: `${sourceId} 的共享对象定义`,
      aliases: ['共享项'],
      sourceRefs: [{ kind: 'table', table: `table_${sourceId}` }],
      fields: [{ key: 'id', cnName: '标识', type: 'string', required: true }],
      primaryKey: ['id'],
      relations: [{ relation: 'self', target: objectKey, note: '自引用' }],
    }],
    operations: [{
      key: operationKey,
      title: '检查共享对象',
      description: `${sourceId} 的检查流程`,
      inputs: [],
      outputs: [],
      flow: ['读取共享对象', '返回检查结果'],
      relatedObjects: [objectKey],
      usesRules: ['valid'],
      usesMetrics: ['count'],
      script: { key: 'calculate', required: true },
      evidence: ['flow-node:step:process_1:step_1'],
    }],
    rules: [{
      key: 'valid', title: '有效性规则', description: `${sourceId} 有效性`,
      content: '标识不能为空。', appliesToObjects: [objectKey], evidence: [`material:${sourceId}#规则`],
    }],
    metrics: [{
      key: 'count', title: '数量', description: `${sourceId} 数量`, caliber: '按标识去重计数。',
      boundaries: ['只统计有效记录'], usesObjects: [objectKey], evidence: [`material:${sourceId}#指标`],
    }],
    scripts: [{
      key: 'calculate', title: '计算脚本', description: `${sourceId} 的计算脚本`,
      boundOperation: operationKey, runtime: 'python', entrypoint: 'calculate.py', timeoutSeconds: 30,
      parameters: [], outputs: [{ key: 'count', type: 'integer' }], boundaries: ['只输出计数'],
      evidence: [`material:${sourceId}#脚本`],
      sourceCode: [
        'import argparse',
        'import json',
        'parser = argparse.ArgumentParser()',
        'parser.parse_args()',
        'print(json.dumps({"count": 1}))',
        '',
      ].join('\n'),
    }],
  };
  const diagrams = renderDiagramFiles({
    diagrams: [{
      id: 'process_1',
      title: '共享检查流程',
      nodes: [
        { id: 'start:process_1', label: '开始', kind: 'start' },
        { id: 'step:process_1:step_1', label: '检查', kind: 'action' },
        { id: 'end:process_1', label: '结束', kind: 'end' },
      ],
      edges: [
        { id: 'edge_1', from: 'start:process_1', to: 'step:process_1:step_1', kind: 'sequence' },
        { id: 'edge_2', from: 'step:process_1:step_1', to: 'end:process_1', kind: 'sequence' },
      ],
    }],
  });
  const files = { ...renderPackage(spec, { validateScripts: false }), ...diagrams };
  return {
    sourceId,
    filename: `source-${sourceId}.zip`,
    archive: {
      spec,
      coverage: { covered: [], uncovered: [] },
      files,
      rawFiles: Object.fromEntries(Object.entries(files).map(([path, content]) => [
        path,
        new TextEncoder().encode(content),
      ])),
      warnings: [],
    },
  };
}

test('批量融合无损改写大小写 key、引用、流程 ID、provider 和脚本入口', () => {
  const inputs = [source('alpha', 'Shared'), source('beta', 'shared')];
  const options = {
    id: 'fused-package',
    name: '融合知识包',
    description: '用于验证批量融合',
    version: '1.0.0-rc.1',
    concepts: [{
      id: 'common.shared',
      title: '共享概念',
      layer: 'common' as const,
      decision: 'preserve_views' as const,
      rationale: '字段口径不同，保留两个来源视图。',
      members: [
        { sourceId: 'alpha', kind: 'object' as const, key: 'Shared', relation: 'overlap' as const },
        { sourceId: 'beta', kind: 'object' as const, key: 'shared', relation: 'overlap' as const },
      ],
    }],
  };

  const result = fuseKnowledgePackages(inputs, options);
  assert.equal(result.quality.passed, true, JSON.stringify(result.quality.gates));
  assert.deepEqual(result.quality.counts, {
    object: 2, operation: 2, rule: 2, metric: 2, script: 2, diagram: 2,
  });
  assert.deepEqual(result.spec.objects.map((item) => item.key).sort(), ['Shared__alpha', 'shared__beta']);
  assert.deepEqual(result.spec.operations.map((item) => item.relatedObjects[0]).sort(), ['Shared__alpha', 'shared__beta']);
  assert.equal(new Set(result.spec.scripts?.map((item) => item.entrypoint)).size, 2);

  const mappings = JSON.parse(String(result.files['mappings/object-data-mappings.json'])) as RenderedMappingDocument;
  assert.deepEqual(Object.values(mappings.objects).map((item) => item.provider_alias).sort(), [
    'alpha__pkg', 'beta__pkg',
  ]);
  assert.equal(mappings.objects.Shared__alpha.resource, 'Shared__alpha');
  assert.equal(mappings.objects.shared__beta.resource, 'shared__beta');

  const processes = JSON.parse(String(result.files['diagrams/processes.json'])) as RenderedFlowDocument;
  assert.equal(new Set(processes.diagrams.map((item) => item.id)).size, 2);
  const nodeIds = processes.diagrams.flatMap((item) => item.nodes.map((node) => node.id));
  assert.equal(new Set(nodeIds).size, nodeIds.length);
  for (const operation of result.spec.operations) {
    const nodeId = operation.evidence?.[0].slice('flow-node:'.length);
    assert.ok(nodeIds.includes(nodeId));
  }

  const reversed = fuseKnowledgePackages([...inputs].reverse(), options);
  assert.equal(packageFusionDigest(result.files), packageFusionDigest(reversed.files));
  const zip = zipPackage(result.files, result.spec.id);
  assert.deepEqual(zip, zipPackage(reversed.files, reversed.spec.id));
  const roundTrip = parsePackageArchive(zip);
  assert.equal(roundTrip.spec.objects.length, 2);
  assert.equal(roundTrip.spec.operations.length, 2);
  assert.equal(roundTrip.spec.scripts?.length, 2);
  assert.equal(result.ledger.resources.length, 12);
  assert.equal(result.ledger.concepts[0].members.length, 2);
});

test('语义概念成员不存在时阻断融合，不生成猜测关系', () => {
  const inputs = [source('alpha', 'Alpha'), source('beta', 'Beta')];
  assert.throws(() => fuseKnowledgePackages(inputs, {
    id: 'fused-package', name: '融合知识包', description: '验证语义门禁', version: '1.0.0',
    concepts: [{
      id: 'missing', title: '不存在的概念', layer: 'common', decision: 'preserve_views',
      rationale: '测试',
      members: [
        { sourceId: 'alpha', kind: 'object', key: 'Alpha', relation: 'overlap' },
        { sourceId: 'beta', kind: 'object', key: 'Missing', relation: 'overlap' },
      ],
    }],
  }), /不存在的成员/u);
});

test('稳定后缀与来源原 key 再次相撞时继续分配唯一 key', () => {
  const result = fuseKnowledgePackages([
    source('alpha', 'Shared'),
    source('beta', 'shared'),
    source('gamma', 'Shared__alpha'),
  ], {
    id: 'fused-package', name: '融合知识包', description: '验证二次碰撞', version: '1.0.0',
  });
  const keys = result.spec.objects.map((object) => object.key);
  assert.equal(result.quality.passed, true, JSON.stringify(result.quality.gates));
  assert.equal(new Set(keys.map((key) => key.toLocaleLowerCase('en-US'))).size, 3);
  assert.ok(keys.some((key) => key.startsWith('Shared__alpha__gamma_')));
});

test('逐元素归并会重写引用并保留两份来源追溯', () => {
  const result = fuseKnowledgePackages([
    source('alpha', 'Shared'),
    source('beta', 'shared'),
  ], {
    id: 'canonical-package', name: '归并知识包', description: '验证证据等价归并', version: '1.0.0',
    dispositions: [{
      sourceId: 'beta', kind: 'object', key: 'shared', action: 'canonicalize',
      targetSourceId: 'alpha', targetKey: 'Shared', reason: '专家确认两者字段与业务口径等价。',
    }],
  });

  assert.equal(result.quality.passed, true, JSON.stringify(result.quality.gates));
  assert.equal(result.spec.objects.length, 1);
  assert.equal(result.spec.objects[0].key, 'Shared');
  assert.ok(result.spec.operations.every((operation) => operation.relatedObjects[0] === 'Shared'));
  const canonical = result.ledger.resources.find((entry) => (
    entry.sourceId === 'beta' && entry.kind === 'object' && entry.sourceKey === 'shared'
  ));
  assert.equal(canonical?.disposition, 'canonicalized');
  assert.equal(canonical?.fusedKey, 'Shared');
  assert.deepEqual(canonical?.canonicalTarget, { sourceId: 'alpha', key: 'Shared' });
  assert.equal(canonical?.decisionReason, '专家确认两者字段与业务口径等价。');
});

test('排除仍被保留元素引用的内容时由闭包门禁阻断', () => {
  assert.throws(() => fuseKnowledgePackages([
    source('alpha', 'Shared'),
    source('beta', 'Other'),
  ], {
    id: 'excluded-package', name: '排除知识包', description: '验证排除闭包', version: '1.0.0',
    dispositions: [{
      sourceId: 'beta', kind: 'script', key: 'calculate', action: 'exclude',
      reason: '本次融合不执行来源脚本。',
    }],
  }), /仍引用已排除的 script\/calculate/u);
});
