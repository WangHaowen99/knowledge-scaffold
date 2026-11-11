// 原始资料「路径#锚点」的解析、位置文案与预览方式；锚点写法与工具箱 source_extract 一致：
// 文本 L12；Word p:12、table:1/row:2/cell:3；Excel sheet:规则!B3。

const TEXT_EXTS = ['md', 'markdown', 'txt', 'csv', 'json', 'yaml', 'yml'];
const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'];
const DOWNLOAD_EXTS = ['doc', 'xls', 'ppt', 'pptx', 'zip', 'rar', '7z'];
const KNOWN_EXTS = [
  ...TEXT_EXTS, ...IMAGE_EXTS, ...DOWNLOAD_EXTS,
  'docx', 'xlsx', 'xlsm', 'pdf', 'drawio', 'dio', 'mmd', 'html', 'py',
];

// 行内代码里的资料路径：resource/ 开头、已知扩展名，可带 #锚点
const SOURCE_REF_RE = new RegExp(
  `^(resource\\/[^\\s\`#]+\\.(?:${KNOWN_EXTS.join('|')}))(?:#(\\S+))?$`,
  'i',
);

export function extensionOf(path) {
  const name = String(path || '').split('/').pop();
  return name.includes('.') ? name.split('.').pop().toLowerCase() : '';
}

export function parseSourceRef(text) {
  const match = SOURCE_REF_RE.exec(String(text || '').trim());
  if (!match) return null;
  return { path: match[1], anchor: match[2] || '' };
}

// 预览方式：segments 走分段预览，excel 走表格预览，其余按浏览器能力打开或下载
export function previewKind(path, anchor = '') {
  const ext = extensionOf(path);
  if (ext === 'docx') return 'segments';
  if (TEXT_EXTS.includes(ext) && /^L\d+$/.test(anchor)) return 'segments';
  if (ext === 'xlsx' || ext === 'xlsm') return 'excel';
  if (ext === 'pdf') return 'pdf';
  if (IMAGE_EXTS.includes(ext)) return 'image';
  if (DOWNLOAD_EXTS.includes(ext)) return 'download';
  return 'default';
}

export function parseCellRef(ref) {
  const match = /^\$?([A-Z]{1,3})\$?(\d+)$/i.exec(String(ref || ''));
  if (!match) return null;
  let col = 0;
  for (const letter of match[1].toUpperCase()) col = col * 26 + (letter.charCodeAt(0) - 64);
  return { row: Number(match[2]) - 1, col: col - 1 };
}

// sheet:规则!B3 → { sheet: '规则', cell: 'B3', row: 2, col: 1 }；工作表名本身可含 !
export function parseSheetAnchor(anchor) {
  if (!String(anchor || '').startsWith('sheet:')) return null;
  const body = anchor.slice('sheet:'.length);
  const split = body.lastIndexOf('!');
  if (split <= 0) return null;
  const cell = parseCellRef(body.slice(split + 1));
  if (!cell) return null;
  return { sheet: body.slice(0, split), cell: body.slice(split + 1).toUpperCase(), ...cell };
}

// 定位结果：命中则给出 sheet 下标与目标格；否则说明是位置失效还是超出预览范围
export function locateSheetCell(data, anchor) {
  const target = parseSheetAnchor(anchor);
  if (!target) return null;
  const index = data.sheets.findIndex((sheet) => sheet.name === target.sheet);
  if (index < 0) {
    return data.sheet_truncated
      ? { problem: 'range', message: `工作表「${target.sheet}」超出预览范围（仅预览前 20 个工作表）` }
      : { problem: 'missing', message: `工作表「${target.sheet}」已不存在（资料可能已被替换）` };
  }
  const sheet = data.sheets[index];
  const colCount = Math.max(0, ...sheet.rows.map((row) => row.length));
  if (target.row >= sheet.rows.length || target.col >= colCount) {
    return sheet.truncated
      ? { index, problem: 'range', message: `${target.sheet}!${target.cell} 超出预览范围（仅预览前 500 行 × 60 列）` }
      : { index, problem: 'missing', message: `${target.sheet}!${target.cell} 在当前文件中已不存在（资料可能已被替换）` };
  }
  return { index, cell: { row: target.row, col: target.col } };
}

