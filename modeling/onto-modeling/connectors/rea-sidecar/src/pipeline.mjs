// 一个任务的流水线（evidence-collection.md §5）：判定类型 → 解包 → 按类型跑白名单命令 → 转记录 → 写结果。
// 在 runner 子进程里跑，和 HTTP 服务不在一个进程：REA 输出再大、解析再慢，最多拖垮这个子进程。
import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdir, readFile, rename, stat, writeFile } from 'node:fs/promises';
import { basename, dirname, join, resolve, sep } from 'node:path';
import { once } from 'node:events';

import { canonicalJson } from './anchors.mjs';
import { inventoryKind, looksLikePyc, sniff, walk } from './detect.mjs';
import { mapManagedMembers, mapManagedNative } from './map-dotnet.mjs';
import { mapJavascript } from './map-js.mjs';
import { mapPythonPyc, pycModuleName } from './map-python.mjs';
import { PYTHON_STDLIB } from './python-stdlib.mjs';
import { RecordSet } from './records.mjs';
import { childEnv, runRea } from './rea.mjs';

const MAX_LIMITATIONS = 200;
const FRAMEWORK_ASSEMBLY = /^(System|Microsoft|mscorlib|netstandard|WindowsBase|PresentationCore|PresentationFramework|Newtonsoft|Mono\.|NuGet)/i;
const ELECTRON_OWN_ASAR = new Set(['electron.asar', 'default_app.asar']);
const PYINSTALLER_INTERNAL = /^(pyiboot\d+_|pyimod\d+_|pyi_rth_|_pyi_|pyi_)/;
const SAFE_CODE = /^[a-z0-9_.-]{1,64}$/i;

const depth = (rel) => rel.split('/').length;
const topModule = (module) => module.split('.')[0];

class Context {
  constructor(job, emit) {
    this.job = job;
    this.emit = emit;
    this.records = new RecordSet({ source: job.source, provider: job.provider });
    this.analyzers = [];
    this.limitations = [];
    this.limitationSet = new Map();
    this.unknowns = 0;
    this.detected = new Set();
    this.items = [];
    this.skipped = [];
    this.outputs = new Map();
    this.workDir = join(job.dir, 'work');
    this.env = childEnv({ home: join(job.dir, 'home'), tmp: join(job.dir, 'tmp') });
  }

  /** 局限去重；REA 按文件逐条给的（`a/b.pyc: 说明`）合成一条，带文件数。 */
  limit(text) {
    const value = String(text);
    const perFile = /^(\S+?): (.+)$/s.exec(value);
    const key = perFile ? perFile[2] : value;
    const existing = this.limitationSet.get(key);
    if (existing) {
      if (perFile) existing.files += 1;
      return;
    }
    if (this.limitations.length >= MAX_LIMITATIONS) return;
    const entry = perFile && key !== value ? { text: key, first: perFile[1], files: 1 } : { text: value, files: 0 };
    this.limitationSet.set(key, entry);
    this.limitations.push(entry);
  }

  renderedLimitations() {
    return this.limitations.map((entry) => {
      if (!entry.first) return entry.text;
      return entry.files > 1 ? `${entry.first} 等 ${entry.files} 个文件：${entry.text}` : `${entry.first}: ${entry.text}`;
    });
  }

  progress(phase, extra = {}) {
    this.emit({ phase, done: 0, total: 0, unit: '', ...extra });
  }

  /** 跑一条 REA 命令并解析输出；失败记进 analyzers（带 error）和局限，回 null。 */
  async rea(command, args, target) {
    const runs = this.outputs.get(command) ?? [];
    const stdoutPath = join(this.workDir, `${command}-${runs.length + 1}.json`);
    const entry = { command, provider: null, ms: 0, target };
    this.analyzers.push(entry);
    let run;
    try {
      run = await runRea(command, args, {
        reaBin: this.job.reaBin, cwd: this.workDir, env: this.env, stdoutPath,
        maxOutputBytes: this.job.maxOutputBytes,
        onProgress: (progress) => this.progress('解析程序', progress),
      });
    } catch (error) {
      entry.error = SAFE_CODE.test(error.code ?? '') ? error.code : 'rea_failed';
      this.limit(`${command} 没跑完（${entry.error}）：${target}`);
      return null;
    }
    entry.ms = run.ms;
    runs.push({ target, path: stdoutPath });
    this.outputs.set(command, runs);
    let data = null;
    try {
      data = JSON.parse(await readFile(stdoutPath, 'utf8'));
    } catch {
      data = null;
    }
    const provider = data?.provider;
    if (provider?.id) entry.provider = `${provider.id}@${provider.version ?? ''}`;
    if (run.code !== 0 || !data || data.error || !data.normalized_result) {
      const code = SAFE_CODE.test(String(data?.code ?? '')) ? data.code : `exit_${run.code ?? run.signal}`;
      entry.error = code;
      this.limit(`${command} 失败（${code}）：${target}`);
      return null;
    }
    for (const item of data.limitations ?? []) this.limit(item);
    return data;
  }

