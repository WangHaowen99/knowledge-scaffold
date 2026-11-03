import { useEffect, useState } from 'react';
import { rpc } from './rpc';
import type { KnowledgeBase } from './types';

export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  enum?: (string | number)[];
  'x-enum-labels'?: Record<string, string>;
  minimum?: number;
  maximum?: number;
  maxLength?: number;
  properties?: Record<string, JsonSchema>;
}

export interface BackendInfo {
  id: string;
  label: string;
  type: string;
  default: boolean;
  readonly: boolean;
  healthy: boolean;
  error: string | null;
  features: string[];
  file_types: string[];
  limits: { max_upload_mb?: number; max_top_k?: number };
  config_schema: JsonSchema | null;
}

/** A knowledge base without gateway annotations comes straight from kb-service, which supports everything. */
export function has(kb: Pick<KnowledgeBase, 'features'> | null | undefined, feature: string): boolean {
  return !kb?.features || kb.features.includes(feature);
}

/** kb-service's own settings shape (sections such as chunking, wiki, retrieval) and its hand-written forms. */
export function nativeConfig(kb: Pick<KnowledgeBase, 'features'> | null | undefined): boolean {
  return has(kb, 'config.meta');
}

// A list that is complete (every backend answered, so its features are known) is kept for a
// while; an incomplete or failed one is fetched again by the next component that needs it.
const FRESH_MS = 60_000;
let current: BackendInfo[] | null = null;
let fetchedAt = 0;
let pending: Promise<BackendInfo[]> | null = null;
const listeners = new Set<(items: BackendInfo[]) => void>();

export function loadBackends(): Promise<BackendInfo[]> {
  const complete = current !== null && current.length > 0 && current.every((b) => b.healthy);
  if (complete && Date.now() - fetchedAt < FRESH_MS) return Promise.resolve(current as BackendInfo[]);
  if (!pending) {
    pending = rpc
      .call<{ items: BackendInfo[] }>('backends.list')
      .then((res) => {
        current = res.items;
        fetchedAt = Date.now();
        listeners.forEach((fn) => fn(res.items));
        return res.items;
      })
      .finally(() => {
        pending = null;
      });
  }
  return pending;
}

export function useBackends(): BackendInfo[] | null {
  const [backends, setBackends] = useState<BackendInfo[] | null>(current);
  useEffect(() => {
    let alive = true;
    listeners.add(setBackends);
    // a plugin talking to kb-service directly has no backend list: treat it as a single native backend
    loadBackends().catch(() => alive && setBackends((cur) => cur ?? []));
    return () => {
      alive = false;
      listeners.delete(setBackends);
    };
  }, []);
  return backends;
}
