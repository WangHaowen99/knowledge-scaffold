import type {
  AgentCaptureAttemptDiagnostics,
  StructuredToolChoiceState,
} from './agent.js';

export const DOCUMENT_READ_CHUNK_SIZE = 6_000;

export interface ReadableDocument {
  id?: string;
  filename: string;
  textContent?: string;
  revisionFingerprint?: string;
}

export interface DocumentReadLedger {
  filename: string;
  totalChars: number;
  ranges: Array<[number, number]>;
  contiguousEnd: number;
  eofSeen: boolean;
  complete: boolean;
}

export interface DocumentReadPage {
  filename: string;
  offset: number;
  endOffset: number;
  totalChars: number;
  nextOffset: number | null;
  eof: boolean;
  content: string;
}

export interface DocumentPageEvidence {
  offset: number;
  note: string;
}

export interface DocumentReadNotes {
  previousPageSummary?: string;
  previousPageEvidence?: DocumentPageEvidence[];
}

interface StoredPageNotes {
  summary: string;
  evidence: DocumentPageEvidence[];
}

interface LastReadPage {
  filename: string;
  offset: number;
  endOffset: number;
}

export interface DocumentReadSessionOptions {
  fullReadMode?: boolean;
  chunkSize?: number;
}

export class DocumentReadOffsetError extends Error {
  constructor(
    readonly filename: string,
    readonly requestedOffset: number,
    readonly expectedOffset: number,
  ) {
    super(`文档 ${filename} 必须连续读取：requestedOffset=${requestedOffset}, expectedOffset=${expectedOffset}`);
    this.name = 'DocumentReadOffsetError';
  }
}

export class DocumentReadSummaryRequiredError extends Error {
  constructor(readonly filename: string, readonly offset: number) {
    super(`继续读取前必须用 previousPageSummary 总结上一页（${filename} @${offset}）`);
    this.name = 'DocumentReadSummaryRequiredError';
  }
}

function mergeRanges(ranges: Array<[number, number]>, next: [number, number]): Array<[number, number]> {
  if (next[1] <= next[0]) return ranges;
  const sorted = [...ranges, next].sort((left, right) => left[0] - right[0]);
  const merged: Array<[number, number]> = [];
  for (const range of sorted) {
    const current = merged.at(-1);
    if (!current || range[0] > current[1]) {
      merged.push([...range]);
      continue;
    }
    current[1] = Math.max(current[1], range[1]);
  }
  return merged;
}

function contiguousEnd(ranges: Array<[number, number]>): number {
  let end = 0;
  for (const range of ranges) {
    if (range[0] > end) break;
    end = Math.max(end, range[1]);
  }
  return end;
}

function normalizedOffset(value: number | undefined): number {
  return Math.max(0, Math.floor(Number.isFinite(value) ? Number(value) : 0));
}

export class DocumentReadSession {
  readonly fullReadMode: boolean;
  readonly chunkSize: number;
  private readonly documents = new Map<string, ReadableDocument>();
  private readonly ledgers = new Map<string, DocumentReadLedger>();
  private readonly pageNotes = new Map<string, StoredPageNotes>();
  private activeFilename?: string;
  private lastRead?: LastReadPage;

  constructor(documents: ReadableDocument[], options: DocumentReadSessionOptions = {}) {
    this.fullReadMode = options.fullReadMode ?? false;
    this.chunkSize = Math.max(1, Math.floor(options.chunkSize ?? DOCUMENT_READ_CHUNK_SIZE));
    for (const document of documents) {
      if (this.documents.has(document.filename)) {
        throw new Error(`文档文件名重复，无法安全建立全文读取账本：${document.filename}`);
      }
      const body = document.textContent ?? '';
      this.documents.set(document.filename, document);
      this.ledgers.set(document.filename, {
        filename: document.filename,
        totalChars: body.length,
        ranges: [],
        contiguousEnd: 0,
        eofSeen: body.length === 0,
        complete: body.length === 0,
      });
    }
  }

