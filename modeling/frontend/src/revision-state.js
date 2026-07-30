const STATUS_ALIASES = {
  add: 'added',
  added: 'added',
  create: 'added',
  created: 'added',
  modify: 'modified',
  modified: 'modified',
  update: 'modified',
  updated: 'modified',
  remove: 'deleted',
  removed: 'deleted',
  delete: 'deleted',
  deleted: 'deleted',
};

const CAUSE_LABELS = {
  project_created: '创建项目',
  migration: '导入历史内容',
  research: '完成资料整理',
  generate: '生成内容',
  generated: '生成内容',
  edit: '手动修改',
  manual_edit: '手动修改',
  agent_update: '智能修改',
  validation_fix: '修复问题',
  optimization: '优化内容',
  restore: '恢复内容',
  release: '发布',
};

const asText = (value) => (value == null ? '' : String(value));

const asNumber = (value, fallback = 0) => {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
};

const firstDefined = (source, names, fallback = undefined) => {
  for (const name of names) {
    if (source?.[name] !== undefined && source[name] !== null) return source[name];
  }
  return fallback;
};

const normalizeActor = (actor, source) => {
  const value = actor && typeof actor === 'object' ? actor : {};
  return {
    kind: asText(firstDefined(value, ['kind', 'type'], firstDefined(source, ['actorKind', 'actor_kind']))),
    displayName: asText(firstDefined(
      value,
      ['displayName', 'display_name', 'name'],
      firstDefined(source, ['actorName', 'actor_name']),
    )),
  };
};

export function normalizeVersion(input = {}) {
  const changedPaths = firstDefined(input, ['changedPaths', 'changed_paths'], []);
  const sequenceValue = firstDefined(input, ['sequence', 'number', 'ordinal'], null);
  const sequence = sequenceValue !== null && sequenceValue !== '' && Number.isFinite(Number(sequenceValue))
    ? Number(sequenceValue)
    : null;

  return {
    id: asText(firstDefined(input, ['id', 'revisionId', 'revision_id', 'oid'])),
    parentRevision: asText(firstDefined(input, ['parentRevision', 'parent_revision', 'parentOid', 'parent_oid'])),
    sequence,
    packageVersion: asText(firstDefined(input, ['packageVersion', 'package_version', 'version'])),
    cause: asText(firstDefined(input, ['cause', 'reason', 'kind'])),
    summary: asText(firstDefined(input, ['summary', 'message', 'note'])),
    actor: normalizeActor(input.actor, input),
    changedPaths: Array.isArray(changedPaths) ? changedPaths.map(asText).filter(Boolean) : [],
    createdAt: asText(firstDefined(input, ['createdAt', 'created_at', 'timestamp'])),
  };
}

export function normalizeVersions(input) {
  const source = Array.isArray(input)
    ? input
    : firstDefined(input, ['versions', 'revisions', 'items'], []);

  return (Array.isArray(source) ? source : [])
    .map(normalizeVersion)
    .filter((version) => version.id)
    .sort((left, right) => {
      if (left.sequence !== null || right.sequence !== null) {
        return (right.sequence ?? -1) - (left.sequence ?? -1);
      }
      const leftTime = Date.parse(left.createdAt) || 0;
      const rightTime = Date.parse(right.createdAt) || 0;
      return rightTime - leftTime;
    });
}

const normalizeLine = (line = {}) => ({
  type: ['context', 'add', 'remove'].includes(line.type)
    ? line.type
    : line.type === 'added' ? 'add' : line.type === 'deleted' ? 'remove' : 'context',
  content: asText(firstDefined(line, ['content', 'text'])),
});

const normalizeHunk = (hunk = {}) => ({
  oldStart: Math.max(0, asNumber(firstDefined(hunk, ['oldStart', 'old_start']), 0)),
  newStart: Math.max(0, asNumber(firstDefined(hunk, ['newStart', 'new_start']), 0)),
  lines: Array.isArray(hunk.lines) ? hunk.lines.map(normalizeLine) : [],
});

export function normalizeDiff(input = {}) {
  const files = Array.isArray(input.files) ? input.files : [];
  return {
    fromRevision: asText(firstDefined(input, ['fromRevision', 'from_revision', 'from'])),
    toRevision: asText(firstDefined(input, ['toRevision', 'to_revision', 'to'])),
    files: files.map((file) => {
      const hunks = Array.isArray(file.hunks) ? file.hunks.map(normalizeHunk) : [];
      const lineTypes = hunks.flatMap((hunk) => hunk.lines.map((line) => line.type));
      return {
        path: asText(firstDefined(file, ['path', 'filePath', 'file_path'])),
        status: STATUS_ALIASES[asText(file.status).toLowerCase()] || 'modified',
        additions: Math.max(0, asNumber(file.additions, lineTypes.filter((type) => type === 'add').length)),
        deletions: Math.max(0, asNumber(file.deletions, lineTypes.filter((type) => type === 'remove').length)),
        hunks,
        binary: Boolean(firstDefined(file, ['binary', 'isBinary', 'is_binary'], false)),
      };
    }).filter((file) => file.path),
  };
}

export function summarizeDiff(input) {
  const diff = normalizeDiff(input);
  return diff.files.reduce((summary, file) => {
    summary.files += 1;
    summary.additions += file.additions;
    summary.deletions += file.deletions;
    summary[file.status] += 1;
    return summary;
  }, { files: 0, added: 0, modified: 0, deleted: 0, additions: 0, deletions: 0 });
}

export function versionTitle(input) {
  const version = normalizeVersion(input);
  if (version.sequence !== null) return `版本 ${version.sequence}`;
  if (version.packageVersion) return `版本 ${version.packageVersion.replace(/^v/i, '')}`;
  return '版本';
}

export function versionSummary(input) {
  const version = normalizeVersion(input);
  return version.summary || CAUSE_LABELS[version.cause] || '内容更新';
}

export function versionActor(input) {
  const version = normalizeVersion(input);
  if (version.actor.displayName) return version.actor.displayName;
  if (['agent', 'assistant', 'ai'].includes(version.actor.kind)) return '智能助手';
  if (['user', 'expert'].includes(version.actor.kind)) return '用户';
  return '系统';
}

export function makeRestoreRequest(targetRevision, expectedRevision) {
  const target = asText(targetRevision).trim();
  const expected = asText(expectedRevision).trim();
  if (!target) throw new Error('请选择要恢复的版本');
  if (!expected) throw new Error('当前版本无效，请刷新后重试');
  if (target === expected) throw new Error('当前已是这个版本');
  return { targetRevision: target, expectedRevision: expected };
}

export function safeUiError(value, fallback = '操作失败，请重试') {
  const message = asText(value?.message ?? value).trim();
  if (!message || message.length > 240) return fallback;
  if (/(?:^|[^a-z])(?:git|revision|artifact|commit)(?:[^a-z]|$)/i.test(message)) return fallback;
  return message;
}
