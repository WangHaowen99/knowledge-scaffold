// 知识包版本对比（契约 PC-01～PC-08）的纯函数：来源选项与默认两侧、文件筛选与分页合并、正文差异怎么展示。
// 来源有两种：发布记录里的版本（{kind:'release', version}）和这次上传的 ZIP（{kind:'upload', input_id}）。
import { formatBytes } from '../../api/errors.js';

export const STATUS = {
  added: ['新增', 'teal'], deleted: ['删除', 'red'], modified: ['修改', 'amber'], unchanged: ['未变化', 'gray'],
};
export const CATEGORY = {
  objects: '对象', operations: '操作', rules: '规则', metrics: '指标', scripts: '脚本',
  mappings: '数据映射', indexes: '索引', metadata: '包元数据', other: '其他',
};
export const UPLOAD = '__upload__';
const CHANGED = ['added', 'deleted', 'modified'];

export const keyOf = (s) => (!s ? '' : s.kind === 'upload' ? `upload:${s.input_id}` : `release:${s.version}`);
export const refOf = (s) => (s.kind === 'upload' ? { kind: 'upload', input_id: s.input_id } : { kind: 'release', version: s.version });
export const sameSource = (a, b) => Boolean(a && b && keyOf(a) === keyOf(b));

/** 发布记录按 version_label（v0.13）显示；同一个界面版本号发过多次时补上包版本号区分 */
export function releaseLabel(r, releases = []) {
  const label = r.version_label || r.version;
  return releases.filter((x) => (x.version_label || x.version) === label).length > 1 ? `${label} · ${r.version}` : label;
}

export const sourceLabel = (s, releases = []) => (!s ? '' : s.kind === 'upload' ? s.filename || s.input_id : releaseLabel(s, releases));

export function sourceOptions(releases = [], uploads = []) {
  return [
    ...releases.map((r) => ({ value: keyOf(r), label: releaseLabel(r, releases), icon: 'tag' })),
    ...uploads.map((u) => ({ value: keyOf(u), label: u.filename || u.input_id, icon: 'archive' })),
    { value: UPLOAD, label: '上传 ZIP…', icon: 'upload' },
  ];
}

export const asRelease = (r) => ({ ...r, kind: 'release' });

/** 默认两侧：从某一行打开时左边是那一版、右边是最新的另一版（点的就是最新一版时和上一版比）；否则是最近两版。旧的总在左 */
export function chooseInitialReleases(releases = [], initialVersion = '') {
  const selected = releases.find((r) => r.version === initialVersion);
  if (selected && selected === releases[0] && releases[1]) return { left: releases[1], right: selected };
  const left = selected || releases[1] || releases[0] || null;
  const right = releases.find((r) => r.version !== left?.version) || left;
  return { left, right };
}

/** 选另一侧：当前那侧和刚选的不同就留着，否则换成别的版本 */
export function chooseOtherRelease(releases = [], selectedVersion = '', current = null) {
  if (current && current.version !== selectedVersion) return current;
  return releases.find((r) => r.version !== selectedVersion) || releases.find((r) => r.version === selectedVersion) || null;
}

/** 发布记录晚到：只填还空着的一侧，加载期间用户已经选过的不覆盖 */
export function fillSources({ releases = [], initialVersion = '', left = null, right = null }) {
  const initial = chooseInitialReleases(releases, initialVersion);
  const nextLeft = left || initial.left;
  return { left: nextLeft, right: right || chooseOtherRelease(releases, nextLeft?.version || '', null) };
}

/** 文件状态页签：有变化（默认）、四种状态，各带数量 */
export function statusTabs(summary) {
  const files = summary?.files || {};
  const changed = CHANGED.reduce((n, k) => n + (files[k] || 0), 0);
  return [['changed', '有变化', changed], ...Object.entries(STATUS).map(([k, [t]]) => [k, t, files[k] || 0])];
}

/** 类别下拉：只列包里有的类别，数量是有变化的文件数 */
export function categoryOptions(summary) {
  const cats = summary?.categories || {};
  return [{ value: '', label: '全部类别' }, ...Object.entries(CATEGORY).filter(([k]) => cats[k]).map(([k, t]) => {
    const n = CHANGED.reduce((sum, s) => sum + (cats[k][s] || 0), 0);
    return { value: k, label: n ? `${t} ${n}` : t };
  })];
}

/** 加载更多：按路径去重拼上下一页 */
export function appendPage(prev, page) {
  const seen = new Set((prev?.items || []).map((x) => x.path));
  return {
    items: [...(prev?.items || []), ...(page.items || []).filter((x) => !seen.has(x.path))],
    next_cursor: page.next_cursor || null,
    total: page.total ?? prev?.total ?? 0,
  };
}

export function shouldClearSelectedFile(items = []) {
  return items.length === 0;
}

const NO_TEXT = ['binary', 'too_large', 'complex_text'];

/** 两侧都是正文（或一侧没有这个文件）时逐行对比，否则各自显示原因 */
export function diffMode(diff) {
  if (!diff) return 'none';
  return [diff.left, diff.right].some((x) => x && NO_TEXT.includes(x.encoding)) ? 'panes' : 'merge';
}

/** 不能逐行对比的一侧显示什么 */
export function paneNote(side) {
  if (!side) return '无此文件';
  if (side.encoding === 'binary') return `二进制 · ${formatBytes(side.bytes)}`;
  if (NO_TEXT.includes(side.encoding)) return `${side.reason || '文件过大'} · ${formatBytes(side.bytes)}`;
  return '';
}

/** 失败结果：两侧不是同一个知识包时可以确认后再比 */
export const isCrossPackage = (result) => result?.status === 'failed' && result.error_code === 'compare_cross_package';

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

/** 请求标识：同一次点击的网络重试落到同一个任务（PC-04 的 request_id、PC-01 的 X-Compare-Upload-Id） */
export function requestId() {
  const raw = globalThis.crypto?.randomUUID?.() || `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return raw.replaceAll('-', '');
}
