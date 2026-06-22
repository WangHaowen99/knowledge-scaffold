function normalizeValue(value) {
  return String(value || '').trim().toLowerCase();
}

function collectAliases(ontology) {
  const aliasValues = [];
  const candidateSources = [
    ontology?.aliases,
    ontology?.alias,
    ontology?.keywords,
    ontology?.keyword,
    ontology?.synonyms,
    ontology?.synonym,
  ];

  candidateSources.forEach((source) => {
    if (Array.isArray(source)) {
      source.forEach((item) => {
        if (item) aliasValues.push(item);
      });
      return;
    }
    if (typeof source === 'string' && source.trim()) {
      aliasValues.push(source);
    }
  });

  return Array.from(new Set(aliasValues.map((item) => String(item).trim()).filter(Boolean)));
}

function splitLookupAndRemainder(value) {
  const trimmedLeft = String(value || '').replace(/^\s+/, '');
  if (!trimmedLeft) {
    return { lookupTerm: '', remainderText: '' };
  }

  const firstWhitespaceIndex = trimmedLeft.search(/\s/);
  if (firstWhitespaceIndex === -1) {
    return { lookupTerm: trimmedLeft, remainderText: '' };
  }

  return {
    lookupTerm: trimmedLeft.slice(0, firstWhitespaceIndex).trim(),
    remainderText: trimmedLeft.slice(firstWhitespaceIndex).trim(),
  };
}

function getOntologyMatchScore(ontology, normalizedTerm) {
  if (!normalizedTerm) {
    return { score: 0, kind: 'all' };
  }

  const id = String(ontology?.id || '').trim();
  const name = String(ontology?.name || '').trim();
  const aliases = collectAliases(ontology);
  const normalizedId = normalizeValue(id);
  const normalizedName = normalizeValue(name);
  const normalizedAliases = aliases.map((alias) => normalizeValue(alias));

  const exactId = normalizedId === normalizedTerm;
  const exactName = normalizedName === normalizedTerm;
  const exactAlias = normalizedAliases.some((alias) => alias === normalizedTerm);
  if (exactId) return { score: 1000 - id.length, kind: 'exact-id' };
  if (exactName) return { score: 950 - name.length, kind: 'exact-name' };
  if (exactAlias) return { score: 920, kind: 'exact-alias' };

  const prefixId = normalizedId.startsWith(normalizedTerm);
  const prefixName = normalizedName.startsWith(normalizedTerm);
  const prefixAlias = normalizedAliases.some((alias) => alias.startsWith(normalizedTerm));
  if (prefixId) return { score: 840 - id.length, kind: 'prefix-id' };
  if (prefixName) return { score: 800 - name.length, kind: 'prefix-name' };
  if (prefixAlias) return { score: 760, kind: 'prefix-alias' };

  const idContains = normalizedId.includes(normalizedTerm);
  const nameContains = normalizedName.includes(normalizedTerm);
  const aliasContains = normalizedAliases.some((alias) => alias.includes(normalizedTerm));
  if (idContains) return { score: 680 - id.length, kind: 'contains-id' };
  if (nameContains) return { score: 640 - name.length, kind: 'contains-name' };
  if (aliasContains) return { score: 600, kind: 'contains-alias' };

  return { score: -1, kind: '' };
}

function getRankedOntologyMatches(ontologyOptions = [], lookupTerm = '') {
  const normalizedTerm = normalizeValue(lookupTerm);

  return ontologyOptions
    .map((ontology, index) => {
      const match = getOntologyMatchScore(ontology, normalizedTerm);
      return {
        ontology,
        index,
        score: match.score,
        kind: match.kind,
      };
    })
    .filter((item) => item.score >= 0)
    .sort((a, b) => {
      if (b.score !== a.score) return b.score - a.score;
      return a.index - b.index;
    });
}

function resolveOntologySelection(lookupTerm, ontologyOptions = []) {
  const rankedMatches = getRankedOntologyMatches(ontologyOptions, lookupTerm);
  if (rankedMatches.length === 0) return null;

  const [bestMatch, secondMatch] = rankedMatches;
  const bestKind = String(bestMatch.kind || '');
  if (bestKind.startsWith('exact-')) {
    return bestMatch.ontology;
  }

  if (!secondMatch) {
    return bestMatch.ontology;
  }

  const scoreGap = bestMatch.score - secondMatch.score;
  if (bestKind.startsWith('prefix-') && scoreGap >= 30) {
    return bestMatch.ontology;
  }

  return null;
}

export function parseOntologySlashInput(input) {
  const raw = String(input || '');
  const trimmedLeft = raw.replace(/^\s+/, '');
  const cleanMessage = raw.trim();

  if (!trimmedLeft.startsWith('/')) {
    return {
      isSlashMode: false,
      cleanMessage,
      lookupTerm: '',
      remainderText: '',
      source: '',
      prefix: '',
    };
  }

  const ontoCommandMatch = trimmedLeft.match(/^\/onto(?:\s+(.*))?$/i);
  if (ontoCommandMatch) {
    const body = String(ontoCommandMatch[1] || '');
    const { lookupTerm, remainderText } = splitLookupAndRemainder(body);
    return {
      isSlashMode: true,
      cleanMessage: remainderText,
      lookupTerm,
      remainderText,
      source: 'slash-command',
      prefix: '/onto',
    };
  }

  const shorthandBody = trimmedLeft.slice(1);
  const { lookupTerm, remainderText } = splitLookupAndRemainder(shorthandBody);
  return {
    isSlashMode: true,
    cleanMessage: remainderText,
    lookupTerm,
    remainderText,
    source: 'slash-id',
    prefix: '/',
  };
}

export function buildOntologySlashState(input, ontologyOptions = [], limit = 8) {
  const parsed = parseOntologySlashInput(input);
  if (!parsed.isSlashMode) {
    return {
      ...parsed,
      matchedOntologies: [],
    };
  }

  return {
    ...parsed,
    matchedOntologies: getRankedOntologyMatches(ontologyOptions, parsed.lookupTerm)
      .slice(0, limit)
      .map((item) => item.ontology),
  };
}

export function getInputAfterOntologyQuickSelect(input) {
  const parsed = parseOntologySlashInput(input);
  return parsed.remainderText;
}

export function parseSlashOntologyCommand(message, ontologyOptions = []) {
  const parsed = parseOntologySlashInput(message);
  if (!parsed.isSlashMode) {
    return {
      cleanMessage: parsed.cleanMessage,
      selectedOntology: null,
      source: '',
      isSlashMode: false,
    };
  }

  return {
    cleanMessage: parsed.cleanMessage,
    selectedOntology: resolveOntologySelection(parsed.lookupTerm, ontologyOptions),
    source: parsed.source,
    isSlashMode: true,
  };
}
