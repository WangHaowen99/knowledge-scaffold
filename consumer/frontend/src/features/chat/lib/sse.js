export const takeSSEFrames = (buffer, { done = false } = {}) => {
  const parts = String(buffer || '').split(/\r?\n\r?\n/);
  const trailing = parts.pop() || '';
  if (done && trailing.trim()) parts.push(trailing);
  return {
    frames: parts.filter((part) => part.trim()),
    remainder: done ? '' : trailing,
  };
};

export const parseSSEMessage = (chunk) => {
  let event = '';
  let id = '';
  const dataLines = [];

  for (const line of String(chunk || '').split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    if (line.startsWith('event:')) event = line.replace(/^event:\s*/, '');
    else if (line.startsWith('id:')) id = line.replace(/^id:\s*/, '');
    else if (line.startsWith('data:')) dataLines.push(line.replace(/^data:\s*/, ''));
  }

  if (dataLines.length === 0) return null;
  const rawData = dataLines.join('\n');
  if (rawData === '[DONE]') return null;
  try {
    return { event: event || 'message', id, data: JSON.parse(rawData) };
  } catch {
    return null;
  }
};
