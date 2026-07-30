const asSequence = (value) => {
  if (value === '' || value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : null;
};

const parseData = (text) => {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
};

export function normalizeProjectEvent(frame) {
  const data = parseData(frame.data || '');
  const source = data && typeof data === 'object' && !Array.isArray(data) ? data : {};
  const seq = asSequence(source.seq ?? source.sequence ?? frame.id);
  const frameType = frame.event && frame.event !== 'message' ? frame.event : '';

  return {
    id: String(frame.id || source.id || (seq ?? '')),
    seq,
    type: String(frameType || source.type || source.event || 'message'),
    projectId: String(source.projectId ?? source.project_id ?? ''),
    createdAt: String(source.createdAt ?? source.created_at ?? ''),
    payload: Object.prototype.hasOwnProperty.call(source, 'payload') ? source.payload : data,
    data,
    retry: frame.retry ?? null,
  };
}

/**
 * Creates a streaming SSE parser. push() accepts string or Uint8Array chunks and
 * returns only newly accepted events. Replayed events are ignored by seq.
 */
export function createProjectEventParser({ lastSeq = -1, onEvent } = {}) {
  const decoder = new TextDecoder();
  let buffer = '';
  let latestSeq = asSequence(lastSeq) ?? -1;
  let retry = null;
  let frame = { data: [], event: '', id: '' };

  const resetFrame = () => { frame = { data: [], event: '', id: '' }; };

  const dispatch = (accepted) => {
    if (!frame.data.length) {
      resetFrame();
      return;
    }
    const event = normalizeProjectEvent({
      data: frame.data.join('\n'),
      event: frame.event,
      id: frame.id,
      retry,
    });
    resetFrame();
    if (event.seq !== null && event.seq <= latestSeq) return;
    if (event.seq !== null) latestSeq = event.seq;
    accepted.push(event);
    onEvent?.(event);
  };

  const consumeLine = (line, accepted) => {
    if (line === '') {
      dispatch(accepted);
      return;
    }
    if (line.startsWith(':')) return;

    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'data') frame.data.push(value);
    if (field === 'event') frame.event = value;
    if (field === 'id' && !value.includes('\0')) frame.id = value;
    if (field === 'retry' && /^\d+$/.test(value)) retry = Number(value);
  };

  const consumeBuffer = (accepted, final = false) => {
    let start = 0;
    for (let index = 0; index < buffer.length; index += 1) {
      const char = buffer[index];
      if (char !== '\n' && char !== '\r') continue;
      if (char === '\r' && index === buffer.length - 1 && !final) break;
      consumeLine(buffer.slice(start, index), accepted);
      if (char === '\r' && buffer[index + 1] === '\n') index += 1;
      start = index + 1;
    }
    buffer = buffer.slice(start);
    if (final && buffer) {
      consumeLine(buffer, accepted);
      buffer = '';
    }
  };

  return {
    push(chunk) {
      const accepted = [];
      if (typeof chunk === 'string') buffer += chunk;
      else if (chunk) buffer += decoder.decode(chunk, { stream: true });
      consumeBuffer(accepted);
      return accepted;
    },
    flush() {
      const accepted = [];
      buffer += decoder.decode();
      consumeBuffer(accepted, true);
      dispatch(accepted);
      return accepted;
    },
    getLastSeq() {
      return latestSeq;
    },
    getRetry() {
      return retry;
    },
  };
}

export function mergeProjectEvents(previous, incoming, limit = 200) {
  const seenSequences = new Set();
  const seenIds = new Set();
  const merged = [];
  for (const event of [...(previous || []), ...(incoming || [])]) {
    const seq = asSequence(event?.seq);
    const id = event?.id == null ? '' : String(event.id);
    if (seq !== null && seenSequences.has(seq)) continue;
    if (seq === null && id && seenIds.has(id)) continue;
    if (seq !== null) seenSequences.add(seq);
    if (id) seenIds.add(id);
    merged.push(event);
  }
  merged.sort((left, right) => {
    const leftSeq = asSequence(left?.seq);
    const rightSeq = asSequence(right?.seq);
    if (leftSeq === null || rightSeq === null) return 0;
    return leftSeq - rightSeq;
  });
  return merged.slice(-Math.max(1, Number(limit) || 200));
}
