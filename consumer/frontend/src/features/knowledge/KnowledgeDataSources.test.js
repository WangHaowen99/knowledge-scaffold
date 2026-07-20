import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const currentDir = dirname(fileURLToPath(import.meta.url));
const normalizeSource = (source) => source.replace(/\r\n?/g, '\n');
const readNormalizedSource = (path) => normalizeSource(readFileSync(path, 'utf8'));
const dataSourceSource = readNormalizedSource(join(currentDir, 'KnowledgeDataSources.jsx'));
const configCenterSource = readNormalizedSource(join(currentDir, '../settings/ConfigCenter.jsx'));
const workbenchSource = readNormalizedSource(join(currentDir, 'KnowledgeWorkbench.jsx'));
const onboardingSource = readNormalizedSource(join(currentDir, '../../app/onboarding/OnboardingWizard.jsx'));

test('source assertions normalize LF and CRLF checkouts identically', () => {
  const lfSource = "setProvider('');\n      setConfig({});";
  const crlfSource = lfSource.replace(/\n/g, '\r\n');

  assert.equal(normalizeSource(crlfSource), lfSource);
});

test('knowledge data source editor exposes only the ontology direct-provider model', () => {
  assert.equal(dataSourceSource.includes('/agentic/data-connections'), false);
  assert.equal(dataSourceSource.includes('connection_ref'), false);
  assert.equal(dataSourceSource.includes('共享连接'), false);
  assert.equal(dataSourceSource.includes("mode: 'direct_provider'"), true);
  assert.equal(dataSourceSource.includes('/data-source${suffix}'), true);
});

test('provider schemas are committed before the ontology data source is loaded', () => {
  const providerFetchIndex = dataSourceSource.indexOf("authFetch('/agentic/business-data/provider-schemas')");
  const setProvidersIndex = dataSourceSource.indexOf('setProviders(nextProviders)');
  const sourceFetchIndex = dataSourceSource.indexOf('/data-source`');

  assert.ok(providerFetchIndex >= 0);
  assert.ok(setProvidersIndex > providerFetchIndex);
  assert.ok(sourceFetchIndex > setProvidersIndex);
});

test('stale scope requests cannot restore a previous ontology data source', () => {
  assert.equal(dataSourceSource.includes('const loadRequestRef = useRef(0);'), true);
  assert.equal(dataSourceSource.includes('const operationRequestRef = useRef(0);'), true);
  assert.equal(dataSourceSource.includes('sameScope(currentScopeRef.current, targetScope)'), true);
  assert.equal(dataSourceSource.includes('setSourceLoadError(true);'), true);
  assert.equal(dataSourceSource.includes("setProvider('');\n      setConfig({});"), true);
  assert.equal(dataSourceSource.includes('重新加载'), true);
});

test('secret fields are omitted when left blank', () => {
  assert.equal(
    dataSourceSource.includes("if (field.secret && (value === '' || value === null || value === undefined)) return;"),
    true,
  );
  assert.equal(dataSourceSource.includes('已配置，留空保留原值'), true);
});

test('workspace config center no longer exposes connection or binding sections', () => {
  assert.equal(configCenterSource.includes("{ id: 'conn', name: '数据连接' }"), false);
  assert.equal(configCenterSource.includes("{ id: 'bind', name: '知识与数据绑定' }"), false);
  assert.equal(configCenterSource.includes('function DataConnections()'), false);
  assert.equal(configCenterSource.includes('function KnowledgeBinding()'), false);
  assert.equal(configCenterSource.includes('/agentic/data-connections'), false);
  assert.equal(configCenterSource.includes('/data-binding'), false);
});

test('saving or clearing a data source refreshes the knowledge package summary', () => {
  assert.equal(dataSourceSource.includes('onDataSourceChanged?.()'), true);
  assert.equal(workbenchSource.includes('onDataSourceChanged={retryPackages}'), true);
});

test('onboarding directs admins to the ontology data-source tab', () => {
  assert.equal(onboardingSource.includes("sec: 'conn'"), false);
  assert.equal(onboardingSource.includes("sec: 'bind'"), false);
  assert.equal(onboardingSource.includes("tab: 'data'"), true);
  assert.equal(onboardingSource.includes('target: \'kb-data-source\''), true);
});
