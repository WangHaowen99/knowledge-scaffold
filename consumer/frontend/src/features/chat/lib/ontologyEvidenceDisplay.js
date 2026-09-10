const EVIDENCE_TOOL_NAME = 'ontology_evidence_search';
const CONTEXT_TOOL_NAME = 'ontology_context';
const BUSINESS_DATA_TOOL_NAME = 'business_data_search';

const plainObject = (value) =>
  value != null && typeof value === 'object' && !Array.isArray(value);

const decodeJsonString = (source, key) => {
  const escapedKey = String(key).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = new RegExp(
    `"${escapedKey}"\\s*:\\s*("(?:\\\\.|[^"\\\\])*")`,
  ).exec(source);
  if (!match) {
    return '';
  }
  try {
    return JSON.parse(match[1]);
  } catch {
    return '';
  }
};

const extractCompleteHitObjects = (source) => {
  const marker = '"hits":[';
  const markerIndex = source.indexOf(marker);
  if (markerIndex === -1) {
    return [];
  }

  const hits = [];
  let objectStart = -1;
  let depth = 0;
  let inString = false;
  let escaped = false;

  for (
    let index = markerIndex + marker.length;
    index < source.length;
    index += 1
  ) {
    const currentCharacter = source[index];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (currentCharacter === '\\') {
        escaped = true;
      } else if (currentCharacter === '"') {
        inString = false;
      }
      continue;
    }
    if (currentCharacter === '"') {
      inString = true;
      continue;
    }
    if (currentCharacter === '{') {
      if (depth === 0) {
        objectStart = index;
      }
      depth += 1;
      continue;
    }
    if (currentCharacter === '}') {
      if (depth === 0) {
        continue;
      }
      depth -= 1;
      if (depth === 0 && objectStart !== -1) {
        try {
          const hit = JSON.parse(source.slice(objectStart, index + 1));
          if (hit && typeof hit === 'object') {
            hits.push(hit);
          }
        } catch {
          // A historical output preview may end in the middle of the last hit.
        }
        objectStart = -1;
      }
      continue;
    }
    if (currentCharacter === ']' && depth === 0) {
      break;
    }
  }
  return hits;
};

export const parseOntologyEvidenceOutput = (value) => {
  if (plainObject(value)) {
    if (value.contract !== 'OntologyEvidenceSearch/v1') {
      return null;
    }
    return {
      ...value,
      preview_truncated: Boolean(
        value.preview_truncated || value.projection_truncated,
      ),
    };
  }
  const source = typeof value === 'string' ? value.trim() : '';
  if (!source) {
    return null;
  }
  try {
    const parsed = JSON.parse(source);
    if (parsed?.contract !== 'OntologyEvidenceSearch/v1') {
      return null;
    }
    return { ...parsed, preview_truncated: false };
  } catch {
    if (!source.includes('"contract":"OntologyEvidenceSearch/v1"')) {
      return null;
    }
    return {
      contract: 'OntologyEvidenceSearch/v1',
      success: source.includes('"success":true'),
      status: decodeJsonString(source, 'status'),
      result_code: decodeJsonString(source, 'result_code'),
      ontology_id: decodeJsonString(source, 'ontology_id'),
      ontology_version: decodeJsonString(source, 'ontology_version'),
      ontology_epoch: decodeJsonString(source, 'ontology_epoch'),
      index_generation: decodeJsonString(source, 'index_generation'),
      query: decodeJsonString(source, 'query'),
      retrieval: { mode: decodeJsonString(source, 'mode') },
      hits: extractCompleteHitObjects(source),
      preview_truncated: true,
    };
  }
};

const appendToolEvent = (event, events) => {
  if (!event?.toolName) {
    return;
  }
  events.push({
    toolName: event.toolName || '',
    input: event.input || {},
    output: event.output || '',
    outputProjection: plainObject(event.outputProjection) ?
      event.outputProjection :
      null,
    outputTruncated: Boolean(event.outputTruncated),
    outputRef: event.outputRef || '',
  });
};

