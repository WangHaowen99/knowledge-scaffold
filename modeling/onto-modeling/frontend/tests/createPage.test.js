import assert from 'node:assert/strict';
import test from 'node:test';

import { limitProjectName, projectNameLengthState } from '../src/lib/projectName.js';

test('project name count stays below the limit at 199 characters', () => {
  assert.deepEqual(projectNameLengthState('名'.repeat(199)), {
    length: 199,
    atLimit: false,
    label: '199 / 200',
  });
});

test('project name count reports the limit at 200 characters', () => {
  assert.deepEqual(projectNameLengthState('名'.repeat(200)), {
    length: 200,
    atLimit: true,
    label: '200 / 200',
  });
});

test('the 201st project name character is rejected consistently', () => {
  const value = limitProjectName('名'.repeat(201));
  assert.equal(value.length, 200);
  assert.deepEqual(projectNameLengthState(value), {
    length: 200,
    atLimit: true,
    label: '200 / 200',
  });
});

test('emoji use the same Unicode code point length as the backend', () => {
  const value = limitProjectName('😀'.repeat(201));
  assert.equal([...value].length, 200);
  assert.equal(value, '😀'.repeat(200));
  assert.deepEqual(projectNameLengthState(value), {
    length: 200,
    atLimit: true,
    label: '200 / 200',
  });
});
