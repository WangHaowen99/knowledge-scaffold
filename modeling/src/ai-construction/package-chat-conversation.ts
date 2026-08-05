import { createHash } from 'node:crypto';

import type { ChatPlanResult } from '../workflow/package-chat.js';
import {
  AiConstructionMessageStore,
  type AiConstructionChangePlanBlock,
  type AiConstructionMessage,
} from './messages.js';
import type { AiConstructionTaskGraphSnapshot } from './task-graph.js';
import { splitAiConstructionText } from './text-blocks.js';

function messageKey(kind: 'user' | 'reply' | 'error' | 'result', requestId: string): string {
  const digest = createHash('sha256').update(requestId).digest('hex').slice(0, 24);
  return `chat.${kind}.${digest}`;
}

function planMessageKey(interactionId: string): string {
  const digest = createHash('sha256').update(interactionId).digest('hex').slice(0, 24);
  return `chat.plan.${digest}`;
}

function planFailureMessageKey(interactionId: string): string {
  const digest = createHash('sha256').update(interactionId).digest('hex').slice(0, 24);
  return `chat.plan-failure.${digest}`;
}

function constructionCompletionMessageKey(interactionId: string): string {
  const digest = createHash('sha256').update(interactionId).digest('hex').slice(0, 24);
  return `chat.completion.${digest}`;
}

export function shouldRequestConstructionCompletion(
  snapshot: AiConstructionTaskGraphSnapshot | null,
  pendingInteractionCount: number,
): boolean {
  if (!snapshot || pendingInteractionCount !== 0 || snapshot.status === 'completed') return false;
  return snapshot.milestones.some((milestone) => (
    milestone.id === 'construction_completed' && milestone.status === 'active'
  ))
    && snapshot.tasks.some((task) => (
      task.kind === 'completion_review'
        && task.milestoneId === 'construction_completed'
        && task.status === 'completed'
    ));
}

function textOf(message: AiConstructionMessage): string | null {
  const parts = message.blocks.flatMap((block) => (
    block.type === 'text' ? [block.text] : []
  ));
  return parts.length ? parts.join('') : null;
}

/**
 * 建模助手对话只复用结构化消息事实源，不维护第二份会话状态。
 * requestId 和 interactionId 均转为稳定 key，HTTP 重试不会重复写消息。
 */
export class PackageChatConversation {
  constructor(private readonly messages: AiConstructionMessageStore) {}

  beginTurn(projectId: string, requestId: string, text: string): AiConstructionMessage {
    return this.messages.ensure(projectId, {
      key: messageKey('user', requestId),
      role: 'user',
      blocks: [{ type: 'text', text }],
    });
  }

  finishTurn(projectId: string, requestId: string, result: ChatPlanResult): AiConstructionMessage {
    if (result.plan?.changes.length && result.interaction?.id) {
      return this.messages.upsert(projectId, {
        key: planMessageKey(result.interaction.id),
        role: 'assistant',
        blocks: [{
          type: 'change_plan',
          interactionId: result.interaction.id,
          title: '确认建模调整',
          summary: result.plan.summary,
          status: 'pending',
          changes: result.plan.changes.map((change, index) => ({
            id: `change_${index + 1}`,
            ...change,
          })),
          ...(result.plan.risks?.length ? { risks: result.plan.risks } : {}),
        }],
      });
    }
    const reply = result.reply?.trim() || '（建模助手未给出答复）';
    return this.messages.ensure(projectId, {
      key: messageKey('reply', requestId),
      role: 'assistant',
      blocks: splitAiConstructionText(reply).map((text) => ({ type: 'text', text })),
    });
  }

  history(projectId: string, limit = 6): Array<{ role: 'user' | 'assistant'; text: string }> {
    return this.messages.list(projectId, 0, 500)
      .filter((message) => (
        message.key?.startsWith('chat.user.') || message.key?.startsWith('chat.reply.')
      ))
      .map((message) => ({ role: message.role, text: textOf(message) }))
      .filter((message): message is { role: 'user' | 'assistant'; text: string } => (
        (message.role === 'user' || message.role === 'assistant') && Boolean(message.text)
      ))
      .slice(-Math.max(1, Math.min(20, limit)));
  }

  failTurn(projectId: string, requestId: string, prefix: string, error: unknown): AiConstructionMessage {
    const detail = error instanceof Error ? error.message : String(error);
    return this.messages.ensure(projectId, {
      key: messageKey('error', requestId),
      role: 'assistant',
      blocks: [{ type: 'text', text: `${prefix}：${detail}` }],
    });
  }

  recordPlanFailure(projectId: string, interactionId: string, feedback: string): AiConstructionMessage {
    return this.messages.upsert(projectId, {
      key: planFailureMessageKey(interactionId),
      role: 'assistant',
      blocks: [{
        type: 'text',
        text: `候选内容未通过校验，当前知识包未更改。请调整方案后重试：${feedback.trim()}`,
      }],
    });
  }

  planFailure(projectId: string, interactionId: string): string | null {
    const message = this.messages.findByKey(projectId, planFailureMessageKey(interactionId));
    return message ? textOf(message) : null;
  }

  requestConstructionCompletion(projectId: string, interactionId: string): AiConstructionMessage {
    return this.messages.ensure(projectId, {
      key: constructionCompletionMessageKey(interactionId),
      role: 'assistant',
      blocks: [
        {
          type: 'status',
          title: 'AI 辅助构建待最终确认',
          summary: '本轮建模调整已取消，当前知识包保持不变',
          milestoneId: 'construction_completed',
        },
        { type: 'text', text: '确认当前成果后将进入场景推演。' },
        {
          type: 'next_actions',
          title: '选择下一步',
          actions: [
            { id: 'confirm_construction', label: '确认并进入场景推演', style: 'primary' },
            { id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary' },
          ],
        },
      ],
    });
  }

  settlePlan(
    projectId: string,
    interactionId: string,
    status: AiConstructionChangePlanBlock['status'],
    replyText: string,
  ): { plan: AiConstructionMessage | null; result: AiConstructionMessage } {
    const key = planMessageKey(interactionId);
    const current = this.messages.findByKey(projectId, key);
    const planBlock = current?.blocks.find(
      (block): block is AiConstructionChangePlanBlock => block.type === 'change_plan',
    );
    const plan = planBlock
      ? this.messages.upsert(projectId, {
        key,
        role: 'assistant',
        blocks: current!.blocks.map((block) => (
          block.type === 'change_plan' ? { ...block, status } : block
        )),
      })
      : null;
    const result = this.messages.ensure(projectId, {
      key: messageKey('result', interactionId),
      role: 'assistant',
      blocks: [{ type: 'text', text: replyText.trim() }],
    });
    return { plan, result };
  }
}
