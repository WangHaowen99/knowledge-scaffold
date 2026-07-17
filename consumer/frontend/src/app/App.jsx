import { useLayoutEffect } from 'react';
import AuthProvider from '../features/auth/AuthProvider';
import AppShell from './AppShell';
import { applyAppearance, applyTheme, getAppearance, getTheme } from '../shared/styles/theme';
import '../shared/ui/ui.css';

export default function App() {
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
