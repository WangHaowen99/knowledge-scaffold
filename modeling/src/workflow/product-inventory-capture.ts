import { Type } from '@earendil-works/pi-ai';
import type { ProductInventoryItem } from '../types.js';

export const productInventoryItemSchema = Type.Object({
  canonicalItemId: Type.String({ minLength: 1 }),
  name: Type.String({ minLength: 1 }),
  ruleRefs: Type.Array(Type.String(), { minItems: 1, description: '规范规则ID；不得混入原始记录ID' }),
  sourceRecordIds: Type.Array(Type.String(), {
    minItems: 1,
    description: '只列原文的原始记录ID。不得放入canonicalItemId、ruleRefs或证据定位字符串；同一产品的多条原始记录才构成归并组。',
  }),
  sourceLocator: Type.String({ minLength: 1 }),
  mergeGroupId: Type.Optional(Type.String()),
  evidenceStatus: Type.Union([Type.Literal('complete'), Type.Literal('insufficient')]),
  processingStatus: Type.Optional(Type.Union([Type.Literal('ready'), Type.Literal('blocked')])),
  insufficiencyReason: Type.Optional(Type.String()),
  applicableSubjects: Type.Array(Type.String()),
  triggerScenarios: Type.Array(Type.String()),
  businessNeeds: Type.Array(Type.String()),
  admissionConditions: Type.Array(Type.String()),
  exclusionConditions: Type.Array(Type.String()),
  semanticWarnings: Type.Optional(Type.Array(Type.String())),
  structuredGaps: Type.Array(Type.String()),
});

const strings = (value: unknown): string[] => (Array.isArray(value)
  ? [...new Set(value.map((item) => String(item ?? '').trim()).filter(Boolean))]
  : []);

/** 构成「资料字段是否齐备」判定的五个来源字段。语义歧义不在此列。 */
export const INVENTORY_FACT_FIELDS = [
  'applicableSubjects', 'triggerScenarios', 'businessNeeds', 'admissionConditions', 'exclusionConditions',
] as const;

const missingValue = /^(?:资料未提供|未提供|未明确|未知|不详|待补充|N\/?A|not provided|unknown|unspecified)$/iu;
const facts = (value: unknown) => strings(value).filter((item) => !missingValue.test(item));

/**
 * 捕获阶段即拒绝残缺事实，而不是在汇总时静默丢弃。
 * 丢弃会让残缺来源看起来完整，并在台账对账时产出貌似自洽的错误计数。
 */
