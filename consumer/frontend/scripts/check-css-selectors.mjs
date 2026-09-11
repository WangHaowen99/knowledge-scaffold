import { readFileSync, readdirSync } from 'node:fs';
import { extname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectDir = resolve(fileURLToPath(new URL('..', import.meta.url)));
const sourceDir = join(projectDir, 'src');
const TEST_FILE_PATTERN = /\.(?:test|spec)\.(?:js|jsx)$/;
const CLASS_TOKEN_PATTERN = /^[_a-zA-Z][\w-]*$/;
const UNKNOWN_VALUE = '\u0000dynamic-class\u0000';
const MAX_VALUE_VARIANTS = 256;

// Classes whose final value is intentionally assembled from runtime state. Keep this
// list explicit: an arbitrary string elsewhere in the application must not make a
// CSS class look reachable.
const DYNAMIC_CLASSES = new Set([
  'hit-history', 'hit-live', 'hit-recent',
  'is-forbidden', 'is-option-0', 'is-option-1', 'is-success', 'is-warning',
  'status-awaiting', 'status-cancelled', 'status-completed', 'status-error',
  'status-failed', 'status-finished', 'status-queued', 'status-running', 'status-success',
  'tone-success',
]);

// Bare interpolations such as `${row.type}` cannot be inferred safely from syntax.
// Record only the combinations that the relevant runtime value can actually emit.
const DYNAMIC_CLASS_GROUPS = [
  ['chat-empty-kicker', 'not-configured'],
  ['chat-empty-kicker', 'not-ready'],
  ['chat-empty-kicker', 'unknown'],
  ['evo-diff-row', 'added'],
  ['evo-diff-row', 'changed'],
  ['evo-diff-row', 'removed'],
  ['kb-ds-feedback', 'error'],
  ['kb-ds-feedback', 'info'],
  ['kb-ds-feedback', 'success'],
  ['mw-model-feedback', 'error'],
  ['mw-model-feedback', 'success'],
  ['obs-state', 'is-error'],
  ['ontology-runtime-node', 'object'],
  ['scope-badge', 'sb-l1'],
  ['scope-badge', 'sb-l2'],
  ['scope-badge', 'sb-l3'],
  ['settings-mcp-notice', 'error'],
  ['settings-mcp-notice', 'success'],
  ['system-config-feedback', 'error'],
  ['system-config-feedback', 'success'],
  ['tag', 'tag-error'],
  ['tr-dot', 'cancelled'],
  ['tr-dot', 'error'],
  ['tr-dot', 'finished'],
  ['tr-dot', 'running'],
  ['ui-toast', 'error'],
  ['ui-toast', 'info'],
];

const walk = (directory) => readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
  const absolutePath = join(directory, entry.name);
  return entry.isDirectory() ? walk(absolutePath) : [absolutePath];
});

const toSourcePath = (absolutePath) => relative(sourceDir, absolutePath).split(sep).join('/');
const files = walk(sourceDir);
const codeFiles = files.filter((file) => ['.js', '.jsx'].includes(extname(file)) && !TEST_FILE_PATTERN.test(file));
const classGroups = new Map();

const classTokens = (value) => value.split(/\s+/).filter((token) => CLASS_TOKEN_PATTERN.test(token));
const addClassGroup = (tokens, origin) => {
  const normalized = [...new Set(tokens.filter((token) => CLASS_TOKEN_PATTERN.test(token)))].sort();
  if (!normalized.length) return;
  const key = normalized.join(' ');
  if (!classGroups.has(key)) classGroups.set(key, { tokens: new Set(normalized), origins: new Set() });
  classGroups.get(key).origins.add(origin);
};

for (const group of DYNAMIC_CLASS_GROUPS) addClassGroup(group, 'dynamic class allowlist');

function skipQuoted(source, start) {
  const quote = source[start];
  let index = start + 1;
  while (index < source.length) {
    if (source[index] === '\\') {
      index += 2;
    } else if (source[index] === quote) {
      return index + 1;
    } else {
      index += 1;
    }
  }
  return source.length;
}

