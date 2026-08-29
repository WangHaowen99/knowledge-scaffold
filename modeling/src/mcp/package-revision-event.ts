import { getAuthoringState } from '../authoring/state.js';
import type { JsonObject } from '../authoring/types.js';
import type { ModelingMcpToolIssue, ModelingMcpToolResourceRef } from './tool-error.js';

export interface ModelingMcpRevisionEventInput {
  projectId: string;
  revisionId: string;
  packageVersion?: string | null;
  cause: string;
  resource?: ModelingMcpToolResourceRef;
}

export type ModelingMcpRevisionEventPublisher = (input: ModelingMcpRevisionEventInput) => void;

export function publishModelingMcpRevisionEvent(input: ModelingMcpRevisionEventInput): void {
  const payload: JsonObject = {
    revisionId: input.revisionId,
    cause: input.cause,
    source: 'mcp',
  };
  if (input.packageVersion) payload.packageVersion = input.packageVersion;
  if (input.resource) {
    payload.resource = {
      type: input.resource.type,
      id: input.resource.id,
      ...(input.resource.key ? { key: input.resource.key } : {}),
    };
  }
  getAuthoringState().apply({
    kind: 'publish_event',
    projectId: input.projectId,
    type: 'package.revision.created',
    payload,
  });
}

export function tryPublishModelingMcpRevisionEvent(
  input: ModelingMcpRevisionEventInput,
  publish: ModelingMcpRevisionEventPublisher = publishModelingMcpRevisionEvent,
): ModelingMcpToolIssue | null {
  try {
    publish(input);
    return null;
  } catch (error) {
    try {
      console.error('[modeling-mcp] 知识包 revision 事件发布失败', {
        projectId: input.projectId,
        revisionId: input.revisionId,
        cause: input.cause,
        resource: input.resource,
        error: error instanceof Error ? error.message : String(error),
      });
    } catch {
      // 日志通道异常不能改变已经成功提交的 revision 结果。
    }
    return {
      code: 'REVISION_EVENT_PUBLISH_FAILED',
      phase: 'persist',
      level: 'warning',
      target: 'package.revision.created',
      message: '修改已成功保存，但页面刷新通知发送失败',
      suggestion: '不要重复提交修改；请刷新知识包页面查看最新 revision',
    };
  }
}
