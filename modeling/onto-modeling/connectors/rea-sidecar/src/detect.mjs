// 按文件头判定类型、遍历解出来的目录、列安装目录清单（evidence-collection.md §3 inventory.json）。
// 只读固定位置的少量字节，不解析内容；真正的解析交给 REA。
import { open, readdir, lstat } from 'node:fs/promises';
import { basename, extname, join, relative, sep } from 'node:path';

const HEAD_BYTES = 4096;
const PYINSTALLER_TAIL = 1024 * 1024;
const PYINSTALLER_COOKIE = Buffer.from('MEI\x0c\x0b\x0a\x0b\x0e', 'latin1');
const JS_EXTENSIONS = new Set(['.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.html', '.htm', '.asar']);

async function readAt(handle, position, length) {
  const buffer = Buffer.alloc(length);
  const { bytesRead } = await handle.read(buffer, 0, length, position);
  return buffer.subarray(0, bytesRead);
}

/** PE 的 CLI 头（数据目录第 14 项）不为空就是托管程序集。 */
async function isManagedPe(handle, head) {
  if (head.length < 0x40) return false;
  const peOffset = head.readUInt32LE(0x3c);
  const header = await readAt(handle, peOffset, 24 + 240);
  if (header.length < 26 || header.toString('latin1', 0, 4) !== 'PE\0\0') return false;
  const optional = 24;
  const magic = header.readUInt16LE(optional);
  const [countAt, dirsAt] = magic === 0x20b ? [108, 112] : magic === 0x10b ? [92, 96] : [-1, -1];
  if (countAt < 0 || header.length < optional + dirsAt + 15 * 8) return false;
  if (header.readUInt32LE(optional + countAt) <= 14) return false;
  return header.readUInt32LE(optional + dirsAt + 14 * 8) !== 0;
}

async function hasPyInstallerCookie(handle, size) {
  const length = Math.min(size, PYINSTALLER_TAIL);
  const tail = await readAt(handle, size - length, length);
  return tail.includes(PYINSTALLER_COOKIE);
}

/**
 * 判定单个文件：zip、asar、pyc、pe（managed 标出托管程序集）、elf、macho；pe/elf/macho 再看是不是 PyInstaller。
 * 返回 {format, managed, pyinstaller}。
 */
export async function sniff(path) {
  const handle = await open(path, 'r');
  try {
    const { size } = await handle.stat();
    const head = await readAt(handle, 0, HEAD_BYTES);
    const result = { format: 'other', managed: false, pyinstaller: false, size };
    const magic4 = head.subarray(0, 4).toString('latin1');
    if (magic4 === 'PK\x03\x04' || magic4 === 'PK\x05\x06') result.format = 'zip';
    else if (head.length >= 16 && head.readUInt32LE(0) === 4 && head.subarray(8, 40).includes('{"files"')) result.format = 'asar';
    else if (extname(path).toLowerCase() === '.pyc' && head.length >= 16 && head[2] === 0x0d && head[3] === 0x0a) result.format = 'pyc';
    else if (head.subarray(0, 2).toString('latin1') === 'MZ') {
      result.format = 'pe';
      result.managed = await isManagedPe(handle, head);
    } else if (magic4 === '\x7fELF') result.format = 'elf';
    else if (['\xcf\xfa\xed\xfe', '\xce\xfa\xed\xfe', '\xca\xfe\xba\xbe'].includes(magic4)) result.format = 'macho';
    if (['pe', 'elf', 'macho'].includes(result.format) && !result.managed) {
      result.pyinstaller = await hasPyInstallerCookie(handle, size);
    }
    return result;
  } finally {
    await handle.close();
  }
}

/** PyInstaller 解出来的主脚本没有扩展名，REA 给补了 pyc 头。 */
export async function looksLikePyc(path) {
  const handle = await open(path, 'r');
  try {
    const head = await readAt(handle, 0, 16);
    return head.length === 16 && head[2] === 0x0d && head[3] === 0x0a;
  } finally {
    await handle.close();
  }
}

/** 遍历目录（不跟符号链接），最多 maxEntries 项；返回相对路径用 / 分隔。 */
export async function walk(root, { maxEntries = 20000 } = {}) {
  const files = [];
  const skipped = [];
  let seen = 0;
  const queue = [root];
  while (queue.length) {
    const dir = queue.shift();
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      skipped.push({ object: toRel(root, dir), reason: '目录读不了' });
      continue;
    }
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      const abs = join(dir, entry.name);
      seen += 1;
      if (seen > maxEntries) {
        skipped.push({ object: '*', reason: `超过 ${maxEntries} 项，后面的没看` });
        return { files, skipped };
      }
      if (entry.isDirectory()) queue.push(abs);
      else if (entry.isFile()) files.push({ rel: toRel(root, abs), abs, size: (await lstat(abs)).size });
      else skipped.push({ object: toRel(root, abs), reason: entry.isSymbolicLink() ? '符号链接不跟' : '不是普通文件' });
    }
  }
  return { files, skipped };
}

const toRel = (root, abs) => relative(root, abs).split(sep).join('/');

/** 清单里的 kind：javascript | managed | pyc | other。 */
export function inventoryKind(rel, sniffed) {
  if (sniffed?.managed) return 'managed';
  if (sniffed?.format === 'pyc' || rel.toLowerCase().endsWith('.pyc')) return 'pyc';
  if (sniffed?.format === 'asar' || JS_EXTENSIONS.has(extname(rel).toLowerCase()) || basename(rel) === 'package.json') {
    return 'javascript';
  }
  return 'other';
}
