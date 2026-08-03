import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'check-finding-handoff-'));

const { AiConstructionMessageStore } = await import('../src/ai-construction/messages.js');
const { AiConstructionTaskGraph } = await import('../src/ai-construction/task-graph.js');
const { getDb } = await import('../src/db/index.js');
const { createProject } = await import('../src/projects/repo.js');

const packageCheckSource = readFileSync(
  new URL('../frontend/src/PackageCheck.jsx', import.meta.url), 'utf8',
);

const appendFileAction = (projectId: string, path: unknown) => {
  const taskId = new AiConstructionTaskGraph(getDb()).snapshot(projectId)!.tasks[0].id;
  return new AiConstructionMessageStore(getDb()).append(projectId, {
    role: 'assistant',
    taskId,
    blocks: [{
      type: 'next_actions',
      actions: [{ id: 'open_package_file', label: '打开文件', style: 'primary', path }],
    }],
  } as any);
};

// ---- 体检面板只列发现，处理动作交给建模助手 ----

test('体检面板不再自带手动检查按钮', () => {
  assert.ok(!packageCheckSource.includes('检查内容'));
  assert.ok(!packageCheckSource.includes('runPackageCheck'));
  // 自动重查仍由工作区驱动，面板只展示状态
  assert.ok(packageCheckSource.includes('rechecking'));
});

test('修改与忽略都交给助手，面板里不再就地填理由', () => {
  assert.ok(packageCheckSource.includes('reviewPackageCheckFinding'));
  assert.ok(packageCheckSource.includes('ignorePackageCheckFinding'));
  // 两个填理由的表单已经移走：面板里不应再有输入框，也不应直接调写入接口
  assert.ok(!packageCheckSource.includes('<textarea'));
  assert.ok(!packageCheckSource.includes('acceptPackageWarnings'));
  assert.ok(!packageCheckSource.includes('deferPackageValidationErrors'));
});

test('提示类发现不给忽略按钮——后端没有对应的忽略通道', () => {
  assert.ok(packageCheckSource.includes("f.severity === '错误' || f.severity === '警告'"));
});

test('自动修复的可用性仍由后端声明，不因为去掉手动检查而失效', () => {
  // 原先 repairAvailability 只在手动检查的响应里赋值，按钮去掉后必须改由上层快照下发
  assert.ok(packageCheckSource.includes('repairAvailability.reason'));
  const workspaceSource = readFileSync(
    new URL('../frontend/src/PackageWorkspace.jsx', import.meta.url), 'utf8',
  );
  assert.ok(workspaceSource.includes('setRepairAvailability(result.repair ?? null)'));
  assert.ok(workspaceSource.includes('repair: repairAvailability'));
});

test('忽略待办要能在助手栏渲染，否则又是「后端说有待办、界面空白」的死路', async () => {
  const { isConstructionInteraction } = await import('../frontend/src/constructionInteraction.js');
  assert.equal(
    isConstructionInteraction({ type: 'question_batch', payload: { purpose: 'check_finding_ignore' } }),
    true,
  );
});

// ---- 动作可以指向具体文件 ----

test('动作保留 path，卡片才能打开这条发现对应的文件', () => {
  const project = createProject({ owner: 'alice', name: '动作路径' });
  const message = appendFileAction(project.id, 'objects/index.md');
  assert.equal((message.blocks[0] as any).actions[0].path, 'objects/index.md');
  // 重新读一遍，确认路径是真的落库了而不只是返回值里带着
  const reloaded = new AiConstructionMessageStore(getDb()).list(project.id);
  assert.equal((reloaded[0].blocks[0] as any).actions[0].path, 'objects/index.md');
});

test('拒绝越出知识包的动作路径', () => {
  const project = createProject({ owner: 'alice', name: '动作路径越界' });
  for (const path of ['../secrets.md', '/etc/passwd', 'a/../../b.md']) {
    assert.throws(
      () => appendFileAction(project.id, path),
      /动作目标路径不正确/,
      `应拒绝 ${path}`,
    );
  }
});
