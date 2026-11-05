// 任务子进程：node src/runner.mjs <任务目录>。读 job.json 跑流水线，进度一行一个 JSON 写到 stdout，
// 结局写 out/outcome.json。服务端按进程组杀它，REA 子进程跟着一起没。
import { writeFile, readFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

import { runPipeline } from './pipeline.mjs';

const dir = process.argv[2];
const job = JSON.parse(await readFile(join(dir, 'job.json'), 'utf8'));
const emit = (progress) => process.stdout.write(`${JSON.stringify({ progress })}\n`);
let outcome;
try {
  outcome = await runPipeline({ ...job, dir }, emit);
} catch (error) {
  process.stderr.write(`${error?.stack ?? error}\n`);
  outcome = { state: 'failed', error_code: 'runner_error', error_message: '旁路容器处理任务出错' };
}
await mkdir(join(dir, 'out'), { recursive: true });
await writeFile(join(dir, 'out', 'outcome.json'), JSON.stringify(outcome));
