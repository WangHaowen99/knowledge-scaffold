import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import {
  evidenceFromSources, evidenceInProject, isEvidenceMcp, isEvidenceNode, isEvidencePack, isEvidencePage, withoutEvidenceSuggestions,
} from '../src/state/evidence.js';

const projectWith = (ext) => ({ hasSubpage: (id) => (ext.subpages || []).some((s) => s.id === id) });

test('证据源可用：项目内看有没有数据源子页，项目外看 EV-01 读不读得到', () => {
  assert.equal(evidenceInProject(projectWith(F.extensions())), true);
  assert.equal(evidenceInProject(projectWith(F.extensions(['trace', 'knowledge', 'codegen', 'diagram']))), false);
  assert.equal(evidenceInProject(null), false);
  assert.equal(evidenceFromSources({ items: [] }), true);
  assert.equal(evidenceFromSources(null), false);
});

test('依赖证据源的包、节点、MCP、页面', () => {
  assert.equal(isEvidencePack('reverse'), true);
  assert.equal(isEvidencePack('trace'), false);
  assert.equal(isEvidenceNode({ id: 'x', kind: 'external' }), true);
  assert.equal(isEvidenceNode({ id: 'rev.evidence', kind: 'agent', plugin: { name: 'reverse', label: '系统逆向建模' } }), true);
  assert.equal(isEvidenceNode({ id: 'core.model', kind: 'agent', plugin: null }), false);
  assert.equal(isEvidenceMcp({ id: 'evidence' }), true);
  assert.equal(isEvidenceMcp({ name: 'evidence.search' }), true);
  assert.equal(isEvidenceMcp({ name: 'artifact_list' }), false);
  assert.deepEqual(['sources', 'snapshots', 'schema', 'resources'].map(isEvidencePage), [true, true, true, false]);
});

test('建议条：证据源不可用时去掉跳到证据源页面的建议，可用时原样', () => {
  const sug = {
    mode: 'normal',
    primary: { id: 'p', label: '连接业务系统', action: { type: 'navigate', page: 'sources' } },
    secondary: [
      { id: 'a', label: '先聊聊目标', action: { type: 'send', text: '先聊聊目标' } },
      { id: 'b', label: '采集一次快照', action: { type: 'navigate', page: 'snapshots' } },
    ],
    all_commands: true,
  };
  const off = withoutEvidenceSuggestions(sug, false);
  assert.equal(off.primary, null);
  assert.deepEqual(off.secondary.map((s) => s.id), ['a']);
  assert.equal(off.all_commands, true);
  assert.equal(withoutEvidenceSuggestions(sug, true), sug);
  assert.equal(withoutEvidenceSuggestions(null, false), null);
});

test('C-01 features.evidence === false 之后：EV-01 不再发请求，直接按 501 evidence_not_available 失败', async () => {
  const { createApi } = await import('../src/api/endpoints.js');
  const calls = [];
  const client = {
    get: async (path) => { calls.push(path); return path === '/client-config' ? { features: { evidence: false } } : { items: [] }; },
  };
  const api = createApi(client);
  await api.clientConfig();
  await assert.rejects(api.evidenceSources(), (e) => e.status === 501 && e.code === 'evidence_not_available');
  await assert.rejects(api.evidenceSources({ project_id: 'p1' }), (e) => e.code === 'evidence_not_available');
  assert.deepEqual(calls, ['/client-config']);
  // 后端没给这个开关（旧版本、mock）时照常请求
  const plain = createApi({ get: async (path) => { calls.push(path); return path === '/client-config' ? { features: {} } : { items: [] }; } });
  await plain.clientConfig();
  assert.deepEqual(await plain.evidenceSources(), { items: [] });
});
