import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import puppeteer, { type Browser, type ElementHandle, type Page } from 'puppeteer-core';

const SUPPORTED_ACTIONS = new Set([
  'goto', 'click', 'fill', 'select', 'upload', 'press', 'waitFor', 'assertText',
  'screenshot', 'sleep', 'scroll',
]);

type Environment = Record<string, string | undefined>;

export interface JourneyStep {
  action: string;
  name?: string;
  path?: string;
  selector?: string;
  text?: string;
  exact?: boolean;
  value?: string;
  valueEnv?: string;
  file?: string;
  fileEnv?: string;
  key?: string;
  timeoutMs?: number;
  durationMs?: number;
  direction?: 'up' | 'down';
  pixels?: number;
  content?: unknown;
}

export interface JourneyPlan {
  name: string;
  steps: JourneyStep[];
  viewport?: { width: number; height: number };
}

export function resolveJourneyValue(
  step: Pick<JourneyStep, 'value' | 'valueEnv'>,
  environment: Environment = process.env,
) {
  if (step.valueEnv) {
    const value = environment[step.valueEnv];
    if (value === undefined) throw new Error(`缺少环境变量 ${step.valueEnv}`);
    return value;
  }
  return step.value ?? '';
}

function resolveJourneyFile(step: Pick<JourneyStep, 'file' | 'fileEnv'>, environment: Environment) {
  if (step.fileEnv) {
    const value = environment[step.fileEnv];
    if (!value) throw new Error(`缺少环境变量 ${step.fileEnv}`);
    return value;
  }
  if (!step.file) throw new Error('上传步骤必须提供 file 或 fileEnv');
  return step.file;
}

export function describeJourneyStep(step: JourneyStep) {
  const target = step.name ?? step.text ?? step.selector ?? step.path ?? step.key ?? '';
  const source = step.valueEnv ? ` valueEnv=${step.valueEnv}` : step.fileEnv ? ` fileEnv=${step.fileEnv}` : '';
  return `${step.action}${target ? ` ${target}` : ''}${source}`;
}

export function validateJourneyPlan(value: unknown): asserts value is JourneyPlan {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('旅程计划必须是对象');
  const plan = value as Record<string, unknown>;
  if (typeof plan.name !== 'string' || !plan.name.trim()) throw new Error('旅程计划缺少 name');
  if (!Array.isArray(plan.steps) || !plan.steps.length) throw new Error('旅程计划缺少 steps');
  for (const [index, raw] of plan.steps.entries()) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error(`第 ${index + 1} 步格式无效`);
    const step = raw as Record<string, unknown>;
    if (typeof step.action !== 'string' || !SUPPORTED_ACTIONS.has(step.action)) {
      throw new Error(`第 ${index + 1} 步是不支持的前端动作: ${String(step.action ?? '')}`);
    }
    if (step.action === 'goto' && typeof step.path !== 'string') throw new Error(`第 ${index + 1} 步缺少 path`);
    if (['click', 'fill', 'select', 'upload', 'waitFor'].includes(step.action)
      && typeof step.selector !== 'string' && typeof step.text !== 'string') {
      throw new Error(`第 ${index + 1} 步缺少 selector 或 text`);
    }
    if (step.action === 'upload'
      && typeof step.file !== 'string' && typeof step.fileEnv !== 'string') {
      throw new Error('上传步骤必须提供 file 或 fileEnv');
    }
  }
}

async function exists(file: string) {
  try { await access(file); return true; } catch { return false; }
}

async function findExecutableIn(root: string): Promise<string | undefined> {
  if (!await exists(root)) return undefined;
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const candidate = path.join(root, entry.name);
    if (entry.isFile() && ['chrome', 'chromium', 'chrome-headless-shell'].includes(entry.name)) return candidate;
    if (entry.isDirectory()) {
      const nested = await findExecutableIn(candidate);
      if (nested) return nested;
    }
  }
  return undefined;
}

async function resolveBrowserExecutable(explicit?: string) {
  const candidates = [
    explicit,
    process.env.MODELING_BROWSER_EXECUTABLE,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ].filter((item): item is string => Boolean(item));
  for (const candidate of candidates) if (await exists(candidate)) return candidate;
  const cached = await findExecutableIn(path.join(process.env.HOME ?? '', '.cache', 'puppeteer'));
  if (cached) return cached;
  throw new Error('未找到 Chrome/Chromium，请设置 MODELING_BROWSER_EXECUTABLE');
}

async function textElement(page: Page, text: string, exact = false, timeout = 10_000) {
  const handle = await page.waitForFunction((needle, strict) => {
    const candidates = [...document.querySelectorAll('button, a, [role="button"], label, summary, option, [data-testid]')];
    return candidates.find((item) => {
      const content = (item.textContent ?? '').replace(/\s+/g, ' ').trim();
      return strict ? content === needle : content.includes(needle);
    }) ?? null;
  }, { timeout }, text, exact);
  const element = handle.asElement() as ElementHandle<Element> | null;
  if (!element) {
    await handle.dispose();
    throw new Error(`找不到文本控件（签名 ${text.length}:${text.charCodeAt(0) || 0}）`);
  }
  return element;
}

