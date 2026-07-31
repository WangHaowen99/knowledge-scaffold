import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';

import { ScenarioPanel } from '../frontend/src/WorkflowExtraPanels.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');
const render = (component: unknown, props: Record<string, unknown>) => renderToStaticMarkup(
  React.createElement(component, props),
);

test('场景面板只显示短操作文案', () => {
  const scenario = render(ScenarioPanel, {
    projectId: 'pj_one',
    step: { status: 'waiting_review' },
    progress: { output: { scenarios: [{ name: '产品推荐', input: '客户号', expected: '产品清单' }] } },
  });
  assert.match(scenario, /重新推演/);
  assert.match(scenario, /去确认/);
});
