import { createHash } from 'node:crypto';
import { readFile, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

type FindingKind = 'sensitive_term' | 'corpus_fingerprint';

export interface LeakageFinding {
  kind: FindingKind;
  file: string;
  signature: string;
}

export interface LeakageScanOptions {
  corpusPath: string;
  targetPaths: string[];
  minFingerprintChars?: number;
  maxFindings?: number;
}

export interface LeakageScanResult {
  scannedFiles: number;
  corpusSignatures: number;
  findings: LeakageFinding[];
}

const SOURCE_EXTENSIONS = new Set([
  '.cjs', '.css', '.html', '.js', '.jsx', '.json', '.md', '.mjs', '.py', '.scss',
  '.sh', '.sql', '.ts', '.tsx', '.yaml', '.yml',
]);
const IGNORED_DIRECTORIES = new Set(['.git', 'dist', 'node_modules', 'coverage']);

function digest(value: string) {
  return createHash('sha256').update(value).digest('hex').slice(0, 16);
}

function normalize(value: string) {
  return value
    .normalize('NFKC')
    .toLocaleLowerCase('zh-CN')
    .replace(/[\p{White_Space}\p{Punctuation}\p{Symbol}]+/gu, '');
}

function collectStrings(value: unknown, output: string[]) {
  if (typeof value === 'string') {
    output.push(value);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectStrings(item, output));
    return;
  }
  if (value && typeof value === 'object') {
    Object.values(value as Record<string, unknown>).forEach((item) => collectStrings(item, output));
  }
}

function collectExplicitTerms(value: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
  const record = value as Record<string, unknown>;
  const terms = record.sensitiveTerms ?? record.sensitive_terms;
  if (!Array.isArray(terms)) return [];
  return terms.filter((item): item is string => typeof item === 'string');
}

function createFingerprints(text: string, minChars: number) {
  const normalized = normalize(text);
  if (normalized.length < minChars) return [];
  const windowSize = Math.max(minChars, Math.min(40, normalized.length));
  if (normalized.length === windowSize) return [normalized];
  const step = Math.max(8, Math.floor(windowSize / 2));
  const values = new Set<string>();
  for (let index = 0; index + windowSize <= normalized.length; index += step) {
    values.add(normalized.slice(index, index + windowSize));
  }
  values.add(normalized.slice(-windowSize));
  return [...values];
}

async function readCorpus(corpusPath: string) {
  const raw = await readFile(corpusPath, 'utf8');
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return { cases: [raw] };
  }
}

async function listSourceFiles(entry: string): Promise<string[]> {
  const info = await stat(entry);
  if (info.isFile()) return SOURCE_EXTENSIONS.has(path.extname(entry).toLowerCase()) ? [entry] : [];
  const files: string[] = [];
  for (const item of await readdir(entry, { withFileTypes: true })) {
    if (item.isDirectory() && IGNORED_DIRECTORIES.has(item.name)) continue;
    const itemPath = path.join(entry, item.name);
    if (item.isDirectory()) files.push(...await listSourceFiles(itemPath));
    else if (item.isFile() && SOURCE_EXTENSIONS.has(path.extname(item.name).toLowerCase())) files.push(itemPath);
  }
  return files;
}

export async function scanForEvaluationLeakage(options: LeakageScanOptions): Promise<LeakageScanResult> {
  const minChars = Math.max(12, options.minFingerprintChars ?? 24);
  const maxFindings = Math.max(1, options.maxFindings ?? 100);
  const corpus = await readCorpus(options.corpusPath);
  const explicitTerms = new Set(collectExplicitTerms(corpus).map(normalize).filter(Boolean));
  const corpusStrings: string[] = [];
  collectStrings(corpus, corpusStrings);
  const fingerprints = new Set<string>();
  for (const value of corpusStrings) {
    for (const fingerprint of createFingerprints(value, minChars)) fingerprints.add(fingerprint);
  }
  for (const term of explicitTerms) fingerprints.delete(term);

  const files = (await Promise.all(options.targetPaths.map(listSourceFiles))).flat().sort();
  const findings: LeakageFinding[] = [];
  const seen = new Set<string>();
  for (const file of files) {
    const normalizedSource = normalize(await readFile(file, 'utf8'));
    for (const term of explicitTerms) {
      if (!normalizedSource.includes(term)) continue;
      const signature = digest(term);
      const key = `${file}:sensitive_term:${signature}`;
      if (!seen.has(key)) findings.push({ kind: 'sensitive_term', file, signature });
      seen.add(key);
      if (findings.length >= maxFindings) break;
    }
    if (findings.length >= maxFindings) break;
    for (const fingerprint of fingerprints) {
      if (!normalizedSource.includes(fingerprint)) continue;
      const signature = digest(fingerprint);
      const key = `${file}:corpus_fingerprint:${signature}`;
      if (!seen.has(key)) findings.push({ kind: 'corpus_fingerprint', file, signature });
      seen.add(key);
      if (findings.length >= maxFindings) break;
    }
    if (findings.length >= maxFindings) break;
  }

  return { scannedFiles: files.length, corpusSignatures: explicitTerms.size + fingerprints.size, findings };
}

function parseCli(argv: string[]) {
  const targetPaths: string[] = [];
  let corpusPath = process.env.MODELING_EVALUATION_CORPUS ?? '';
  let minFingerprintChars: number | undefined;
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--corpus') corpusPath = argv[index += 1] ?? '';
    else if (arg === '--target') targetPaths.push(argv[index += 1] ?? '');
    else if (arg === '--min-chars') minFingerprintChars = Number(argv[index += 1]);
    else throw new Error(`未知参数: ${arg}`);
  }
  if (!corpusPath) throw new Error('必须通过 --corpus 或 MODELING_EVALUATION_CORPUS 指定仓库外验收语料');
  const cwd = process.cwd();
  return {
    corpusPath: path.resolve(cwd, corpusPath),
    targetPaths: (targetPaths.length ? targetPaths : ['src', 'frontend/src', 'scripts'])
      .filter(Boolean)
      .map((item) => path.resolve(cwd, item)),
    minFingerprintChars,
  } satisfies LeakageScanOptions;
}

const isMain = process.argv[1]
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  scanForEvaluationLeakage(parseCli(process.argv.slice(2)))
    .then((result) => {
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
      if (result.findings.length) process.exitCode = 1;
    })
    .catch((error: unknown) => {
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
      process.exitCode = 2;
    });
}
