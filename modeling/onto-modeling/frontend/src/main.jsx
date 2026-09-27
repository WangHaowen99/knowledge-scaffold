import { ConfigProvider, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import { ThemeCtx } from './lib/theme-context.js';
import { antdTokens, applyCssVars } from './theme.js';
import './styles.css';

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
