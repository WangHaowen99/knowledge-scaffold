import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const currentDir = dirname(fileURLToPath(import.meta.url));
const normalizeSource = (source) => source.replace(/\r\n?/g, '\n');
const readNormalizedSource = (path) => normalizeSource(readFileSync(path, 'utf8'));
const dataSourceSource = readNormalizedSource(join(currentDir, 'KnowledgeDataSources.jsx'));
const kbBrowserSource = readNormalizedSource(join(currentDir, 'hooks/useKbBrowser.js'));
const workbenchSource = readNormalizedSource(join(currentDir, 'KnowledgeWorkbench.jsx'));
const onboardingSource = readNormalizedSource(join(currentDir, '../../app/onboarding/OnboardingWizard.jsx'));

test('source assertions normalize LF and CRLF checkouts identically', () => {
  const lfSource = "setProvider('');\n      setConfig({});";
  const crlfSource = lfSource.replace(/\n/g, '\r\n');

  assert.equal(normalizeSource(crlfSource), lfSource);
});

test('knowledge data source editor uses the ontology direct-provider model', () => {
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

test('saving or clearing a data source invalidates the package summary before independent readback', () => {
  const submitStart = dataSourceSource.indexOf('const submit = async (action) => {');
  const removeStart = dataSourceSource.indexOf('const remove = async () => {');
  const submitBody = dataSourceSource.slice(submitStart, removeStart);
  const removeBody = dataSourceSource.slice(removeStart);
  const submitNotify = submitBody.indexOf('notifyDataSourceChanged(targetScope)');
  const removeNotify = removeBody.indexOf('notifyDataSourceChanged(targetScope)');

  assert.ok(submitStart >= 0);
  assert.ok(removeStart > submitStart);
  assert.ok(submitNotify >= 0);
  assert.ok(submitNotify < submitBody.indexOf('const data = await response.json()'));
  assert.ok(submitNotify < submitBody.indexOf('const reloaded = await load();'));
  assert.ok(removeNotify >= 0);
  assert.ok(removeNotify < removeBody.indexOf('const data = await response.json()'));
  assert.ok(removeNotify < removeBody.indexOf('const reloaded = await load();'));
  assert.equal(
    dataSourceSource.includes('return Promise.resolve(onDataSourceChanged?.(targetScope)).catch(() => undefined);'),
    true,
  );
  assert.equal(workbenchSource.includes('onDataSourceChanged={retryPackages}'), true);
});

test('opening the Markdown picker clears stale package-operation notices first', () => {
  assert.equal(kbBrowserSource.includes("const clearOperationNotice = useCallback(() => setOpNotice(''), []);"), true);
  assert.equal(workbenchSource.includes('clearOperationNotice();\n    markdownUploadPicker.open();'), true);
});

test('knowledge write permissions are split by operation', () => {
  assert.equal(kbBrowserSource.includes("const canImport = can('ws.kb.import');"), true);
  assert.equal(kbBrowserSource.includes("const canActivate = can('ws.kb.activate');"), true);
  assert.equal(kbBrowserSource.includes("const canManage = can('ws.kb.write');"), true);
  assert.equal(workbenchSource.includes('{canActivate && !viewingActive &&'), true);
  assert.equal(dataSourceSource.includes("const canEdit = can('ws.data_source.write');"), true);
  assert.equal(dataSourceSource.includes("const canTest = can('ws.data_source.test');"), true);
  assert.equal(dataSourceSource.includes('{canTest && ('), true);
});

test('onboarding directs admins to the ontology data-source tab', () => {
  assert.equal(onboardingSource.includes("tab: 'data'"), true);
  assert.equal(onboardingSource.includes('target: \'kb-data-source\''), true);
});
