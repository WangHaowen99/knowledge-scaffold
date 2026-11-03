import { useEffect, useState } from 'react';
import { rpc } from './rpc';
import type { ConfigMeta } from './types';

const cached = new Map<string, Promise<ConfigMeta>>();

/** kb-service settings metadata; ``backend`` picks one of several kb-service instances behind the gateway. */
export function loadConfigMeta(backend = ''): Promise<ConfigMeta> {
  let pending = cached.get(backend);
  if (!pending) {
    pending = rpc.call<ConfigMeta>('config.meta', backend ? { backend } : {}).catch((err) => {
      cached.delete(backend);
      throw err;
    });
    cached.set(backend, pending);
  }
  return pending;
}

/** ``null`` asks nothing: the backend has no kb-service settings (no ``config.meta`` feature). */
export function useConfigMeta(backend: string | null = ''): ConfigMeta | null {
  const [meta, setMeta] = useState<ConfigMeta | null>(null);
  useEffect(() => {
    let alive = true;
    setMeta(null);
    if (backend === null) return undefined;
    loadConfigMeta(backend)
      .then((m) => alive && setMeta(m))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [backend]);
  return meta;
}