export function parseTableAnchor(anchor) {
  const match = /^table:(\d+)\/row:(\d+)\/cell:(\d+)$/.exec(String(anchor || ''));
  if (!match) return null;
  return { table: Number(match[1]), row: Number(match[2]), cell: Number(match[3]) };
}

export function anchorLabel(anchor) {
  if (!anchor) return '';
  let match = /^L(\d+)$/.exec(anchor);
  if (match) return `第 ${match[1]} 行`;
  match = /^p:(\d+)$/.exec(anchor);
  if (match) return `第 ${match[1]} 段`;
  const table = parseTableAnchor(anchor);
  if (table) return `表 ${table.table} 第 ${table.row} 行第 ${table.cell} 列`;
  const sheet = parseSheetAnchor(anchor);
  if (sheet) return `${sheet.sheet}!${sheet.cell}`;
  return anchor;
}

export function sourceChipLabel(path, anchor = '') {
  const name = String(path || '').split('/').pop();
  const location = anchorLabel(anchor);
  return location ? `${name} · ${location}` : name;
}

// 分段预览的渲染块：连续的 Word 表格单元格合成一张表，其余逐段（行）输出
export function groupSegments(segments) {
  const blocks = [];
  for (const segment of segments || []) {
    const cell = parseTableAnchor(segment.anchor);
    const last = blocks[blocks.length - 1];
    if (!cell) {
      blocks.push({ type: 'segment', segment });
    } else if (last?.type === 'table' && last.table === cell.table) {
      last.cells.push({ ...cell, segment });
    } else {
      blocks.push({ type: 'table', table: cell.table, cells: [{ ...cell, segment }] });
    }
  }
  for (const block of blocks) {
    if (block.type !== 'table') continue;
    block.rows = Math.max(...block.cells.map((c) => c.row));
    block.cols = Math.max(...block.cells.map((c) => c.cell));
  }
  return blocks;
}

// 旧版二进制 Office 格式：资料分析读不了、平台内也无法预览，上传后提示另存（不拦截上传）
export function legacyOfficeHint(fileName) {
  const ext = extensionOf(fileName);
  if (ext !== 'doc' && ext !== 'xls') return '';
  const target = ext === 'doc' ? '.docx' : '.xlsx';
  return `${fileName} 是旧版格式，建议另存为 ${target} 后上传，否则无法分析和预览`;
}

// 行内代码里带小节的产物路径：research/报告.md#小节（规则「来源」节的写法）
const HEADING_REF_RE = /^((?:ontology|research|reports)\/[^\s`#]+\.md)#(.+)$/i;

export function parseHeadingRef(text) {
  const match = HEADING_REF_RE.exec(String(text || '').trim());
  return match ? { path: match[1], anchor: match[2] } : null;
}

// 小节定位：优先标题全文相同，其次标题包含小节名；找不到返回 -1
export function findHeadingIndex(headings, anchor) {
  const texts = headings.map((text) => String(text).trim());
  const exact = texts.indexOf(anchor);
  return exact >= 0 ? exact : texts.findIndex((text) => text.includes(anchor));
}

// 正文里识别到的 `路径#小节`：小节名末尾多出的右括号属于外层括号，例如
// 「（见 research/报告.md#退款时限）」，截下来放回正文
export function trimHeadingRef(text) {
  const value = String(text || '');
  const split = value.indexOf('#');
  if (split < 0) return { ref: value, rest: '' };
  let end = value.length;
  const count = (str, ch) => str.split(ch).length - 1;
  for (const [open, close] of [['（', '）'], ['(', ')']]) {
    while (end > split + 1 && value[end - 1] === close
      && count(value.slice(split, end), close) > count(value.slice(split, end), open)) {
      end -= 1;
    }
  }
  return { ref: value.slice(0, end), rest: value.slice(end) };
}
