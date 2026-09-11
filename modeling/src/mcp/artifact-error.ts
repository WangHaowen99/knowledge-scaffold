import { PackageArtifactStoreError } from '../artifacts/package-store.js';
import { ArtifactRepositoryError } from '../artifacts/repository.js';
import {
  ModelingMcpToolError,
  type ModelingMcpToolDetails,
  type ModelingMcpToolResourceRef,
} from './tool-error.js';

export interface ModelingMcpArtifactErrorContext {
  phase: 'input' | 'load' | 'validate' | 'persist';
  resource?: ModelingMcpToolResourceRef;
}

function details(
  context: ModelingMcpArtifactErrorContext,
  reasonCode: string,
  retryable: boolean,
  extra: Record<string, unknown> = {},
): ModelingMcpToolDetails {
  return {
    phase: context.phase,
    ...(context.resource ? { resource: context.resource } : {}),
    reason_code: reasonCode,
    retryable,
    ...extra,
  };
}

export function mapMcpArtifactError(
  error: unknown,
  context: ModelingMcpArtifactErrorContext,
): ModelingMcpToolError {
  if (error instanceof ModelingMcpToolError) {
    if (error.details.phase) return error;
    return new ModelingMcpToolError(error.code, error.message, error.statusCode, error.next, {
      ...details(
        context,
        error.code,
        error.code === 'REVISION_CONFLICT' || error.code === 'PROJECT_WRITE_BUSY',
      ),
      ...error.details,
    });
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'revision_not_found') {
    return new ModelingMcpToolError(
      'RESOURCE_NOT_FOUND',
      '指定的 revision 不存在',
      404,
      ['使用最新 revision 或从第一页重新读取'],
      details(context, 'REVISION_NOT_FOUND', false),
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'not_initialized') {
    return new ModelingMcpToolError(
      'RESOURCE_NOT_FOUND',
      '项目还没有版本记录',
      404,
      ['先初始化项目或导入知识包后再调用 MCP 工具'],
      details(context, 'PROJECT_NOT_INITIALIZED', false),
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'invalid_project') {
    return new ModelingMcpToolError(
      'INVALID_INPUT',
      '项目编号格式不正确',
      400,
      [],
      details(context, 'INVALID_PROJECT', false),
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'invalid_path') {
    return new ModelingMcpToolError(
      'INVALID_INPUT',
      '文件路径不正确',
      400,
      [],
      details(context, 'INVALID_PATH', false),
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'invalid_input') {
    return new ModelingMcpToolError(
      'INVALID_INPUT',
      '请求内容不正确，请检查后重试',
      400,
      [],
      details(context, 'INVALID_INPUT', false),
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'empty_change') {
    return new ModelingMcpToolError(
      'INVALID_INPUT',
      '没有要保存的改动',
      400,
      [],
      details(context, 'EMPTY_CHANGE', false),
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'workspace_not_empty') {
    return new ModelingMcpToolError(
      'RESOURCE_ALREADY_EXISTS',
      '项目目录已有文件',
      409,
      [],
      details(context, 'WORKSPACE_NOT_EMPTY', false),
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'invalid_repository') {
    return new ModelingMcpToolError(
      'SUBMISSION_BLOCKED',
      '版本库状态不正确，暂时无法保存修改',
      503,
      ['检查项目版本库状态后重试'],
      details(context, 'INVALID_REPOSITORY', true),
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'no_change') {
    return new ModelingMcpToolError(
      'NO_CHANGES',
      '提交内容与当前资源一致，没有产生修改',
      409,
      [],
      details(context, 'NO_CHANGES', false),
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'revision_conflict') {
    return new ModelingMcpToolError(
      'REVISION_CONFLICT',
      '知识包内容已更新，请刷新后重试',
      409,
      ['重新读取资源和 revision 后再次分析'],
      details(context, 'REVISION_CONFLICT', true, {
        expected_revision: error.details?.expectedRevision,
        actual_revision: error.details?.actualRevision,
      }),
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'commit_in_progress') {
    return new ModelingMcpToolError(
      'PROJECT_WRITE_BUSY',
      '知识包正在保存，请稍后重试',
      409,
      ['短暂退避后重试；若 revision 已变化则重新分析'],
      details(context, 'PROJECT_WRITE_BUSY', true),
    );
  }
  if (error instanceof PackageArtifactStoreError
    && ['package_not_found', 'revision_has_no_package'].includes(error.code)) {
    return new ModelingMcpToolError(
      'RESOURCE_NOT_FOUND',
      '当前项目还没有可用的知识包版本',
      404,
      ['先生成或导入知识包后再调用 MCP 工具'],
      details(context, 'PACKAGE_NOT_FOUND', false),
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'project_not_found') {
    return new ModelingMcpToolError(
      'RESOURCE_NOT_FOUND',
      '项目不存在',
      404,
      [],
      details(context, 'PROJECT_NOT_FOUND', false),
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'file_not_found') {
    return new ModelingMcpToolError(
      'RESOURCE_NOT_FOUND',
      '文件不存在',
      404,
      ['确认文件路径来自当前 revision 后重试'],
      details(context, 'FILE_NOT_FOUND', false),
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'candidate_not_found') {
    return new ModelingMcpToolError(
      'RESOURCE_NOT_FOUND',
      '候选修订不存在',
      404,
      ['重新读取当前候选列表后重试'],
      details(context, 'CANDIDATE_NOT_FOUND', false),
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'candidate_not_pending') {
    return new ModelingMcpToolError(
      'CANDIDATE_CONFLICT',
      '候选修订已处理，请刷新后重试',
      409,
      ['重新读取候选状态后再提交'],
      details(context, 'CANDIDATE_NOT_PENDING', false),
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'candidate_validation_failed') {
    return new ModelingMcpToolError(
      'SUBMISSION_BLOCKED',
      '候选修订未通过校验，请先修正知识包问题',
      422,
      ['先查看校验结果并修正候选内容'],
      details(context, 'CANDIDATE_VALIDATION_FAILED', false),
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'invalid_package') {
    return new ModelingMcpToolError(
      'SUBMISSION_BLOCKED',
      '知识包内容不完整，无法执行当前修改',
      422,
      ['先修复当前知识包存量结构问题后重试'],
      details(context, 'PACKAGE_INTEGRITY_FAILED', false),
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'invalid_input') {
    return new ModelingMcpToolError(
      'INVALID_INPUT',
      '请求内容不正确，请检查后重试',
      400,
      [],
      details(context, 'INVALID_INPUT', false),
    );
  }
  throw error;
}
