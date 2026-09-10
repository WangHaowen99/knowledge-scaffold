const PHASE_LABELS = {
  intake: '资料准备',
  goal: '目标确认',
  process: '流程建模',
  knowledge: '知识建模',
  increment: '增量建模',
  validation: '确定性验证',
  evaluation: '正式评测',
  release: '发布交付',
};

/* 这些 key 同时是普通英文单词。整段英文散文里出现 "ask the user"、"the revision"
   时不能替换，否则会把句子改成病句（见 #16）；只在中文语境或反引号里才当作内部术语。 */
const AMBIGUOUS_TOKEN_LABELS = {
  focus: '定位内容',
  notify: '更新进度',
  ask: '业务问题',
  diff: '查看变更对照',
  revision: '版本',
  consumer: '下游应用',
};

const TOKEN_LABELS = {
  list_documents: '查看资料清单',
  read_document: '读取资料',
  set_material_directive: '保存资料设置',
  read_documents_fully: '全文精读资料',
  search_documents: '检索资料',
  list_tables: '查看数据表',
  describe_table: '分析数据表',
  read_table_rows: '读取表格样例',
  stage_tabular_dataset: '同步完整数据表',
  sync_schema_objects: '按数据 Schema 同步对象',
  list_dir: '查看知识包目录',
  read_file: '读取知识内容',
  write_file: '整理待确认修改',
  delete_file: '整理候选删除',
  regroup_changes: '整理确认分组',
  grep_package: '检索知识包',
  request_release: '发布确认',
  release_preflight: '发布条件核对',
  get_project_state: '当前进展核对',
  transition_project_state: '进入下一阶段',
  update_project_context: '保存当前进展',
  update_work_notes: '更新工作笔记',
  update_plan: '更新历史工作笔记',
  run_evaluation: '运行评测',
  get_evaluation_results: '查看评测结果',
  gate_status: '条件核对',
  analyze_deterministic_capabilities: '分析确定性计算范围',
  confirm_deterministic_scope: '确认脚本范围',
  who_references: '核对引用关系',
  analyze_material_impact: '核对资料变更影响',
  list_package_sources: '查看来源知识包',
  analyze_package_merge: '核对融合范围',
  read_package_source_file: '读取来源包内容',
  stage_package_merge: '生成融合候选',
  request_confirmation: '内容确认',
  claim_candidate_workspace: '准备本次修改',
  evaluation_passed: '正式评测',
  ready_for_eval: '评测准备',
  product_ledger_verified: '产品清单台账',
  deterministic_confirmed: '确定性计算范围确认',
  releasable: '发布条件',
  query_eval_db: '查询评测数据',
  run_script_test: '测试确定性脚本',
  agent_project_workflow_state: '项目进展',
  project_agent_state: '项目进展',
  modeling_leader: '建模助手',
  material_analyst: '资料分析',
  knowledge_extractor: '知识分析',
  merge_analyst: '融合分析',
};

const DATABASE_FAILURE = /(?:评测库查询失败[：:]?\s*)?Unknown database\s+['"][^'"]+['"]/gi;
const MODEL_QUOTA_FAILURE = /^.*(?:Allocated quota exceeded|insufficient_quota|RateLimitError|Error code:\s*429).*$/gim;
const INTERNAL_RESOURCE = /\b(?:mv_shadow_)?pj[_:-][A-Za-z0-9:_-]+\b/gi;
const RAW_MATERIAL_REFERENCE = /material:(?:doc|rd)[_:-][A-Za-z0-9:_-]+#[^\s，。；、)\]}>]*/gi;
const INTERNAL_DOCUMENT_ID = /\bdoc_(?:[A-Za-z0-9][A-Za-z0-9_-]*)?/gi;
const INTERNAL_ID = /\b(rd|tc|run|session|interaction)[_:-][A-Za-z0-9:_-]+\b/gi;
const CHECKPOINT_ID = /\bcp[-_][A-Za-z0-9-]+\b/gi;
const HEX_REVISION = /\b(?=[a-f0-9]{7,64}\b)(?=[a-f0-9]*[a-f])[a-f0-9]{7,64}\b/gi;
const STATE_CODE = /\b(intake|goal|process|knowledge|increment|validation|evaluation|release):(idle|queued|running|waiting|suspended|completed|failed|cancelled)\b/gi;
const IMPLEMENTATION_TOKEN = new RegExp(`\\b(${Object.keys(TOKEN_LABELS).join('|')})\\b`, 'gi');
const AMBIGUOUS_TOKEN = new RegExp(`\\b(${Object.keys(AMBIGUOUS_TOKEN_LABELS).join('|')})\\b`, 'gi');
const CJK = /[\u3400-\u9fff\uf900-\ufaff]/;

