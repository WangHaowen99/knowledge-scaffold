import { authFetch } from '../../shared/api/auth.js';

const resourceKey = ({ principalId, workspaceId, revision }) =>
  JSON.stringify([
    String(principalId || ''),
    String(workspaceId || ''),
    'ontologies',
    String(revision ?? 0),
  ]);

const sameScope = (entry, { principalId, workspaceId }) =>
  entry.principalId === String(principalId || '') &&
  entry.workspaceId === String(workspaceId || '');

export function createOntologyResource({ fetcher = authFetch } = {}) {
  const entries = new Map();

  const remove = (key, entry) => {
    if (entries.get(key) === entry) {
      entries.delete(key);
    }
  };

  const normalizeScope = ({ principalId, workspaceId, revision = 0 } = {}) => {
    const normalizedPrincipal = String(principalId || '').trim();
    const normalizedWorkspace = String(workspaceId || '').trim();
    if (!normalizedPrincipal || !normalizedWorkspace) {
      return null;
    }
    return {
      principalId: normalizedPrincipal,
      workspaceId: normalizedWorkspace,
      revision,
    };
  };

  const getOrCreateEntry = (scope) => {
    const key = resourceKey(scope);
    const existing = entries.get(key);
    if (existing) {
      return { entry: existing, key };
    }

    const controller = new AbortController();
    const entry = {
      ...scope,
      controller,
      pending: true,
      subscribers: 0,
      promise: null,
    };
    entry.promise = Promise.resolve()
      .then(() => fetcher('/agentic/ontologies', { signal: controller.signal }))
      .then(async (response) => {
        let payload;
        try {
          payload = await response.json();
        } catch (error) {
          if (!response.ok) {
            throw new Error(
              `Ontology list request failed (${response.status})`,
              { cause: error },
            );
          }
          throw new Error('Ontology list response is not valid JSON', {
            cause: error,
          });
        }
        if (!response.ok) {
          throw new Error(
            payload?.error ||
              payload?.detail ||
              `Ontology list request failed (${response.status})`,
          );
        }
        if (
          !payload ||
          typeof payload !== 'object' ||
          !Array.isArray(payload.ontologies)
        ) {
          throw new Error(
            'Ontology list response must contain an ontologies array',
          );
        }
        const hasInvalidOntology = payload.ontologies.some(
          (ontology) =>
            !ontology ||
            typeof ontology !== 'object' ||
            Array.isArray(ontology) ||
            typeof ontology.id !== 'string' ||
            !ontology.id.trim(),
        );
        if (hasInvalidOntology) {
          throw new Error(
            'Ontology list response must contain a valid ontology object with a non-empty id',
          );
        }
        return payload.ontologies;
      })
      .then((ontologies) => {
        entry.pending = false;
        return ontologies;
      })
      .catch((error) => {
        entry.pending = false;
        remove(key, entry);
        throw error;
      });
    entries.set(key, entry);
    return { entry, key };
  };

  const subscribe = (scopeInput) => {
    const scope = normalizeScope(scopeInput);
    if (!scope) {
      return {
        promise: Promise.reject(
          new Error('Ontology resource requires principalId and workspaceId'),
        ),
        release() {},
      };
    }

    const { entry, key } = getOrCreateEntry(scope);
    entry.subscribers += 1;
    let released = false;
    const release = () => {
      if (released) {
        return;
      }
      released = true;
      entry.subscribers = Math.max(0, entry.subscribers - 1);
      if (entry.pending && entry.subscribers === 0) {
        remove(key, entry);
        entry.controller.abort();
      }
    };
    return { promise: entry.promise, release };
  };

  const load = (scopeInput) => {
    const subscription = subscribe(scopeInput);
    subscription.promise.then(subscription.release, subscription.release);
    return subscription.promise;
  };

  const invalidateScope = ({ principalId, workspaceId } = {}) => {
    const scope = { principalId, workspaceId };
    for (const [key, entry] of entries) {
      if (!sameScope(entry, scope)) {
        continue;
      }
      entry.controller.abort();
      entries.delete(key);
    }
  };

  const clearPrincipal = (principalId) => {
    const normalizedPrincipal = String(principalId || '');
    for (const [key, entry] of entries) {
      if (entry.principalId !== normalizedPrincipal) {
        continue;
      }
      entry.controller.abort();
      entries.delete(key);
    }
  };

  const clear = () => {
    for (const entry of entries.values()) {
      entry.controller.abort();
    }
    entries.clear();
  };

  return {
    load,
    subscribe,
    invalidateScope,
    clearPrincipal,
    clear,
  };
}

export const ontologyResource = createOntologyResource();

export const clearOntologyResourcePrincipal = (principalId) => {
  ontologyResource.clearPrincipal(principalId);
};
