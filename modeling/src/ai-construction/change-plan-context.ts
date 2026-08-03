/**
 * 给「修改」类变更补上「当前是什么样」。
 *
 * 变更方案原先只有 targetKey + 一句怎么改的说明。专家看到「修改规则 R：把门槛提高」
 * 时，屏幕上没有 R 现在的内容——他在确认一个自己看不见的东西。增量建模要支持修改
 * 已有规则，这一条就成了硬要求。
 *
 * 当前内容不向模型索取而是从 spec 直接取：让模型复述已有内容既费 token 又可能抄错，
 * 而抄错的「修改前」比没有更危险——专家会以为自己看到的是真的。
 */
import type { PackageSpec } from '../types.js';

export type ChangeTargetKind = 'object' | 'operation' | 'rule' | 'metric' | 'script';

export interface ChangeTargetRef {
  targetKind: ChangeTargetKind;
  targetKey: string;
  action?: string;
}

const MAX_SUMMARY_LENGTH = 800;

function clamp(value: string): string {
  const text = value.trim().replace(/\n{3,}/gu, '\n\n');
  return text.length > MAX_SUMMARY_LENGTH ? `${text.slice(0, MAX_SUMMARY_LENGTH)}…` : text;
}

/**
 * 当前内容的可读摘要。取不到时返回 null——宁可不显示，也不要给一个空壳让人以为
 * 「这条规则本来就是空的」。
 */
export function currentResourceSummary(
  spec: PackageSpec | null | undefined,
  ref: ChangeTargetRef,
): string | null {
  if (!spec) return null;
  const key = ref.targetKey?.trim();
  if (!key) return null;

  if (ref.targetKind === 'rule') {
    const rule = spec.rules?.find((item) => item.key === key);
    if (!rule) return null;
    const conditions = (rule.conditions ?? [])
      .map((item) => `· 当${item.condition}时，${item.conclusion}`);
    const exceptions = (rule.exceptions ?? []).map((item) => `· 例外：${item}`);
    return clamp([rule.title, rule.content, ...conditions, ...exceptions].filter(Boolean).join('\n'));
  }
  if (ref.targetKind === 'metric') {
    const metric = spec.metrics?.find((item) => item.key === key) as
      { title?: string; description?: string; formula?: string } | undefined;
    if (!metric) return null;
    return clamp([metric.title, metric.description, metric.formula].filter(Boolean).join('\n'));
  }
  if (ref.targetKind === 'operation') {
    const operation = spec.operations?.find((item) => item.key === key);
    if (!operation) return null;
    const flow = (operation.flow ?? []).map((step, index) => `${index + 1}. ${step}`);
    return clamp([operation.title, operation.description, ...flow].filter(Boolean).join('\n'));
  }
  if (ref.targetKind === 'object') {
    const object = spec.objects?.find((item) => item.key === key) as
      { title?: string; description?: string } | undefined;
    if (!object) return null;
    return clamp([object.title, object.description].filter(Boolean).join('\n'));
  }
  if (ref.targetKind === 'script') {
    const script = spec.scripts?.find((item) => item.key === key);
    if (!script) return null;
    return clamp([script.title, script.description, `绑定业务操作：${script.boundOperation}`]
      .filter(Boolean).join('\n'));
  }
  return null;
}

/**
 * 给变更列表补 current 字段。只补「修改」类——新增本来就没有「当前内容」，
 * 硬塞一个空值只会让卡片上多出一块看不懂的空白。
 */
export function withCurrentContent<T extends ChangeTargetRef>(
  spec: PackageSpec | null | undefined,
  changes: T[],
): Array<T & { current?: string }> {
  return changes.map((change) => {
    if (change.action !== 'modify' && change.action !== 'remove') return change;
    const current = currentResourceSummary(spec, change);
    return current ? { ...change, current } : change;
  });
}
