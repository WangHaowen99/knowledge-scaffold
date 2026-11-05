// 测试用的 REA 替身：行为由目标程序内容里的标记决定（子进程拿不到测试进程的环境变量）。
//   fake:sleep:<pid 文件>  起一个孙进程，把两个 pid 写进文件，然后一直不退
//   fake:big               往 stdout 一直写
//   fake:many:<n>          输出 n 个模块
//   其他                   输出 1 个模块，并带上自己看到的环境变量
import { spawn } from 'node:child_process';
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const [command, target] = process.argv.slice(2);
appendFileSync(join(process.env.HOME, 'calls.log'), `${command}\n`);
const content = readFileSync(target, 'latin1');
const progress = (message) => process.stderr.write(`${JSON.stringify({ rea_progress: { phase: 'x', completed: 0, total: 1, message } })}\n`);
const evidence = (paths, extra = {}) => ({
  subject: { name: 'fake' },
  provider: { id: 'rea-javascript-application', name: 'fake', version: '1' },
  limitations: ['fake limitation'],
  normalized_result: {
    graph: {
      nodes: paths.map((path, index) => ({
        node_id: `n${index}`,
        kind: 'javascript-module',
        observations: [{ label: path, properties: { semantic_role: 'source-module', file_occurrence: { path } }, evidence: { state: 'observed' } }],
      })),
      edges: [],
    },
    semantic_graph: { nodes: [], relations: [], unknowns: [{}] },
  },
  ...extra,
});

progress('Parsing and projecting a.js (2/5)');
const sleep = /fake:sleep:(\S+)/.exec(content);
if (sleep) {
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
  writeFileSync(sleep[1], JSON.stringify([process.pid, child.pid]));
  setInterval(() => {}, 1000);
} else if (content.includes('fake:big')) {
  const chunk = 'x'.repeat(64 * 1024);
  const write = () => process.stdout.write(chunk, write);
  write();
} else {
  const many = /fake:many:(\d+)/.exec(content);
  const paths = many ? Array.from({ length: Number(many[1]) }, (_, index) => `m${String(index).padStart(3, '0')}.js`) : ['main.js'];
  process.stdout.write(`${JSON.stringify(evidence(paths, { fake_env: process.env }))}\n`);
}
