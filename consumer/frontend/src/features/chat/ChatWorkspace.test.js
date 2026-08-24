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
const streamManagerSource = readFileSync(join(currentDir, 'hooks/useStreamManager.js'), 'utf8');
const runAdmissionSource = readFileSync(join(currentDir, 'lib/runAdmission.js'), 'utf8');
const chatSessionsSource = readFileSync(join(currentDir, 'hooks/useChatSessions.js'), 'utf8');
const messageBuilderSource = readFileSync(join(currentDir, 'lib/messageBuilder.js'), 'utf8');

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

test('session knowledge package options include the stable package id', () => {
  assert.equal(chatSource.includes('{packageDisplayLabel(ontology)}'), true);
});

test('session version appears directly after the knowledge package selector', () => {
  const ontologySelect = chatSource.indexOf('<select value={selectedOntologyId}');
  const versionNote = chatSource.indexOf('{displayedOntologyVersion && (', ontologySelect);
  const configRowEnd = chatSource.indexOf('</div>', ontologySelect);

  assert.ok(ontologySelect >= 0);
  assert.ok(versionNote > ontologySelect);
  assert.ok(versionNote < configRowEnd);
  assert.equal((chatSource.match(/<div className="chat-config-locks">/g) || []).length, 1);
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

test('live chat consumes PublicEvent while legacy decoding stays in hydration', () => {
  assert.equal(streamManagerSource.includes('decodePublicEventFrame'), true);
  assert.equal(streamManagerSource.includes('error instanceof PublicEventProtocolError'), true);
  assert.equal(streamManagerSource.includes('reader.cancel(error).catch'), true);
  assert.equal(streamManagerSource.includes('agent-stream.v2'), false);
  assert.equal(agenticStreamSource.includes('applyPublicEvent'), true);
  assert.equal(agenticStreamSource.includes('applyLocalRunTermination'), true);
  assert.equal(agenticStreamSource.includes('agent-stream.v2'), false);
  assert.equal(messageBuilderSource.includes('storedAgentEventToPublicEvent'), true);
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

test('business data progress rows display and wrap their provider event text before output', () => {
  assert.equal(tracePanelSource.includes('businessDataTraceLabel(node)'), true);
  assert.equal(tracePanelSource.includes("isBusinessDataProgress ? 'trace-progress-label' : ''"), true);
  assert.equal(chatCssSource.includes('.mwr .trace .trace-progress-label { overflow: visible; text-overflow: clip; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word;'), true);
  assert.equal(tracePanelSource.indexOf('className="trace-node-progress"') < tracePanelSource.indexOf('<ToolOutput'), true);
});

test('prompt editor and user messages preserve multiline formatting', () => {
  const promptRule = chatCssSource.match(/\.mwr \.ti-box \.ti-input\s*\{([^}]*)\}/)?.[1] || '';
  const userBubbleRule = chatCssSource.match(/\.mwr \.msg\.u \.bub\s*\{([^}]*)\}/)?.[1] || '';

  assert.match(promptRule, /white-space:\s*pre-wrap/);
  assert.match(promptRule, /overflow-wrap:\s*anywhere/);
  assert.match(promptRule, /resize:\s*none/);
  assert.match(userBubbleRule, /white-space:\s*pre-wrap/);
  assert.match(userBubbleRule, /overflow-wrap:\s*anywhere/);
});

test('live run dots keep a visible heartbeat in reduced-motion environments', () => {
  assert.match(chatCssSource, /@keyframes\s+mw-dot-heartbeat/);
  assert.match(
    chatCssSource,
    /\.mwr \.run-status-summary \.tdots span\s*\{\s*animation-duration:\s*1\.2s !important;\s*animation-iteration-count:\s*infinite !important;/,
  );
});

test('new chat navigation is active only for a blank chat session', () => {
  assert.equal(
    appShellSource.includes("className={surface === 'chat' && !chat.currentChatId ? 'on' : ''}"),
    true,
  );
});

test('awaiting interaction can be cancelled by button or Escape', () => {
  assert.match(chatSource, /sessions\/\$\{encodeURIComponent\(targetSessionId\)\}\/interactions\/\$\{encodeURIComponent\(interactionId\)\}\/cancel/);
  assert.equal(chatSource.includes("headers: { 'Idempotency-Key': createIdempotencyKey() }"), true);
  assert.equal(chatSource.includes("event.key !== 'Escape'"), true);
  assert.equal(chatSource.includes('取消（Esc）'), true);
  assert.equal(chatSource.includes('applyInteractionCancellation'), true);
});

test('awaiting interaction creates a new server-assigned run from cursor zero', () => {
  assert.equal(runAdmissionSource.includes('interaction_response: options.interactionResponse'), true);
  assert.equal(agenticStreamSource.includes('streamManager.startRunForSession('), true);
  assert.equal(agenticStreamSource.includes('runLifecycle.activeRunId = admission.runId;'), true);
  assert.equal(streamManagerSource.includes('cursor: 0'), true);
});

test('pending interaction replaces and guards the ordinary composer', () => {
  assert.equal(chatSource.includes('{pendingInteraction ? ('), true);
  assert.equal(
    chatSource.includes('if (!workspaceDataReady || !text || streamState.isLoading || pendingInteraction) return;'),
    true,
  );
});
