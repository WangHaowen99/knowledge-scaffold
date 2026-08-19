import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DocumentReadIncompleteError,
  DocumentReadOffsetError,
  DocumentReadStalledError,
  DocumentReadSession,
  runAgentWithDocumentReadGate,
} from '../src/workflow/document-read-session.js';
import { verifiedDocumentReadCoverage } from '../src/workflow/document-read-coverage.js';

test('全文读取账本只在从 0 连续覆盖到 EOF 后完成', () => {
  const session = new DocumentReadSession([
    { filename: '营销产品手册.docx', textContent: 'x'.repeat(12_500) },
  ], { fullReadMode: true, chunkSize: 6_000 });

  assert.deepEqual(session.read('营销产品手册.docx', 0), {
    filename: '营销产品手册.docx', offset: 0, endOffset: 6_000,
    totalChars: 12_500, nextOffset: 6_000, eof: false,
    content: 'x'.repeat(6_000),
  });
  session.read('营销产品手册.docx', 6_000, {
    previousPageSummary: '第一页摘要',
    previousPageEvidence: [{ offset: 120, note: '产品适用条件' }],
  });
  assert.equal(session.complete, false);
  session.read('营销产品手册.docx', 12_000, { previousPageSummary: '第二页摘要' });

  assert.deepEqual(session.ledger('营销产品手册.docx'), {
    filename: '营销产品手册.docx', totalChars: 12_500,
    ranges: [[0, 12_500]], contiguousEnd: 12_500,
    eofSeen: true, complete: true,
  });
  assert.equal(session.complete, true);
});

test('全文模式拒绝跳 offset，且重复区间不会重复计数', () => {
  const session = new DocumentReadSession([
    { filename: 'manual.md', textContent: 'a'.repeat(13_000) },
  ], { fullReadMode: true, chunkSize: 6_000 });

  session.read('manual.md', 0);
  assert.throws(
    () => session.read('manual.md', 12_000, { previousPageSummary: '第一页摘要' }),
    (error: unknown) => error instanceof DocumentReadOffsetError && error.expectedOffset === 6_000,
  );

  const relaxed = new DocumentReadSession([
    { filename: 'manual.md', textContent: 'a'.repeat(13_000) },
  ], { fullReadMode: false, chunkSize: 6_000 });
  relaxed.read('manual.md', 0);
  relaxed.read('manual.md', 0);
  assert.equal(relaxed.ledger('manual.md').contiguousEnd, 6_000);
  assert.deepEqual(relaxed.ledger('manual.md').ranges, [[0, 6_000]]);
});

test('同名文档必须明确拒绝，不能在账本中静默覆盖', () => {
  assert.throws(() => new DocumentReadSession([
    { id: 'doc-1', filename: 'manual.md', textContent: '第一份正文' },
    { id: 'doc-2', filename: 'manual.md', textContent: '第二份正文' },
  ], { fullReadMode: true }), /文档文件名重复.*manual\.md/u);
});

test('全文未完成时错误明确列出已读和未读范围', () => {
  const session = new DocumentReadSession([
    { filename: '营销产品手册.docx', textContent: 'x'.repeat(199_873) },
  ], { fullReadMode: true, chunkSize: 6_000 });
  session.read('营销产品手册.docx', 0);
  session.read('营销产品手册.docx', 6_000, { previousPageSummary: '第一页摘要' });

  const error = new DocumentReadIncompleteError(session);
  assert.match(error.message, /本次未完成全文读取/);
  assert.match(error.message, /营销产品手册\.docx 已读取 12000 \/ 199873 字符/);
  assert.match(error.message, /未读取区间为 12000–199873/);
});

test('20 万字符文档可以连续读取 34 页并覆盖全部正文', () => {
  const document = { filename: 'manual.md', textContent: 'x'.repeat(200_000) };
  const session = new DocumentReadSession([document], { fullReadMode: true });
  let offset = 0;
  let pages = 0;
  while (!session.complete) {
    const page = session.read(document.filename, offset, pages
      ? { previousPageSummary: `第 ${pages} 页摘要` }
      : {});
    offset = page.nextOffset ?? page.endOffset;
    pages += 1;
  }

  assert.equal(pages, 34);
  assert.equal(session.ledger(document.filename).contiguousEnd, 200_000);
  assert.equal(session.coveredChars, 200_000);
});

