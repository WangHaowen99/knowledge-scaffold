export class OperationAlreadyRunningError extends Error {
  constructor() {
    super('operation already running');
    this.name = 'OperationAlreadyRunningError';
  }
}

export class InFlightOperations {
  private readonly active = new Map<string, string>();

  current(key: string): string | null {
    return this.active.get(key) ?? null;
  }

  async run<T>(key: string, operation: string, task: () => Promise<T>): Promise<T> {
    if (this.active.has(key)) throw new OperationAlreadyRunningError();
    this.active.set(key, operation);
    try {
      return await task();
    } finally {
      this.active.delete(key);
    }
  }
}