  get complete(): boolean {
    return [...this.ledgers.values()].every((ledger) => ledger.complete);
  }

  get documentCount(): number {
    return this.ledgers.size;
  }

  /**
   * 当前账本已经覆盖的去重字符数。
   * 只统计合并后的区间，重复读取同一页不会被当成进展。
   */
  get coveredChars(): number {
    return [...this.ledgers.values()].reduce((total, ledger) => (
      total + ledger.ranges.reduce((covered, [from, to]) => covered + to - from, 0)
    ), 0);
  }

  ledger(filename: string): DocumentReadLedger {
    const ledger = this.ledgers.get(filename);
    if (!ledger) throw new Error(`未找到文档：${filename}`);
    return {
      ...ledger,
      ranges: ledger.ranges.map((range) => [...range] as [number, number]),
    };
  }

  allLedgers(): DocumentReadLedger[] {
    return [...this.ledgers.keys()].map((filename) => this.ledger(filename));
  }

  read(filename: string, offset?: number, notes: DocumentReadNotes = {}): DocumentReadPage {
    const document = this.documents.get(filename);
    const ledger = this.ledgers.get(filename);
    if (!document || !ledger) throw new Error(`未找到文档或无正文：${filename}`);
    const body = document.textContent ?? '';
    const requestedOffset = normalizedOffset(offset);

    if (this.fullReadMode) {
      if (this.activeFilename && this.activeFilename !== filename) {
        const active = this.ledgers.get(this.activeFilename)!;
        if (!active.complete) {
          throw new DocumentReadOffsetError(filename, requestedOffset, active.contiguousEnd);
        }
      }
      const expectedOffset = ledger.contiguousEnd;
      if (requestedOffset !== expectedOffset) {
        throw new DocumentReadOffsetError(filename, requestedOffset, expectedOffset);
      }
      if (this.lastRead) {
        const summary = String(notes.previousPageSummary ?? '').trim();
        if (!summary) {
          throw new DocumentReadSummaryRequiredError(this.lastRead.filename, this.lastRead.offset);
        }
      }
    }

    if (this.lastRead && this.fullReadMode) {
      const summary = String(notes.previousPageSummary ?? '').trim().slice(0, 2_000);
      const evidence = (notes.previousPageEvidence ?? []).flatMap((item) => {
        const evidenceOffset = normalizedOffset(item.offset);
        const note = String(item.note ?? '').trim().slice(0, 500);
        if (!note || evidenceOffset < this.lastRead!.offset || evidenceOffset >= this.lastRead!.endOffset) return [];
        return [{ offset: evidenceOffset, note }];
      }).slice(0, 20);
      this.pageNotes.set(this.pageKey(this.lastRead), { summary, evidence });
    }

    const endOffset = Math.min(body.length, requestedOffset + this.chunkSize);
    const eof = endOffset >= body.length;
    ledger.ranges = mergeRanges(ledger.ranges, [requestedOffset, endOffset]);
    ledger.contiguousEnd = contiguousEnd(ledger.ranges);
    ledger.eofSeen ||= eof;
    ledger.complete = ledger.eofSeen && ledger.contiguousEnd >= ledger.totalChars
      && (ledger.ranges[0]?.[0] ?? 0) === 0;

    if (this.fullReadMode) {
      this.activeFilename = ledger.complete ? undefined : filename;
      this.lastRead = { filename, offset: requestedOffset, endOffset };
    }

    return {
      filename,
      offset: requestedOffset,
      endOffset,
      totalChars: body.length,
      nextOffset: eof ? null : endOffset,
      eof,
      content: body.slice(requestedOffset, endOffset),
    };
  }