function skipLineComment(source, start) {
  const end = source.indexOf('\n', start + 2);
  return end === -1 ? source.length : end + 1;
}

function skipBlockComment(source, start) {
  const end = source.indexOf('*/', start + 2);
  return end === -1 ? source.length : end + 2;
}

function findMatching(source, start, open = '{', close = '}') {
  let depth = 1;
  let index = start + 1;
  while (index < source.length) {
    const char = source[index];
    if (char === '\'' || char === '"') {
      index = skipQuoted(source, index);
      continue;
    }
    if (char === '`') {
      index = skipTemplate(source, index);
      continue;
    }
    if (char === '/' && source[index + 1] === '/') {
      index = skipLineComment(source, index);
      continue;
    }
    if (char === '/' && source[index + 1] === '*') {
      index = skipBlockComment(source, index);
      continue;
    }
    if (char === open) depth += 1;
    if (char === close) {
      depth -= 1;
      if (depth === 0) return index;
    }
    index += 1;
  }
  return -1;
}

function skipTemplate(source, start) {
  let index = start + 1;
  while (index < source.length) {
    if (source[index] === '\\') {
      index += 2;
      continue;
    }
    if (source[index] === '`') return index + 1;
    if (source[index] === '$' && source[index + 1] === '{') {
      const end = findMatching(source, index + 1);
      if (end === -1) return source.length;
      index = end + 1;
      continue;
    }
    index += 1;
  }
  return source.length;
}

const stripOuterParentheses = (input) => {
  let value = input.trim();
  while (value.startsWith('(')) {
    const end = findMatching(value, 0, '(', ')');
    if (end !== value.length - 1) break;
    value = value.slice(1, -1).trim();
  }
  return value;
};

function forEachTopLevel(source, visit) {
  let parentheses = 0;
  let brackets = 0;
  let braces = 0;
  let index = 0;
  while (index < source.length) {
    const char = source[index];
    if (char === '\'' || char === '"') {
      index = skipQuoted(source, index);
      continue;
    }
    if (char === '`') {
      index = skipTemplate(source, index);
      continue;
    }
    if (char === '/' && source[index + 1] === '/') {
      index = skipLineComment(source, index);
      continue;
    }
    if (char === '/' && source[index + 1] === '*') {
      index = skipBlockComment(source, index);
      continue;
    }
    if (char === '(') parentheses += 1;
    else if (char === ')') parentheses -= 1;
    else if (char === '[') brackets += 1;
    else if (char === ']') brackets -= 1;
    else if (char === '{') braces += 1;
    else if (char === '}') braces -= 1;
    if (parentheses === 0 && brackets === 0 && braces === 0 && visit(index) === false) return;
    index += 1;
  }
}

function findTernary(source) {
  let question = -1;
  let nested = 0;
  let colon = -1;
  forEachTopLevel(source, (index) => {
    if (source[index] === '?' && source[index + 1] !== '?' && source[index + 1] !== '.' && source[index - 1] !== '?') {
      if (question === -1) question = index;
      else nested += 1;
    } else if (question !== -1 && source[index] === ':') {
      if (nested > 0) nested -= 1;
      else {
        colon = index;
        return false;
      }
    }
    return true;
  });
  return question === -1 || colon === -1 ? null : { question, colon };
}

function findTopLevelOperator(source, operators) {
  let found = null;
  forEachTopLevel(source, (index) => {
    const operator = operators.find((candidate) => source.startsWith(candidate, index));
    if (!operator) return true;
    found = { index, operator };
    return false;
  });
  return found;
}

const uniqueValues = (values) => [...new Set(values)].slice(0, MAX_VALUE_VARIANTS);

const combineValues = (leftValues, rightValues, separator = '') => {
  const combined = [];
  for (const left of leftValues) {
    for (const right of rightValues) {
      combined.push(`${left}${separator}${right}`);
      if (combined.length >= MAX_VALUE_VARIANTS) return uniqueValues(combined);
    }
  }
  return uniqueValues(combined);
};

