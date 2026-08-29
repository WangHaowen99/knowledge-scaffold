import { config } from '../config.js';
import type { Project } from '../types.js';
import { KEY_SCHEMA_DESCRIPTION, KEY_SCHEMA_PATTERN } from '../workflow/keys.js';
import { ObjectMcpService } from './object/service.js';
import { OperationRuleMcpService } from './operation-rule/service.js';
import {
  isOperationRuleTool,
  OPERATION_RULE_TOOL_DEFINITIONS,
} from './operation-rule/tool-definitions.js';
import type { ModelingMcpScope, ModelingMcpToolset } from './service.js';

export interface ModelingMcpToolDefinition {
  name: string;
  title: string;
  description: string;
  toolset: ModelingMcpToolset;
  requiredScope: ModelingMcpScope;
  inputSchema: Record<string, unknown>;
  outputSchema?: Record<string, unknown>;
  internalToolName: string;
}

const KEY_PATTERN_BODY = KEY_SCHEMA_PATTERN.slice(1, -1);
const OBJECT_ID_SCHEMA = {
  type: 'string',
  pattern: `^object:${KEY_PATTERN_BODY}$`,
  description: `对象 ID，格式 object:key；${KEY_SCHEMA_DESCRIPTION}`,
};

const OBJECT_TOOL_DEFINITIONS: readonly ModelingMcpToolDefinition[] = [
  {
    name: 'list_ontology_objects',
    title: '列出对象模型',
    description: '分页查询正式知识包 revision 中的对象列表。',
    toolset: 'read',
    requiredScope: 'ontology.read',
    internalToolName: 'list_ontology_objects',
    inputSchema: {
      type: 'object',
      properties: {
        revision: { type: 'string', description: '可选，指定当前读取的正式 revision。' },
        query: { type: 'string', description: '可选，对对象标题和关键字做筛选。' },
        sort: { type: 'string', description: '可选，排序字段。' },
        order: { type: 'string', enum: ['asc', 'desc'], description: '可选，排序方向。' },
        cursor: { type: 'string', description: '可选，分页游标。' },
        limit: { type: 'integer', minimum: 1, maximum: 200, description: '可选，单页大小。' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'get_ontology_object',
    title: '读取对象模型详情',
    description: '读取正式知识包 revision 中指定对象的元属性、字段定义和对象关系。',
    toolset: 'read',
    requiredScope: 'ontology.read',
    internalToolName: 'get_ontology_object',
    inputSchema: {
      type: 'object',
      required: ['object_id'],
      properties: {
        object_id: OBJECT_ID_SCHEMA,
        revision: { type: 'string', description: '可选，指定当前读取的正式 revision。' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'validate_ontology_object_change',
    title: '校验对象模型修改',
    description: '对对象元属性、字段和对象关系的修改做只校验预演，不写入正式 revision。',
    toolset: 'change',
    requiredScope: 'ontology.change.validate',
    internalToolName: 'validate_ontology_object_change',
    inputSchema: objectChangeSchema(),
  },
  {
    name: 'update_ontology_object',
    title: '修改对象模型',
    description: '对对象元属性、字段和对象关系做字段级修改；确定性校验通过后直接生成正式 revision。',
    toolset: 'change',
    requiredScope: 'ontology.change.apply',
    internalToolName: 'update_ontology_object',
    inputSchema: objectChangeSchema(),
  },
] as const;

export function toolDefinitionsFor(
  toolset: ModelingMcpToolset,
  scopes: readonly ModelingMcpScope[],
): ModelingMcpToolDefinition[] {
  return activeToolDefinitions()
    .filter((tool) => tool.toolset === toolset && scopes.includes(tool.requiredScope))
    .map((tool) => ({ ...tool, inputSchema: structuredClone(tool.inputSchema) }));
}

export function toolNamesByToolset(): Record<ModelingMcpToolset, string[]> {
  const definitions = activeToolDefinitions();
  return {
    read: definitions.filter((tool) => tool.toolset === 'read').map((tool) => tool.name),
    change: definitions.filter((tool) => tool.toolset === 'change').map((tool) => tool.name),
  };
}

export function toolDefinitionByName(name: string): ModelingMcpToolDefinition | null {
  const normalized = String(name ?? '').trim();
  return activeToolDefinitions().find((tool) => tool.name === normalized) ?? null;
}

export async function executeModelingMcpTool(input: {
  toolName: string;
  project: Project;
  username: string;
  tokenId: string;
  argumentsValue: Record<string, unknown>;
}): Promise<Record<string, unknown>> {
  const tool = toolDefinitionByName(input.toolName);
  if (!tool) throw new Error(`未知 MCP 工具: ${input.toolName}`);
  if (isOperationRuleTool(tool.name)) {
    return new OperationRuleMcpService().execute(
      tool.name,
      input.project,
      input.username,
      input.argumentsValue,
    );
  }
  return new ObjectMcpService().execute(
    tool.name,
    input.project,
    input.username,
    input.argumentsValue,
  );
}

function activeToolDefinitions(): readonly ModelingMcpToolDefinition[] {
  return config.mcp.operationRuleEnabled
    ? [...OBJECT_TOOL_DEFINITIONS, ...OPERATION_RULE_TOOL_DEFINITIONS]
    : OBJECT_TOOL_DEFINITIONS;
}

function objectChangeSchema(): Record<string, unknown> {
  return {
    type: 'object',
    required: ['object_id', 'expected_revision'],
    properties: {
      object_id: OBJECT_ID_SCHEMA,
      expected_revision: { type: 'string' },
      metadata: { type: 'object', additionalProperties: true },
      field_ops: {
        type: 'object',
        properties: {
          add: { type: 'array', items: { type: 'object' } },
          update: { type: 'array', items: { type: 'object' } },
          remove: {
            type: 'array',
            items: { type: 'string' },
          },
        },
        additionalProperties: false,
      },
      relation_ops: {
        type: 'object',
        properties: {
          add: { type: 'array', items: { type: 'object' } },
          update: { type: 'array', items: { type: 'object' } },
          remove: {
            type: 'array',
            items: {
              oneOf: [
                { type: 'string' },
                { type: 'object' },
              ],
            },
          },
        },
        additionalProperties: false,
      },
    },
    additionalProperties: false,
  };
}
