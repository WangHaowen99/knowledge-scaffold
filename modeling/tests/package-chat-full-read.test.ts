import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'package-chat-full-read-'));

const { getPackageArtifactStore } = await import('../src/artifacts/runtime.js');
const { buildFauxModeler } = await import('../src/llm/provider.js');
const { createProject } = await import('../src/projects/repo.js');
const {
  hasExplicitFullReadIntent,
  hasExplicitDocumentLookupIntent,
  hasDocumentBasedGenerationIntent,
  packageChatPlan,
  resolvePackageChatDocumentAccess,
} = await import('../src/workflow/package-chat.js');

test('packageChatPlan 仅识别明确的全文阅读诉求', () => {
  for (const message of [
    '读完整份营销产品手册',
    '把这份文档全文看完',
    '通读后再回答',
    '请完整阅读这份文档后回答',
    '完整阅读营销产品手册后给建议',
    '不用通读旧手册，请通读新手册',
    '不要只看摘要，请通读全文后回答',
    '请帮我补充规则，基于产品文档的docx的每个章节都有对应规则，请全量提取出来补充，可以拆分为多个文件。',
    '全量读取产品手册后，生成每个章节的产品匹配规则',
    '请逐章提取产品规则',
    '把所有章节的内容都梳理出来',
    '覆盖每一章后再生成规则',
  ]) {
    assert.equal(hasExplicitFullReadIntent(message), true, message);
  }
  for (const message of [
    '解释一下这个操作',
    '查一下产品准入条件',
    '根据知识包回答',
    '不用通读，直接解释当前规则',
    '无需重新通读这份资料',
    '不是要求全文阅读，只解释当前规则',
    '无需全量提取，只说明当前规则',
    '不用逐章梳理，只看前言',
  ]) {
    assert.equal(hasExplicitFullReadIntent(message), false, message);
  }
});

test('文档局部查询进入按需模式，全文诉求进入全文模式', () => {
  assert.equal(resolvePackageChatDocumentAccess('解释一下这个操作'), 'none');
  assert.equal(hasExplicitDocumentLookupIntent('请重新读取上传的产品手册第二章'), true);
  assert.equal(hasDocumentBasedGenerationIntent('基于产品手册生成产品匹配规则'), true);
  assert.equal(hasExplicitDocumentLookupIntent('基于产品手册生成产品匹配规则'), false);
  assert.equal(hasExplicitDocumentLookupIntent('不用重新读取这份文档，只解释当前规则'), false);
  assert.equal(resolvePackageChatDocumentAccess('请重新读取上传的产品手册第二章'), 'on-demand');
  assert.equal(resolvePackageChatDocumentAccess('基于产品手册生成产品匹配规则'), 'full');
  assert.equal(resolvePackageChatDocumentAccess('不要基于产品手册生成规则，只解释当前知识包'), 'none');
  assert.equal(resolvePackageChatDocumentAccess('全量读取产品手册后生成每个章节的规则'), 'full');
  assert.equal(resolvePackageChatDocumentAccess('定位新增资料里的准入条件'), 'on-demand');
  assert.equal(resolvePackageChatDocumentAccess('读完整份营销产品手册'), 'full');
  assert.equal(resolvePackageChatDocumentAccess('请全量提取每个章节的规则'), 'full');
  assert.equal(resolvePackageChatDocumentAccess('生成增量修改方案', 'full'), 'full');
});

