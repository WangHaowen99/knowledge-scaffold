import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { getChatDataSourceStatus } from './chatDataSourceStatus.js';

const currentDir = dirname(fileURLToPath(import.meta.url));
const chatSource = readFileSync(join(currentDir, 'ChatWorkspace.jsx'), 'utf8');
const appShellSource = readFileSync(join(currentDir, 'AppShell.jsx'), 'utf8');

test('automatic ontology selection does not claim that business data is ready', () => {
  const status = getChatDataSourceStatus(null);

  assert.equal(status.kind, 'automatic');
  assert.equal(status.kicker, '知识问答已就绪');
  assert.equal(status.actionLabel, '');
});

test('ready data source is reported as configured without claiming a successful connection test', () => {
  const status = getChatDataSourceStatus({
    business_data: { configured: true, ready: true },
  });

  assert.equal(status.kind, 'ready');
  assert.equal(status.kicker, '知识包与数据源已配置');
  assert.equal(status.actionLabel, '');
});

test('missing data source keeps knowledge chat available and offers configuration', () => {
  const status = getChatDataSourceStatus({
    business_data: { configured: false, ready: false },
  });

  assert.equal(status.kind, 'not-configured');
  assert.match(status.description, /仍可进行知识问答/);
  assert.equal(status.actionLabel, '配置数据源');
});

test('configured but unusable data source offers a repair path', () => {
  const status = getChatDataSourceStatus({
    business_data: { configured: true, ready: false },
  });

  assert.equal(status.kind, 'not-ready');
  assert.equal(status.actionLabel, '完善数据源配置');
});

test('unknown data source summary does not get treated as unconfigured', () => {
  const status = getChatDataSourceStatus({ id: 'demo' });

  assert.equal(status.kind, 'unknown');
  assert.equal(status.actionLabel, '查看数据源');
});

test('chat data source action opens the selected ontology data tab', () => {
  assert.equal(chatSource.includes('getChatDataSourceStatus(selectedOntology)'), true);
  assert.equal(chatSource.includes('onConfigureDataSource?.(selectedOntology.id)'), true);
  assert.equal(appShellSource.includes("navigate({ surface: 'kb', tab: 'data', ontologyId })"), true);
  assert.equal(appShellSource.includes("if (sf === 'kb' && ontologyId) kb.selectPkg(ontologyId);"), true);
});
