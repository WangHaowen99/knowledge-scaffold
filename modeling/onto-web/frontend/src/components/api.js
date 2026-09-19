// legacy Studio 模块的构建垫片：原版 studio-api.js 以相对路径引用 modeling
// 工作台的全局 api 客户端（../../api.js）。onto-web 的图模式默认 kind 下
// 不会触达这些接口（OperationFlowPanel 等仅在 modeling 专属视图出现），
// 该垫片只为保持搬迁文件逐字节零差异并让构建通过；若被意外调用则显式报错。
export function api(path) {
  return Promise.reject(
    new Error(`modeling 专属接口在 onto-web 中不可用：${path}`),
  );
}
