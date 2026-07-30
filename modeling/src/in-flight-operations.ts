export class OperationAlreadyRunningError extends Error {
  constructor() {
    super('operation already running');
    this.name = 'OperationAlreadyRunningError';
  }
}

export class InFlightOperations {
  private readonly active = new Map<string, { operation: string; startedAt: string }>();

  current(key: string): string | null {
    return this.active.get(key)?.operation ?? null;
  }

  status(key: string): { operation: string; startedAt: string } | null {
    const value = this.active.get(key);
    return value ? { ...value } : null;
  }

  currentByPrefix(prefix: string): string | null {
    for (const [key, value] of this.active) {
      if (key.startsWith(prefix)) return value.operation;
    }
    return null;
  }

  async run<T>(key: string, operation: string, task: () => Promise<T>): Promise<T> {
    if (this.active.has(key)) throw new OperationAlreadyRunningError();
    this.active.set(key, { operation, startedAt: new Date().toISOString() });
    try {
      return await task();
    } finally {
      this.active.delete(key);
    }
  }
}
