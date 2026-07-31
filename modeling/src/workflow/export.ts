import { zipSync, strToU8 } from 'fflate';
import { assertSafeZipPath } from './keys.js';

// 把渲染出的包文件打成标准 zip（consumer import-zip 用 Python zipfile 解压）。
// 顶层放一个包目录，符合 examples 的 ontology/ 结构惯例（内部即 manifest 等）。
export function zipPackage(files: Record<string, string | Uint8Array>, rootDir = 'ontology'): Uint8Array {
  const entries: Record<string, Uint8Array> = {};
  for (const [path, content] of Object.entries(files)) {
    // 二次断言：拼 zip 条目前确认路径不越界（renderPackage 已校验 key，这里防御其他调用方）
    assertSafeZipPath(path);
    entries[`${rootDir}/${path}`] = typeof content === 'string' ? strToU8(content) : new Uint8Array(content);
  }
  return zipSync(entries, { level: 6 });
}