const collectSubagentToolEvents = (group, events) => {
  Object.values(group?.children || {}).forEach((child) => {
    const orderedItems =
      Array.isArray(child?.items) && child.items.length ?
        child.items :
        [...(child?.events || []), ...(child?.childGroups || [])];
    orderedItems.forEach((event) => {
      if (event?.subtype === 'group' || (event?.children && !event?.toolName)) {
        collectSubagentToolEvents(event, events);
        return;
      }
      if (event?.kind === 'tool' || event?.toolName) {
        appendToolEvent(event, events);
      }
    });
  });
};

const collectToolEvents = (segments = []) => {
  const events = [];
  (Array.isArray(segments) ? segments : []).forEach((segment) => {
    if (segment?.type === 'tool') {
      const meta = segment.meta || {};
      events.push({
        toolName: meta.tool_name || '',
        input: meta.input || {},
        output: meta.output_preview || segment.text || '',
        outputProjection: plainObject(meta.output_projection) ?
          meta.output_projection :
          null,
        outputTruncated: Boolean(meta.output_truncated),
        outputRef: meta.output_ref || '',
      });
    } else if (segment?.type === 'subagent_group') {
      collectSubagentToolEvents(segment, events);
    } else if (
      ['tool', 'business_data', 'web_search'].includes(segment?.kind) &&
      segment?.toolName
    ) {
      appendToolEvent(segment, events);
    } else if (segment?.kind === 'subagent' && segment?.group) {
      collectSubagentToolEvents(segment.group, events);
    }
  });
  return events;
};

const parseJsonObject = (value) => {
  if (plainObject(value)) {
    return value;
  }
  const source = typeof value === 'string' ? value.trim() : '';
  if (!source) {
    return null;
  }
  try {
    const parsed = JSON.parse(source);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ?
      parsed :
      null;
  } catch {
    return null;
  }
};

export const parseOntologyContextOutput = (value) => {
  const parsed = parseJsonObject(value);
  if (parsed?.contract !== 'OntologyContext/v2' || parsed?.success !== true) {
    return null;
  }
  return parsed;
};

export const parseRuntimeEvidenceOutput = (value) => {
  if (plainObject(value)) {
    if (plainObject(value.runtime_evidence)) {
      return { receipt: value.runtime_evidence, payload: value };
    }
    return null;
  }
  const source = typeof value === 'string' ? value.trim() : '';
  if (!source) {
    return null;
  }

  const payload = parseJsonObject(source);
  if (
    payload?.runtime_evidence &&
    typeof payload.runtime_evidence === 'object'
  ) {
    return { receipt: payload.runtime_evidence, payload };
  }

  const tagged =
    /<runtime-evidence>\s*(?<metadata>[\s\S]*?)\s*<\/runtime-evidence>/.exec(
      source,
    );
  if (!tagged) {
    return null;
  }
  const metadata = parseJsonObject(tagged.groups.metadata);
  const receipt = metadata?.runtime_evidence;
  if (!receipt || typeof receipt !== 'object') {
    return null;
  }
  return { receipt, payload: metadata };
};

const subjectKey = (resource, key) =>
  `${String(resource || '').trim()}:${String(key || '').trim()}`;