test('全文读取续读次数不受旧的 128 回合上限限制', async () => {
  const document = { filename: 'manual.md', textContent: 'x'.repeat(129 * 6_000) };
  const session = new DocumentReadSession([document], { fullReadMode: true });
  let listener: ((event: unknown) => void) | undefined;
  let calls = 0;
  const agent = {
    state: { messages: [] as unknown[] },
    subscribe: (next: (event: unknown) => void) => {
      listener = next;
      return () => { listener = undefined; };
    },
    prompt: async () => {
      calls += 1;
      const ledger = session.allLedgers().find((item) => !item.complete);
      if (!ledger) return;
      const page = session.read(document.filename, ledger.contiguousEnd,
        ledger.contiguousEnd ? { previousPageSummary: '上一页摘要' } : {});
      agent.state.messages.push({
        role: 'toolResult', toolName: 'read_document',
        details: { filename: page.filename, offset: page.offset, endOffset: page.endOffset },
      });
      listener?.({ type: 'turn_end' });
    },
    waitForIdle: async () => undefined,
    abort: () => undefined,
  };

  await runAgentWithDocumentReadGate({
    agent,
    prompt: '读取全部资料',
    session,
    captured: () => session.complete,
  });

  assert.ok(calls > 128);
  assert.equal(session.complete, true);
  assert.equal(session.coveredChars, document.textContent.length);
});

test('连续无进展达到保护阈值时停止全文读取', async () => {
  let listener: ((event: unknown) => void) | undefined;
  let calls = 0;
  let aborts = 0;
  const session = new DocumentReadSession([
    { filename: 'manual.md', textContent: 'x'.repeat(12_000) },
  ], { fullReadMode: true });
  const agent = {
    state: { messages: [] as unknown[] },
    subscribe: (next: (event: unknown) => void) => {
      listener = next;
      return () => { listener = undefined; };
    },
    prompt: async () => {
      calls += 1;
      listener?.({ type: 'turn_end' });
    },
    waitForIdle: async () => undefined,
    abort: () => { aborts += 1; },
  };

  await assert.rejects(
    runAgentWithDocumentReadGate({
      agent,
      prompt: '读取全部资料',
      session,
      noProgressTurnLimit: 3,
    }),
    (error: unknown) => error instanceof DocumentReadStalledError
      && error.noProgressTurns === 3
      && error.noProgressTurnLimit === 3,
  );
  assert.equal(calls, 3);
  assert.equal(aborts, 1);
});

test('旧文档页在模型上下文中压缩为摘要和 offset 证据', () => {
  const session = new DocumentReadSession([
    { filename: 'manual.md', textContent: 'a'.repeat(13_000) },
  ], { fullReadMode: true, chunkSize: 6_000 });
  session.read('manual.md', 0);
  session.read('manual.md', 6_000, {
    previousPageSummary: '第一章讲准入条件',
    previousPageEvidence: [{ offset: 88, note: '年龄门槛' }],
  });
  const messages = session.compactMessages([
    {
      role: 'toolResult', toolName: 'read_document', toolCallId: 'r1',
      content: [{ type: 'text', text: 'a'.repeat(6_000) }],
      details: { filename: 'manual.md', offset: 0, endOffset: 6_000 },
    },
    {
      role: 'toolResult', toolName: 'read_document', toolCallId: 'r2',
      content: [{ type: 'text', text: 'a'.repeat(6_000) }],
      details: { filename: 'manual.md', offset: 6_000, endOffset: 12_000 },
    },
  ] as any[]);

  assert.match((messages[0] as any).content[0].text, /第一章讲准入条件/);
  assert.match((messages[0] as any).content[0].text, /88/);
  assert.equal((messages[1] as any).content[0].text.length, 6_000);
});

test('历史全文覆盖记录必须绑定当前文档修订，不能复用旧修订', () => {
  const coverage = [{
    documentId: 'doc_manual', filename: 'manual.md', revisionFingerprint: 'doc_manual:rev1',
    totalChars: 12_500, ranges: [[0, 12_500] as [number, number]],
    contiguousEnd: 12_500, eofSeen: true, complete: true,
  }];

  assert.equal(verifiedDocumentReadCoverage(coverage, [{
    id: 'doc_manual', filename: 'manual.md', revisionFingerprint: 'doc_manual:rev2',
    textContent: 'x'.repeat(12_500),
  }]).length, 0);
  assert.equal(verifiedDocumentReadCoverage(coverage, [{
    id: 'doc_manual', filename: 'manual.md', revisionFingerprint: 'doc_manual:rev1',
    textContent: 'x'.repeat(12_500),
  }]).length, 1);
});
