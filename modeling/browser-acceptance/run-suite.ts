import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { runBrowserJourney, validateJourneyPlan } from '../scripts/browser-journey.js';
import { browserAcceptanceCases } from './cases.js';

interface SuiteArgs {
  baseUrl: string;
  outputDir: string;
  executable?: string;
  filter?: RegExp;
  strictRepeats: boolean;
}

function parseArgs(argv: string[]): SuiteArgs {
  const values: Record<string, string> = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith('--') || value === undefined) throw new Error(`参数格式错误: ${key ?? ''}`);
    values[key.slice(2)] = value;
  }
  if (!values['base-url']) throw new Error('必须提供 --base-url');
  return {
    baseUrl: values['base-url'],
    outputDir: path.resolve(values.output ?? path.join('tmp', `browser-acceptance-${Date.now()}`)),
    ...(values.executable ? { executable: values.executable } : {}),
    ...(values.filter ? { filter: new RegExp(values.filter, 'u') } : {}),
    strictRepeats: values['strict-repeats'] === 'true',
  };
}

function assertCredentials(): void {
  if (!process.env.E2E_USERNAME || !process.env.E2E_PASSWORD) {
    throw new Error('必须通过 E2E_USERNAME 和 E2E_PASSWORD 提供浏览器登录输入');
  }
}

export async function runBrowserAcceptanceSuite(args: SuiteArgs) {
  assertCredentials();
  const selected = browserAcceptanceCases.filter((item) => !args.filter || args.filter.test(item.id));
  if (!selected.length) throw new Error('没有匹配的浏览器验收用例');
  await mkdir(args.outputDir, { recursive: true });
  const startedAt = new Date();
  const results: Array<Record<string, unknown>> = [];

  for (const acceptanceCase of selected) {
    validateJourneyPlan(acceptanceCase.plan);
    const repeats = args.strictRepeats ? acceptanceCase.strictRepeats ?? 1 : 1;
    const attempts: Array<Record<string, unknown>> = [];
    let passed = true;
    for (let attempt = 1; attempt <= repeats; attempt += 1) {
      const caseOutput = path.join(args.outputDir, acceptanceCase.id, `attempt-${attempt}`);
      const attemptStarted = Date.now();
      process.stdout.write(`\n[case] ${acceptanceCase.id} · ${acceptanceCase.title} · ${attempt}/${repeats}\n`);
      try {
        const result = await runBrowserJourney({
          baseUrl: args.baseUrl,
          plan: acceptanceCase.plan,
          outputDir: caseOutput,
          executablePath: args.executable,
        });
        const pageErrors = result.events.filter((event) => event.type === 'pageerror');
        if (pageErrors.length) throw new Error(`页面发生 ${pageErrors.length} 个未处理异常`);
        attempts.push({
          attempt,
          status: 'PASSED',
          durationMs: Date.now() - attemptStarted,
          browserWarnings: result.events,
          evidenceDir: path.relative(args.outputDir, caseOutput),
        });
      } catch (error) {
        passed = false;
        attempts.push({
          attempt,
          status: 'FAILED',
          durationMs: Date.now() - attemptStarted,
          error: error instanceof Error ? error.message : String(error),
          evidenceDir: path.relative(args.outputDir, caseOutput),
        });
        break;
      }
    }
    results.push({
      id: acceptanceCase.id,
      title: acceptanceCase.title,
      status: passed ? 'PASSED' : 'FAILED',
      requiredAttempts: repeats,
      attempts,
    });
  }

  const passed = results.filter((item) => item.status === 'PASSED').length;
  const report = {
    suite: 'agent-controlled-change-set-browser-acceptance-v1',
    startedAt: startedAt.toISOString(),
    finishedAt: new Date().toISOString(),
    strictRepeats: args.strictRepeats,
    selected: selected.length,
    passed,
    failed: selected.length - passed,
    status: passed === selected.length ? 'PASSED' : 'FAILED',
    results,
  };
  await writeFile(path.join(args.outputDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`);
  return report;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  runBrowserAcceptanceSuite(parseArgs(process.argv.slice(2)))
    .then((report) => {
      process.stdout.write(`\n${JSON.stringify({
        status: report.status,
        selected: report.selected,
        passed: report.passed,
        failed: report.failed,
      }, null, 2)}\n`);
      if (report.status !== 'PASSED') process.exitCode = 1;
    })
    .catch((error: unknown) => {
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
      process.exitCode = 1;
    });
}
