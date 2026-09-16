// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { HttpClient } from '../../transport/http.js';
import type { SkillDetail, SkillListResponse, SkillSummary } from '../../types/agentic.js';

export class AgenticSkillsApi {
  constructor(private readonly http: HttpClient) {}

  async list(options: { signal?: AbortSignal } = {}): Promise<SkillSummary[]> {
    const response = await this.http.get<SkillListResponse>('/agentic/skills', {
      signal: options.signal,
    });
    return response.skills;
  }

  get(name: string, options: { signal?: AbortSignal } = {}): Promise<SkillDetail> {
    return this.http.get<SkillDetail>(`/agentic/skills/${encodeURIComponent(name)}`, {
      signal: options.signal,
    });
  }
}
