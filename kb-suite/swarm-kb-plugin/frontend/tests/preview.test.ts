// Synthetic file names and text only.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { bboxStyle, blobType, focusOf, focusView, linesOf, previewKind, rangeCalls, textWindow } from '../src/preview.ts';

test('the kind of preview comes from the extension, then the content type', () => {
  assert.equal(previewKind('PDF'), 'pdf');
  assert.equal(previewKind('docx'), 'docx');
  assert.equal(previewKind('mov'), 'video');
  assert.equal(previewKind('m4a'), 'audio');
  assert.equal(previewKind('', 'image/png; charset=binary'), 'image');
  assert.equal(previewKind('bin', 'application/pdf'), 'pdf');
  assert.equal(previewKind('md', 'text/markdown'), 'none');
  assert.equal(previewKind('doc'), 'none');
});

test('a blob gets a real MIME type when the backend only says octet-stream', () => {
  assert.equal(blobType('mp4', 'application/octet-stream'), 'video/mp4');
  assert.equal(blobType('mp4', 'video/webm'), 'video/webm');
  assert.equal(blobType('xyz', null), 'application/octet-stream');
});

test('ranges are cut into calls of at most the given size', () => {
  assert.deepEqual(rangeCalls(0, 20, 8), [
    { offset: 0, length: 8 },
    { offset: 8, length: 8 },
    { offset: 16, length: 4 },
  ]);
  assert.deepEqual(rangeCalls(5, 5, 8), []);
});

test('a bbox in PDF points maps to a CSS box at the page scale, whichever corner comes first', () => {
  assert.deepEqual(bboxStyle([72, 140.5, 520, 188], 2), { left: 144, top: 281, width: 896, height: 95 });
  assert.deepEqual(bboxStyle([520, 188, 72, 140.5], 1)?.left, 72);
  assert.equal(bboxStyle([1, 2, 3], 1), null);
  assert.equal(bboxStyle(null, 1), null);
});

test('evidence becomes a focus: first page with its box, media time, and the line range', () => {
  const focus = focusOf({
    locators: [{ line_start: 12, line_end: 14 }, { page: 3, bbox: [1, 2, 3, 4] }, { page: 4 }, { time_start: 61.5 }],
    text: '  合成段落  ',
  });
  assert.deepEqual(focus, { lineStart: 12, lineEnd: 14, page: 3, bbox: [1, 2, 3, 4], time: 61.5, text: '合成段落' });
  assert.deepEqual(focusOf({ locators: [], line_start: 7 }), { lineStart: 7, lineEnd: undefined });
  assert.deepEqual(focusOf({}), {});
});

test('the original file is shown only where it can show the focus', () => {
  assert.equal(focusView({ page: 2 }, 'pdf', true), 'file');
  assert.equal(focusView({ page: 2 }, 'pdf', false), 'content');
  assert.equal(focusView({ page: 2 }, 'docx', true), 'content');
  assert.equal(focusView({ time: 3 }, 'video', true), 'file');
  assert.equal(focusView({ lineStart: 3 }, 'pdf', true), 'content');
});

test('a passage is found across line breaks and extra spaces', () => {
  const text = '# 标题\n\n第一段文字。\n第二段  文字，\n跨行继续。\n';
  assert.deepEqual(linesOf(text, '第二段 文字， 跨行继续。'), { from: 4, to: 5 });
  assert.deepEqual(linesOf(text, '第一段文字。'), { from: 3, to: 3 });
  assert.equal(linesOf(text, '不存在的句子'), null);
  assert.equal(linesOf(text, '   '), null);
});

test('a long text is windowed around the line to show, at line boundaries', () => {
  const lines = Array.from({ length: 1000 }, (_, i) => `line ${i + 1}`);
  const text = lines.join('\n');
  assert.deepEqual(textWindow('short', 100), { text: 'short', firstLine: 1, truncated: false });
  const head = textWindow(text, 200);
  assert.equal(head.firstLine, 1);
  assert.ok(head.text.length <= 200 && !head.text.endsWith('\n'));
  const around = textWindow(text, 300, 700);
  assert.ok(around.firstLine < 700 && around.firstLine > 650);
  const shown = around.text.split('\n');
  assert.equal(shown[0], `line ${around.firstLine}`);
  assert.ok(shown.includes('line 700'));
});