export const collectOntologyEvidence = (segments = []) => {
  const toolEvents = collectToolEvents(segments);
  const loadedSubjects = new Set(
    toolEvents
      .filter((event) => event.toolName === CONTEXT_TOOL_NAME)
      .map((event) => subjectKey(event.input?.resource, event.input?.key))
      .filter((key) => key !== ':'),
  );
  const searches = toolEvents
    .filter((event) => event.toolName === EVIDENCE_TOOL_NAME)
    .map((event) => {
      const parsed = parseOntologyEvidenceOutput(
        event.outputProjection || event.output,
      );
      if (!parsed) {
        return null;
      }
      return {
        ...parsed,
        preview_truncated: Boolean(
          parsed.preview_truncated ||
            parsed.projection_truncated ||
            (!event.outputProjection && event.outputTruncated),
        ),
      };
    })
    .filter(Boolean);

  if (searches.length === 0) {
    return null;
  }

  const hitsById = new Map();
  searches.forEach((search, searchIndex) => {
    (Array.isArray(search.hits) ? search.hits : []).forEach((hit) => {
      const evidenceId = String(hit?.evidence_id || '').trim();
      if (!evidenceId) {
        return;
      }
      const nextHit = {
        ...hit,
        search_index: searchIndex,
        loaded: Boolean(
          hit.loaded_before ||
            loadedSubjects.has(subjectKey(hit.resource, hit.key)),
        ),
      };
      const existing = hitsById.get(evidenceId);
      if (
        !existing ||
        Number(nextHit.relevance_score || 0) >
          Number(existing.relevance_score || 0)
      ) {
        hitsById.set(evidenceId, nextHit);
      }
    });
  });

  const hits = [...hitsById.values()].sort(
    (left, right) =>
      Number(right.relevance_score || 0) - Number(left.relevance_score || 0) ||
      String(left.title || '').localeCompare(
        String(right.title || ''),
        'zh-CN',
      ),
  );
  const [firstSearch] = searches;
  const primary = searches.find((search) => search.success) || firstSearch;

  return {
    searches,
    hits,
    query:
      searches
        .map((search) => search.query)
        .filter(Boolean)
        .at(-1) || '',
    ontologyId: primary.ontology_id || '',
    ontologyVersion: primary.ontology_version || '',
    ontologyEpoch: primary.ontology_epoch || '',
    indexGeneration: primary.index_generation || '',
    retrievalMode: primary.retrieval?.mode || '',
    status: primary.status || '',
    resultCode: primary.result_code || '',
    previewTruncated: searches.some((search) => search.preview_truncated),
  };
};

const mergeEvidenceHit = (hitsById, hit) => {
  const evidenceId = String(hit?.evidence_id || '').trim();
  if (!evidenceId) {
    return;
  }
  const existing = hitsById.get(evidenceId);
  if (!existing) {
    hitsById.set(evidenceId, hit);
    return;
  }
  hitsById.set(evidenceId, {
    ...hit,
    ...existing,
    loaded: Boolean(existing.loaded || hit.loaded),
    snippet: existing.snippet || hit.snippet || '',
    source_path: existing.source_path || hit.source_path || '',
  });
};

const runtimeSummary = (payload) => {
  const candidates = [
    payload?.summary,
    payload?.message,
    payload?.result?.summary,
    payload?.data?.summary,
  ];
  return (
    candidates.map((value) => String(value || '').trim()).find(Boolean) ||
    '业务查询结果已固化为可查回的运行事实。'
  );
};

/**
 * Build the evidence view for one assistant turn.
 *
 * The UI must not depend on the model choosing ontology_evidence_search. A
 * structured ontology_context call already carries immutable source evidence
 * IDs, while business_data_search carries the Runtime Fact receipt. This
 * projection keeps both paths visible and deduplicates them with explicit
 * search hits when the search tool was also used.
 */
