const assert = require('node:assert/strict');
const { test } = require('node:test');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const puppeteer = require('../../node_modules/puppeteer-core');

// Runs against actual React Flow and Mermaid in Chromium, including DOM measurement.
// CHROME_BIN can point at any local Chromium installation.
function chromiumPath() {
  if (process.env.CHROME_BIN) return process.env.CHROME_BIN;
  const cache = path.join(require('node:os').homedir(), '.cache/ms-playwright');
  for (const dir of fs.readdirSync(cache).filter((name) => name.startsWith('chromium-')).reverse()) {
    for (const suffix of ['chrome-linux64/chrome', 'chrome-linux/chrome']) {
      const candidate = path.join(cache, dir, suffix);
      if (fs.existsSync(candidate)) return candidate;
    }
  }
  throw new Error('Set CHROME_BIN to a Chromium executable');
}

test('MMD uses original modeling cards without losing source semantics', async (t) => {
  const root = path.resolve(__dirname, '..');
  const { createServer } = await import(pathToFileURL(path.join(root, 'node_modules/vite/dist/node/index.js')));
  const server = await createServer({ root, configFile: path.join(root, 'vite.config.js'),
    server: { host: '127.0.0.1', port: 0, strictPort: false }, logLevel: 'error' });
  await server.listen();
  let browser;
  try {
    browser = await puppeteer.launch({ executablePath: chromiumPath(), headless: true, args: ['--no-sandbox'] });
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.setViewport({ width: 1440, height: 1000 });
    const address = server.httpServer.address();
    await page.goto(`http://127.0.0.1:${address.port}/tests/mmd-preview.html`);
    await page.waitForSelector('.wb-legacy-flow[data-layout-ready="true"]');
    const parse = (source) => page.evaluate((value) => window.previewTest.parse(value), source);
    const source = (value) => page.evaluate((value) => window.previewTest.setSource(value), value);
    const waitGraph = (label) => page.waitForFunction((value) =>
      document.querySelector('.wb-legacy-flow[data-layout-ready="true"]')
      && [...document.querySelectorAll('.fc-node__label')].some((node) => node.textContent === value), {}, label);

    await t.test('parser keeps Chinese, comments, chains, direction, implicit nodes and edge marks', async () => {
      const result = await parse('%% 前置注释\nflowchart LR\nA([入口]) -->|同意| B["保留 50%% 和 --> 文本<br/>换行 #quot;引号#quot;"] --> C{通过？}\nC -.->|返回| B\nC ==> D\nD <--> E[(数据)]');
      assert.equal(result.kind, 'legacy');
      assert.equal(result.graph.direction, 'LR');
      assert.equal(result.graph.nodes.length, 5);
      assert.equal(result.graph.edges.length, 5);
      assert.match(result.graph.nodes.find((node) => node.id === 'B').label, /50%% 和 --> 文本\n换行 "引号"/);
      assert.equal(result.graph.nodes.find((node) => node.id === 'D').label, 'D');
      assert.equal(result.graph.edges[2].stroke, 'dotted');
      assert.equal(result.graph.edges[3].stroke, 'thick');
      assert.equal(result.graph.edges[4].arrow, 'double_arrow_point');
    });

    await t.test('special structures keep native rendering and parser recovers after errors', async () => {
      const special = [
        'flowchart TD\nsubgraph 组\nA[甲] --> B[乙]\nend',
        'flowchart TD\nA[[子流程]] --> B',
        'flowchart TD\nA[甲] --> B\nstyle A fill:#f00',
        'flowchart TD\nA[甲] --> B\nclassDef red fill:#f00\nclass A red',
        'flowchart TD\nA -->|一| B\nA -->|二| B',
        'flowchart TD\nA --> A',
        'flowchart TD\nA["`**粗体**`"] --> B',
        'sequenceDiagram\nAlice->>Bob: hello',
      ];
      for (const mmd of special) assert.equal((await parse(mmd)).kind, 'native', mmd);
      await assert.rejects(parse('flowchart TD\nA[未闭合'));
      assert.equal((await parse('flowchart TD\nA --> B')).kind, 'legacy');
      await source(special[0]);
      await page.waitForSelector('.wb-mermaid-svg svg');
      assert.equal(await page.$('.wb-legacy-flow'), null);
      await source('flowchart TD\nA[未闭合');
      await page.waitForSelector('[role="alert"]');
      assert.equal(await page.$('.react-flow__edge'), null);
    });

    await t.test('full labels are measured before layout; source switching and focus work', async () => {
      const long = '资料完整性与冲突检查，保留所有原始证据、明细、规则以及完整的条件说明。'.repeat(4);
      const mmd = `flowchart TD\nA([起点]) --> B["${long}"]\nB --> C{核对}\nC -->|通过| D([完成])\nC -.->|补充| E[补充材料]\nE --> B`;
      await source(mmd); await waitGraph(long);
      assert.equal(await page.$$eval('.react-flow__node', (nodes) => nodes.length), 5);
      assert.equal(await page.$$eval('.react-flow__edge', (edges) => edges.length), 5);
      const measurements = await page.$$eval('.fc-node__label', (labels) => labels.map((label) => ({
        text: label.textContent, client: label.clientHeight, scroll: label.scrollHeight,
        clamp: getComputedStyle(label).webkitLineClamp,
      })));
      assert.ok(measurements.every((item) => item.scroll <= item.client + 1));
      assert.ok(measurements.find((item) => item.text === long).client > 100);
      const overlaps = await page.$$eval('.react-flow__node', (nodes) => {
        const boxes = nodes.map((node) => node.getBoundingClientRect());
        return boxes.some((a, i) => boxes.slice(i + 1).some((b) =>
          Math.min(a.right, b.right) > Math.max(a.left, b.left) + 1
          && Math.min(a.bottom, b.bottom) > Math.max(a.top, b.top) + 1));
      });
      assert.equal(overlaps, false);
      await page.click('[role="tab"]:nth-child(2)');
      assert.equal(await page.$eval('.wb-flow-source', (node) => node.textContent), mmd);
      await page.click('[role="tab"]:first-child'); await waitGraph(long);
      await page.evaluate(() => window.previewTest.setFocus('D'));
      await page.waitForFunction(() => document.querySelector('[data-id="D"] .fc-node.is-selected'));
      await page.waitForFunction(() => {
        const d = document.querySelector('[data-id="D"]').getBoundingClientRect();
        const c = document.querySelector('.wb-legacy-flow').getBoundingClientRect();
        return d.top >= c.top && d.bottom <= c.bottom;
      });
      await page.click('[data-id="D"] .fc-node');
      await page.waitForSelector('[aria-label="节点属性"]');
      await page.click('[aria-label="关闭节点属性"]');
      await page.keyboard.press('Delete');
      assert.equal(await page.$$eval('.react-flow__node', (nodes) => nodes.length), 5);
      await page.evaluate(() => window.previewTest.setFocus(''));
    });

    await t.test('zoom, simultaneous candidate preview, source updates and narrow screens', async () => {
      await page.click('[aria-label="缩小流程图"]');
      await page.waitForFunction(() => document.querySelector('.wb-flow-zoom span').textContent !== '100%');
      const zoom = await page.$eval('.wb-flow-zoom span', (node) => node.textContent);
      await page.setViewport({ width: 1200, height: 900 });
      assert.equal(await page.$eval('.wb-flow-zoom span', (node) => node.textContent), zoom);
      await page.click('[aria-label="查看全图"]');
      await page.click('[aria-label="恢复 100%"]');
      await page.waitForFunction(() => document.querySelector('.wb-flow-zoom span').textContent === '100%');
      await page.evaluate(() => window.previewTest.setSecond(true));
      await page.waitForFunction(() => document.querySelectorAll('.wb-legacy-flow[data-layout-ready="true"]').length === 2);
      await source('flowchart RL\nA[新的正文] --> B[新的结果]'); await waitGraph('新的正文');
      assert.equal(await page.$$eval('.wb-doc .react-flow__node', (nodes) => nodes.length), 2);
      assert.equal(await page.$$eval('.wb-diff-preview .react-flow__node', (nodes) => nodes.length), 2);
      const centers = await page.$$eval('.wb-doc .react-flow__node', (nodes) => nodes.map((node) => ({ id: node.dataset.id, x: node.getBoundingClientRect().x })));
      assert.ok(centers.find((node) => node.id === 'A').x > centers.find((node) => node.id === 'B').x);
      await page.evaluate(() => window.previewTest.setSecond(false));
      await page.setViewport({ width: 390, height: 844 });
      await page.click('[aria-label="流程图适合窗口"]');
      assert.ok(await page.$eval('.wb-legacy-flow', (node) => node.getBoundingClientRect().right <= 390));
      assert.ok(await page.$eval('[aria-label="恢复 100%"]', (node) => node.getBoundingClientRect().right <= 390));
    });
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
