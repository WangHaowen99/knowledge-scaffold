import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react';

type Toast = { text: string; error: boolean } | null;
type Notify = (text: string, error?: boolean) => void;

const ToastContext = createContext<Notify>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<Toast>(null);
  const timer = useRef<number>();
  const notify = useCallback<Notify>((text, error = false) => {
    setToast({ text, error });
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setToast(null), error ? 5000 : 2500);
  }, []);
  return (
    <ToastContext.Provider value={notify}>
      {children}
      {toast ? <div className={`toast ${toast.error ? 'toast-error' : ''}`}>{toast.text}</div> : null}
    </ToastContext.Provider>
  );
}

export function useToast(): Notify {
  return useContext(ToastContext);
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
