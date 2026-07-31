import { access, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import puppeteer, { type Browser, type ElementHandle, type Page } from 'puppeteer-core';

const SUPPORTED_ACTIONS = new Set([
  'goto', 'click', 'fill', 'select', 'upload', 'press', 'waitFor', 'assertText',
  'screenshot', 'sleep', 'scroll', 'guidedConstruction',
]);

const GUIDED_ACTION_PRIORITY = [
  'analyze_materials',
  'confirm_materials',
  'confirm_goal',
  'confirm_process',
  'generate_knowledge_structure',
  'repair_knowledge_structure',
  'accept_candidate_revision',
  'confirm_knowledge',
  'confirm_deterministic',
  'confirm_construction',
] as const;

export interface GuidedConstructionSnapshot {
  pendingInteraction: null | { type?: string; canSubmit: boolean };
  warningAcceptance?: null | { canSubmit: boolean };
  nextActionIds: string[];
  completed?: boolean;
  processing?: boolean;
}

export type GuidedConstructionAction =
  | { kind: 'answer_interaction' }
  | { kind: 'accept_warnings' }
  | { kind: 'click_action'; actionId: string }
  | { kind: 'complete' }
  | { kind: 'wait' };

export function guidedInteractionCanAdvance(input: {
  hasEnabledSubmit: boolean;
  hasSelectableChoice: boolean;
}) {
  return input.hasEnabledSubmit || input.hasSelectableChoice;
}

export function guidedConstructionIsProcessing(input: {
  hasSpinner: boolean;
  hasRunningTask: boolean;
  hasPendingInteraction: boolean;
}) {
  return input.hasSpinner || (input.hasRunningTask && !input.hasPendingInteraction);
}

export function isGuidedInteractionResponse(url: string, method: string) {
  if (method.toUpperCase() !== 'POST') return false;
  try {
    return /\/api\/projects\/[^/]+\/interactions\/[^/]+\/respond$/.test(new URL(url).pathname);
  } catch {
    return false;
  }
}

export function shouldRecoverGuidedRepairConflict(status: number, code?: string) {
  return status === 409 && (code === 'REVISION_CONFLICT' || code === 'busy');
}

export function chooseGuidedConstructionAction(
  snapshot: GuidedConstructionSnapshot,
): GuidedConstructionAction {
  if (snapshot.processing) return { kind: 'wait' };
  if (snapshot.pendingInteraction) {
    if (snapshot.pendingInteraction.canSubmit) return { kind: 'answer_interaction' };
    if (snapshot.nextActionIds.includes('repair_knowledge_structure')) {
      return { kind: 'click_action', actionId: 'repair_knowledge_structure' };
    }
    return { kind: 'wait' };
  }
  if (snapshot.warningAcceptance) {
    return { kind: 'accept_warnings' };
  }
  const available = new Set(snapshot.nextActionIds);
  const actionId = GUIDED_ACTION_PRIORITY.find((candidate) => available.has(candidate));
  if (actionId) return { kind: 'click_action', actionId };
  if (snapshot.completed) return { kind: 'complete' };
  return { kind: 'wait' };
}

export function nextGuidedIdleIterations(
  current: number,
  state: { progressed: boolean; processing: boolean },
) {
  return state.progressed || state.processing ? 0 : current + 1;
}

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
  files?: string[];
  filesEnv?: string;
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

export function browserProtocolTimeout(plan: JourneyPlan) {
  const longestStep = Math.max(0, ...plan.steps.map((step) => (
    step.timeoutMs ?? step.durationMs ?? 0
  )));
  return Math.max(180_000, longestStep + 60_000);
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

export function resolveJourneyFiles(
  step: Pick<JourneyStep, 'file' | 'fileEnv' | 'files' | 'filesEnv'>,
  environment: Environment = process.env,
) {
  if (step.filesEnv) {
    const raw = environment[step.filesEnv];
    if (!raw) throw new Error(`缺少环境变量 ${step.filesEnv}`);
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { throw new Error(`${step.filesEnv} 必须是 JSON 文件路径数组`); }
    if (!Array.isArray(parsed) || !parsed.length || parsed.some((item) => typeof item !== 'string' || !item.trim())) {
      throw new Error(`${step.filesEnv} 必须是非空 JSON 文件路径数组`);
    }
    return parsed.map((item) => String(item));
  }
  if (Array.isArray(step.files)) {
    if (!step.files.length || step.files.some((item) => typeof item !== 'string' || !item.trim())) {
      throw new Error('上传步骤的 files 必须是非空文件路径数组');
    }
    return [...step.files];
  }
  if (step.fileEnv) {
    const value = environment[step.fileEnv];
    if (!value) throw new Error(`缺少环境变量 ${step.fileEnv}`);
    return [value];
  }
  if (!step.file) throw new Error('上传步骤必须提供 file、fileEnv、files 或 filesEnv');
  return [step.file];
}

export function describeJourneyStep(step: JourneyStep) {
  const target = step.name ?? step.text ?? step.selector ?? step.path ?? step.key ?? '';
  const source = step.valueEnv ? ` valueEnv=${step.valueEnv}`
    : step.filesEnv ? ` filesEnv=${step.filesEnv}`
      : step.fileEnv ? ` fileEnv=${step.fileEnv}` : '';
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
      && typeof step.file !== 'string' && typeof step.fileEnv !== 'string'
      && !Array.isArray(step.files) && typeof step.filesEnv !== 'string') {
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

async function installBrowserEvaluationHelpers(page: Page) {
  const helperSource = 'globalThis.__name = globalThis.__name || ((value) => value);';
  await page.evaluateOnNewDocument(helperSource);
  await page.evaluate(helperSource);
}

export function matchesJourneyControlText(
  content: string,
  descendantContents: string[],
  needle: string,
  exact = false,
) {
  const normalize = (value: string) => value.replace(/\s+/g, ' ').trim();
  const normalizedContent = normalize(content);
  const normalizedNeedle = normalize(needle);
  if (!exact) return normalizedContent.includes(normalizedNeedle);
  return normalizedContent === normalizedNeedle
    || descendantContents.some((child) => normalize(child) === normalizedNeedle);
}

async function textElement(page: Page, text: string, exact = false, timeout = 10_000) {
  const handle = await page.waitForFunction((needle, strict) => {
    const candidates = [...document.querySelectorAll('button, a, [role="button"], label, summary, option, [data-testid]')];
    return candidates.find((item) => {
      const content = (item.textContent ?? '').replace(/\s+/g, ' ').trim();
      if (!strict) return content.includes(needle);
      return content === needle || [...item.querySelectorAll('*')].some((child) => (
        (child.textContent ?? '').replace(/\s+/g, ' ').trim() === needle
      ));
    }) ?? null;
  }, { timeout }, text, exact);
  const element = handle.asElement() as ElementHandle<Element> | null;
  if (!element) {
    await handle.dispose();
    throw new Error(`找不到文本控件（签名 ${text.length}:${text.charCodeAt(0) || 0}）`);
  }
  return element;
}

export async function waitForJourneyText(
  page: Pick<Page, 'waitForFunction'>,
  text: string,
  timeout = 10_000,
) {
  const handle = await page.waitForFunction(
    (needle) => document.body.innerText.includes(needle),
    { timeout },
    text,
  );
  await handle.dispose();
}

async function targetElement(page: Page, step: JourneyStep, options: { visible?: boolean } = {}) {
  const timeout = step.timeoutMs ?? 10_000;
  if (step.selector) {
    const element = await page.waitForSelector(step.selector, {
      timeout,
      ...(options.visible === false ? {} : { visible: true }),
    });
    if (!element) throw new Error(`找不到控件 ${step.selector}`);
    return element;
  }
  if (step.text) return textElement(page, step.text, step.exact, timeout);
  throw new Error('步骤缺少 selector 或 text');
}

async function guidedSnapshot(page: Page): Promise<GuidedConstructionSnapshot> {
  return page.evaluate(() => {
    const visible = (element: Element) => {
      const style = window.getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
    };
    const pending = [...document.querySelectorAll<HTMLElement>('section[aria-label="待确认事项"]')]
      .find(visible);
    const submit = pending
      ? [...pending.querySelectorAll<HTMLButtonElement>('button')]
        .find((button) => visible(button) && !button.disabled && /提交答案|确认修改|确认|通过|采用建议/.test(button.textContent ?? ''))
      : undefined;
    const selectableChoice = pending
      ? [...pending.querySelectorAll<HTMLInputElement>('input[type="radio"], input[type="checkbox"]')]
        .find((input) => visible(input) && !input.disabled)
      : undefined;
    const warningSection = [...document.querySelectorAll<HTMLElement>('.pcheck-warning-acceptance')]
      .find(visible);
    const warningSubmit = warningSection
      ? [...warningSection.querySelectorAll<HTMLButtonElement>('button')]
        .find((button) => visible(button) && /接受\s*\d+\s*条警告/.test(button.textContent ?? ''))
      : undefined;
    const actionSections = [...document.querySelectorAll<HTMLElement>('.ac-message-actions')].filter(visible);
    const currentActionSection = actionSections.at(-1);
    const currentButtons = currentActionSection
      ? [...currentActionSection.querySelectorAll<HTMLButtonElement>('button[data-action]')]
      : [];
    const candidateButtons = [...document.querySelectorAll<HTMLButtonElement>('button[data-action="accept_candidate_revision"]')];
    const nextActionIds = [...currentButtons, ...candidateButtons]
      .filter((button) => visible(button) && !button.disabled)
      .map((button) => button.dataset.action ?? '')
      .filter(Boolean);
    const bodyText = document.body.innerText;
    const hasSpinner = [...document.querySelectorAll<HTMLElement>('.pchat .spinner')].some(visible);
    const hasRunningTask = [...document.querySelectorAll<HTMLElement>('.ac-message-task-list article.running')]
      .some(visible);
    return {
      pendingInteraction: pending ? {
        type: pending.dataset.interactionType,
        canSubmit: Boolean(submit || selectableChoice),
      } : null,
      warningAcceptance: warningSection ? { canSubmit: Boolean(warningSubmit && !warningSubmit.disabled) } : null,
      nextActionIds,
      completed: bodyText.includes('AI 辅助构建已完成') && !nextActionIds.includes('confirm_construction'),
      processing: hasSpinner || (hasRunningTask && !pending),
    };
  });
}

async function acceptVisibleWarnings(page: Page) {
  const textarea = await page.$('.pcheck-warning-acceptance textarea');
  if (!textarea) return false;
  await textarea.click({ clickCount: 3 });
  await textarea.press('Backspace');
  await textarea.type('已查看影响范围；这些非阻断项不改变当前业务结论，进入验证后继续保留为已知限制。');
  const clicked = await page.evaluate(() => {
    const section = document.querySelector<HTMLElement>('.pcheck-warning-acceptance');
    const button = section
      ? [...section.querySelectorAll<HTMLButtonElement>('button')]
        .find((item) => !item.disabled && /接受\s*\d+\s*条警告/.test(item.textContent ?? ''))
      : undefined;
    button?.click();
    return Boolean(button);
  });
  await textarea.dispose();
  return clicked;
}

async function answerGuidedInteraction(page: Page) {
  const submitToken = `guided-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const prepared = await page.evaluate((token) => {
    const visible = (element: Element) => {
      const style = window.getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
    };
    const section = [...document.querySelectorAll<HTMLElement>('section[aria-label="待确认事项"]')]
      .find(visible);
    if (!section) return false;
    const groups = [...section.querySelectorAll<HTMLFieldSetElement>('fieldset')];
    let changed = false;
    for (const group of groups) {
      const inputs = [...group.querySelectorAll<HTMLInputElement>('input[type="radio"], input[type="checkbox"]')]
        .filter((input) => !input.disabled && !input.closest('label')?.textContent?.trim().startsWith('其他'));
      if (!inputs.length) {
        const question = group.querySelector('legend')?.textContent?.replace(/\s+/g, ' ').trim() ?? '未知问题';
        throw new Error(`当前待办仍要求自由输入：${question}`);
      }
      if (inputs[0]?.type === 'checkbox') {
        const preferred = inputs.filter((input) => {
          const label = input.closest('label')?.textContent?.replace(/\s+/g, ' ').trim() ?? '';
          return !['__none__', '__other__', '无'].includes(input.value) && !/暂不|没有额外/.test(label);
        });
        const targets = preferred.length ? preferred : [inputs[0]];
        for (const input of targets) if (input && !input.checked) {
          input.click();
          changed = true;
        }
      } else if (!inputs.some((input) => input.checked)) {
        inputs[0]?.click();
        changed = Boolean(inputs[0]);
      }
    }
    const standaloneInputs = [...section.querySelectorAll<HTMLInputElement>('input[type="radio"], input[type="checkbox"]')]
      .filter((input) => !input.disabled);
    if (!groups.length && standaloneInputs.length && !standaloneInputs.some((input) => input.checked)) {
      standaloneInputs[0]?.click();
      changed = Boolean(standaloneInputs[0]);
    }
    const submit = [...section.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => visible(button) && !button.disabled && /提交答案|确认修改|确认|通过|采用建议/.test(button.textContent ?? ''));
    if (submit) submit.dataset.guidedSubmit = token;
    return { hasSubmit: Boolean(submit), changed };
  }, submitToken);
  if (!prepared.hasSubmit) return prepared.changed;

  const requestPromise = page.waitForRequest((request) => (
    isGuidedInteractionResponse(request.url(), request.method())
  ), { timeout: 3_000 });
  const responsePromise = page.waitForResponse((response) => (
    isGuidedInteractionResponse(response.url(), response.request().method())
  ), { timeout: 15 * 60_000 });
  await page.click(`[data-guided-submit="${submitToken}"]`);
  try {
    await requestPromise;
  } catch {
    void responsePromise.catch(() => undefined);
    throw new Error('提交按钮没有发起交互请求，请检查选择状态或页面反馈');
  }
  const response = await responsePromise;
  if (!response.ok()) {
    const payload = await response.json().catch(() => ({})) as { error?: string; code?: string };
    if (shouldRecoverGuidedRepairConflict(response.status(), payload.code)) {
      await page.reload({ waitUntil: 'networkidle2' });
      return true;
    }
    throw new Error(payload.error || payload.code || `提交待确认事项失败（${response.status()}）`);
  }
  return true;
}

async function runGuidedConstruction(page: Page, timeoutMs: number) {
  const startedAt = Date.now();
  let idleIterations = 0;
  let lastStateSignature = '';
  while (Date.now() - startedAt < timeoutMs) {
    const snapshot = await guidedSnapshot(page);
    const action = chooseGuidedConstructionAction(snapshot);
    const stateSignature = JSON.stringify({ snapshot, action });
    if (stateSignature !== lastStateSignature) {
      process.stdout.write(`[guided] ${stateSignature}\n`);
      lastStateSignature = stateSignature;
    }
    if (action.kind === 'complete') return;
    if (action.kind === 'answer_interaction') {
      const progressed = await answerGuidedInteraction(page);
      idleIterations = nextGuidedIdleIterations(idleIterations, { progressed, processing: Boolean(snapshot.processing) });
    } else if (action.kind === 'accept_warnings') {
      const progressed = await acceptVisibleWarnings(page);
      idleIterations = nextGuidedIdleIterations(idleIterations, { progressed, processing: Boolean(snapshot.processing) });
    } else if (action.kind === 'click_action') {
      const clicked = await page.evaluate((actionId) => {
        const button = [...document.querySelectorAll<HTMLButtonElement>('button[data-action]')]
          .find((item) => item.dataset.action === actionId && !item.disabled);
        button?.click();
        return Boolean(button);
      }, action.actionId);
      idleIterations = nextGuidedIdleIterations(idleIterations, { progressed: clicked, processing: Boolean(snapshot.processing) });
    } else {
      idleIterations = nextGuidedIdleIterations(idleIterations, { progressed: false, processing: Boolean(snapshot.processing) });
    }
    if (idleIterations > 150) {
      throw new Error('AI 辅助构建长时间没有可推进的页面动作');
    }
    await new Promise((resolve) => setTimeout(resolve, action.kind === 'wait' ? 1_000 : 800));
  }
  throw new Error(`AI 辅助构建未在 ${Math.round(timeoutMs / 1000)} 秒内完成`);
}

async function dismissVisibleTour(page: Page) {
  const dismissed = await page.evaluate(() => {
    const overlay = document.querySelector<HTMLElement>('.tour-ov');
    if (!overlay) return false;
    const style = window.getComputedStyle(overlay);
    const box = overlay.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' || box.width === 0 || box.height === 0) {
      return false;
    }
    const skip = overlay.querySelector<HTMLButtonElement>('.tour-skip');
    if (!skip || skip.disabled) return false;
    skip.click();
    return true;
  });
  if (dismissed) {
    await page.waitForFunction(() => !document.querySelector('.tour-ov'), { timeout: 3_000 });
  }
  return dismissed;
}

async function runStep(page: Page, step: JourneyStep, baseUrl: string, outputDir: string, environment: Environment) {
  const timeout = step.timeoutMs ?? 15_000;
  if (step.action === 'goto') {
    const url = new URL(step.path ?? '/', baseUrl).toString();
    await page.goto(url, { waitUntil: 'networkidle2', timeout });
    return;
  }
  if (['click', 'fill', 'select', 'upload', 'press', 'guidedConstruction'].includes(step.action)) {
    await dismissVisibleTour(page);
  }
  if (step.action === 'click') {
    const element = await targetElement(page, step);
    await (element as ElementHandle<HTMLElement>).evaluate((node) => {
      node.scrollIntoView({ block: 'center', inline: 'center' });
      node.click();
    });
    await element.dispose();
    return;
  }
  if (step.action === 'fill') {
    const element = await targetElement(page, step) as ElementHandle<HTMLInputElement | HTMLTextAreaElement>;
    await element.evaluate((field) => {
      const prototype = field instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
      setter?.call(field, '');
      field.dispatchEvent(new Event('input', { bubbles: true }));
      field.dispatchEvent(new Event('change', { bubbles: true }));
      field.focus();
    });
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
    // 浏览器原生文件控件通常由可视化 dropzone 代理并保持 hidden；上传只要求元素存在。
    const element = await targetElement(page, step, { visible: false }) as ElementHandle<HTMLInputElement>;
    const files = resolveJourneyFiles(step, environment).map((file) => path.resolve(file));
    await element.uploadFile(...files);
    await element.dispose();
    return;
  }
  if (step.action === 'press') {
    await page.keyboard.press(step.key as never);
    return;
  }
  if (step.action === 'waitFor') {
    if (step.text && !step.selector) {
      await waitForJourneyText(page, step.text, timeout);
      return;
    }
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
    return;
  }
  if (step.action === 'guidedConstruction') {
    await runGuidedConstruction(page, step.timeoutMs ?? 45 * 60_000);
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
      protocolTimeout: browserProtocolTimeout(input.plan),
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    });
    const page = await browser.newPage();
    await page.setViewport(input.plan.viewport ?? { width: 1600, height: 1000 });
    await installBrowserEvaluationHelpers(page);
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