test('有文本资料时普通包聊天也注入三个只读文档工具', async () => {
  const project = createProject({ owner: 'tester', name: '文档工具全分支注入' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id}`,
    name: project.name,
    description: '验证普通聊天也能看到只读文档工具',
  });

  let toolNames: string[] = [];
  const core = createFauxCore({ provider: 'tool-injection', models: [{ id: 'tool-injection' }] });
  const result = await packageChatPlan(project, [], '解释一下当前操作', {
    documents: [{ filename: '营销产品手册.docx', textContent: '第一章 产品准入条件' }],
    documentAccess: 'full-read-only',
    llmOverride: {
      model: core.getModel(), getApiKey: () => 'faux',
      streamFn: ((model: unknown, context: any, options: unknown) => {
        toolNames = (context.tools ?? []).map((tool: any) => String(tool.name));
        core.setResponses([fauxAssistantMessage('已根据当前知识包回答。')]);
        return core.streamSimple(model as any, context, options as any);
      }) as any,
    },
  });

  assert.match(result.reply ?? '', /当前知识包/);
  assert.deepEqual(toolNames.sort(), ['list_documents', 'read_document', 'search_documents', 'submit_change_plan'].sort());
});

test('指定章节查询未直接回答时会被门禁推动先调用文档工具', async () => {
  const project = createProject({ owner: 'tester', name: '按需文档查询门禁' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id}`,
    name: project.name,
    description: '验证局部文档查询必须取得本轮文档证据',
  });

  const calls: string[] = [];
  const core = createFauxCore({ provider: 'document-lookup-gate', models: [{ id: 'document-lookup-gate' }] });
  core.setResponses([
    fauxAssistantMessage('我直接根据已有上下文回答。'),
    fauxAssistantMessage([fauxToolCall('search_documents', { query: '第二章' })]),
    fauxAssistantMessage('已根据本轮文档检索结果回答。'),
  ]);
  const result = await packageChatPlan(project, [], '请重新读取上传的产品手册第二章', {
    documents: [{ filename: '营销产品手册.docx', textContent: '第一章\n第二章 产品准入条件\n准入规则正文' }],
    documentAccess: 'full-read-only',
    llmOverride: {
      model: core.getModel(), getApiKey: () => 'faux',
      streamFn: ((model: unknown, context: any, options: unknown) => (
        core.streamSimple(model as any, context, options as any)
      )) as any,
    },
    observe: (event: any) => {
      if (event?.type === 'tool_execution_start') calls.push(String(event.toolName));
    },
  });

  assert.match(result.reply ?? '', /本轮文档检索结果/);
  assert.deepEqual(calls, ['search_documents']);
});

test('按需文档工具调用后没有最终文本时必须失败，不返回空答复占位', async () => {
  const project = createProject({ owner: 'tester', name: '文档工具空回复门禁' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id}`,
    name: project.name,
    description: '验证工具调用后没有最终文本时不会伪装成成功',
  });

  const core = createFauxCore({ provider: 'document-empty-final', models: [{ id: 'document-empty-final' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('search_documents', { query: '第二章' })]),
  ]);
  await assert.rejects(
    packageChatPlan(project, [], '请重新读取上传的产品手册第二章', {
      documents: [{ filename: '营销产品手册.docx', textContent: '第二章 产品准入条件' }],
      documentAccess: 'full-read-only',
      llmOverride: {
        model: core.getModel(), getApiKey: () => 'faux',
        streamFn: ((model: unknown, context: any, options: unknown) => (
          core.streamSimple(model as any, context, options as any)
        )) as any,
      },
    }),
  );
});

test('普通聊天明确区分证据锚点和后端全文覆盖账本', async () => {
  const project = createProject({ owner: 'tester', name: '锚点语义' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id}`,
    name: project.name,
    description: '验证普通聊天不会把少量 sourceRefs 误判为正文未读取',
  });

  let systemPrompt = '';
  const core = createFauxCore({ provider: 'anchor-semantics', models: [{ id: 'anchor-semantics' }] });
  const result = await packageChatPlan(project, [], '当前收录了哪些章节？', {
    documents: [{
      id: 'doc_manual', filename: '营销产品手册.docx', textContent: 'x'.repeat(12_500),
      revisionFingerprint: 'doc_manual:rev1',
    }],
    analysis: {
      domainOverview: '营销产品', candidateObjects: [], openQuestions: [],
      documentReadCoverage: [{
        documentId: 'doc_manual', filename: '营销产品手册.docx',
        revisionFingerprint: 'doc_manual:rev1', totalChars: 12_500,
        ranges: [[0, 12_500]], contiguousEnd: 12_500, eofSeen: true, complete: true,
      }],
    },
    documentAccess: 'full-read-only',
    llmOverride: {
      model: core.getModel(), getApiKey: () => 'faux',
      streamFn: ((model: unknown, context: any, options: unknown) => {
        systemPrompt = String(context.systemPrompt ?? '');
        core.setResponses([fauxAssistantMessage('已根据后端阅读覆盖记录说明。')]);
        return core.streamSimple(model as any, context, options as any);
      }) as any,
    },
  });

  assert.match(result.reply ?? '', /后端阅读覆盖记录/);
  assert.match(systemPrompt, /sourceRefs.*不是.*阅读/u);
  assert.match(systemPrompt, /营销产品手册\.docx.*12500.*完整/u);
});

