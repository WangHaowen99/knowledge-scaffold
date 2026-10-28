export interface KbStats {
  documents: number;
  completed: number;
  processing: number;
  failed: number;
  chunks: number;
  questions: number;
  enriching: number;
  wiki_pages: number;
}

export interface ChunkingConfig {
  strategy: 'heading' | 'separator';
  chunk_size: number;
  chunk_overlap: number;
  separators: string[];
}

export interface ParsingConfig {
  engine_rules: Record<string, string>;
}

export interface IndexingConfig {
  rag_enabled: boolean;
}

export interface SummaryConfig {
  enabled: boolean;
  model: string;
  instructions: string;
  table_instructions: string;
  input_chars: number;
}

export interface QuestionConfig {
  enabled: boolean;
  count: number;
  instructions: string;
  model: string;
}

export interface WikiConfig {
  enabled: boolean;
  model: string;
  granularity: 'focused' | 'standard' | 'exhaustive';
  extraction_instructions: string;
  content_instructions: string;
  max_pages_per_doc: number;
  include_in_search: boolean;
  boost: number;
  max_search_results: number;
}

export interface ProfileConfig {
  enabled: boolean;
  model: string;
  instructions: string;
}

export interface RetrievalConfig {
  vector_enabled: boolean;
  keyword_enabled: boolean;
  use_questions: boolean;
  vector_top_k: number;
  keyword_top_k: number;
  vector_threshold: number;
  keyword_threshold: number;
  rrf_k: number;
  vector_weight: number;
  keyword_weight: number;
  query_expansion: boolean;
  rerank_enabled: boolean;
  rerank_candidates: number;
  rerank_threshold: number;
  composite_score: boolean;
  mmr_enabled: boolean;
  mmr_lambda: number;
  merge_adjacent: boolean;
  expand_enabled: boolean;
  expand_min_chars: number;
  expand_max_chars: number;
  parent_child: boolean;
  parent_chunk_size: number;
}

export interface KbConfig {
  language: string;
  chunking: ChunkingConfig;
  parsing: ParsingConfig;
  indexing: IndexingConfig;
  summary: SummaryConfig;
  questions: QuestionConfig;
  wiki: WikiConfig;
  profile: ProfileConfig;
  retrieval: RetrievalConfig;
}

export type ConfigSection = Exclude<keyof KbConfig, 'language'>;

export interface ParserEngine {
  name: string;
  description: string;
  file_types: string[];
  available: boolean;
  unavailable_reason: string;
}

export interface ConfigMeta {
  defaults: KbConfig;
  models: { chat: string[]; default_chat: string; embedding: string; embedding_dim: number; rerank: string | null };
  overridable: Record<string, string[]>;
  parser_engines: ParserEngine[];
  file_types: string[];
}

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  embed_model: string;
  embed_dim: number;
  chunk_size: number;
  chunk_overlap: number;
  config: KbConfig;
  profile: string | null;
  profile_status: string;
  profile_updated_at: string | null;
  created_at: string;
  updated_at: string;
  stats: KbStats;
}

export interface DocumentItem {
  id: string;
  kb_id: string;
  title: string;
  file_name: string | null;
  file_type: string | null;
  file_size: number | null;
  folder_path: string;
  source_type: 'file' | 'text' | 'url';
  source_url: string | null;
  parse_status: string;
  stage: string | null;
  error_code: string | null;
  error_message: string | null;
  summary: string | null;
  summary_status: string;
  chunk_count: number;
  l1_chars: number | null;
  created_at: string;
  processed_at: string | null;
  engine?: string | null;
  question_status: string;
  question_count: number;
  wiki_status: string;
  wiki_error: string | null;
  process_overrides: Record<string, Record<string, unknown>>;
}

export interface Locator {
  type?: string;
  page?: number;
  slide?: number;
  sheet?: string;
  row_start?: number;
  row_end?: number;
  bbox?: number[];
}

export interface ChunkItem {
  id: string;
  seq: number;
  chunk_type: string;
  content: string;
  context_header: string;
  line_start: number;
  anchor_id: string | null;
  source_locators: Locator[];
  questions: string[];
}

export interface SearchHit {
  chunk_id: string;
  doc_id: string;
  kb_id: string;
  seq: number;
  chunk_type: string;
  title: string;
  file_name: string | null;
  context_header: string;
  content: string;
  score: number;
  line_start: number;
  anchor_id: string | null;
  locators: Locator[];
  matched_by: string[];
  matched_question: string | null;
  questions: string[];
  merged_chunk_ids: string[];
  expanded: boolean;
  core: [number, number] | null;
  relevance: number;
  scores: { vector: number | null; keyword: number | null; rerank: number | null; base: number | null };
  wiki_slug?: string;
  wiki_type?: string;
  wiki_sources?: string[];
}

export interface SearchTrace {
  counts: Record<string, number>;
  reranked: boolean;
  variants: string[];
  settings: RetrievalConfig;
  elapsed_ms: number;
}

export type WikiPageType = 'summary' | 'entity' | 'concept' | 'index';

export interface WikiPageItem {
  id: string;
  slug: string;
  title: string;
  page_type: WikiPageType;
  summary: string;
  aliases: string[];
  sources: number;
  in_count: number;
  out_count: number;
  version: number;
  last_edit_source: string;
  updated_at: string;
}

export interface WikiRef {
  slug: string;
  title: string;
  page_type: WikiPageType | null;
}

export interface WikiPage {
  id: string;
  kb_id: string;
  slug: string;
  title: string;
  page_type: WikiPageType;
  summary: string;
  content: string;
  aliases: string[];
  source_doc_ids: string[];
  version: number;
  last_edit_source: string;
  updated_at: string;
  links_to: WikiRef[];
  linked_from: WikiRef[];
  sources: { id: string; title: string; file_name: string | null }[];
  revisions: { version: number; title: string; summary: string; edit_source: string; created_at: string; chars: number }[];
}

export interface WikiStatus {
  enabled: boolean;
  pages: Partial<Record<WikiPageType, number>>;
  updated_at: string | null;
  documents: Record<string, number>;
  pending_jobs: number;
  errors: { id: string; title: string; wiki_error: string | null }[];
}

export interface WikiGraph {
  nodes: { slug: string; title: string; page_type: WikiPageType; degree: number }[];
  edges: { source: string; target: string }[];
}
