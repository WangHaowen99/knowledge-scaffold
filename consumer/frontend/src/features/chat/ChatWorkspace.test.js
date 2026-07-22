import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { getChatDataSourceStatus } from './lib/chatDataSourceStatus.js';

const currentDir = dirname(fileURLToPath(import.meta.url));
const chatSource = readFileSync(join(currentDir, 'ChatWorkspace.jsx'), 'utf8');
const tracePanelSource = readFileSync(join(currentDir, 'components/TracePanel.jsx'), 'utf8');
const chatCssSource = readFileSync(join(currentDir, 'chat.css'), 'utf8');
const appShellSource = readFileSync(join(currentDir, '../../app/AppShell.jsx'), 'utf8');
const agenticStreamSource = readFileSync(join(currentDir, 'hooks/useAgenticStream.js'), 'utf8');
const chatSessionsSource = readFileSync(join(currentDir, 'hooks/useChatSessions.js'), 'utf8');

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

test('chat activity refreshes session ordering at send and terminal stream states', () => {
  assert.equal(chatSource.includes('onSessionUpdated: refreshSessionTime'), true);
  assert.equal(agenticStreamSource.includes('onSessionUpdated?.();'), true);
  assert.equal(chatSessionsSource.includes('const touchSession = useCallback'), true);
  assert.equal(chatSessionsSource.includes('updated_at: updatedAt'), true);
});

test('agent stream ownership remains mounted while the chat surface changes', () => {
  assert.equal(appShellSource.includes('const agenticStream = useAgenticStream({'), true);
  assert.equal(appShellSource.includes('agenticStream={agenticStream}'), true);
  assert.equal(chatSource.includes('const { sendAgentic, resumeAgentic } = agenticStream;'), true);
});

test('trace panel has one accessible toggle and keeps the mobile backdrop close action', () => {
  assert.equal(chatSource.includes("const traceToggleLabel = traceOpen ? '收起运行轨迹' : '展开运行轨迹';"), true);
  assert.equal(chatSource.includes('className="iconbtn" type="button" title={traceToggleLabel} aria-label={traceToggleLabel}'), true);
  assert.equal(chatSource.includes('onClick={() => setTraceOpen((v) => !v)}'), true);
  assert.equal(chatSource.includes('className="trace-backdrop"'), true);
  assert.equal(chatSource.includes('onClose={() => setTraceOpen(false)}'), false);
  assert.equal(tracePanelSource.includes('onClose'), false);
  assert.equal(tracePanelSource.includes('trace-close'), false);
  assert.equal(chatCssSource.includes('trace-close'), false);
});

test('new chat navigation is active only for a blank chat session', () => {
  assert.equal(
    appShellSource.includes("className={surface === 'chat' && !chat.currentChatId ? 'on' : ''}"),
    true,
  );
});

test('awaiting interaction can be cancelled by button or Escape', () => {
  assert.match(chatSource, /interactions\/\$\{encodeURIComponent\(interactionId\)\}\/cancel/);
  assert.equal(chatSource.includes("event.key !== 'Escape'"), true);
  assert.equal(chatSource.includes('取消（Esc）'), true);
  assert.equal(chatSource.includes('applyInteractionCancellation'), true);
});
