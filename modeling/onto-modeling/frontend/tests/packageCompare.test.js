// 知识包版本对比的纯函数（pages/publish/packageCompareModel.js）。自 develop 的 packageCompareView.test.js 移植：
// 发布版本按 version 标识（develop 是 Hub release_id）；分页改成游标「更多」、跨包确认改由服务端失败码给出，相应用例换成新行为。
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  UPLOAD,
  appendPage,
  categoryOptions,
  chooseInitialReleases,
  chooseOtherRelease,
  debounceValue,
  describeFormatChange,
  diffMode,
  fillSources,
  isCrossPackage,
  isFormatOnlyChange,
  keyOf,
  paneNote,
  refOf,
  releaseLabel,
  sameSource,
  shouldClearSelectedFile,
  sourceOptions,
  statusTabs,
} from '../src/pages/publish/packageCompareModel.js';

const releases = [
  { kind: 'release', version: '0.3.0', version_label: 'v3' },
  { kind: 'release', version: '0.2.0', version_label: 'v2' },
  { kind: 'release', version: '0.1.0', version_label: 'v1' },
];

test('row compare keeps the clicked release as the left source', () => {
  const sources = chooseInitialReleases(releases, '0.1.0');
  assert.equal(sources.left.version, '0.1.0');
  assert.equal(sources.right.version, '0.3.0');
});

test('row compare from the newest release compares it with the previous one, older on the left', () => {
  const sources = chooseInitialReleases(releases, '0.3.0');
  assert.equal(sources.left.version, '0.2.0');
  assert.equal(sources.right.version, '0.3.0');
  const filled = fillSources({ releases, initialVersion: '0.3.0' });
  assert.equal(filled.left.version, '0.2.0');
  assert.equal(filled.right.version, '0.3.0');
});

test('generic compare defaults to the latest two releases', () => {
  const sources = chooseInitialReleases(releases);
  assert.equal(sources.left.version, '0.2.0');
  assert.equal(sources.right.version, '0.3.0');
});

test('a full release refresh replaces a duplicate comparison side', () => {
  const right = chooseOtherRelease(releases, '0.1.0', releases[2]);
  assert.equal(right.version, '0.3.0');
});

test('a late release list never overwrites a source selected while it was pending', () => {
  const userSelected = { kind: 'upload', input_id: 'cmpin_user' };
  const filled = fillSources({ releases, initialVersion: '0.1.0', left: userSelected, right: releases[0] });
  assert.equal(filled.left, userSelected);
  assert.equal(filled.right, releases[0]);
});

test('the release list fills empty sides, the other side differing from the left', () => {
  const filled = fillSources({ releases, initialVersion: '0.1.0' });
  assert.equal(filled.left.version, '0.1.0');
  assert.equal(filled.right.version, '0.3.0');
  const onlyRight = fillSources({ releases, left: releases[0] });
  assert.equal(onlyRight.left, releases[0]);
  assert.equal(onlyRight.right.version, '0.2.0');
});

test('empty filtering clears the selected file and its body', () => {
  assert.equal(shouldClearSelectedFile([]), true);
  assert.equal(shouldClearSelectedFile([{ path: 'manifest.yaml' }]), false);
});

test('sources map to the PC-04 references and stable keys', () => {
  assert.deepEqual(refOf(releases[0]), { kind: 'release', version: '0.3.0' });
  assert.deepEqual(refOf({ kind: 'upload', input_id: 'cmpin_a', filename: 'a.zip' }), { kind: 'upload', input_id: 'cmpin_a' });
  assert.equal(keyOf(releases[0]), 'release:0.3.0');
  assert.equal(sameSource(releases[0], { ...releases[0] }), true);
  assert.equal(sameSource(releases[0], releases[1]), false);
});

test('source options list releases, this session uploads and the upload entry', () => {
  const options = sourceOptions(releases, [{ kind: 'upload', input_id: 'cmpin_a', filename: 'local.zip' }]);
  assert.deepEqual(options.map((o) => o.label), ['v3', 'v2', 'v1', 'local.zip', '上传 ZIP…']);
  assert.equal(options.at(-1).value, UPLOAD);
});

