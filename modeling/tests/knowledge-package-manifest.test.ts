import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  latestKnowledgePackageManifestId,
  loadLatestKnowledgePackageManifestId,
  parseKnowledgePackageManifestId,
} from '../frontend/src/knowledge-package-manifest.js';

test('知识包 manifest：读取普通或双引号包裹的 id', () => {
  assert.equal(parseKnowledgePackageManifestId('kind: ontology-package\nid: ontology-latest\n'), 'ontology-latest');
  assert.equal(parseKnowledgePackageManifestId('kind: ontology-package\nid: "ontology:latest"\n'), 'ontology:latest');
});

test('知识包 manifest：自动同步使用最新生成内容并在 id 缺失时报错', () => {
  assert.equal(latestKnowledgePackageManifestId({
    content: 'id: manually-overridden\n',
    rendered: 'id: latest-generated\n',
  }), 'latest-generated');
  assert.throws(
    () => latestKnowledgePackageManifestId({ content: 'kind: ontology-package\n' }),
    /manifest\.yaml.*id/,
  );
});

test('知识包 manifest：自动同步固定读取最新版本的 manifest.yaml', async () => {
  const calls: unknown[][] = [];
  const ontologyId = await loadLatestKnowledgePackageManifestId(
    'project-001',
    async (...args: unknown[]) => {
      calls.push(args);
      return { rendered: 'id: ontology-latest\n' };
    },
  );

  assert.equal(ontologyId, 'ontology-latest');
  assert.deepEqual(calls, [['project-001', 'manifest.yaml']]);
});