  continuationPrompt(): string {
    if (this.complete) return '全文覆盖已经完成，请基于滚动摘要、章节索引和重要证据提交最终结果。';
    const active = this.activeFilename ? this.ledgers.get(this.activeFilename) : undefined;
    const next = active && !active.complete
      ? active
      : [...this.ledgers.values()].find((ledger) => !ledger.complete);
    if (!next) return '全文覆盖已经完成，请提交最终结果。';
    const summary = this.lastRead
      ? '调用时必须用 previousPageSummary 总结上一页，并用 previousPageEvidence 记录重要证据及其 offset。'
      : '';
    return [
      '后端账本判定全文尚未读完，当前不能提交或声称已经完整阅读。',
      `请继续调用 read_document：filename=${JSON.stringify(next.filename)}, offset=${next.contiguousEnd}。`,
      summary,
      this.incompleteReport(),
    ].filter(Boolean).join('\n');
  }

  incompleteReport(): string {
    const lines = this.allLedgers().filter((ledger) => !ledger.complete).map((ledger) => {
      const covered = ledger.ranges.reduce((total, range) => total + range[1] - range[0], 0);
      const gaps: Array<[number, number]> = [];
      let start = 0;
      for (const range of ledger.ranges) {
        if (range[0] > start) gaps.push([start, range[0]]);
        start = Math.max(start, range[1]);
      }
      if (start < ledger.totalChars) gaps.push([start, ledger.totalChars]);
      return `${ledger.filename} 已读取 ${covered} / ${ledger.totalChars} 字符，\n未读取区间为 ${gaps.map(([from, to]) => `${from}–${to}`).join('、') || '无（尚未观察到 EOF）'}。`;
    });
    return ['本次未完成全文读取：', ...lines].join('\n');
  }

  compactMessages<T extends unknown[]>(messages: T): T {
    const readIndexes = messages.flatMap((message: any, index) => (
      message?.role === 'toolResult' && message?.toolName === 'read_document' ? [index] : []
    ));
    const latestIndex = readIndexes.at(-1);
    return messages.map((message: any, index) => {
      if (index === latestIndex || message?.role !== 'toolResult' || message?.toolName !== 'read_document') {
        return message;
      }
      const filename = String(message.details?.filename ?? '未知文档');
      const offset = normalizedOffset(message.details?.offset);
      const endOffset = normalizedOffset(message.details?.endOffset);
      const notes = this.pageNotes.get(this.pageKey({ filename, offset, endOffset }));
      const compacted = {
        filename, offset, endOffset, compacted: true,
        summary: notes?.summary ?? '该页已连续读取；未提供额外摘要。',
        evidence: notes?.evidence ?? [],
      };
      return { ...message, content: [{ type: 'text', text: JSON.stringify(compacted) }] };
    }) as T;
  }

  private pageKey(page: LastReadPage): string {
    return `${page.filename}\u0000${page.offset}\u0000${page.endOffset}`;
  }
}

export class DocumentReadIncompleteError extends Error {
  constructor(readonly session: DocumentReadSession) {
    super(session.incompleteReport());
    this.name = 'DocumentReadIncompleteError';
  }
}

export const DEFAULT_DOCUMENT_READ_NO_PROGRESS_TURNS = 30;

export class DocumentReadStalledError extends DocumentReadIncompleteError {
  readonly code = 'DOCUMENT_READ_STALLED';

  constructor(
    session: DocumentReadSession,
    readonly noProgressTurns: number,
    readonly noProgressTurnLimit: number,
  ) {
    super(session);
    this.name = 'DocumentReadStalledError';
    this.message = [
      `全文读取连续 ${noProgressTurns} 个模型回合没有新增正文覆盖，已停止重复调用（保护阈值 ${noProgressTurnLimit}）。`,
      session.incompleteReport(),
    ].join('\n');
  }
}

