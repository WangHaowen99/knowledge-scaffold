// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import type { AgenticRunContext } from './agentic.js';

export type A2ATaskState =
  | 'pending'
  | 'in_progress'
  | 'completed'
  | 'failed'
  | 'canceled'
  | 'not_found';

export interface A2ATaskSnapshot {
  id: string;
  session_id?: string | null;
  state: A2ATaskState | string;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
  result?: unknown;
  metadata?: Record<string, unknown>;
}

export interface A2ATaskResponse {
  task: A2ATaskSnapshot;
}

export interface A2ACancelTaskResponse {
  success: boolean;
  task: A2ATaskSnapshot;
}

export interface A2AInputPart {
  text: string;
}

export interface A2AInput {
  parts: A2AInputPart[];
}

export interface A2ACreateTaskRequest {
  message?: string;
  session_id?: string;
  task_id?: string;
  context?: AgenticRunContext;
  input?: A2AInput;
}

export interface A2AStreamCreateRequest {
  message: string;
  session_id: string;
  task_id?: string;
  context?: AgenticRunContext;
}

export interface A2AAgentCard {
  name: string;
  description?: string;
  version?: string;
  url?: string;
  skills?: string[];
  capabilities?: {
    streaming?: boolean;
    cancellation?: boolean;
  };
  endpoints?: Record<string, string>;
  authentication?: {
    type?: string;
    notes?: string;
  };
}