const decodeQuoted = (literal) => literal.slice(1, -1).replace(/\\(['"`\\])/g, '$1');

function evaluateTemplate(source) {
  const end = skipTemplate(source, 0);
  if (end !== source.length) return [UNKNOWN_VALUE];
  let values = [''];
  let segmentStart = 1;
  let index = 1;
  while (index < source.length - 1) {
    if (source[index] === '\\') {
      index += 2;
      continue;
    }
    if (source[index] === '$' && source[index + 1] === '{') {
      const literalSegment = source.slice(segmentStart, index).replace(/\\`/g, '`');
      values = values.map((value) => `${value}${literalSegment}`);
      const expressionEnd = findMatching(source, index + 1);
      if (expressionEnd === -1) return [UNKNOWN_VALUE];
      const expressionValues = evaluateClassValues(source.slice(index + 2, expressionEnd));
      values = combineValues(values, expressionValues);
      index = expressionEnd + 1;
      segmentStart = index;
      continue;
    }
    index += 1;
  }
  const finalSegment = source.slice(segmentStart, -1).replace(/\\`/g, '`');
  return uniqueValues(values.map((value) => `${value}${finalSegment}`));
}

function splitArguments(source) {
  const boundaries = [];
  forEachTopLevel(source, (index) => {
    if (source[index] === ',') boundaries.push(index);
    return true;
  });
  const parts = [];
  let start = 0;
  for (const boundary of boundaries) {
    parts.push(source.slice(start, boundary));
    start = boundary + 1;
  }
  parts.push(source.slice(start));
  return parts;
}

function evaluateClassValues(input) {
  let source = stripOuterParentheses(input);
  source = source.replace(/\.trim\(\)\s*$/, '').trim();
  if (!source) return [''];

  if ((source[0] === '\'' || source[0] === '"') && skipQuoted(source, 0) === source.length) {
    return [decodeQuoted(source)];
  }
  if (source[0] === '`') return evaluateTemplate(source);

  const ternary = findTernary(source);
  if (ternary) {
    return uniqueValues([
      ...evaluateClassValues(source.slice(ternary.question + 1, ternary.colon)),
      ...evaluateClassValues(source.slice(ternary.colon + 1)),
    ]);
  }

  const fallback = findTopLevelOperator(source, ['||', '??']);
  if (fallback) {
    return uniqueValues([
      ...evaluateClassValues(source.slice(0, fallback.index)),
      ...evaluateClassValues(source.slice(fallback.index + fallback.operator.length)),
    ]);
  }

  const conditional = findTopLevelOperator(source, ['&&']);
  if (conditional) return uniqueValues(['', ...evaluateClassValues(source.slice(conditional.index + 2))]);

  const concatenation = findTopLevelOperator(source, ['+']);
  if (concatenation) {
    return combineValues(
      evaluateClassValues(source.slice(0, concatenation.index)),
      evaluateClassValues(source.slice(concatenation.index + 1)),
    );
  }

  const helper = /^(?:clsx|classnames|classNames|cn)\s*\(/.exec(source);
  if (helper) {
    const opening = source.indexOf('(', helper.index);
    const closing = findMatching(source, opening, '(', ')');
    if (closing === source.length - 1) {
      let values = [''];
      for (const argument of splitArguments(source.slice(opening + 1, closing))) {
        values = combineValues(values, evaluateClassValues(argument), ' ');
      }
      return values;
    }
  }

  return [UNKNOWN_VALUE];
}

const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function addClassValue(value, origin) {
  const rawTokens = value.trim().split(/\s+/).filter(Boolean);
  let variants = [[]];
  for (const rawToken of rawTokens) {
    if (!rawToken.includes(UNKNOWN_VALUE)) {
      if (!CLASS_TOKEN_PATTERN.test(rawToken)) continue;
      variants = variants.map((tokens) => [...tokens, rawToken]);
      continue;
    }

    // A prefixed/suffixed dynamic class (for example status-${status}) can be
    // matched safely against the explicit allowlist. A bare interpolation is
    // deliberately ignored and must be listed in DYNAMIC_CLASS_GROUPS.
    if (rawToken === UNKNOWN_VALUE) continue;
    const pattern = new RegExp(`^${rawToken.split(UNKNOWN_VALUE).map(escapeRegExp).join('.+')}$`);
    const matches = [...DYNAMIC_CLASSES].filter((className) => pattern.test(className));
    if (matches.length) {
      variants = variants.flatMap((tokens) => matches.map((className) => [...tokens, className]));
    }
  }
  for (const tokens of variants) addClassGroup(tokens, origin);
}

function collectClassNameAttributes(source, origin) {
  const attribute = /\bclassName\s*=\s*/g;
  let match;
  while ((match = attribute.exec(source)) !== null) {
    const start = match.index + match[0].length;
    const first = source[start];
    if (first === '\'' || first === '"') {
      const end = skipQuoted(source, start);
      addClassValue(decodeQuoted(source.slice(start, end)), origin);
      attribute.lastIndex = end;
      continue;
    }
    if (first !== '{') continue;
    const end = findMatching(source, start);
    if (end === -1) continue;
    for (const value of evaluateClassValues(source.slice(start + 1, end))) addClassValue(value, origin);
    attribute.lastIndex = end + 1;
  }
}

function collectClassNameProperties(source, origin) {
  const property = /\bclassName\s*:\s*/g;
  let match;
  while ((match = property.exec(source)) !== null) {
    const start = match.index + match[0].length;
    if (!['\'', '"', '`'].includes(source[start])) continue;
    const end = source[start] === '`' ? skipTemplate(source, start) : skipQuoted(source, start);
    for (const value of evaluateClassValues(source.slice(start, end))) addClassValue(value, origin);
    property.lastIndex = end;
  }
}

for (const file of codeFiles) {
  const source = readFileSync(file, 'utf8');
  const origin = toSourcePath(file);
  collectClassNameAttributes(source, origin);
  collectClassNameProperties(source, origin);
}

const usedClasses = new Set(DYNAMIC_CLASSES);
for (const { tokens } of classGroups.values()) {
  for (const token of tokens) usedClasses.add(token);
}

const unusedByFile = new Map();
const unusedCompoundsByFile = new Map();
const COMPOUND_CLASS_PATTERN = /\.([_a-zA-Z][\w-]*(?:\.[_a-zA-Z][\w-]*)+)/g;

for (const file of files.filter((candidate) => extname(candidate) === '.css')) {
  const css = readFileSync(file, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const selectorSource = [...css.matchAll(/([^{}]+)\{/g)]
    .map((match) => match[1].trim())
    .filter((prelude) => prelude && !prelude.startsWith('@'))
    .join('\n');
  const classes = [...new Set([...selectorSource.matchAll(/\.([_a-zA-Z][\w-]*)/g)].map((match) => match[1]))];
  const unused = classes.filter((className) => !usedClasses.has(className));
  if (unused.length) unusedByFile.set(toSourcePath(file), unused.sort());

  const compounds = [...new Set([...selectorSource.matchAll(COMPOUND_CLASS_PATTERN)].map((match) => `.${match[1]}`))];
  const unusedCompounds = compounds.filter((compound) => {
    const tokens = compound.slice(1).split('.');
    return ![...classGroups.values()].some((group) => tokens.every((token) => group.tokens.has(token)));
  });
  if (unusedCompounds.length) unusedCompoundsByFile.set(toSourcePath(file), unusedCompounds.sort());
}

if (unusedByFile.size || unusedCompoundsByFile.size) {
  console.error('Potentially orphaned CSS classes or compound selectors:');
  for (const [file, classes] of unusedByFile) {
    console.error(`  - ${file} (classes): ${classes.map((className) => `.${className}`).join(', ')}`);
  }
  for (const [file, compounds] of unusedCompoundsByFile) {
    console.error(`  - ${file} (compound selectors never emitted together): ${compounds.join(', ')}`);
  }
  process.exitCode = 1;
} else {
  console.log(`CSS classes and compound selectors: ${files.filter((file) => extname(file) === '.css').length} stylesheets checked`);
}
