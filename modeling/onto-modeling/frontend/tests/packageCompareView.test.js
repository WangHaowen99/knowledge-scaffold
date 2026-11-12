import assert from 'node:assert/strict';
import test from 'node:test';
import {
  canApplyReleaseRefresh,
  chooseInitialReleases,
  chooseOtherRelease,
  debounceValue,
  describeFormatChange,
  isFormatOnlyChange,
  mergeReleaseRefresh,
  packageIdentity,
  representativeChangesLabel,
  shouldClearSelectedFile,
  validPageForTotal,
} from '../src/lib/packageCompareView.js';

const releases = [
  { release_id: 'release-3', version: '3.0.0' },
  { release_id: 'release-2', version: '2.0.0' },
  { release_id: 'release-1', version: '1.0.0' },
];

test('row compare keeps the clicked release as the left source', () => {
  const sources = chooseInitialReleases(releases, 'release-1');
  assert.equal(sources.left.release_id, 'release-1');
  assert.equal(sources.right.release_id, 'release-3');
});

test('generic compare defaults to the latest two releases', () => {
  const sources = chooseInitialReleases(releases);
  assert.equal(sources.left.release_id, 'release-2');
  assert.equal(sources.right.release_id, 'release-3');
});

test('a full release refresh replaces a duplicate comparison side', () => {
  const right = chooseOtherRelease(releases, 'release-1', releases[2]);
  assert.equal(right.release_id, 'release-3');
});

test('a late release refresh never overwrites a source selected while it was pending', () => {
  const userSelected = { release_id: 'user-selected', version: 'custom' };
  const refreshed = mergeReleaseRefresh({
    releases,
    initialReleaseId: 'release-1',
    currentLeft: userSelected,
    currentRight: releases[0],
  });
  assert.equal(refreshed.left, userSelected);
  assert.equal(refreshed.right, releases[0]);
});

test('the first release refresh can still fill sources when the initial page had none', () => {
  const refreshed = mergeReleaseRefresh({ releases, initialReleaseId: 'release-1' });
  assert.equal(refreshed.left.release_id, 'release-1');
  assert.equal(refreshed.right.release_id, 'release-3');
});

test('a release refresh only applies to an unchanged release-mode source', () => {
  assert.equal(canApplyReleaseRefresh('release', 2, 2), true);
  assert.equal(canApplyReleaseRefresh('upload', 2, 2), false);
  assert.equal(canApplyReleaseRefresh('release', 3, 2), false);
});

test('empty filtering clears the selected file and its body', () => {
  assert.equal(shouldClearSelectedFile([]), true);
  assert.equal(shouldClearSelectedFile([{ path: 'manifest.yaml' }]), false);
});

test('a smaller comparison result moves an out-of-range page back into range', () => {
  assert.equal(validPageForTotal(3, 6), 1);
  assert.equal(validPageForTotal(4, 120), 3);
  assert.equal(validPageForTotal(1, 0), 1);
});

test('representative changes explain the displayed and total counts', () => {
  assert.equal(representativeChangesLabel(5, 11), '代表性变化（展示 5/11）');
});

test('published and uploaded sources expose their real package identity', () => {
  assert.equal(packageIdentity({ ontology_id: 'ontology-a' }), 'ontology-a');
  assert.equal(packageIdentity({ hub_package_id: 'legacy-a' }), 'legacy-a');
  assert.equal(packageIdentity({ package_id: 'uploaded-a' }), 'uploaded-a');
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
