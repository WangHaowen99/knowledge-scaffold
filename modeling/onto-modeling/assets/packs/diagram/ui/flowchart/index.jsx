// 图示包的流程图编辑器入口（ADR-0055）：官方包的编辑器随平台编译，平台按 `diagram/flowchart` 懒加载，
// 图示包没启用时不加载。默认导出接 `{artifact: {type, key, title}, onClose}`。
export { default } from './FlowchartDialog.jsx';
