import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  DATA_SOURCE_TOUR_TARGETS,
  ONTOLOGY_ID_SYNC_BUTTON_LABEL,
  dataSourceTourSteps,
} from '../frontend/src/data-source-tour.js';

const selector = (target: string) => `[data-tour="${target}"]`;

test('数据源配置引导：不同 Provider 覆盖各自全部可见配置控件', () => {
  const commonNl2SqlTargets = [
    DATA_SOURCE_TOUR_TARGETS.provider,
    DATA_SOURCE_TOUR_TARGETS.url,
    DATA_SOURCE_TOUR_TARGETS.scene,
    DATA_SOURCE_TOUR_TARGETS.token,
    DATA_SOURCE_TOUR_TARGETS.evaluationWorkspace,
    DATA_SOURCE_TOUR_TARGETS.evalDbEngine,
    DATA_SOURCE_TOUR_TARGETS.evalDbHost,
    DATA_SOURCE_TOUR_TARGETS.evalDbPort,
    DATA_SOURCE_TOUR_TARGETS.evalDbDatabase,
    DATA_SOURCE_TOUR_TARGETS.evalDbUser,
    DATA_SOURCE_TOUR_TARGETS.evalDbPassword,
  ];
  const neo4jTargets = [
    DATA_SOURCE_TOUR_TARGETS.provider,
    DATA_SOURCE_TOUR_TARGETS.neo4jUri,
    DATA_SOURCE_TOUR_TARGETS.neo4jDatabase,
    DATA_SOURCE_TOUR_TARGETS.neo4jUser,
    DATA_SOURCE_TOUR_TARGETS.neo4jPassword,
    DATA_SOURCE_TOUR_TARGETS.workspaceProperty,
    DATA_SOURCE_TOUR_TARGETS.workspaceValue,
    DATA_SOURCE_TOUR_TARGETS.ontologyProperty,
    DATA_SOURCE_TOUR_TARGETS.ontologyValue,
  ];

  assert.deepEqual(
    dataSourceTourSteps('nl2sql_template').map((step) => step.sel),
    commonNl2SqlTargets.map(selector),
  );
  assert.deepEqual(
    dataSourceTourSteps('nl2sql_http').map((step) => step.sel),
    commonNl2SqlTargets.map(selector),
  );
  assert.deepEqual(
    dataSourceTourSteps('neo4j').map((step) => step.sel),
    neo4jTargets.map(selector),
  );
});

test('数据源配置引导：Provider 说明不同且包含关键填写约束', () => {
  const templateText = dataSourceTourSteps('nl2sql_template').map((step) => step.body).join('\n');
  const httpText = dataSourceTourSteps('nl2sql_http').map((step) => step.body).join('\n');
  const neo4jText = dataSourceTourSteps('neo4j').map((step) => step.body).join('\n');

  assert.match(templateText, /模板/);
  assert.match(templateText, /Token.*必填/);
  assert.match(httpText, /直接调用/);
  assert.match(httpText, /Token.*可留空/);
  assert.match(templateText, /MySQL.*PostgreSQL/);
  assert.match(httpText, /工作空间 ID/);
  assert.equal(ONTOLOGY_ID_SYNC_BUTTON_LABEL, '同步知识包id');
  assert.match(neo4jText, /“同步知识包id”/);
  assert.doesNotMatch(neo4jText, /自动同步知识包id/);
  assert.notEqual(templateText, httpText);
});