  map(entry, mapper) {
    try {
      return mapper();
    } catch {
      entry.error = 'mapping_failed';
      this.limit(`${entry.command} 的结果转换失败：${entry.target}`);
      return null;
    }
  }

  lastAnalyzer() {
    return this.analyzers.at(-1);
  }
}

/** 规划要跑的分析：一个 JS 应用、若干托管程序集、PyInstaller 程序、松散 pyc。 */
async function plan(ctx, root, isDir) {
  const { job } = ctx;
  const targets = { js: null, dotnet: [], pyinstaller: [], pyc: [] };
  if (!isDir) {
    const sniffed = await sniff(root);
    ctx.items.push({ object: job.filename, kind: inventoryKind(job.filename, sniffed), bytes: sniffed.size });
    const target = { abs: root, rel: job.filename };
    if (sniffed.format === 'asar') targets.js = { ...target, asar: true };
    else if (sniffed.managed) targets.dotnet.push(target);
    else if (sniffed.pyinstaller) targets.pyinstaller.push(target);
    else if (sniffed.format === 'pyc') targets.pyc.push({ ...target, module: pycModuleName(job.filename) });
    return targets;
  }
  const { files, skipped } = await walk(root, { maxEntries: job.maxEntries });
  ctx.skipped.push(...skipped);
  const asars = [];
  const packages = [];
  let looseJs = false;
  for (const file of files) {
    const sniffed = await sniff(file.abs).catch(() => null);
    ctx.items.push({ object: file.rel, kind: inventoryKind(file.rel, sniffed), bytes: file.size });
    const inModules = file.rel.split('/').includes('node_modules');
    if (sniffed?.format === 'asar' && !ELECTRON_OWN_ASAR.has(basename(file.rel))) asars.push(file);
    else if (basename(file.rel) === 'package.json' && !inModules) packages.push(file);
    else if (/\.(m|c)?js$/i.test(file.rel) && !inModules) looseJs = true;
    if (sniffed?.managed) {
      if (FRAMEWORK_ASSEMBLY.test(basename(file.rel))) ctx.skipped.push({ object: file.rel, reason: '框架程序集，不分析' });
      else targets.dotnet.push(file);
    } else if (sniffed?.pyinstaller) targets.pyinstaller.push(file);
    else if (sniffed?.format === 'pyc') targets.pyc.push({ ...file, module: pycModuleName(file.rel) });
  }
  const byDepth = (a, b) => depth(a.rel) - depth(b.rel) || (a.rel < b.rel ? -1 : 1);
  if (asars.length) {
    asars.sort((a, b) => Number(basename(b.rel) === 'app.asar') - Number(basename(a.rel) === 'app.asar') || byDepth(a, b));
    targets.js = { ...asars[0], asar: true };
  } else if (packages.length) {
    packages.sort(byDepth);
    const rel = dirname(packages[0].rel);
    targets.js = { abs: dirname(packages[0].abs), rel: rel === '.' ? '' : rel, asar: false };
  } else if (looseJs) {
    targets.js = { abs: root, rel: '', asar: false };
  }
  for (const [kind, max] of [['dotnet', job.maxAssemblies], ['pyinstaller', job.maxPrograms], ['pyc', job.maxPyc]]) {
    for (const extra of targets[kind].splice(max)) ctx.skipped.push({ object: extra.rel, reason: `同类超过 ${max} 个，没分析` });
  }
  return targets;
}

async function analyzeJavascript(ctx, target) {
  const label = target.rel || ctx.job.filename;
  const data = await ctx.rea('analyze-javascript-application', [target.abs], label);
  if (!data) return;
  const info = ctx.map(ctx.lastAnalyzer(), () => mapJavascript(data, ctx.records));
  if (!info) return;
  ctx.detected.add(info.detected);
  ctx.unknowns += info.unknowns;
  if (target.asar) {
    // asar 里的文件只有 REA 看得到，清单从它的结果里补
    for (const node of data.normalized_result.graph?.nodes ?? []) {
      const props = node.observations?.[0]?.properties ?? {};
      const path = props.file_occurrence?.path ?? props.path;
      if (!['javascript-asset', 'artifact'].includes(node.kind) || typeof path !== 'string') continue;
      ctx.items.push({ object: `${label}/${path}`, kind: inventoryKind(path), bytes: Number.isSafeInteger(props.bytes) ? props.bytes : 0 });
    }
  }
}