/** 歧义术语只在中文语境或反引号包裹时替换，英文散文里保持原样。 */
export function replaceAmbiguousTokens(value) {
  return String(value).replace(AMBIGUOUS_TOKEN, (match, token, offset, whole) => {
    const before = whole.slice(Math.max(0, offset - 4), offset);
    const after = whole.slice(offset + match.length, offset + match.length + 4);
    const quoted = before.endsWith('`') && after.startsWith('`');
    if (!quoted && !CJK.test(before) && !CJK.test(after)) return match;
    return AMBIGUOUS_TOKEN_LABELS[token.toLowerCase()] || match;
  });
}
const INTERNAL_LINK = /(?:https?:\/\/(?:localhost|127\.0\.0\.1)[^\s)\]}]*|https?:\/\/[^\s)\]}]+\/api\/[^\s)\]}]*|\/api\/[^\s)\]}]*)/gi;
const MODELING_PATH = /\.modeling(?:\/[A-Za-z0-9._/-]+)?/gi;
const DISCLOSURE_SUMMARY = /<summary(?:\s[^>]*)?>([\s\S]*?)<\/summary>/gi;
const DISCLOSURE_CONTAINER = /<\/?details(?:\s[^>]*)?>/gi;
const UNRESOLVED_IMPACT_FIELD = /\bscopeResolved\s*=\s*false\b/gi;
const RESOLVED_IMPACT_FIELD = /\bscopeResolved\s*=\s*true\b/gi;
const IMPACT_PARAMETER = /\b(?:direct_resources|directResources)\b/gi;
const TECHNICAL_CAPABILITY_CLAIM = /[^。！？\n]*(?:工具集中没有|工具集没有|没有(?:该|读取)能力|本会话工具|平台侧能力缺口|平台能力缺口|平台是否开放)[^。！？\n]*[。！？]?/giu;

export function userFacingText(value) {
  if (value == null) return '';
  return String(value)
    .replace(DISCLOSURE_SUMMARY, '\n\n**$1**\n\n')
    .replace(DISCLOSURE_CONTAINER, '\n')
    .replace(UNRESOLVED_IMPACT_FIELD, '影响范围尚未界定')
    .replace(RESOLVED_IMPACT_FIELD, '影响范围已界定')
    .replace(IMPACT_PARAMETER, '直接影响范围')
    .replace(TECHNICAL_CAPABILITY_CLAIM, '当前步骤需要系统继续核对，现有进展已保留。')
    .replace(MODEL_QUOTA_FAILURE, '模型额度暂不可用，本轮已暂停；候选和进度已保留，可稍后继续。')
    .replace(DATABASE_FAILURE, '评测数据暂时不可用')
    .replace(INTERNAL_RESOURCE, '当前处理')
    .replace(RAW_MATERIAL_REFERENCE, '对应资料的原文位置')
    .replace(INTERNAL_DOCUMENT_ID, '资料')
    .replace(INTERNAL_ID, (_, prefix) => {
      const normalized = prefix.toLowerCase();
      if (normalized === 'rd') return '本次评测';
      if (normalized === 'tc') return '该用例';
      return '当前处理';
    })
    .replace(CHECKPOINT_ID, '最近保存点')
    .replace(HEX_REVISION, '当前版本')
    .replace(STATE_CODE, (_, phase) => PHASE_LABELS[phase.toLowerCase()] || '当前阶段')
    .replace(IMPLEMENTATION_TOKEN, (token) => TOKEN_LABELS[token.toLowerCase()] || token)
    .replace(AMBIGUOUS_TOKEN, (match, token, offset, whole) => {
      const before = whole.slice(Math.max(0, offset - 4), offset);
      const after = whole.slice(offset + match.length, offset + match.length + 4);
      const quoted = before.endsWith('`') && after.startsWith('`');
      if (!quoted && !CJK.test(before) && !CJK.test(after)) return match;
      return AMBIGUOUS_TOKEN_LABELS[token.toLowerCase()] || match;
    })
    .replace(INTERNAL_LINK, '内部链接')
    .replace(MODELING_PATH, '项目工作记录');
}
