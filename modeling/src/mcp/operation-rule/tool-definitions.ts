import type { ModelingMcpToolDefinition } from '../toolset.js';
import { KEY_SCHEMA_DESCRIPTION, KEY_SCHEMA_PATTERN } from '../../workflow/keys.js';

const KEY_PATTERN_BODY = KEY_SCHEMA_PATTERN.slice(1, -1);
const OPERATION_ID_PATTERN = `^operation:${KEY_PATTERN_BODY}$`;
const RULE_ID_PATTERN = `^rule:${KEY_PATTERN_BODY}$`;

export const OPERATION_RULE_TOOL_NAMES = new Set([
  'list_ontology_operations',
  'get_ontology_operation',
  'create_ontology_operation',
  'update_ontology_operation',
  'delete_ontology_operation',
  'list_ontology_rules',
  'get_ontology_rule',
  'create_ontology_rule',
  'update_ontology_rule',
  'delete_ontology_rule',
  'validate_ontology_operation_rule_change',
]);

export const OPERATION_RULE_TOOL_DEFINITIONS: readonly ModelingMcpToolDefinition[] = [
  readListTool('operation'),
  readGetTool('operation'),
  readListTool('rule'),
  readGetTool('rule'),
  {
    name: 'validate_ontology_operation_rule_change',
    title: '校验 Operation/Rule 修改',
    description: '同步分析一个 Operation 或 Rule 的新增、修改或删除；返回阻塞点和修改建议，不写入。',
    toolset: 'change',
    requiredScope: 'ontology.change.validate',
    internalToolName: 'validate_ontology_operation_rule_change',
    inputSchema: {
      type: 'object',
      required: ['expected_revision', 'resource_type', 'action', 'resource_id'],
      properties: {
        expected_revision: { type: 'string' },
        resource_type: { type: 'string', enum: ['operation', 'rule'] },
        action: { type: 'string', enum: ['create', 'update', 'delete'] },
        resource_id: { type: 'string' },
        payload: { type: 'object' },
      },
      additionalProperties: false,
    },
  },
  writeCreateTool('operation'),
  writeUpdateTool('operation'),
  writeDeleteTool('operation'),
  writeCreateTool('rule'),
  writeUpdateTool('rule'),
  writeDeleteTool('rule'),
];

export function isOperationRuleTool(name: string): boolean {
  return OPERATION_RULE_TOOL_NAMES.has(name);
}

function readListTool(kind: 'operation' | 'rule'): ModelingMcpToolDefinition {
  const plural = kind === 'operation' ? 'operations' : 'rules';
  return {
    name: `list_ontology_${plural}`,
    title: `列出${kind === 'operation' ? '操作' : '规则'}`,
    description: `分页查询正式知识包 revision 中的 ${kind === 'operation' ? 'Operation' : 'Rule'}。`,
    toolset: 'read',
    requiredScope: 'ontology.read',
    internalToolName: `list_ontology_${plural}`,
    inputSchema: {
      type: 'object',
      properties: {
        revision: { type: 'string' },
        query: { type: 'string' },
        cursor: { type: 'string' },
        limit: { type: 'integer', minimum: 1, maximum: 200 },
      },
      additionalProperties: false,
    },
  };
}

function readGetTool(kind: 'operation' | 'rule'): ModelingMcpToolDefinition {
  const field = kind === 'operation' ? 'operation_id' : 'rule_id';
  return {
    name: `get_ontology_${kind}`,
    title: `读取${kind === 'operation' ? '操作' : '规则'}详情`,
    description: `读取正式知识包 revision 中的 ${kind === 'operation' ? 'Operation' : 'Rule'} 结构化详情。`,
    toolset: 'read',
    requiredScope: 'ontology.read',
    internalToolName: `get_ontology_${kind}`,
    inputSchema: {
      type: 'object',
      required: [field],
      properties: {
        [field]: { type: 'string', pattern: kind === 'operation' ? OPERATION_ID_PATTERN : RULE_ID_PATTERN },
        revision: { type: 'string' },
      },
      additionalProperties: false,
    },
  };
}

