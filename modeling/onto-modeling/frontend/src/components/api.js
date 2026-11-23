// legacy Studio 组件沿用 api(path, options) 调用约定，统一转到
// onto-modeling 的鉴权请求器；目前关系图只会触达已实现的操作流读取接口。
export { request as api } from '../lib/api.js';
