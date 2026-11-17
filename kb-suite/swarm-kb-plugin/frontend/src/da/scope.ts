// The page's knowledge-base selection against what kb-gateway lists right now.
//
// A backend that is down drops out of the list with all its knowledge bases. A selection
// made on it must stay in the question's scope: kb-gateway's tools then say those knowledge
// bases were not searched. Filtering them out would hide that, and a selection of only such
// knowledge bases would turn into "every knowledge base".

/** A backend whose knowledge bases are missing from the list (from the plugin's `hello`). */
export type DownBackend = {
  label: string;
  /** "" for the backend without an id prefix, null when unknown */
  id_prefix: string | null;
  /** the knowledge bases kb-gateway last saw there */
  knowledge_bases: { id: string; name: string }[];
};

export type DownKb = { id: string; name: string; backend: string };

export type Reconciled = {
  /** the selection to keep: listed ids and ids on a backend that is down */
  ids: string[];
  /** the kept ids that sit on a backend that is down */
  down: DownKb[];
  /** there was a selection and nothing of it is left: ask again instead of searching everything */
  lost: boolean;
};

// kb-gateway gives every backend but one an id prefix of letters and digits ending in "_";
// the one without (kb-service) uses UUIDs, which never contain "_"
const PREFIXED = /^[A-Za-z0-9]+_/;

/** The backend that is down and owns `id`, by kb-gateway's own routing rule. */
export function downBackendOf(id: string, down: DownBackend[]): DownBackend | undefined {
  return (
    down.find((b) => b.knowledge_bases.some((kb) => kb.id === id)) ??
    down.find((b) => Boolean(b.id_prefix) && id.startsWith(b.id_prefix as string)) ??
    down.find((b) => b.id_prefix === '' && !PREFIXED.test(id))
  );
}

/** Keep the listed ids and those on a backend that is down; drop the rest (deleted knowledge bases). */
export function reconcileSelection(selected: string[], listed: { id: string }[], down: DownBackend[]): Reconciled {
  const present = new Set(listed.map((kb) => kb.id));
  const kept: string[] = [];
  const downKbs: DownKb[] = [];
  for (const id of selected) {
    if (present.has(id)) {
      kept.push(id);
      continue;
    }
    const backend = downBackendOf(id, down);
    if (!backend) continue;
    kept.push(id);
    downKbs.push({ id, name: backend.knowledge_bases.find((kb) => kb.id === id)?.name ?? id, backend: backend.label });
  }
  return { ids: kept, down: downKbs, lost: selected.length > 0 && kept.length === 0 };
}

/** The plugin's `hello.unavailable`; an older plugin sent labels only, which cannot keep a selection. */
export function parseDown(value: unknown): DownBackend[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): DownBackend[] => {
    if (typeof item === 'string') return item ? [{ label: item, id_prefix: null, knowledge_bases: [] }] : [];
    if (!item || typeof item !== 'object') return [];
    const raw = item as { label?: unknown; id_prefix?: unknown; knowledge_bases?: unknown };
    if (typeof raw.label !== 'string' || !raw.label) return [];
    const kbs = Array.isArray(raw.knowledge_bases) ? raw.knowledge_bases : [];
    return [{
      label: raw.label,
      id_prefix: typeof raw.id_prefix === 'string' ? raw.id_prefix : null,
      knowledge_bases: kbs
        .filter((kb): kb is { id: string; name?: unknown } => Boolean(kb) && typeof (kb as { id?: unknown }).id === 'string')
        .map((kb) => ({ id: kb.id, name: typeof kb.name === 'string' && kb.name ? kb.name : kb.id })),
    }];
  });
}