function writeCreateTool(kind: 'operation' | 'rule'): ModelingMcpToolDefinition {
  const operationProperties = {
    title: { type: 'string' },
    description: { type: 'string' },
    inputs: { type: 'array', items: { type: 'object' } },
    outputs: { type: 'array', items: { type: 'object' } },
    flow: { type: 'array', items: { type: 'string' } },
    related_objects: { type: 'array', items: { type: 'string' } },
    uses_rules: { type: 'array', items: { type: 'string' } },
    uses_metrics: { type: 'array', items: { type: 'string' } },
    notes: { type: 'array', items: { type: 'string' } },
  };
  const ruleProperties = {
    title: { type: 'string' },
    description: { type: 'string' },
    applies_to_objects: { type: 'array', items: { type: 'string' } },
    applies_to_operations: { type: 'array', items: { type: 'string' } },
    content: { type: 'string' },
    conditions: { type: 'array', items: { type: 'object' } },
    exceptions: { type: 'array', items: { type: 'string' } },
  };
  return {
    name: `create_ontology_${kind}`,
    title: `新增${kind === 'operation' ? '操作' : '规则'}`,
    description: '同步确定性校验通过后直接提交新的正式知识包 revision。',
    toolset: 'change',
    requiredScope: 'ontology.change.apply',
    internalToolName: `create_ontology_${kind}`,
    inputSchema: {
      type: 'object',
      required: ['expected_revision', 'key', 'title', ...(kind === 'operation'
        ? ['description', 'inputs', 'outputs', 'flow', 'related_objects']
        : ['content'])],
      properties: {
        expected_revision: { type: 'string' },
        key: { type: 'string', pattern: KEY_SCHEMA_PATTERN, description: KEY_SCHEMA_DESCRIPTION },
        ...(kind === 'operation' ? operationProperties : ruleProperties),
      },
      additionalProperties: false,
    },
  };
}

function writeUpdateTool(kind: 'operation' | 'rule'): ModelingMcpToolDefinition {
  const field = kind === 'operation' ? 'operation_id' : 'rule_id';
  return {
    name: `update_ontology_${kind}`,
    title: `修改${kind === 'operation' ? '操作' : '规则'}`,
    description: '对允许字段做 patch；同步确定性校验通过后直接提交正式 revision。',
    toolset: 'change',
    requiredScope: 'ontology.change.apply',
    internalToolName: `update_ontology_${kind}`,
    inputSchema: {
      type: 'object',
      required: ['expected_revision', field, 'changes'],
      properties: {
        expected_revision: { type: 'string' },
        [field]: { type: 'string', pattern: kind === 'operation' ? OPERATION_ID_PATTERN : RULE_ID_PATTERN },
        changes: { type: 'object', minProperties: 1 },
      },
      additionalProperties: false,
    },
  };
}

function writeDeleteTool(kind: 'operation' | 'rule'): ModelingMcpToolDefinition {
  const field = kind === 'operation' ? 'operation_id' : 'rule_id';
  return {
    name: `delete_ontology_${kind}`,
    title: `删除${kind === 'operation' ? '操作' : '规则'}`,
    description: '仅在不存在阻塞引用且确定性检查通过时删除并提交正式 revision。',
    toolset: 'change',
    requiredScope: 'ontology.change.apply',
    internalToolName: `delete_ontology_${kind}`,
    inputSchema: {
      type: 'object',
      required: ['expected_revision', field],
      properties: {
        expected_revision: { type: 'string' },
        [field]: { type: 'string', pattern: kind === 'operation' ? OPERATION_ID_PATTERN : RULE_ID_PATTERN },
      },
      additionalProperties: false,
    },
  };
}
