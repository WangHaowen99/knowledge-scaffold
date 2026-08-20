import assert from 'node:assert/strict';
import test from 'node:test';

import {
  importMermaidFlowchart,
  MermaidImportError,
} from '../src/flowchart/mermaid-import.js';
import { renderDiagramFiles } from '../src/flowchart/projection.js';

const emptyDocument = { diagrams: [] };

test('导入 flowchart 时保留节点 ID、形状、连线文字和反向布局', () => {
  const imported = importMermaidFlowchart(emptyDocument, {
    title: '订单处理流程',
    filename: 'order-flow.mmd',
    source: [
      'flowchart RL',
      '  start([收到订单]) --> review{信息完整？}',
      '  review -->|是| finish([完成处理])',
      '  review -.->|否| supplement[补充信息]',
      '  supplement --> review',
    ].join('\n'),
  });

  assert.equal(imported.summary.direction, 'RL');
  assert.equal(imported.summary.nodeCount, 4);
  assert.equal(imported.summary.edgeCount, 4);
  assert.deepEqual(imported.diagram.nodes.map((node) => [node.id, node.kind]), [
    ['start', 'start'], ['review', 'decision'], ['finish', 'end'], ['supplement', 'action'],
  ]);
  assert.equal(imported.diagram.edges[1].label, '是');
  assert.equal(imported.diagram.edges[1].conditionText, '是');
  assert.equal(imported.diagram.edges[1].kind, 'condition');
  assert.equal(imported.diagram.edges[2].kind, 'exception');
  assert.deepEqual(imported.diagram.importSource, {
    kind: 'mermaid', diagramType: 'flowchart', filename: 'order-flow.mmd',
  });
  assert.match(renderDiagramFiles(imported.document)['diagrams/订单处理流程.mmd'], /^flowchart RL/mu);
});

test('graph、分号和 subgraph 可导入，分组会给出明确说明', () => {
  const imported = importMermaidFlowchart(emptyDocument, {
    title: '资料整理流程',
    source: [
      'graph TD',
      '  subgraph 准备阶段',
      '    a[收集资料]; b[整理资料]',
      '    a -- 完成后 --> b',
      '  end',
    ].join('\n'),
  });

  assert.equal(imported.summary.diagramType, 'graph');
  assert.equal(imported.summary.direction, 'TB');
  assert.equal(imported.summary.subgraphCount, 1);
  assert.equal(imported.notices[0].code, 'subgraph_flattened');
  assert.deepEqual(imported.diagram.edges.map((edge) => edge.label), ['完成后']);
});

test('行尾 Mermaid 注释不会被误判为连线语法', () => {
  const imported = importMermaidFlowchart(emptyDocument, {
    title: '带注释流程',
    source: 'flowchart TD\n  a[开始] --> b[结束] %% 这是注释',
  });
  assert.equal(imported.diagram.edges.length, 1);
  assert.equal(imported.diagram.nodes.length, 2);
});

test('不支持的图表类型返回类型、行列、源码和修复建议', () => {
  assert.throws(
    () => importMermaidFlowchart(emptyDocument, {
      title: '状态变化流程', source: 'sequenceDiagram\n  participant A',
    }),
    (cause) => {
      assert.ok(cause instanceof MermaidImportError);
      assert.deepEqual(cause.diagnostic, {
        code: 'MERMAID_DIAGRAM_TYPE_UNSUPPORTED',
        category: '图表类型不支持',
        message: '仅支持 flowchart/graph，当前图表类型为 sequenceDiagram',
        suggestion: '请将文件转换为 flowchart 或 graph，并在首行声明布局方向，例如 flowchart TD。',
        line: 1,
        column: 1,
        sourceLine: 'sequenceDiagram',
      });
      return true;
    },
  );
});

test('节点形状未闭合时指出准确源码位置并保留可操作建议', () => {
  assert.throws(
    () => importMermaidFlowchart(emptyDocument, {
      title: '错误示例流程', source: 'flowchart TD\n  start[开始 --> finish[结束]',
    }),
    (cause) => {
      assert.ok(cause instanceof MermaidImportError);
      assert.equal(cause.diagnostic.category, 'Mermaid 语法错误');
      assert.equal(cause.diagnostic.line, 2);
      assert.equal(cause.diagnostic.column, 8);
      assert.equal(cause.diagnostic.sourceLine, '  start[开始 --> finish[结束]');
      assert.match(cause.diagnostic.message, /形状没有闭合/u);
      assert.match(cause.diagnostic.suggestion, /补充 \]/u);
      return true;
    },
  );
});

test('节点纯文本中的普通括号不会被误报为形状未闭合', () => {
  const imported = importMermaidFlowchart(emptyDocument, {
    title: '括号文字流程',
    source: 'flowchart TD\n  a[处理（自动）] --> b[完成 (Done)]',
  });
  assert.deepEqual(imported.diagram.nodes.map((node) => node.label), [
    '处理（自动）', '完成 (Done)',
  ]);
});

test('安全指令、HTML、重复流程名称和错误扩展名在写入前被拒绝', () => {
  const current = importMermaidFlowchart(emptyDocument, {
    title: '已有流程', source: 'flowchart TD\n  a[开始] --> b[结束]',
  }).document;
  const cases = [
    {
      input: { title: '点击流程', source: 'flowchart TD\n  a[开始]\n  click a "https://example.com"' },
      code: 'MERMAID_DIRECTIVE_FORBIDDEN',
    },
    {
      input: { title: '标记流程', source: 'flowchart TD\n  a[<b>开始</b>] --> b[结束]' },
      code: 'MERMAID_HTML_FORBIDDEN',
    },
    {
      input: { title: '已有流程', source: 'flowchart TD\n  a[开始]' },
      code: 'MERMAID_TITLE_DUPLICATE',
    },
    {
      input: { title: '文件流程', filename: 'flow.txt', source: 'flowchart TD\n  a[开始]' },
      code: 'MERMAID_FILE_TYPE_UNSUPPORTED',
    },
  ];
  for (const item of cases) {
    assert.throws(() => importMermaidFlowchart(current, item.input), (cause) => {
      assert.ok(cause instanceof MermaidImportError);
      assert.equal(cause.diagnostic.code, item.code);
      assert.ok(cause.diagnostic.suggestion);
      return true;
    });
  }
});

test('文件开头的 Mermaid 初始化指令也会被拒绝并定位到原行', () => {
  assert.throws(() => importMermaidFlowchart(emptyDocument, {
    title: '初始化流程',
    source: '%%{init: {"securityLevel":"loose"}}%%\nflowchart TD\n  a[开始]',
  }), (cause) => {
    assert.ok(cause instanceof MermaidImportError);
    assert.equal(cause.diagnostic.code, 'MERMAID_INIT_DIRECTIVE_FORBIDDEN');
    assert.equal(cause.diagnostic.line, 1);
    assert.match(cause.diagnostic.sourceLine || '', /^%%\{init/u);
    return true;
  });
});

test('显式重复节点 ID 被拒绝，普通连线引用允许复用已声明节点', () => {
  const valid = importMermaidFlowchart(emptyDocument, {
    title: '节点引用流程',
    source: 'flowchart LR\n  a[开始] --> b[处理]\n  b --> c[结束]',
  });
  assert.equal(valid.diagram.nodes.length, 3);

  assert.throws(() => importMermaidFlowchart(emptyDocument, {
    title: '重复节点流程',
    source: 'flowchart LR\n  a[开始]\n  a[再次声明]',
  }), (cause) => cause instanceof MermaidImportError
    && cause.diagnostic.code === 'MERMAID_DUPLICATE_NODE_ID');
});
