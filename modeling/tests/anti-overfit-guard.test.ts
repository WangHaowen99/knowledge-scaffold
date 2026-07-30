import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { scanForEvaluationLeakage } from '../scripts/anti-overfit-guard.js';

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), 'modeling-leakage-'));
  const target = path.join(root, 'src');
  await mkdir(target, { recursive: true });
  return { root, target };
}

test('防过拟合门禁识别外部清单声明的敏感词，但报告不回显原文', async () => {
  const { root, target } = await fixture();
  const corpusPath = path.join(root, 'corpus.json');
  const sensitiveTerm = '仅存在于验收项目的内部代号';
  await writeFile(corpusPath, JSON.stringify({ sensitiveTerms: [sensitiveTerm], cases: [] }));
  await writeFile(path.join(target, 'prompt.ts'), `export const prompt = ${JSON.stringify(sensitiveTerm)};`);

  const result = await scanForEvaluationLeakage({ corpusPath, targetPaths: [target] });

  assert.equal(result.findings.length, 1);
  assert.equal(result.findings[0]?.kind, 'sensitive_term');
  assert.equal(result.findings[0]?.file.endsWith('prompt.ts'), true);
  assert.equal(JSON.stringify(result).includes(sensitiveTerm), false);
});

test('防过拟合门禁识别从验收答案复制的长片段', async () => {
  const { root, target } = await fixture();
  const corpusPath = path.join(root, 'corpus.json');
  const copied = '先核对输入约束再执行确定性计算并返回可追溯证据';
  await writeFile(corpusPath, JSON.stringify({
    cases: [{ question: '如何完成本次计算？', groundTruth: copied }],
  }));
  await writeFile(path.join(target, 'workflow.ts'), `const hiddenAnswer = ${JSON.stringify(copied)};`);

  const result = await scanForEvaluationLeakage({
    corpusPath,
    targetPaths: [target],
    minFingerprintChars: 18,
  });

  assert.equal(result.findings.some((item) => item.kind === 'corpus_fingerprint'), true);
  assert.equal(JSON.stringify(result).includes(copied), false);
});

test('防过拟合门禁忽略短小通用措辞和非源码文件', async () => {
  const { root, target } = await fixture();
  const corpusPath = path.join(root, 'corpus.json');
  await writeFile(corpusPath, JSON.stringify({
    cases: [{ question: '请计算结果', groundTruth: '返回结果' }],
  }));
  await writeFile(path.join(target, 'workflow.ts'), 'export const prompt = "请计算结果并返回结果";');
  await writeFile(path.join(target, 'bundle.map'), '一段足够长但只存在于构建产物的验收文本');

  const result = await scanForEvaluationLeakage({
    corpusPath,
    targetPaths: [target],
    minFingerprintChars: 18,
  });

  assert.deepEqual(result.findings, []);
  assert.equal(result.scannedFiles, 1);
});
