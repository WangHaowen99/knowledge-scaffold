import assert from 'node:assert/strict';
import { test } from 'node:test';

import { validateMermaidSource } from '../src/workflow/mermaid-source-contract.js';

const PATH = 'diagrams/flight_anomaly_handling.mmd';

function labelRuleIssues(line: string) {
  return validateMermaidSource(PATH, `flowchart TD\n  ${line}\n`)
    .filter((issue) => issue.ruleId === 'mermaid.label-quoting');
}

// 判定基准取自本仓库实装的 mermaid 11.17.0 实际解析结果：未加引号的标签里
// ( ) [ ] { } | " 会被词法器当成形状或连线分隔符而报 Parse error，其余字符
// （≥、？、全角括号、<br/>、中文顿号）都合法。规则只拦真正渲染不出来的写法。
const REJECTED_BY_MERMAID = [
  'CHK{X ≥ MIN(TURN, COMPANY)？} --> Z',
  'REL -->|往返| CHK_TURN{X ≥ MIN(TURN, COMPANY)？}',
  'A[计算 MIN(TURN, COMPANY)] --> Z',
  'A -->|超限(含税)| Z',
  'A([收到订单(加急)]) --> Z',
  'A[[子流程(含税)]] --> Z',
  'A{{六边形(备注)}} --> Z',
  'A>提示(重要)] --> Z',
  'A -.-> B[结束(含税)]',
  'CHK{取值 [A] 判断} --> Z',
  'CHK[集合 {A,B}] --> Z',
  'CHK{A | B} --> Z',
  'CHK{他说"好"} --> Z',
];

const ACCEPTED_BY_MERMAID = [
  'CHK{"X ≥ MIN(TURN, COMPANY)？"} --> Z',
  'REL -->|往返| CHK_TURN{"X ≥ MIN(TURN, COMPANY)？"}',
  'A -->|"超限(含税)"| Z',
  'A(["收到订单(加急)"]) --> Z',
  'A[("数据表(主)")] --> Z',
  'CHK{"取值 [A] {B} A|B"} --> Z',
  'A["含 ] 的文字"] --> Z',
  'A{"含 } 的文字"} --> Z',
  'CHK{X ≥ MIN（TURN、COMPANY）？} --> Z',
  'CHK{X ≥ 30 分钟？} --> Z',
  'CHK{条件：A、B 满足？} --> Z',
  'A[第一行<br/>第二行] --> Z',
  'A[处理（自动）] --> Z',
  'A[开始] --> B[结束]',
  'A[开始] --> B[中间] --> C[结束]',
  'A[标签] -.-> B{判断} --> C([完成])',
  'node-1[开始] --> node-2[结束]',
  'A ==> B[结束]',
  'A --- B[结束]',
  'A -- 超限(含税) --> Z',
  'A>提示] --> Z',
  'A -->|往返| B',
  'start --> finish',
];

test('未加引号的流程图文字含 Mermaid 结构字符时判为违规', () => {
  for (const line of REJECTED_BY_MERMAID) {
    const issues = labelRuleIssues(line);
    assert.equal(issues.length, 1, `应判违规但未命中：${line}`);
    assert.equal(issues[0]!.path, PATH);
    assert.match(issues[0]!.message, /第 2 行/u);
  }
});

test('加引号或不含结构字符的写法不判为违规', () => {
  for (const line of ACCEPTED_BY_MERMAID) {
    assert.deepEqual(labelRuleIssues(line), [], `误判为违规：${line}`);
  }
});

test('双引号内部再出现双引号单独给出修复建议', () => {
  const issues = labelRuleIssues('CHK{"他说"好"了"} --> Z');
  assert.equal(issues.length, 1);
  assert.match(issues[0]!.message, /未转义的双引号/u);
  assert.match(issues[0]!.suggestion, /「」/u);
});

test('%% 证据注释里的括号不判为违规', () => {
  const source = [
    '%% evidence: material:doc_demo#12 (中转衔接)',
    'flowchart TD',
    '  A["开始"] --> B["结束"]',
    '',
  ].join('\n');
  assert.deepEqual(validateMermaidSource(PATH, source), []);
});

test('违规条数封顶并说明剩余数量', () => {
  const lines = Array.from({ length: 14 }, (_, index) => `  N${index}[计算 MIN(A, B)] --> Z`);
  const issues = validateMermaidSource(PATH, ['flowchart TD', ...lines, ''].join('\n'));
  assert.equal(issues.length, 11);
  assert.match(issues.at(-1)!.message, /还有 4 处同类流程图文字/u);
});

test('非 diagrams 路径和其他规则不受影响', () => {
  assert.deepEqual(validateMermaidSource('operations/index.md', 'A[计算 MIN(A, B)]'), []);
  const declaration = validateMermaidSource(PATH, 'graph TD\n  A[计算 MIN(A, B)] --> Z\n');
  assert.deepEqual(declaration.map((issue) => issue.ruleId), [
    'mermaid.flowchart-declaration',
    'mermaid.label-quoting',
  ]);
});
