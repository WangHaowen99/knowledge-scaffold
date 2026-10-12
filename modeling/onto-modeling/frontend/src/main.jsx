import { ConfigProvider, theme as antdTheme, unstableSetRender } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import { ThemeCtx } from './lib/theme-context.js';
import { antdTokens, applyCssVars } from './theme.js';
import './styles.css';

// antd v5 静态方法（Modal.confirm / message.* / notification.*）在 React 19 下必须改用
// createRoot 渲染，否则**静默失效**：antd 默认实现走 rc-util 的 render，它从 `react-dom`
// 顶层取 render 与 createRoot，而 React 19 把两者都移出了 `react-dom`（只留在
// `react-dom/client`），取不到就什么也不做——点删除没有确认弹窗、也没有报错提示，一个请求
// 都发不出去（删除项目失效即源于此）。这段等价于官方 @ant-design/v5-patch-for-react-19，
// 内容就是这十几行，按依赖规则不再为此引一个包。必须在任何静态调用之前执行。
unstableSetRender((node, container) => {
  container._reactRoot ||= createRoot(container);
  const root = container._reactRoot;
  root.render(node);
  return async () => {
    // 让出一个微任务：antd 在卸载动画回调里同步调用，直接 unmount 会撞上 React 的
    // "synchronously unmount while rendering" 警告
    await new Promise((resolve) => setTimeout(resolve, 0));
    root.unmount();
  };
});

// 系统「减少动态效果」时关闭 antd 动效：弹窗/抽屉/下拉直接显隐，不依赖 animationend 收尾
const reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');

function Root() {
  const [mode, setMode] = useState(localStorage.getItem('onto-theme-mode') || 'light');
  const [accent, setAccent] = useState(
    localStorage.getItem('onto-theme-accent') || 'blue',
  );
  const [reducedMotion, setReducedMotion] = useState(reducedMotionQuery.matches);

  useEffect(() => {
    const onChange = (event) => setReducedMotion(event.matches);
    reducedMotionQuery.addEventListener('change', onChange);
    return () => reducedMotionQuery.removeEventListener('change', onChange);
  }, []);

  useEffect(() => {
    applyCssVars(mode, accent);
    localStorage.setItem('onto-theme-mode', mode);
    localStorage.setItem('onto-theme-accent', accent);
    // 非 antd 渲染体系（mermaid/代码高亮/drawio 容器）经属性与事件跟随主题
    document.documentElement.dataset.theme = mode;
    window.dispatchEvent(new Event('onto-theme-changed'));
  }, [mode, accent]);

  return (
    <ThemeCtx.Provider value={{ mode, accent, setMode, setAccent }}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          token: { ...antdTokens(mode, accent), motion: !reducedMotion },
          algorithm:
            mode === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        }}
      >
        <App />
      </ConfigProvider>
    </ThemeCtx.Provider>
  );
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
