export function chooseInitialReleases(releases = [], initialReleaseId = '') {
  const selected = releases.find((item) => item.release_id === initialReleaseId);
  const left = selected || releases[1] || releases[0] || null;
  const right = releases.find((item) => item.release_id !== left?.release_id) || left;
  return { left, right };
}

export function chooseOtherRelease(releases = [], selectedReleaseId = '', current = null) {
  if (current && current.release_id !== selectedReleaseId) return current;
  return releases.find((item) => item.release_id !== selectedReleaseId)
    || releases.find((item) => item.release_id === selectedReleaseId)
    || null;
}

export function mergeReleaseRefresh({ releases = [], initialReleaseId = '', currentLeft = null, currentRight = null }) {
  const selected = releases.find((item) => item.release_id === initialReleaseId);
  return {
    left: currentLeft || selected || releases[1] || releases[0] || null,
    right: currentRight || chooseOtherRelease(releases, selected?.release_id || (releases[1] || releases[0])?.release_id || '', null),
  };
}

export function canApplyReleaseRefresh(mode, currentGeneration, requestedGeneration) {
  return mode === 'release' && currentGeneration === requestedGeneration;
}

export function shouldClearSelectedFile(items = []) {
  return items.length === 0;
}

export function validPageForTotal(page, total, limit = 50) {
  const pageCount = Math.max(1, Math.ceil(Math.max(0, total) / limit));
  return Math.min(Math.max(1, page), pageCount);
}

export function representativeChangesLabel(displayed, total) {
  return `代表性变化（展示 ${displayed}/${total}）`;
}

export function debounceValue(callback, delay = 200) {
  let timer = null;
  return {
    schedule(value) {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => { timer = null; callback(value); }, delay);
    },
    cancel() { if (timer) clearTimeout(timer); timer = null; },
  };
}

const encodingLabel = (encoding) => ({
  'utf-8-sig': 'UTF-8（含 BOM）',
  'utf-8': 'UTF-8',
  'utf-16': 'UTF-16',
}[encoding] || encoding || '未知编码');

const normalizedText = (value = '') => value.replace(/\r\n?/g, '\n');

const lineEnding = (value = '') => {
  if (/\r\n/.test(value)) return /(^|[^\r])\n/.test(value) ? '混合换行符' : 'CRLF';
  if (/\r/.test(value)) return 'CR';
  return /\n/.test(value) ? 'LF' : '无换行符';
};

export function isFormatOnlyChange({ status, chunkCount, left = '', right = '' }) {
  return status === 'modified' && chunkCount === 0 && normalizedText(left) === normalizedText(right);
}

export function describeFormatChange({ left = '', right = '', leftEncoding, rightEncoding, leftBytes, rightBytes }) {
  if (leftEncoding !== rightEncoding) {
    return `文件编码由 ${encodingLabel(leftEncoding)} 变为 ${encodingLabel(rightEncoding)}`;
  }
  const leftLineEnding = lineEnding(left);
  const rightLineEnding = lineEnding(right);
  if (leftLineEnding !== rightLineEnding) {
    return `文件换行符由 ${leftLineEnding} 变为 ${rightLineEnding}`;
  }
  return `文件正文相同，但字节格式发生变化（${leftBytes ?? 0} → ${rightBytes ?? 0} bytes）`;
}
export const packageIdentity = (value) => (
  value?.package_id || value?.manifest_id || value?.ontology_id || value?.hub_package_id || ''
);
