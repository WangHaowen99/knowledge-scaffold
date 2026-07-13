import { useLayoutEffect } from 'react';
import AuthProvider from './AuthContext';
import AppShell from './AppShell';
import { applyAppearance, applyTheme, getAppearance, getTheme } from './theme';
import './redesign.css';

// meta-wise console 重设计版入口（feat/console-redesign）。
// 设计基准：consumer/frontend/prototype/console-redesign-prototype.html（评审胜出的变体 D）。
export default function RedesignApp() {
  useLayoutEffect(() => {
    applyTheme(getTheme());
    applyAppearance(getAppearance());
    const media = window.matchMedia?.('(prefers-color-scheme: dark)');
    const syncSystemAppearance = () => {
      if (getAppearance() === 'system') applyAppearance('system');
    };
    media?.addEventListener?.('change', syncSystemAppearance);
    return () => media?.removeEventListener?.('change', syncSystemAppearance);
  }, []);
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  );
}