export const collectTurnEvidence = (segments = []) => {
  const toolEvents = collectToolEvents(segments);
  const searchEvidence = collectOntologyEvidence(segments);
  const ontologyHitsById = new Map();
  const runtimeHitsById = new Map();

  (searchEvidence?.hits || []).forEach((hit) =>
    mergeEvidenceHit(ontologyHitsById, {
      ...hit,
      kind: 'ontology_evidence',
      origin: 'search',
    }),
  );

  toolEvents
    .filter((event) => event.toolName === CONTEXT_TOOL_NAME)
    .forEach((event) => {
      const context = parseOntologyContextOutput(
        event.outputProjection || event.output,
      );
      const evidenceIds = context?.context_meta?.source_evidence_ids;
      if (!Array.isArray(evidenceIds)) {
        return;
      }
      evidenceIds.forEach((evidenceId, index) =>
        mergeEvidenceHit(ontologyHitsById, {
          evidence_id: evidenceId,
          kind: 'ontology_evidence',
          origin: 'context',
          resource: context.resource,
          key: context.key,
          title: context.key || '本体依据',
          section: context.section || '',
          section_anchor: context.section || '',
          source_path: context.context_meta?.source_path || '',
          snippet: String(context.content || '')
            .trim()
            .slice(0, 280),
          ontology_id: context.ontology_id || '',
          ontology_version: context.context_meta?.ontology_version || '',
          ontology_epoch: context.context_meta?.ontology_epoch || '',
          index_generation: context.context_meta?.index_generation || '',
          relevance_score: Math.max(0.01, 1 - index * 0.01),
          confidence_band: 'high',
          loaded: true,
        }),
      );
    });

  toolEvents
    .filter((event) => event.toolName === BUSINESS_DATA_TOOL_NAME)
    .forEach((event) => {
      const runtime = parseRuntimeEvidenceOutput(
        event.outputProjection || event.output,
      );
      const evidenceId = String(runtime?.receipt?.evidence_id || '').trim();
      if (!evidenceId) {
        return;
      }
      const entity = String(event.input?.entity || '').trim();
      const query = String(event.input?.query || '').trim();
      mergeEvidenceHit(runtimeHitsById, {
        evidence_id: evidenceId,
        kind: 'provider_query',
        origin: 'runtime',
        title: entity ? `${entity}查询` : '业务数据查询',
        subject: entity,
        query,
        snippet: runtimeSummary(runtime.payload),
        persistence_status: runtime.receipt.persistence_status || '',
        grounding_status: runtime.receipt.grounding_status || 'unverified',
        grounding_warnings: Array.isArray(runtime.receipt.grounding_warnings) ?
          runtime.receipt.grounding_warnings :
          [],
        derived_from: Array.isArray(runtime.receipt.derived_from) ?
          runtime.receipt.derived_from :
          [],
      });
      (Array.isArray(runtime.receipt.derived_from) ?
        runtime.receipt.derived_from :
        []
      )
        .filter((derivedEvidenceId) =>
          String(derivedEvidenceId || '').startsWith('ont_ev_'),
        )
        .forEach((derivedEvidenceId, index) =>
          mergeEvidenceHit(ontologyHitsById, {
            evidence_id: derivedEvidenceId,
            kind: 'ontology_evidence',
            origin: 'runtime_grounding',
            resource: 'object',
            key: entity,
            title: entity || '业务对象本体依据',
            snippet:
              '该本体证据已被当前运行事实引用，可在证据详情中查回原始定义。',
            relevance_score: Math.max(0.01, 0.95 - index * 0.01),
            confidence_band: 'high',
            loaded: true,
          }),
        );
    });

  const ontology = [...ontologyHitsById.values()].sort(
    (left, right) =>
      Number(right.relevance_score || 0) - Number(left.relevance_score || 0) ||
      String(left.title || '').localeCompare(
        String(right.title || ''),
        'zh-CN',
      ),
  );
  const runtime = [...runtimeHitsById.values()];
  if (ontology.length === 0 && runtime.length === 0) {
    return null;
  }

  const contextMeta =
    ontology.find((hit) => hit.ontology_id || hit.ontology_epoch) || {};
  return {
    ontology,
    runtime,
    all: [...ontology, ...runtime],
    searches: searchEvidence?.searches || [],
    query: searchEvidence?.query || '',
    ontologyId: searchEvidence?.ontologyId || contextMeta.ontology_id || '',
    ontologyVersion:
      searchEvidence?.ontologyVersion || contextMeta.ontology_version || '',
    ontologyEpoch:
      searchEvidence?.ontologyEpoch || contextMeta.ontology_epoch || '',
    indexGeneration:
      searchEvidence?.indexGeneration || contextMeta.index_generation || '',
    retrievalMode:
      searchEvidence?.retrievalMode ||
      (ontology.length ? 'direct_context' : ''),
    status: searchEvidence?.status || (ontology.length ? 'resolved' : ''),
    previewTruncated: Boolean(searchEvidence?.previewTruncated),
  };
};
