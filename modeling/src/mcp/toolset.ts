import { AgentCoreService } from '../agent/core-service.js';
import type { Project } from '../types.js';
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

const TOOL_DEFINITIONS: readonly ModelingMcpToolDefinition[] = [
  {
    name: 'list_ontology_objects',
    title: '列出对象模型',
    description: '查询当前建模项目中可用的对象列表，返回对象摘要和 working_revision。',
    toolset: 'read',
    requiredScope: 'ontology.read',
    internalToolName: 'list_ontology_objects',
    inputSchema: {
      type: 'object',
      properties: {
        working_revision: { type: 'string', description: '可选，指定当前读取的工作修订。' },
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
    description: '读取指定对象的元属性、字段定义和对象关系。',
    toolset: 'read',
    requiredScope: 'ontology.read',
    internalToolName: 'get_ontology_object',
    inputSchema: {
      type: 'object',
      required: ['object_id'],
      properties: {
        object_id: { type: 'string', pattern: '^object:[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$' },
        working_revision: { type: 'string', description: '可选，指定当前读取的工作修订。' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'get_ontology_object_review_status',
    title: '读取对象模型复核状态',
    description: '查询指定对象当前草稿、复核请求、待确认候选以及最近一次确认结果。',
    toolset: 'read',
    requiredScope: 'ontology.read',
    internalToolName: 'get_ontology_object_review_status',
    inputSchema: {
      type: 'object',
      required: ['object_id'],
      properties: {
        object_id: { type: 'string', pattern: '^object:[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'list_ontology_object_audit_records',
    title: '列出对象模型审计记录',
    description: '按对象查询候选、确认和退回记录，支持游标翻页。',
    toolset: 'read',
    requiredScope: 'ontology.read',
    internalToolName: 'list_ontology_object_audit_records',
    inputSchema: {
      type: 'object',
      required: ['object_id'],
      properties: {
        object_id: { type: 'string', pattern: '^object:[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$' },
        status: {
          type: 'string',
          enum: ['pending', 'accepted', 'revision_requested', 'rejected', 'superseded'],
          description: '可选，按候选状态过滤。',
        },
        cursor: { type: 'string', description: '可选，上一页返回的 next_cursor。' },
        limit: { type: 'integer', minimum: 1, maximum: 100, description: '可选，单页大小。' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'validate_ontology_object_patch',
    title: '校验对象模型修改',
    description: '对对象元属性、字段和对象关系的修改做只校验预演，不落库。',
    toolset: 'change',
    requiredScope: 'ontology.change.validate',
    internalToolName: 'validate_ontology_object_patch',
    inputSchema: objectPatchSchema(true),
  },
  {
    name: 'apply_ontology_object_patch',
    title: '保存对象模型修改草稿',
    description: '将对象元属性、字段和对象关系修改写入当前项目草稿 candidate。',
    toolset: 'change',
    requiredScope: 'ontology.change.apply',
    internalToolName: 'apply_ontology_object_patch',
    inputSchema: {
      ...objectPatchSchema(true),
      required: ['object_id', 'expected_revision', 'client_mutation_id'],
      properties: {
        ...(objectPatchSchema(true).properties as Record<string, unknown>),
        client_mutation_id: {
          type: 'string',
          description: '调用方幂等键，相同 mutation 重试时复用同一个值。',
        },
        edit_session_id: {
          type: 'string',
          description: '可选，调用方维护的编辑会话标识。',
        },
      },
    },
  },
  {
    name: 'submit_ontology_object_review',
    title: '提交对象模型复核',
    description: '将当前对象模型 candidate 提交到复核流程，等待人工确认。',
    toolset: 'change',
    requiredScope: 'ontology.change.review',
    internalToolName: 'submit_ontology_object_review',
    inputSchema: {
      type: 'object',
      required: ['expected_revision', 'client_mutation_id'],
      properties: {
        expected_revision: { type: 'string' },
        client_mutation_id: { type: 'string' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'prepare_ontology_object_confirmation_groups',
    title: '准备对象模型确认分组',
    description: '读取当前对象模型 candidate 的确认分组，供外部审批或人工确认前展示。',
    toolset: 'change',
    requiredScope: 'ontology.change.review',
    internalToolName: 'prepare_confirmation_groups',
    inputSchema: {
      type: 'object',
      properties: {},
      additionalProperties: false,
    },
  },
  {
    name: 'preflight_ontology_object_confirmation',
    title: '预检对象模型确认',
    description: '基于确认分组做发布前复检，返回候选回执和确认所需的校验票据。',
    toolset: 'change',
    requiredScope: 'ontology.change.review',
    internalToolName: 'request_confirmation_preflight',
    inputSchema: {
      type: 'object',
      required: ['change_groups'],
      properties: {
        summary: { type: 'string', description: '可选，本次确认摘要。' },
        change_groups: confirmationGroupsSchema(),
      },
      additionalProperties: false,
    },
  },
  {
    name: 'confirm_ontology_object_review',
    title: '确认对象模型复核',
    description: '提交人工确认后的最终决定，将当前对象模型 candidate 应用到正式修订。',
    toolset: 'change',
    requiredScope: 'ontology.change.review',
    internalToolName: 'request_confirmation',
    inputSchema: {
      type: 'object',
      required: [
        'summary',
        'change_groups',
        'expected_candidate_revision',
        'expected_candidate_hash',
        'expected_validator_version',
        'review',
      ],
      properties: {
        summary: { type: 'string' },
        change_groups: confirmationGroupsSchema(),
        expected_candidate_revision: { type: 'string' },
        expected_candidate_hash: { type: 'string' },
        expected_validator_version: { type: 'string' },
        review: confirmationReviewSchema(true),
      },
      additionalProperties: false,
    },
  },
  {
    name: 'reject_ontology_object_review',
    title: '退回对象模型复核',
    description: '退回当前待确认的对象模型 candidate，不将其应用到正式修订。',
    toolset: 'change',
    requiredScope: 'ontology.change.review',
    internalToolName: 'request_confirmation_rejected',
    inputSchema: {
      type: 'object',
      required: ['expected_candidate_revision', 'review'],
      properties: {
        expected_candidate_revision: { type: 'string' },
        review: confirmationReviewSchema(false),
      },
      additionalProperties: false,
    },
  },
] as const;

export function toolDefinitionsFor(
  toolset: ModelingMcpToolset,
  scopes: readonly ModelingMcpScope[],
): ModelingMcpToolDefinition[] {
  return TOOL_DEFINITIONS
    .filter((tool) => tool.toolset === toolset && scopes.includes(tool.requiredScope))
    .map((tool) => ({ ...tool, inputSchema: structuredClone(tool.inputSchema) }));
}

export function toolNamesByToolset(): Record<ModelingMcpToolset, string[]> {
  return {
    read: TOOL_DEFINITIONS.filter((tool) => tool.toolset === 'read').map((tool) => tool.name),
    change: TOOL_DEFINITIONS.filter((tool) => tool.toolset === 'change').map((tool) => tool.name),
  };
}

export function toolDefinitionByName(name: string): ModelingMcpToolDefinition | null {
  const normalized = String(name ?? '').trim();
  return TOOL_DEFINITIONS.find((tool) => tool.name === normalized) ?? null;
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
  const service = new AgentCoreService();
  const result = await service.invoke({
    project: input.project,
    runId: `mcp-${tool.toolset}-${input.tokenId}`,
    username: input.username,
  }, tool.internalToolName, input.argumentsValue);
  return result as Record<string, unknown>;
}

function objectPatchSchema(requireExpectedRevision: boolean): Record<string, unknown> {
  return {
    type: 'object',
    required: requireExpectedRevision ? ['object_id', 'expected_revision'] : ['object_id'],
    properties: {
      object_id: { type: 'string', pattern: '^object:[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$' },
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

function confirmationGroupsSchema(): Record<string, unknown> {
  return {
    type: 'array',
    minItems: 1,
    maxItems: 5,
    items: {
      type: 'object',
      required: ['id', 'tier', 'title'],
      properties: {
        id: {
          type: 'string',
          pattern: '^[a-z][a-z0-9_.-]{0,63}$',
        },
        tier: {
          type: 'string',
          enum: ['caliber', 'structural', 'index', 'script'],
        },
        title: { type: 'string' },
        impact: {
          type: 'array',
          items: { type: 'string' },
        },
        warning: { type: 'string' },
      },
      additionalProperties: false,
    },
  };
}

function confirmationReviewSchema(approved: boolean): Record<string, unknown> {
  return {
    type: 'object',
    required: ['approved'],
    properties: {
      approved: {
        type: 'boolean',
        const: approved,
      },
      review_mode: {
        type: 'string',
        enum: ['full', 'sequential', 'sampled', 'automatic'],
      },
      selected_groups: {
        type: 'array',
        items: {
          type: 'string',
          pattern: '^[a-z][a-z0-9_.-]{0,63}$',
        },
      },
      sampled_groups: {
        type: 'array',
        items: {
          type: 'string',
          pattern: '^[a-z][a-z0-9_.-]{0,63}$',
        },
      },
      feedback: { type: 'string' },
    },
    additionalProperties: false,
  };
}
