import { ConfigProvider, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import { antdTokens, applyCssVars } from './theme.js';
import './styles.css';

function Root() {
  const [mode] = useState('light');
  const [accent] = useState('blue');

  useEffect(() => {
    applyCssVars(mode, accent);
  }, [mode, accent]);

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: antdTokens(mode, accent),
        algorithm: mode === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
      }}
    >
      <App />
    </ConfigProvider>
  );
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
