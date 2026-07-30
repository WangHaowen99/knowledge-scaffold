import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import {
  ArtifactRepositoryError,
  IsomorphicGitArtifactRepository,
  type ArtifactSnapshot,
} from '../src/artifacts/index.js';

async function fixture(t: test.TestContext) {
  const rootDir = await mkdtemp(join(tmpdir(), 'artifact-repository-'));
  t.after(() => rm(rootDir, { recursive: true, force: true }));
  let tick = 0;
  const repository = new IsomorphicGitArtifactRepository({
    rootDir,
    author: { name: '建模系统', email: 'modeling@example.com' },
    now: () => new Date(Date.UTC(2026, 0, 1, 0, 0, tick++)),
  });
  return { rootDir, repository };
}

function text(snapshot: ArtifactSnapshot, path: string): string | undefined {
  const file = snapshot.files.find((item) => item.path === path);
  return file ? new TextDecoder().decode(file.content) : undefined;
}

async function expectRepositoryError(
  action: () => Promise<unknown>,
  code: ArtifactRepositoryError['code'],
): Promise<ArtifactRepositoryError> {
  try {
    await action();
    assert.fail(`预期错误 ${code}`);
  } catch (error) {
    assert.ok(error instanceof ArtifactRepositoryError);
    assert.equal(error.code, code);
    return error;
  }
}

test('初始化项目并保存独立版本', async (t) => {
  const { rootDir, repository } = await fixture(t);
  const first = await repository.initialize('pj_bank');
  const again = await repository.initialize('pj_bank');
  const other = await repository.initialize('pj_other');

  assert.equal(again.oid, first.oid);
  assert.equal(first.parentOid, null);
  assert.equal(first.message, '创建项目');
  assert.equal((await repository.snapshot('pj_bank')).files.length, 0);
  assert.notEqual(other.oid, first.oid);
  assert.equal(await readFile(join(rootDir, 'pj_bank', '.git', 'HEAD'), 'utf8'), 'ref: refs/heads/main\n');
});

test('提交、读取历史版本并比较文件', async (t) => {
  const { rootDir, repository } = await fixture(t);
  const initial = await repository.initialize('pj_bank');
  const first = await repository.commit('pj_bank', initial.oid, [
    { kind: 'write', path: 'model/package-spec.json', content: '{"version":1}\n' },
    { kind: 'write', path: 'package/index.md', content: '# 第一版\n' },
    { kind: 'write', path: 'package/logo.bin', content: new Uint8Array([0, 1, 255]) },
  ], { message: '生成第一版' });
  const second = await repository.commit('pj_bank', first.oid, [
    { kind: 'write', path: 'model/package-spec.json', content: '{"version":2}\n' },
    { kind: 'delete', path: 'package/index.md' },
    { kind: 'write', path: 'reports/check.md', content: '通过\n' },
  ], { message: '完成检查', author: { name: '张三', email: 'zhangsan@example.com' } });

  const oldSnapshot = await repository.snapshot('pj_bank', first.oid);
  const currentSnapshot = await repository.snapshot('pj_bank');
  assert.equal(text(oldSnapshot, 'model/package-spec.json'), '{"version":1}\n');
  assert.equal(text(currentSnapshot, 'model/package-spec.json'), '{"version":2}\n');
  assert.equal(text(currentSnapshot, 'package/index.md'), undefined);
  assert.equal(await readFile(join(rootDir, 'pj_bank', 'model', 'package-spec.json'), 'utf8'), '{"version":2}\n');
  assert.deepEqual(currentSnapshot.files.find((file) => file.path === 'package/logo.bin')?.content, new Uint8Array([0, 1, 255]));

  const history = await repository.history('pj_bank');
  assert.deepEqual(history.map((revision) => revision.oid), [second.oid, first.oid, initial.oid]);
  assert.equal(history[0].author.name, '张三');

  const diff = await repository.diff('pj_bank', first.oid, second.oid);
  assert.deepEqual(diff.files.map((file) => [file.path, file.kind]), [
    ['model/package-spec.json', 'modified'],
    ['package/index.md', 'deleted'],
    ['reports/check.md', 'added'],
  ]);
  assert.equal(new TextDecoder().decode(diff.files[0].before), '{"version":1}\n');
  assert.equal(new TextDecoder().decode(diff.files[0].after), '{"version":2}\n');
});

test('旧版本不能覆盖新内容', async (t) => {
  const { repository } = await fixture(t);
  const initial = await repository.initialize('pj_bank');
  const saved = await repository.commit('pj_bank', initial.oid, [
    { kind: 'write', path: 'package/index.md', content: '# 当前内容\n' },
  ], { message: '保存内容' });

  const error = await expectRepositoryError(() => repository.commit('pj_bank', initial.oid, [
    { kind: 'write', path: 'package/index.md', content: '# 旧页面内容\n' },
  ], { message: '覆盖内容' }), 'revision_conflict');

  assert.equal(error.message, '内容已更新，请刷新后重试');
  assert.equal(error.details?.currentRevision, saved.oid);
  assert.equal(text(await repository.snapshot('pj_bank'), 'package/index.md'), '# 当前内容\n');
});

