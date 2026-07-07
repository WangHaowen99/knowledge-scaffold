# console 重设计原型 — NOTES

**⚠️ THROWAWAY PROTOTYPE**：本目录是一次性原型，不是生产代码，评审结束后应删除或将胜出方案重写进 `platform/console/src/`。

## 回答的问题

按五层配置作用域设计（见 `/Users/example/meta-wise-config-scopes.png` 信息图）重做 console 前端——配置中心三层 IA（全局 L1 / 工作空间 L2 / 个人 L3）+ 新增知识包工作台，应该长什么样？

## 怎么看

```
open console-redesign-prototype.html
```

- 底部浮动条或 ←/→ 键切换变体；URL 参数 `?variant=A|B|C&surface=kb|config|chat&role=admin|user`
- 右上角"管理员 / 普通用户"切换可演示权限交互（只读横幅、禁用输入、隐藏写操作按钮——统一为一种只读范式）

## 三个变体

| 变体 | 结构 | 配置中心 | 知识包工作台 |
|---|---|---|---|
| A | 左侧导航 · 经典控制台 | 顶部作用域分段 tab + 页内子导航，一次一个 section | 左列表 + 右详情（tab：概览/对象/操作/脚本/数据与测试） |
| B | 顶部导航 · 文档式设置 | 左侧按 L1/L2/L3 分组的大纲 + 右侧长文档滚动，全部 section 一页看完 | 卡片画廊 → 面包屑下钻详情 |
| C | 图标栏 · 浏览器式（IDE 风） | 搜索优先的设置树 + 右侧编辑面板 | 三栏浏览器：包树 → 条目列表 → .md 内容预览 |

## 结论（2026-07-07 用户评审）

- **胜出组合：A 的配置中心 + C 的知识包三栏浏览器**，已合并为变体 D（现为默认变体）。
- 之后：将变体 D 重写为 React 组件（配合权限重构 Phase 1 的 AuthContext / useCan / capability 下发），删除本目录。
