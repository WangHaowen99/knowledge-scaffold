// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { HttpClient } from '../../transport/http.js';
import type { A2AAgentCard } from '../../types/a2a.js';

export class A2AAgentCardApi {
  constructor(private readonly http: HttpClient) {}

  get(options: { signal?: AbortSignal } = {}): Promise<A2AAgentCard> {
    return this.http.get<A2AAgentCard>('/.well-known/a2a/agent-card.json', {
      signal: options.signal,
    });
  }
}
