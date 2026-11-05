// 端到端验收：对一个跑着的旁路容器走一遍 POST → 轮询 → result / records / raw → DELETE，并按 spec 核对记录。
// 用法：node test/e2e.mjs <地址> <目标程序> [源ID] [保存目录]
// 旁路容器断网时，用 docker run --network container:<旁路容器> 起一个 node 容器跑它。
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, join } from 'node:path';

import { canonicalJson, recordHash } from '../src/anchors.mjs';
import { RECORD_TYPES } from '../src/records.mjs';

const [base, file, source = 'sample-app', saveDir] = process.argv.slice(2);
const body = readFileSync(file);
const sha256 = createHash('sha256').update(body).digest('hex');
const id = `ej-${Date.now().toString(36)}`;
const started = Date.now();
const query = new URLSearchParams({ id, source, filename: basename(file), sha256, max_seconds: '900', max_records: '200000' });
let response = await fetch(`${base}/v1/jobs?${query}`, { method: 'POST', body });
if (response.status !== 202) throw new Error(`POST ${response.status} ${await response.text()}`);

const phases = [];
let status;
for (;;) {
  status = await (await fetch(`${base}/v1/jobs/${id}`)).json();
  const label = `${status.state}/${status.phase}`;
  if (phases.at(-1) !== label) phases.push(label);
  if (['succeeded', 'partial', 'failed', 'cancelled'].includes(status.state)) break;
  await new Promise((resolve) => setTimeout(resolve, 300));
}
const elapsed = Date.now() - started;
const problems = [];
let result = null;
let records = [];
if (status.state === 'succeeded' || status.state === 'partial') {
  result = await (await fetch(`${base}/v1/jobs/${id}/result`)).json();
  const text = await (await fetch(`${base}/v1/jobs/${id}/records`)).text();
  records = text.split('\n').filter(Boolean).map((line) => JSON.parse(line));
  const prefix = `bin:${source}/`;
  const ids = new Set();
  for (const record of records) {
    if (!record.id.startsWith(prefix)) problems.push(`前缀不对 ${record.id}`);
    if (ids.has(record.id)) problems.push(`锚点重复 ${record.id}`);
    ids.add(record.id);
    if (!RECORD_TYPES.includes(record.type)) problems.push(`类型不对 ${record.id}`);
    if (record.hash !== recordHash(record)) problems.push(`hash 不对 ${record.id}`);
    try {
      canonicalJson(record);
    } catch {
      problems.push(`有浮点数 ${record.id}`);
    }
    if (!['observed', 'inferred'].includes(record.evidence?.confidence)) problems.push(`置信度不对 ${record.id}`);
  }
  for (const record of records) {
    if (record.parent && !ids.has(record.parent)) problems.push(`parent 悬空 ${record.id}`);
    if (record.type === 'relation' && (!ids.has(record.from) || (record.to && !ids.has(record.to)))) problems.push(`关系端点悬空 ${record.id}`);
  }
  const counted = Object.values(result.records).reduce((a, b) => a + b, 0);
  if (counted !== records.length) problems.push(`result.records 合计 ${counted}，实收 ${records.length}`);
  for (const raw of result.raw) {
    const data = Buffer.from(await (await fetch(`${base}/v1/jobs/${id}/raw/${encodeURIComponent(raw.name)}`)).arrayBuffer());
    if (data.length !== raw.size || createHash('sha256').update(data).digest('hex') !== raw.sha256) problems.push(`原始结果对不上 ${raw.name}`);
    if (saveDir) {
      mkdirSync(join(saveDir, 'raw'), { recursive: true });
      writeFileSync(join(saveDir, 'raw', raw.name), data);
    }
  }
  if (saveDir) {
    writeFileSync(join(saveDir, 'result.json'), JSON.stringify(result, null, 1));
    writeFileSync(join(saveDir, 'records.jsonl'), text);
  }
}
response = await fetch(`${base}/v1/jobs/${id}`, { method: 'DELETE' });
const gone = (await fetch(`${base}/v1/jobs/${id}`)).status === 404;

const kinds = {};
for (const record of records) {
  const key = `${record.type}:${record.type === 'relation' ? record.name : record.attrs?.kind}`;
  kinds[key] = (kinds[key] ?? 0) + 1;
}
console.log(JSON.stringify({
  file: basename(file), state: status.state, error: status.error_code, ms: elapsed, phases,
  detected: result?.artifact?.detected, analyzers: result?.analyzers?.map((item) => `${item.command}${item.error ? `!${item.error}` : ''}:${item.ms}ms`),
  counts: result?.records, kinds, inventory: result?.inventory?.items?.length, raw: result?.raw?.map((item) => `${item.name}:${item.size}`),
  unknowns: result?.unknowns, limitations: result?.limitations?.length, examples: records.filter((r) => r.type !== 'relation').slice(0, 4).map((r) => r.id),
  deleted: response.status === 200 && gone, problems,
}, null, 1));
