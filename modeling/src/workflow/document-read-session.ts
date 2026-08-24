export const DOCUMENT_READ_CHUNK_SIZE = 6_000;

export interface ReadableDocument {
  id?: string;
  filename: string;
  textContent?: string;
  revisionFingerprint?: string;
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

/**
 * 局部原文查询的轻量分页器。
 *
 * 全文分析不使用此函数；全文覆盖统一由 material-analysis-map-reduce 的动态分段、
 * 持久化检查点和覆盖校验完成。这里仅服务聊天中的章节/条款按需核对。
 */
export function readDocumentPage(
  document: ReadableDocument,
  offset = 0,
  chunkSize = DOCUMENT_READ_CHUNK_SIZE,
): DocumentReadPage {
  const body = document.textContent ?? '';
  const normalizedOffset = Math.max(0, Math.min(body.length, Math.floor(Number.isFinite(offset) ? offset : 0)));
  const normalizedSize = Math.max(1, Math.floor(Number.isFinite(chunkSize) ? chunkSize : DOCUMENT_READ_CHUNK_SIZE));
  const endOffset = Math.min(body.length, normalizedOffset + normalizedSize);
  const eof = endOffset >= body.length;
  return {
    filename: document.filename,
    offset: normalizedOffset,
    endOffset,
    totalChars: body.length,
    nextOffset: eof ? null : endOffset,
    eof,
    content: body.slice(normalizedOffset, endOffset),
  };
}
