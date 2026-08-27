import { createHash } from 'node:crypto';

export type GenerationPlanKind = 'object' | 'knowledge' | 'operation';

export interface GenerationUnit<T> {
  index: number;
  phase: `${GenerationPlanKind}_batch_${number}`;
  items: T[];
  dependencyHash: string;
}

function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stable(item)]));
  }
  return value;
}

export function dependencyHash(value: unknown): string {
  return createHash('sha256').update(JSON.stringify(stable(value))).digest('hex');
}

/**
 * 以输入大小和模型上下文动态决定批大小。上限是为了控制单次模型输出，
 * 下限是为了避免少量候选被拆成大量请求；没有上下文窗口时使用保守默认值。
 */
export function dynamicBatchSize(
  kind: GenerationPlanKind,
  items: unknown[],
  contextWindow = 1_000_000,
): number {
  const limits = { object: 12, knowledge: 12, operation: 3 };
  const minimums = { object: 8, knowledge: 8, operation: 2 };
  const max = limits[kind];
  const min = minimums[kind];
  if (items.length <= min) return Math.max(1, items.length);
  const estimatedChars = Math.max(1, items.reduce<number>((sum, item) => sum + JSON.stringify(item).length, 0));
  const charsPerToken = 3;
  const budgetChars = Math.max(8_000, Math.floor(contextWindow * charsPerToken * 0.12));
  const average = Math.max(1, Math.ceil(estimatedChars / items.length));
  return Math.max(1, Math.min(max, Math.max(min, Math.floor(budgetChars / average))));
}

export function planGenerationUnits<T>(
  kind: GenerationPlanKind,
  items: T[],
  contextWindow?: number,
): GenerationUnit<T>[] {
  if (!items.length) return [];
  const size = dynamicBatchSize(kind, items, contextWindow);
  const units: GenerationUnit<T>[] = [];
  for (let offset = 0; offset < items.length; offset += size) {
    const batch = items.slice(offset, offset + size);
    const index = units.length + 1;
    units.push({
      index,
      phase: `${kind}_batch_${index}`,
      items: batch,
      dependencyHash: dependencyHash({ kind, items: batch }),
    });
  }
  return units;
}

export async function mapWithConcurrency<T, R>(
  items: T[],
  concurrency: number,
  worker: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  if (!items.length) return [];
  const values = new Array<R>(items.length);
  let cursor = 0;
  let failure: unknown;
  const limit = Math.max(1, Math.min(8, Math.floor(concurrency) || 1));
  const run = async (): Promise<void> => {
    while (true) {
      if (failure) return;
      const index = cursor++;
      if (index >= items.length) return;
      try {
        values[index] = await worker(items[index], index);
      } catch (error) {
        failure ??= error;
        return;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => run()));
  if (failure) throw failure;
  return values;
}

export function configuredGenerationConcurrency(kind: GenerationPlanKind): number {
  const key = kind === 'object'
    ? 'MODELING_OBJECT_GENERATION_CONCURRENCY'
    : kind === 'knowledge'
      ? 'MODELING_KNOWLEDGE_GENERATION_CONCURRENCY'
      : 'MODELING_OPERATION_GENERATION_CONCURRENCY';
  const fallback = kind === 'operation' ? 3 : 4;
  const value = Number(process.env[key] ?? fallback);
  return Number.isSafeInteger(value) && value > 0 ? Math.min(value, 8) : fallback;
}

export function retryDelayMs(error: unknown, attempt: number): number {
  const message = error instanceof Error ? error.message : String(error);
  const retryAfter = message.match(/retry[- ]after\s*[:=]?\s*(\d+)/iu);
  if (retryAfter) return Math.min(60_000, Number(retryAfter[1]) * 1_000);
  return Math.min(8_000, 500 * 2 ** Math.max(0, attempt - 1));
}

export async function waitBeforeGenerationRetry(error: unknown, attempt: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, retryDelayMs(error, attempt)));
}

/**
 * 模型调用共享的自适应门闩。429 后立刻把后续调用并发减半，并尊重 Retry-After；
 * 已经发出的请求不强制中断，避免产生未知提交状态。
 */
export class AdaptiveGenerationGate {
  private active = 0;
  private limit: number;
  private pausedUntil = 0;
  private waiters: Array<() => void> = [];

  constructor(initialConcurrency: number) {
    this.limit = Math.max(1, Math.min(8, Math.floor(initialConcurrency) || 1));
  }

  currentLimit(): number {
    return this.limit;
  }

  rateLimited(retryAfterMs?: number): void {
    this.limit = Math.max(1, Math.floor(this.limit / 2));
    this.pausedUntil = Math.max(this.pausedUntil, Date.now() + Math.max(500, retryAfterMs ?? 1_000));
  }

  async run<T>(work: () => Promise<T>): Promise<T> {
    await this.acquire();
    try {
      return await work();
    } finally {
      this.active -= 1;
      this.drain();
    }
  }

  private async acquire(): Promise<void> {
    while (true) {
      const delay = this.pausedUntil - Date.now();
      if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
      if (this.active < this.limit) {
        this.active += 1;
        return;
      }
      await new Promise<void>((resolve) => this.waiters.push(resolve));
    }
  }

  private drain(): void {
    const waiter = this.waiters.shift();
    waiter?.();
  }
}