async function analyzeDotnet(ctx, target) {
  const members = await ctx.rea('inspect-managed-members', [target.abs], target.rel);
  const mapped = members && ctx.map(ctx.lastAnalyzer(), () => mapManagedMembers(members, ctx.records, { file: target.rel }));
  if (mapped) ctx.detected.add('dotnet');
  const native = await ctx.rea('inspect-managed-native-boundaries', [target.abs], target.rel);
  if (native && mapped) {
    ctx.map(ctx.lastAnalyzer(), () => mapManagedNative(native, ctx.records, { methodAnchors: mapped.methodAnchors }));
  }
}

async function analyzePyc(ctx, file, rel, module, modules) {
  const data = await ctx.rea('inspect-python-pyc', [file], rel);
  if (!data) return;
  const mapped = ctx.map(ctx.lastAnalyzer(), () => mapPythonPyc(data, ctx.records, { module, file: rel }));
  if (mapped?.anchor) modules.push({ module, anchor: mapped.anchor, names: new Set(mapped.names) });
}

/**
 * PyInstaller：extract-artifact 解出 CArchive 和 PYZ，先分析主脚本，再沿主脚本引用的名字找包里的非标准库模块
 * （逐层往外，总数不超过 maxPyc）。
 */
async function analyzePyInstaller(ctx, target, modules) {
  ctx.progress('解包');
  const extracted = await ctx.rea('extract-artifact', [target.abs], target.rel);
  const result = extracted?.normalized_result ?? {};
  const root = typeof result.output_root === 'string' ? resolve(result.output_root) : null;
  if (!root || !root.startsWith(resolve(ctx.job.dir) + sep)) {
    if (extracted) ctx.limit(`PyInstaller 程序没解出来：${target.rel}`);
    return;
  }
  ctx.detected.add('pyinstaller');
  const entries = [];
  for (const artifact of result.artifacts ?? []) {
    if (typeof artifact?.relative_path !== 'string') continue;
    entries.push(artifact.relative_path);
    ctx.items.push({
      object: `${target.rel}/${artifact.relative_path}`,
      kind: artifact.relative_path.endsWith('.pyc') ? 'pyc' : 'other',
      bytes: Number.isSafeInteger(artifact.bytes_written) ? artifact.bytes_written : 0,
    });
  }
  const mains = [];
  for (const rel of entries) {
    if (rel.includes('/') || rel.includes('.') || PYINSTALLER_INTERNAL.test(rel) || PYTHON_STDLIB.has(rel)) continue;
    if (await looksLikePyc(join(root, rel)).catch(() => false)) mains.push({ rel, module: rel });
  }
  const archived = entries
    .filter((rel) => /^PYZ-\d+\.pyz\/.+\.pyc$/.test(rel))
    .map((rel) => ({ rel, module: rel.replace(/^PYZ-\d+\.pyz\//, '').replace(/\.pyc$/, '').replace(/\/__init__$/, '').split('/').join('.') }))
    .filter(({ module }) => !PYTHON_STDLIB.has(topModule(module)) && !PYINSTALLER_INTERNAL.test(topModule(module)));
  const done = new Set();
  let wave = mains;
  let count = 0;
  while (wave.length) {
    const before = modules.length;
    for (const item of wave) {
      if (count >= ctx.job.maxPyc) {
        ctx.limit(`PyInstaller 包里的模块超过 ${ctx.job.maxPyc} 个，后面的没分析：${target.rel}`);
        return;
      }
      done.add(item.rel);
      count += 1;
      ctx.progress('解析程序', { done: count, total: count + wave.length - wave.indexOf(item) - 1, unit: '文件' });
      await analyzePyc(ctx, join(root, item.rel), `${target.rel}/${item.rel}`, item.module, modules);
    }
    const names = new Set(modules.slice(before).flatMap((item) => [...item.names]));
    wave = archived.filter((item) => !done.has(item.rel) && (names.has(item.module) || names.has(topModule(item.module))));
  }
}

/** 已分析的 Python 模块之间：A 引用了 B 的模块名（或 from 包 import 子模块）就连 imports。 */
function linkPythonImports(ctx, modules) {
  for (const from of modules) {
    for (const to of modules) {
      if (from === to) continue;
      const parts = to.module.split('.');
      const imported = from.names.has(to.module)
        || (parts.length > 1 && from.names.has(parts.slice(0, -1).join('.')) && from.names.has(parts.at(-1)));
      if (imported) ctx.records.relate({ from: from.anchor, name: 'imports', to: to.anchor, method: 'rea inspect-python-pyc' });
    }
  }
}

async function sha256File(path) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}

/** 原始结果：一条命令只跑一次就是 <命令>.json；跑了多次（多个 pyc、多个程序集）合成一个 <命令>.jsonl，一行一份。 */
async function writeRaw(ctx) {
  const rawDir = join(ctx.job.dir, 'raw');
  const raw = [];
  for (const [command, runs] of ctx.outputs) {
    let name;
    let mediaType;
    if (runs.length === 1) {
      name = `${command}.json`;
      mediaType = 'application/json';
      await rename(runs[0].path, join(rawDir, name));
    } else {
      name = `${command}.jsonl`;
      mediaType = 'application/x-ndjson';
      const out = createWriteStream(join(rawDir, name));
      for (const run of runs) {
        let text = (await readFile(run.path, 'utf8')).trim();
        if (!text) continue;
        if (text.includes('\n')) {
          try {
            text = JSON.stringify(JSON.parse(text));
          } catch {
            continue;
          }
        }
        if (!out.write(`${text}\n`)) await once(out, 'drain');
      }
      out.end();
      await once(out, 'finish');
    }
    const path = join(rawDir, name);
    raw.push({ name, sha256: await sha256File(path), size: (await stat(path)).size, media_type: mediaType });
  }
  return raw;
}

/** 跑完整个任务，写 out/records.jsonl、out/result.json，回 {state, error_code, error_message}。 */
export async function runPipeline(job, emit = () => {}) {
  const ctx = new Context(job, emit);
  for (const dir of ['work', 'home', 'tmp', 'raw', 'out']) await mkdir(join(job.dir, dir), { recursive: true });
  ctx.progress('识别类型');
  const input = await sniff(job.inputPath);
  let root = job.inputPath;
  let isDir = false;
  if (input.format === 'zip') {
    ctx.progress('解包');
    const extracted = await ctx.rea('extract-artifact', [job.inputPath], job.filename);
    const outputRoot = extracted?.normalized_result?.output_root;
    if (typeof outputRoot === 'string' && resolve(outputRoot).startsWith(resolve(job.dir) + sep)) {
      root = resolve(outputRoot);
      isDir = true;
    } else {
      ctx.items.push({ object: job.filename, kind: 'other', bytes: input.size });
    }
  }
  const targets = (isDir || input.format !== 'zip') ? await plan(ctx, root, isDir) : { js: null, dotnet: [], pyinstaller: [], pyc: [] };
  if (targets.js) await analyzeJavascript(ctx, targets.js);
  for (const target of targets.dotnet) await analyzeDotnet(ctx, target);
  const modules = [];
  for (const target of targets.pyinstaller) await analyzePyInstaller(ctx, target, modules);
  for (const [index, target] of targets.pyc.entries()) {
    ctx.progress('解析程序', { done: index + 1, total: targets.pyc.length, unit: '文件' });
    await analyzePyc(ctx, target.abs, target.rel, target.module, modules);
  }
  if (targets.pyc.length && modules.length) ctx.detected.add('python');
  linkPythonImports(ctx, modules);

  ctx.progress('转换记录');
  const finished = ctx.records.finish(job.maxRecords);
  const recordsOut = createWriteStream(join(job.dir, 'out', 'records.jsonl'));
  for (const record of finished.records) {
    if (!recordsOut.write(`${canonicalJson(record)}\n`)) await once(recordsOut, 'drain');
  }
  recordsOut.end();
  await once(recordsOut, 'finish');
  if (finished.truncated) ctx.limit(`记录超过 ${job.maxRecords} 条，只保留前 ${finished.records.length} 条`);
  if (ctx.items.length > job.maxInventory) {
    ctx.skipped.push({ object: '*', reason: `清单超过 ${job.maxInventory} 项，后面的没列` });
    ctx.items.length = job.maxInventory;
  }

  const failed = ctx.analyzers.filter((entry) => entry.error);
  let outcome = { state: 'succeeded', error_code: null, error_message: null };
  if (ctx.analyzers.length && failed.length === ctx.analyzers.length) {
    outcome = { state: 'failed', error_code: 'analyzer_failed', error_message: `REA 分析失败（${failed[0].error}）` };
  } else if (failed.length || finished.truncated) {
    outcome = { state: 'partial', error_code: null, error_message: null };
  }
  const result = {
    artifact: { sha256: job.sha256, size: input.size, filename: job.filename, detected: [...ctx.detected].join('+') || 'unknown' },
    provider: job.provider,
    rea_version: job.reaVersion,
    analyzers: ctx.analyzers,
    inventory: { items: ctx.items, skipped: ctx.skipped.slice(0, job.maxInventory) },
    limitations: ctx.renderedLimitations(),
    unknowns: ctx.unknowns,
    records: finished.counts,
    raw: await writeRaw(ctx),
    complete: outcome.state === 'succeeded',
    truncated: finished.truncated,
  };
  if (finished.truncated) result.coverage_pct = Math.floor((finished.records.length * 1000) / finished.total) / 10;
  await writeFile(join(job.dir, 'out', 'result.json'), JSON.stringify(result));
  return outcome;
}
