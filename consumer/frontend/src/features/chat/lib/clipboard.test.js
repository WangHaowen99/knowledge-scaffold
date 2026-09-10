import assert from 'node:assert/strict';
import test from 'node:test';

import { writeClipboardText } from './clipboard.js';

test('clipboard API is preferred when available', async () => {
  let copied = '';
  const result = await writeClipboardText('answer', {
    navigatorRef: {
      clipboard: {
        writeText: async (value) => {
          copied = value;
        },
      },
    },
    documentRef: null,
  });

  assert.deepEqual(result, { ok: true, method: 'clipboard', error: '' });
  assert.equal(copied, 'answer');
});

test('selection fallback cleans up its temporary textarea', async () => {
  const body = {
    child: null,
    appendChild(node) {
      this.child = node;
      node.parentNode = this;
    },
    removeChild(node) {
      if (this.child === node) {
        this.child = null;
      }
      node.parentNode = null;
    },
  };
  const textarea = {
    style: {},
    setAttribute() {},
    focus() {},
    select() {},
    parentNode: null,
  };
  const result = await writeClipboardText('fallback', {
    navigatorRef: {
      clipboard: {
        writeText: async () => {
          throw new Error('denied');
        },
      },
    },
    documentRef: {
      body,
      createElement: () => textarea,
      execCommand: () => true,
    },
  });

  assert.deepEqual(result, { ok: true, method: 'selection', error: '' });
  assert.equal(textarea.value, 'fallback');
  assert.equal(body.child, null);
});

test('copy failure returns a visible user-facing error', async () => {
  const result = await writeClipboardText('answer', {
    navigatorRef: {},
    documentRef: null,
  });

  assert.equal(result.ok, false);
  assert.match(result.error, /手动选择/);
});
