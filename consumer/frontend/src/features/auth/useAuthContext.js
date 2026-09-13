import { createContext, useContext } from 'react';

// Context 对象与消费 hook 单独成文件（满足 react-refresh 的组件文件纯度要求）。
export const AuthCtx = createContext(null);

export function useAuthContext() {
  const ctx = useContext(AuthCtx);
  if (!ctx) {
    throw new Error('useAuthContext 必须在 <AuthProvider> 内使用');
  }
  return ctx;
}
