import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import {
  binAnchor,
  canonicalJson,
  decodeJobHeader,
  encodeJobHeader,
  encodeSegment,
  jobScope,
  recordHash,
  relationAnchor,
  sha256Text,
} from '../src/anchors.mjs';

// 和 spec.py 共用的向量；单独拷出这个目录跑测试时用 EVIDENCE_VECTORS 指过去
const vectorsPath = process.env.EVIDENCE_VECTORS
  ?? fileURLToPath(new URL('../../../backend/tests/fixtures/evidence_vectors.json', import.meta.url));
const vectors = JSON.parse(readFileSync(vectorsPath, 'utf8'));

test('规范 JSON 与 Python 一致', () => {
  for (const item of vectors.canonical) {
    assert.equal(canonicalJson(item.value), item.json);
    assert.equal(sha256Text(canonicalJson(item.value)), item.sha256);
  }
});

test('名字段编码', () => {
  for (const item of vectors.segments) assert.equal(encodeSegment(item.text), item.encoded);
});

test('锚点', () => {
  for (const item of vectors.bin_anchor) assert.equal(binAnchor(...item.args), item.anchor);
  for (const item of vectors.relation) assert.equal(relationAnchor(...item.args), item.anchor);
});

test('记录哈希', () => {
  for (const item of vectors.record) {
    assert.equal(recordHash(item.record), item.hash);
    assert.equal(recordHash({ ...item.record, hash: 'x' }), item.hash);
  }
});

test('票据范围与任务头', () => {
  for (const item of vectors.job_scope) assert.equal(jobScope(item.job_id), item.scope);
  for (const item of vectors.job_header) {
    assert.equal(encodeJobHeader(item.spec), item.header);
    assert.deepEqual(decodeJobHeader(item.header), item.spec);
  }
});

test('浮点数和增补平面键', () => {
  assert.throws(() => canonicalJson({ a: 1.5 }));
  // 码点序：U+FF61 < U+1F600，UTF-16 码元序会反过来
  assert.equal(canonicalJson({ '\u{1F600}': 1, '｡': 2 }), '{"｡":2,"😀":1}');
});
