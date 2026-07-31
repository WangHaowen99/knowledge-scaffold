import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import ReleasePanel, {
  releaseGateItems,
  releaseIsReady,
} from '../frontend/src/ReleasePanel.jsx';
import {
  acceptPackageWarnings,
  exportModelingAuditUrl,
  releaseProject,
} from '../frontend/src/api.js';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');
const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

const readyRelease = {
  available: true,
  ready: true,
  released: false,
  version: '1.2.9',
  gates: [
    { id: 'package_check', label: '内容检查', passed: true, detail: '当前版本已通过' },
    { id: 'flowchart', label: '流程图确认', passed: true, detail: '专家已确认' },
    { id: 'evaluation', label: '评测', passed: true, detail: '3/3 通过' },
  ],
};

test('发布面板只显示三项检查和简短操作', () => {
  const html = renderToStaticMarkup(React.createElement(ReleasePanel, {
    projectId: 'project-a',
    release: readyRelease,
  }));
  for (const text of ['发布检查', '内容检查', '流程图确认', '评测', '当前 1.2.9']) {
    assert.match(html, new RegExp(text));
  }
  assert.equal((html.match(/<li/g) || []).length, 3);
  assert.match(html, />发布<\/button>/);
  assert.match(html, /导出建模审计包/);
  assert.doesNotMatch(html, /disabled=""/);
});

test('建模审计包使用项目隔离的受保护下载地址', () => {
  assert.equal(exportModelingAuditUrl('项目/一'), '/api/projects/%E9%A1%B9%E7%9B%AE%2F%E4%B8%80/audit/export');
});

test('未满足检查时禁用发布', () => {
  const release = {
    ...readyRelease,
    ready: false,
    gates: readyRelease.gates.map((gate, index) => ({ ...gate, passed: index !== 0 })),
  };
  const html = renderToStaticMarkup(React.createElement(ReleasePanel, {
    projectId: 'project-a',
    release,
  }));
  assert.match(html, />暂不可发布<\/button>/);
  assert.match(html, /disabled=""/);
  assert.equal(releaseIsReady(release), false);
});

test('发布面板展示待再次确认的已知限制', () => {
  const release = {
    ...readyRelease,
    ready: false,
    revision: 'revision-a',
    gates: [...readyRelease.gates, {
      id: 'known_limitations', label: '已知限制确认', passed: false, detail: '1 条警告待发布确认',
    }],
    warningReview: {
      warningTotal: 1, readyForRelease: false,
      items: [{
        fingerprint: 'fingerprint-a', accepted: true, releaseConfirmed: false,
        finding: { category: '口径待确认', target: 'metrics/score.md', message: '边界口径尚未确认' },
      }],
    },
  };
  const html = renderToStaticMarkup(React.createElement(ReleasePanel, {
    projectId: 'project-a', release,
  }));
  assert.match(html, /发布已知限制/);
  assert.match(html, /边界口径尚未确认/);
  assert.match(html, /确认已知限制/);
  assert.match(html, />暂不可发布<\/button>/);
});

test('没有发布详情时从流程状态生成检查项', () => {
  const steps = [
    { id: 'flowchart', status: 'completed' },
    { id: 'validate', status: 'completed' },
    { id: 'evaluate', status: 'completed' },
  ];
  assert.deepEqual(releaseGateItems(null, steps).map((gate) => [gate.id, gate.passed]), [
    ['package_check', true],
    ['flowchart', true],
    ['evaluation', true],
  ]);
  assert.equal(releaseIsReady(null, steps), true);
});

test('发布请求使用项目地址和 POST', async () => {
  const calls: Array<{ url: string; options: RequestInit }> = [];
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ version: '1.2.10', revision: 'abc' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;

  assert.deepEqual(await releaseProject('企业银行/营销'), { version: '1.2.10', revision: 'abc' });
  assert.equal(calls[0].url, '/api/projects/%E4%BC%81%E4%B8%9A%E9%93%B6%E8%A1%8C%2F%E8%90%A5%E9%94%80/release');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.body, '{}');
});

test('警告确认请求携带修订、警告指纹、理由和阶段', async () => {
  const calls: Array<{ url: string; options: RequestInit }> = [];
  globalThis.fetch = (async (url: string | URL | Request, options: RequestInit = {}) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify({ warningReview: { readyForRelease: true } }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;

  await acceptPackageWarnings('project-a', 'revision-a', ['fingerprint-a'], '确认影响', 'release');
  assert.equal(calls[0].url, '/api/projects/project-a/package/warnings/accept');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.body, JSON.stringify({
    revision: 'revision-a', fingerprints: ['fingerprint-a'], note: '确认影响', phase: 'release',
  }));
});