async function targetElement(page: Page, step: JourneyStep) {
  const timeout = step.timeoutMs ?? 10_000;
  if (step.selector) {
    const element = await page.waitForSelector(step.selector, { timeout, visible: true });
    if (!element) throw new Error(`找不到控件 ${step.selector}`);
    return element;
  }
  if (step.text) return textElement(page, step.text, step.exact, timeout);
  throw new Error('步骤缺少 selector 或 text');
}

async function runStep(page: Page, step: JourneyStep, baseUrl: string, outputDir: string, environment: Environment) {
  const timeout = step.timeoutMs ?? 15_000;
  if (step.action === 'goto') {
    const url = new URL(step.path ?? '/', baseUrl).toString();
    await page.goto(url, { waitUntil: 'networkidle2', timeout });
    return;
  }
  if (step.action === 'click') {
    const element = await targetElement(page, step);
    await (element as ElementHandle<HTMLElement>).click();
    await element.dispose();
    return;
  }
  if (step.action === 'fill') {
    const element = await targetElement(page, step) as ElementHandle<HTMLInputElement | HTMLTextAreaElement>;
    await element.click({ clickCount: 3 });
    await element.press('Backspace');
    await element.type(resolveJourneyValue(step, environment));
    await element.dispose();
    return;
  }
  if (step.action === 'select') {
    if (!step.selector) throw new Error('select 步骤必须使用 selector');
    await page.waitForSelector(step.selector, { timeout, visible: true });
    await page.select(step.selector, resolveJourneyValue(step, environment));
    return;
  }
  if (step.action === 'upload') {
    const element = await targetElement(page, step) as ElementHandle<HTMLInputElement>;
    const file = path.resolve(resolveJourneyFile(step, environment));
    await element.uploadFile(file);
    await element.dispose();
    return;
  }
  if (step.action === 'press') {
    await page.keyboard.press(step.key as never);
    return;
  }
  if (step.action === 'waitFor') {
    const element = await targetElement(page, step);
    await element.dispose();
    return;
  }
  if (step.action === 'assertText') {
    if (!step.text) throw new Error('assertText 步骤缺少 text');
    const found = await page.waitForFunction((needle) => document.body.innerText.includes(needle), { timeout }, step.text);
    await found.dispose();
    return;
  }
  if (step.action === 'screenshot') {
    const name = (step.name ?? `step-${Date.now()}`).replace(/[^a-zA-Z0-9_-]+/g, '-');
    await page.screenshot({ path: path.join(outputDir, `${name}.png`), fullPage: true });
    return;
  }
  if (step.action === 'sleep') {
    await new Promise((resolve) => setTimeout(resolve, step.durationMs ?? 1_000));
    return;
  }
  if (step.action === 'scroll') {
    const pixels = Math.max(1, step.pixels ?? 600) * (step.direction === 'up' ? -1 : 1);
    await page.mouse.wheel({ deltaY: pixels });
  }
}

export async function runBrowserJourney(input: {
  baseUrl: string;
  plan: JourneyPlan;
  outputDir: string;
  executablePath?: string;
  environment?: Environment;
}) {
  validateJourneyPlan(input.plan);
  await mkdir(input.outputDir, { recursive: true });
  const executablePath = await resolveBrowserExecutable(input.executablePath);
  let browser: Browser | undefined;
  const events: Array<{ type: string; message: string }> = [];
  try {
    browser = await puppeteer.launch({
      executablePath,
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    });
    const page = await browser.newPage();
    await page.setViewport(input.plan.viewport ?? { width: 1600, height: 1000 });
    page.on('console', (message) => {
      if (message.type() === 'error' || message.type() === 'warning') events.push({ type: message.type(), message: message.text().slice(0, 500) });
    });
    page.on('pageerror', (error) => events.push({ type: 'pageerror', message: error.message.slice(0, 500) }));
    page.on('dialog', (dialog) => { void dialog.accept(); });

    for (const [index, step] of input.plan.steps.entries()) {
      process.stdout.write(`[${index + 1}/${input.plan.steps.length}] ${describeJourneyStep(step)}\n`);
      try {
        await runStep(page, step, input.baseUrl, input.outputDir, input.environment ?? process.env);
      } catch (error) {
        await page.screenshot({ path: path.join(input.outputDir, `failure-${index + 1}.png`), fullPage: true });
        await writeFile(path.join(input.outputDir, `failure-${index + 1}.html`), await page.content());
        throw error;
      }
    }
    await writeFile(path.join(input.outputDir, 'browser-events.json'), JSON.stringify(events, null, 2));
    return { steps: input.plan.steps.length, events };
  } finally {
    await browser?.close();
  }
}

function parseArgs(argv: string[]) {
  const values: Record<string, string> = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith('--') || value === undefined) throw new Error(`参数格式错误: ${key ?? ''}`);
    values[key.slice(2)] = value;
  }
  if (!values['base-url'] || !values.plan) throw new Error('必须提供 --base-url 和 --plan');
  return values;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  (async () => {
    const args = parseArgs(process.argv.slice(2));
    const plan = JSON.parse(await readFile(path.resolve(args.plan), 'utf8')) as unknown;
    validateJourneyPlan(plan);
    const result = await runBrowserJourney({
      baseUrl: args['base-url'],
      plan,
      outputDir: path.resolve(args.output ?? path.join('tmp', `browser-journey-${Date.now()}`)),
      executablePath: args.executable,
    });
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  })().catch((error: unknown) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
