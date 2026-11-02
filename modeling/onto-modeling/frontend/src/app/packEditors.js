// 官方包随平台编译的编辑器（ADR-0055）：assets/packs/<包>/ui/<编辑器>/index.jsx。
// 每个编辑器单独成一个懒加载块，只有打开它时才下载；包没启用时 AR-01 里没有它的产物类型，根本走不到这里。
import { lazy } from 'react';
import { bundledKey } from './packUi.js';

const MODULES = import.meta.glob('@packs/*/ui/*/index.jsx');

const LOADERS = Object.fromEntries(
  Object.entries(MODULES).map(([path, load]) => [bundledKey(path), load]).filter(([key]) => key),
);
const COMPONENTS = new Map();

export const BUNDLED = new Set(Object.keys(LOADERS));

// `包/编辑器` → React.lazy 组件（同一个编辑器只建一次，免得重复挂载时重新加载）
export function bundledEditor(key) {
  if (!LOADERS[key]) return null;
  if (!COMPONENTS.has(key)) COMPONENTS.set(key, lazy(LOADERS[key]));
  return COMPONENTS.get(key);
}
