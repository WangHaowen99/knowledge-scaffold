import { test } from 'node:test';
import assert from 'node:assert/strict';
import { workbenchAutoFocus } from '../frontend/src/workbench-focus.js';

test('工作台：领域问题选满后仍停留在领域分析，直到访谈显式启动', () => {
  assert.equal(workbenchAutoFocus({
    analyzeUnlocked: true,
    hasTables: true,
    analyzeDone: true,
    interviewUnlocked: true,
    interviewStarted: false,
    interviewDone: false,
    generateDone: false,
    stage: 'competency',
  }), 'analyze');

  assert.equal(workbenchAutoFocus({
    analyzeUnlocked: true,
    hasTables: true,
    analyzeDone: true,
    interviewUnlocked: true,
    interviewStarted: true,
    interviewDone: false,
    generateDone: false,
    stage: 'interview',
  }), 'interview');
});

test('工作台：仅有文档且尚未分析时保持原有上传页焦点', () => {
  assert.equal(workbenchAutoFocus({
    analyzeUnlocked: true,
    hasTables: false,
    analyzeDone: false,
    interviewUnlocked: false,
    interviewStarted: false,
    interviewDone: false,
    generateDone: false,
    stage: 'ingest',
  }), 'ingest');
});