test('同一版本的并发提交只接受一个', async (t) => {
  const { rootDir, repository } = await fixture(t);
  const secondRepository = new IsomorphicGitArtifactRepository({ rootDir });
  const initial = await repository.initialize('pj_bank');

  const results = await Promise.allSettled([
    secondRepository.commit('pj_bank', initial.oid, [
      { kind: 'write', path: 'package/a.md', content: 'A' },
    ], { message: '保存 A' }),
    repository.commit('pj_bank', initial.oid, [
      { kind: 'write', path: 'package/b.md', content: 'B' },
    ], { message: '保存 B' }),
  ]);

  assert.equal(results.filter((result) => result.status === 'fulfilled').length, 1);
  const rejected = results.find((result) => result.status === 'rejected');
  assert.ok(rejected?.status === 'rejected');
  assert.ok(rejected.reason instanceof ArtifactRepositoryError);
  assert.equal(rejected.reason.code, 'revision_conflict');
  assert.equal((await repository.history('pj_bank')).length, 2);
});

test('恢复旧内容时创建新版本', async (t) => {
  const { rootDir, repository } = await fixture(t);
  const initial = await repository.initialize('pj_bank');
  const first = await repository.commit('pj_bank', initial.oid, [
    { kind: 'write', path: 'package/index.md', content: '# 第一版\n' },
  ], { message: '第一版' });
  const second = await repository.commit('pj_bank', first.oid, [
    { kind: 'write', path: 'package/index.md', content: '# 第二版\n' },
    { kind: 'write', path: 'package/new.md', content: '新增\n' },
  ], { message: '第二版' });

  const restored = await repository.restore('pj_bank', first.oid, second.oid);

  assert.notEqual(restored.oid, first.oid);
  assert.equal(restored.parentOid, second.oid);
  assert.equal(text(await repository.snapshot('pj_bank'), 'package/index.md'), '# 第一版\n');
  assert.equal(text(await repository.snapshot('pj_bank'), 'package/new.md'), undefined);
  assert.equal(await readFile(join(rootDir, 'pj_bank', 'package', 'index.md'), 'utf8'), '# 第一版\n');
  await assert.rejects(readFile(join(rootDir, 'pj_bank', 'package', 'new.md')));
  assert.equal(text(await repository.snapshot('pj_bank', second.oid), 'package/index.md'), '# 第二版\n');
  assert.deepEqual((await repository.history('pj_bank')).map((item) => item.oid), [
    restored.oid,
    second.oid,
    first.oid,
    initial.oid,
  ]);
});

test('拒绝越界项目和文件路径', async (t) => {
  const { rootDir, repository } = await fixture(t);

  await expectRepositoryError(() => repository.initialize('../outside'), 'invalid_project');
  const initial = await repository.initialize('pj_bank');
  await expectRepositoryError(() => repository.commit('pj_bank', initial.oid, [
    { kind: 'write', path: '../outside.txt', content: 'bad' },
  ], { message: '错误路径' }), 'invalid_path');
  await expectRepositoryError(() => repository.commit('pj_bank', initial.oid, [
    { kind: 'write', path: '.git/config', content: 'bad' },
  ], { message: '错误路径' }), 'invalid_path');
  await expectRepositoryError(() => repository.commit('pj_bank', initial.oid, [
    { kind: 'write', path: 'a', content: 'file' },
    { kind: 'write', path: 'a/b', content: 'nested' },
  ], { message: '冲突路径' }), 'invalid_path');

  await assert.rejects(readFile(join(rootDir, '..', 'outside.txt')));
  assert.equal((await repository.current('pj_bank')).oid, initial.oid);
});

test('拒绝把项目目录替换为符号链接', async (t) => {
  const { rootDir, repository } = await fixture(t);
  const outside = await mkdtemp(join(tmpdir(), 'artifact-repository-outside-'));
  t.after(() => rm(outside, { recursive: true, force: true }));
  await symlink(outside, join(rootDir, 'pj_link'));

  await expectRepositoryError(() => repository.initialize('pj_link'), 'invalid_repository');
  await expectRepositoryError(() => repository.current('pj_link'), 'invalid_repository');
});

test('空改动和不存在的版本返回明确错误', async (t) => {
  const { repository } = await fixture(t);
  const initial = await repository.initialize('pj_bank');

  await expectRepositoryError(
    () => repository.commit('pj_bank', initial.oid, [], { message: '空改动' }),
    'empty_change',
  );
  await expectRepositoryError(() => repository.commit('pj_bank', initial.oid, [
    { kind: 'write', path: 'same.md', content: '内容' },
  ], { message: '首次保存' }).then((saved) => repository.commit('pj_bank', saved.oid, [
    { kind: 'write', path: 'same.md', content: '内容' },
  ], { message: '重复保存' })), 'no_change');
  await expectRepositoryError(
    () => repository.snapshot('pj_bank', '0'.repeat(40)),
    'revision_not_found',
  );
});
