import {
  PackageArtifactStoreError,
  type PackageArtifactStoreErrorCode,
} from './package-store.js';
import {
  ArtifactRepositoryError,
  type ArtifactRepositoryErrorCode,
} from './repository.js';
import {
  ArtifactRevisionIndexError,
  type ArtifactRevisionIndexErrorCode,
} from './revision-repo.js';

export interface ArtifactHttpError {
  source: 'repository' | 'package' | 'revision-index';
  code: ArtifactRepositoryErrorCode | PackageArtifactStoreErrorCode | ArtifactRevisionIndexErrorCode;
  statusCode: 400 | 404 | 409 | 422 | 503;
  message: string;
}

type ArtifactHttpErrorDefinition = Pick<ArtifactHttpError, 'statusCode' | 'message'>;

export const ARTIFACT_REPOSITORY_HTTP_ERRORS = {
  invalid_project: { statusCode: 400, message: '项目编号格式不正确' },
  invalid_path: { statusCode: 400, message: '文件路径不正确' },
  invalid_input: { statusCode: 400, message: '请求内容不正确，请检查后重试' },
  not_initialized: { statusCode: 404, message: '项目还没有版本记录' },
  revision_not_found: { statusCode: 404, message: '找不到这个版本' },
  revision_conflict: { statusCode: 409, message: '内容已更新，请刷新后重试' },
  empty_change: { statusCode: 400, message: '没有要保存的改动' },
  no_change: { statusCode: 409, message: '内容没有变化' },
  workspace_not_empty: { statusCode: 409, message: '项目目录已有文件' },
  invalid_repository: { statusCode: 503, message: '版本库暂时不可用，请稍后重试' },
} as const satisfies Record<ArtifactRepositoryErrorCode, ArtifactHttpErrorDefinition>;

export const PACKAGE_ARTIFACT_HTTP_ERRORS = {
  invalid_input: { statusCode: 400, message: '请求内容不正确，请检查后重试' },
  project_not_found: { statusCode: 404, message: '项目不存在' },
  package_not_found: { statusCode: 404, message: '知识包不存在' },
  file_not_found: { statusCode: 404, message: '文件不存在' },
  revision_has_no_package: { statusCode: 404, message: '该版本没有知识包' },
  invalid_package: { statusCode: 422, message: '知识包内容不正确，请检查后重试' },
  commit_in_progress: { statusCode: 409, message: '知识包正在保存，请稍后重试' },
  candidate_not_found: { statusCode: 404, message: '候选修订不存在' },
  candidate_not_pending: { statusCode: 409, message: '候选修订已处理，请刷新后重试' },
  candidate_validation_failed: { statusCode: 422, message: '候选修订未通过校验，请检查后重试' },
} as const satisfies Record<PackageArtifactStoreErrorCode, ArtifactHttpErrorDefinition>;

/** 修订索引与 Git 仓库使用不同的错误类型，但对外遵循同一工件契约。 */
export const ARTIFACT_REVISION_INDEX_HTTP_ERRORS = {
  invalid_input: { statusCode: 400, message: '修订索引请求内容不正确，请检查后重试' },
  project_not_found: { statusCode: 404, message: '项目不存在' },
} as const satisfies Record<ArtifactRevisionIndexErrorCode, ArtifactHttpErrorDefinition>;

export function mapArtifactHttpError(error: unknown): ArtifactHttpError | null {
  if (error instanceof ArtifactRepositoryError) {
    return {
      source: 'repository',
      code: error.code,
      ...ARTIFACT_REPOSITORY_HTTP_ERRORS[error.code],
    };
  }
  if (error instanceof PackageArtifactStoreError) {
    return {
      source: 'package',
      code: error.code,
      ...PACKAGE_ARTIFACT_HTTP_ERRORS[error.code],
    };
  }
  if (error instanceof ArtifactRevisionIndexError) {
    return {
      source: 'revision-index',
      code: error.code,
      ...ARTIFACT_REVISION_INDEX_HTTP_ERRORS[error.code],
    };
  }
  return null;
}
