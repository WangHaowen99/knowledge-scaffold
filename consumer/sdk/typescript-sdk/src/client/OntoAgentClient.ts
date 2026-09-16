// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { ChatFacade } from '../facades/chat.js';
import { AgenticOntologiesApi } from '../protocols/agentic/ontologies.js';
import { AgenticRunsApi } from '../protocols/agentic/runs.js';
import { AgenticSessionsApi } from '../protocols/agentic/sessions.js';
import { AgenticSkillsApi } from '../protocols/agentic/skills.js';
import { AgenticWorkspacesApi } from '../protocols/agentic/workspaces.js';
import { A2AAgentCardApi } from '../protocols/a2a/agentCard.js';
import { A2ATasksApi } from '../protocols/a2a/tasks.js';
import { HttpClient } from '../transport/http.js';
import type { ClientOptions } from '../types/common.js';

export class OntoAgentClient {
  readonly http: HttpClient;
  readonly agentic: {
    runs: AgenticRunsApi;
    sessions: AgenticSessionsApi;
    skills: AgenticSkillsApi;
    ontologies: AgenticOntologiesApi;
    workspaces: AgenticWorkspacesApi;
  };
  readonly a2a: {
    agentCard: A2AAgentCardApi;
    tasks: A2ATasksApi;
  };
  readonly chat: ChatFacade;

  constructor(options: ClientOptions) {
    this.http = new HttpClient(options);

    const runs = new AgenticRunsApi(this.http);
    const sessions = new AgenticSessionsApi(this.http);
    const skills = new AgenticSkillsApi(this.http);
    const ontologies = new AgenticOntologiesApi(this.http);
    const workspaces = new AgenticWorkspacesApi(this.http);
    const agentCard = new A2AAgentCardApi(this.http);
    const tasks = new A2ATasksApi(this.http);

    this.agentic = {
      runs,
      sessions,
      skills,
      ontologies,
      workspaces,
    };

    this.a2a = {
      agentCard,
      tasks,
    };

    this.chat = new ChatFacade(runs, sessions);
  }

  setToken(token: string | null | undefined): void {
    this.http.setToken(token);
  }

  setWorkspace(workspaceId: string | null | undefined): void {
    this.http.setWorkspace(workspaceId);
  }
}
