export const AI_CONSTRUCTION_TEXT_BLOCK_MAX_LENGTH = 10_000;

function safeCodeUnitBoundary(text: string, index: number): number {
  if (index <= 0 || index >= text.length) return index;
  const previous = text.charCodeAt(index - 1);
  const next = text.charCodeAt(index);
  return previous >= 0xd800 && previous <= 0xdbff && next >= 0xdc00 && next <= 0xdfff
    ? index - 1
    : index;
}

export function splitAiConstructionText(text: string): string[] {
  const chunks: string[] = [];
  let remaining = text.trim();
  while (remaining.length > AI_CONSTRUCTION_TEXT_BLOCK_MAX_LENGTH) {
    const boundary = safeCodeUnitBoundary(remaining, AI_CONSTRUCTION_TEXT_BLOCK_MAX_LENGTH);
    chunks.push(remaining.slice(0, boundary));
    remaining = remaining.slice(boundary);
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}