export async function runAgentWithDocumentReadGate(input: {
  agent: any;
  prompt: string;
  session: DocumentReadSession;
  observe?: (event: unknown) => void;
  captured?: () => boolean;
  submitNudge?: string;
  missingSubmissionError?: string;
  targetToolName?: string;
  maxCaptureAttempts?: number;
  toolChoiceState?: StructuredToolChoiceState;
  onCaptureAttempt?: (diagnostics: AgentCaptureAttemptDiagnostics) => void | Promise<void>;
  noProgressTurnLimit?: number;
}): Promise<unknown[]> {
  const { runAgent, runAgentCaptured } = await import('./agent.js');
  const configuredNoProgressLimit = Number(input.noProgressTurnLimit ?? DEFAULT_DOCUMENT_READ_NO_PROGRESS_TURNS);
  const noProgressTurnLimit = Number.isFinite(configuredNoProgressLimit) && configuredNoProgressLimit > 0
    ? Math.floor(configuredNoProgressLimit)
    : DEFAULT_DOCUMENT_READ_NO_PROGRESS_TURNS;
  let messages: unknown[] = [];
  let prompt = input.prompt;
  let submissionNudges = 0;
  let noProgressTurns = 0;
  let coveredChars = input.session.coveredChars;
  let stalled = false;

  const observe = (event: unknown) => {
    input.observe?.(event);
    if ((event as any)?.type !== 'turn_end' || stalled || input.session.complete) return;
    const nextCoveredChars = input.session.coveredChars;
    if (nextCoveredChars > coveredChars) {
      noProgressTurns = 0;
    } else {
      noProgressTurns += 1;
    }
    coveredChars = nextCoveredChars;
    if (noProgressTurns >= noProgressTurnLimit) {
      stalled = true;
      try { input.agent.abort(); } catch { /* 让当前 run 自然收尾 */ }
    }
  };

  while (true) {
    const turnsBeforeRun = noProgressTurns;
    let turnsObserved = 0;
    const runObserve = (event: unknown) => {
      if ((event as any)?.type === 'turn_end') turnsObserved += 1;
      observe(event);
    };
    try {
      // 全文任务只由账本完成度和无进展保护终止，不继承通用 Agent 的回合上限。
      messages = await runAgent(input.agent, prompt, runObserve, { maxTurns: null });
    } catch (error) {
      if (stalled) throw new DocumentReadStalledError(input.session, noProgressTurns, noProgressTurnLimit);
      throw error;
    }
    if (stalled) throw new DocumentReadStalledError(input.session, noProgressTurns, noProgressTurnLimit);
    // 某些 provider/测试 double 不发 turn_end，但仍然返回；把这次调用算作一个无进展回合，
    // 否则 while (true) 会在没有任何可观察事件时永不终止。
    if (turnsObserved === 0 && !input.session.complete) {
      noProgressTurns = input.session.coveredChars > coveredChars ? 0 : turnsBeforeRun + 1;
      if (noProgressTurns >= noProgressTurnLimit) {
        throw new DocumentReadStalledError(input.session, noProgressTurns, noProgressTurnLimit);
      }
    }
    coveredChars = input.session.coveredChars;
    if (input.session.complete && (!input.captured || input.captured())) {
      return input.session.compactMessages(messages);
    }
    if (input.session.complete) {
      if (input.targetToolName && input.captured) {
        const capturedMessages = await runAgentCaptured(
          input.agent,
          input.submitNudge ?? '全文覆盖已经完成，请立即调用提交工具提交最终结果。',
          input.captured,
          input.submitNudge ?? '请立即调用提交工具提交最终结果。',
          input.observe,
          {
            maxCaptureAttempts: input.maxCaptureAttempts ?? 3,
            requireCapture: true,
            requiredToolName: input.targetToolName,
            toolChoiceState: input.toolChoiceState,
            captureDiagnostics: {
              targetToolName: input.targetToolName,
              onAttempt: input.onCaptureAttempt ?? (() => undefined),
            },
          },
        );
        return input.session.compactMessages(capturedMessages);
      }
      if (submissionNudges >= 1) {
        throw new Error(input.missingSubmissionError ?? '全文读取完成，但模型未按协议提交结构化结果');
      }
      submissionNudges += 1;
      prompt = input.submitNudge ?? '全文覆盖已经完成，请立即调用提交工具提交最终结果。';
    } else {
      prompt = input.session.continuationPrompt();
    }
  }
}
