export type AgentMode = 'quick' | 'thinking' | 'multi_agent';

export interface AgenticRunContext {
  selected_ontology_id?: string;
  agent_mode?: AgentMode;
  llm_request_mode?: 'stream' | 'sync';
  [key: string]: unknown;
}

export interface AgenticInteractionResponse {
  interaction_id: string;
  type?: string;
  value?: unknown;
  option_id?: string;
}

export interface AgenticRunLinks {
  self: string;
  events: string;
  cancel: string;
}

export interface AgenticRunError {
  error_code: string;
  message: string;
  retriable: boolean;
  source?: string;
  details?: Record<string, unknown>;
}

export interface StartAgenticRunRequest {
  session_id: string;
  request: string;
  agent_mode?: AgentMode;
  llm_request_mode?: 'stream' | 'sync';
  model_profile_id?: string;
  arguments?: Record<string, unknown>;
  include_trace?: boolean;
  interaction_response?: AgenticInteractionResponse;
}

export interface StartAgenticRunResponse {
  run_id: string;
  session_id: string;
  turn_id: number;
  status: string;
  created: boolean;
  links: AgenticRunLinks;
}

export interface AgenticRunStatus {
  run_id: string;
  session_id: string;
  turn_id: number;
  status: string;
  links: AgenticRunLinks;
  created?: boolean;
  last_seq?: number;
  created_at?: string | null;
  updated_at?: string | null;
  result?: unknown;
  error?: AgenticRunError | null;
  pending_interaction?: Record<string, unknown> | null;
}

export interface CancelRunRequest {
  reason?: string;
}

export interface SessionSummary {
  id: string;
  title?: string;
  created_at?: string;
  updated_at?: string;
  message_count?: number;
  metadata?: Record<string, unknown>;
  active_run_id?: string | null;
  active_run_status?: string;
  last_seq?: number;
  pending_interaction?: Record<string, unknown> | null;
}

export interface SessionMessage {
  id: string;
  session_id: string;
  role: string;
  content: string;
  timestamp?: string;
  metadata?: Record<string, unknown>;
}

export interface SessionEvent {
  event: string;
  data?: unknown;
  ts?: number;
}

export interface SessionDetail extends SessionSummary {
  messages?: SessionMessage[];
  events?: SessionEvent[];
}

export interface SessionListResponse {
  sessions: SessionSummary[];
}

export interface SessionPageResponse extends SessionListResponse {
  next_cursor: string | null;
  has_more: boolean;
  index_revision: number;
}

export interface SessionMessagesResponse {
  messages: SessionMessage[];
}

export interface CreateSessionRequest {
  title?: string;
  llm_request_mode?: 'stream' | 'sync';
}

export interface GenerateTitleRequest {
  message: string;
}

export interface GenerateTitleResponse {
  title: string;
  session_id: string;
  fallback?: boolean;
}

export interface SkillSummary {
  name: string;
  description?: string;
  file_path?: string;
  allowed_tools?: string[];
}

export interface SkillDetail extends SkillSummary {
  content?: string;
}

export interface SkillListResponse {
  skills: SkillSummary[];
}

export interface OntologySummary {
  id: string;
  name?: string;
  description?: string;
  version?: string;
  language?: string;
  manifest_path?: string;
  index_path?: string;
}

export interface OntologyListResponse {
  ontologies: OntologySummary[];
}

export interface OntologyFileSummary {
  name: string;
  size?: number;
  updated_at?: number;
}

export interface OntologyFileListResponse {
  folder: string;
  label?: string;
  files: OntologyFileSummary[];
}

export interface OntologyFileResponse {
  folder: string;
  label?: string;
  name: string;
  content: string;
}

export interface PutOntologyFileRequest {
  folder: 'operations' | 'objects';
  name: string;
  content: string;
}

export interface PutOntologyFileResponse {
  success: boolean;
  folder: string;
  label?: string;
  name: string;
}

export interface DeleteOntologyResponse {
  success: boolean;
  ontology_id: string;
}

export interface UploadOntologyFileResponse {
  success: boolean;
  folder: string;
  label?: string;
  name: string;
  overwritten?: boolean;
}

export interface ImportOntologyZipResponse {
  success: boolean;
  created?: boolean;
  overwritten?: boolean;
  ontology_id?: string;
  ontology_name?: string;
  source_root?: string;
  objects_count?: number;
  operations_count?: number;
  objects_index_path?: string;
  operations_index_path?: string;
  warnings?: Record<string, unknown>;
}

export interface Workspace {
  id: string;
  name: string;
  description?: string;
  created_at?: string;
  updated_at?: string;
  is_default?: boolean;
}

export interface WorkspaceListResponse {
  workspaces: Workspace[];
  total?: number;
  default_workspace_id?: string;
}

export interface CreateWorkspaceRequest {
  id?: string;
  name: string;
  description?: string;
  set_as_default?: boolean;
}

export interface UpdateWorkspaceRequest {
  name?: string;
  description?: string;
}

export interface DeleteWorkspaceResponse {
  success: boolean;
  id: string;
}

export interface SetDefaultWorkspaceResponse {
  workspace: Workspace;
  default_workspace_id: string;
}
