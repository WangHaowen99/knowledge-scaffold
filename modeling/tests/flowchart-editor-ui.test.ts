import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';

import {
  AddFlowchartCard,
  FlowchartAddDialog,
  FlowchartEditor,
} from '../frontend/src/flowchart-editor/index.js';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('流程列表末尾卡片同时提供 AI 推荐和空白新建入口', () => {
  const html = renderToStaticMarkup(React.createElement(AddFlowchartCard, {
    onRecommend: () => {},
    onCreateBlank: () => {},
  }));

  assert.match(html, /继续添加流程图/);
  assert.match(html, /AI 推荐新流程/);
  assert.match(html, /新建空白流程/);
  assert.match(html, /aria-label="AI 推荐新流程"/);
  assert.match(html, /aria-label="新建空白流程"/);
});

test('流程图有未保存修改时继续添加卡片说明禁用原因', () => {
  const html = renderToStaticMarkup(React.createElement(AddFlowchartCard, {
    disabled: true,
    disabledReason: '请先保存当前流程图修改，再继续添加',
    onRecommend: () => {},
    onCreateBlank: () => {},
  }));

  assert.match(html, /请先保存当前流程图修改，再继续添加/);
  assert.match(html, /disabled=""/);
});

test('继续添加弹窗同时展示目标、资料候选和 AI 自定义入口', () => {
  const html = renderToStaticMarkup(React.createElement(FlowchartAddDialog, {
    open: true,
    modelingGoal: { problem: '形成可执行的授信审批知识' },
    candidates: [
      { candidateId: 'process_candidate_1', title: '授信审批', rationale: '资料包含初审步骤' },
      { candidateId: 'process_candidate_2', title: '授信审批', rationale: '资料包含终审步骤' },
    ],
    onAddCandidate: () => {},
    onAddAi: () => {},
  }));
  assert.match(html, /形成可执行的授信审批知识/);
  assert.match(html, /授信审批/);
  assert.match(html, /data-candidate-id="process_candidate_1"/);
  assert.match(html, /data-candidate-id="process_candidate_2"/);
  assert.match(html, /AI 生成新流程/);
});

test('A 方案同时显示流程列表、编辑画布、工具栏和属性栏', () => {
  const html = renderToStaticMarkup(React.createElement(FlowchartEditor, {
    document: {
      diagrams: [{
        id: 'approval',
        title: '审批流程',
        nodes: [
          { id: 'start', kind: 'start', label: '收到申请' },
          { id: 'finish', kind: 'end', label: '完成审批' },
        ],
        edges: [{ id: 'edge-1', from: 'start', to: 'finish', kind: 'sequence', label: '下一步' }],
        layout: { direction: 'TB', positions: { start: { x: 80, y: 40 }, finish: { x: 80, y: 220 } } },
      }],
    },
    savedRevision: 3,
    onSave: async () => ({ savedRevision: 4 }),
    onRecommendDiagram: () => {},
    onCreateBlankDiagram: () => {},
  }));

  assert.match(html, /aria-label="流程图列表"/);
  assert.match(html, /审批流程/);
  assert.match(html, /继续添加流程图/);
  assert.match(html, /aria-label="流程图编辑画布"/);
  assert.match(html, /新增业务步骤/);
  assert.match(html, /新增判断节点/);
  assert.match(html, /撤销/);
  assert.match(html, /重做/);
  assert.match(html, /自动布局/);
  assert.match(html, /aria-label="删除当前业务流程"/);
  assert.match(html, /保存草稿/);
  assert.match(html, /流程属性/);
  assert.match(html, /流程名称/);
  assert.match(html, /参与角色/);
  assert.match(html, /流程输入/);
  assert.match(html, /异常处理/);
  assert.match(html, /流程交接/);
  assert.match(html, /触发条件请编辑开始节点/);
  assert.match(html, /草稿修订 3/);
});

test('提交流程图期间锁定列表、工具栏和属性输入', () => {
  const html = renderToStaticMarkup(React.createElement(FlowchartEditor, {
    document: {
      diagrams: [{
        id: 'approval', title: '审批流程',
        nodes: [{ id: 'start', kind: 'start', label: '收到申请' }], edges: [],
      }],
    },
    savedRevision: 3,
    disabled: true,
  }));

  assert.match(html, /data-editor-locked="true"/);
  assert.match(html, /<input disabled="" value="审批流程"/);
  assert.match(html, /aria-label="新增业务步骤" disabled=""/);
  assert.match(html, /flowchart-editor-rail__item[^>]*disabled=""/);
});
