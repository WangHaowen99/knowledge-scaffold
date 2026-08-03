/**
 * 增量补充：知识包建完之后补新资料，只处理增量而不重走全流程。
 *
 * 与从头建模的区别不在于流程更短，而在于**边界**：模型只被允许在新资料带来的范围内
 * 提出变更，其余内容一律不碰。边界写在提示词里是不够的——提示词是建议，不是约束，
 * 所以服务端还要再挡一道（assertIncrementChangesAllowed）。
 */
import type { PackageSpec } from '../types.js';
import type { ChangeTargetKind } from './change-plan-context.js';
import { currentResourceSummary } from './change-plan-context.js';

export interface IncrementMaterial {
  filename: string;
  /** 资料正文。只给文件名模型无从下手——实测它会直接回「未附上具体内容」并放弃 */
  content?: string;
}

/** 单份资料带进提示词的长度上限，避免一次塞爆上下文 */
const MAX_MATERIAL_CHARS = 6_000;

export interface IncrementChange {
  targetKind: ChangeTargetKind;
  targetKey: string;
  action: string;
}

/** 增量里禁止删除：下线一条规则应该改成「标记不再适用」，删掉就没法解释历史评测为什么那样判 */
export class IncrementRemovalNotAllowedError extends Error {
  constructor(readonly changes: IncrementChange[]) {
    super(`增量补充不支持删除内容（${changes.map((item) => item.targetKey).join('、')}）。`
      + '如果这些内容不再适用，请改为修改它们的适用条件或例外，保留可追溯性。');
    this.name = 'IncrementRemovalNotAllowedError';
  }
}

/**
 * 服务端兜底：提示词说了不许删，模型仍可能提交删除。
 * 放行会造成不可逆的内容丢失，所以这里直接挡下而不是过滤——静默丢弃会让专家以为
 * 方案已经完整，实际少了一部分。
 */
export function assertIncrementChangesAllowed(changes: IncrementChange[]): void {
  const removals = changes.filter((change) => change.action === 'remove');
  if (removals.length) throw new IncrementRemovalNotAllowedError(removals);
}

/**
 * 增量提案的需求文本。
 *
 * 带上被涉及资源的当前内容，模型才能提出「怎么改」而不是「重写一遍」——
 * 这也是让它倾向 modify 而不是 add 一条重复规则的关键。
 */
export function buildIncrementRequirement(input: {
  materials: IncrementMaterial[];
  affected: IncrementChange[];
  spec?: PackageSpec | null;
}): string {
  const materials = input.materials.length
    ? input.materials.map((item, index) => {
      const body = (item.content ?? '').trim();
      const clipped = body.length > MAX_MATERIAL_CHARS
        ? `${body.slice(0, MAX_MATERIAL_CHARS)}\n…（内容过长已截断）`
        : body;
      return `${index + 1}. 《${item.filename}》\n${clipped || '（这份资料没有可读正文）'}`;
    }).join('\n\n')
    : '（本轮没有新增资料，仅根据已变更资料复核）';

  const affected = input.affected
    .map((ref) => {
      const current = currentResourceSummary(input.spec, ref);
      return current
        ? `- ${ref.targetKind} ${ref.targetKey} 当前内容：\n${current}`
        : `- ${ref.targetKind} ${ref.targetKey}`;
    })
    .join('\n');

  return [
    '这是一次增量补充：知识包已经建成并确认过，现在只根据下列新增或变更的资料提出增量修改。',
    '',
    '本轮资料：',
    materials,
    ...(affected ? ['', '这些已有内容与本轮资料直接相关，请优先修改它们：', affected] : []),
    '',
    '硬性要求：',
    '① 只提出与本轮资料直接相关的变更；与本轮资料无关的内容一律不要动，即使你觉得它可以更好。',
    '② 只能使用 add 和 modify，不得使用 remove。内容不再适用时改为修改它的适用条件或例外，保留可追溯性。',
    // 实测（deepseek-v4-flash）：知识包非空时模型会正确选择 modify 已有规则。
    // 这条只是把判据说清楚，不额外重复一遍规格——【当前规格】里已经完整给过了。
    '③ 提出每一条变更前，先在【当前规格】里找有没有讲同一件事的条目：'
      + '有就用 modify 改它（例如门槛、口径、例外发生变化），只有确实没有对应条目时才用 add。'
      + '新增一条与已有条目语义重复的规则或指标，属于错误做法。',
    '④ 逐条说明这次改动的依据来自本轮哪份资料，无法追溯到资料的改动不要提。',
    '⑤ 如果本轮资料不足以支撑任何可靠变更，就不要提交计划，直接说明还缺什么。',
  ].join('\n');
}
