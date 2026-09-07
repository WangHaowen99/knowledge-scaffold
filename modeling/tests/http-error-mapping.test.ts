import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ARTIFACT_REPOSITORY_HTTP_ERRORS,
  mapArtifactHttpError,
  PACKAGE_ARTIFACT_HTTP_ERRORS,
} from '../src/artifacts/http-error.js';
import { PackageArtifactStoreError } from '../src/artifacts/package-store.js';
import { ArtifactRepositoryError } from '../src/artifacts/repository.js';
import { EvaluationAuthenticationError } from '../src/evaluation/auth-session.js';
import { mapEvaluationHttpError } from '../src/evaluation/http-error.js';
import { mapMcpArtifactError } from '../src/mcp/artifact-error.js';
import { ModelingMcpToolError } from '../src/mcp/tool-error.js';

const PRIVATE_DETAIL = 'synthetic backend detail\nError: internal stack';

test('ArtifactRepositoryError 的 HTTP 状态和公开文案完整映射', () => {
  const expected = {
    invalid_project: 400,
    invalid_path: 400,
    invalid_input: 400,
    not_initialized: 404,
    revision_not_found: 404,
    revision_conflict: 409,
    empty_change: 400,
    no_change: 409,
    workspace_not_empty: 409,
    invalid_repository: 503,
  } as const;

  assert.deepEqual(Object.keys(ARTIFACT_REPOSITORY_HTTP_ERRORS).sort(), Object.keys(expected).sort());
  for (const [code, statusCode] of Object.entries(expected)) {
    const mapped = mapArtifactHttpError(new ArtifactRepositoryError(
      code as keyof typeof expected,
      PRIVATE_DETAIL,
    ));
    assert.equal(mapped?.source, 'repository', code);
    assert.equal(mapped?.code, code, code);
    assert.equal(mapped?.statusCode, statusCode, code);
    assert.ok(mapped?.message, code);
    assert.doesNotMatch(mapped!.message, /synthetic backend|internal stack/iu, code);
  }
});

test('PackageArtifactStoreError 的 HTTP 状态和公开文案完整映射', () => {
  const expected = {
    invalid_input: 400,
    project_not_found: 404,
    package_not_found: 404,
    file_not_found: 404,
    revision_has_no_package: 404,
    invalid_package: 422,
    commit_in_progress: 409,
    candidate_not_found: 404,
    candidate_not_pending: 409,
    candidate_validation_failed: 422,
  } as const;

  assert.deepEqual(Object.keys(PACKAGE_ARTIFACT_HTTP_ERRORS).sort(), Object.keys(expected).sort());
  for (const [code, statusCode] of Object.entries(expected)) {
    const mapped = mapArtifactHttpError(new PackageArtifactStoreError(
      code as keyof typeof expected,
      PRIVATE_DETAIL,
    ));
    assert.equal(mapped?.source, 'package', code);
    assert.equal(mapped?.code, code, code);
    assert.equal(mapped?.statusCode, statusCode, code);
    assert.ok(mapped?.message, code);
    assert.doesNotMatch(mapped!.message, /synthetic backend|internal stack/iu, code);
  }
  assert.equal(mapArtifactHttpError(new Error(PRIVATE_DETAIL)), null);
});

test('MCP 工件错误适配保留协议语义并隐藏后端详情', () => {
  const invalidPackage = mapMcpArtifactError(
    new PackageArtifactStoreError('invalid_package', PRIVATE_DETAIL),
    { phase: 'load', resource: { type: 'rule', id: 'rule:synthetic', key: 'synthetic' } },
  );
  assert.equal(invalidPackage.code, 'SUBMISSION_BLOCKED');
  assert.equal(invalidPackage.statusCode, 422);
  assert.equal(invalidPackage.details.reason_code, 'PACKAGE_INTEGRITY_FAILED');
  assert.equal(invalidPackage.details.retryable, false);
  assert.doesNotMatch(invalidPackage.message, /synthetic backend|internal stack/iu);

  const invalidInput = mapMcpArtifactError(
    new PackageArtifactStoreError('invalid_input', PRIVATE_DETAIL),
    { phase: 'input' },
  );
  assert.equal(invalidInput.code, 'INVALID_INPUT');
  assert.equal(invalidInput.statusCode, 400);
  assert.doesNotMatch(invalidInput.message, /synthetic backend|internal stack/iu);

  const conflict = mapMcpArtifactError(
    new ArtifactRepositoryError('revision_conflict', PRIVATE_DETAIL, {
      expectedRevision: 'expected-revision',
      actualRevision: 'actual-revision',
    }),
    { phase: 'persist' },
  );
  assert.equal(conflict.statusCode, 409);
  assert.equal(conflict.details.expected_revision, 'expected-revision');
  assert.equal(conflict.details.actual_revision, 'actual-revision');
  assert.equal(conflict.details.retryable, true);

  const existing = new ModelingMcpToolError('KNOWN_ERROR', '已知错误', 409);
  const enriched = mapMcpArtifactError(existing, { phase: 'validate' });
  assert.equal(enriched.details.phase, 'validate');
  assert.equal(enriched.details.reason_code, 'KNOWN_ERROR');
});

test('浏览器评测认证错误映射为 401 和固定公开文案', () => {
  const mapped = mapEvaluationHttpError(new EvaluationAuthenticationError(PRIVATE_DETAIL));
  assert.deepEqual(mapped, {
    code: 'EVALUATION_AUTH_REQUIRED',
    statusCode: 401,
    message: '登录状态已失效，请重新登录后再运行评测',
  });
  assert.equal(mapEvaluationHttpError(new Error(PRIVATE_DETAIL)), null);
});
