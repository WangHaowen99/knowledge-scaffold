// 解析钩子（tests/pack-resolve.mjs 登记）：只改写 @onto/、@packs/ 与包目录里的裸模块名，其余原样交给 node
const SRC = new URL('../src/', import.meta.url).href;
const PACKS = new URL('../../assets/packs/', import.meta.url).href;
const APP_ENTRY = new URL('../src/main.jsx', import.meta.url).href;

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('@onto/')) return nextResolve(SRC + specifier.slice('@onto/'.length), context);
  if (specifier.startsWith('@packs/')) return nextResolve(PACKS + specifier.slice('@packs/'.length), context);
  if (context.parentURL?.startsWith(PACKS) && !/^(\.|\/|[a-z]+:)/.test(specifier)) {
    return nextResolve(specifier, { ...context, parentURL: APP_ENTRY });
  }
  return nextResolve(specifier, context);
}
