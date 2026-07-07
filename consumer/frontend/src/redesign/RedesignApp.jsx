import { useEffect } from 'react';
import AuthProvider from './AuthContext';
import AppShell from './AppShell';
import { applyTheme, getTheme } from './theme';
import './redesign.css';

// meta-wise console 重设计版入口（feat/console-redesign）。
// 设计基准：consumer/frontend/prototype/console-redesign-prototype.html（评审胜出的变体 D）。
export default function RedesignApp() {
  useEffect(() => { applyTheme(getTheme()); }, []);
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  );
}
