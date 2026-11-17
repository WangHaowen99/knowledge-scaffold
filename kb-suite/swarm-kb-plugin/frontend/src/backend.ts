import { createContext, useContext, useEffect, useState } from 'react';
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
  /** write-only: read back as "******" when set, sending "******" keeps it */
  'x-secret'?: boolean;
}

export type Scope = 'kb' | 'document';

export interface ActionDecl {
  id: string;
  scope: Scope;
  label: string;
  description?: string;
  params_schema?: JsonSchema | null;
  confirm?: boolean;
  job?: boolean;
}

export interface ViewDecl {
  id: string;
  scope: Scope;
  label: string;
  media_type: 'text/markdown' | 'application/json';
  renderer?: 'timeline' | 'table' | 'kv';
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
  // contract section 9 declarations; absent from a gateway that predates them
  actions?: ActionDecl[];
  views?: ViewDecl[];
  settings_schema?: JsonSchema | null;
  search_options_schema?: JsonSchema | null;
  export_formats?: string[];
}

/** A knowledge base without gateway annotations comes straight from kb-service, which supports everything. */
export function has(kb: Pick<KnowledgeBase, 'features'> | null | undefined, feature: string): boolean {
  return !kb?.features || kb.features.includes(feature);
}

/**
 * Strict check for the optional mechanisms of contract section 9: only a backend that says so has them,
 * so kb-service reached directly (no gateway, no feature list) shows none of them.
 */
export function offers(kb: Pick<KnowledgeBase, 'features'> | null | undefined, feature: string): boolean {
  return Boolean(kb?.features?.includes(feature));
}

export function declaredActions(backend: BackendInfo | undefined, kb: KnowledgeBase | null | undefined, scope: Scope): ActionDecl[] {
  return offers(kb, 'actions') ? (backend?.actions ?? []).filter((a) => a.scope === scope) : [];
}

export function declaredViews(backend: BackendInfo | undefined, kb: KnowledgeBase | null | undefined, scope: Scope): ViewDecl[] {
  return offers(kb, 'views') ? (backend?.views ?? []).filter((v) => v.scope === scope) : [];
}

/** The knowledge base on screen, its backend's declarations, and a way to tell the jobs list something started. */
export interface KbScope {
  kb: KnowledgeBase;
  backend: BackendInfo | undefined;
  refreshJobs: () => void;
}

export const KbScopeContext = createContext<KbScope | null>(null);

export function useKbScope(): KbScope | null {
  return useContext(KbScopeContext);
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