test('packageChatPlan 明确要求全文时实际连续翻页后才生成答复', async () => {
  const project = createProject({ owner: 'tester', name: '全文聊天门禁' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id}`,
    name: project.name,
    description: '验证显式全文聊天的连续翻页门禁',
  });

  const readOffsets: number[] = [];
  const result = await packageChatPlan(project, [], '请完整阅读这份文档后回答', {
    documents: [{ filename: '营销产品手册.docx', textContent: 'x'.repeat(12_500) }],
    documentAccess: 'full-read-only',
    llmOverride: buildFauxModeler(),
    observe: (event: any) => {
      if (event?.type === 'tool_execution_start' && event.toolName === 'read_document') {
        readOffsets.push(Number(event.args?.offset ?? 0));
      }
    },
  });

  assert.match(result.reply ?? '', /演示模式/);
  assert.deepEqual(readOffsets, [0, 6_000, 12_000]);
});

test('packageChatPlan 对“每个章节全量提取”强制连续读完正文', async () => {
  const project = createProject({ owner: 'tester', name: '逐章全量提取门禁' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id}`,
    name: project.name,
    description: '验证穷举式提取诉求也进入全文读取模式',
  });

  const readOffsets: number[] = [];
  await packageChatPlan(
    project,
    [],
    '请帮我补充规则，基于产品文档的docx的每个章节都有对应规则，请全量提取出来补充，可以拆分为多个文件。',
    {
      documents: [{ filename: '营销产品手册.docx', textContent: 'x'.repeat(12_500) }],
      documentAccess: 'full-read-only',
      llmOverride: buildFauxModeler(),
      observe: (event: any) => {
        if (event?.type === 'tool_execution_start' && event.toolName === 'read_document') {
          readOffsets.push(Number(event.args?.offset ?? 0));
        }
      },
    },
  );

  assert.deepEqual(readOffsets, [0, 6_000, 12_000]);
});

test('基于文档生成产物时即使未写“全文”也强制连续读完正文', async () => {
  const project = createProject({ owner: 'tester', name: '文档生成默认全文' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id}`,
    name: project.name,
    description: '验证源文档驱动的生成请求默认完成全文覆盖',
  });

  const readOffsets: number[] = [];
  await packageChatPlan(project, [], '请基于营销产品手册生成产品匹配规则', {
    documents: [{ filename: '营销产品手册.docx', textContent: 'x'.repeat(12_500) }],
    documentAccess: 'full-read-only',
    llmOverride: buildFauxModeler(),
    observe: (event: any) => {
      if (event?.type === 'tool_execution_start' && event.toolName === 'read_document') {
        readOffsets.push(Number(event.args?.offset ?? 0));
      }
    },
  });

  assert.deepEqual(readOffsets, [0, 6_000, 12_000]);
});

test('packageChatPlan 强制全文模式无需显式措辞也会连续读完全部资料', async () => {
  const project = createProject({ owner: 'tester', name: '增量全文门禁' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id}`,
    name: project.name,
    description: '验证增量方案强制连续读取本轮全部文本资料',
  });

  const reads: string[] = [];
  const result = await packageChatPlan(project, [], '生成本轮增量修改方案', {
    documents: [
      { filename: '新增规则.docx', textContent: 'x'.repeat(6_500) },
      { filename: '补充说明.md', textContent: 'y'.repeat(500) },
    ],
    documentAccess: 'full',
    llmOverride: buildFauxModeler(),
    observe: (event: any) => {
      if (event?.type === 'tool_execution_start' && event.toolName === 'read_document') {
        reads.push(`${String(event.args?.filename)}:${Number(event.args?.offset ?? 0)}`);
      }
    },
  });

  assert.match(result.reply ?? '', /演示模式/);
  assert.deepEqual(reads, [
    '新增规则.docx:0',
    '新增规则.docx:6000',
    '补充说明.md:0',
  ]);
});
