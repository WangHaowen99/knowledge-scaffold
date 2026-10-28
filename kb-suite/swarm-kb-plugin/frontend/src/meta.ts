import { useEffect, useState } from 'react';
import { rpc } from './rpc';
import type { ConfigMeta } from './types';

let cached: Promise<ConfigMeta> | null = null;

export function loadConfigMeta(): Promise<ConfigMeta> {
  if (!cached) {
    cached = rpc.call<ConfigMeta>('config.meta').catch((err) => {
      cached = null;
      throw err;
    });
  }
  return cached;
}

export function useConfigMeta(): ConfigMeta | null {
  const [meta, setMeta] = useState<ConfigMeta | null>(null);
  useEffect(() => {
    let alive = true;
    loadConfigMeta()
      .then((m) => alive && setMeta(m))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);
  return meta;
}
