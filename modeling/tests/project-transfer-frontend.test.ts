import assert from 'node:assert/strict';
import test from 'node:test';

import {
  responseDownloadFilename,
  waitForProjectTransferJob,
} from '../frontend/src/projectTransfer.js';

test('项目迁移任务持续报告进度并返回完成结果', async () => {
  const states = [
    { status: 'queued', progress: { percent: 0, message: '等待导入' } },
    { status: 'running', progress: { percent: 45, message: '正在恢复项目数据' } },
    { status: 'completed', progress: { percent: 100, message: '项目导入完成' }, project: { projectId: 'pj_target' } },
  ];
  const progress: number[] = [];
  const job = await waitForProjectTransferJob(
    async () => ({ job: states.shift() }),
    'ptj_demo',
    {
      onProgress: (current) => progress.push(current.percent),
      wait: async () => undefined,
    },
  );
  assert.deepEqual(progress, [0, 45, 100]);
  assert.equal(job.project.projectId, 'pj_target');
});

test('项目迁移任务向页面返回后端失败原因', async () => {
  await assert.rejects(
    waitForProjectTransferJob(
      async () => ({ job: { status: 'failed', progress: { percent: 30 }, error: '迁移包文件校验失败' } }),
      'ptj_demo',
      { wait: async () => undefined },
    ),
    /迁移包文件校验失败/,
  );
});

test('项目迁移下载优先采用 UTF-8 文件名', () => {
  const response = {
    headers: new Headers({
      'content-disposition': "attachment; filename=project-transfer.zip; filename*=UTF-8''project_alpha.project-transfer.zip",
    }),
  };
  assert.equal(responseDownloadFilename(response, 'fallback.zip'), 'project_alpha.project-transfer.zip');
});
