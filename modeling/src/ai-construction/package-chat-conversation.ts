import { createHash } from 'node:crypto';

import type { ChatPlanResult } from '../workflow/package-chat.js';
import {
  AiConstructionMessageStore,
  type AiConstructionChangePlanBlock,
  type AiConstructionMessage,
} from './messages.js';

function messageKey(kind: 'user' | 'reply' | 'error' | 'result', requestId: string): string {
  const digest = createHash('sha256').update(requestId).digest('hex').slice(0, 24);
  return `chat.${kind}.${digest}`;
}

function planMessageKey(interactionId: string): string {
  const digest = createHash('sha256').update(interactionId).digest('hex').slice(0, 24);
  return `chat.plan.${digest}`;
}

function textOf(message: AiConstructionMessage): string | null {
  const block = message.blocks.find((item) => item.type === 'text');
  return block?.type === 'text' ? block.text : null;
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
    return this.messages.ensure(projectId, {
      key: messageKey('reply', requestId),
      role: 'assistant',
      blocks: [{ type: 'text', text: result.reply?.trim() || '（建模助手未给出答复）' }],
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
