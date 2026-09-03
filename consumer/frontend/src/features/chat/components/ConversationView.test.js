import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const currentDir = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(currentDir, 'ConversationView.jsx'), 'utf8');
const styles = readFileSync(join(currentDir, '../chat.css'), 'utf8');

test('user messages expose a copy action for the original question text', () => {
  assert.match(
    source,
    /<div className="user-message-actions">[\s\S]*<CopyButton text=\{message\.content\} label="复制问题" \/>/,
  );
});

test('question copy action is aligned with the user message metadata', () => {
  assert.match(
    styles,
    /\.mwr \.msg\.u \.user-message-actions\s*\{[^}]*display:\s*flex[^}]*justify-content:\s*flex-end/,
  );
});
