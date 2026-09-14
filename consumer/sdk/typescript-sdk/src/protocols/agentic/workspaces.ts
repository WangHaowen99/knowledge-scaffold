// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { HttpClient } from '../../transport/http.js';
import type {
  CreateWorkspaceRequest,
  DeleteWorkspaceResponse,
  SetDefaultWorkspaceResponse,
  UpdateWorkspaceRequest,
  Workspace,
  WorkspaceListResponse,
} from '../../types/agentic.js';

export class AgenticWorkspacesApi {
  constructor(private readonly http: HttpClient) {}

  list(options: { signal?: AbortSignal } = {}): Promise<WorkspaceListResponse> {
    return this.http.get<WorkspaceListResponse>('/agentic/workspaces', {
      signal: options.signal,
    });
  }

  get(workspaceId: string, options: { signal?: AbortSignal } = {}): Promise<Workspace> {
    return this.http.get<Workspace>(`/agentic/workspaces/${encodeURIComponent(workspaceId)}`, {
      signal: options.signal,
    });
  }

  create(request: CreateWorkspaceRequest, options: { signal?: AbortSignal } = {}): Promise<Workspace> {
    return this.http.post<Workspace, CreateWorkspaceRequest>('/agentic/workspaces', {
      json: request,
      signal: options.signal,
    });
  }

  update(
    workspaceId: string,
    request: UpdateWorkspaceRequest,
    options: { signal?: AbortSignal } = {},
  ): Promise<Workspace> {
    return this.http.patch<Workspace, UpdateWorkspaceRequest>(
      `/agentic/workspaces/${encodeURIComponent(workspaceId)}`,
      {
        json: request,
        signal: options.signal,
      },
    );
  }

  delete(workspaceId: string, options: { signal?: AbortSignal } = {}): Promise<DeleteWorkspaceResponse> {
    return this.http.delete<DeleteWorkspaceResponse>(`/agentic/workspaces/${encodeURIComponent(workspaceId)}`, {
      signal: options.signal,
    });
  }

  setDefault(
    workspaceId: string,
    options: { signal?: AbortSignal } = {},
  ): Promise<SetDefaultWorkspaceResponse> {
    return this.http.post<SetDefaultWorkspaceResponse>(
      `/agentic/workspaces/${encodeURIComponent(workspaceId)}/default`,
      {
        signal: options.signal,
      },
    );
  }
}
