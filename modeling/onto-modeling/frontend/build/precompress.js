// 构建后给静态资源各写一份 .br、.gz：后端按 Accept-Encoding 直接发预压缩的文件（backend/app/static_assets.py），
// 不在请求时现压。只用 node 自带的 zlib，不加依赖
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { brotliCompress, constants, gzip } from 'node:zlib';

const br = promisify(brotliCompress);
const gz = promisify(gzip);
const COMPRESSIBLE = /\.(js|mjs|css|html|svg|json|txt|wasm)$/;
// 太小的文件压了也省不了几个字节
export const MIN_SIZE = 1024;

/** 给 dir 下的这些文件（相对路径）写 .br、.gz；压完不比原文件小的不写 */
export async function precompressFiles(dir, files) {
  await Promise.all(files.filter((f) => COMPRESSIBLE.test(f)).map(async (f) => {
    const path = join(dir, f);
    const raw = await readFile(path);
    if (raw.length < MIN_SIZE) return;
    const [b, g] = await Promise.all([
      br(raw, { params: { [constants.BROTLI_PARAM_QUALITY]: 11, [constants.BROTLI_PARAM_SIZE_HINT]: raw.length } }),
      gz(raw, { level: 9 }),
    ]);
    if (b.length < raw.length) await writeFile(`${path}.br`, b);
    if (g.length < raw.length) await writeFile(`${path}.gz`, g);
  }));
}

export default function precompress() {
  return {
    name: 'onto-precompress',
    apply: 'build',
    async writeBundle(options, bundle) {
      await precompressFiles(options.dir, Object.keys(bundle));
    },
  };
}