test('duplicated version labels fall back to the package version', () => {
  const twice = [{ version: '0.2.1', version_label: 'v2' }, { version: '0.2.0', version_label: 'v2' }];
  assert.equal(releaseLabel(twice[0], twice), 'v2 · 0.2.1');
  assert.equal(releaseLabel(releases[0], releases), 'v3');
});

test('status tabs count changed files first', () => {
  const tabs = statusTabs({ files: { added: 1, deleted: 2, modified: 3, unchanged: 4 } });
  assert.deepEqual(tabs[0], ['changed', '有变化', 6]);
  assert.deepEqual(tabs.map((t) => t[0]), ['changed', 'added', 'deleted', 'modified', 'unchanged']);
});

test('category options only list present categories with their changed counts', () => {
  const options = categoryOptions({ categories: {
    objects: { added: 1, deleted: 0, modified: 2, unchanged: 5 },
    metadata: { added: 0, deleted: 0, modified: 0, unchanged: 1 },
  } });
  assert.deepEqual(options.map((o) => o.label), ['全部类别', '对象 3', '包元数据']);
});

test('load more appends the next cursor page without duplicates', () => {
  const first = appendPage(undefined, { items: [{ path: 'a' }, { path: 'b' }], next_cursor: 'c1', total: 3 });
  const next = appendPage(first, { items: [{ path: 'b' }, { path: 'c' }], next_cursor: null, total: 3 });
  assert.deepEqual(next.items.map((x) => x.path), ['a', 'b', 'c']);
  assert.equal(next.next_cursor, null);
  assert.equal(next.total, 3);
});

test('text on both sides (or a missing side) uses the line diff, otherwise each side explains itself', () => {
  assert.equal(diffMode({ left: { text: 'a', encoding: 'utf-8' }, right: null }), 'merge');
  assert.equal(diffMode({ left: { text: null, encoding: 'binary', bytes: 2048 }, right: { text: 'a', encoding: 'utf-8' } }), 'panes');
  assert.equal(paneNote(null), '无此文件');
  assert.equal(paneNote({ encoding: 'binary', bytes: 2048 }), '二进制 · 2 KB');
  assert.equal(paneNote({ encoding: 'too_large', bytes: 10, reason: '文件超过正文差异展示限制' }), '文件超过正文差异展示限制 · 10 B');
  assert.equal(paneNote({ encoding: 'utf-8', text: 'a' }), '');
});

test('a cross-package failure can be confirmed and retried', () => {
  assert.equal(isCrossPackage({ status: 'failed', error_code: 'compare_cross_package' }), true);
  assert.equal(isCrossPackage({ status: 'failed', error_code: 'compare_timeout' }), false);
  assert.equal(isCrossPackage({ status: 'ready' }), false);
});

test('format-only changes explain BOM and byte-level differences', () => {
  assert.equal(describeFormatChange({ leftEncoding: 'utf-8-sig', rightEncoding: 'utf-8' }),
    '文件编码由 UTF-8（含 BOM） 变为 UTF-8');
  assert.match(describeFormatChange({ leftEncoding: 'utf-8', rightEncoding: 'utf-8', leftBytes: 10, rightBytes: 12 }), /10 → 12 bytes/);
});

test('LF and CRLF differences are treated and explained as format-only changes', () => {
  assert.equal(isFormatOnlyChange({ status: 'modified', chunkCount: 0, left: 'a\r\nb\r\n', right: 'a\nb\n' }), true);
  assert.equal(describeFormatChange({
    left: 'a\r\nb\r\n', right: 'a\nb\n', leftEncoding: 'utf-8', rightEncoding: 'utf-8',
  }), '文件换行符由 CRLF 变为 LF');
});

test('real text changes are not presented as format-only changes', () => {
  assert.equal(isFormatOnlyChange({ status: 'modified', chunkCount: 0, left: 'a\n', right: 'b\n' }), false);
});

test('search input is debounced and only applies the latest value', async () => {
  const values = [];
  const debounced = debounceValue((value) => values.push(value), 1);
  debounced.schedule('a');
  debounced.schedule('ab');
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(values, ['ab']);
});

test('cancelling a pending search prevents it from restoring an old filter', async () => {
  const values = [];
  const debounced = debounceValue((value) => values.push(value), 1);
  debounced.schedule('old-query');
  debounced.cancel();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(values, []);
});
