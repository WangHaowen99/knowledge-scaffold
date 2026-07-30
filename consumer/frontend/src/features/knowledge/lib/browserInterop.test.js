import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createNativeFilePickerController,
  describeZipDownload,
  MARKDOWN_UPLOAD_CANCEL_NOTICE,
  resolveKnowledgeWorkbenchNotices,
  resolveZipDownloadFileName,
  startBlobDownload,
} from './browserInterop.js';

test('native file picker controller treats an empty picker close as a cancel', () => {
  const events = [];
  const picker = createNativeFilePickerController({
    onSelect: (file) => events.push(['select', file.name]),
    onCancel: (reason) => events.push(['cancel', reason]),
  });

  picker.begin();
  assert.equal(picker.isPending(), true);
  assert.equal(picker.handleWindowFocus(() => []), true);
  assert.deepEqual(events, [['cancel', 'fallback']]);
  assert.equal(picker.isPending(), false);

  picker.begin();
  assert.equal(picker.handleChange({ name: 'demo.md' }), 'selected');
  assert.deepEqual(events.at(-1), ['select', 'demo.md']);
  assert.equal(picker.isPending(), false);
});

test('native file picker controller settles native and empty changes once', () => {
  const events = [];
  const picker = createNativeFilePickerController({
    onCancel: (reason) => events.push(['cancel', reason]),
  });

  picker.begin();
  assert.equal(picker.handleNativeCancel(), true);
  assert.equal(picker.handleNativeCancel(), false);
  assert.deepEqual(events, [['cancel', 'native']]);

  picker.begin();
  assert.equal(picker.handleChange(null), 'cancelled');
  assert.equal(picker.handleWindowFocus(() => []), false);
  assert.deepEqual(events, [['cancel', 'native'], ['cancel', 'empty']]);
});

test('native file picker controller updates handlers without resetting a pending selection', () => {
  const events = [];
  const picker = createNativeFilePickerController({
    onSelect: () => events.push('stale'),
  });

  picker.begin();
  picker.setHandlers({
    onSelect: (file) => events.push(file.name),
  });

  assert.equal(picker.isPending(), true);
  assert.equal(picker.handleChange({ name: 'latest.md' }), 'selected');
  assert.deepEqual(events, ['latest.md']);
});

test('zip download helpers keep file names and notices observable without claiming completion', () => {
  assert.equal(
    resolveZipDownloadFileName('demo', '1.0.0', 'attachment; filename*=UTF-8\'\'demo%20bundle.zip'),
    'demo bundle.zip',
  );
  assert.equal(
    resolveZipDownloadFileName(
      'demo',
      '1.0.0',
      'attachment; filename="fallback.zip"; filename*=UTF-8\'\'actual%20bundle.zip',
    ),
    'actual bundle.zip',
  );
  assert.equal(
    resolveZipDownloadFileName('demo', '1.0.0', 'attachment; filename*=UTF-8\'zh-CN\'中文%20包.zip'),
    '中文 包.zip',
  );
  assert.equal(
    resolveZipDownloadFileName('demo', '1.0.0', 'attachment; filename="fallback.zip"'),
    'fallback.zip',
  );
  assert.equal(resolveZipDownloadFileName('demo', '1.0.0', ''), 'demo-1.0.0.zip');
  assert.equal(describeZipDownload('demo.zip'), '已发起 ZIP 下载：demo.zip');
});

test('workbench promotes Markdown cancellation and ZIP initiation without duplicating inline notices', () => {
  assert.deepEqual(
    resolveKnowledgeWorkbenchNotices(MARKDOWN_UPLOAD_CANCEL_NOTICE, ''),
    { inlineMessage: '', toastMessage: MARKDOWN_UPLOAD_CANCEL_NOTICE, toastTone: 'info' },
  );
  assert.deepEqual(
    resolveKnowledgeWorkbenchNotices('', describeZipDownload('demo.zip')),
    { inlineMessage: '', toastMessage: '已发起 ZIP 下载：demo.zip', toastTone: 'success' },
  );
  assert.deepEqual(
    resolveKnowledgeWorkbenchNotices('文件已保存到当前激活版本', ''),
    { inlineMessage: '文件已保存到当前激活版本', toastMessage: '', toastTone: 'info' },
  );
});

test('blob download helper clicks a temporary link and defers object URL revocation', () => {
  const appended = [];
  const removed = [];
  const clicks = [];
  const revoked = [];
  const scheduled = [];
  const fakeLink = {
    parentNode: null,
    click: () => { clicks.push('clicked'); },
  };
  const fakeDocument = {
    body: {
      appendChild(node) {
        appended.push(node);
        node.parentNode = this;
      },
      removeChild(node) {
        removed.push(node);
        node.parentNode = null;
      },
    },
    createElement: (tag) => {
      assert.equal(tag, 'a');
      return fakeLink;
    },
  };
  const fakeUrlApi = {
    createObjectURL: () => 'blob:demo',
    revokeObjectURL: (url) => revoked.push(url),
  };

  const result = startBlobDownload({}, 'demo.zip', {
    doc: fakeDocument,
    urlApi: fakeUrlApi,
    schedule: (fn) => {
      scheduled.push(fn);
      return 1;
    },
  });

  assert.equal(result.fileName, 'demo.zip');
  assert.equal(clicks.length, 1);
  assert.equal(appended.length, 1);
  assert.equal(removed.length, 1);
  assert.deepEqual(revoked, []);
  scheduled[0]();
  assert.deepEqual(revoked, ['blob:demo']);
});
