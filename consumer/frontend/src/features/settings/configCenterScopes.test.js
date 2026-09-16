// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import { CONFIG_CENTER_SCOPES } from './configCenterScopes.js';

test('observability configuration is exposed only at workspace scope', () => {
  const globalSectionIds = CONFIG_CENTER_SCOPES.global.sections.map(({ id }) => id);
  const workspaceSectionIds = CONFIG_CENTER_SCOPES.workspace.sections.map(({ id }) => id);

  assert.equal(globalSectionIds.includes('observability'), false);
  assert.equal(globalSectionIds.includes('system'), true);
  assert.equal(workspaceSectionIds.includes('wsobs'), true);
  assert.equal(workspaceSectionIds.includes('conn'), false);
  assert.equal(workspaceSectionIds.includes('bind'), false);
});