export function normalizeProductInventory(value: unknown): ProductInventoryItem[] {
  if (!Array.isArray(value)) throw new Error('productInventory 必须为数组；无产品事实时显式提交空数组');
  return value.map((raw, index) => {
    const item = (raw ?? {}) as Record<string, unknown>;
    const canonicalItemId = String(item.canonicalItemId ?? '').trim();
    const name = String(item.name ?? '').trim();
    const ruleRefs = strings(item.ruleRefs);
    const sourceRecordIds = strings(item.sourceRecordIds);
    const sourceLocator = String(item.sourceLocator ?? '').trim();
    if (!canonicalItemId || !name || !ruleRefs.length || !sourceRecordIds.length || !sourceLocator) {
      throw new Error(`productInventory 第 ${index + 1} 项缺少标识、名称、规则引用或来源，不能以删除该项代替修正`);
    }
    if (sourceRecordIds.some((id) => id === canonicalItemId || ruleRefs.includes(id) || id === sourceLocator)) {
      throw new Error(`productInventory 第 ${index + 1} 项将规范业务项ID、规则ID或证据位置混入sourceRecordIds；这里只能保存原始记录ID，请按原文角色修正，不能以增加来源数补齐统计`);
    }
    if (!['complete', 'insufficient'].includes(String(item.evidenceStatus))) {
      throw new Error(`productInventory 第 ${index + 1} 项 evidenceStatus 无效`);
    }
    for (const field of [...INVENTORY_FACT_FIELDS, 'structuredGaps']) {
      if (!Array.isArray(item[field])) {
        throw new Error(`productInventory 第 ${index + 1} 项 ${field} 必须为数组`);
      }
    }
    // 来源字段齐备不等于业务规则可执行。歧义和阈值保留为警告，
    // 但任何情况下都不得用期望条数倒推出并不存在的事实。
    const structuredGaps = INVENTORY_FACT_FIELDS.filter((field) => facts(item[field]).length === 0);
    const semanticWarnings = strings([
      ...strings(item.semanticWarnings),
      ...strings(item.structuredGaps).filter((gap) => !(INVENTORY_FACT_FIELDS as readonly string[]).includes(gap)),
      ...(item.evidenceStatus === 'insufficient' && !structuredGaps.length && item.insufficiencyReason
        ? [String(item.insufficiencyReason)]
        : []),
    ]);
    const insufficient = structuredGaps.length > 0;
    return {
      completenessContract: 'source-fields-v1' as const,
      canonicalItemId,
      name,
      ruleRefs,
      sourceRecordIds,
      sourceLocator,
      ...(sourceRecordIds.length > 1 ? { mergeGroupId: `merge:${canonicalItemId}` } : {}),
      evidenceStatus: insufficient ? 'insufficient' as const : 'complete' as const,
      processingStatus: insufficient ? 'blocked' as const : 'ready' as const,
      ...(insufficient ? { insufficiencyReason: `资料未提供：${structuredGaps.join('、')}` } : {}),
      applicableSubjects: facts(item.applicableSubjects),
      triggerScenarios: facts(item.triggerScenarios),
      businessNeeds: facts(item.businessNeeds),
      admissionConditions: facts(item.admissionConditions),
      exclusionConditions: facts(item.exclusionConditions),
      semanticWarnings,
      structuredGaps,
    };
  });
}

/** 小批回执让模型不必在一次响应里吐出整册密集清单。 */
export function createProductInventoryCapture() {
  const batches = new Map<string, ProductInventoryItem[]>();
  const tool = {
    name: 'submit_product_inventory_batch',
    label: '提交分段产品事实批次',
    description: '当前原文分段内，每批提交1–10项产品事实。提交成功不表示分段完成；最后仍须提交分段分析。',
    parameters: Type.Object({
      batchId: Type.String({ minLength: 1 }),
      items: Type.Array(productInventoryItemSchema, { minItems: 1, maxItems: 10 }),
    }),
    async execute(_id: string, args: { batchId: string; items: unknown[] }) {
      const batchId = String(args?.batchId ?? '').trim();
      const items = normalizeProductInventory(args?.items);
      if (!batchId || items.length < 1 || items.length > 10) {
        throw new Error('产品事实批次需要batchId和1–10项事实');
      }
      const previous = batches.get(batchId);
      if (previous && JSON.stringify(previous) !== JSON.stringify(items)) {
        throw new Error('相同batchId已用于不同产品事实，不得覆盖已接收批次');
      }
      batches.set(batchId, items);
      const count = [...batches.values()].reduce((total, batch) => total + batch.length, 0);
      return {
        content: [{
          type: 'text' as const,
          text: `已接收${batches.size}批、${count}项分段产品事实，请继续处理剩余原文，最后提交分段分析。`,
        }],
        details: { batchCount: batches.size, itemCount: count },
      };
    },
  };
  return {
    tool,
    finish(disposition: unknown, inline: unknown): ProductInventoryItem[] {
      const items = [...batches.values()].flat();
      if (inline !== undefined) items.push(...normalizeProductInventory(inline));
      if (!['extracted', 'not_applicable'].includes(String(disposition))) {
        throw new Error('必须声明 productInventoryDisposition：已提取为extracted，原文确无产品条目为not_applicable；不能只提交摘要');
      }
      if (disposition === 'extracted' && !items.length) {
        throw new Error('声明已提取产品事实但尚未提交逐项产品数据');
      }
      if (disposition === 'not_applicable' && items.length) {
        throw new Error('已接收产品事实，不能声明not_applicable');
      }
      return items;
    },
  };
}
