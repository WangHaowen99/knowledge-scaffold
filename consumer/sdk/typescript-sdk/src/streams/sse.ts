// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
export interface SseEventMessage {
  event: string;
  data: string;
  json?: unknown;
  id?: string;
  retry?: number;
}

export interface AbortableAsyncIterable<T> extends AsyncIterable<T> {
  abort(reason?: string): void;
}

const safeJsonParse = (value: string): unknown => {
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
};

const createAbortableAsyncIterable = <T>(
  iteratorFactory: () => AsyncIterator<T>,
  abort: (reason?: string) => void,
): AbortableAsyncIterable<T> => ({
  [Symbol.asyncIterator]: iteratorFactory,
  abort,
});

export const streamSse = (
  response: Response,
  controller?: AbortController,
): AbortableAsyncIterable<SseEventMessage> => {
  if (!response.body) {
    throw new Error('SSE response body is empty');
  }

  const iteratorFactory = async function* (): AsyncGenerator<SseEventMessage> {
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();

    let buffer = '';
    let currentEvent = 'message';
    let currentId: string | undefined;
    let currentRetry: number | undefined;
    let dataLines: string[] = [];

    const emitCurrentEvent = (): SseEventMessage | null => {
      if (dataLines.length === 0 && !currentId && currentRetry === undefined && currentEvent === 'message') {
        return null;
      }

      const data = dataLines.join('\n');
      const message: SseEventMessage = {
        event: currentEvent || 'message',
        data,
        json: data ? safeJsonParse(data) : undefined,
        id: currentId,
        retry: currentRetry,
      };

      currentEvent = 'message';
      currentId = undefined;
      currentRetry = undefined;
      dataLines = [];

      return message;
    };

    const processLine = (rawLine: string): SseEventMessage | null => {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
      if (!line) {
        return emitCurrentEvent();
      }

      if (line.startsWith(':')) {
        return null;
      }

      const separatorIndex = line.indexOf(':');
      const field = separatorIndex === -1 ? line : line.slice(0, separatorIndex);
      let value = separatorIndex === -1 ? '' : line.slice(separatorIndex + 1);
      if (value.startsWith(' ')) {
        value = value.slice(1);
      }

      switch (field) {
        case 'event':
          currentEvent = value || 'message';
          break;
        case 'data':
          dataLines.push(value);
          break;
        case 'id':
          currentId = value;
          break;
        case 'retry': {
          const parsed = Number.parseInt(value, 10);
          if (!Number.isNaN(parsed)) {
            currentRetry = parsed;
          }
          break;
        }
        default:
          break;
      }
      return null;
    };

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          buffer += decoder.decode();
          if (buffer) {
            const trailing = processLine(buffer);
            if (trailing) {
              yield trailing;
            }
            buffer = '';
          }
          const finalEvent = emitCurrentEvent();
          if (finalEvent) {
            yield finalEvent;
          }
          return;
        }

        buffer += decoder.decode(value, { stream: true });
        let lineBreakIndex = buffer.indexOf('\n');
        while (lineBreakIndex !== -1) {
          const line = buffer.slice(0, lineBreakIndex);
          buffer = buffer.slice(lineBreakIndex + 1);
          const message = processLine(line);
          if (message) {
            yield message;
          }
          lineBreakIndex = buffer.indexOf('\n');
        }
      }
    } finally {
      reader.releaseLock();
    }
  };

  return createAbortableAsyncIterable(iteratorFactory, (reason?: string) => {
    controller?.abort(reason);
  });
};

export const mapAbortableStream = <TInput, TOutput>(
  source: AbortableAsyncIterable<TInput>,
  mapValue: (value: TInput) => TOutput | Promise<TOutput>,
): AbortableAsyncIterable<TOutput> => {
  const iteratorFactory = async function* (): AsyncGenerator<TOutput> {
    for await (const value of source) {
      yield await mapValue(value);
    }
  };

  return createAbortableAsyncIterable(iteratorFactory, (reason?: string) => {
    source.abort(reason);
  });
};
