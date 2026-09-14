/**
 * 评测用例的有界并发执行器。
 *
 * 并发只作用在“跑用例 + judge”这一段：每条用例互相独立，但结果必须按用例顺序落库，
 * 否则 eval_results 依 created_at 排序后会和用例列表错位。这里用滑动前缀提交来保证
 * 顺序：先完成的结果先挂起，等它前面的用例都完成后再按序 commit。
 */

export interface CaseRunnerHooks<TItem, TOutcome> {
  /** 执行一条用例。抛出的错误交给 recover 判定是否致命。 */
  run(item: TItem, index: number): Promise<TOutcome>;
  /** 按 items 顺序提交结果，同一条只会调用一次。 */
  commit(item: TItem, index: number, outcome: TOutcome): void;
  /**
   * 把单条用例的失败转成可提交的结果（例如记一条 error 判定）。
   * 若 recover 自身抛出，视为致命错误：停止取新用例，并在全部在途用例结束后抛出。
   */
  recover(item: TItem, index: number, error: unknown): TOutcome;
  /** 每条用例开始执行时回调，用于刷新“当前用例开始时间”。 */
  onStart?(item: TItem, index: number): void;
}

export function normalizeCaseConcurrency(value: unknown, fallback = 1, max = 16): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) return fallback;
  return Math.min(parsed, max);
}

export async function runCasesWithLimit<TItem, TOutcome>(
  items: readonly TItem[],
  limit: number,
  hooks: CaseRunnerHooks<TItem, TOutcome>,
  signal?: AbortSignal,
): Promise<void> {
  if (!items.length) return;
  const workers = Math.max(1, Math.min(Math.trunc(limit) || 1, items.length));
  const outcomes = new Array<{ value: TOutcome } | undefined>(items.length);

  let nextIndex = 0;
  let commitIndex = 0;
  let fatal: unknown;
  let stopped = false;

  /** 只提交“前面都已完成”的连续前缀，保证 commit 严格按用例顺序发生。 */
  const flush = (): void => {
    while (commitIndex < items.length) {
      const ready = outcomes[commitIndex];
      if (!ready) return;
      hooks.commit(items[commitIndex], commitIndex, ready.value);
      outcomes[commitIndex] = undefined;
      commitIndex += 1;
    }
  };

  const worker = async (): Promise<void> => {
    for (;;) {
      if (stopped || signal?.aborted) return;
      const index = nextIndex;
      if (index >= items.length) return;
      nextIndex += 1;
      const item = items[index];
      hooks.onStart?.(item, index);
      let outcome: TOutcome;
      try {
        outcome = await hooks.run(item, index);
      } catch (error) {
        try {
          outcome = hooks.recover(item, index, error);
        } catch (fatalError) {
          // 致命错误：不再领新用例，等在途的结束后统一抛出。
          if (fatal === undefined) fatal = fatalError;
          stopped = true;
          return;
        }
      }
      outcomes[index] = { value: outcome };
      flush();
    }
  };

  await Promise.all(Array.from({ length: workers }, () => worker()));
  flush();

  if (fatal !== undefined) throw fatal;
  if (signal?.aborted) throw signal.reason;
}
